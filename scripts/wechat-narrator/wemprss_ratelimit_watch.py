#!/usr/bin/env python3
"""One-shot rate-limit recovery watcher for we-mp-rss.

Runs while the OA gather is paused during a WeChat soft-rate-limit cooldown.
Every few minutes it briefly UN-pauses gather, triggers one gather run, and
checks whether MAX(publish_time) has advanced past the pre-pause watermark.
  - Not recovered  -> re-pause (keep cooling), loop.
  - Recovered      -> leave gather RUNNING, email an [紧急] recovery notice, exit.
Also emails an [紧急] alert if it gives up after MAX_HOURS without recovery.

Meant to be launched in the background for a single cooldown episode.
"""
import json
import os
import smtplib
import sys
import time
import urllib.request
from email.mime.text import MIMEText
from email.header import Header

sys.path.insert(0, "/home/dantsinghua/clawd/scripts/wechat-narrator")
import wemprss_mail_bridge as b   # reuse BASE / AUTH / secret loading

TASK_ID = "90f16c61-ab1a-4e33-99ec-c7c2f55a7b0f"
CRON = "15 */3 * * *"
POLL_SEC = 900          # probe every 15 min (gentle — don't re-trigger 风控)
MAX_HOURS = 16          # give up after this long, alert
SMTP = ("127.0.0.1", 3025)
MAIL_FROM = "wechat-narrator@test.local"
MAIL_TO = "wechat-narrator@test.local"
LOG = os.path.join(os.path.expanduser("~"), ".wechat-narrator", "logs", "ratelimit_watch.log")

_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def api(method, path, body=None):
    req = urllib.request.Request(
        b.BASE + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": b.AUTH, "Content-Type": "application/json"}, method=method)
    return _op.open(req, timeout=30)


def set_gather(status):
    api("PUT", f"/message_tasks/{TASK_ID}",
        {"name": "定时采集-全部公众号", "message_template": "", "web_hook_url": "",
         "mps_id": "[]", "message_type": 0, "cron_exp": CRON, "status": status})
    api("PUT", "/message_tasks/job/fresh")


def trigger_gather():
    try:
        api("GET", f"/message_tasks/{TASK_ID}/run")
    except Exception as e:
        log(f"trigger failed: {e}")


def max_publish():
    """Newest publish_time epoch across articles, via API (avoids sqlite lock)."""
    try:
        r = api("GET", "/articles?limit=1")   # articles come newest-first
        d = json.load(r)
        items = d.get("data", {}).get("list", d.get("data", []))
        if items:
            return int(items[0].get("publish_time") or 0)
    except Exception as e:
        log(f"max_publish failed: {e}")
    return 0


def alert(subject, body, severity="urgent"):
    try:
        tag = "[紧急]" if severity == "urgent" else "[提醒]"
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(f"{tag}[公众号] {subject}", "utf-8")
        msg["From"] = MAIL_FROM
        msg["To"] = MAIL_TO
        msg["X-WN-Category"] = "oa"
        msg["X-WN-Severity"] = severity
        msg["X-WN-Source"] = "ratelimit-watch"
        s = smtplib.SMTP(*SMTP, timeout=15)
        s.sendmail(MAIL_FROM, [MAIL_TO], msg.as_string())
        s.quit()
        log(f"alert sent: {subject}")
    except Exception as e:
        log(f"alert FAILED: {e}")


def main():
    baseline = max_publish()      # watermark at cooldown start (~16:30 today)
    t0 = time.time()
    log(f"ratelimit watcher started. baseline publish={baseline} "
        f"({time.strftime('%m-%d %H:%M', time.localtime(baseline)) if baseline else '?'})")
    while True:
        # probe: briefly resume gather, trigger once, check
        set_gather(1)
        trigger_gather()
        time.sleep(120)                     # let the gather round pull
        cur = max_publish()
        if cur > baseline:
            # RECOVERED — leave gather running, notify, done.
            log(f"RECOVERED publish {baseline}->{cur} "
                f"({time.strftime('%m-%d %H:%M', time.localtime(cur))})")
            alert("公众号限流已解除,采集已恢复",
                  f"最新文章发布时间已推进到 "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(cur))}"
                  f"(此前卡在 {time.strftime('%H:%M', time.localtime(baseline)) if baseline else '?'})。\n"
                  "采集任务已自动恢复运行,bridge 将陆续补发新文章。", severity="warning")
            return
        # not recovered — re-pause to keep cooling
        set_gather(0)
        elapsed_h = (time.time() - t0) / 3600
        log(f"still limited (publish={cur}), re-paused. elapsed {elapsed_h:.1f}h")
        if elapsed_h > MAX_HOURS:
            alert("公众号限流超时未解除,请人工介入",
                  f"已监视 {elapsed_h:.1f} 小时,限流仍未解除(publish 仍在 "
                  f"{time.strftime('%H:%M', time.localtime(baseline)) if baseline else '?'})。\n"
                  "采集当前处于暂停。请检查 we-mp-rss 授权是否过期或需人工处理。")
            return
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
