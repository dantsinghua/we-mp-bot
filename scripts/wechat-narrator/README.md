# WeChat Linux Narrator (讲述人)

Reads incoming WeChat messages aloud in Chinese on a **headless** Linux server.
Built after the Android/Assists and D-Bus-notification approaches proved unworkable
for WeChat (see "Why AT-SPI" below).

## How it works

WeChat Linux exposes its whole UI as an **AT-SPI accessibility tree** when launched
with `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`. The narrator polls the conversation list,
detects which conversation got a new message (preview text changed + has unread),
and speaks `"<contact>发来消息：<preview>"` via **espeak-ng** (Chinese `cmn` voice).

Stack (all on host, `DISPLAY=:99`, session bus `/run/user/1000/bus`):

| Component | Role |
|-----------|------|
| Xvfb :99 | virtual display (headless) |
| openbox | window manager (fixes rendering + lets WeChat lose focus) |
| at-spi-bus-launcher | AT-SPI accessibility bus |
| wechat (QT a11y env) | WeChat Linux 4.1.1.7, exposes accessible tree |
| `wechat_narrator_atspi.py` | polls conversation list → dedup → espeak-ng |

## Usage

```bash
./start_narrator.sh          # idempotent; starts whatever isn't running
tail -f ~/.wechat-narrator/narrator.log   # watch narration
```

**Login** (first run or after being kicked offline): WeChat shows a QR / quick-login.
Screenshot it (xwd gives garbage — WeChat renders on GPU; use ffmpeg):

```bash
ffmpeg -y -f x11grab -video_size 292x396 -i :99.0+494,162 -frames:v 1 /tmp/wx.png
```

Scan the QR with your phone. WeChat PC is **single-session** — logging in on another
computer kicks this one offline.

Narration is synthesized to `~/.wechat-narrator/wav/` (server has a PulseAudio
null-sink, no speaker — so you get wav files, not audible sound, unless run on a
machine with real audio).

## Files

- `wechat_narrator_atspi.py` — the narrator (AT-SPI polling). **This is the one in use.**
- `wechat_narrator.py` — old D-Bus-notification version. **Dead end** (WeChat Linux
  doesn't send desktop notifications), kept for reference.
- `start_narrator.sh` — one-shot launcher for the whole stack.

## Why AT-SPI (not notifications)

WeChat Linux 4.1.1.7 Notifications settings only have "New Message Alert Sound" and a
sidebar flag — **no desktop/system-notification option**. It never calls
`org.freedesktop.Notifications`, so listening for notifications yields nothing.
AT-SPI reads the UI directly instead — the standard Linux screen-reader mechanism.

## Notes / gotchas

- Contacts can contain spaces ("File Transfer"). Parse uses the `Stuck on Top` /
  `N unread message(s)` markers as the contact/message boundary, not spaces.
- Dedup compares the message-preview text, not the raw list-item string (which jitters
  with unread counts and timestamps).
- Don't `pkill -f <pat>` when `<pat>` also appears in the running shell command line —
  it kills the shell (exit 144). Split kill and start into separate invocations.
