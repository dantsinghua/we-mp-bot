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
STAMP_UPSTREAM = os.path.join(LOG_DIR, ".wd_last_upstream_alert")
STAMP_TASK = os.path.join(LOG_DIR, ".wd_last_task_alert")
PATCH_SH = os.path.join(HOME, "clawd", "scripts", "wechat-narrator", "wemprss_patch.sh")

INGEST_STALE_H = 6        # created_at 停滞阈值 → 本地卡死,重启
UPSTREAM_STALE_H = 5      # publish_time 停滞阈值(created_at 仍新鲜) → 上游限流,只告警
UPSTREAM_ALERT_GAP = 6 * 3600
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


def _local_broken():
    """本地故障实证:僵尸 playwright/node 进程(>20min) / 内存超限 / 近期崩溃日志。
    有实证才对入库停滞做重启;否则视为上游限流(重启无用)。"""
    # 僵尸浏览器/driver:etime 超过 20 分钟的 node/firefox(正常抓取几十秒内结束)
    rc, out, _ = sh("docker exec we-mp-rss sh -c \"ps -eo etimes,comm | "
                    "grep -E 'node|firefox' | awk '$1>1200{print}'\" 2>/dev/null || true")
    if out.strip():
        log(f"local_broken: 僵尸浏览器进程 {out.strip()[:80]}")
        return True
    # 内存逼近上限(OOM 前兆)
    rc, out, _ = sh("docker exec we-mp-rss cat /sys/fs/cgroup/memory.current 2>/dev/null")
    if rc == 0 and out.isdigit() and int(out) > MEM_LIMIT:
        log(f"local_broken: 内存 {int(out)/1024**3:.2f}G > 限")
        return True
    # 近期 Python traceback / 崩溃
    rc, out, _ = sh("docker logs we-mp-rss --since 40m 2>&1 | "
                    "grep -acE 'Traceback|CRITICAL|OOM' || true")
    if out.isdigit() and int(out) > 0:
        log(f"local_broken: 近40min {out} 条崩溃日志")
        return True
    return False


def alert(subject, body, severity="urgent"):
    # Outlook 分类:主题带 [紧急]/[提醒]<类别> 标签 + X-WN-* 头(可按头建规则)。
    try:
        tag = "[紧急]" if severity == "urgent" else "[提醒]"
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(f"{tag}[公众号] {subject}", "utf-8")
        msg["From"] = "wechat-narrator@test.local"
        msg["To"] = "wechat-group@test.local"  # 安琳 Outlook 读此邮箱
        msg["X-WN-Category"] = "oa"
        msg["X-WN-Severity"] = severity
        msg["X-WN-Source"] = "watchdog"
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
    # 暂停开关：存在 .wd_paused 时看门狗完全空转(不重启/不触发/不告警)。
    # 用于风控冷却期人为停抓——删掉该文件即恢复看护。
    if os.path.exists(os.path.join(LOG_DIR, ".wd_paused")):
        log("PAUSED (.wd_paused 存在) → 本轮跳过所有检查")
        return
    # container up?
    rc, out, _ = sh("docker ps --filter name=we-mp-rss --format '{{.Status}}'")
    if not out.startswith("Up"):
        log(f"container not up: {out!r}")
        sh("docker start we-mp-rss", timeout=120)
        alert("we-mp-rss 容器不在运行", f"状态: {out!r}，已尝试启动")
        return

    # 1. ingest staleness (created_at stored in LOCAL time (CST) -> 'utc' modifier converts to epoch)
    #    关键:入库停滞可能是 (a) 本地卡死 或 (b) 上游限流(限流时上游不返回=也不产生新行)。
    #    单靠 created_at 停滞无法区分,故本地卡死重启必须带**本地故障实证**(下方 local_broken),
    #    否则一律按上游限流处理(只告警,不重启——重启对限流无用且增加请求)。
    age_h = None
    broken = _local_broken()
    rc, out = container_py(
        "import sqlite3\n"
        "c = sqlite3.connect('file:/app/data/db.db?mode=ro', uri=True, timeout=5)\n"
        "print(c.execute(\"SELECT strftime('%s', MAX(created_at), 'utc') FROM articles\").fetchone()[0])\n")
    if rc == 0 and out and out != "None":
        age_h = (time.time() - int(out)) / 3600
        log(f"ingest age(created_at): {age_h:.1f}h, local_broken={broken}")
        if age_h > INGEST_STALE_H and broken:
            restart_container(f"本地卡死实证+入库停滞 {age_h:.1f}h")
        elif age_h > INGEST_STALE_H:
            log(f"入库停滞 {age_h:.1f}h 但容器健康 → 判上游限流,不重启(见 1b 告警)")
    else:
        log(f"ingest check failed rc={rc} out={out!r}")

    # 1b. 上游限流判别:publish_time 冻结但 created_at 仍在动 = 微信软限流(风控),
    #     重启无用(限流在微信端),甚至有害(增加请求)→ 只限频告警,不重启。
    rc, out = container_py(
        "import sqlite3\n"
        "c = sqlite3.connect('file:/app/data/db.db?mode=ro', uri=True, timeout=5)\n"
        "print(c.execute(\"SELECT MAX(publish_time) FROM articles\").fetchone()[0])\n")
    if rc == 0 and out and out.isdigit():
        pub_age_h = (time.time() - int(out)) / 3600
        log(f"publish freshness: {pub_age_h:.1f}h")
        if pub_age_h > UPSTREAM_STALE_H and not broken:
            if stamp_ok(STAMP_UPSTREAM, UPSTREAM_ALERT_GAP):
                touch(STAMP_UPSTREAM)
                alert("公众号疑似被微信限流(风控)",
                      f"最新文章发布已 {pub_age_h:.1f} 小时前,但采集仍在回填旧文"
                      f"(created_at 新鲜)——典型微信软限流,非本地故障。\n"
                      "不重启容器(重启无用且增加请求)。通常数小时自愈;"
                      "已把采集降频到每3小时以缓解。", severity="warning")
            else:
                log(f"upstream rate-limit suspected ({pub_age_h:.1f}h), alert rate-limited")

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

    # 4. 授权过期：旧版打 'Invalid Session'；新版(登录模块 v1.5.2+)过期时转而打
    #    '微信公众平台登录'(启动扫码浏览器)。两个签名都算授权过期。
    rc, out, _ = sh("docker logs we-mp-rss --since 35m 2>&1 | "
                    "grep -acE 'Invalid Session|微信公众平台登录' || true")
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

    # 6. 定时采集任务未启用 (status != 1) → 根本不会自动抓取(静默停真凶,区别于风控)
    rc, out = container_py(
        "import sqlite3\n"
        "c = sqlite3.connect('file:/app/data/db.db?mode=ro', uri=True, timeout=5)\n"
        f"r = c.execute(\"SELECT status FROM message_tasks WHERE id='{TASK_ID}'\").fetchone()\n"
        "print(r[0] if r else 'MISSING')\n")
    if rc == 0 and out and out not in ("1",):
        log(f"crawl task not enabled: status={out!r}")
        if stamp_ok(STAMP_TASK, AUTH_ALERT_GAP):
            touch(STAMP_TASK)
            alert("定时采集任务未启用，公众号不会自动抓取",
                  f"we-mp-rss 定时采集任务 status={out}（需为 1=启用）。\n"
                  "→ 系统不会自动爬取新文章（这与微信风控不同，是任务被关）。\n"
                  "处理：we-mp-rss「定时任务」里启用该任务，或对 Claude 说「启用定时采集」。",
                  severity="urgent")

    log("cycle done")


if __name__ == "__main__":
    main()
