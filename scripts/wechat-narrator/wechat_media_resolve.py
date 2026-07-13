#!/usr/bin/env python3
"""微信群媒体消息解析 —— 被 wechat_group_to_email.py 调用。

会话列表预览里图片/语音只显示 [Photo]/[Audio] 占位。本模块在检测到这些占位时：
  1. 打开该群聊天窗口（AT-SPI 定位会话 + 点击）
  2. 图片：定位最新 `list item name="Image"` → ffmpeg 截取 → OpenClaw 多模态模型描述
  3. 语音：微信自带"转文字"（方案 A，TODO 待有语音消息验证 UI 流程）
  4. 返回会话列表（Esc），把占位替换成解析结果

注意：打开聊天窗口会短暂影响 narrator/group2email 的会话列表轮询（约 3-4s），
处理完 Esc 返回即恢复。
"""
import base64
import json
import os
import re
import subprocess
import time
import urllib.request

import pyatspi


def _secret(name, default=""):
    """密钥读取：环境变量优先，其次 ~/.wechat-narrator/secrets.json（仓库外，0600）。"""
    v = os.environ.get(name)
    if v:
        return v
    try:
        with open(os.path.join(os.path.expanduser("~"), ".wechat-narrator", "secrets.json")) as f:
            return json.load(f).get(name, default)
    except (OSError, ValueError):
        return default


OPENCLAW_URL = os.environ.get("OPENCLAW_URL", "http://127.0.0.1:18789/v1/chat/completions")
OPENCLAW_TOKEN = _secret("OPENCLAW_TOKEN")
VISION_MODEL = os.environ.get("OPENCLAW_VISION_MODEL", "bailian/qwen3.5-plus")
DISPLAY = os.environ.get("DISPLAY", ":99")

# 图片归档目录：截取的图片存这里，同时作为邮件附件
IMG_DIR = os.path.join(os.path.expanduser("~"), ".wechat-narrator", "images")
os.makedirs(IMG_DIR, exist_ok=True)


def _safe(name):
    """群名转成安全文件名片段。"""
    return re.sub(r"[^\w一-鿿]+", "_", (name or "img")).strip("_")[:40] or "img"


def _apps():
    d = pyatspi.Registry.getDesktop(0)
    for i in range(d.childCount):
        a = d.getChildAtIndex(i)
        if "wechat" in (a.name or "").lower():
            yield a


def find_group_coord(group_name):
    """会话列表里找群会话，返回中心坐标 (x,y)。

    只认左侧会话列表(e.x<280)且 name 以目标名开头的行。
    历史教训(2026-07-09 误发事故)：不限区域的全名子串匹配会命中
    聊天区里的消息行(如「Quote 灰灰's message」引用行)，点击后会话
    不切换，后续读历史/回复全部落在错误会话。"""
    def f(n):
        try:
            if (n.getRoleName() == "list item"
                    and (n.name or "").startswith(group_name)
                    and n.getState().contains(pyatspi.STATE_SHOWING)):
                e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                if e.width > 0 and e.x < 280:
                    return (e.x + e.width // 2, e.y + e.height // 2)
            for c in n:
                if c:
                    r = f(c)
                    if r:
                        return r
        except Exception:
            pass
        return None
    for a in _apps():
        r = f(a)
        if r:
            return r
    return None


def _latest_node(name_prefix):
    """聊天区找 name 以 name_prefix 开头的 list item，取 y 最大(最新)的 extents。"""
    found = []

    def w(n):
        try:
            if (n.getRoleName() == "list item"
                    and (n.name or "").strip().startswith(name_prefix)
                    and n.getState().contains(pyatspi.STATE_SHOWING)):
                e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                if e.x > 280 and e.width > 0:
                    found.append((e.x, e.y, e.width, e.height))
            for c in n:
                if c:
                    w(c)
        except Exception:
            pass
    for a in _apps():
        w(a)
    return max(found, key=lambda x: x[1]) if found else None


def capture_image(group_name):
    """截取最新图片消息存成文件，返回文件路径；失败返回 None。"""
    box = _latest_node("Image")
    if not box:
        return None
    x, y, ww, hh = box
    png = os.path.join(IMG_DIR, f"{int(time.time())}_{_safe(group_name)}.png")
    subprocess.run(["ffmpeg", "-y", "-f", "x11grab", "-video_size", f"{ww}x{hh}",
                    "-i", f"{DISPLAY}.0+{x},{y}", "-frames:v", "1", png],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return png if os.path.exists(png) else None


# 多模态"看不到图"类拒答特征(模型侧偶发故障/failover到非多模态模型时出现,
# 2026-07-13 观测)。命中则视为无效描述,重试一次,仍失败由调用方回退占位。
REFUSAL_RE = re.compile(
    r"无法(访问|查看|识别|打开)|没有(收到|看到|接收)|未(收到|提供)|看不到|无法获取|重新发送")


def _describe_once(img_b64):
    body = {"model": "openclaw/default", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "这是微信群里的一张图片，用一句话简短描述内容"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img_b64}}]}]}
    req = urllib.request.Request(
        OPENCLAW_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                 "x-openclaw-model": VISION_MODEL}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def describe_image(png):
    """把图片文件交给 OpenClaw 多模态描述。

    返回有效描述文本；模型拒答(看不到图)时重试一次；两次都拒答/失败返回 None。
    调用方(resolve_media)拿到 None 会回退成「[图片(见附件)]」，不把废话写进邮件。
    """
    try:
        with open(png, "rb") as fp:
            img = base64.b64encode(fp.read()).decode()
    except Exception:
        return None
    for _ in range(2):
        desc = _describe_once(img)
        if desc and not REFUSAL_RE.search(desc):
            return desc
        if desc is None:
            break                       # 网络/接口错误,重试同样会失败,不空转
        time.sleep(1)                   # 拒答:等 1 秒再试一次(避开瞬时 failover)
    return None


# 语音 list item 前缀，转写文字拼在其后：'Audio11"sec我们怎么调…'
AUDIO_PREFIX = re.compile(r'^Audio\s*\d+\s*["\'″]?\s*sec', re.I)


def _latest_audio():
    """找最新(y最大)语音 list item，返回 ((x,y,w,h), name)。"""
    found = []

    def w(n):
        try:
            nm = (n.name or "").strip()
            if (n.getRoleName() == "list item" and nm.startswith("Audio")
                    and n.getState().contains(pyatspi.STATE_SHOWING)):
                e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                if e.x > 280 and e.width > 0:
                    found.append(((e.x, e.y, e.width, e.height), nm))
            for c in n:
                if c:
                    w(c)
        except Exception:
            pass
    for a in _apps():
        w(a)
    return max(found, key=lambda t: t[0][1]) if found else None


def _voice_text(name):
    """从语音 list item name 提取转写文字；未转写/失败返回 None。"""
    body = AUDIO_PREFIX.sub("", name).strip()
    if not body or body.lower() == "unplay" or "conversion failed" in body.lower():
        return None
    return body


def transcribe_voice():
    """方案A：微信自带语音转文字。关键点：
      - 触发用键盘导航(右键→Down→Return)，坐标点击 Qt 菜单项不生效；
      - 转写文字会拼进语音 list item 的 name('AudioNN"sec<文字>')，AT-SPI 直读，无需 OCR。
    成功返回文字；唱歌/失败/超时返回 None(保留占位)。"""
    nd = _latest_audio()
    if not nd:
        return None
    (x, y, w, h), name0 = nd
    already = _voice_text(name0)
    if already:                      # 已转写过，直接读
        return already
    # 右键语音气泡(群对方消息靠左，气泡在行左下部) → 键盘激活第一项 Audio to Text
    subprocess.run(["xdotool", "mousemove", str(x + 95), str(y + h - 22), "click", "3"])
    time.sleep(1.3)
    subprocess.run(["xdotool", "key", "Down"]); time.sleep(0.4)
    subprocess.run(["xdotool", "key", "Return"]); time.sleep(0.3)
    for _ in range(20):              # 轮询最多 ~30s
        time.sleep(1.5)
        cur = _latest_audio()
        if not cur:
            continue
        if "conversion failed" in cur[1].lower():
            return None
        txt = _voice_text(cur[1])
        if txt:
            return txt
    return None


def _scroll_list(direction, times=4):
    """滚动左侧会话列表。direction: 'down'|'up'。"""
    subprocess.run(["xdotool", "mousemove", "250", "400"])
    btn = "5" if direction == "down" else "4"
    for _ in range(times):
        subprocess.run(["xdotool", "click", btn]); time.sleep(0.12)


def find_group_scroll(group_name, tries=6):
    """会话列表定位群；不在可见区则下滚查找。返回 (坐标, 滚动轮数)。"""
    pos = find_group_coord(group_name)
    if pos:
        return pos, 0
    for i in range(1, tries + 1):
        _scroll_list("down")
        time.sleep(0.5)
        pos = find_group_coord(group_name)
        if pos:
            return pos, i
    return None, tries


def _chat_loaded():
    """聊天区是否已加载消息(有 list item)，用于确认会话真的打开了。"""
    for a in _apps():
        stack = [a]
        while stack:
            n = stack.pop()
            try:
                if (n.getRoleName() == "list item"
                        and n.getState().contains(pyatspi.STATE_SHOWING)):
                    e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                    if e.x > 280 and e.width > 0:
                        return True
                for c in n:
                    if c:
                        stack.append(c)
            except Exception:
                pass
    return False


DM_PINNED = os.environ.get("WN_DM_PINNED", "灰灰")   # 置顶私聊,规范态锚点(须保持置顶)
_canon = {"fails": 0, "last_alert": 0.0}


def _list_rows():
    """左侧会话列表当前可见的行 [(y, name)]。"""
    rows = []
    for a in _apps():
        stack = [a]
        while stack:
            n = stack.pop()
            try:
                nm = (n.name or "")
                if (n.getRoleName() == "list item" and nm
                        and n.getState().contains(pyatspi.STATE_SHOWING)):
                    e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                    if e.x < 280 and e.width > 100:
                        rows.append((e.y, nm))
                for c in n:
                    if c:
                        stack.append(c)
            except Exception:
                pass
    return rows


def _forensic_shot(tag):
    """告警取证截图,返回文件路径(失败返回 None)。"""
    d = os.path.join(os.path.expanduser("~"), ".wechat-narrator", "logs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"forensic_{tag}_{int(time.time())}.png")
    try:
        subprocess.run(["import", "-window", "root", p],
                       env={**os.environ, "DISPLAY": DISPLAY},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return p if os.path.exists(p) else None
    except Exception:
        return None


def ensure_canonical(alert_cb=None):
    """规范态自愈:会话列表须在顶部、置顶私聊行(DM_PINNED)可见。

    背景(2026-07-11/13 两次私聊失明):微信打开列表下方会话时会自动滚动列表,
    置顶区被顶出视口后轮询对私聊行全盲。本函数每轮轮询前调用:
      可见 → 直接返回 True(一次 AT-SPI 读,零扰动);
      不可见 → 滚顶自愈再查;连续 2 次自愈失败 → 截图 + alert_cb 告警(限频 1 次/小时)。
    行数为 0 视作可能掉线(登录哨兵),同样走告警路径。
    alert_cb(subject, body, attachment_path_or_None)
    """
    rows = _list_rows()
    if any(nm.startswith(DM_PINNED) for _, nm in rows):
        _canon["fails"] = 0
        return True
    # 滚到绝对顶部:漂移深度不定(打开靠下的群会滚很远),固定次数可能回不到顶;
    # 列表到顶后多余滚动是无副作用空操作,故用足量次数保证到顶。
    _scroll_list("up", 30)
    time.sleep(0.8)
    rows = _list_rows()
    if any(nm.startswith(DM_PINNED) for _, nm in rows):
        _canon["fails"] = 0
        return True
    _canon["fails"] += 1
    if _canon["fails"] >= 2 and alert_cb and time.time() - _canon["last_alert"] > 3600:
        _canon["last_alert"] = time.time()
        shot = _forensic_shot("canon")
        if not rows:
            alert_cb("微信可能已掉线(会话列表为空)",
                     "连续 2 轮读不到任何会话行——大概率被登出或有弹窗遮挡。\n"
                     "请查看取证截图;需要扫码对 Claude 说「出二维码」。", shot)
        else:
            alert_cb(f"规范态自愈失败({DM_PINNED} 行不可见)",
                     f"滚顶后仍看不到「{DM_PINNED}」置顶行,当前可见 {len(rows)} 行。\n"
                     f"请确认该会话仍为置顶;私聊检测在恢复前处于失明状态。", shot)
    return False


def find_input_box():
    """当前打开会话的输入框：返回 (聊天标题, (x,y,w,h))；未找到返回 (None, None)。

    输入框是聊天区(e.x>280)内 EDITABLE 的 text 节点，其 name 即当前聊天标题
    (实测无成员数等后缀)；x>280 同时排除左上角 Search 框。"""
    for a in _apps():
        stack = [a]
        while stack:
            n = stack.pop()
            try:
                st = n.getState()
                if (n.getRoleName() == "text"
                        and st.contains(pyatspi.STATE_EDITABLE)
                        and st.contains(pyatspi.STATE_SHOWING)):
                    e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                    if e.x > 280 and e.width > 0:
                        return (n.name or "").strip(), (e.x, e.y, e.width, e.height)
                for c in n:
                    if c:
                        stack.append(c)
            except Exception:
                pass
    return None, None


def current_chat_title():
    """当前打开会话的标题(输入框 name)；无打开会话/未找到返回 None。"""
    return find_input_box()[0]


def _open_chat(pos):
    """点击会话并确认聊天区加载；落空则重试点击。返回是否成功。"""
    for _ in range(3):
        subprocess.run(["xdotool", "mousemove", str(pos[0]), str(pos[1]), "click", "1"])
        time.sleep(2.5)
        if _chat_loaded():
            return True
    return _chat_loaded()


def resolve_media(group_name, text):
    """text 含 [Photo]/[Audio] 时打开群处理媒体。

    返回 (增强后的 text, 附件文件路径列表)。图片会被截图存成文件，
    既替换占位为文字描述，也作为附件路径返回供邮件挂附件；语音走微信自带转文字。
    """
    if "[Photo]" not in text and "[Audio]" not in text:
        return text, []
    pos, scrolled = find_group_scroll(group_name)
    if not pos:
        if scrolled:
            _scroll_list("up", scrolled * 4 + 4)   # 滚回会话列表顶部
        return text, []
    if not _open_chat(pos):                          # 点击落空(聊天区空白)则重试
        if scrolled:
            _scroll_list("up", scrolled * 4 + 4)
        return text, []
    if current_chat_title() != group_name:           # 开窗校验：必须就是目标群
        if scrolled:
            _scroll_list("up", scrolled * 4 + 4)
        return text, []
    out, attachments = text, []
    try:
        if "[Photo]" in out:
            png = capture_image(group_name)
            if png:
                attachments.append(png)
                desc = describe_image(png)
                out = out.replace("[Photo]", f"[图片: {desc}]" if desc else "[图片(见附件)]")
        if "[Audio]" in out:
            txt = transcribe_voice()
            if txt:
                out = out.replace("[Audio]", f"[语音: {txt}]")
    finally:
        subprocess.run(["xdotool", "key", "Escape"])
        time.sleep(0.5)
        if scrolled:
            _scroll_list("up", scrolled * 4 + 4)   # 滚回会话列表顶部，避免干扰轮询
        time.sleep(0.5)
    return out, attachments
