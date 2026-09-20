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
# 实际端口以 echo-port.txt 为准（首选端口被占/落在保留段时 ECHO 会让位）。
# 它由 run_mac.py 在 uvicorn 启动前写出，所以这里短暂轮询一下。
# 关键：端口文件在**实际数据根**下，不一定是仓库的 data/ —— 全新安装（仓库里没有
# data/echo.db、ECHO_DATA 未导出）时数据根是平台默认（mac 上
# ~/Library/Application Support/ECHO），写死 data/echo-port.txt 会读到空文件、
# 于是打印出首选端口而不是真实端口。这里问应用自己的路径层要数据根
# （与 mac_runtime.py 传给浮动框的 --data 同源）。
DATA_DIR="$(./venv/bin/python -c "from app import paths; print(paths.data_root())" 2>/dev/null || true)"
[ -n "$DATA_DIR" ] || DATA_DIR="$DIR/data"
PORT=""
for _ in 1 2 3 4 5 6; do
  if [ -s "$DATA_DIR/echo-port.txt" ]; then PORT="$(cat "$DATA_DIR/echo-port.txt" 2>/dev/null || true)"; break; fi
  sleep 0.5
done
if [ -z "$PORT" ]; then
  PORT="$(./venv/bin/python -c "from app.config import settings; print(int(settings.get('serverPort', 8970)))" 2>/dev/null || echo 8970)"
fi
echo "ECHO 已启动（pid $NEW_PID）"
echo "面板： http://127.0.0.1:$PORT"
