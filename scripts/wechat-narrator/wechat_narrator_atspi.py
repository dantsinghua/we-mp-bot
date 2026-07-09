#!/usr/bin/env python3
"""微信 Linux 讲述人（AT-SPI 版）—— 真·屏幕阅读器路线。

微信 Linux 不发桌面通知，但（用 QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 启动后）
会通过 AT-SPI 暴露整棵界面可访问树。本脚本轮询微信会话列表，检测哪些会话
出现了新消息（未读数出现/预览文本变化），把「联系人：消息」用 espeak-ng 朗读。

依赖：python3-pyatspi、espeak-ng、微信以 QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 启动、
at-spi-bus-launcher 运行。

用法：
  DISPLAY=:99 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
    python3 wechat_narrator_atspi.py [--interval 2] [--wav-dir DIR]
"""
import argparse
import os
import re
import subprocess
import time

import pyatspi

UNREAD_RE = re.compile(r"(\d+)\s+unread message")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def read_conversations():
    """返回会话列表：[(raw_text, unread_count), ...]，只取当前可见的 list item。"""
    d = pyatspi.Registry.getDesktop(0)
    convs = []

    def walk(n):
        try:
            if (n.getRoleName() == "list item" and n.name
                    and n.getState().contains(pyatspi.STATE_SHOWING)):
                raw = n.name.strip()
                m = UNREAD_RE.search(raw)
                unread = int(m.group(1)) if m else 0
                convs.append((raw, unread))
            for c in n:
                if c:
                    walk(c)
        except Exception:
            pass

    for i in range(d.childCount):
        a = d.getChildAtIndex(i)
        if "wechat" in (a.name or "").lower():
            walk(a)
    return convs


MARK_RE = re.compile(r"(Stuck on Top|\d+\s+unread message\(s\))")


def parse(raw):
    """从 list item 文本里切出联系人和消息预览。

    格式：'<联系人> [Stuck on Top] [N unread message(s)] <消息> <时间>'
    例：'File Transfer Stuck on Top 1 unread message(s) 我也不知道… 17:47'
        '灰灰 Stuck on Top [Link] 柔光洗手台，充满居家治愈感 17:10'
    关键：'Stuck on Top' / 'N unread message(s)' 标记正好是联系人名的右边界，
    联系人可能含空格（如 File Transfer），所以用标记而非空格来切。
    """
    s = re.sub(r"\s+\d{1,2}:\d{2}\s*$", "", raw).strip()  # 去尾部时间
    m = MARK_RE.search(s)
    if m:
        who = s[:m.start()].strip()
        msg = MARK_RE.sub("", s[m.start():]).strip()  # 去掉所有标记，剩下消息
    else:
        parts = s.split(" ", 1)
        who = parts[0]
        msg = parts[1] if len(parts) > 1 else ""
    # File Transfer 是系统会话，用中文名更自然
    if who == "File Transfer":
        who = "文件传输助手"
    return who, msg


# 公众号/服务号聚合会话：默认不朗读（噪音大且非真人）
SKIP_CONTACTS = {"Official Accounts", "Service Accounts", ""}


class Narrator:
    def __init__(self, voice="cmn", rate=175, wav_dir=None, skip_official=True):
        self.voice = voice
        self.rate = rate
        self.wav_dir = wav_dir
        self.skip_official = skip_official
        self.msg_state = {}      # who -> last message preview
        self.unread_state = {}   # who -> last unread count
        self.count = 0
        if wav_dir:
            os.makedirs(wav_dir, exist_ok=True)

    def speak(self, text):
        self.count += 1
        cmd = ["espeak-ng", "-v", self.voice, "-s", str(self.rate)]
        if self.wav_dir:
            wav = os.path.join(self.wav_dir, f"atspi_{self.count}.wav")
            cmd += ["-w", wav]
        cmd.append(text)
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"espeak 失败: {e}")

    def tick(self, baseline):
        for raw, unread in read_conversations():
            who, msg = parse(raw)
            prev_u = self.unread_state.get(who)
            prev_m = self.msg_state.get(who)
            self.unread_state[who] = unread
            self.msg_state[who] = msg
            if baseline:
                continue  # 基线期：只记录状态，不朗读（吸收登录后的历史同步）
            if self.skip_official and who in SKIP_CONTACTS:
                continue
            # 真·新消息判定：未读数「增加」了（别人发来才 +1），且预览内容变化。
            # 用未读增量而非单纯预览变化，能滤掉历史同步刷新和「[N条]」聚合抖动。
            if (prev_u is not None and unread > prev_u
                    and msg and msg != prev_m):
                spoken = f"{who}发来消息：{msg}"
                log(f"新消息 → {spoken}")
                self.speak(spoken)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--voice", default="cmn")
    ap.add_argument("--rate", type=int, default=175)
    ap.add_argument("--wav-dir", default=None)
    ap.add_argument("--baseline-seconds", type=float, default=20.0,
                    help="启动后多少秒内只建基线不朗读（吸收登录历史同步）")
    ap.add_argument("--include-official", action="store_true",
                    help="也朗读公众号/服务号（默认跳过）")
    args = ap.parse_args()

    n = Narrator(voice=args.voice, rate=args.rate, wav_dir=args.wav_dir,
                 skip_official=not args.include_official)
    scope = "含公众号" if args.include_official else "仅真人/群"
    log(f"微信讲述人（AT-SPI 版）已启动（{scope}），前 {args.baseline_seconds:.0f}s 建立基线…")
    baseline_rounds = max(1, int(args.baseline_seconds / max(args.interval, 0.5)))
    rounds = 0
    while True:
        try:
            n.tick(baseline=rounds < baseline_rounds)
            if rounds == baseline_rounds:
                log("基线建立完成，开始朗读新消息。")
            rounds += 1
        except Exception as e:
            log(f"tick 异常: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
