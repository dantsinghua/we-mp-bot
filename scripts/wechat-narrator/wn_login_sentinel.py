#!/usr/bin/env python3
"""WeChat login sentinel — dedicated daemon.

Polls WeChat login state every POLL_SEC (read-only window-geometry probe; never
touches the WeChat client, so it is safe to restart independently and does NOT
risk dropping the session the way restarting supervisor would).

On the logged_in -> logged_out EDGE it immediately emails an alert with:
  - the current login QR as an inline + attached PNG (scan straight from the mail),
  - the supervisor's live QR web page URL (auto-refreshes every 5s, never expires).
Emits once per logout episode (edge-triggered, not level), and re-nudges every
RENUDGE_SEC while still logged out in case the first mail was missed.

NOTE: A daemon cannot publish a claude.ai artifact link (only an interactive
Claude session can). The attached PNG + live web page are the automatic
equivalent and are actually more robust (the artifact QR expires in ~2min).
"""
import os
import re
import smtplib
import subprocess
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.header import Header

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".wechat-narrator")
LOG_DIR = os.path.join(STATE_DIR, "logs")
QR_PATH = os.path.join(STATE_DIR, "login_qr.png")
os.makedirs(LOG_DIR, exist_ok=True)
LOG = os.path.join(LOG_DIR, "login_sentinel.log")

DISPLAY = os.environ.get("DISPLAY", ":99")
SMTP_HOST = os.environ.get("NARRATOR_SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("NARRATOR_SMTP_PORT", "3025"))
MAIL_FROM = os.environ.get("NARRATOR_MAIL_FROM", "wechat-narrator@test.local")
MAIL_TO = os.environ.get("NARRATOR_MAIL_TO", "wechat-narrator@test.local")
HTTP_PORT = os.environ.get("NARRATOR_HTTP_PORT", "8899")
# Live QR page URL shown in the mail. Override with the tunneled/public URL if set.
QR_WEB_URL = os.environ.get("WN_QR_WEB_URL", f"http://192.100.2.100:{HTTP_PORT}/")
SENTINEL_QR = os.path.join(STATE_DIR, "sentinel_qr.png")

POLL_SEC = int(os.environ.get("WN_SENTINEL_POLL", "30"))
RENUDGE_SEC = int(os.environ.get("WN_SENTINEL_RENUDGE", "3600"))   # re-alert if still down


def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def wechat_state():
    """'logged_in' | 'logged_out' | 'unknown' — read-only, never touches WeChat.

    logged_in: AT-SPI sees conversation list items.
    logged_out: a 292x396-class Weixin login window exists (and no list items).
    """
    items = 0
    try:
        import pyatspi
        d = pyatspi.Registry.getDesktop(0)

        def walk(n):
            nonlocal items
            try:
                if (n.getRoleName() == "list item" and n.name
                        and n.getState().contains(pyatspi.STATE_SHOWING)):
                    e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                    if e.x < 280 and e.width > 100:
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
    except Exception:
        pass
    if items > 0:
        return "logged_in"
    # geometry fallback for login window (QR screen exposes no AT-SPI buttons)
    try:
        out = subprocess.run(["xdotool", "search", "--name", "^Weixin$"],
                             env={**os.environ, "DISPLAY": DISPLAY},
                             capture_output=True, text=True, timeout=10).stdout.split()
        for wid in out:
            g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                               env={**os.environ, "DISPLAY": DISPLAY},
                               capture_output=True, text=True, timeout=10).stdout
            m = dict(re.findall(r"(\w+)=(\S+)", g))
            if 200 < int(m.get("WIDTH", 0)) < 450 and 300 < int(m.get("HEIGHT", 0)) < 500:
                return "logged_out"
    except Exception:
        pass
    return "unknown"


def capture_login_qr():
    """Grab the QR itself (proven method: move login window to corner, screenshot by
    window id, crop the QR block). Robust vs supervisor's root-grab which the Openbox
    workspace-switcher overlay corrupts. Writes SENTINEL_QR; returns path or None."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    try:
        out = subprocess.run(["xdotool", "search", "--name", "^Weixin$"],
                             env=env, capture_output=True, text=True, timeout=10).stdout.split()
        wid = None
        for w in out:
            g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", w],
                               env=env, capture_output=True, text=True, timeout=10).stdout
            m = dict(re.findall(r"(\w+)=(\S+)", g))
            if 200 < int(m.get("WIDTH", 0)) < 450 and 300 < int(m.get("HEIGHT", 0)) < 500:
                wid = w
                break
        if not wid:
            return None
        subprocess.run(["xdotool", "windowactivate", wid], env=env, timeout=10)
        subprocess.run(["xdotool", "windowmove", wid, "0", "0"], env=env, timeout=10)
        time.sleep(1.5)
        raw = SENTINEL_QR + ".raw.png"
        for _ in range(4):
            r = subprocess.run(["import", "-window", wid, raw], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if r.returncode == 0 and os.path.exists(raw):
                break
            time.sleep(1)
        else:
            return None
        # crop QR block from the 292x396 login window and upscale
        r = subprocess.run(["convert", raw, "-crop", "176x176+58+52", "+repage",
                            "-filter", "point", "-resize", "440x440", SENTINEL_QR],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        try:
            os.remove(raw)
        except OSError:
            pass
        return SENTINEL_QR if r.returncode == 0 and os.path.exists(SENTINEL_QR) else None
    except Exception as e:
        log(f"capture_login_qr failed: {e}")
        return None


def send_logout_mail():
    """Email the logout alert with a freshly-grabbed QR (inline + attached) and web link."""
    qr_file = capture_login_qr() or (QR_PATH if os.path.exists(QR_PATH) else None)
    msg = MIMEMultipart("related")
    msg["Subject"] = Header("🔴 微信已掉线,请重新扫码登录", "utf-8")
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO

    qr_ok = bool(qr_file)
    inline = ('<p><img src="cid:qr" style="width:260px;image-rendering:pixelated;'
              'border:1px solid #ddd;border-radius:8px"></p>' if qr_ok else
              '<p>(二维码尚未截取到,请打开下方网页查看最新码)</p>')
    html = f"""<div style="font-family:system-ui,PingFang SC,sans-serif">
<h2 style="color:#c0392b">微信桌面端已掉线</h2>
<p>检测时间：{time.strftime('%Y-%m-%d %H:%M:%S')}。所有微信转发/回复/朗读已暂停,请尽快重新登录。</p>
{inline}
<p><b>扫码方式(任选其一)：</b></p>
<ul>
  <li>直接扫上方(或附件里的)二维码图片；</li>
  <li>打开活二维码网页(每5秒自动刷新,永不过期)：<br>
      <a href="{QR_WEB_URL}">{QR_WEB_URL}</a></li>
</ul>
<p style="color:#888;font-size:13px">扫码后在手机上点「登录」确认。本邮件每 {RENUDGE_SEC//60} 分钟重发一次直至恢复。</p>
</div>"""
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("微信已掉线,请重新扫码登录。见 " + QR_WEB_URL, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    if qr_ok:
        try:
            with open(qr_file, "rb") as f:
                data = f.read()
            img = MIMEImage(data, "png")
            img.add_header("Content-ID", "<qr>")
            img.add_header("Content-Disposition", "attachment", filename="wechat_login_qr.png")
            msg.attach(img)
        except Exception as e:
            log(f"attach QR failed: {e}")

    try:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        s.sendmail(MAIL_FROM, [MAIL_TO], msg.as_string())
        s.quit()
        log("logout alert email sent" + ("" if qr_ok else " (no QR file yet)"))
        return True
    except Exception as e:
        log(f"send mail FAILED: {e}")
        return False


def main():
    log(f"login sentinel started (poll {POLL_SEC}s, renudge {RENUDGE_SEC}s)")
    prev = None            # last observed state
    last_alert = 0.0       # last time we emailed while logged out
    alerted_this_episode = False
    while True:
        st = wechat_state()
        now = time.time()
        if st == "logged_in":
            if prev == "logged_out":
                log("recovered: logged back in")
            alerted_this_episode = False
        elif st == "logged_out":
            # edge: just went down, OR still down past renudge interval
            if prev == "logged_in" or not alerted_this_episode \
                    or now - last_alert > RENUDGE_SEC:
                if prev == "logged_in":
                    log("EDGE: logged_in -> logged_out")
                if send_logout_mail():   # captures its own fresh QR
                    last_alert = now
                    alerted_this_episode = True
        # 'unknown' -> don't flap; keep prev unchanged for edge detection
        if st in ("logged_in", "logged_out"):
            prev = st
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
