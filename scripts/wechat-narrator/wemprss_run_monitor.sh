#!/bin/bash
# 盯 11:11 手工触发的这轮全量采集：进度/卡点/完成。40min 自停。
DB=/home/dantsinghua/clawd/scripts/wechat-narrator/wemprss-data/db.db
LOG=/tmp/wemprss_run_monitor.log
START_TS=1784689800   # ~11:10，本轮触发前
POLL=45
STALL_ROUNDS=4        # 连续 4 轮(~3min)无进展 → 疑似卡
MAXRUN=$((6*3600))

log(){ echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }

q(){ python3 -c "
import sqlite3
c=sqlite3.connect('file:$DB?mode=ro',uri=True,timeout=8)
synced=c.execute('SELECT COUNT(*) FROM feeds WHERE sync_time>=$START_TS').fetchone()[0]
mx=c.execute('SELECT MAX(sync_time) FROM feeds').fetchone()[0]
na=c.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
print(f'{synced} {mx} {na}')
" 2>/dev/null; }

log "=== 本轮采集监控启动 ==="
start=$(date +%s); prev=-1; stall=0; base_art=""
while true; do
  now=$(date +%s)
  [ $((now-start)) -ge $MAXRUN ] && { log "达最长监控时长，退出"; break; }
  read synced mx na <<< "$(q)"
  [ -z "$synced" ] && { sleep $POLL; continue; }
  [ -z "$base_art" ] && base_art=$na
  running=$(docker logs we-mp-rss --since 3m 2>&1 | grep -oE "'pending_tasks': [0-9]+" | tail -1)
  log "进度 同步$synced/131 | $running | 文章$na(+$((na-base_art))) | 最新$(date -d @$mx '+%H:%M:%S' 2>/dev/null)"
  # 完成判定
  pend=$(echo "$running" | grep -oE "[0-9]+$")
  if [ "$synced" -ge 128 ] 2>/dev/null || { [ -n "$pend" ] && [ "$pend" -le 1 ] 2>/dev/null && [ "$synced" -ge 100 ]; }; then
    log "✅ 采集完成: 同步 $synced/131, 文章 $na(本轮+$((na-base_art)))"; break
  fi
  # 卡点判定：看容器日志近5min有无任何采集活动（滚动/爬取/入库/同步）。
  # 深度采集单篇慢时 DB 计数不动，但日志一直在刷 → 只有日志彻底静默才是真卡。
  act=$(docker logs we-mp-rss --since 5m 2>&1 | grep -cE "Added article|sync_time为|采集完成等待|开始爬取|滚动进度|开始滚动" 2>/dev/null)
  if [ "${act:-0}" -eq 0 ] 2>/dev/null; then
    stall=$((stall+1))
    [ $stall -ge 2 ] && log "⚠ 疑似卡住: 容器近5min零采集活动 (同步$synced/131 文章$na pending=$pend act=$act)"
  else
    stall=0
  fi
  prev="$synced-$na"
  sleep $POLL
done
log "=== 监控结束 ==="
