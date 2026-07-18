#!/usr/bin/env python3
"""wechat_auto_reply 老公 channel（C2）单元测试 —— 纯 mock，不触发 AT-SPI / 不发微信。

导入 wechat_auto_reply 前先把 pyatspi / wechat_media_resolve / PIL 塞进 sys.modules
（MagicMock 桩），避免 import 时连 AT-SPI 总线或截图等真实副作用。

覆盖 generate_reply 文本分支的 LinChat 路由 + 降级：
1. 合法回声令牌 → 用其 reply，不调 _openclaw
2. 请求超时 → 降级 _openclaw
3. 非 200 → 降级 _openclaw
4. 回声令牌不符（channel/origin_peer 不匹配）→ 弃用，降级 _openclaw
5. 空 reply → 降级 _openclaw
6. 图片消息 → 不打 husband 端点，走原 _openclaw 多模态分支
"""
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

# ---- 在 import 目标模块前打桩，隔离 AT-SPI / 截图 / 图像库 ----
for _name in ("pyatspi", "wechat_media_resolve"):
    sys.modules.setdefault(_name, MagicMock())
# wechat_media_resolve 的具名导出需为可用属性（from ... import 绑定）
_wmr = sys.modules["wechat_media_resolve"]
_wmr.OPENCLAW_URL = "http://openclaw.local/v1/chat"
_wmr.OPENCLAW_TOKEN = "tok"

import wechat_auto_reply as war  # noqa: E402


class _FakeResp:
    """urlopen 返回的上下文管理器桩。"""

    def __init__(self, code=200, payload=None):
        self._code = code
        self._payload = payload if payload is not None else {}

    def getcode(self):
        return self._code

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _text_history(text="老公我今天好累"):
    return [
        {"kind": "text", "text": "早上好呀", "sender": "self"},
        {"kind": "text", "text": text, "sender": "peer"},
    ]


def _image_history():
    return [
        {"kind": "text", "text": "你看", "sender": "peer"},
        {"kind": "image", "text": "[图片]", "sender": "peer"},
    ]


class TestHusbandRouting(unittest.TestCase):
    # ---------- 1. 合法回声 → 用 LinChat reply，不降级 ----------
    def test_valid_echo_uses_linchat_reply(self):
        resp = _FakeResp(200, {"data": {"reply": "辛苦啦亲爱滴", "channel": "wechat", "origin_peer": war.PEER}})
        with patch.object(war.urllib.request, "urlopen", return_value=resp), \
                patch.object(war, "_openclaw") as m_oc:
            out = war.generate_reply(_text_history())
        self.assertEqual(out, "辛苦啦亲爱滴")
        m_oc.assert_not_called()

    # ---------- 2. 超时 → 降级 _openclaw ----------
    def test_timeout_degrades_to_openclaw(self):
        with patch.object(war.urllib.request, "urlopen", side_effect=TimeoutError("timed out")), \
                patch.object(war, "_openclaw", return_value="本地回复") as m_oc:
            out = war.generate_reply(_text_history())
        self.assertEqual(out, "本地回复")
        m_oc.assert_called_once()

    # ---------- 3. 非 200 → 降级 ----------
    def test_non_200_degrades(self):
        resp = _FakeResp(502, {"data": {"reply": "x", "channel": "wechat", "origin_peer": war.PEER}})
        with patch.object(war.urllib.request, "urlopen", return_value=resp), \
                patch.object(war, "_openclaw", return_value="本地回复") as m_oc:
            out = war.generate_reply(_text_history())
        self.assertEqual(out, "本地回复")
        m_oc.assert_called_once()

    # ---------- 4. 回声令牌不符 → 弃用，降级 ----------
    def test_echo_mismatch_degrades(self):
        # channel 回带 web（非 wechat）→ 视为串台，弃用
        resp = _FakeResp(200, {"data": {"reply": "污染回复", "channel": "web", "origin_peer": war.PEER}})
        with patch.object(war.urllib.request, "urlopen", return_value=resp), \
                patch.object(war, "_openclaw", return_value="本地回复") as m_oc:
            out = war.generate_reply(_text_history())
        self.assertEqual(out, "本地回复")
        m_oc.assert_called_once()

    def test_echo_peer_mismatch_degrades(self):
        resp = _FakeResp(200, {"data": {"reply": "给别人的", "channel": "wechat", "origin_peer": "别人"}})
        with patch.object(war.urllib.request, "urlopen", return_value=resp), \
                patch.object(war, "_openclaw", return_value="本地回复") as m_oc:
            out = war.generate_reply(_text_history())
        self.assertEqual(out, "本地回复")
        m_oc.assert_called_once()

    # ---------- 5. 空 reply → 降级 ----------
    def test_empty_reply_degrades(self):
        resp = _FakeResp(200, {"data": {"reply": "   ", "channel": "wechat", "origin_peer": war.PEER}})
        with patch.object(war.urllib.request, "urlopen", return_value=resp), \
                patch.object(war, "_openclaw", return_value="本地回复") as m_oc:
            out = war.generate_reply(_text_history())
        self.assertEqual(out, "本地回复")
        m_oc.assert_called_once()

    # ---------- 6. 图片消息 → 不打 husband 端点，走原 _openclaw ----------
    def test_image_does_not_call_husband(self):
        with patch.object(war, "_linchat_husband") as m_lh, \
                patch.object(war, "capture_image", return_value=None), \
                patch.object(war, "_openclaw", return_value="图片回复") as m_oc:
            out = war.generate_reply(_image_history())
        self.assertEqual(out, "图片回复")
        m_lh.assert_not_called()
        m_oc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
