#!/usr/bin/env bash
# stop_mac.sh — 停止 ECHO（macOS）
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

if [ -z "${ECHO_DATA:-}" ] && [ -f "$DIR/data/echo.db" ]; then
  export ECHO_DATA="$DIR/data"
fi

PID_FILE="data/echo-mac.pid"
# 端口优先取 echo-port.txt（ECHO 让位后的实际端口）；读不到才回退配置的首选端口。
PORT="$(cat data/echo-port.txt 2>/dev/null || true)"
if [ -z "$PORT" ]; then
  PORT="$(./venv/bin/python -c "from app.config import settings; print(int(settings.get('serverPort', 8970)))" 2>/dev/null || echo 8970)"
fi

is_echo_pid() {
  case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac
  CMD="$(ps -p "$1" -o command= 2>/dev/null || true)"
  case "$CMD" in *"mac/run_mac.py"*) return 0 ;; *) return 1 ;; esac
}

if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null && is_echo_pid "$PID"; then
    kill "$PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "$PID" 2>/dev/null || true
    echo "已停止 ECHO（pid $PID）"
  fi
  rm -f "$PID_FILE"
fi

# 兜底：清理占用该端口的残留进程
if command -v lsof >/dev/null 2>&1; then
  LEFT="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
  if [ -n "$LEFT" ]; then
    CLEANED=""
    for LEFT_PID in $LEFT; do
      if is_echo_pid "$LEFT_PID"; then
        kill "$LEFT_PID" 2>/dev/null || true
        CLEANED="$CLEANED $LEFT_PID"
      fi
    done
    if [ -n "$CLEANED" ]; then
      echo "已清理端口 $PORT 上的 ECHO 残留进程：$CLEANED"
    else
      echo "端口 $PORT 被其他程序占用，未做处理"
    fi
  fi
fi
