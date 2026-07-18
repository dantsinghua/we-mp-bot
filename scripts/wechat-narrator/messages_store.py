#!/usr/bin/env python3
"""wechat-narrator 对话旁路落库（B1）。

在不影响「微信 → 邮件」转发主流程的前提下，把群/私聊/AI 回复逐条 tee 到本地
SQLite（messages.db），供后续「日合成 → LinChat 记忆摄入」（B2/C2）消费。

设计要点：
- **绝不影响主流程**：`tee()` 全程 try/except，任何异常只 log 不抛。
- **幂等去重权衡**：`content_hash` 用 `day_bucket`（当天日期）而非精确 `ts` 参与哈希，
  故 *同日 + 同 session + 同 sender + 同 msg_type + 同 text* 会被去重合一（INSERT OR
  IGNORE）。对「日合成要点」这个用途可接受：同一天里完全重复的一句话本就无需重复摄入；
  代价是无法区分同一天里字字相同的两条独立消息（罕见，且合成层不敏感）。
- **DB 路径**：环境变量 `WN_MESSAGES_DB` 覆盖（测试用临时 db），默认脚本同目录
  `messages.db`。
"""
import hashlib
import json
import os
import sqlite3
import time

DB_PATH = os.environ.get(
    "WN_MESSAGES_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT NOT NULL, kind TEXT NOT NULL,
  sender TEXT, msg_type TEXT DEFAULT 'text',
  text TEXT, assets TEXT,
  ts INTEGER, day_bucket TEXT,
  content_hash TEXT UNIQUE NOT NULL,
  ingested INTEGER DEFAULT 0, created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_msg_ingested ON messages(ingested, day_bucket);
CREATE INDEX IF NOT EXISTS idx_msg_session  ON messages(session, ts);
"""


def _log(msg):
    """诊断日志；日志层不可用时静默。"""
    try:
        from wn_logging import get_logger
        get_logger("messages_store").warning(msg)
    except Exception:
        pass


def _connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def _ensure_schema(db):
    db.executescript(_SCHEMA)


def _hash(session, kind, sender, msg_type, text, day):
    raw = f"{session}\x1f{kind}\x1f{sender}\x1f{msg_type}\x1f{text}\x1f{day}"
    return hashlib.sha256(raw.encode()).hexdigest()


def tee(session, kind, sender, text, msg_type="text", assets=None, ts=None):
    """旁路落库；任何异常只 log 不抛。返回 True=新入库 / False=去重或失败。

    幂等权衡：content_hash 用 day_bucket 而非精确 ts，故同日同 session 同 sender
    同 msg_type 同 text 会去重合一（对日合成要点用途可接受）。
    """
    try:
        ts = int(ts if ts is not None else time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        text = text or ""
        sender = sender or "unknown"
        h = _hash(session, kind, sender, msg_type, text, day)
        aj = json.dumps(assets, ensure_ascii=False) if assets else None
        db = _connect()
        try:
            _ensure_schema(db)
            cur = db.execute(
                "INSERT OR IGNORE INTO messages"
                "(session,kind,sender,msg_type,text,assets,ts,day_bucket,content_hash,ingested,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                (session, kind, sender, msg_type, text, aj, ts, day, h, int(time.time())),
            )
            db.commit()
            return cur.rowcount > 0
        finally:
            db.close()
    except Exception as e:
        _log(f"tee failed: {e}")
        return False


def fetch_pending(day=None, limit=5000):
    """取未摄入消息（ingested=0），可选按 day_bucket 过滤，按 ts 升序。

    返回 dict 列表；任何异常只 log 不抛，返回 []。
    """
    try:
        db = _connect()
        try:
            _ensure_schema(db)
            sql = ("SELECT id,session,kind,sender,msg_type,text,assets,ts,day_bucket"
                   " FROM messages WHERE ingested=0")
            params = []
            if day:
                sql += " AND day_bucket=?"
                params.append(day)
            sql += " ORDER BY ts LIMIT ?"
            params.append(int(limit))
            cols = ["id", "session", "kind", "sender", "msg_type", "text",
                    "assets", "ts", "day_bucket"]
            rows = db.execute(sql, params).fetchall()
            return [dict(zip(cols, r)) for r in rows]
        finally:
            db.close()
    except Exception as e:
        _log(f"fetch_pending failed: {e}")
        return []


def mark_ingested(ids):
    """标记指定 id 已摄入（ingested=1）。幂等；异常只 log 不抛，返回受影响行数。"""
    try:
        ids = [int(i) for i in (ids or [])]
        if not ids:
            return 0
        db = _connect()
        try:
            _ensure_schema(db)
            placeholders = ",".join("?" for _ in ids)
            cur = db.execute(
                f"UPDATE messages SET ingested=1 WHERE id IN ({placeholders})", ids)
            db.commit()
            return cur.rowcount
        finally:
            db.close()
    except Exception as e:
        _log(f"mark_ingested failed: {e}")
        return 0
