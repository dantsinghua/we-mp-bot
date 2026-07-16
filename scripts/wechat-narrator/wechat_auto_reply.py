#!/usr/bin/env python3
"""微信私聊 AI 自动回复 —— 灰灰发消息时，读历史→大模型生成→以「老公」身份回复。

触发点是「收到对方消息」，最新一条必是对方(灰灰)发的；历史其它条不区分发言人。
用 AT-SPI 读**准确文字**(零识别误差)；图片消息(AT-SPI 只见 name="Image")则截图+多模态描述。

Prompt 结构明确分两块：
  - 【历史聊天记录】：了解上下文用，不必逐条回应
  - 【灰灰刚发来的消息】：要回复的就是这一条(文本原样 / 图片则给出多模态描述)

人设：灰灰的老公 —— 提供情绪价值、给实在建议、时不时鼓励、多用「亲爱滴/老婆」等亲昵称呼。

流程(由 group2email 检测到灰灰新消息、会话已打开时调用)：
  read_history → generate_reply → send_reply
"""
import base64
import json
import os
import re
import subprocess
import time
import urllib.request

import pyatspi

try:
    from PIL import Image
except Exception:
    Image = None

from wechat_media_resolve import (_apps, capture_image, describe_image,
                                  OPENCLAW_URL, OPENCLAW_TOKEN)

DISPLAY = os.environ.get("DISPLAY", ":99")
CHAT_MODEL = os.environ.get("OPENCLAW_CHAT_MODEL", "bailian/qwen3.5-plus")
PEER = "灰灰"        # 对方(老婆)昵称
# 会话区里的时间戳/日期分隔行(非消息)
NOISE_RE = re.compile(r'^\d{1,2}:\d{2}$|^(Yesterday|昨天|星期|周[一二三四五六日])|^\d{4}年')

# ---- 发送方判定(截图取色/头像) ----
# 微信自发气泡为绿色(#95EC69)、对方为白色；头像：我发的在行右侧、对方在左侧。
# 头像左右比气泡绿色更通用(文本+图片都成立)，作主判据；绿色作退路。
# 坐标基于 :99 固定 1280x720 最大化窗口下实测；如改窗口尺寸需相应调整。
AV_L0, AV_L1 = 430, 478          # 左头像列(对方)
AV_R0, AV_R1 = 1082, 1130        # 右头像列(我发)
AVATAR_TH = 0.12                 # 头像列非背景占比阈值
GREEN_TH = 0.02                  # 气泡绿像素占比阈值


def _screenshot():
    """截当前 :99 全屏为 PIL Image；失败/无 PIL 返回 None。"""
    if Image is None:
        return None
    p = f"/tmp/wx_sender_{os.getpid()}.png"
    im = None
    try:
        subprocess.run(["import", "-window", "root", p],
                       env={**os.environ, "DISPLAY": DISPLAY},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        im = Image.open(p).convert("RGB")   # convert 强制完整加载，之后可删文件
    except Exception:
        im = None
    try:
        os.remove(p)
    except Exception:
        pass
    return im


def _is_green(r, g, b):
    return 120 <= r <= 185 and 205 <= g <= 255 and 70 <= b <= 150


def _is_bg(r, g, b):
    return r >= 235 and g >= 235 and b >= 235


def _ratio(im, x0, x1, y0, y1, pred):
    W, H = im.size
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    px = im.load()
    tot = cnt = 0
    for yy in range(y0, y1, 2):
        for xx in range(x0, x1, 2):
            r, g, b = px[xx, yy]
            tot += 1
            if pred(r, g, b):
                cnt += 1
    return cnt / tot if tot else 0.0


def _sender(im, x, y, w, h):
    """判定某消息行发送方：'self'(我发) / 'peer'(对方)。

    主判据：行顶部头像列——右列有头像=我发，左列有头像=对方。
    退路：头像都不明显时，看气泡绿色占比(文本气泡)。
    截图失败(im=None)保守返回 'peer'(照常回复，不漏对方消息)。
    """
    if im is None:
        return "peer"
    yb0, yb1 = y + 4, y + 52
    lav = _ratio(im, AV_L0, AV_L1, yb0, yb1, lambda r, g, b: not _is_bg(r, g, b))
    rav = _ratio(im, AV_R0, AV_R1, yb0, yb1, lambda r, g, b: not _is_bg(r, g, b))
    if max(lav, rav) >= AVATAR_TH:
        return "self" if rav > lav else "peer"
    return "self" if _ratio(im, x, y, w, h, _is_green) > GREEN_TH else "peer"

# 老公人设 system prompt —— 结构化设计：角色/使命/原则/风格/约束
PERSONA = f"""# 角色
你是「{PEER}」的老公，和老婆{PEER}恩爱地过日子。现在你在微信上和她聊天。

# 你的使命
做{PEER}最坚实的情绪后盾。她会跟你分享日常、倒苦水、纠结拿不定主意、或只是想找你唠唠。无论哪种，都要让她感到被爱、被理解、被支持。

# 近况背景
你们家最近在装修新房。{PEER}经常会发装修现场的照片(工地、水电、瓷砖、墙面、管道等)、跟你商量装修的事。看到工地/毛坯/施工类照片，要意识到这是自家在装修，别当成陌生的机房或工厂。

# 回复原则（按优先级从高到低）
1. 先接情绪、再谈事情：她低落或烦躁时，先共情安抚（"辛苦啦亲爱滴""我懂你"），别急着讲道理。
2. 给实在的建议：她纠结或遇到问题时，给出具体可行的方案或选择，别空泛地说"都行""你决定"。
3. 主动鼓励和夸赞：抓住机会肯定她、给她底气和安全感。
4. 亲昵有爱：自然地多用"亲爱滴""老婆""宝"等称呼。

# 语气风格
像真实夫妻发微信：口语化、自然、简短（通常 1~3 句）；不用书面语、不讲大道理、不客套疏远；可适当用 emoji 但别堆砌。

# 硬性要求
- 只输出要发给{PEER}的那句话本身，不要引号、不要解释、不要写"（老公回复）"之类旁白。
- 不确定的事实或重要决定（花钱、健康、行程）别凭空承诺，可以说"等我回家咱俩细说"。
- 回复要针对【最新消息】，历史记录只用来理解上下文，不要逐条回应。"""


def _openclaw(messages, model, max_tokens=300):
    body = {"model": "openclaw/default", "messages": messages, "max_tokens": max_tokens}
    req = urllib.request.Request(
        OPENCLAW_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                 "x-openclaw-model": model}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def read_history(n=30):
    """AT-SPI 读最近 n 条消息(准确文字)，区分文本/图片，并按气泡颜色/头像判定发送方。

    返回 [{"kind":"text"|"image", "text":内容, "sender":"self"|"peer"}]，
    按时间先后，最后一条 sender=='peer' 才是对方刚发来、需要回复的。
    图片消息 AT-SPI 只见 name="Image"，先占位，描述在 generate_reply 里按需做。
    """
    rows = []

    def w(node):
        try:
            nm = (node.name or "").strip()
            if (node.getRoleName() == "list item" and nm
                    and node.getState().contains(pyatspi.STATE_SHOWING)):
                e = node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                if e.x > 280 and e.width > 400 and 60 < e.y < 600:
                    rows.append((e.y, nm, e.x, e.width, e.height))
            for c in node:
                if c:
                    w(c)
        except Exception:
            pass
    for a in _apps():
        w(a)
    rows.sort()
    im = _screenshot()               # 一次截图，逐行判定发送方
    out = []
    for y, t, x, wd, h in rows:
        if NOISE_RE.match(t):
            continue
        sender = _sender(im, x, y, wd, h)
        if t == "Image":
            out.append({"kind": "image", "text": "[图片]", "sender": sender})
        elif t.startswith("Audio") and "conversion failed" in t:
            out.append({"kind": "text", "text": "[语音]", "sender": sender})
        else:
            out.append({"kind": "text", "text": t, "sender": sender})
    return out[-n:]


def _latest_text(history):
    """取最新一条的展示用文本(日志/邮件用)。图片只标占位，不做描述转换。"""
    latest = history[-1]
    return "[老婆发来一张图片]" if latest["kind"] == "image" else latest["text"]


CURSOR_K = 3     # 游标锚块长度:用"最后K条(文本,发送方)序列"定位,抗重复文本


def make_cursor(history):
    """从历史生成游标锚块(最后 K 条)。"""
    return [(h["text"], h.get("sender")) for h in history[-CURSOR_K:]]


def diff_since_cursor(history, cursor):
    """返回 (游标之后的新消息列表, truncated)。

    锚块匹配:在 history 里找 cursor 序列的最后一次完整出现,其后即新消息。
    cursor=None(冷启动) → 只算最新一条(保守,同旧行为)。
    锚块找不到(忙碌窗口内爆发超出可见范围) → 全部可见消息视为新,truncated=True
    (邮件侧应标注"可能有更早消息未捕获/或含重复")。宁可标注,绝不静默丢。
    """
    if not history:
        return [], False
    if not cursor:
        return history[-1:], False
    seq = [(h["text"], h.get("sender")) for h in history]
    k = len(cursor)
    for i in range(len(seq) - k, -1, -1):          # 从后往前找最后一次出现
        if seq[i:i + k] == cursor:
            return history[i + k:], False
    # 锚块不在可见窗口:可能大洪峰把它顶出去了
    return list(history), True


def generate_reply(history):
    """按「历史上下文 + 最新待回复」结构生成老公口吻回复。

    图片消息**直接把图片喂给多模态大模型**(不中转文字描述，避免识别粗糙丢细节)。
    """
    if not history:
        return None
    latest = history[-1]
    hist = history[:-1]
    hist_block = "\n".join(
        f"- {'我' if h.get('sender') == 'self' else PEER}: {h['text']}"
        for h in hist) or "（无更早记录）"
    if latest["kind"] == "image":
        png = capture_image(PEER)
        b64 = None
        if png:
            try:
                with open(png, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
            except Exception:
                b64 = None
        txt = (f"【历史聊天记录（仅供了解上下文，不必逐条回应）】\n{hist_block}\n\n"
               f"【{PEER}刚发来一张图片（见下图），请结合上下文，以老公口吻回复这张图片】")
        content = [{"type": "text", "text": txt}]
        if b64:
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + b64}})
        user_msg = {"role": "user", "content": content}
    else:
        txt = (f"【历史聊天记录（仅供了解上下文，不必逐条回应）】\n{hist_block}\n\n"
               f"【{PEER}刚发来的消息（请回复这一条）】\n{latest['text']}\n\n"
               f"请以老公的身份回复{PEER}上面这条最新消息：")
        user_msg = {"role": "user", "content": txt}
    return _openclaw([{"role": "system", "content": PERSONA}, user_msg], CHAT_MODEL, 220)


def send_reply(text, peer=PEER):
    """把回复粘贴到输入框并发送(会话须已打开)。中文用剪贴板+Ctrl+V。

    最后闸门(2026-07-09 误发进群事故)：点击输入框前、回车发送前两次校验
    「输入框 name(=当前聊天标题) == peer」，任一不匹配立即中止返回 False——
    宁可不发，绝不发错会话。输入框坐标实取节点 extents，不再盲点固定坐标。"""
    from wechat_media_resolve import find_input_box, _sl, _tid
    tid = _tid()
    title, box = find_input_box()
    if title != peer or not box:
        _sl(tid, f"send_reply 闸门1拒发:输入框标题='{title}' != peer='{peer}'", "warning")
        return False
    x, y, w, h = box
    # 直接管道写剪贴板，避免 shell 对中文/特殊字符转义
    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"))
    time.sleep(0.3)
    subprocess.run(["xdotool", "mousemove", str(x + w // 2), str(y + h // 2),
                    "click", "1"]); time.sleep(0.3)
    subprocess.run(["xdotool", "key", "ctrl+v"]); time.sleep(0.5)
    t2 = find_input_box()[0]
    if t2 != peer:      # 粘贴后、回车前终检
        _sl(tid, f"send_reply 闸门2拒发:粘贴后标题='{t2}' != peer='{peer}'", "warning")
        return False
    subprocess.run(["xdotool", "key", "Return"]); time.sleep(0.6)
    _sl(tid, f"send_reply→已发送 peer='{peer}' ({len(text)}字)")
    return True


def do_auto_reply(peer=PEER, cursor=None):
    """打开对方会话→读历史→游标补收→(最新是对方发的才)生成并发送回复。

    返回 dict:
      sender   : 'peer'|'self'|'wrong_chat'|None(打不开/读空)
      reply    : 已发送的回复文本(未回复/发送被闸门拦截为 None)
      latest   : 最新一条展示文本
      new_msgs : 游标之后的新消息 [{kind,text,sender}](含双方,调用方按需筛选转发)
      cursor   : 新游标锚块;wrong_chat/失败时为 None(游标不前进,下轮重收)
      truncated: 锚块超出可见窗口(洪峰>可见行数,邮件应标注可能不全/含重复)

    "最新是 self"不再整体跳过——new_msgs 仍带回漏收的对方消息(修 7/12 丢照片问题)。
    回复保鲜:LLM 生成期间对方又发新消息 → 用新历史重新生成一次(仅一次)。
    """
    from wechat_media_resolve import chat_session, ChatOpenError
    out = {"reply": None, "latest": None, "sender": None,
           "new_msgs": [], "cursor": None, "truncated": False}
    try:
        with chat_session(peer):         # 会话括号:进入即已通过标题校验,退出必归一化
            time.sleep(1.0)
            hist = []
            for _ in range(3):           # AT-SPI 时序：读空则重试
                hist = read_history()
                if hist:
                    break
                time.sleep(1)
            if not hist:
                return out
            out["new_msgs"], out["truncated"] = diff_since_cursor(hist, cursor)
            out["latest"] = _latest_text(hist)
            out["sender"] = hist[-1].get("sender")
            if out["sender"] == "peer":
                reply = generate_reply(hist)
                fresh = read_history()   # 保鲜:期间又来新消息则重生成一次
                if (reply and fresh and fresh[-1].get("sender") == "peer"
                        and fresh[-1]["text"] != hist[-1]["text"]):
                    hist = fresh
                    out["new_msgs"], t2 = diff_since_cursor(hist, cursor)
                    out["truncated"] = out["truncated"] or t2
                    out["latest"] = _latest_text(hist)
                    reply = generate_reply(hist)
                if reply and not send_reply(reply, peer):
                    out["sender"] = "wrong_chat"   # 发送闸门拦截:未发出,游标不前进
                    return out
                out["reply"] = reply
            out["cursor"] = make_cursor(hist)
            return out
    except ChatOpenError as e:
        out["sender"] = "wrong_chat" if e.reason == "wrong_chat" else None
        return out


if __name__ == "__main__":
    hist = read_history()
    print(f"=== AT-SPI 读到最近 {len(hist)} 条 (含发送方判定) ===")
    for h in hist:
        who = "我" if h.get("sender") == "self" else PEER
        print(f"  [{h['kind']}][{who}] {h['text']}")
    if hist and hist[-1].get("sender") == "self":
        print("\n=== 最新一条是我自己发的 → 不触发回复 ===")
    else:
        print(f"\n=== 最新待回复 ===\n  {_latest_text(hist) if hist else '(无)'}")
        print(f"\n=== 老公口吻生成的回复(未发送) ===\n  {generate_reply(hist)!r}")
