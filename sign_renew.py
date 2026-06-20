#!/usr/bin/env python3
"""
FreeCloud Panel (panel.freecloud.ltd) 自动签到 + 续费脚本
使用 SeleniumBase UC Mode 绕过 Cloudflare Turnstile

功能：
  1. SeleniumBase UC Mode 自动绕过 Cloudflare Turnstile
     ── 登录页 URL 本身就带 CF 拦截，uc_open_with_reconnect 打开时立刻处理
  2. 邮箱 + 密码登录
  3. 向下滚动找到"每日签到 (+1积分)"绿色按钮并点击
  4. 检测服务到期日，到期前 N 天自动续费
  5. WxPusher / Telegram 推送结果
  6. Xray/V2Ray SOCKS5 代理（与 runfreecloud 相同方案，--proxy-server 传给 Chrome）

环境变量：
  EMAIL                - 登录邮箱
  PASSWORD             - 登录密码
  V2RAY_CONFIG         - Xray config.json（由 workflow 写入文件后启动代理）
  PROXY                - SOCKS5 代理地址，默认 socks5://127.0.0.1:10808
  WXPUSHER_TOKEN       - WxPusher AppToken（可选）
  WXPUSHER_UID         - WxPusher UID，多个用逗号分隔（可选）
  TG_BOT_TOKEN         - Telegram Bot Token（可选）
  TG_CHAT_ID           - Telegram Chat ID（可选）
  RENEW_THRESHOLD_DAYS - 到期前几天续费，默认 2
"""

import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

from seleniumbase import Driver

# ── 环境变量 ──────────────────────────────────────────────
EMAIL                = os.environ.get("EMAIL", "").strip()
PASSWORD             = os.environ.get("PASSWORD", "").strip()
WXPUSHER_TOKEN       = os.environ.get("WXPUSHER_TOKEN", "").strip()
WXPUSHER_UID         = os.environ.get("WXPUSHER_UID", "").strip()
TG_BOT_TOKEN         = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID           = os.environ.get("TG_CHAT_ID", "").strip()
PROXY                = os.environ.get("PROXY", "socks5://127.0.0.1:10808").strip()
RENEW_THRESHOLD_DAYS = int(os.environ.get("RENEW_THRESHOLD_DAYS", "2"))

BASE_URL     = "https://panel.freecloud.ltd"
LOGIN_URL    = f"{BASE_URL}/index.php?rp=/login"
CLIENTAREA   = f"{BASE_URL}/clientarea.php"
SERVICES_URL = f"{BASE_URL}/clientarea.php?action=services"
RENEWALS_URL = f"{BASE_URL}/index.php?rp=/service-renewals"

SCREENSHOT_DIR   = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)
REC_FRAME_DIR    = Path("screenshots/rec")
REC_FRAME_DIR.mkdir(exist_ok=True)
RECORDING_DIR    = Path("recordings")
RECORDING_DIR.mkdir(exist_ok=True)
ENABLE_RECORDING = os.environ.get("ENABLE_RECORDING", "true").strip().lower() == "true"

# ── 日志 ──────────────────────────────────────────────────
def log(msg):  print(f"[INFO]  {msg}", flush=True)
def warn(msg): print(f"[WARN]  {msg}", flush=True)
def err(msg):  print(f"[ERROR] {msg}", flush=True)

# ── 截图 ──────────────────────────────────────────────────
def snap(sb, name: str) -> str | None:
    try:
        path = str(SCREENSHOT_DIR / f"{name}.png")
        sb.save_screenshot(path)
        log(f"📸 截图: {path}")
        return path
    except Exception as e:
        warn(f"截图失败: {e}")
        return None

# ── WxPusher 推送 ─────────────────────────────────────────
def send_wx(title: str, content: str):
    if not WXPUSHER_TOKEN or not WXPUSHER_UID:
        return
    uids = [u.strip() for u in WXPUSHER_UID.split(",") if u.strip()]
    payload = {
        "appToken":    WXPUSHER_TOKEN,
        "content":     content,
        "summary":     title,
        "contentType": 1,
        "uids":        uids,
    }
    for attempt in range(3):
        try:
            req = Request(
                "https://wxpusher.zjiecode.com/api/send/message",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if result.get("success"):
                    log("WxPusher 推送成功")
                    return
                else:
                    warn(f"WxPusher 返回异常: {result.get('msg')}")
                    return
        except Exception as e:
            warn(f"WxPusher 推送失败 [{attempt+1}/3]: {e}")
            if attempt < 2:
                time.sleep(3)

# ── Telegram 推送 ─────────────────────────────────────────
def send_tg(text: str, img_path: str | None = None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        if img_path and Path(img_path).exists():
            img_bytes = Path(img_path).read_bytes()
            boundary  = "----FreecloudBoundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TG_CHAT_ID}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{text}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"snap.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n"
            ).encode() + img_bytes + f"\r\n--{boundary}--\r\n".encode()
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        else:
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                data=json.dumps({"chat_id": TG_CHAT_ID, "text": text}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urlopen(req, timeout=20):
            log("TG 推送成功")
    except Exception as e:
        warn(f"TG 推送失败: {e}")

def send_notify(title: str, content: str, img_path: str | None = None):
    send_tg(f"{title}\n\n{content}", img_path)
    send_wx(title, content)

# ── 等待 URL 关键字 ───────────────────────────────────────
def wait_for_url(sb, keyword: str, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if keyword in sb.get_current_url():
            return True
        time.sleep(0.5)
    return False

# ── Cloudflare Managed Challenge 处理（核心）────────────

def _js_click_cf_checkbox(sb) -> bool:
    result = sb.execute_script("""
        var cb = document.querySelector('input[type="checkbox"]');
        if (cb) { cb.click(); return 'direct-checkbox'; }

        function deepQuery(root, sel) {
            var el = root.querySelector(sel);
            if (el) return el;
            var all = root.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                if (all[i].shadowRoot) {
                    var found = deepQuery(all[i].shadowRoot, sel);
                    if (found) return found;
                }
            }
            return null;
        }
        var cb2 = deepQuery(document, 'input[type="checkbox"]');
        if (cb2) { cb2.click(); return 'shadow-checkbox'; }

        var widget = document.querySelector(
            '[class*="cf-turnstile"], [id*="cf-turnstile"], ' +
            '[class*="challenge"], [id*="challenge-stage"], ' +
            '[class*="chl-body"]'
        );
        if (widget) { widget.click(); return 'widget-click:' + widget.tagName; }

        return 'not_found';
    """)
    log(f"  [JS点击] 结果: {result}")
    return result != 'not_found'


def _pyautogui_click_cf(sb) -> bool:
    try:
        import pyautogui
        pyautogui.FAILSAFE = False

        try:
            dims = sb.execute_script("""
                return {
                    outerH: window.outerHeight,
                    innerH: window.innerHeight,
                    screenX: window.screenX || 0,
                    screenY: window.screenY || 0
                };
            """)
            toolbar_h = dims['outerH'] - dims['innerH']
            win_x     = dims['screenX']
            win_y     = dims['screenY']
        except Exception:
            toolbar_h, win_x, win_y = 74, 0, 0

        cb_css_x = 126
        cb_css_y = 336

        screen_x = win_x + cb_css_x
        screen_y = win_y + toolbar_h + cb_css_y

        log(f"  [pyautogui] toolbar={toolbar_h}, win=({win_x},{win_y}), 点击=({screen_x},{screen_y})")
        pyautogui.moveTo(screen_x, screen_y, duration=0.4)
        time.sleep(0.3)
        pyautogui.click(screen_x, screen_y)
        log("  [pyautogui] 点击完成")
        return True

    except ImportError:
        warn("  pyautogui 未安装")
        return False
    except Exception as e:
        warn(f"  [pyautogui] 异常: {e}")
        return False


def handle_cloudflare(sb, timeout: int = 90):
    passed_keywords = [
        "邮件地址", "邮箱", "密码", "登录用户中心",
        "仪表盘", "产品服务", "财务管理", "购买服务",
        "clientarea",
    ]

    def already_passed():
        try:
            cur = sb.get_current_url()
            pg  = sb.execute_script("return document.body.innerText || '';")
            return any(kw in pg for kw in passed_keywords) or "clientarea" in cur
        except Exception:
            return False

    def is_cf_page():
        try:
            cur  = sb.get_current_url()
            page = sb.execute_script("return document.body.innerText || '';")
            return (
                "challenge" in cur
                or "performing security verification" in page.lower()
                or "verify you are human" in page.lower()
                or "just a moment" in page.lower()
                or "checking your browser" in page.lower()
            )
        except Exception:
            return False

    MAX_RETRIES = 8
    deadline    = time.time() + timeout

    for attempt in range(1, MAX_RETRIES + 1):
        if time.time() > deadline:
            break

        if already_passed():
            log("✅ Cloudflare 验证已通过")
            return True

        if not is_cf_page():
            time.sleep(1)
            if already_passed():
                log("✅ Cloudflare 验证已通过")
                return True
            if not is_cf_page():
                continue

        log(f"检测到 CF 验证页（第 {attempt}/{MAX_RETRIES} 次）...")

        time.sleep(2)

        if _js_click_cf_checkbox(sb):
            time.sleep(4)
            if already_passed():
                log("✅ CF 已通过（策略A JS点击）")
                return True

        if _pyautogui_click_cf(sb):
            time.sleep(4)
            if already_passed():
                log("✅ CF 已通过（策略B 坐标点击）")
                return True
            time.sleep(3)
            if already_passed():
                log("✅ CF 已通过（策略B 延迟）")
                return True

        try:
            sb.uc_gui_click_captcha()
            log("  [策略C] uc_gui_click_captcha 完成")
            time.sleep(4)
            if already_passed():
                log("✅ CF 已通过（策略C）")
                return True
        except Exception as e:
            warn(f"  [策略C] uc_gui_click_captcha 异常: {e}")

        warn(f"  第 {attempt} 次未通过，重新导航后重试...")
        try:
            cur_url = sb.get_current_url()
            sb.uc_open_with_reconnect(cur_url, reconnect_time=3)
            time.sleep(3)
        except Exception:
            time.sleep(5)

    warn("Cloudflare 处理超时，继续尝试...")
    return False

# ── 导航到指定 URL 并处理 CF ─────────────────────────────

def mask_pii(sb):
    sb.execute_script("""
        function maskEl(el) {
            if (el.dataset.piiMasked) return;
            el.dataset.piiOrig = el.innerHTML;
            el.dataset.piiMasked = '1';
            el.childNodes.forEach(function(n) {
                if (n.nodeType === 3 && /[A-Za-z]/.test(n.textContent)) {
                    n.textContent = n.textContent.replace(/[A-Za-z0-9][A-Za-z0-9\s,\.#-]{2,}/g, '████');
                }
            });
        }
        document.querySelectorAll('.card h5, .card h4, .card p, .card address, .card-body p, .card-body h5')
            .forEach(maskEl);
        document.querySelectorAll('.navbar-right a, .navbar .dropdown > a, .navbar .dropdown-toggle, [id*="nav"] .dropdown-toggle')
            .forEach(maskEl);
        document.querySelectorAll('*').forEach(function(el) {
            if (el.children.length === 0) {
                var t = el.textContent || '';
                if (/#\d+\s+[A-Z]/.test(t)) maskEl(el.parentElement || el);
            }
        });
    """)

def unmask_pii(sb):
    sb.execute_script("""
        document.querySelectorAll('[data-pii-masked]').forEach(function(el) {
            if (el.dataset.piiOrig !== undefined) {
                el.innerHTML = el.dataset.piiOrig;
                delete el.dataset.piiOrig;
                delete el.dataset.piiMasked;
            }
        });
    """)

def snap_safe(sb, name: str):
    mask_pii(sb)
    snap(sb, name)
    unmask_pii(sb)

def goto(sb, url: str, reconnect_time: int = 3):
    log(f"导航: {url}")
    sb.uc_open_with_reconnect(url, reconnect_time=reconnect_time)
    time.sleep(2)
    handle_cloudflare(sb)

# ── 关闭弹窗 ─────────────────────────────────────────────
def dismiss_popups(sb):
    for _ in range(3):
        result = sb.execute_script("""
            var btns = document.querySelectorAll('button, [role="button"], a');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].innerText || '').trim().toLowerCase();
                if (t === 'close' || t === '×' || t === 'x' || t === '关闭') {
                    btns[i].click();
                    return 'closed:' + btns[i].innerText.trim();
                }
            }
            return 'none';
        """)
        if result == "none":
            break
        log(f"关闭弹窗: {result}")
        time.sleep(0.5)

# ── 登录 ─────────────────────────────────────────────────
def login(sb) -> bool:
    goto(sb, LOGIN_URL, reconnect_time=3)
    snap(sb, "01_login_page")

    cur = sb.get_current_url()
    page = sb.execute_script("return document.body.innerText || '';")

    already_logged = (
        "clientarea" in cur and "rp=/login" not in cur
    ) or any(kw in page for kw in ["仪表盘", "Hello,", "您好,", "My Account"])
    if already_logged:
        log("✅ 已是登录状态，跳过登录")
        return True

    log("填写登录表单...")

    filled_email = sb.execute_script(f"""
        var el = document.querySelector('input[name="username"]')
               || document.querySelector('#inputEmail')
               || document.querySelector('input[type="email"]')
               || document.querySelector('input[placeholder*="邮"]');
        if (!el) return 'no_email_field';
        el.focus();
        el.value = {json.dumps(EMAIL)};
        el.dispatchEvent(new Event('input',  {{bubbles:true}}));
        el.dispatchEvent(new Event('change', {{bubbles:true}}));
        return 'ok:' + (el.name || el.id);
    """)
    log(f"邮箱填写: {filled_email}")
    time.sleep(0.4)

    filled_pass = sb.execute_script(f"""
        var el = document.querySelector('input[name="password"]')
               || document.querySelector('input[type="password"]');
        if (!el) return 'no_pass_field';
        el.focus();
        el.value = {json.dumps(PASSWORD)};
        el.dispatchEvent(new Event('input',  {{bubbles:true}}));
        el.dispatchEvent(new Event('change', {{bubbles:true}}));
        return 'ok';
    """)
    log(f"密码填写: {filled_pass}")
    time.sleep(0.4)

    sb.execute_script("""
        var el = document.querySelector('input[name="username"]') || document.querySelector('#inputEmail');
        if (el) { el.setAttribute('data-real', el.value); el.value = '●●●●●●●●●●●●'; el.type = 'text'; }
    """)
    snap(sb, "01b_before_submit")
    sb.execute_script("""
        var el = document.querySelector('input[name="username"]') || document.querySelector('#inputEmail');
        if (el && el.getAttribute('data-real')) { el.value = el.getAttribute('data-real'); el.removeAttribute('data-real'); }
    """)

    clicked = sb.execute_script("""
        var btn = document.querySelector('#login')
                  || document.querySelector('button[type="submit"]')
                  || document.querySelector('input[type="submit"]');
        if (btn) { btn.click(); return 'clicked:' + (btn.id || btn.type); }
        return 'not_found';
    """)
    log(f"登录按钮点击: {clicked}")

    deadline = time.time() + 20
    while time.time() < deadline:
        cur = sb.get_current_url()
        if "clientarea" in cur:
            break
        page = sb.execute_script("return document.body.innerText || '';")
        if "just a moment" in page.lower() or "verify you are human" in page.lower():
            log("登录后遇到 CF，再次处理...")
            handle_cloudflare(sb)
        time.sleep(1)

    cur = sb.get_current_url()
    page = sb.execute_script("return document.body.innerText || '';")
    if (("clientarea" in cur and "rp=/login" not in cur)
            or any(kw in page for kw in ["仪表盘", "Hello,", "您好,", "My Account"])):
        log("✅ 登录成功")
        snap_safe(sb, "02_after_login")
        return True

    warn(f"登录失败，当前: {cur}")
    snap(sb, "02_login_failed")
    return False

# ── 每日签到 ─────────────────────────────────────────────
def do_checkin(sb) -> str | None:
    log("前往用户中心页面（签到）...")
    goto(sb, CLIENTAREA, reconnect_time=2)
    dismiss_popups(sb)
    snap_safe(sb, "03_clientarea")

    log("向下滚动寻找签到按钮...")
    found = False
    for scroll_y in [300, 600, 900, 1200, 1500, 2000, 2500]:
        sb.execute_script(f"window.scrollTo(0, {scroll_y});")
        time.sleep(0.7)

        result = sb.execute_script("""
            var all = document.querySelectorAll('button, a, div[onclick], span[onclick], input[type="button"]');
            for (var i = 0; i < all.length; i++) {
                var t = (all[i].innerText || all[i].textContent || all[i].value || '').trim();
                if (t.includes('每日签到') || t.includes('+1积分') || t === '签到') {
                    var style = getComputedStyle(all[i]);
                    var rect  = all[i].getBoundingClientRect();
                    if (style.display !== 'none' && style.visibility !== 'hidden' && rect.height > 0) {
                        all[i].scrollIntoView({block:'center'});
                        all[i].click();
                        return 'clicked:' + t;
                    }
                }
            }
            return 'not_found';
        """)

        if result and "not_found" not in str(result):
            log(f"✅ 点击签到按钮: {result}")
            found = True
            time.sleep(2)

            for _ in range(5):
                confirm = sb.execute_script("""
                    var btns = document.querySelectorAll('button, a');
                    for (var i = 0; i < btns.length; i++) {
                        var t = (btns[i].innerText || '').trim();
                        if ((t === '确定' || t === 'OK' || t === '确认') && btns[i].offsetParent !== null) {
                            btns[i].click();
                            return 'confirmed:' + t;
                        }
                    }
                    return 'none';
                """)
                if confirm == "none":
                    break
                log(f"弹窗确认: {confirm}")
                time.sleep(1)
            break

    if not found:
        page = sb.execute_script("return document.body.innerText || '';")
        if "今日已签" in page or "已签到" in page:
            log("今日已签到过了")
        else:
            warn("未找到签到按钮，截图记录")
            snap(sb, "03b_no_checkin_btn")

    sb.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.5)
    snap_safe(sb, "03c_after_checkin")

    time.sleep(1)
    sb.refresh()
    time.sleep(2)

    balance = read_credit_balance(sb)
    log(f"当前积分: {balance}")
    return balance


def read_credit_balance(sb) -> str | None:
    """读取页面上当前的积分余额（用于签到后、续费后等多处复用）。"""
    page = sb.execute_script("return document.body.innerText || '';")

    balance = sb.execute_script("""
        var els = document.querySelectorAll('*');
        for (var i = 0; i < els.length; i++) {
            var t = els[i].childNodes.length === 1 && els[i].childNodes[0].nodeType === 3
                    ? els[i].innerText.trim() : '';
            var m = t.match(/([\d.]+)\s*积分/);
            if (m) return m[1];
        }
        return null;
    """)
    if not balance:
        bal = re.search(r'([\d.]+)\s*积分', page)
        balance = bal.group(1) if bal else None
    return balance

# ── 读取服务到期日 ────────────────────────────────────────
def get_service_expiry(sb) -> tuple[str | None, int | None]:
    log("前往服务页面读取到期日...")
    goto(sb, SERVICES_URL, reconnect_time=2)
    snap_safe(sb, "04_services_page")

    expiry_str = sb.execute_script("""
        var tables = document.querySelectorAll('table');
        for (var t = 0; t < tables.length; t++) {
            var headers = tables[t].querySelectorAll('th');
            var colIdx = -1;
            for (var i = 0; i < headers.length; i++) {
                var ht = (headers[i].innerText || '').trim();
                if (ht.includes('付款') || ht.includes('到期') || ht.includes('Next') || ht.includes('Due')) {
                    colIdx = i; break;
                }
            }
            if (colIdx >= 0) {
                var rows = tables[t].querySelectorAll('tbody tr');
                for (var r = 0; r < rows.length; r++) {
                    var tds = rows[r].querySelectorAll('td');
                    if (tds[colIdx]) {
                        var ct = tds[colIdx].innerText.trim();
                        var m = ct.match(/(\d{4})[\/\-](\d{2})[\/\-](\d{2})/);
                        if (m) return m[1] + '-' + m[2] + '-' + m[3];
                    }
                }
            }
        }
        var rows = document.querySelectorAll('tr');
        for (var r = 0; r < rows.length; r++) {
            var rt = rows[r].innerText || '';
            if (rt.includes('有效') || rt.includes('Active')) {
                var m = rt.match(/(\d{4})[\/\-](\d{2})[\/\-](\d{2})/);
                if (m) return m[1] + '-' + m[2] + '-' + m[3];
            }
        }
        return null;
    """)

    if not expiry_str:
        page = sb.execute_script("return document.body.innerText || '';")
        m = re.search(r'(\d{4})[/\-](\d{2})[/\-](\d{2})', page)
        if m:
            expiry_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    if not expiry_str:
        log("未找到服务到期日")
        return None, None

    try:
        expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
        remain = (expiry - datetime.now()).days
        log(f"服务到期日: {expiry_str}，剩余 {remain} 天")
        return expiry_str, remain
    except Exception as e:
        warn(f"日期解析失败: {e}")
        return expiry_str, None

# ── 关闭所有弹窗 ──────────────────────────────────────────
def close_popups(sb) -> int:
    closed = sb.execute_script("""
        var count = 0;

        var closeBtns = document.querySelectorAll(
            '[data-dismiss="modal"], [data-bs-dismiss="modal"], ' +
            '.modal .close, .modal .btn-close, ' +
            '.modal-header .close, .modal-footer .btn-secondary'
        );
        closeBtns.forEach(function(btn) {
            if (btn.offsetParent !== null) { btn.click(); count++; }
        });

        var modals = document.querySelectorAll('.modal.show, .modal[style*="display: block"]');
        modals.forEach(function(m) {
            m.style.display = 'none';
            m.classList.remove('show');
            count++;
        });

        var backdrops = document.querySelectorAll('.modal-backdrop');
        backdrops.forEach(function(b) { b.remove(); count++; });

        document.body.classList.remove('modal-open');
        document.body.style.overflow = '';
        document.body.style.paddingRight = '';

        var alerts = document.querySelectorAll(
            '.cookie-bar, .alert-dismissible .close, ' +
            '.toast.show .btn-close, .notification .close'
        );
        alerts.forEach(function(el) {
            if (el.offsetParent !== null) { el.click(); count++; }
        });

        return count;
    """)
    if closed:
        log(f"  [弹窗] 关闭了 {closed} 个弹窗/遮罩")
        time.sleep(0.5)
    return closed or 0


# ── 滚动到元素并点击 ──────────────────────────────────────
def scroll_and_click(sb, js_find_and_click: str) -> str:
    sb.execute_script(f"""
        var el = (function(){{ {js_find_and_click} }})();
        if (el) el.scrollIntoView({{behavior:'smooth', block:'center'}});
    """)
    time.sleep(0.8)
    close_popups(sb)
    time.sleep(0.3)
    result = sb.execute_script(f"""
        var el = (function(){{ {js_find_and_click} }})();
        if (el && el.offsetParent !== null) {{ el.click(); return 'clicked:' + (el.innerText || el.value || el.id || '').trim().slice(0,30); }}
        if (el) {{ el.click(); return 'forced:' + (el.innerText || el.id || '').trim().slice(0,30); }}
        return 'not_found';
    """)
    return str(result)


# ── 续费服务 ─────────────────────────────────────────────
def do_renew(sb) -> bool:
    """
    前往续费页（service-renewals）：
      1. 关闭所有弹窗
      2. 点击"添加到购物车"按钮（.btn-add-renewal-to-cart）
      3. 关闭弹窗，点击 sidebar 的"继续"按钮跳转购物车
      4. 勾选 TOS checkbox（#tos-checkbox）
      5. 点击购物车页的"结账"按钮（button#checkout）
    """
    log("前往续费服务页面...")
    goto(sb, RENEWALS_URL, reconnect_time=2)
    time.sleep(2)

    close_popups(sb)
    snap(sb, "05_renewals_page")

    # ── 步骤1：点"添加到购物车" ──────────────────────────
    log("尝试点击'添加到购物车'...")
    add_cart = scroll_and_click(sb, """
        var el = document.querySelector('.btn-add-renewal-to-cart, [class*="btn-add-renewal"]');
        if (el) return el;
        var btns = document.querySelectorAll('button, a, input[type="submit"]');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || btns[i].value || '').trim();
            if (t.includes('添加到购物车') || t.includes('加入购物车') || t.toLowerCase().includes('add to cart')) {
                return btns[i];
            }
        }
        return null;
    """)
    log(f"添加购物车: {add_cart}")

    if "not_found" in add_cart:
        warn("未找到'添加到购物车'按钮")
        snap(sb, "05b_no_add_cart")
        return False

    # 等待弹窗出现再关闭
    time.sleep(1.5)
    close_popups(sb)
    time.sleep(0.5)
    close_popups(sb)
    snap(sb, "05b_after_add_cart")

    # ── 步骤2：点续费页 sidebar 的"继续"按钮 ──────────────
    log("点击续费页的'继续'按钮...")
    sb.execute_script("window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'});")
    time.sleep(1)
    close_popups(sb)

    continue_btn = scroll_and_click(sb, """
        // 精确 selector：续费页 sidebar checkout 链接里的 span
        var el = document.querySelector('a#checkout span');
        if (el && el.offsetParent !== null) return el;
        var el2 = document.querySelector('a#checkout');
        if (el2 && el2.offsetParent !== null) return el2;
        // 兜底：按文字匹配
        var btns = document.querySelectorAll('button, a, input[type="submit"]');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || btns[i].value || '').trim();
            if (t === '继续' || t === 'Continue' || t.toLowerCase() === 'continue') {
                return btns[i];
            }
        }
        return null;
    """)
    log(f"继续按钮: {continue_btn}")

    # ── 步骤3：等待跳转到购物车页 ────────────────────────
    time.sleep(2)
    if "cart.php" not in sb.get_current_url():
        log("未跳转到购物车，直接导航...")
        goto(sb, f"{BASE_URL}/cart.php?a=checkout", reconnect_time=2)
        time.sleep(2)

    close_popups(sb)
    snap(sb, "05c_cart_checkout_page")

    # ── 步骤4：勾选 TOS checkbox（iCheck 插件，需点 label/helper） ──
    # 该站点的 TOS checkbox 由 iCheck 插件渲染：
    #   <div id="tos-checkbox"><label><input type="checkbox" style="visibility:hidden">
    #     <ins class="iCheck-helper" style="position:absolute;top:-40%;left:-40%;width:180px;height:180px">
    #   </label></div>
    # 原生 input 被 iCheck 设为 visibility:hidden 且脱离正常点击区域，
    # 直接对它调 .click() 不一定能让 iCheck 同步内部状态/视觉。
    # 必须点击 label 或 .iCheck-helper（用户真实点击会走的路径）。
    log("勾选服务条款 checkbox（iCheck label/helper）...")
    tos_result = sb.execute_script("""
        var container = document.querySelector('#tos-checkbox');
        if (!container) return 'no_container';

        var input  = container.querySelector('input[type="checkbox"]');
        var label  = container.querySelector('label');
        var helper = container.querySelector('.iCheck-helper');

        if (input && input.checked) return 'already_checked';

        if (label) {
            label.click();
            return 'clicked:label';
        }
        if (helper) {
            helper.click();
            return 'clicked:helper';
        }
        if (input) {
            input.click();
            return 'clicked:input';
        }
        return 'no_clickable_target';
    """)
    log(f"TOS checkbox 点击: {tos_result}")
    time.sleep(0.6)

    # 校验是否真的勾上了，没有就用 helper 兜底重试一次
    tos_checked = sb.execute_script("""
        var input = document.querySelector('#tos-checkbox input[type="checkbox"]');
        return input ? input.checked : null;
    """)
    log(f"TOS checkbox 勾选状态: {tos_checked}")

    if not tos_checked:
        warn("TOS checkbox 未生效，尝试点击 .iCheck-helper 兜底...")
        sb.execute_script("""
            var helper = document.querySelector('#tos-checkbox .iCheck-helper');
            if (helper) helper.click();
        """)
        time.sleep(0.6)
        tos_checked = sb.execute_script("""
            var input = document.querySelector('#tos-checkbox input[type="checkbox"]');
            return input ? input.checked : null;
        """)
        log(f"TOS checkbox 兜底后状态: {tos_checked}")

        if not tos_checked:
            warn("TOS checkbox 仍未勾选，最后兜底：直接设置 input.checked 并派发事件")
            sb.execute_script("""
                var input = document.querySelector('#tos-checkbox input[type="checkbox"]');
                if (input) {
                    input.checked = true;
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    input.dispatchEvent(new Event('ifChecked', {bubbles: true}));
                }
            """)
            time.sleep(0.4)

    # ── 步骤5：点购物车页的"结账"按钮 ────────────────────
    log("点击购物车页的'结账'按钮...")
    sb.execute_script("window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'});")
    time.sleep(0.8)
    close_popups(sb)

    checkout = scroll_and_click(sb, """
        // 购物车页结账按钮：button#checkout（DevTools 截图确认）
        var el = document.querySelector('button#checkout');
        if (el) return el;
        // 兜底：按文字
        var btns = document.querySelectorAll('button, a, input[type="submit"]');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || btns[i].value || '').trim();
            if (t === '结账' || t.toLowerCase() === 'checkout') return btns[i];
        }
        return null;
    """)
    log(f"结账按钮: {checkout}")

    if "not_found" in checkout:
        warn("未找到购物车结账按钮")
        snap(sb, "05d_no_checkout_btn")
        return False

    time.sleep(3)
    close_popups(sb)
    snap(sb, "05d_after_checkout")

    cur_url = sb.get_current_url()
    page    = sb.execute_script("return document.body.innerText || '';")
    page_lower = page.lower()

    success = (
        "成功" in page
        or "success" in page_lower
        or "order placed" in page_lower
        or "invoice" in cur_url
        or "complete" in cur_url
    )

    if success:
        log(f"✅ 续费成功，当前: {cur_url}")
        return True

    # 仍停留在 checkout 页，多半是 TOS 未勾选导致表单校验失败未跳转
    if "cart.php" in cur_url and "checkout" in cur_url:
        still_unchecked = sb.execute_script("""
            var input = document.querySelector('#tos-checkbox input[type="checkbox"]');
            return input ? !input.checked : null;
        """)
        if still_unchecked:
            warn("续费失败：仍停留在购物车页，且 TOS checkbox 未勾选")
        else:
            warn(f"续费失败：仍停留在购物车页，TOS 已勾选但未跳转，当前: {cur_url}")
        return False

    warn(f"续费结果不确定，当前: {cur_url}")
    return False

# ── 录屏（截图序列 → ffmpeg MP4）────────────────────────
class ScreenRecorder:
    def __init__(self, sb, interval: float = 2.0):
        self.sb        = sb
        self.interval  = interval
        self._frames: list[Path] = []
        self._running  = False
        self._thread: threading.Thread | None = None
        self._idx      = 0

    def start(self):
        if not ENABLE_RECORDING:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log("🎬 录屏已启动（设 ENABLE_RECORDING=false 可关闭）")

    def _loop(self):
        while self._running:
            try:
                p = REC_FRAME_DIR / f"rec_{self._idx:04d}.png"
                self.sb.save_screenshot(str(p))
                self._frames.append(p)
                self._idx += 1
            except Exception:
                pass
            time.sleep(self.interval)

    def stop(self, name: str = "run") -> str | None:
        if not ENABLE_RECORDING:
            return None
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if not self._frames:
            warn("录屏：无帧，跳过合成")
            return None
        return self._compile(name)

    def _compile(self, name: str) -> str | None:
        if not shutil.which("ffmpeg"):
            warn("ffmpeg 未安装，录屏帧保留在 screenshots/rec/，视频未生成")
            return None
        concat = RECORDING_DIR / "frames.txt"
        with open(concat, "w") as f:
            for p in self._frames:
                f.write(f"file '{p.resolve()}'\n")
                f.write(f"duration {self.interval}\n")
            if self._frames:
                f.write(f"file '{self._frames[-1].resolve()}'\n")
        out = RECORDING_DIR / f"{name}.mp4"
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "10",
            str(out),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                log(f"🎬 录屏已保存: {out}")
                return str(out)
            else:
                warn(f"ffmpeg 合成失败:\n{r.stderr[-400:]}")
                return None
        except Exception as e:
            warn(f"ffmpeg 异常: {e}")
            return None


# ── 主流程 ────────────────────────────────────────────────
def run():
    if not EMAIL or not PASSWORD:
        raise RuntimeError("缺少环境变量: EMAIL 或 PASSWORD")

    log(f"▶ 开始执行 FreeCloud Panel 签到+续费")
    log(f"  代理: {PROXY}")
    log(f"  续费阈值: ≤ {RENEW_THRESHOLD_DAYS} 天")

    proxy_arg = f"--proxy-server={PROXY}"

    driver = Driver(
        uc=True,
        headless=False,
        undetectable=True,
        chromium_arg=(
            f"--no-sandbox,--disable-dev-shm-usage,--disable-gpu,"
            f"--window-size=1280,800,{proxy_arg}"
        ),
    )

    results = {
        "checkin": False,
        "balance": None,
        "balance_after_renew": None,
        "expiry":  None,
        "remain":  None,
        "renewed": False,
    }

    with driver as sb:
        recorder = ScreenRecorder(sb, interval=2.0)
        recorder.start()
        try:
            # ① 登录（包含 CF 处理）
            if not login(sb):
                send_notify("❌ FreeCloud 登录失败", "请检查账号密码或网络/代理")
                recorder.stop("run-login-fail")
                return

            # ② 每日签到
            balance = do_checkin(sb)
            results["balance"] = balance
            results["checkin"] = True

            # ③ 读取服务到期日
            expiry_str, remain = get_service_expiry(sb)
            results["expiry"] = expiry_str
            results["remain"] = remain

            # ④ 续费判断
            if remain is not None and remain <= RENEW_THRESHOLD_DAYS:
                log(f"⚠️ 剩余 {remain} 天 ≤ {RENEW_THRESHOLD_DAYS}，触发续费")
                ok = do_renew(sb)
                results["renewed"] = ok
                if ok:
                    time.sleep(2)
                    new_expiry, new_remain = get_service_expiry(sb)
                    if new_expiry:
                        results["expiry"] = new_expiry
                        results["remain"] = new_remain
                    # 续费后重新读取积分余额，确认确实被扣费（即续费生效）
                    new_balance = read_credit_balance(sb)
                    if new_balance:
                        results["balance_after_renew"] = new_balance
                        log(f"续费后积分: {new_balance}（续费前: {balance}）")
            elif remain is not None:
                log(f"✅ 剩余 {remain} 天，无需续费")
            else:
                log("未能读取到期日，跳过续费")

            snap_safe(sb, "99_final")

        except Exception as e:
            err(f"异常: {e}")
            traceback.print_exc()
            snap_safe(sb, "99_error")
            send_notify("❌ FreeCloud 脚本异常", str(e))
            recorder.stop("run-error")
            sys.exit(1)
        finally:
            video = recorder.stop("run")
            if video:
                log(f"录屏保存于: {video}")

    # ── 汇总推送 ──────────────────────────────────────────
    lines = []
    if results["checkin"]:
        lines.append("✅ 签到成功")
    if results["balance"]:
        lines.append(f"当前积分：{results['balance']} 积分")
    if results["expiry"]:
        lines.append(f"服务到期日：{results['expiry']}")
        if results["renewed"]:
            lines.append("✅ 已自动续期")
            if results["balance_after_renew"]:
                lines.append(f"续费后积分：{results['balance_after_renew']} 积分")
                try:
                    spent = float(results["balance"]) - float(results["balance_after_renew"])
                    if spent > 0:
                        lines.append(f"本次续费花费：{spent:g} 积分")
                except (TypeError, ValueError):
                    pass
        elif results["remain"] is not None:
            renew_date = (
                datetime.strptime(results["expiry"], "%Y-%m-%d") - timedelta(days=RENEW_THRESHOLD_DAYS)
            ).strftime("%Y-%m-%d")
            lines.append(f"剩余 {results['remain']} 天，{renew_date} 前将自动续费")
    else:
        lines.append("⚠️ 未读取到服务到期日")

    content = "\n".join(lines)
    log(f"\n{'='*40}\n{content}\n{'='*40}")
    send_notify("📋 FreeCloud 每日任务", content)


if __name__ == "__main__":
    run()
