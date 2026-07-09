#!/usr/bin/env python3
"""微信订阅号(公众号)消息解析 —— 被 wechat_group_to_email.py 调用。

订阅号消息聊天区是纯 Qt/CEF 自绘，AT-SPI 完全读不到（0 节点），
所以走「截图 + 多模态」路线：
  1. 打开「Official Accounts」订阅号信息流
  2. 截图信息流区域 → OpenClaw 多模态提取每篇文章 (公众号名/标题/时间)
  3. (可选) 逐篇点开 → "..."菜单 Copy Link → 拿 mp.weixin.qq.com 原文链接
  4. 返回文章列表，供 group2email 组织成邮件

注意：文章正文有反爬(curl/WebFetch 拿到的是验证页)，链接需在微信客户端内打开，
所以服务端只转发「标题+链接」，正文由用户点链接自行阅读。
"""
import base64
import json
import os
import re
import subprocess
import time
import urllib.request

import pyatspi

from wechat_media_resolve import (_apps, find_group_scroll, _scroll_list,
                                  OPENCLAW_URL, OPENCLAW_TOKEN, VISION_MODEL)

DISPLAY = os.environ.get("DISPLAY", ":99")
# 信息流截图区域(右侧内容区)；订阅号入口标题栏在顶部，内容从 y~90 起
FEED_REGION = (390, 88, 720, 600)   # x, y, w, h


def _feed_screenshot():
    """截订阅号信息流区域，返回 PNG 路径。"""
    x, y, w, h = FEED_REGION
    png = "/tmp/wx_oa_%d.png" % int(time.time())
    subprocess.run(["ffmpeg", "-y", "-f", "x11grab", "-video_size", f"{w}x{h}",
                    "-i", f"{DISPLAY}.0+{x},{y}", "-frames:v", "1", png],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return png if os.path.exists(png) else None


def _vision(png, prompt):
    """把截图交给 OpenClaw 多模态，返回文本响应。"""
    with open(png, "rb") as f:
        img = base64.b64encode(f.read()).decode()
    body = {"model": "openclaw/default", "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}}]}]}
    req = urllib.request.Request(
        OPENCLAW_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                 "x-openclaw-model": VISION_MODEL}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def extract_articles(png):
    """多模态提取信息流里的文章，返回 [{account,title,time}, ...]。"""
    prompt = ("这是微信订阅号消息信息流截图。请提取其中每篇文章，按JSON数组输出，"
              "每项含 account(公众号名)、title(文章标题)、time(时间，如'18分钟前')。"
              "只输出JSON数组，不要代码块标记，不要多余文字。")
    out = _vision(png, prompt)
    m = re.search(r"\[.*\]", out, re.S)          # 容错：剥离 ```json 等包裹
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        return [a for a in arr if isinstance(a, dict) and a.get("title")]
    except Exception:
        return []


def _close_article_windows():
    """关闭内置文章浏览器窗口(窗口名 'WeChat'，区别于主窗口 'Weixin')。

    点开公众号文章会弹出独立浏览器窗口并「记住阅读位置」，不关掉的话再点
    订阅号入口会停在文章详情而非信息流列表。
    """
    try:
        out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "^WeChat$"],
                             capture_output=True, text=True).stdout.split()
        for wid in out:
            subprocess.run(["xdotool", "windowclose", wid])
        if out:
            time.sleep(1.5)
    except Exception:
        pass


def open_official_accounts():
    """打开订阅号信息流列表。返回 (是否成功, 滚动轮数)。

    注意：OA 信息流无 AT-SPI 节点，不能用 list-item 判据，只能点击+等待。
    先关掉残留的文章浏览器窗口，确保落在信息流列表而非某篇文章详情。
    """
    _close_article_windows()
    pos, scrolled = find_group_scroll("Official Accounts")
    if not pos:
        if scrolled:
            _scroll_list("up", scrolled * 4 + 4)
        return False, scrolled
    subprocess.run(["xdotool", "mousemove", str(pos[0]), str(pos[1]), "click", "1"])
    time.sleep(2.5)
    # 滚到信息流顶部，确保截到最新几篇
    subprocess.run(["xdotool", "mousemove", "700", "400"])
    for _ in range(6):
        subprocess.run(["xdotool", "click", "4"]); time.sleep(0.1)
    time.sleep(0.8)
    return True, scrolled


def resolve_official_accounts():
    """打开订阅号信息流，多模态提取文章列表。

    返回 (articles, scrolled)。articles=[{account,title,time}]；失败返回 ([], scrolled)。
    调用方负责用完 _scroll_list('up', ...) 复位并 Escape。
    """
    ok, scrolled = open_official_accounts()
    if not ok:
        return [], scrolled
    png = _feed_screenshot()
    if not png:
        return [], scrolled
    try:
        articles = extract_articles(png)
    finally:
        try:
            os.remove(png)
        except Exception:
            pass
    return articles, scrolled


if __name__ == "__main__":
    # 手测：打开订阅号信息流并提取文章列表
    arts, scr = resolve_official_accounts()
    print(f"提取到 {len(arts)} 篇 (滚动 {scr}):")
    for a in arts:
        print(f"  [{a.get('account')}] {a.get('title')}  ({a.get('time')})")
    subprocess.run(["xdotool", "key", "Escape"])
    if scr:
        _scroll_list("up", scr * 4 + 4)
