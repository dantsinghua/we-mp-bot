#!/usr/bin/env python3
"""公众号知识底座 —— we-mp-rss 全文 → SQLite FTS5 (trigram) 索引 + 检索。

微信外脑 v3 · Phase A（知识面）。旁路只读 we-mp-rss 主库，索引写入独立 sidecar 库，
零新增服务、零嵌入、零 GPU 依赖。供 LinChat graph 的 oa_search 工具调用。

用法:
    python3 oa_indexer.py build            # 增量索引（只补新文章）
    python3 oa_indexer.py build --rebuild  # 全量重建
    python3 oa_indexer.py search "具身智能" [limit]
    python3 oa_indexer.py stats

设计:
  - 源库 we-mp-rss db.db 以 mode=ro 打开，绝不写它（root 属主，采集进程在写）。
  - content 是 HTML，剥标签成纯文本再进 FTS，否则 trigram 会索引到标签污染召回。
  - 幂等: indexed(article_id) 主键去重，build 只补未索引的；--rebuild 清空重来。
"""
import html
import os
import re
import sqlite3
import sys
import time
from html.parser import HTMLParser

WERSS_DB = os.environ.get(
    "WERSS_DB", "/home/dantsinghua/clawd/scripts/wechat-narrator/wemprss-data/db.db")
FTS_DB = os.environ.get(
    "OA_FTS_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "oa_fts.db"))

_WS_RE = re.compile(r"[ \t 　]+")
_NL_RE = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """剥 HTML 取可读正文，丢弃 script/style，块级标签补换行。"""

    _SKIP = {"script", "style", "head"}
    _BLOCK = {"p", "div", "br", "section", "li", "h1", "h2", "h3", "h4", "tr", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._buf = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._buf.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data:
            self._buf.append(data)

    def text(self):
        raw = "".join(self._buf)
        raw = _WS_RE.sub(" ", raw)
        raw = _NL_RE.sub("\n\n", raw)
        return raw.strip()


def html_to_text(s):
    if not s:
        return ""
    try:
        p = _TextExtractor()
        p.feed(s)
        return p.text()
    except Exception:
        # 极端畸形 HTML 兜底: 粗暴去标签
        return _WS_RE.sub(" ", re.sub(r"<[^>]+>", " ", html.unescape(s))).strip()


def _connect_src():
    return sqlite3.connect(f"file:{WERSS_DB}?mode=ro", uri=True)


def _connect_fts():
    db = sqlite3.connect(FTS_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    return db


def _ensure_schema(fts):
    fts.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS oa USING fts5("
        "  article_id UNINDEXED, mp_name, title, body,"
        "  url UNINDEXED, publish_ts UNINDEXED,"
        "  tokenize='trigram')")
    fts.execute("CREATE TABLE IF NOT EXISTS indexed (article_id TEXT PRIMARY KEY, ts INTEGER)")
    fts.commit()


def _mp_map(src):
    return {r[0]: (r[1] or "") for r in src.execute("SELECT id, mp_name FROM feeds")}


def build(rebuild=False):
    t0 = time.time()
    src = _connect_src()
    fts = _connect_fts()
    _ensure_schema(fts)
    if rebuild:
        fts.execute("DELETE FROM oa")
        fts.execute("DELETE FROM indexed")
        fts.commit()
        print("[build] rebuild: cleared existing index")

    done = {r[0] for r in fts.execute("SELECT article_id FROM indexed")}
    mp = _mp_map(src)
    rows = src.execute(
        "SELECT id, mp_id, title, url, publish_time, content "
        "FROM articles WHERE has_content=1 AND content IS NOT NULL AND length(content) > 0")

    added = skipped = 0
    batch = []
    for aid, mp_id, title, url, pub, content in rows:
        if aid in done:
            skipped += 1
            continue
        body = html_to_text(content)
        title = (title or "").strip()
        # 图片型推文正文剥离后为空; 只要标题非空仍索引(标题可搜), 二者皆空才跳过
        if not body and not title:
            skipped += 1
            continue
        batch.append((aid, mp.get(mp_id, ""), title, body, url or "", int(pub or 0)))
        if len(batch) >= 200:
            _flush(fts, batch)
            added += len(batch)
            batch = []
            print(f"[build] indexed {added} (+{skipped} skipped) ...")
    if batch:
        _flush(fts, batch)
        added += len(batch)

    if rebuild:
        # DELETE 不回收磁盘页, rebuild 后 checkpoint + VACUUM 收缩文件
        fts.commit()
        fts.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        fts.execute("VACUUM")

    total = fts.execute("SELECT COUNT(*) FROM oa").fetchone()[0]
    fts.close()
    src.close()
    print(f"[build] done: +{added} new, {skipped} skipped, total {total} docs, "
          f"{time.time()-t0:.1f}s, db={FTS_DB} ({os.path.getsize(FTS_DB)//1048576}MB)")


def _flush(fts, batch):
    fts.executemany(
        "INSERT INTO oa(article_id, mp_name, title, body, url, publish_ts) "
        "VALUES(?,?,?,?,?,?)", batch)
    fts.executemany(
        "INSERT OR IGNORE INTO indexed(article_id, ts) VALUES(?, strftime('%s','now'))",
        [(b[0],) for b in batch])
    fts.commit()


def _fts_quote(query):
    # trigram 需要 ≥3 字符; 用双引号包裹作短语，转义内部引号
    q = (query or "").strip().replace('"', '""')
    return f'"{q}"'


def search(query, limit=5):
    if not os.path.exists(FTS_DB):
        return []
    fts = _connect_fts()
    q = (query or "").strip()
    try:
        if len(q) < 3:
            # trigram 需 ≥3 字符; 短词(名/缩写)退化为 标题+公众号名 LIKE 兜底
            like = f"%{q}%"
            rows = fts.execute(
                "SELECT mp_name, title, url, publish_ts, substr(body,1,80) AS snip "
                "FROM oa WHERE title LIKE ? OR mp_name LIKE ? "
                "ORDER BY publish_ts DESC LIMIT ?",
                (like, like, int(limit))).fetchall()
        else:
            rows = fts.execute(
                "SELECT mp_name, title, url, publish_ts, "
                "  snippet(oa, 3, '<<', '>>', ' … ', 12) AS snip "
                "FROM oa WHERE oa MATCH ? ORDER BY rank LIMIT ?",
                (_fts_quote(q), int(limit))).fetchall()
    except sqlite3.OperationalError as e:
        fts.close()
        raise ValueError(f"FTS query failed: {e}")
    fts.close()
    out = []
    for mp_name, title, url, pub, snip in rows:
        date = time.strftime("%Y-%m-%d", time.localtime(pub)) if pub else ""
        snip = re.sub(r"\s+", " ", snip or "").strip()
        out.append({"mp_name": mp_name, "title": title, "url": url,
                    "date": date, "snippet": snip})
    return out


def stats():
    if not os.path.exists(FTS_DB):
        print("no index yet")
        return
    fts = _connect_fts()
    n = fts.execute("SELECT COUNT(*) FROM oa").fetchone()[0]
    size = os.path.getsize(FTS_DB) // 1048576
    print(f"docs={n}  size={size}MB  db={FTS_DB}")
    fts.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "build":
        build(rebuild="--rebuild" in sys.argv)
    elif cmd == "search":
        q = sys.argv[2] if len(sys.argv) > 2 else ""
        lim = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        for i, r in enumerate(search(q, lim), 1):
            print(f"{i}. [{r['mp_name']}｜{r['date']}] {r['title']}")
            print(f"   {r['snippet']}")
            print(f"   {r['url']}")
    elif cmd == "stats":
        stats()
    else:
        print(__doc__)
