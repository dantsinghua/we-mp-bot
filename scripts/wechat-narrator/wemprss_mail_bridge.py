#!/usr/bin/env python3
"""we-mp-rss → 邮件桥接：公众号新文章，每篇单独一封「图文全文」邮件。

数据：we-mp-rss 官方 API + Access Key(不碰 SQLite、无锁)。
  列表 GET /articles?has_content=true  → 有全文的最新文章(元数据)
  单篇 GET /articles/{id}              → 完整全文 content(HTML)
  认证头 `Authorization: AK-SK <ak>:<sk>`。

内容处理(图文并茂 + 去广告)：
  1. HTML → 有序 blocks(文本段落 + 图片占位)
  2. 规则截断尾部(往期回顾/阅读原文/关注引导/赞助/免责声明…)
  3. 图片转 [IMG_n] 占位 → qwen3.7-plus 清洗(删广告/推广/引导，保留正文和占位)
  4. 下载图片(带微信 UA/referer 绕防盗链，≤25张，跳过小图) → 内嵌邮件(cid)
  5. HTML 邮件(MIMEMultipart/related)，图文并茂 + 原文链接
  LLM 失败降级到规则结果；图下载失败退回图片链接文字。

增量水位：seen 文件记已处理 id；publish_time>=基线(since)才推。
由 systemd 服务 wemprss-bridge.service 常驻(Restart=always)。
"""
import argparse
import html as _html
import io
import json
import os
import re
import smtplib
import time
import urllib.request
from html.parser import HTMLParser
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formatdate

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


# ---- we-mp-rss API ----
AK = _secret("WEMPRSS_AK")
SK = _secret("WEMPRSS_SK")
BASE = os.environ.get("WEMPRSS_API", "http://127.0.0.1:8001/api/v1/wx")
AUTH = f"AK-SK {AK}:{SK}"

# ---- 大模型(裸调阿里百炼，不经 OpenClaw) ----
LLM_BASE = os.environ.get("BAILIAN_BASE", "https://coding.dashscope.aliyuncs.com/v1")
LLM_KEY = _secret("BAILIAN_KEY")
LLM_MODEL = os.environ.get("BAILIAN_MODEL", "qwen3.7-plus")

# ---- 图片 ----
IMG_MAX = int(os.environ.get("WEMPRSS_IMG_MAX", "25"))
IMG_MIN_BYTES = 3000        # 小于此字节视作图标/装饰，跳过
IMG_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/109.0 Safari/537.36 MicroMessenger/7.0.20")

# 尾部无意义内容标志词(命中且在文章后半段则从此截断)
TAIL_MARKERS = [
    "往期回顾", "往期精彩", "往期文章", "往期推荐", "推荐阅读", "热文推荐", "更多精彩",
    "点击阅读原文", "阅读原文", "长按关注", "长按识别", "长按二维码", "扫码关注",
    "扫描二维码", "扫描下方二维码", "点亮在看", "点个在看", "点个赞", "分享收藏",
    "设为星标", "星标我们", "赞助单位", "赞助商", "免责声明", "风险提示", "版权声明",
    "转载须知", "商务合作", "投稿邮箱", "关注我们", "点击上方", "关注公众号",
]

STATE_DIR = os.path.expanduser("~/.wechat-narrator")
SEEN_FILE = os.path.join(STATE_DIR, "wemprss_seen.txt")
SINCE_FILE = os.path.join(STATE_DIR, "wemprss_since.txt")
os.makedirs(STATE_DIR, exist_ok=True)
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连(阿里/微信CDN国内)


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(l.strip() for l in f if l.strip())
    return set()


def add_seen(ids):
    with open(SEEN_FILE, "a") as f:
        for i in ids:
            f.write(i + "\n")


def load_since():
    try:
        with open(SINCE_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def save_since(ts):
    with open(SINCE_FILE, "w") as f:
        f.write(str(ts))


# ---- we-mp-rss API ----
def _api(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def list_articles(limit=50):
    d = _api(f"/articles?has_content=true&limit={limit}")
    return (d.get("data") or {}).get("list") or []


def fulltext_html(article_id):
    a = (_api(f"/articles/{article_id}") or {}).get("data") or {}
    return a.get("content") or a.get("content_html") or ""


# ---- 大模型裸调 ----
def _llm(prompt, timeout=120):
    body = {"model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "enable_thinking": False}       # qwen3.7-plus 默认开推理链(慢7倍)，关闭大幅提速
    req = urllib.request.Request(LLM_BASE + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {LLM_KEY}"},
                                 method="POST")
    with _NOPROXY.open(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


# ---- HTML → blocks(文本 + 图片) ----
class _Extractor(HTMLParser):
    # 段落级标签开始/结束都切段；_flush 规范化空白；碎片由 merge_fragments 合并
    _BLOCK = {"p", "br", "section", "div", "li", "h1", "h2", "h3", "h4", "blockquote", "tr"}

    def __init__(self):
        super().__init__()
        self.blocks = []      # [("text", str) | ("img", url)]
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            d = dict(attrs)
            src = d.get("data-src") or d.get("src") or ""   # 懒加载真实URL在 data-src
            if "qpic.cn" in src:                             # 只认真实图，排除 res.wx.qq.com 占位gif
                self._flush()
                self.blocks.append(("img", src if src.startswith("http") else "https:" + src))
        elif tag in self._BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self._BLOCK:
            self._flush()

    def handle_data(self, data):
        if data.strip():
            self._buf.append(data)

    def _flush(self):
        if self._buf:
            t = _html.unescape("".join(self._buf))
            t = re.sub(r"\s+", " ", t).strip()      # 规范化内部空白(去HTML缩进/换行)
            if t:
                self.blocks.append(("text", t))
            self._buf = []


# 视频号/播放器/互动 UI 文字(被误当正文)，短词行直接过滤
NOISE_WORDS = {
    "重播", "分享", "赞", "在看", "点赞", "收藏", "关闭", "退出全屏", "切换到竖屏全屏",
    "切换到横屏全屏", "观看更多", "更多", "已关注", "关注", "分享视频", "写留言", "留言",
    "视频", "继续观看", "正在直播", "进入直播间", "点击观看", "长按识别", "识别二维码",
    "阅读原文", "点击上方", "喜欢此内容的人还喜欢", "预览时标签不可点",
}


def _is_noise(t):
    s = t.strip()
    return s in NOISE_WORDS or (len(s) <= 4 and s in NOISE_WORDS)


def merge_fragments(blocks):
    """合并被拆碎的文本：标点开头 / 上段过短 / 上段无句末标点 → 并入上一段。"""
    out = []
    for t, v in blocks:
        if t == "text":
            v = v.strip()
            if not v:
                continue
            if out and out[-1][0] == "text":
                prev = out[-1][1]
                if v[0] in "，,、。；;：:）)】」》”" or len(prev) < 8 or prev[-1] not in "。！？!?.…":
                    out[-1] = ("text", prev + v)
                    continue
        out.append((t, v))
    return out


def html_to_blocks(html):
    p = _Extractor()
    try:
        p.feed(html)
    except Exception:
        pass
    p._flush()
    # 过滤 UI 噪声词 + 合并碎片
    blocks = [(t, v) for t, v in p.blocks if not (t == "text" and _is_noise(v))]
    return merge_fragments(blocks)


def strip_tail(blocks):
    """从文章后半段起，命中尾部标志词则截断其后(去掉往期/关注/赞助等)。"""
    n = len(blocks)
    start = n // 2
    for i in range(start, n):
        t, v = blocks[i]
        if t == "text" and len(v) < 60 and any(m in v for m in TAIL_MARKERS):
            return blocks[:i]
    return blocks


def blocks_to_text(blocks):
    """blocks → 带 [IMG_n] 占位的文本 + {n: url} 映射。"""
    lines, imgmap, n = [], {}, 0
    for t, v in blocks:
        if t == "text":
            lines.append(v)
        else:
            n += 1
            imgmap[n] = v
            lines.append(f"[IMG_{n}]")
    return "\n".join(lines), imgmap


def llm_clean(text):
    """qwen3.7-plus 清洗：删广告/推广/引导，保留正文与 [IMG_n] 占位。失败降级返回原文。"""
    prompt = ("下面是一篇微信公众号文章的正文（图片用 [IMG_数字] 占位符表示）。"
              "请删除其中与正文无关的内容：广告、推广、引导关注/点赞/在看/转发/分享、"
              "往期回顾、推荐阅读、二维码及其提示文字、赞助商、免责声明、版权声明、商务合作等。"
              "**务必原样保留所有 [IMG_数字] 占位符，位置不要改动、不要新增或删除占位符**。"
              "**保持合理的段落划分：标题、小标题、不同话题的段落之间用空行分隔，每段是完整通顺的一段，"
              "不要把标题和正文挤在一起。**"
              "只输出清洗后的正文本身，不要任何解释或前后缀：\n\n" + text)
    try:
        out = _llm(prompt)
        # 安全校验：若模型把占位符弄丢太多，退回原文
        if text.count("[IMG_") and out.count("[IMG_") < text.count("[IMG_") * 0.5:
            return text
        out = re.sub(r"(?<=[一-鿿])[ \t]+(?=[一-鿿])", "", out)  # 去中文间空格
        return out
    except Exception as e:
        log(f"  LLM清洗失败(降级用规则结果): {e}")
        return text


def download_img(url):
    """带微信 UA/referer 下载图片，绕防盗链；统一转 JPEG(webp/png/gif 很多邮件客户端不显示)。

    返回 JPEG bytes；过小(图标)或失败返回 None。
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": IMG_UA, "Referer": "https://mp.weixin.qq.com/"})
        with _NOPROXY.open(req, timeout=20) as r:
            data = r.read()
        if len(data) < IMG_MIN_BYTES:
            return None
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "LA", "P"):        # 透明→白底，避免转jpeg变黑
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return None


def build_and_send(cfg, art, cleaned_text, imgmap):
    """把清洗后的文本(含[IMG_n])渲染为 HTML 邮件：图片下载内嵌(cid)，发送。"""
    ts = art.get("publish_time") or 0
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
    mp, title, url = art.get("mp_name", ""), art.get("title", ""), art.get("url", "")

    parts, images, used = [], [], 0
    for line in cleaned_text.split("\n"):
        s = line.strip()
        if not s:
            continue
        m = re.fullmatch(r"\[IMG_(\d+)\]", s)
        if m:
            iurl = imgmap.get(int(m.group(1)))
            if not iurl:
                continue
            data = download_img(iurl) if used < IMG_MAX else None
            if data:
                cid = f"img{len(images)}"
                images.append((cid, data))
                used += 1
                parts.append(f'<p style="text-align:center"><img src="cid:{cid}" '
                             f'style="max-width:100%;height:auto"></p>')
            else:
                parts.append(f'<p style="color:#888;font-size:13px">[图片] '
                             f'<a href="{_html.escape(iurl)}">查看</a></p>')
        else:
            parts.append(f"<p>{_html.escape(s)}</p>")

    head = (f'<h2 style="margin:0 0 4px">{_html.escape(title)}</h2>'
            f'<p style="color:#888;font-size:13px;margin:0 0 12px">公众号：{_html.escape(mp)}'
            f'　{when}</p><hr style="border:none;border-top:1px solid #eee">')
    tail = (f'<hr style="border:none;border-top:1px solid #eee">'
            f'<p style="font-size:13px">原文链接：<a href="{_html.escape(url)}">{_html.escape(url)}</a></p>')
    html_body = (f'<div style="max-width:680px;margin:0 auto;font-size:15px;'
                 f'line-height:1.8;color:#222">{head}{"".join(parts)}{tail}</div>')

    msg = MIMEMultipart("related")
    msg["Subject"] = Header(f"[微信公众号] {mp}｜{title}", "utf-8")
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]
    msg["Date"] = formatdate(localtime=True)
    msg["X-WN-Category"] = "oa-article"   # Outlook 分类:公众号全文,与告警区分
    msg["X-WN-Severity"] = "info"
    msg["X-WN-Source"] = "wemprss-bridge"
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)
    for cid, data in images:
        img = MIMEImage(data, "jpeg")          # 已统一转 JPEG
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline")
        msg.attach(img)

    s = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
    if cfg["tls"]:
        s.starttls()
    if cfg["user"]:
        s.login(cfg["user"], cfg["passwd"])
    s.sendmail(cfg["from_addr"], [cfg["to_addr"]], msg.as_string())
    s.quit()
    return used


def process_and_send(cfg, art):
    """完整管线：取全文HTML→blocks→截尾→LLM清洗→图文HTML邮件。返回内嵌图数。"""
    html = fulltext_html(art["id"])
    if not html:
        return 0
    blocks = strip_tail(html_to_blocks(html))
    # 图集帖(image_content/show_type=8)：轮播图不在 content，只有封面 → 内嵌 pic_url 保底
    if not any(t == "img" for t, _ in blocks):
        pic = art.get("pic_url")
        if pic and ("js_image_content" in html or art.get("item_show_type") == 8):
            blocks = [("img", pic)] + blocks
    text, imgmap = blocks_to_text(blocks)
    cleaned = llm_clean(text)
    return build_and_send(cfg, art, cleaned, imgmap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smtp-host", default="127.0.0.1")
    ap.add_argument("--smtp-port", type=int, default=3025)
    ap.add_argument("--smtp-user", default="")
    ap.add_argument("--smtp-pass", default="")
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--from", dest="from_addr", default="wechat-narrator@test.local")
    ap.add_argument("--to", dest="to_addr", default="wechat-group@test.local")
    ap.add_argument("--interval", type=float, default=90)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--init-mark", action="store_true")
    args = ap.parse_args()
    cfg = {"host": args.smtp_host, "port": args.smtp_port, "user": args.smtp_user,
           "passwd": args.smtp_pass, "tls": args.tls,
           "from_addr": args.from_addr, "to_addr": args.to_addr}

    if args.init_mark:
        seen = load_seen()
        fresh = [a["id"] for a in list_articles(100) if a.get("id") and a["id"] not in seen]
        add_seen(fresh)
        save_since(time.time())
        log(f"首次标记：{len(fresh)} 篇现有文章设为已处理；基线已记录。之后只推新发布文章。")
        return

    log(f"we-mp-rss 图文桥接启动(API+AK, {LLM_MODEL}清洗, 图片内嵌)：→ {args.to_addr}"
        f"（每 {args.interval:.0f}s 检查）")
    last_hb = 0.0
    while True:
        try:
            seen = load_seen()
            since = load_since()
            for a in list_articles(args.limit):
                aid = a.get("id")
                if not aid or aid in seen or (a.get("publish_time") or 0) < since:
                    continue
                try:
                    nimg = process_and_send(cfg, a)
                    add_seen([aid])
                    seen.add(aid)
                    log(f"发送：[{a.get('mp_name')}] {a.get('title', '')[:24]} (内嵌{nimg}图)")
                except Exception as e:
                    log(f"发送失败 {aid}: {e}")
        except Exception as e:
            log(f"循环异常: {e}")
        if time.time() - last_hb >= 3600:    # 每小时一条心跳,供看门狗判活(空闲时也有日志)
            last_hb = time.time()
            log("心跳")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
