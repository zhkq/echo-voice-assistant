#!/usr/bin/env bash
# setup_mac.sh — ECHO macOS 环境安装（初始化一次即可）
set -e

# 3.0 安装根布局：代码目录名叫 echo-core 时，安装根是它的父目录 ——
# venv/ 与 data/ 都放安装根（否则"升级整体覆盖 echo-core"会把运行时和数据一起删掉）。
# 判据与 app/paths.py:echo_base() 一致；扁平安装里 CODE == BASE，行为不变。
CODE="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$CODE"
if [ "$(basename "$CODE")" = "echo-core" ]; then BASE="$(cd "$CODE/.." && pwd)"; fi
cd "$CODE"

echo "==> 检查 Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  echo "未检测到 Homebrew，请先安装： https://brew.sh"
  exit 1
fi

echo "==> 安装 Python 3.11 与 PortAudio"
brew list python@3.11 >/dev/null 2>&1 || brew install python@3.11
brew list portaudio   >/dev/null 2>&1 || brew install portaudio

PY="$(brew --prefix python@3.11)/bin/python3.11"
if [ ! -x "$PY" ]; then
  echo "找不到 Python 3.11：$PY"
  exit 1
fi

echo "==> 创建虚拟环境 venv/"
"$PY" -m venv "$BASE/venv"
"$BASE/venv/bin/python" -m pip install --upgrade pip

echo "==> 安装依赖（首次较慢，请耐心等待）"
"$BASE/venv/bin/python" -m pip install -r mac/requirements-mac.txt

mkdir -p "$BASE/data/logs"

if xcrun --find swiftc >/dev/null 2>&1; then
  bash mac/build_sidebar.sh || echo "浮动框构建失败，可继续使用浏览器面板。"
else
  echo "可选：安装 Apple Command Line Tools 后运行 bash mac/build_sidebar.sh，启用原生浮动框。"
fi

echo ""
echo "✅ 安装完成！"
echo "   启动：  mac/start_mac.sh"
echo "   面板：  http://127.0.0.1:8970"
