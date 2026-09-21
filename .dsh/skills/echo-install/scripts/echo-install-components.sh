#!/usr/bin/env bash
# =====================================================================
# echo-install-components.sh — macOS：按需求把 ECHO 的引擎依赖 / 模型 / 设置装好
#
# 谁在用它：`.dsh/skills/echo-install`（安装专用技能）。技能先跟用户确认「要哪些组件」，
# 再把选择翻译成本脚本的参数。**不用 git、不用 GitHub**：依赖走 PyPI，模型走
# ModelScope / hf-mirror（ECHO 内置 HF_ENDPOINT=https://hf-mirror.com）。
#
# 前提：先跑过 `bash mac/setup_mac.sh`（它建 venv、装 mac/requirements-mac.txt）。
#       本脚本自己会检查，缺了就报错并告诉你跑哪一条。
#
# 用法：
#   bash echo-install-components.sh --dest "$HOME/ECHO" --engines sherpa,whisper-base --wake
#   bash echo-install-components.sh --dest "$HOME/ECHO" --engines sherpa --skip-pip
#
# 参数（与 Windows 版 echo-install-components.ps1 一一对应，便于技能两边共用一套说法）：
#   --dest <目录>          ECHO 安装目录（必填；里面要有 app/ 和 mac/）
#   --engines <列表>       转写引擎，逗号分隔。可选：
#                          sherpa(189MB,免 torch) / whisper-tiny(75) / whisper-base(141)
#                          / whisper-small(464) / whisper-medium(1500) / whisper-large-v3(2950)
#                          / sensevoice(896,需 torch) / qwen3asr(3.6GB)
#   --wake                 装唤醒词 KWS（40 MB，默认不装：要常开麦克风）
#   --diarize              装说话人分离（pyannote，需 HF 授权，重依赖）
#   --accel-cuda           mac 上无意义（没有 CUDA），会明确忽略并提示
#   --agent <名>           智能体后端：harness(默认,= DSH 标准版) / dsh / none
#   --models-dir / --meetings-dir / --notes-dir   三处位置（留空=默认；--notes-dir 填了会开归档）
#   --pip-index <url>      国内 pip 镜像，例如 https://pypi.tuna.tsinghua.edu.cn/simple
#   --skip-pip             只下模型 / 只写设置，不装 pip 依赖
#   --wait-seconds <秒>    等模型下载的上限（默认 1800）
#   --help                 看这段说明
#
# 为什么单独有一份 .sh 而不是复用 .ps1：mac 上没有 Windows 那套语义
#   （install.ps1 用 .NET/WebView2 边条、Windows 嵌入包 CPython、注册表快捷方式），
#   mac 的运行时是 mac/setup_mac.sh 建的 venv，浮动框是 Swift 现编（mac/sidebar）。
#
# 兼容性：**必须能在 macOS 自带的 bash 3.2 上跑** —— 所以这里不用关联数组
#   （declare -A 是 bash 4+）、不用 ${var,,}、不用 mapfile。引擎映射用 case 实现。
#   同事机器上的 /bin/bash 就是 3.2，用 4.x 特性会直接报错。
# =====================================================================
set -u

DEST=""
ENGINES="sherpa"
WAKE=0
DIARIZE=0
ACCEL_CUDA=0
AGENT="harness"
MODELS_DIR=""
MEETINGS_DIR=""
NOTES_DIR=""
PIP_INDEX=""
SKIP_PIP=0
WAIT_SECONDS=1800

# ---------------------------------------------------------------- 输出

if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_OK="$(tput setaf 2)"; C_WARN="$(tput setaf 3)"; C_ERR="$(tput setaf 1)"
  C_STEP="$(tput setaf 6)"; C_OFF="$(tput sgr0)"
else
  C_OK=""; C_WARN=""; C_ERR=""; C_STEP=""; C_OFF=""
fi
say()  { echo "  $*"; }
ok()   { echo "  ${C_OK}[ok]${C_OFF}   $*"; }
warn() { echo "  ${C_WARN}[warn]${C_OFF} $*"; }
err()  { echo "  ${C_ERR}[fail]${C_OFF} $*"; }
step() { echo ""; echo "${C_STEP}== $*${C_OFF}"; }

usage() {
  cat <<'USAGE'
  echo-install-components.sh — macOS：按需求把 ECHO 的引擎依赖 / 模型 / 设置装好

  用法：
    bash echo-install-components.sh --dest <ECHO 安装目录> [选项]

  必填：
    --dest <目录>        ECHO 安装目录（里面要有 app/ 和 mac/，且已跑过 mac/setup_mac.sh）

  选项：
    --engines <列表>     转写引擎，逗号分隔，默认 sherpa。可选：
                         sherpa / whisper-tiny / whisper-base / whisper-small /
                         whisper-medium / whisper-large-v3 / sensevoice / qwen3asr
    --wake               装唤醒词 KWS（40 MB）
    --diarize            装说话人分离（pyannote，需 HF 授权）
    --accel-cuda         mac 上无意义，会忽略并提示
    --agent <名>         harness（默认，标准版）/ dsh / none
    --models-dir <目录>  模型放哪（默认安装目录下）
    --meetings-dir <目录> 会议录音放哪（默认安装目录下）
    --notes-dir <目录>   笔记库（填了会打开纪要归档）
    --pip-index <url>    国内 pip 镜像
    --skip-pip           不装 pip 依赖
    --wait-seconds <秒>  等模型下载的上限，默认 1800
    --help               这段说明
USAGE
}

# ---------------------------------------------------------------- 参数

while [ $# -gt 0 ]; do
  case "$1" in
    --dest)           DEST="${2:-}"; shift 2 ;;
    --dest=*)         DEST="${1#*=}"; shift ;;
    --engines)        ENGINES="${2:-}"; shift 2 ;;
    --engines=*)      ENGINES="${1#*=}"; shift ;;
    --wake)           WAKE=1; shift ;;
    --diarize)        DIARIZE=1; shift ;;
    --accel-cuda)     ACCEL_CUDA=1; shift ;;
    --agent)          AGENT="${2:-}"; shift 2 ;;
    --agent=*)        AGENT="${1#*=}"; shift ;;
    --models-dir)     MODELS_DIR="${2:-}"; shift 2 ;;
    --models-dir=*)   MODELS_DIR="${1#*=}"; shift ;;
    --meetings-dir)   MEETINGS_DIR="${2:-}"; shift 2 ;;
    --meetings-dir=*) MEETINGS_DIR="${1#*=}"; shift ;;
    --notes-dir)      NOTES_DIR="${2:-}"; shift 2 ;;
    --notes-dir=*)    NOTES_DIR="${1#*=}"; shift ;;
    --pip-index)      PIP_INDEX="${2:-}"; shift 2 ;;
    --pip-index=*)    PIP_INDEX="${1#*=}"; shift ;;
    --skip-pip)       SKIP_PIP=1; shift ;;
    --wait-seconds)   WAIT_SECONDS="${2:-1800}"; shift 2 ;;
    --wait-seconds=*) WAIT_SECONDS="${1#*=}"; shift ;;
    -h|--help)        usage; exit 0 ;;
    *) err "不认识的参数：$1（用 --help 看用法）"; exit 2 ;;
  esac
done

if [ -z "$DEST" ]; then
  err "必须给 --dest <ECHO 安装目录>（例如 --dest \"$HOME/ECHO\"）"
  say "用法见： bash $0 --help"
  exit 2
fi
# 自己展开 ~：bash 不会替引号里的 ~ 展开，而同事很可能写成 --dest '~/ECHO'
case "$DEST" in
  '~')   DEST="$HOME" ;;
  '~/'*) DEST="$HOME/${DEST#\~/}" ;;
esac
if [ ! -d "$DEST" ]; then err "目录不存在：$DEST"; exit 2; fi
DEST="$(cd "$DEST" && pwd)"

case "$WAIT_SECONDS" in
  ''|*[!0-9]*) err "--wait-seconds 要是整数秒：$WAIT_SECONDS"; exit 2 ;;
esac

# ---------------------------------------------------------------- 引擎映射
# 依据：app/audio/stt.py 的 resolve_engine() + WHISPER_MODELS（sttModel 取值 =
#       sherpa|sensevoice|qwen3asr|whisper 档名），以及 app/components.py 的 model_id。
# 改这里之前先看 tests/test_install_entry.py —— 它会把这个表与 Windows 版、与 app 对齐核对。

engine_known() {
  case "$1" in
    sherpa|whisper-tiny|whisper-base|whisper-small|whisper-medium|whisper-large-v3|sensevoice|qwen3asr)
      return 0 ;;
    *) return 1 ;;
  esac
}

engine_pip() {
  case "$1" in
    sherpa)   echo "sherpa-onnx" ;;
    whisper-tiny|whisper-base|whisper-small|whisper-medium|whisper-large-v3)
              echo "faster-whisper huggingface-hub" ;;
    sensevoice) echo "funasr modelscope torch" ;;
    qwen3asr)   echo "transformers modelscope torch" ;;
    *) echo "" ;;
  esac
}

engine_model() {
  case "$1" in
    sherpa)     echo "sherpa" ;;
    whisper-*)  echo "$1" ;;
    sensevoice) echo "sensevoice" ;;
    qwen3asr)   echo "qwen3asr" ;;
    *) echo "" ;;
  esac
}

engine_stt() {
  case "$1" in
    sherpa)           echo "sherpa" ;;
    whisper-tiny)     echo "tiny" ;;
    whisper-base)     echo "base" ;;
    whisper-small)    echo "small" ;;
    whisper-medium)   echo "medium" ;;
    whisper-large-v3) echo "large-v3" ;;
    sensevoice)       echo "sensevoice" ;;
    qwen3asr)         echo "qwen3asr" ;;
    *) echo "" ;;
  esac
}

# 装完拿什么验证「真装上了」：import 这个模块名（有 python.exe / pip 说成功都不算数 ——
# Windows 版就栽在「依赖一个没装、却打印安装完成」上，2026-09-21）
engine_module() {
  case "$1" in
    sherpa)     echo "sherpa_onnx" ;;
    whisper-*)  echo "faster_whisper" ;;
    sensevoice) echo "funasr" ;;
    qwen3asr)   echo "transformers" ;;
    *) echo "" ;;
  esac
}

model_label() {
  case "$1" in
    kws) echo "唤醒词 KWS" ;;
    *)   echo "$1" ;;
  esac
}

# ---------------------------------------------------------------- 运行时 / 数据根 / 端口

find_python() {
  for rel in "venv/bin/python" "runtime-core/bin/python"; do
    if [ -x "$DEST/$rel" ]; then echo "$DEST/$rel"; return 0; fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  err "找不到运行时：$DEST/venv/bin/python"
  say "先跑环境安装（它会用 Homebrew 装 python@3.11 与 portaudio，再建 venv、装依赖）："
  say "    bash $DEST/mac/setup_mac.sh"
  exit 1
fi

# 数据根**不能**写死 $DEST/data：全新安装时 mac 上的数据根是平台默认
# （~/Library/Application Support/ECHO），端口文件也在那边 —— 问应用自己的路径层最稳
# （与 mac/start_mac.sh 同一套做法）。
data_root() {
  local d
  d="$(cd "$DEST" && "$PY" -c 'from app import paths; print(paths.data_root())' 2>/dev/null || true)"
  [ -n "$d" ] || d="$DEST/data"
  echo "$d"
}
DATA_DIR="$(data_root)"
PORT_FILE="$DATA_DIR/echo-port.txt"

echo_port() {
  if [ -n "${ECHO_PORT:-}" ]; then echo "$ECHO_PORT"; return; fi
  if [ -s "$PORT_FILE" ]; then
    local p
    p="$(cat "$PORT_FILE" 2>/dev/null | tr -d '[:space:]')"
    case "$p" in ''|*[!0-9]*) ;; *) echo "$p"; return ;; esac
  fi
  echo 8970
}

# ---------------------------------------------------------------- HTTP

api() {  # api <METHOD> <PATH> [JSON_BODY] [TIMEOUT]
  local method="$1" path="$2" body="${3:-}" timeout="${4:-30}"
  local port
  port="$(echo_port)"
  if [ -n "$body" ]; then
    curl -sS -m "$timeout" -X "$method" -H 'Content-Type: application/json' \
      -d "$body" "http://127.0.0.1:$port$path" 2>/dev/null
  else
    curl -sS -m "$timeout" -X "$method" "http://127.0.0.1:$port$path" 2>/dev/null
  fi
}

# 从 stdin 的 JSON 里取值。用 venv 的 python —— mac 上不一定有 jq，而 python 一定在。
json_query() {  # json_query <mode> [arg]
  "$PY" -c '
import json, sys
mode = sys.argv[1]
arg = sys.argv[2] if len(sys.argv) > 2 else ""
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if mode == "model_ready":
    for it in (d.get("items") or []):
        if it.get("id") == arg:
            print("1" if it.get("ready") else "0")
            sys.exit(0)
    print("0")
elif mode == "job_status":
    print(((d.get("jobs") or {}).get(arg) or {}).get("status") or "")
elif mode == "job_error":
    print(((d.get("jobs") or {}).get(arg) or {}).get("error") or "")
elif mode == "components":
    for c in (d.get("components") or []):
        print("  %-12s %s" % (c.get("name", ""), c.get("status", "")))
' "$1" "${2:-}" 2>/dev/null || true
}

api_up() { api GET /api/status "" 5 | grep -q '"components"'; }

ensure_service() {
  step "启动 ECHO 服务"
  if api_up; then ok "服务已在运行（端口 $(echo_port)）"; return 0; fi
  local starter="$DEST/mac/start_mac.sh"
  if [ ! -f "$starter" ]; then err "找不到启动脚本：$starter"; exit 1; fi
  say "服务没在跑，后台启动一次…"
  ( cd "$DEST" && nohup bash "$starter" >/dev/null 2>&1 & ) || true
  local i=0
  while [ "$i" -lt 60 ]; do
    sleep 2
    if api_up; then ok "服务已就绪（端口 $(echo_port)）"; return 0; fi
    i=$((i + 1))
  done
  err "启动后 120 秒仍未就绪 —— 看 $DATA_DIR/logs/ 下的日志"
  exit 1
}

# ---------------------------------------------------------------- pip 依赖

install_pip_deps() {
  local pkgs="$1"
  if [ "$SKIP_PIP" -eq 1 ]; then warn "已跳过 pip 依赖（--skip-pip）：$pkgs"; return 0; fi
  [ -n "$pkgs" ] || return 0
  step "安装依赖：$pkgs"
  local pip_args=""
  if [ -n "$PIP_INDEX" ]; then pip_args="-i $PIP_INDEX"; say "pip 源：$PIP_INDEX"; fi
  # 这里就是要按空格拆成多个包名 / 参数，所以不加引号
  # shellcheck disable=SC2086
  if "$PY" -m pip install --no-warn-script-location $pip_args $pkgs; then
    ok "pip 返回成功：$pkgs"
  else
    warn "pip 返回非零 —— 下面会用 import 逐个复核，装不上的会明确列出来"
  fi
}

# 依赖是不是真装上了：以 import 为准（python.exe 存在、pip 说成功都不算）
verify_engines() {
  [ "$SKIP_PIP" -eq 1 ] && return 0
  step "验收：这些引擎真的能用吗（逐个 import）"
  local bad=""
  for one in $ENGINE_LIST; do
    local mod
    mod="$(engine_module "$one")"
    [ -n "$mod" ] || continue
    if "$PY" -c "import $mod" >/dev/null 2>&1; then
      ok "$one（import $mod 通过）"
    else
      err "$one 还不能用：import $mod 失败"
      bad="$bad $one"
    fi
  done
  if [ "$WAKE" -eq 1 ]; then
    if "$PY" -c "import sherpa_onnx" >/dev/null 2>&1; then ok "唤醒词 KWS（import sherpa_onnx 通过）"
    else err "唤醒词 KWS 还不能用：import sherpa_onnx 失败"; bad="$bad kws"; fi
  fi
  if [ -n "$bad" ]; then
    err "这些还没装好：$bad"
    say "补救：加 --pip-index https://pypi.tuna.tsinghua.edu.cn/simple 重跑本脚本（装好的会跳过）"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- 模型

model_ready() { api GET /api/models "" 20 | json_query model_ready "$1"; }

wait_model() {
  local id="$1" waited=0
  while [ "$waited" -lt "$WAIT_SECONDS" ]; do
    local st
    st="$(api GET /api/models "" 20 | json_query job_status "$id")"
    if [ "$st" = "done" ]; then return 0; fi
    if [ "$st" = "error" ]; then
      warn "$id 下载失败：$(api GET /api/models "" 20 | json_query job_error "$id")"
      return 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
  warn "等 $id 超时（${WAIT_SECONDS}s）—— 下载在服务里继续跑，稍后在面板看进度"
  return 1
}

install_model() {
  local id="$1"
  if [ "$(model_ready "$id")" = "1" ]; then ok "$(model_label "$id") 已经装好，跳过"; return 0; fi
  step "下载模型：$(model_label "$id")"
  local r
  r="$(api POST /api/models/download "{\"id\": \"$id\"}" 30)"
  if ! printf '%s' "$r" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
    warn "触发下载失败：$r"
    return 1
  fi
  say "下载中（走 ModelScope / hf-mirror 镜像）…"
  wait_model "$id"
}

# ---------------------------------------------------------------- 设置

write_settings() {  # write_settings key=value [key=value ...]
  if [ $# -eq 0 ]; then say "没有要写的设置"; return 0; fi
  step "写入设置"
  local kv
  for kv in "$@"; do say "$kv"; done
  local payload
  payload="$("$PY" -c '
import json, sys
vals = {}
for p in sys.argv[1:]:
    k, _, v = p.partition("=")
    if v == "true":
        vals[k] = True
    elif v == "false":
        vals[k] = False
    else:
        vals[k] = v
print(json.dumps({"values": vals}))
' "$@")"
  local r
  r="$(api PUT /api/settings "$payload" 60)"
  if printf '%s' "$r" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
    ok "设置已写入"
  else
    err "写设置失败：$r"
    return 1
  fi
}

# ---------------------------------------------------------------- 主流程

echo ""
echo "  === ECHO 组件安装（macOS，按需下载）==="
say "安装目录：$DEST"
[ -d "$DEST/app" ] || { err "这个目录里没有 app/ —— 看起来不是 ECHO 安装目录"; exit 1; }
ok "运行时：$PY"
say "数据根：$DATA_DIR"

PIPS=""
MODEL_IDS=""
ENGINE_LIST=""
FIRST_STT=""
WHISPER_STT=""
OLD_IFS="$IFS"
IFS=','
for one in $ENGINES; do
  IFS="$OLD_IFS"
  one="$(printf '%s' "$one" | tr -d '[:space:]')"
  if [ -n "$one" ]; then
    if engine_known "$one"; then
      PIPS="$PIPS $(engine_pip "$one")"
      MODEL_IDS="$MODEL_IDS $(engine_model "$one")"
      ENGINE_LIST="$ENGINE_LIST $one"
      [ -n "$FIRST_STT" ] || FIRST_STT="$(engine_stt "$one")"
      case "$one" in
        whisper-*) [ -n "$WHISPER_STT" ] || WHISPER_STT="$(engine_stt "$one")" ;;
      esac
    else
      warn "不认识这个引擎，跳过：$one"
    fi
  fi
  IFS=','
done
IFS="$OLD_IFS"

if [ "$WAKE" -eq 1 ]; then
  PIPS="$PIPS sherpa-onnx"
  MODEL_IDS="$MODEL_IDS kws"
fi
if [ "$DIARIZE" -eq 1 ]; then
  PIPS="$PIPS pyannote.audio torch"
  warn "说话人分离（pyannote）是 HF 上的 gated 模型：ECHO 不能替你下载，要在 HF 同意条款后自己拉（面板 → 组件）"
fi
if [ "$ACCEL_CUDA" -eq 1 ]; then
  warn "mac 上没有 CUDA —— 忽略 --accel-cuda（Apple 芯片走 MPS，装普通版 torch 即可）"
fi

# shellcheck disable=SC2086
install_pip_deps "$PIPS"
# shellcheck disable=SC2086
ENGINE_LIST="${ENGINE_LIST# }"

pip_ok=0
verify_engines && pip_ok=1

ensure_service

for id in $MODEL_IDS; do
  install_model "$id" || true
done

SETTINGS=""
if [ -n "$FIRST_STT" ]; then
  SETTINGS="sttModel=$FIRST_STT"
  # 会议通常要更准：选里有 whisper 档就用它
  if [ -n "$WHISPER_STT" ]; then SETTINGS="$SETTINGS meetingSttModel=$WHISPER_STT"
  else SETTINGS="$SETTINGS meetingSttModel=$FIRST_STT"; fi
fi
if [ "$WAKE" -eq 1 ]; then SETTINGS="$SETTINGS wakeEnabled=true"; fi
if [ -n "$MODELS_DIR" ]; then SETTINGS="$SETTINGS modelsDir=$MODELS_DIR"; fi
if [ -n "$MEETINGS_DIR" ]; then SETTINGS="$SETTINGS meetingsDir=$MEETINGS_DIR"; fi
if [ -n "$NOTES_DIR" ]; then SETTINGS="$SETTINGS worklogVaultRoot=$NOTES_DIR worklogEnabled=true"; fi
case "$AGENT" in
  harness) SETTINGS="$SETTINGS agentBackend=harness agentHarnessEnabled=true" ;;
  dsh)     SETTINGS="$SETTINGS agentBackend=dsh" ;;
  none)    ;;
  *)       warn "不认识的 --agent：$AGENT（按 none 处理）" ;;
esac
# shellcheck disable=SC2086
write_settings $SETTINGS || true

step "自检"
api GET /api/status "" 20 | json_query components || warn "取 /api/status 失败"
for id in $MODEL_IDS; do
  if [ "$(model_ready "$id")" = "1" ]; then say "$(printf '%-14s' "$id") ✓ 就绪"
  else say "$(printf '%-14s' "$id") ✗ 还没好"; fi
done

echo ""
if [ "$pip_ok" -eq 1 ]; then
  echo "  完成。剩下的："
else
  echo "  装完了，但**有引擎不可用**（见上面的 [fail]）—— 别当成装好了。"
fi
say "1) 面板：浏览器打开 http://127.0.0.1:$(echo_port)/"
say "   mac 上热键要先在「系统设置 → 隐私与安全性 → 辅助功能 / 输入监控」里授权"
say "2) 会议纪要 / 归档 / 语音指令需要「智能体」：面板 → 设置 → 智能体（本脚本已按 --agent 选好）"
say "   harness 需要本机有 Node.js；没有就装 Node，或改用已装的 DSH 桌面版"
say "3) 首次运行 macOS 会提示「来自身份不明的开发者」（还没签名公证）"
say "   右键打开，或去「系统设置 → 隐私与安全性」里允许即可"

if [ "$pip_ok" -eq 0 ]; then exit 1; fi
exit 0
