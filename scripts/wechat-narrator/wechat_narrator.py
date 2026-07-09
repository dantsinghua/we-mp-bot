#!/usr/bin/env python3
"""微信 Linux 讲述人 —— 监听桌面通知，把微信新消息用 espeak-ng 朗读出来。

原理：注册成 D-Bus session bus 上的 org.freedesktop.Notifications 服务
（无头服务器没有 gnome-shell/dunst 之类的通知守护进程，这里我们自己当那个守护进程）。
微信 Linux 收到新消息时会通过该接口发通知 —— summary=发件人/标题，body=消息内容，
我们收到后提取文本，交给 espeak-ng 朗读。

用法：
  DISPLAY=:99 python3 wechat_narrator.py            # 只朗读微信通知
  python3 wechat_narrator.py --all                  # 朗读所有应用的通知（调试用）
  python3 wechat_narrator.py --wav-dir /path        # 无音频设备时，把每条朗读存成 wav
"""
import argparse
import os
import subprocess
import sys
import time

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

NOTIFY_IFACE = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class NarratorService(dbus.service.Object):
    def __init__(self, bus, only_wechat=True, voice="cmn", rate=175, wav_dir=None):
        super().__init__(bus, NOTIFY_PATH)
        self._next_id = 0
        self.only_wechat = only_wechat
        self.voice = voice
        self.rate = rate
        self.wav_dir = wav_dir
        if wav_dir:
            os.makedirs(wav_dir, exist_ok=True)

    # ---- org.freedesktop.Notifications 接口实现 ----
    @dbus.service.method(NOTIFY_IFACE, in_signature="susssasa{sv}i", out_signature="u")
    def Notify(self, app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout):
        self._next_id += 1
        nid = self._next_id
        if self._is_wechat(app_name, summary, hints) or not self.only_wechat:
            text = self._compose(summary, body)
            if text:
                log(f"通知 app={app_name!r} summary={summary!r} body={body!r} -> 朗读")
                self._speak(text, nid)
        return dbus.UInt32(nid)

    @dbus.service.method(NOTIFY_IFACE, in_signature="", out_signature="as")
    def GetCapabilities(self):
        return ["body", "body-markup", "actions"]

    @dbus.service.method(NOTIFY_IFACE, in_signature="", out_signature="ssss")
    def GetServerInformation(self):
        return ("wechat-narrator", "self", "1.0", "1.2")

    @dbus.service.method(NOTIFY_IFACE, in_signature="u", out_signature="")
    def CloseNotification(self, nid):
        pass

    @dbus.service.signal(NOTIFY_IFACE, signature="uu")
    def NotificationClosed(self, nid, reason):
        pass

    @dbus.service.signal(NOTIFY_IFACE, signature="us")
    def ActionInvoked(self, nid, action_key):
        pass

    # ---- 内部逻辑 ----
    def _is_wechat(self, app_name, summary, hints):
        blob = f"{app_name or ''} {summary or ''}".lower()
        if "wechat" in blob or "微信" in f"{app_name or ''}{summary or ''}":
            return True
        # 微信有时把应用名放在 hints 的 desktop-entry
        try:
            de = str(hints.get("desktop-entry", "")).lower()
            if "wechat" in de:
                return True
        except Exception:
            pass
        return False

    def _compose(self, summary, body):
        parts = [p.strip() for p in (summary, body) if p and p.strip()]
        return "，".join(parts)

    def _speak(self, text, nid):
        cmd = ["espeak-ng", "-v", self.voice, "-s", str(self.rate)]
        if self.wav_dir:
            wav = os.path.join(self.wav_dir, f"msg_{nid}.wav")
            cmd += ["-w", wav]
            log(f"  写入 {wav}")
        cmd.append(text)
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"  espeak-ng 调用失败: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="朗读所有通知（默认只读微信）")
    ap.add_argument("--voice", default="cmn", help="espeak-ng 语音（默认 cmn 中文）")
    ap.add_argument("--rate", type=int, default=175, help="语速")
    ap.add_argument("--wav-dir", default=None, help="无音频时把朗读写成 wav 到该目录")
    args = ap.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    # 抢占 notification 名字（若系统已有通知守护进程会失败，无头环境通常空闲）
    try:
        name = dbus.service.BusName(NOTIFY_IFACE, bus, do_not_queue=True, replace_existing=True)
    except dbus.exceptions.NameExistsException:
        log("错误：org.freedesktop.Notifications 已被占用（是否已有通知守护进程？）")
        sys.exit(1)

    NarratorService(bus, only_wechat=not args.all, voice=args.voice,
                    rate=args.rate, wav_dir=args.wav_dir)
    scope = "所有通知" if args.all else "仅微信"
    log(f"微信讲述人已启动（{scope}），语音={args.voice} 语速={args.rate}。监听桌面通知中…")
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        log("退出")


if __name__ == "__main__":
    main()
