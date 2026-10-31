#!/usr/bin/env bash
# =====================================================================
# echo-install-components.sh — macOS：按需求把 ECHO 的引擎依赖 / 模型 / 设置装好
#
# 谁在用它：`.dsh/skills/echo-install`（安装专用技能）。技能先跟用户确认「要哪些组件」，
# 再把选择翻译成本脚本的参数。**不用 git、不用 GitHub**：依赖走 PyPI，模型走
# 客户端不再下载本地 STT 模型（2026-10-31：转写全部走能力后端）。
#
# 前提：先跑过 `bash mac/setup_mac.sh`（它建 venv、装 mac/requirements-mac.txt）。
#       本脚本自己会检查，缺了就报错并告诉你跑哪一条。
#
# 用法：
#   bash echo-install-components.sh --dest "$HOME/ECHO"
#   （2026-10-31 起客户端只装 runtime-core，不再有本地 STT 引擎/模型/唤醒）
#
# 参数（2026-10-31 起客户端不再装本地 STT 引擎/唤醒/模型）：
#   --dest <目录>          ECHO 安装目录（必填；里面要有 app/ 和 mac/）
#   --engines <列表>       **已废弃**：传了只警告、不生效
#   --wake / --diarize / --accel-cuda   **已废弃**：唤醒取消、分离与 torch 都在能力后端
#   --agent <名>           智能体后端：harness(默认,= DSH 标准版) / dsh / none
#   --models-dir / --meetings-dir / --notes-dir   三处位置（留空=默认）
#   --pip-index <url>      国内 pip 镜像（核心依赖由 setup_mac.sh 的 venv 提供）
#   --skip-pip             只写设置/智能体，不装 pip 依赖
#   --wait-seconds <秒>    历史兼容参数（客户端不再下载模型）
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
ENGINES=""
WAKE=0
DIARIZE=0
ACCEL_CUDA=0
AGENT="harness"
DSH_VERSION=""
PIN_HARNESS_COMMAND=0
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
# A6：每一步同时留一份**独立于终端**的日志（终端关掉、输出被 agent 收走时还能回看）。
# 落点 <安装目录>/data/logs/install-<时间戳>.log；写不进去（权限/只读）就静默放弃，绝不挡安装。
INSTALL_LOG=""
_tee() {
  if [ -n "$INSTALL_LOG" ]; then
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >> "$INSTALL_LOG" 2>/dev/null || true
  fi
  return 0
}
say()  { echo "  $*"; _tee "  $*"; }
ok()   { echo "  ${C_OK}[ok]${C_OFF}   $*"; _tee "[ok]   $*"; }
warn() { echo "  ${C_WARN}[warn]${C_OFF} $*"; _tee "[warn] $*"; }
err()  { echo "  ${C_ERR}[fail]${C_OFF} $*"; _tee "[fail] $*"; }
step() { echo ""; echo "${C_STEP}== $*${C_OFF}"; _tee "== $*"; }

usage() {
  cat <<'USAGE'
  echo-install-components.sh — macOS：按需求把 ECHO 的引擎依赖 / 模型 / 设置装好

  用法：
    bash echo-install-components.sh --dest <ECHO 安装目录> [选项]

  必填：
    --dest <目录>        ECHO 安装目录（里面要有 app/ 和 mac/，且已跑过 mac/setup_mac.sh）

  选项：
    --engines <列表>      已废弃：客户端不再装本地 STT 引擎（传了不生效）
    --wake                已废弃：语音唤醒取消
    --diarize             已废弃：说话人分离在能力后端做
    --accel-cuda          已废弃：客户端不跑本地 torch 模型
    --agent <名>         harness（默认，标准版）/ dsh / none
    --dsh-version <版本> 标准版本地安装的版本（默认 0.1.5-rc.2）。npm 装不下来时会
                         从 npx 缓存里找**同版本**那份复制
    --pin-harness-command 把本地入口的绝对路径写进设置 harnessCommand（默认**不写**：
                         ECHO 自己会优先本地入口，node 换版本不用改配置）
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
    --dsh-version)    DSH_VERSION="${2:-}"; shift 2 ;;
    --dsh-version=*)  DSH_VERSION="${1#*=}"; shift ;;
    --pin-harness-command) PIN_HARNESS_COMMAND=1; shift ;;
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
# --dest 是**安装根**。3.0 布局里代码在 <根>/echo-core，数据/模型/DSH/会议/指令是它的兄弟目录；
# 老式扁平安装里代码就在根下（判据与 app/paths.py:echo_base() 一致：代码目录名叫 echo-core）。
#   BASE  数据/日志/运行时都按它算（新老布局都是 --dest 本身）
#   CODE  代码目录（`from app import paths` 与启动脚本要在这里跑）
BASE="$DEST"
CODE="$DEST"
if [ -f "$DEST/echo-core/app/main.py" ]; then CODE="$DEST/echo-core"; fi
LOG_DIR="$BASE/data/logs"
if mkdir -p "$LOG_DIR" 2>/dev/null; then
  INSTALL_LOG="$LOG_DIR/install-$(date '+%Y%m%d-%H%M%S').log"
  printf '===== echo-install-components %s 安装目录=%s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$DEST" \
    >> "$INSTALL_LOG" 2>/dev/null || INSTALL_LOG=""
fi

case "$WAIT_SECONDS" in
  ''|*[!0-9]*) err "--wait-seconds 要是整数秒：$WAIT_SECONDS"; exit 2 ;;
esac

# 2026-10-31 用户拍板：客户端默认档不装任何本地 STT 模型/引擎，语音唤醒取消。
# 指令 / 回顾 / 会议转写统一走能力后端（echo-server + Qwen3-ASR）。
# 所以这里不再有 ENGINE_MAP / engine_* 映射函数（历史安装器才需要它们）。

# ---------------------------------------------------------------- 运行时 / 数据根 / 端口

find_python() {
  for rel in "venv/bin/python" "runtime-core/bin/python"; do
    if [ -x "$DEST/$rel" ]; then echo "$DEST/$rel"; return 0; fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  err "找不到运行时：$BASE/venv/bin/python（或 $BASE/runtime-core）"
  say "先跑环境安装（它会用 Homebrew 装 python@3.11 与 portaudio，再建 venv、装依赖）："
  say "    bash $CODE/mac/setup_mac.sh"
  exit 1
fi

# 数据根**不能**写死 $DEST/data：全新安装时 mac 上的数据根是平台默认
# （~/Library/Application Support/ECHO），端口文件也在那边 —— 问应用自己的路径层最稳
# （与 mac/start_mac.sh 同一套做法）。
data_root() {
  local d
  # 必须在**代码目录**里跑（`from app import paths`）：新布局下 app 在 <根>/echo-core，
  # 在安装根里跑只会 ImportError → 回落成 $BASE/data，而 mac 的数据根其实在
  # ~/Library/Application Support/ECHO（老布局）或 <根>/data（新布局）。
  d="$(cd "$CODE" && "$PY" -c 'from app import paths; print(paths.data_root())' 2>/dev/null || true)"
  [ -n "$d" ] || d="$BASE/data"
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
elif mode == "job_state":
    # 归一化：worker 失败时写的是 "failed"，判断 "error" 会让失败显示成"排队中"
    # （2026-09-21 同事实测：qwen3asr 下载失败，面板一直显示排队中）。
    st = str(((d.get("jobs") or {}).get(arg) or {}).get("status") or "").strip().lower()
    print({"failed": "failed", "error": "failed", "fail": "failed",
           "running": "running", "done": "done"}.get(st, "queued"))
elif mode == "job_error":
    j = (d.get("jobs") or {}).get(arg) or {}
    print(j.get("message") or j.get("error") or "")
elif mode == "components":
    # 未选中的那个智能体印成 offline 会被读成"坏了"——把选中的 agent 名当参数传进来，
    # 未选中的那条标 skipped 并写清"你选的是另一个"（2026-09-22 同事被这条误导过两次）。
    for c in (d.get("components") or []):
        name = c.get("name", "")
        if name in ("dsh", "harness") and arg and name != arg:
            print("  %-12s %s  （未使用：你选的是 %s）" % (name, "skipped", arg))
        else:
            print("  %-12s %s" % (name, c.get("status", "")))
elif mode == "harness_state":
    # 标准版 harness 的状态在 **components** 里（name='harness'）。
    # 顶层 `dsh` 是 DSH **桌面版**那条适配器，选 harness 时它本来就该是 offline ——
    # 拿它判 harness 会**恒为假**：白等 150 秒、报"还差 1 项"（2026-09-22 同事实测）。
    for c in (d.get("components") or []):
        if c.get("name") == "harness":
            print(c.get("status") or "")
            sys.exit(0)
    print("")
' "$1" "${2:-}" 2>/dev/null || true
}

api_up() { api GET /api/status "" 5 | grep -q '"components"'; }

ensure_service() {
  step "启动 ECHO 服务"
  if api_up; then ok "服务已在运行（端口 $(echo_port)）"; return 0; fi
  local starter="$CODE/mac/start_mac.sh"
  if [ ! -f "$starter" ]; then err "找不到启动脚本：$starter"; exit 1; fi
  say "服务没在跑，后台启动一次…"
  ( cd "$CODE" && nohup bash "$starter" >/dev/null 2>&1 & ) || true
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
    if "$PY" -m pip install --no-warn-script-location $pip_args $pkgs; then
    ok "pip 返回成功：$pkgs"
  else
    warn "pip 返回非零 —— 下面会用 import 逐个复核，装不上的会明确列出来"
  fi
}

# 依赖是不是真装上了：以 import 为准（python.exe 存在、pip 说成功都不算）
# 2026-10-31：客户端没有本地 STT 引擎可验证 —— 转写链路由能力后端负责。

# 2026-10-31：客户端不再下载/安装任何本地模型。

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
[ -d "$CODE/app" ] || { err "这个目录里没有 app/ —— 看起来不是 ECHO 安装目录：$CODE"; exit 1; }
ok "运行时：$PY"
say "数据根：$DATA_DIR"

PIPS=""
MODEL_IDS=""
ENGINE_LIST=""
FIRST_STT=""
WHISPER_STT=""
if [ -n "$ENGINES" ] || [ "$WAKE" -eq 1 ] || [ "$DIARIZE" -eq 1 ] || [ "$ACCEL_CUDA" -eq 1 ]; then
  warn "客户端已不再安装本地 STT 引擎/唤醒/分离/CUDA 组件（转写全部走能力后端）——这些参数已忽略"
fi

# 智能体（标准版 harness）：**本地永久安装 + 绝对路径直连**（2026-09-22 改）。
# 为什么不用 npx：同一台机器实测 `npx -y @deepseek-ai/dsh web` 冷启动要 **2 分 10 秒**，
# 而直连本地 bin.js 只要 **9 秒** —— 慢在 npx 每次重新解析安装 + 回滚清理。
# 绝对路径还顺带绕开"进程 PATH 里没有 node"与宿主的安全删除 shim。
# 装法见 harness-install-local.sh（npm 装不下来时从 npx 缓存复制同版本树；再失败才回退 npx）。
HARNESS_COMMAND=""
prepare_agent() {
  [ "$AGENT" = "harness" ] || return 0
  step "准备智能体（标准版 harness：本地永久安装）"
  local node_bin="" d=""
  if command -v node >/dev/null 2>&1; then node_bin="$(command -v node)"; fi
  if [ -z "$node_bin" ]; then
    for d in /opt/homebrew/bin /usr/local/bin "$HOME/.volta/bin"; do
      if [ -x "$d/node" ]; then node_bin="$d/node"; break; fi
    done
  fi
  if [ -z "$node_bin" ]; then
    warn "没找到 node —— 标准版 harness 需要 Node.js（brew install node）"
    return 1
  fi
  ok "node: $node_bin"

  # 装/修本地永久入口交给专门的脚本：它按"① 已装好 ② npm install ③ 从 npx 缓存复制"
  # 三条路依次试。**为什么要留着第 ③ 条**：@deepseek-ai/dsh@0.1.5-rc.2 的依赖图出过
  # registry 事故（子包依赖 ^0.1.5-rc.3，而那个 rc.3 当时没发布），npm 会 ETARGET ——
  # 老逻辑这时只会回退 npx，用户那边每次冷启动都要 2 分钟。该事故 2026-09-22 晚复测已恢复
  # （rc.3 发布了、npm 584 包装成），但这类事故会复发，而 npx 慢是必然的，故三条路都保留。
  local helper="" cmd_file=""
  helper="$(dirname "$0")/harness-install-local.sh"
  if [ ! -f "$helper" ]; then
    warn "缺 $helper —— 回退到 npx（首次启动要多等 1-2 分钟）"
    return 1
  fi
  cmd_file="${TMPDIR:-/tmp}/echo-harness-command-$$.txt"
  rm -f "$cmd_file" 2>/dev/null || true
  if ! bash "$helper" --dest "$DEST" --version "$DSH_VERSION" --node "$node_bin" \
        --command-file "$cmd_file" --log-file "$INSTALL_LOG"; then
    warn "标准版本地永久安装没成 —— 回退到 npx，首次启动要多等 1-2 分钟（日志里有原因）"
    rm -f "$cmd_file" 2>/dev/null || true
    return 1
  fi
  if [ -s "$cmd_file" ]; then
    HARNESS_COMMAND="$(cat "$cmd_file")"
  fi
  rm -f "$cmd_file" 2>/dev/null || true
  if [ -z "$HARNESS_COMMAND" ]; then
    warn "安装脚本没回传启动命令 —— 回退 npx"
    return 1
  fi
  ok "标准版已就绪（本地永久安装，冷启动约 10 秒）"
  say "  启动命令：$HARNESS_COMMAND"
  return 0
}

wait_harness() {   # 写完设置后 ECHO 会自动拉起；这里等它 online
  [ "$AGENT" = "harness" ] || return 0
  # 等待时长分档：本地永久安装（绝对路径直连）实测 ~11 秒就起；走 npx 才要一两分钟。
  # 从前一律 150 秒，那是照 npx 时代定的 —— 失败时白等两分半。
  local limit=45 how="本地永久安装，通常 10 秒内"
  if [ -z "$HARNESS_COMMAND" ]; then limit=150; how="npx 路径，首次要 1-2 分钟"; fi
  step "等智能体就绪（$how）"
  local waited=0 st="" last=""
  while [ "$waited" -lt "$limit" ]; do
    st="$(api GET /api/status "" 10 | json_query harness_state)"
    if [ "$st" = "online" ]; then
      ok "智能体已就绪（harness online）"
      return 0
    fi
    if [ "$st" = "failed" ]; then
      # 它已经明确失败了就别等满 —— 直接把原因指向日志（多半是 node 不在 PATH / 上次装残）
      err "智能体启动失败 —— 看 logs 下的 harness 日志（node 不在 PATH、或上次装残了）"
      return 1
    fi
    [ -n "$st" ] && last="$st"
    sleep 3
    waited=$((waited + 3))
  done
  warn "等 ${limit}s harness 仍未 online（最后状态：${last:-取不到}）—— 看 logs 下的 harness 日志"
  if [ -n "$HARNESS_COMMAND" ]; then
    warn "本地件已装好，先查 node 能否直接跑那条 harnessCommand"
  else
    warn "回退到了 npx，首次拉包慢是常见的"
  fi
  return 1
}

agent_ok=0
prepare_agent && agent_ok=1

ensure_service

# 2026-10-31：客户端没有本地模型可下/可跳过。

SETTINGS=()
# 2026-10-31：客户端不再使用本地转写引擎 —— 不写 sttModel/meetingSttModel/wakeEnabled。
if [ -n "$MODELS_DIR" ]; then SETTINGS+=("modelsDir=$MODELS_DIR"); fi
if [ -n "$MEETINGS_DIR" ]; then SETTINGS+=("meetingsDir=$MEETINGS_DIR"); fi
if [ -n "$NOTES_DIR" ]; then SETTINGS+=("worklogVaultRoot=$NOTES_DIR" "worklogEnabled=true"); fi
case "$AGENT" in
  harness)
    SETTINGS+=("agentBackend=harness" "agentHarnessEnabled=true")
    # **默认不写 harnessCommand**（2026-09-22 晚改）：设置停在出厂值时，ECHO 自己会
    # 优先用本地入口（`harness_proc.command()`：出厂值 + 本地入口存在 → 直连），
    # node 也由它探测（与上面 Resolve-NodeDir/ECHO 的 node_dirs 同一批目录）。
    # 好处：node 换版本（WorkBuddy / brew 升级）不用改配置 —— 写死绝对路径时，
    # 那条路径一失效 harness 就起不来，而这个设置是隐藏项，用户很难自己找到。
    # 想钉死：加 --pin-harness-command。
    if [ -n "$HARNESS_COMMAND" ] && [ "$PIN_HARNESS_COMMAND" -eq 1 ]; then
      SETTINGS+=("harnessCommand=$HARNESS_COMMAND")
    fi
    ;;
  dsh)     SETTINGS+=("agentBackend=dsh") ;;
  none)    ;;
  *)       warn "不认识的 --agent：$AGENT（按 none 处理）" ;;
esac
if [ "${#SETTINGS[@]}" -gt 0 ]; then write_settings "${SETTINGS[@]}" || true; else say "没有要写的设置"; fi

step "自检"
api GET /api/status "" 20 | json_query components "$AGENT" || warn "取 /api/status 失败"
FAILED=""
wait_harness || FAILED="$FAILED 智能体"

# 两个默认分组（「会议空间」/「指令空间」）：装好就该在 DSH 侧栏看到，而不是等开完
# 第一场会、说第一句指令才冒出来。走 ECHO 自己的接口，幂等；用户改过名的工作区不动
# （见 app/workspaces.py）。放在 wait_harness 之后：workspace/create 要 DSH 正在监听。
step "建立 DSH 分组（会议空间 / 指令空间）"
SP="$(api POST /api/dsh/workspaces/ensure '{}' 90)"
if [ -z "$SP" ]; then
  warn "调 /api/dsh/workspaces/ensure 失败（不影响使用，ECHO 建会话时会自动补）"
else
  # 每项一行：label / action / title / path，用 python 拆（mac 上不一定有 jq）
  printf '%s' "$SP" | "$PY" -c '
import json, sys
try:
    spaces = json.load(sys.stdin).get("spaces") or []
except Exception as e:
    print("  [warn] 解析分组结果失败：%s" % e); raise SystemExit(0)
for it in spaces:
    a = it.get("action")
    if a == "failed":
        print("  [fail] %-10s 建立失败：%s" % (it.get("label"), it.get("detail")))
    elif a == "no-agent":
        print("  [warn] %-10s 智能体没就绪，稍后 ECHO 会自动补：%s" % (it.get("label"), it.get("detail")))
    elif a == "skipped":
        print("  [warn] %-10s 没配目录，跳过（会话会落在「未分组」）" % it.get("label"))
    else:
        print("  [ok]   %-10s 「%s」→ %s" % (it.get("label"), it.get("title"), it.get("path")))
'
  case "$SP" in *'"action": "failed"'*|*'"action":"failed"'*) FAILED="$FAILED 分组" ;; esac
fi

# 登记安装 —— 这是"装完了"的凭据：面板据此不再提示未安装、也不再自动进向导
step "登记安装"
REPORT=$(cat <<JSON
{"installer":"echo-install skill","platform":"macos","destDir":"$DEST",
 "engines":"","wake":false,"diarize":false,
 "agent":"$AGENT","models":"","notes":"客户端无本地 STT（转写走能力后端）"}
JSON
)
r="$(api POST /api/install/report "{\"report\": $REPORT}" 30)"
if printf '%s' "$r" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
  ok "已登记（面板不会再提示「还没装完」）"
else
  warn "登记失败（不影响使用）：$r"
fi

echo ""
if [ -z "$FAILED" ]; then
  echo "  全部就绪。怎么开始用："
  say "1) 浏览器打开 http://127.0.0.1:$(echo_port)/"
  say "   mac 上热键要先在「系统设置 → 隐私与安全性 → 辅助功能 / 输入监控」里授权"
  say "2) 首次运行会提示「来自身份不明的开发者」（还没签名公证）—— 右键打开或去隐私设置里允许"
  say "3) 以后想加能力：面板 → 能力 → 随时补（不用重装）"
  exit 0
fi
echo "  装完了，但**这些没就绪** —— 别当成装好了：$FAILED"
say "补救：按上面的提示装依赖 / 重跑本脚本（已装好的会跳过）；也可以到 面板 → 能力 里重试。"
exit 1
