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
    """会话列表里找群会话，返回中心坐标 (x,y)。"""
    def f(n):
        try:
            if (n.getRoleName() == "list item" and group_name in (n.name or "")
                    and n.getState().contains(pyatspi.STATE_SHOWING)):
                e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                if e.width > 0:
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


def describe_image(png):
    """把图片文件交给 OpenClaw 多模态描述；失败返回 None。"""
    try:
        with open(png, "rb") as fp:
            img = base64.b64encode(fp.read()).decode()
    except Exception:
        return None
    body = {"model": "openclaw/default", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "这是微信群里的一张图片，用一句话简短描述内容"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}}]}]}
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
