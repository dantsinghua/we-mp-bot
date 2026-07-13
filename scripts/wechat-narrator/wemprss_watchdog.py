#!/usr/bin/env python3
"""we-mp-rss watchdog — runs every 30 min via systemd user timer.

Checks (all failure modes observed 2026-07-09..14):
  1. ingest staleness : MAX(articles.created_at) older than 6h (UTC)  -> restart container (+repatch, +trigger gather)
  2. container memory : memory.current > 1.7 GiB                     -> restart container (OOM precursor; host has only 7G total)
  3. FETCHING leak    : articles stuck status=6 for >1h              -> unlock to ACTIVE(1)
  4. Invalid Session  : in recent container logs                     -> alert email (auth expired ~80h, needs manual QR re-scan; rate-limited 6h)
  5. bridge heartbeat : wemprss-bridge journal silent >26h           -> restart bridge service

Restarts rate-limited to 1 per 2h (stamp file). Log: ~/.wechat-narrator/logs/watchdog.log
"""
import json
import os
import smtplib
import subprocess
import time
from email.mime.text import MIMEText
from email.header import Header

HOME = os.path.expanduser("~")
LOG_DIR = os.path.join(HOME, ".wechat-narrator", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG = os.path.join(LOG_DIR, "watchdog.log")
STAMP_RESTART = os.path.join(LOG_DIR, ".wd_last_restart")
STAMP_AUTH = os.path.join(LOG_DIR, ".wd_last_auth_alert")
PATCH_SH = os.path.join(HOME, "clawd", "scripts", "wechat-narrator", "wemprss_patch.sh")

INGEST_STALE_H = 6
MEM_LIMIT = 1.7 * 1024**3
RESTART_MIN_GAP = 2 * 3600
AUTH_ALERT_GAP = 6 * 3600
def _secret(name, default=""):
    v = os.environ.get(name)
    if v:
        return v
    try:
        with open(os.path.join(HOME, ".wechat-narrator", "secrets.json")) as f:
            return json.load(f).get(name, default)
    except (OSError, ValueError):
        return default


AK = _secret("WEMPRSS_AK")
SK = _secret("WEMPRSS_SK")
TASK_ID = "90f16c61-ab1a-4e33-99ec-c7c2f55a7b0f"


def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def sh(cmd, timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def container_py(code, timeout=30):
    r = subprocess.run(["docker", "exec", "-i", "we-mp-rss", "python3", "-"],
                       input=code, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "").strip()


def alert(subject, body):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(f"⚠ [watchdog] {subject}", "utf-8")
        msg["From"] = "wechat-narrator@test.local"
        msg["To"] = "wechat-narrator@test.local"
        s = smtplib.SMTP("127.0.0.1", 3025, timeout=15)
        s.sendmail(msg["From"], [msg["To"]], msg.as_string())
        s.quit()
        log(f"alert sent: {subject}")
    except Exception as e:
        log(f"alert FAILED ({subject}): {e}")


def stamp_ok(path, gap):
    try:
        return time.time() - os.path.getmtime(path) > gap
    except OSError:
        return True


def touch(path):
    open(path, "a").close()
    os.utime(path)


def restart_container(reason):
    if not stamp_ok(STAMP_RESTART, RESTART_MIN_GAP):
        log(f"restart wanted ({reason}) but rate-limited")
        return
    touch(STAMP_RESTART)
    log(f"RESTART we-mp-rss: {reason}")
    sh("docker restart we-mp-rss", timeout=120)
    time.sleep(20)
    rc, out, err = sh(f"bash {PATCH_SH}", timeout=60)
    log(f"repatch rc={rc} {out or err}")
    # patch needs a load; restart once more so the patched code runs
    sh("docker restart we-mp-rss", timeout=120)
    time.sleep(20)
    sh("curl -sf -m 10 'http://127.0.0.1:8001/api/v1/wx/message_tasks/" + TASK_ID +
       f"/run' -H 'Authorization: AK-SK {AK}:{SK}'", timeout=30)
    alert("we-mp-rss 已自动重启", f"原因: {reason}\n已重打补丁并触发采集。")


def main():
    # container up?
    rc, out, _ = sh("docker ps --filter name=we-mp-rss --format '{{.Status}}'")
    if not out.startswith("Up"):
        log(f"container not up: {out!r}")
        sh("docker start we-mp-rss", timeout=120)
        alert("we-mp-rss 容器不在运行", f"状态: {out!r}，已尝试启动")
        return

    # 1. ingest staleness (created_at stored in LOCAL time (CST) -> 'utc' modifier converts to epoch)
    rc, out = container_py(
        "import sqlite3\n"
        "c = sqlite3.connect('file:/app/data/db.db?mode=ro', uri=True, timeout=5)\n"
        "print(c.execute(\"SELECT strftime('%s', MAX(created_at), 'utc') FROM articles\").fetchone()[0])\n")
    if rc == 0 and out and out != "None":
        age_h = (time.time() - int(out)) / 3600
        log(f"ingest age: {age_h:.1f}h")
        if age_h > INGEST_STALE_H:
            restart_container(f"入库停滞 {age_h:.1f} 小时")
    else:
        log(f"ingest check failed rc={rc} out={out!r}")

    # 2. memory
    rc, out, _ = sh("docker exec we-mp-rss cat /sys/fs/cgroup/memory.current")
    if rc == 0 and out.isdigit():
        gb = int(out) / 1024**3
        log(f"mem: {gb:.2f}G")
        if int(out) > MEM_LIMIT:
            restart_container(f"容器内存 {gb:.2f}G > 1.7G")

    # 3. FETCHING leak (updated_at UTC, stuck >1h)
    rc, out = container_py(
        "import sqlite3\n"
        "c = sqlite3.connect('/app/data/db.db', timeout=5)\n"
        "cur = c.execute(\"UPDATE articles SET status=1 WHERE status=6 AND has_content=0 \"\n"
        "                \"AND updated_at < datetime('now','localtime','-1 hour')\")\n"
        "c.commit(); print(cur.rowcount)\n")
    if rc == 0 and out.isdigit() and int(out) > 0:
        log(f"unlocked {out} stuck FETCHING articles")

    # 4. Invalid Session
    rc, out, _ = sh("docker logs we-mp-rss --since 35m 2>&1 | grep -ac 'Invalid Session' || true")
    n = int(out) if out.isdigit() else 0
    if n > 0 and stamp_ok(STAMP_AUTH, AUTH_ALERT_GAP):
        touch(STAMP_AUTH)
        alert("公众号授权已过期，需要重新扫码",
              f"近35分钟 {n} 条 Invalid Session。\n"
              "处理: 打开局域网 http://<本机>:8001 登录(admin)后扫码，\n"
              "或对 Claude 说「出二维码」。授权有效期约80小时。")

    # 5. bridge heartbeat (journal age; effective once bridge logs hourly heartbeat)
    rc, out, _ = sh("journalctl --user -u wemprss-bridge.service -n 1 -o short-unix --no-pager 2>/dev/null | awk '{print $1}'")
    try:
        if out and time.time() - float(out) > 26 * 3600:
            log("bridge journal silent >26h -> restart bridge")
            sh("systemctl --user restart wemprss-bridge.service")
            alert("wemprss-bridge 无心跳已重启", "journal 静默超过 26 小时")
    except ValueError:
        pass

    log("cycle done")


if __name__ == "__main__":
    main()
