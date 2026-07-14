#!/usr/bin/env python3
"""微信讲述人 supervisor —— systemd 常驻进程。

职责：
  1. 启动并守护整个无头栈：Xvfb :99 + openbox + at-spi-bus + 微信(启用无障碍)
  2. 每次启动清除微信登录态，强制显示扫码二维码（满足「每次重启重新扫码」）
  3. 循环检测登录状态：
       - 待登录 → 截二维码到 login_qr.png，网页展示
       - 已登录 → 拉起 narrator（AT-SPI 朗读脚本），网页显示运行中
       - 掉线   → 回到待登录，重新截二维码
  4. 内置 HTTP 服务(默认 :8899) 展示当前二维码 / 状态，浏览器扫码即可

作为 systemd user service 的 ExecStart 前台运行。
"""
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UID = os.getuid()
os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{UID}/bus")

HOME = os.path.expanduser("~")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HOME, ".wechat-narrator")
QR_PATH = os.path.join(STATE_DIR, "login_qr.png")
WAV_DIR = os.path.join(STATE_DIR, "wav")
NARRATOR = os.path.join(SCRIPT_DIR, "wechat_narrator_atspi.py")
GROUP_SCRIPT = os.path.join(SCRIPT_DIR, "wechat_group_to_email.py")
BRIDGE_SCRIPT = os.path.join(SCRIPT_DIR, "wemprss_mail_bridge.py")
XWECHAT = os.path.join(HOME, ".xwechat")

# 群消息 → 邮件 转发配置（发到本机 GreenMail 测试邮箱，经 NAS NAT 供内网访问）
GROUP_KEYWORDS = os.environ.get("NARRATOR_GROUPS", "润珑苑4A1205,南银理财")
DM_KEYWORDS = os.environ.get("NARRATOR_DMS", "灰灰")  # 私聊联系人关键词
MAIL_TO = os.environ.get("NARRATOR_MAIL_TO", "wechat-group@test.local")
MAIL_FROM = os.environ.get("NARRATOR_MAIL_FROM", "wechat-narrator@test.local")
SMTP_HOST = os.environ.get("NARRATOR_SMTP_HOST", "127.0.0.1")
SMTP_PORT = os.environ.get("NARRATOR_SMTP_PORT", "3025")
HTTP_PORT = int(os.environ.get("NARRATOR_HTTP_PORT", "8899"))
LOGIN_WIN = (494, 162, 292, 396)  # 登录窗典型位置/尺寸

os.makedirs(STATE_DIR, exist_ok=True)
_status = {"state": "starting", "since": time.time(), "detail": ""}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def set_status(state, detail=""):
    if _status["state"] != state:
        _status.update(state=state, since=time.time(), detail=detail)
        log(f"状态 → {state} {detail}")
    else:
        _status["detail"] = detail


def running(pattern):
    return subprocess.run(["pgrep", "-f", pattern],
                          stdout=subprocess.DEVNULL).returncode == 0


def spawn(cmd, log_name):
    """setsid 后台启动一个组件，日志写到 STATE_DIR。"""
    logf = open(os.path.join(STATE_DIR, f"{log_name}.log"), "ab")
    subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                     start_new_session=True)


def ensure_stack():
    """幂等拉起 Xvfb / openbox / at-spi / 微信。"""
    if not running("Xvfb :99"):
        spawn(["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp"], "xvfb")
        time.sleep(3)
    if not running("openbox"):
        spawn(["openbox"], "openbox"); time.sleep(1)
    if not running("at-spi-bus-launcher"):
        spawn(["/usr/libexec/at-spi-bus-launcher", "--launch-immediately"], "atspi")
        time.sleep(2)
    if not running("/opt/wechat/wechat"):
        # 每次启动微信前清除登录态 → 强制扫码
        clear_login()
        env = dict(os.environ, QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1", QT_ACCESSIBILITY="1")
        logf = open(os.path.join(STATE_DIR, "wechat.log"), "ab")
        subprocess.Popen(["/usr/bin/wechat"], env=env, stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        time.sleep(13)


def clear_login():
    """移除微信登录凭据，使其显示扫码二维码。"""
    import shutil
    bk = os.path.join(STATE_DIR, "login_backup")
    os.makedirs(bk, exist_ok=True)
    for name in os.listdir(XWECHAT) if os.path.isdir(XWECHAT) else []:
        if name.startswith("wxid_") or name in ("login", "lock"):
            src = os.path.join(XWECHAT, name)
            dst = os.path.join(bk, f"{name}.{int(time.time())}")
            try:
                shutil.move(src, dst)
            except Exception as e:
                log(f"清理 {name} 失败: {e}")


# ---- AT-SPI 登录状态检测 ----
def wechat_state():
    """返回 'login' | 'logged_in' | 'unknown'。"""
    try:
        import pyatspi
    except Exception:
        return "unknown"
    try:
        d = pyatspi.Registry.getDesktop(0)
    except Exception:
        return "unknown"
    login_btns, items = [], 0
    def walk(n):
        nonlocal items
        try:
            r = n.getRoleName(); st = n.getState()
            if st.contains(pyatspi.STATE_SHOWING) and n.name:
                if r == "push button" and n.name in ("Log In", "Scan again",
                                                     "Scan to log in", "Switch Account"):
                    login_btns.append(n.name)
                if r == "list item":
                    items += 1
            for c in n:
                if c:
                    walk(c)
        except Exception:
            pass
    for i in range(d.childCount):
        a = d.getChildAtIndex(i)
        if "wechat" in (a.name or "").lower():
            walk(a)
    if items > 0:
        return "logged_in"
    if login_btns:
        return "login"
    # 二维码登录界面无任何按钮(仅"Scan to log in"文字标签,AT-SPI 不一定暴露)——
    # 用窗口几何兜底:存在 292x396 级别的 Weixin 小窗即视为待登录(2026-07-14 盲区修复)
    try:
        out = subprocess.run(["xdotool", "search", "--name", "^Weixin$"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        for wid in out:
            g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                               capture_output=True, text=True, timeout=10).stdout
            m = dict(re.findall(r"(\w+)=(\S+)", g))
            if 200 < int(m.get("WIDTH", 0)) < 450 and 300 < int(m.get("HEIGHT", 0)) < 500:
                return "login"
    except Exception:
        pass
    return "unknown"


def find_login_window():
    """返回登录窗 (x,y,w,h)，找不到用默认。"""
    try:
        out = subprocess.run(["xdotool", "search", "--name", "^Weixin$"],
                             capture_output=True, text=True).stdout.split()
        for wid in out:
            g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                               capture_output=True, text=True).stdout
            d = dict(re.findall(r"(\w+)=(\S+)", g))
            w, h = int(d.get("WIDTH", 0)), int(d.get("HEIGHT", 0))
            if 250 < w < 400 and 350 < h < 450:
                return int(d["X"]), int(d["Y"]), w, h
    except Exception:
        pass
    return LOGIN_WIN


def switch_to_qr():
    """若是快速登录界面(Log In)，点 Switch Account 切到二维码。"""
    x, y, w, h = find_login_window()
    try:
        # Switch Account 在左下
        subprocess.run(["xdotool", "mousemove", str(x + 70), str(y + 351),
                        "mousedown", "1"], check=False)
        time.sleep(0.15)
        subprocess.run(["xdotool", "mouseup", "1"], check=False)
        time.sleep(2)
    except Exception:
        pass


def capture_qr():
    """截当前登录窗到 QR_PATH。"""
    x, y, w, h = find_login_window()
    tmp = QR_PATH + ".tmp"
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "x11grab", "-video_size", f"{w}x{h}",
         "-i", f":99.0+{x},{y}", "-frames:v", "1", tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode == 0 and os.path.exists(tmp):
        os.replace(tmp, QR_PATH)
        return True
    return False


def ensure_narrator():
    if not running("wechat_narrator_atspi"):
        log("拉起 narrator 朗读服务")
        spawn(["/usr/bin/python3", NARRATOR, "--interval", "2", "--wav-dir", WAV_DIR],
              "narrator")


def ensure_group_to_email():
    if not running("wechat_group_to_email"):
        log("拉起群转邮件服务")
        spawn(["/usr/bin/python3", GROUP_SCRIPT, "--group", GROUP_KEYWORDS,
               "--dm", DM_KEYWORDS,
               "--smtp-host", SMTP_HOST, "--smtp-port", SMTP_PORT,
               "--from", MAIL_FROM, "--to", MAIL_TO, "--baseline-seconds", "15"],
              "group2email")


def ensure_wemprss_bridge():
    """we-mp-rss 公众号新文章 → 邮件桥接(独立于微信，只读 we-mp-rss 数据库)。"""
    if not os.path.exists(BRIDGE_SCRIPT):
        return
    if not running("wemprss_mail_bridge"):
        log("拉起 we-mp-rss 邮件桥接")
        spawn(["/usr/bin/python3", BRIDGE_SCRIPT,
               "--smtp-host", SMTP_HOST, "--smtp-port", SMTP_PORT,
               "--from", MAIL_FROM, "--to", MAIL_TO, "--interval", "60"],
              "wemprss_bridge")


# ---- HTTP 展示二维码 / 状态 ----
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/qr.png"):
            if os.path.exists(QR_PATH):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(QR_PATH, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)
            return
        state = _status["state"]
        body = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>微信讲述人</title>
<style>body{{font-family:system-ui;background:#f4f6f4;color:#16201a;text-align:center;padding:32px}}
.card{{max-width:340px;margin:0 auto;background:#fff;border-radius:16px;padding:24px;box-shadow:0 8px 28px rgba(20,40,28,.1)}}
img{{width:240px;height:240px;image-rendering:pixelated;border:1px solid #eee;border-radius:8px}}
.s{{color:#07c160;font-weight:600}}</style>
<div class=card><h2>微信讲述人</h2>
<p>状态：<span class=s>{state}</span></p>
{"<p>手机微信扫码登录：</p><img src='/qr.png?t=%d'>" % int(time.time()) if state=="need_login" else ""}
{"<p>✅ 已登录，正在监听消息</p>" if state=="running" else ""}
<p style='color:#888;font-size:13px'>页面每 5 秒自动刷新</p></div>
<script>setTimeout(()=>location.reload(),5000)</script>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


def http_thread():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
        log(f"二维码网页: http://<本机>:{HTTP_PORT}/")
        srv.serve_forever()
    except Exception as e:
        log(f"HTTP 服务异常: {e}")


def main():
    threading.Thread(target=http_thread, daemon=True).start()
    set_status("starting")
    ensure_stack()
    qr_shot = 0
    while True:
        try:
            ensure_stack()  # 守护：组件挂了自动拉起
            # 注：we-mp-rss 邮件桥接改由独立 systemd 服务 wemprss-bridge.service 常驻管理
            st = wechat_state()
            if st == "logged_in":
                set_status("running")
                ensure_narrator()
                ensure_group_to_email()
                qr_shot = 0
            elif st == "login":
                set_status("need_login", "手机扫码登录")
                # 若是快速登录界面先切二维码，然后定期刷新二维码截图
                now = time.time()
                if now - qr_shot > 8:
                    switch_to_qr()
                    capture_qr()
                    qr_shot = now
            else:
                set_status("starting", "等待微信界面")
        except Exception as e:
            log(f"主循环异常: {e}")
        time.sleep(3)


if __name__ == "__main__":
    main()
