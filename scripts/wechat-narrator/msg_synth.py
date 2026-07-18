#!/usr/bin/env python3
"""日合成：把某一天旁路落库的对话合成要点，摄入 LinChat 记忆（B1）。

流程：
  --day（默认昨日）
    → messages_store.fetch_pending(day)   取当天未摄入消息
    → 空 → 退出 0
    → 按 session 分组拼对话（assistant 标「AI老公」）
    → wechat_auto_reply._openclaw(...) 合成要点（≤10000 字）
    → 可插拔摄入 hook：
        尝试 import linchat_ingest_client（B2 产出）：
          有 → 调 ingest(...)，成功才 mark_ingested(ids)
          无 → 降级：写 ~/.wechat-narrator/synth/{day}.md，**不** mark_ingested
               （待 B2 接线后重跑摄入）
    → 网关离线 / 合成失败 → log 降级、不 mark、退出非 0

全程 try/except；任何异常不影响转发主流程（本脚本由 systemd timer 独立触发）。
"""
import os
import sys
import time

try:
    import messages_store
except Exception as e:  # pragma: no cover - import 失败极罕见
    sys.stderr.write(f"messages_store import failed: {e}\n")
    sys.exit(2)

SYNTH_DIR = os.path.join(os.path.expanduser("~"), ".wechat-narrator", "synth")

SYNTH_PROMPT = (
    "你是家庭助理。下面是某一天微信里发生的对话（含群消息、私聊、AI老公的自动回复）。"
    "请提炼当天值得长期记住的要点：家人动态、约定、待办、情绪、重要事实。"
    "用简洁中文分条列出，不超过 800 字；无要点则回复「（当日无值得记忆的要点）」。"
)


def _log(msg):
    try:
        from wn_logging import get_logger
        get_logger("msg_synth").info(msg)
    except Exception:
        pass
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _yesterday():
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))


def _group_by_session(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r["session"], []).append(r)
    return groups


def _render_dialogue(rows):
    """把一个 session 的消息拼成可读对话文本。"""
    lines = []
    for r in rows:
        sender = r.get("sender") or "unknown"
        if sender == "assistant":
            name = "AI老公"
        elif sender == "self":
            name = "我"
        elif sender == "peer":
            name = "对方"
        else:
            name = sender
        mt = r.get("msg_type") or "text"
        text = r.get("text") or ""
        if mt == "image" and not text:
            text = "[图片]"
        lines.append(f"{name}：{text}")
    return "\n".join(lines)


def _synthesize(day, rows):
    """调网关合成要点；失败抛异常（由 main 捕获降级）。"""
    import wechat_auto_reply as ar
    groups = _group_by_session(rows)
    blocks = []
    for session, msgs in groups.items():
        blocks.append(f"## 会话：{session}\n{_render_dialogue(msgs)}")
    convo = "\n\n".join(blocks)
    if len(convo) > 10000:
        convo = convo[:10000]
    messages = [
        {"role": "system", "content": SYNTH_PROMPT},
        {"role": "user", "content": f"日期：{day}\n\n{convo}"},
    ]
    return ar._openclaw(messages, ar.CHAT_MODEL, max_tokens=1200)


def _ingest(day, summary, rows):
    """可插拔摄入 hook。返回 True=已摄入（可 mark） / False=降级本地落盘（不 mark）。"""
    ids = [r["id"] for r in rows]
    try:
        import linchat_ingest_client  # B2 产出；缺失走降级
    except Exception:
        _degrade_to_file(day, summary)
        _log(f"linchat_ingest_client 缺失，降级写本地 md（{len(ids)} 条未 mark，待 B2 重跑）")
        return False
    try:
        linchat_ingest_client.ingest(day=day, summary=summary)
        return True
    except Exception as e:
        _degrade_to_file(day, summary)
        _log(f"摄入失败（{e}），降级写本地 md（{len(ids)} 条未 mark）")
        return False


def _degrade_to_file(day, summary):
    try:
        os.makedirs(SYNTH_DIR, exist_ok=True)
        path = os.path.join(SYNTH_DIR, f"{day}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {day} 对话日合成\n\n{summary}\n")
        _log(f"降级落盘 → {path}")
    except Exception as e:
        _log(f"降级落盘失败: {e}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    day = _yesterday()
    if "--day" in argv:
        i = argv.index("--day")
        if i + 1 < len(argv):
            day = argv[i + 1]

    rows = messages_store.fetch_pending(day)
    if not rows:
        _log(f"{day} 无未摄入消息，退出。")
        return 0

    _log(f"{day} 取到 {len(rows)} 条未摄入消息，开始合成。")
    try:
        summary = _synthesize(day, rows)
    except Exception as e:
        _log(f"网关合成失败（离线或异常）：{e} —— 不 mark，退出非 0，待重跑。")
        return 3
    if not summary:
        _log("合成结果为空，不 mark，退出非 0。")
        return 3

    if _ingest(day, summary, rows):
        n = messages_store.mark_ingested([r["id"] for r in rows])
        _log(f"摄入成功，已 mark {n} 条。")
        return 0
    # 降级：已落盘本地 md，不 mark，退出非 0 提示待 B2 重跑
    return 4


if __name__ == "__main__":
    sys.exit(main())
