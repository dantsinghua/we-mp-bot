#!/bin/bash
# we-mp-rss 采集监控：盯进度，卡死1小时自动重启容器重试，爬完自动停。
DB=/home/dantsinghua/clawd/scripts/wechat-narrator/wemprss-data/db.db
LOG=/tmp/wemprss_crawl_monitor.log
STALL=3600      # 卡死判定：进度 3600s 不动 → 重试
POLL=300        # 每 5 分钟检查一次
MAXRUN=$((4*3600))  # 最长跑 4 小时后自停（安全）
TODAY_TS=1784620800 # 2026-07-20 00:00 起算“已同步到今天附近”

metric(){ python3 -c "
import sqlite3
c=sqlite3.connect('file:$DB?mode=ro', uri=True)
mx=c.execute('SELECT COALESCE(MAX(sync_time),0) FROM feeds').fetchone()[0]
na=c.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
sd=c.execute('SELECT COUNT(*) FROM feeds WHERE sync_time > $TODAY_TS').fetchone()[0]
tot=c.execute('SELECT COUNT(*) FROM feeds').fetchone()[0]
print(f'{mx} {na} {sd} {tot}')
" 2>/dev/null; }

log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }
log "=== 采集监控启动 (卡死${STALL}s自动重试) ==="

start=$(date +%s); last_progress=$(date +%s); prev=""
while true; do
  now=$(date +%s)
  [ $((now-start)) -ge $MAXRUN ] && { log "达最长运行时间，监控退出"; break; }
  read MX NA SD TOT <<< "$(metric)"
  [ -z "$TOT" ] && { sleep $POLL; continue; }
  cur="$MX-$NA"
  if [ "$cur" != "$prev" ]; then
    last_progress=$now; prev="$cur"
    log "进度: 已同步 $SD/$TOT 号, 文章 $NA 篇, feeds最新同步 $(date -d @$MX '+%m-%d %H:%M' 2>/dev/null)"
  fi
  # 完成判定：绝大多数号已同步到今天
  if [ "$SD" -ge $((TOT*9/10)) ] 2>/dev/null; then
    log "✅ 采集完成: $SD/$TOT 号已同步到今天, 文章 $NA 篇"; break
  fi
  # 卡死判定
  if [ $((now-last_progress)) -ge $STALL ]; then
    log "⚠ 进度卡死 $((STALL/60)) 分钟无变化 → 重启 we-mp-rss 重试"
    docker restart we-mp-rss >/dev/null 2>&1
    log "已重启 we-mp-rss，等待爬取恢复"
    last_progress=$now
  fi
  sleep $POLL
done
log "=== 采集监控结束 ==="
