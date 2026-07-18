#!/usr/bin/env python3
"""linchat_ingest_client 单测（stdlib unittest，临时 db + mock urlopen，绝不碰生产库/真实网络）。

运行：cd scripts/wechat-narrator && python3 -m unittest tests.test_ingest_client -v
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _FakeResp:
    """支持 with 上下文的伪 HTTP 响应。"""

    def __init__(self, code=201):
        self._code = code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self._code


class IngestClientTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="wn_ingest_test_", suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # 让 sqlite 自建
        os.environ["WN_INGEST_DB"] = self.db_path
        os.environ["LINCHAT_BASE_URL"] = "http://test-host:8002"
        os.environ["LINCHAT_DEVICE_TOKEN"] = "test-device-token"
        import linchat_ingest_client
        importlib.reload(linchat_ingest_client)
        self.c = linchat_ingest_client

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.environ.pop("WN_INGEST_DB", None)

    def _pending_count(self):
        import sqlite3
        if not os.path.exists(self.db_path):
            return 0  # noop 路径不建库
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
        except sqlite3.OperationalError:
            return 0  # 表未建
        finally:
            conn.close()

    def test_enqueues_and_flushes(self):
        """1. 正常路径：mock 201 → ingest 后 pending 清空；请求体含 name/content，头含 X-Device-Token。"""
        with mock.patch(
            "linchat_ingest_client.urllib.request.urlopen", return_value=_FakeResp(201)
        ) as m:
            res = self.c.ingest(day="2026-07-18", summary="今天团子很乖")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["flushed"], 1)
        self.assertEqual(self._pending_count(), 0)
        # 校验请求
        req = m.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/api/v1/internal/ingest/"))
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("X-device-token"), "test-device-token")
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["name"], "2026-07-18")
        self.assertEqual(payload["content"], "今天团子很乖")
        self.assertEqual(payload["source"], "wechat")

    def test_idempotent_hash(self):
        """2. 同 (source,name,content) 二次 enqueue → 只 1 条 pending。"""
        self.c.enqueue("2026-07-18", "同样的内容", "wechat")
        self.c.enqueue("2026-07-18", "同样的内容", "wechat")
        self.assertEqual(self._pending_count(), 1)

    def test_retries_then_keeps_on_down(self):
        """3. LinChat 离线（URLError）→ ingest 不抛，pending 保留；恢复后 flush 清空。"""
        import urllib.error
        with mock.patch(
            "linchat_ingest_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            res = self.c.ingest(day="2026-07-18", summary="离线时的要点")  # 不应抛
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["flushed"], 0)
        self.assertEqual(self._pending_count(), 1)  # 条目保留

        # 恢复：再次 flush → 成功清空
        with mock.patch(
            "linchat_ingest_client.urllib.request.urlopen", return_value=_FakeResp(201)
        ):
            flushed = self.c.flush_pending()
        self.assertEqual(flushed, 1)
        self.assertEqual(self._pending_count(), 0)

    def test_empty_summary_noop(self):
        """4. 空 summary → noop：不入队、不 POST。"""
        with mock.patch(
            "linchat_ingest_client.urllib.request.urlopen"
        ) as m:
            res1 = self.c.ingest(day="2026-07-18", summary="")
            res2 = self.c.ingest(day="2026-07-18", summary="   ")
            res3 = self.c.ingest(day="2026-07-18", summary=None)
        self.assertEqual(res1["status"], "noop")
        self.assertEqual(res2["status"], "noop")
        self.assertEqual(res3["status"], "noop")
        self.assertEqual(self._pending_count(), 0)
        m.assert_not_called()

    def test_local_persist_failure_raises(self):
        """5. 本地 pending 写失败（db 路径不可用）→ ingest 抛（供 msg_synth 降级不 mark）。"""
        os.environ["WN_INGEST_DB"] = "/nonexistent_dir_xyz_wn/ingest.db"
        importlib.reload(self.c)
        with mock.patch("linchat_ingest_client.urllib.request.urlopen") as m:
            with self.assertRaises(Exception):
                self.c.ingest(day="2026-07-18", summary="内容")
        m.assert_not_called()  # 本地入队先于 POST，入队失败即抛


if __name__ == "__main__":
    unittest.main()
