#!/usr/bin/env python3
"""微信群消息 → 邮件 实时转发。

锁定指定群（名称包含 --group 关键词），用 AT-SPI 轮询会话列表检测该群新消息，
每条新消息立即用 SMTP 发一封邮件（原样转发：主题=群名，正文=发言人+内容+时间）。

默认发到本机 GreenMail（测试用，无需真实 Gmail 密码）：
  python3 wechat_group_to_email.py --group 润珑苑 \
      --smtp-host 127.0.0.1 --smtp-port 3025 \
      --from wechat@local --to anlin0610182@gmail.com

换成真实 Gmail 转发时：--smtp-host smtp.gmail.com --smtp-port 587 --smtp-user <你的gmail> \
      --smtp-pass <应用专用密码> --tls
"""
import argparse
import os
import re
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formatdate

import pyatspi

try:
    from wechat_media_resolve import resolve_media
except Exception:
    resolve_media = None

try:
    from wechat_media_resolve import ensure_canonical
except Exception:
    ensure_canonical = None

try:
    import wechat_oa_resolve as oa
except Exception:
    oa = None

try:
    import wechat_auto_reply as auto_reply
except Exception:
    auto_reply = None

try:
    from wn_logging import get_logger
    _dlog = get_logger("group2email")
except Exception:
    _dlog = None

try:
    import messages_store  # 对话旁路落库（B1）；缺失或异常绝不影响转发主流程
except Exception:
    messages_store = None

# 会话预览里的标记，用作「群名」与「群内发言」的边界
MARK_RE = re.compile(
    r"(Stuck on Top|\d+\s+unread message\(s\)|\[You were mentioned\]|\[有人@我\])")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
    if _dlog is not None:
        try:
            _dlog.info(m)
        except Exception:
            pass


def read_conversations():
    d = pyatspi.Registry.getDesktop(0)
    out = []

    def walk(n):
        try:
            if (n.getRoleName() == "list item" and n.name
                    and n.getState().contains(pyatspi.STATE_SHOWING)):
                raw = n.name.strip()
                m = re.search(r"(\d+)\s+unread message", raw)
                unread = int(m.group(1)) if m else 0
                out.append((raw, unread))
            for c in n:
                if c:
                    walk(c)
        except Exception:
            pass

    for i in range(d.childCount):
        a = d.getChildAtIndex(i)
        if "wechat" in (a.name or "").lower():
            walk(a)
    return out


def parse(raw):
    """切出 (群名, 发言预览)。群消息形如：
    '润珑苑4A1205【瓷砖铺贴】 Stuck on Top [You were mentioned] 信发装饰(徐娟): xxx 13:31'
    """
    s = re.sub(r"\s+\d{1,2}:\d{2}\s*$", "", raw).strip()  # 去尾部时间
    m = MARK_RE.search(s)
    if m:
        who = s[:m.start()].strip()
        msg = MARK_RE.sub("", s[m.start():]).strip()
    else:
        parts = s.split(" ", 1)
        who = parts[0]
        msg = parts[1] if len(parts) > 1 else ""
    return who, msg


def send_mail(cfg, name, speaker_and_text, kind="群", attachments=None):
    when = time.strftime("%Y-%m-%d %H:%M:%S")
    body = f"{kind}：{name}\n时间：{when}\n\n{speaker_and_text}"
    attachments = [p for p in (attachments or []) if p and os.path.exists(p)]
    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for p in attachments:
            try:
                with open(p, "rb") as fp:
                    img = MIMEImage(fp.read())
                img.add_header("Content-Disposition", "attachment",
                               filename=os.path.basename(p))
                msg.attach(img)
            except Exception as e:
                log(f"附件失败 {p}: {e}")
    else:
        msg = MIMEText(body, "plain", "utf-8")
    # Outlook 分类头:告警类(kind=告警)标紧急,其余是正常转发内容
    if kind == "告警":
        msg["Subject"] = Header(f"[紧急][状态机] {name}", "utf-8")
        msg["X-WN-Category"] = "statemachine"
        msg["X-WN-Severity"] = "urgent"
    else:
        msg["Subject"] = Header(f"[微信{kind}] {name}", "utf-8")
        msg["X-WN-Category"] = "forward"
        msg["X-WN-Severity"] = "info"
    msg["X-WN-Source"] = "group2email"
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]
    msg["Date"] = formatdate(localtime=True)
    try:
        s = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
        if cfg["tls"]:
            s.starttls()
        if cfg["user"]:
            s.login(cfg["user"], cfg["passwd"])
        s.sendmail(cfg["from_addr"], [cfg["to_addr"]], msg.as_string())
        s.quit()
        return True
    except Exception as e:
        log(f"发邮件失败: {e}")
        return False


def send_oa_mail(cfg, articles):
    """订阅号新推送 → 一封邮件，列出每篇 公众号/标题/时间。"""
    when = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"{i+1}. [{a.get('account','')}] {a.get('title','')}  （{a.get('time','')}）"
             for i, a in enumerate(articles)]
    body = f"订阅号新推送 {len(articles)} 篇\n时间：{when}\n\n" + "\n".join(lines)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(f"[微信公众号] {len(articles)}篇新推送", "utf-8")
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]
    msg["Date"] = formatdate(localtime=True)
    try:
        s = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
        if cfg["tls"]:
            s.starttls()
        if cfg["user"]:
            s.login(cfg["user"], cfg["passwd"])
        s.sendmail(cfg["from_addr"], [cfg["to_addr"]], msg.as_string())
        s.quit()
        return True
    except Exception as e:
        log(f"发公众号邮件失败: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="",
                    help="群名关键词，逗号分隔可多个（每个做包含匹配）")
    ap.add_argument("--dm", default="",
                    help="私聊联系人关键词，逗号分隔（如 灰灰），主题标记为「私信」")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--baseline-seconds", type=float, default=15.0)
    ap.add_argument("--smtp-host", default="127.0.0.1")
    ap.add_argument("--smtp-port", type=int, default=3025)
    ap.add_argument("--smtp-user", default="")
    ap.add_argument("--smtp-pass", default="")
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--from", dest="from_addr", default="wechat-narrator@local")
    ap.add_argument("--to", dest="to_addr", required=True)
    ap.add_argument("--no-media", action="store_true",
                    help="不解析图片/语音（默认解析：图片→多模态描述，语音→转文字）")
    args = ap.parse_args()

    cfg = {"host": args.smtp_host, "port": args.smtp_port, "user": args.smtp_user,
           "passwd": args.smtp_pass, "tls": args.tls,
           "from_addr": args.from_addr, "to_addr": args.to_addr}

    group_kw = [k.strip() for k in args.group.split(",") if k.strip()]
    # DM 接收人优先读灵活配置 narrator_config.json 的 dm_peer（改人不用重启服务：改配置 + safe-pkill group2email 即可），缺省回落 --dm
    _cfg_dm_peer = None
    try:
        import json
        with open(os.path.join(os.path.expanduser("~"), ".wechat-narrator", "narrator_config.json")) as _f:
            _cfg_dm_peer = (json.load(_f) or {}).get("dm_peer") or None
    except (OSError, ValueError):
        _cfg_dm_peer = None
    dm_kw = [_cfg_dm_peer] if _cfg_dm_peer else [k.strip() for k in args.dm.split(",") if k.strip()]
    if _cfg_dm_peer:
        log(f"DM 接收人来自配置: {_cfg_dm_peer}")
    msg_state, unread_state = {}, {}
    reply_state = {}         # 私聊每个对象最近一次自动回复文本，避免把自己的回复当新消息
    cursor_state = {}        # 每个会话的游标锚块(最后K条消息序列)，洪峰补转/漏收补转用

    def collect_group_new(who, cursor):
        """洪峰路径:会话括号内按游标收新消息。返回 (msgs, truncated, new_cursor)；失败 (None, False, None)。"""
        if auto_reply is None:
            return None, False, None
        from wechat_media_resolve import chat_session, ChatOpenError
        try:
            with chat_session(who):          # 进入即通过标题校验,退出必归一化
                time.sleep(1.0)
                hist = auto_reply.read_history()
                if not hist:
                    return None, False, None
                new, trunc = auto_reply.diff_since_cursor(hist, cursor)
                return new, trunc, auto_reply.make_cursor(hist)
        except ChatOpenError:
            return None, False, None
    oa_seen = set()          # 已转发过的公众号文章标题，去重
    oa_last = 0.0            # 上次处理订阅号的时间戳，用于节流
    log(f"转邮件已启动：群{group_kw} 私信{dm_kw} → {args.smtp_host}:{args.smtp_port} → {args.to_addr}"
        f"（前 {args.baseline_seconds:.0f}s 建基线）")
    baseline_rounds = max(1, int(args.baseline_seconds / max(args.interval, 0.5)))
    def _alert(subject, body, shot=None):
        log(f"⚠ 告警: {subject}")
        try:
            send_mail(cfg, subject, body, "告警", [shot] if shot else None)
        except Exception as e:
            log(f"  告警邮件发送失败: {e}")

    rounds = 0
    while True:
        try:
            # S1/S2 规范态自愈+登录哨兵:置顶私聊行必须可见,否则滚顶;连续失败→告警
            if ensure_canonical is not None and rounds >= baseline_rounds:
                ensure_canonical(_alert)
            for raw, unread in read_conversations():
                who, text = parse(raw)
                # 公众号(订阅号)已由独立的 wemprss-bridge 图文全文桥接处理，这里不再转发(避免重复+旧列表格式)
                if "Official Accounts" in who or "Service Accounts" in who or not who:
                    continue
                is_group = any(k in who for k in group_kw)
                is_dm = any(k in who for k in dm_kw)
                if not is_group and not is_dm:
                    continue
                kind = "群" if is_group else "私信"
                prev_u = unread_state.get(who)
                prev_m = msg_state.get(who)
                unread_state[who] = unread
                msg_state[who] = text
                if rounds < baseline_rounds:
                    continue

                # 私信(灰灰)：预览文本一变就唤醒，打开会话按气泡颜色/头像判定最新一条谁发的。
                # 只有"最新是对方发的"才回复+转发；我自己发的(绿气泡/右头像)一律跳过——
                # 这样天然免疫"回复我自己发的"与"回复机器人上一条→自问自答死循环"，
                # 也不再依赖未读数(修掉"会话开着 unread=0 漏触发")。
                if is_dm:
                    if not (prev_m is not None and text and text != prev_m):
                        continue                         # 预览没变，无新动静
                    if text == reply_state.get(who):
                        continue                         # 这条预览就是我们刚发出的回复
                    if auto_reply is None:               # 无自动回复模块，退化为直接转发
                        send_mail(cfg, who, text, kind)
                        continue
                    log(f"{kind}预览变化[{who}] → {text}")
                    try:
                        r = auto_reply.do_auto_reply(who, cursor_state.get(who))
                    except Exception as e:
                        log(f"  自动回复异常: {e}")
                        send_mail(cfg, who, text, kind)      # 异常退化:至少转发预览
                        continue
                    sender = r["sender"]
                    if sender == "wrong_chat":
                        log(f"  ⚠ 会话校验失败(当前窗口≠{who})，已中止，仅转发预览")
                        send_mail(cfg, who, text, kind)
                        continue
                    if sender is None:
                        log("  打不开会话/读空，仅转发预览")
                        send_mail(cfg, who, text, kind)
                        continue
                    if r["cursor"]:
                        cursor_state[who] = r["cursor"]
                    peer_new = [m for m in r["new_msgs"] if m.get("sender") == "peer"]
                    if sender == "self" and not peer_new:
                        log("  最新是我自己发的且无漏收，跳过，不回复不转发")
                        continue
                    lines = [("[图片]" if m["kind"] == "image" else m["text"])
                             for m in peer_new]
                    body = "\n".join(lines) if lines else (r["latest"] or text)
                    tags = []
                    if len(lines) > 1:
                        tags.append(f"补转 {len(lines)} 条")
                    if r["truncated"]:
                        tags.append("洪峰超窗，可能不全或含重复")
                    if sender == "self":
                        tags.append("你已手动回复，未生成AI回复")
                        log(f"  最新是我自己发的；补转漏收 {len(lines)} 条")
                    if r["reply"]:
                        reply_state[who] = r["reply"]
                        log(f"  AI自动回复 → {r['reply']}")
                        body += f"\n\n【AI老公自动回复】{r['reply']}"
                    if tags:
                        body = f"【{'；'.join(tags)}】\n{body}"
                    send_mail(cfg, who, body, kind)
                    if messages_store is not None:      # 旁路落库(B1)：逐条 + AI 回复
                        try:
                            for m in r["new_msgs"]:
                                messages_store.tee(
                                    who, kind, m.get("sender") or "unknown",
                                    m.get("text") or "",
                                    msg_type=m.get("kind", "text"))
                            if r.get("reply"):
                                messages_store.tee(who, kind, "assistant", r["reply"])
                        except Exception:
                            pass
                    continue

                # 群：沿用"未读数上涨"判定(天然区分收到 vs 自己发)
                if (prev_u is not None and unread > prev_u
                        and text and text != prev_m):
                    delta = unread - prev_u
                    log(f"{kind}新消息[{who}] → {text}"
                        + (f"  (未读+{delta})" if delta > 1 else ""))
                    # 洪峰路径:一次涌入多条时只有最后一条在预览里,开群按游标补收全部
                    if delta > 1:
                        try:
                            msgs, trunc, cur = collect_group_new(who, cursor_state.get(who))
                        except Exception as e:
                            log(f"  洪峰补收异常: {e}")
                            msgs = None
                        if msgs:
                            if cur:
                                cursor_state[who] = cur
                            unread_state[who] = 0        # 开聊已读,未读清零(与增量判定自洽)
                            lines = [("[图片]" if m["kind"] == "image" else m["text"])
                                     for m in msgs]
                            log(f"  洪峰补转 {len(lines)} 条" + (" (超窗)" if trunc else ""))
                            head = (f"【洪峰补转 {len(lines)} 条，发言人未逐条标注"
                                    + ("；可能不全或含重复】" if trunc else "】"))
                            attachments = None
                            if resolve_media and not args.no_media \
                                    and ("[Photo]" in text or "[Audio]" in text):
                                try:      # 最新一条含媒体仍走既有解析(带附件)
                                    resolved, attachments = resolve_media(who, text)
                                    if resolved != text:
                                        log(f"  媒体解析 → {resolved}")
                                        lines.append(f"※ 最新一条解析: {resolved}")
                                except Exception as e:
                                    log(f"  媒体解析异常: {e}")
                            send_mail(cfg, who, head + "\n" + "\n".join(lines),
                                      kind, attachments)
                            if messages_store is not None:  # 旁路落库(B1)：洪峰逐条
                                try:
                                    for m in msgs:
                                        messages_store.tee(
                                            who, kind, m.get("sender") or "unknown",
                                            m.get("text") or "",
                                            msg_type=m.get("kind", "text"))
                                except Exception:
                                    pass
                            continue
                        log("  洪峰补收失败，退化为单条转发")
                    # 单条路径:图片/语音打开群定位媒体消息，多模态/转文字解析
                    attachments = None
                    if resolve_media and not args.no_media \
                            and ("[Photo]" in text or "[Audio]" in text):
                        try:
                            resolved, attachments = resolve_media(who, text)
                            if resolved != text:
                                log(f"  媒体解析 → {resolved}")
                                text = resolved
                            if attachments:
                                log(f"  附件 → {attachments}")
                        except Exception as e:
                            log(f"  媒体解析异常: {e}")
                    send_mail(cfg, who, text, kind, attachments)
                    if messages_store is not None:      # 旁路落库(B1)：群单条
                        try:
                            messages_store.tee(who, kind, "group", text,
                                               assets=attachments)
                        except Exception:
                            pass
            if rounds == baseline_rounds:
                log("基线建立完成，开始转发群消息。")
            rounds += 1
        except Exception as e:
            log(f"循环异常: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
