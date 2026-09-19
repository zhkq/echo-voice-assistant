#!/usr/bin/env bash
# start_mac.sh — 后台启动 ECHO（macOS）
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

if [ -z "${ECHO_DATA:-}" ] && [ -f "$DIR/data/echo.db" ]; then
  export ECHO_DATA="$DIR/data"
fi

if [ ! -x ./venv/bin/python ]; then
  echo "尚未初始化环境，请先运行： mac/setup_mac.sh"
  exit 1
fi

mkdir -p data/logs

PID_FILE="data/echo-mac.pid"
is_echo_pid() {
  case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac
  CMD="$(ps -p "$1" -o command= 2>/dev/null || true)"
  case "$CMD" in *"mac/run_mac.py"*) return 0 ;; *) return 1 ;; esac
}

if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if kill -0 "$OLD_PID" 2>/dev/null && is_echo_pid "$OLD_PID"; then
    echo "ECHO 已在运行（pid $OLD_PID）"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

nohup ./venv/bin/python mac/run_mac.py >> data/logs/echo-mac.out 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 0.5
if ! kill -0 "$NEW_PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "ECHO 启动失败，请查看 data/logs/echo-mac.out"
  exit 1
fi
PORT="$(./venv/bin/python -c "from app.config import settings; print(int(settings.get('serverPort', 8970)))" 2>/dev/null || echo 8970)"
echo "ECHO 已启动（pid $NEW_PID）"
echo "面板： http://127.0.0.1:$PORT"
