#!/bin/bash
# 触发 we-mp-rss 全量采集：登录→遍历所有公众号调 update（单号超时防卡+进度日志）
BASE=http://127.0.0.1:8001
DB=/home/dantsinghua/clawd/scripts/wechat-narrator/wemprss-data/db.db
LOG=/tmp/wemprss_crawl_all.log
PER_TIMEOUT=150   # 单个公众号最多等 150s，卡了就跳过下一个

log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# 管理员凭据：环境变量优先，其次 ~/.wechat-narrator/secrets.json（与各 .py 的 _secret() 约定一致，不在代码中写死）
_sec(){ python3 -c "import json,os,sys;n=sys.argv[1];v=os.environ.get(n) or json.load(open(os.path.expanduser('~/.wechat-narrator/secrets.json'))).get(n,'');print(v)" "$1" 2>/dev/null; }
WM_USER=$(_sec WEMPRSS_ADMIN_USER); WM_PASS=$(_sec WEMPRSS_ADMIN_PASS)
[ -z "$WM_PASS" ] && { log "缺少 WEMPRSS_ADMIN_PASS（env 或 secrets.json），退出"; exit 1; }
TOKEN=$(curl -s --max-time 10 -X POST "$BASE/api/v1/wx/auth/token" --data-urlencode "username=${WM_USER:-admin}" --data-urlencode "password=$WM_PASS" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])" 2>/dev/null)
[ -z "$TOKEN" ] && { log "登录失败，退出"; exit 1; }
log "=== 全量采集开始，已登录 ==="

MPS=$(python3 -c "import sqlite3;c=sqlite3.connect('file:$DB?mode=ro',uri=True);print('\n'.join(str(r[0]) for r in c.execute('SELECT id FROM feeds WHERE status=1 OR status IS NULL')))" 2>/dev/null)
TOTAL=$(echo "$MPS" | grep -c .)
log "待采集公众号: $TOTAL 个"

i=0; newart=0
while IFS= read -r mp; do
  [ -z "$mp" ] && continue
  i=$((i+1))
  RESP=$(curl -s --max-time $PER_TIMEOUT -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/wx/mps/update/$mp" 2>/dev/null)
  N=$(echo "$RESP" | python3 -c "import sys,json;
try: print(json.load(sys.stdin).get('data',{}).get('total',0))
except: print('ERR')" 2>/dev/null)
  [ "$N" != "ERR" ] && [ -n "$N" ] && newart=$((newart+N)) 2>/dev/null
  if [ $((i % 10)) -eq 0 ] || [ "$N" != "0" ]; then
    log "进度 $i/$TOTAL | 本号新文章:$N | 累计新增:$newart"
  fi
  sleep 2   # 防风控
done <<< "$MPS"

log "✅ 全量采集完成: 遍历 $i/$TOTAL 个号, 累计新增文章 $newart 篇"
