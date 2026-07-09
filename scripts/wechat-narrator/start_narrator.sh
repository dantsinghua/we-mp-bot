#!/usr/bin/env bash
# 微信 Linux 讲述人（AT-SPI 版）一键启动 —— 幂等，已在跑的组件会跳过。
# 无头服务器上：Xvfb 虚拟屏 + openbox 窗管 + at-spi 总线 + 微信(启用无障碍) + 讲述人轮询朗读。
set -u

export DISPLAY=:99
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
DIR="$(cd "$(dirname "$0")" && pwd)"
PY=/usr/bin/python3
LOG_DIR="${NARRATOR_LOG_DIR:-$HOME/.wechat-narrator}"
mkdir -p "$LOG_DIR"

start() { # name  pgrep-pattern  command...
  local name="$1" pat="$2"; shift 2
  if pgrep -f "$pat" >/dev/null 2>&1; then
    echo "  [skip] $name 已在运行"
  else
    setsid "$@" >"$LOG_DIR/$name.log" 2>&1 </dev/null &
    sleep "${DELAY:-2}"
    echo "  [ok]   $name 已启动"
  fi
}

echo "启动微信讲述人栈…"
start Xvfb        "Xvfb :99"               Xvfb :99 -screen 0 1280x800x24 -nolisten tcp
start openbox     "openbox"                openbox
start atspi       "at-spi-bus-launcher"    /usr/libexec/at-spi-bus-launcher --launch-immediately
DELAY=13 start wechat "/opt/wechat/wechat" \
  env QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 QT_ACCESSIBILITY=1 /usr/bin/wechat
start narrator    "wechat_narrator_atspi"  \
  "$PY" "$DIR/wechat_narrator_atspi.py" --interval 2 --wav-dir "$LOG_DIR/wav"

echo
echo "完成。日志在 $LOG_DIR/ ，朗读语音 wav 在 $LOG_DIR/wav/"
echo "首次或掉线后需登录：截图看二维码/登录按钮 —"
echo "  ffmpeg -y -f x11grab -video_size 292x396 -i :99.0+494,162 -frames:v 1 /tmp/wx.png"
echo "讲述人日志：tail -f $LOG_DIR/narrator.log"
