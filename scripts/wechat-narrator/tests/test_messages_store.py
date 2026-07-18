#!/usr/bin/env python3
"""messages_store + msg_synth 单测（stdlib unittest，临时 db，绝不碰生产 messages.db）。

运行：cd scripts/wechat-narrator && python3 -m unittest tests.test_messages_store -v
"""
import importlib
import os
import sys
import tempfile
import time
import unittest

# 让 tests/ 能 import 同目录上一级的模块
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class MessagesStoreTest(unittest.TestCase):
    def setUp(self):
        # 每个用例一个独立临时 db，绝不碰生产库
        fd, self.db_path = tempfile.mkstemp(prefix="wn_test_", suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # 让 sqlite 自建，避免 0 字节文件干扰
        os.environ["WN_MESSAGES_DB"] = self.db_path
        # 重新 import 使模块级 DB_PATH 读到新环境变量
        import messages_store
        importlib.reload(messages_store)
        self.ms = messages_store

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        os.environ.pop("WN_MESSAGES_DB", None)

    def test_tee_inserts(self):
        self.assertTrue(self.ms.tee("群A", "群", "group", "你好", ts=1_700_000_000))
        rows = self.ms.fetch_pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "群A")
        self.assertEqual(rows[0]["sender"], "group")
        self.assertEqual(rows[0]["text"], "你好")

    def test_tee_idempotent_same_day(self):
        ts = 1_700_000_000
        self.assertTrue(self.ms.tee("群A", "群", "group", "重复", ts=ts))
        # 同日同人同文本第二次 → 去重合一，返回 False，仍只 1 行
        self.assertFalse(self.ms.tee("群A", "群", "group", "重复", ts=ts + 5))
        self.assertEqual(len(self.ms.fetch_pending()), 1)

    def test_tee_distinct_across_days(self):
        day1 = 1_700_000_000                       # 某天
        day2 = day1 + 86400 * 2                     # 两天后，day_bucket 不同
        self.assertTrue(self.ms.tee("群A", "群", "group", "同文本", ts=day1))
        self.assertTrue(self.ms.tee("群A", "群", "group", "同文本", ts=day2))
        self.assertEqual(len(self.ms.fetch_pending()), 2)

    def test_tee_never_raises(self):
        # 指向不可写路径 → tee 只返回 False，绝不抛
        os.environ["WN_MESSAGES_DB"] = "/proc/nonexistent/cannot/write.db"
        importlib.reload(self.ms)
        try:
            self.assertFalse(self.ms.tee("x", "群", "group", "y"))
        finally:
            os.environ["WN_MESSAGES_DB"] = self.db_path
            importlib.reload(self.ms)

    def test_private_routing(self):
        # 私聊三角色：self / peer / assistant 都能落库且各自成行
        ts = 1_700_100_000
        self.assertTrue(self.ms.tee("灰灰", "私信", "peer", "在吗", ts=ts))
        self.assertTrue(self.ms.tee("灰灰", "私信", "self", "在的", ts=ts))
        self.assertTrue(self.ms.tee("灰灰", "私信", "assistant", "老公在", ts=ts))
        senders = sorted(r["sender"] for r in self.ms.fetch_pending())
        self.assertEqual(senders, ["assistant", "peer", "self"])

    def test_group_flood_routing(self):
        # 洪峰 sender 缺失 → tee 归一 "unknown"
        ts = 1_700_200_000
        self.assertTrue(self.ms.tee("群B", "群", None, "无名发言", ts=ts))
        rows = self.ms.fetch_pending()
        self.assertEqual(rows[0]["sender"], "unknown")

    def test_mark_ingested(self):
        ts = 1_700_300_000
        self.ms.tee("群C", "群", "group", "一", ts=ts)
        self.ms.tee("群C", "群", "group", "二", ts=ts + 1)
        pending = self.ms.fetch_pending()
        self.assertEqual(len(pending), 2)
        n = self.ms.mark_ingested([r["id"] for r in pending])
        self.assertEqual(n, 2)
        self.assertEqual(self.ms.fetch_pending(), [])
        # 幂等：再 mark 已 mark 的不抛、不改变结果（仍无 pending）
        self.ms.mark_ingested([r["id"] for r in pending])
        self.assertEqual(self.ms.fetch_pending(), [])

    def test_fetch_pending_by_day(self):
        d1 = time.mktime(time.strptime("2026-07-10", "%Y-%m-%d"))
        d2 = time.mktime(time.strptime("2026-07-11", "%Y-%m-%d"))
        self.ms.tee("群D", "群", "group", "十号", ts=int(d1))
        self.ms.tee("群D", "群", "group", "十一号", ts=int(d2))
        day = time.strftime("%Y-%m-%d", time.localtime(d1))
        rows = self.ms.fetch_pending(day=day)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "十号")


class MsgSynthDegradeTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="wn_synth_", suffix=".db")
        os.close(fd)
        os.remove(self.db_path)
        os.environ["WN_MESSAGES_DB"] = self.db_path
        import messages_store
        importlib.reload(messages_store)
        self.ms = messages_store
        import msg_synth
        importlib.reload(msg_synth)
        self.synth = msg_synth

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        os.environ.pop("WN_MESSAGES_DB", None)

    def test_synth_degrades_on_gateway_down(self):
        # 落一条昨日消息
        ts = int(time.time() - 86400)
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        self.ms.tee("灰灰", "私信", "peer", "网关离线测试", ts=ts)

        # mock _synthesize 抛异常（模拟网关离线）
        orig = self.synth._synthesize

        def _boom(*a, **k):
            raise RuntimeError("gateway offline")

        self.synth._synthesize = _boom
        try:
            rc = self.synth.main(["--day", day])
        finally:
            self.synth._synthesize = orig

        # 退出非 0，且消息未被 mark（仍 pending）
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.ms.fetch_pending(day=day)), 1)

    def test_synth_empty_day_exits_zero(self):
        rc = self.synth.main(["--day", "2000-01-01"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
