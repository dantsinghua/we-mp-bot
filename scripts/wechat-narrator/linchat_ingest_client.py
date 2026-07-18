#!/usr/bin/env python3
"""linchat_ingest_client.py — wechat 侧 → LinChat 记忆摄入客户端（B2）。

msg_synth 调 `ingest(day=, summary=)`，语义：
- **空 summary → noop**：不入队、不 POST，直接返回（不抛）。
- **先入本地 pending**（sqlite，`content_hash` UNIQUE）：入队成功即视为"已受理"，
  函数不抛 → msg_synth 可安全 mark。
- **再尽力 flush**（POST /api/v1/internal/ingest/，带 `X-Device-Token` 头）：
  LinChat 宕机 / 网络错误 → 条目留在 pending，下次 `ingest()` 或 `flush_pending()`
  （`--flush`）补投；**flush 失败不抛**。
- **只有本地 pending 写失败才抛** → msg_synth 据此降级（写本地 md，不 mark，待重跑）。

env：
- `LINCHAT_BASE_URL`   默认 http://127.0.0.1:8002
- `LINCHAT_DEVICE_TOKEN` 设备明文 token（缺失则 POST 必 401，条目留 pending）
- `WN_INGEST_DB`       本地 pending 库路径（测试用临时库；默认脚本同目录 ingest.db）

纯 stdlib，无第三方依赖。所有 env 在调用时读取（便于测试覆盖）。
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

INGEST_PATH = "/api/v1/internal/ingest/"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending(
  content_hash TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'wechat',
  created_at INTEGER
);
"""


def _secret(name, default=""):
    """密钥读取：环境变量优先，其次 ~/.wechat-narrator/secrets.json（与 wechat 侧惯例一致）。"""
    v = os.environ.get(name)
    if v:
        return v
    try:
        with open(os.path.join(os.path.expanduser("~"), ".wechat-narrator", "secrets.json")) as f:
            return json.load(f).get(name, default)
    except (OSError, ValueError):
        return default


def _db_path():
    return os.environ.get(
        "WN_INGEST_DB",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ingest.db"),
    )


def _conn():
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def _hash(name, content, source):
    h = hashlib.sha256()
    h.update(f"{source}\x00{name}\x00{content}".encode("utf-8"))
    return h.hexdigest()


def enqueue(name, content, source="wechat"):
    """写入本地 pending（幂等：同 (source,name,content) → 同 hash → INSERT OR IGNORE）。
    返回 content_hash。**本地写失败会抛**（供 ingest 传播给 msg_synth 降级）。"""
    ch = _hash(name, content, source)
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO pending(content_hash,name,content,source,created_at) "
            "VALUES(?,?,?,?,?)",
            (ch, name, content, source, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    return ch


def _post(name, content, source):
    """POST 到 LinChat 内部摄入端点；非 2xx / 网络错误抛异常。"""
    base = _secret("LINCHAT_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
    token = _secret("LINCHAT_DEVICE_TOKEN", "")
    url = base + INGEST_PATH
    body = json.dumps(
        {"content": content, "name": name, "source": source}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Device-Token", token)
    with urllib.request.urlopen(req, timeout=15) as resp:
        code = resp.getcode()
        if code not in (200, 201):
            raise RuntimeError(f"ingest HTTP {code}")


def flush_pending():
    """尽力投递所有 pending：成功即删除，遇失败停止（保留该条及其后所有条目）。
    返回本次成功投递条数。不抛（网络错误吞掉，留待下次）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT content_hash,name,content,source FROM pending ORDER BY created_at, content_hash"
        ).fetchall()
    finally:
        conn.close()
    flushed = 0
    for ch, name, content, source in rows:
        try:
            _post(name, content, source)
        except Exception:
            break  # LinChat 离线/异常：保留本条及余下，下次补投
        conn = _conn()
        try:
            conn.execute("DELETE FROM pending WHERE content_hash=?", (ch,))
            conn.commit()
        finally:
            conn.close()
        flushed += 1
    return flushed


def ingest(day=None, summary=None, source="wechat", name=None):
    """msg_synth 契约入口。返回 dict {status, flushed}。
    空 summary → noop；否则先本地入队（失败抛），再尽力 flush（失败不抛）。"""
    content = summary
    key = name or day
    if not content or not str(content).strip():
        return {"status": "noop", "flushed": 0}
    enqueue(key, content, source)  # 本地失败 → 抛 → msg_synth 降级
    try:
        flushed = flush_pending()
    except Exception:
        flushed = 0
    return {"status": "ok", "flushed": flushed}


def _main(argv):
    if "--flush" in argv:
        n = flush_pending()
        print(f"flushed {n} pending item(s)")
        return 0
    print("usage: linchat_ingest_client.py --flush", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
