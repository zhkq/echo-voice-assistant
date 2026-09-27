#!/usr/bin/env bash
# =====================================================================
# ECHO 能力后端 —— 容器部署准备（在 **GPU 宿主**上跑，不是开发机）
#
# 它把"跑成容器 + 管理端页面仍可用"这条路走完。每一步都**幂等**（可以反复跑），
# 失败时**明确报真原因**并给出下一步，绝不静默跳过。
#
#   1. 前置检查：nvidia-smi / nvidia-container-toolkit / docker + compose v2 / 磁盘余量
#   2. 目录与权限：<root>/{models,tmp,state,tls,config}
#   3. 模型卷（**只读**挂进容器）：qwen3asr + **强制对齐器** + pyannote 三件套，
#      按容器里的目录布局摆放并**逐个校验**（缺对齐器默认算失败，见 --allow-missing-aligner）
#   4. 状态卷 + <root>/echo-backend.env（生成 ECHO_JWT_SECRET；**已存在就沿用**）
#   5. TLS：自签证书（默认 `--tls self-signed`）或"前面放 TLS 终结"（`--tls proxy`）
#   6. 生成 compose override（配置挂载 / 打开鉴权 / 探针 scheme）
#   7. `docker compose ... up -d --build`
#   8. 建管理员（**只在还没有账号时**；管理面刻意不开账号管理）
#   9. 验收：/v1/health 期望值 + `scripts/smoke-echo-backend.py`
#
# 用法（在仓库根目录跑）：
#   sudo bash scripts/prepare-backend.sh --from user@源机:/home/u/echo --server-host gpu-01
#   sudo bash scripts/prepare-backend.sh --from-modelscope          # 联网下模型
#   sudo bash scripts/prepare-backend.sh --dry-run                  # 只看它要做什么
#   sudo bash scripts/prepare-backend.sh                            # 只校验 + 起容器
#
# 退出码：0 成功（可能带警告）｜1 前置/用法/权限｜2 compose 起不来｜3 模型卷有问题
#         ｜4 验收（冒烟）没过
#
# 详尽的背景、目录布局为什么是这样、以及**哪些东西没在真机验过**，
# 见 `docs/后端容器部署.md`。
# =====================================================================
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

#: pyannote 三件套：与 `app/modelinfo.py:PYANNOTE_ASSETS` **必须逐字一致**
#: （两处各写一份必然漂，所以 `tests/test_server_contract.py::ContainerDeploymentTests`
#: 有一条用例直接比对这两份；改动这里就要同时改代码那边）。格式：仓库|目录|文件1,文件2
PYANNOTE_ASSETS="pyannote/segmentation-3.0|pyannote-segmentation-3.0-local|pytorch_model.bin
pyannote/wespeaker-voxceleb-resnet34-LM|pyannote-wespeaker-local|pytorch_model.bin
pyannote/speaker-diarization-community-1|pyannote-plda-local|plda/plda.npz,plda/xvec_transform.npz"

# ---------------------------------------------------------------- 参数
DRY_RUN=0
ROOT_DIR=/srv/echo
SRC=""
USE_MODELSCOPE=0
MODELS_ONLY=0
SKIP_MODELS=0
ALLOW_MISSING_ALIGNER=0
ADMIN_NAME=ops
TLS_MODE=self-signed
SERVER_HOST=""
VRAM_BUDGET_MB=""
AUTH_ENABLED=1
DO_UP=1
DO_SMOKE=1
COMPOSE_FILE="server/compose.yaml"
CONTAINER=echo-backend
# 老卡（Pascal/Volta）相关的两个构建期参数（见 docs/后端容器部署.md 的「按显卡选规格」）
TORCH_INDEX=""
TORCH_VERSION=""
PIP_INDEX=""
NO_TORCH_INDEX=0
EXTRA=0
ASR_IMPL=""
ASR_EST_VRAM_MB=""
GPU_CC=""
OLD_CARD=0
NO_BF16=0

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    cat <<'EOF'

选项：
  --from SRC            从源机拷模型（推荐）。SRC = user@host:/path/to/echo 或本地路径。
                        脚本会在 SRC 下依次找 HF 布局 / ModelScope 缓存 / models/ 三处。
  --from-modelscope     联网从 ModelScope 下（宿主需 python3 + modelscope）。
  --root DIR            宿主数据根，默认 /srv/echo。
  --models-only         只准备模型卷（不碰容器）。
  --skip-models         跳过模型卷（只做密钥 / TLS / 起容器）。
  --allow-missing-aligner
                        缺强制对齐器时降级成**警告**（默认是失败）。
                        ⚠️ 接受它 = 接受 `supports: [asr.text, asr.timestamps]` 是假话。
  --extra               在 override 里把 ECHO_EXTRA 设成 "1"（装上 torch 那组；
                        不装的话转写/说话人分离都会 model_failed）。镜像会大 3~5 GB。
  --asr-impl NAME       写进生成配置的转写引擎：sensevoice | qwen3asr。**老卡要用它** ——
                        Pascal/Volta 没有 bf16，而 qwen3asr 那条路是按 bf16 加载的（文档 10.3）。
                        给了它就会在 /srv/echo/config/server.yaml 里写一份**完整**的
                        models.specs（三档一起写：只写 asr-long 会把默认清单整份替换掉）。
  --asr-est-vram-mb N   配合 --asr-impl：asr-long 的 est_vram_mb（sensevoice 默认 1200，
                        qwen3asr 默认 4700）。跑过真音频后用 /v1/health 的 vram.usedMb
                        校准，再把实测值写回这里 —— 上面那两个默认值都是估算。
  --torch-index URL     torch 的 CUDA 源（构建期）。老卡（Pascal/Volta，sm_<7.5）
                        由脚本**自动**写成 https://download.pytorch.org/whl/cu118。
  --pip-index URL       **普通依赖**的 pip 源（构建期；默认 PyPI，与从前一致）。
                        国内网络上 PyPI 的 wheel 可能只有一二百 kB/s，而清华镜像同一时刻
                        能到 26 MB/s（2026-09-28 实测，差 170 倍）—— 长构建就差在这里：
                        `--pip-index https://pypi.tuna.tsinghua.edu.cn/simple`
                        ⚠️ torch **不**走它（torch 走 --torch-index）。
  --torch-version VER   钉 torch 版本（例如 2.7.1）。老卡上会自动补一个建议值。
  --no-torch-index      不写 torch 源（用 server/Dockerfile 里的默认）。
  --admin NAME          管理员账号名，默认 ops。
  --server-host HOST    客户端要连的地址（证书 CN/SAN 用它），默认自动探测。
  --tls MODE            self-signed（默认）| proxy | none。
  --vram-budget-mb N    写进生成的配置（8 GB 卡建议 7000）。默认不写 = 0（不限）。
  --no-auth             不打开鉴权（**只用于隔离实验网**，会大声警告）。
  --no-up               只准备，不 `up`。
  --skip-smoke          不跑冒烟自测。
  --dry-run             只打印要做什么，不改任何东西（校验仍然照跑，是只读的）。
  -h | --help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --root) ROOT_DIR="${2:?--root 后面要给目录}"; shift ;;
        --from) SRC="${2:?--from 后面要给路径}"; shift ;;
        --from-modelscope) USE_MODELSCOPE=1 ;;
        --models-only) MODELS_ONLY=1; DO_UP=0; DO_SMOKE=0 ;;
        --skip-models) SKIP_MODELS=1 ;;
        --allow-missing-aligner) ALLOW_MISSING_ALIGNER=1 ;;
        --extra) EXTRA=1 ;;
        --asr-impl) ASR_IMPL="${2:?--asr-impl 后面要给 sensevoice|qwen3asr}"; shift ;;
        --asr-est-vram-mb) ASR_EST_VRAM_MB="${2:?--asr-est-vram-mb 后面要给数字}"; shift ;;
        --torch-index) TORCH_INDEX="${2:?--torch-index 后面要给 URL}"; shift ;;
        --pip-index) PIP_INDEX="${2:?--pip-index 后面要给 URL}"; shift ;;
        --torch-version) TORCH_VERSION="${2:?--torch-version 后面要给版本号}"; shift ;;
        --no-torch-index) NO_TORCH_INDEX=1 ;;
        --admin) ADMIN_NAME="${2:?--admin 后面要给名字}"; shift ;;
        --server-host) SERVER_HOST="${2:?--server-host 后面要给地址}"; shift ;;
        --tls) TLS_MODE="${2:?--tls 后面要给 self-signed|proxy|none}"; shift ;;
        --vram-budget-mb) VRAM_BUDGET_MB="${2:?--vram-budget-mb 后面要给数字}"; shift ;;
        --no-auth) AUTH_ENABLED=0 ;;
        --no-up) DO_UP=0 ;;
        --skip-smoke) DO_SMOKE=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "不认识的参数：$1（--help 看用法）" >&2; exit 1 ;;
    esac
    shift
done

# ---------------------------------------------------------------- 输出与执行
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_GRN=$'\033[32m'; C_DIM=$'\033[2m'; C_B=$'\033[1m'
    C_OFF=$'\033[0m'
else
    C_RED=""; C_YEL=""; C_GRN=""; C_DIM=""; C_B=""; C_OFF=""
fi

WARNINGS=0
ERRORS=0

#: 只给"**qwen3asr / 强制对齐器 这两份权重不在**"这一类错误计数。
#: 为什么单独数：这两份权重在两条**既定**路线上是**允许缺**的 ——
#:   ① 老卡（Pascal/Volta，sm_<7.5）+ SenseVoice 在位 → 转写本来就走 SenseVoice（文档 10.4 B）；
#:   ② 显式给了 `--allow-missing-aligner` → 接受没有句级时间戳（文档 2.3）。
#: 但这两条路线在真机上跑过才发现：脚本一边打印"只算警告"，一边把它们记进 `ERRORS`，
#: 于是**汇总里报"有 N 处错误"并以退出码 1 收场** —— 与它自己说的话矛盾，也让人以为
#: 部署失败了。所以这些错误走 `bad_model`，在"允许缺"成立时统一降级（见第 3 节末尾）。
MODEL_ERRS=0

step() { printf '\n%s== %s%s\n' "$C_B" "$*" "$C_OFF"; }
log()  { printf '%s[prepare]%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { WARNINGS=$((WARNINGS + 1)); printf '  %s[警告]%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
bad()  { ERRORS=$((ERRORS + 1)); printf '  %s[错误]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
#: 与 bad 一样，但**可以**被"允许缺"的那两条路线降级成警告（见 MODEL_ERRS 的注释）
bad_model() { MODEL_ERRS=$((MODEL_ERRS + 1)); bad "$@"; }
die()  { bad "$@"; exit 1; }

#: 所有会改系统的动作都过这里 —— `--dry-run` 下只打印
run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '  %s(dry-run)%s' "$C_DIM" "$C_OFF"
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

#: 写文件（内容从 stdin 来）：内容没变就不动它（幂等）。`--dry-run` 下只报字节数。
write_file_if_changed() { # path mode
    local path="$1" mode="${2:-644}" new="${1}.new.$$" n
    if [ "$DRY_RUN" = 1 ]; then
        n="$(wc -c)"
        printf '  %s(dry-run)%s 将写 %s（%s 字节，mode %s）\n' \
            "$C_DIM" "$C_OFF" "$path" "$n" "$mode"
        return 0
    fi
    mkdir -p "$(dirname "$path")" || return 1
    cat >"$new" || { bad "写不了临时文件 $new"; return 1; }
    if [ -f "$path" ] && cmp -s "$new" "$path"; then
        rm -f "$new"
        log "内容没变，不动 $path"
        return 0
    fi
    chmod "$mode" "$new" || true
    mv "$new" "$path"
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- 路径
MODELS_DIR="$ROOT_DIR/models"
HUB_DIR="$MODELS_DIR/hub"
PYANNOTE_DIR="$MODELS_DIR/pyannote"
TMP_DIR="$ROOT_DIR/tmp"
STATE_DIR="$ROOT_DIR/state"
#: ModelScope 的**运行期缓存**（SenseVoice 的 `vad_model="fsmn-vad"` 要现拉，约 1.7 MB）。
#: 根文件系统只读，所以它必须落在**可写**的卷上（见 server/compose.yaml 里那两行环境变量）。
CACHE_DIR="$ROOT_DIR/cache"
TLS_DIR="$ROOT_DIR/tls"
CONF_DIR="$ROOT_DIR/config"
ENV_FILE="$ROOT_DIR/echo-backend.env"
SERVER_YAML="$CONF_DIR/server.yaml"
OVERRIDE_YML="$ROOT_DIR/compose.local.yml"

ASR_REPO="Qwen/Qwen3-ASR-0.6B"
ALIGNER_REPO="Qwen/Qwen3-ForcedAligner-0.6B"
ASR_DIR="$HUB_DIR/models--Qwen--Qwen3-ASR-0.6B/snapshots/master"
ALIGNER_DIR="$HUB_DIR/models--Qwen--Qwen3-ForcedAligner-0.6B/snapshots/master"
#: SenseVoice（老卡推荐的转写引擎）：容器里必须落在模型卷下（ModelScope 缓存不在卷里）
SENSEVOICE_DIR="$MODELS_DIR/sensevoice"

[ -f "$REPO_ROOT/$COMPOSE_FILE" ] || \
    die "找不到 $REPO_ROOT/$COMPOSE_FILE —— 请在仓库根目录跑这个脚本（或用完整路径调用）"

printf '%sECHO 能力后端 · 容器部署准备%s（仓库：%s）\n' "$C_B" "$C_OFF" "$REPO_ROOT"
if [ "$DRY_RUN" = 1 ]; then printf '%s（dry-run：不会改动任何东西）%s\n' "$C_YEL" "$C_OFF"; fi

# =====================================================================
# 1. 前置检查
# =====================================================================
step "1/9 前置检查"

if [ "$(uname -s 2>/dev/null || echo unknown)" != "Linux" ]; then
    die "这个脚本是给 Linux GPU 宿主用的（Docker 在 Windows/macOS 上跑的也是 Linux 容器，
但宿主侧的驱动、卷路径、证书都要在 Linux 上准备）。当前系统：$(uname -s 2>/dev/null)"
fi

if [ "$(id -u 2>/dev/null || echo 1)" != "0" ]; then
    warn "当前不是 root —— 写 $ROOT_DIR、动 Docker 都可能失败。建议：sudo bash $0 ..."
fi

# --- GPU 驱动 ---
if need_cmd nvidia-smi; then
    if gpus="$(nvidia-smi -L 2>&1)"; then
        ok "驱动可见 GPU：$(printf '%s' "$gpus" | head -1)"
    else
        bad "nvidia-smi 存在但跑不起来：$gpus"
        bad "  → 先修驱动（重启 / dkms / 驱动版本与内核匹配），别继续往下走"
    fi
else
    bad "没有 nvidia-smi —— 装 NVIDIA 驱动（发行版仓库的 nvidia-driver-* 或官方 runfile）"
fi

# --- 算力等级：决定 torch 的 CUDA 源与"能不能吃 bf16"（见文档「按显卡选规格」）---
gpu_cc() {
    local cc name
    cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"
    case "$cc" in
        [0-9].[0-9]) printf '%s' "$cc"; return 0 ;;
    esac
    # 老驱动没有 compute_cap 字段：按型号名兜底（可能认不准，认不准就当"新卡"但会提示）
    name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
    case "$name" in
        *V100*|*"Tesla V"*)                              printf '7.0'; return 0 ;;
        *"GTX 10"*|*"Tesla P"*|*P100*|*P40*|*P4*|*MX1*|*MX2*|*"Quadro P"*)
                                                         printf '6.1'; return 0 ;;
        *T4*|*"GTX 16"*|*"RTX 20"*|*"Quadro RTX"*|*"Tesla T"*)
                                                         printf '7.5'; return 0 ;;
    esac
    return 1
}
if GPU_CC="$(gpu_cc)"; then
    ok "GPU 算力等级 sm_$GPU_CC"
else
    GPU_CC=""
    warn "认不出 GPU 的算力等级（nvidia-smi 既没给 compute_cap，型号名也没匹配上）——"
    warn "  请自己对照 docs/后端容器部署.md「按显卡选规格」判断 torch 源与 bf16。"
fi
if [ -n "$GPU_CC" ]; then
    _cc_major="${GPU_CC%%.*}"; _cc_minor="${GPU_CC##*.}"
    # Maxwell/Pascal/Volta（< 7.5）：新 CUDA 已经不含它们的架构 → 必须钉 cu118
    if [ "$_cc_major" -lt 7 ] || { [ "$_cc_major" -eq 7 ] && [ "$_cc_minor" -lt 5 ]; }; then
        OLD_CARD=1
    fi
    # bf16 要 sm_80+；Turing(7.5)/Volta/Pascal 都没有
    if [ "$_cc_major" -lt 8 ]; then NO_BF16=1; fi
fi

# --- nvidia-container-toolkit（容器 GPU 直通）---
TOOLKIT_OK=0
if need_cmd nvidia-ctk || need_cmd nvidia-container-toolkit || need_cmd nvidia-container-cli; then
    TOOLKIT_OK=1
elif [ -e /usr/bin/nvidia-container-toolkit ] || [ -e /usr/local/bin/nvidia-container-toolkit ]; then
    TOOLKIT_OK=1
fi
if [ "$TOOLKIT_OK" = 1 ]; then
    ok "nvidia-container-toolkit 在位"
else
    bad "没找到 nvidia-container-toolkit —— 缺了它容器看不到 GPU（compose up 会报"
    bad "  \"could not select device driver '' with capabilities: [[gpu]]\"）。装法："
    bad "    Debian/Ubuntu: sudo apt-get install -y nvidia-container-toolkit && \\"
    bad "                   sudo nvidia-ctk runtime configure --runtime=docker && \\"
    bad "                   sudo systemctl restart docker"
    bad "    RHEL/Rocky:    sudo dnf install -y nvidia-container-toolkit && sudo systemctl restart docker"
fi

# --- docker + compose v2 ---
COMPOSE_BIN=""
if need_cmd docker; then
    if ver="$(docker version --format '{{.Server.Version}}' 2>&1)"; then
        ok "docker 服务端 $ver"
    else
        bad "docker 客户端在，但连不上守护进程：$ver"
        bad "  → systemctl start docker（或把当前用户加进 docker 组后重新登录）"
    fi
    if docker compose version >/dev/null 2>&1; then
        COMPOSE_BIN="docker compose"
        ok "compose v2（$(docker compose version --short 2>/dev/null || echo '?')）"
    else
        bad "没有 \`docker compose\`（v2）—— 这个部署只用 v2（老 \`docker-compose\` 没测过）。"
        bad "  → 装 docker-compose-plugin（或升级 docker-ce）"
    fi
    if runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null)"; then
        case "$runtimes" in
            *nvidia*) ok "Docker 已注册 nvidia runtime" ;;
            *)
                if [ "$TOOLKIT_OK" = 1 ]; then
                    warn "Docker 的 runtimes 里没看到 nvidia（$runtimes）—— 跑一次"
                    warn "  sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
                fi
                ;;
        esac
    fi
else
    bad "没有 docker —— 先装 Docker Engine（https://docs.docker.com/engine/install/）"
fi
[ -n "$COMPOSE_BIN" ] || die "compose v2 不可用，后面的步骤没意义（先把上面两条 [错误] 解决）"

# --- 磁盘余量（模型 ~6 GB + 镜像 ~2 GB，ECHO_EXTRA=1 再加 3~5 GB）---
if avail_kb="$(df -Pk "$(dirname "$ROOT_DIR")" 2>/dev/null | awk 'NR==2 {print $4}')"; then
    if [ -n "$avail_kb" ]; then
        avail_gb=$((avail_kb / 1024 / 1024))
        if [ "$avail_gb" -lt 12 ]; then
            warn "磁盘只剩 ${avail_gb} GB（$(dirname "$ROOT_DIR")）：模型卷约 6 GB + 镜像约 2 GB，"
            warn "  而 ECHO_EXTRA=1（要跑模型就得开）会再要 3~5 GB。先清一点空间。"
        else
            ok "磁盘余量 ${avail_gb} GB"
        fi
    fi
fi

if [ "$ERRORS" != 0 ]; then
    if [ "$DRY_RUN" = 1 ]; then
        warn "前置检查有 $ERRORS 处错误（dry-run 继续把计划打完，真跑会在这里停下）"
    else
        die "前置检查有 $ERRORS 处错误 —— 先把它们解决（脚本刻意不带着问题往下走）"
    fi
fi

# --- 按显卡定 torch 源 + bf16 提醒（这一段的判据见 docs/后端容器部署.md「按显卡选规格」）---
if [ "$NO_TORCH_INDEX" = 1 ]; then
    log "--no-torch-index：用 server/Dockerfile 里的默认源（cu126），override 里不写这一项"
elif [ -n "$TORCH_INDEX" ]; then
    log "torch 源用你给的 --torch-index $TORCH_INDEX"
    [ -n "$TORCH_VERSION" ] || log "  （没给 --torch-version：装那个源上最新的一版；老卡建议一起钉）"
elif [ "$OLD_CARD" = 1 ]; then
    # Pascal（sm_6x）/ Volta（sm_70）：CUDA 13.x 只支持 sm_75+，PyTorch 2.15 起连
    # CUDA 12.6 的 wheel 都不再发布 —— 默认源装出来的 torch **不含这块卡的架构**。
    TORCH_INDEX="https://download.pytorch.org/whl/cu118"
    TORCH_VERSION="2.7.1"          # cu118 源上目前最新的一版
    ok "老卡（sm_$GPU_CC < 7.5）→ torch 源自动设成 cu118，并钉 2.7.1"
else
    log "torch 源用默认（cu126；Turing 及以后）；override 里不写这一项"
fi

if [ "$OLD_CARD" = 1 ]; then
    warn "这块卡是 Maxwell/Pascal/Volta（sm_$GPU_CC）：新 CUDA 已经不含它的架构，"
    if [ -n "$TORCH_INDEX" ]; then
        warn "  所以 torch **必须**从 cu118 装 —— 脚本已把 build.args 写进 override（见上面那行）。"
    else
        warn "  所以 torch **必须**从 cu118 装，但你给了 --no-torch-index —— 请自己把镜像的"
        warn "  torch 源换成 cu118，否则这块卡上装出来的 torch 跑不起来。"
    fi
fi
if [ "$NO_BF16" = 1 ]; then
    warn "这块卡（sm_$GPU_CC）**没有 bf16**，而 qwen3asr 那条加载路径是按 bf16 请求的"
    warn "  （app/audio/stt.py:_get_qwen3asr 里 dtype 写死 \"bf16\"）—— fp32 会把显存翻倍，"
    warn "  8 GB 卡上很容易踩回『溢出换页 → 慢十倍、文本只出几十字』那个坑。"
    warn "  → **建议这类卡别跑 qwen3asr，改用 SenseVoice**（见文档「按显卡选规格」）。"
fi

# =====================================================================
# 2. 目录与权限
# =====================================================================
step "2/9 目录与权限"
for d in "$MODELS_DIR" "$HUB_DIR" "$PYANNOTE_DIR" "$TMP_DIR" "$STATE_DIR" "$TLS_DIR" "$CONF_DIR" "$CACHE_DIR"; do
    run mkdir -p "$d"
done
run chmod 700 "$STATE_DIR" "$TMP_DIR" "$TLS_DIR"
ok "数据根 $ROOT_DIR（models 只读挂 / tmp 可丢 / state 持久 / cache 是 ModelScope 的运行期缓存）"

# =====================================================================
# 3. 模型卷
# =====================================================================
step "3/9 模型卷（$MODELS_DIR，只读挂进容器）"

is_remote() { case "$1" in *:*) return 0 ;; *) return 1 ;; esac; }

src_exists() { # path（可能是 user@host:/path）
    local p="$1"
    if is_remote "$p"; then
        ssh -o BatchMode=yes -o ConnectTimeout=8 "${p%%:*}" "test -d '${p#*:}'" >/dev/null 2>&1
    else
        [ -d "$p" ]
    fi
}

first_src() { # 候选路径…（打印第一个存在的；都不在则返回 1）
    local p
    for p in "$@"; do
        if src_exists "$p"; then printf '%s\n' "$p"; return 0; fi
    done
    return 1
}

copy_tree() { # src dst label
    local src="$1" dst="$2" label="$3"
    # 不带 --delete：万一源机缺了某一个模型，不该顺手把目标里已有的那个删掉。
    if is_remote "$src"; then
        if ! need_cmd rsync; then
            bad "$label：源在远端但没有 rsync —— 源机与宿主都装一个（apt-get install -y rsync）"
            return 1
        fi
        run mkdir -p "$dst" || return 1
        # `-aL` 把符号链接实体化：HF/ModelScope 缓存里大量文件是软链，而容器里的
        # 只读挂载看不到链子的目标 —— 那就是"白准备几 GB 然后 model_failed"的坑。
        run rsync -aL --info=progress2 "$src"/ "$dst"/ || return 1
    else
        run mkdir -p "$dst" || return 1
        if need_cmd rsync; then
            run rsync -aL "$src"/ "$dst"/ || return 1
        else
            warn "宿主没有 rsync，退回 cp -aL（慢一些，结果一样）"
            run cp -aL "$src"/. "$dst"/ || return 1
        fi
    fi
    ok "$label ← $src"
}

fetch_modelscope() { # repo target label
    local repo="$1" target="$2" label="$3"
    if ! need_cmd python3; then
        bad "$label：--from-modelscope 需要宿主的 python3"; return 1
    fi
    if ! python3 -c 'import modelscope' >/dev/null 2>&1; then
        bad "$label：宿主没装 modelscope —— python3 -m pip install modelscope（或改用 --from 从源机拷）"
        return 1
    fi
    run mkdir -p "$target" || return 1
    if [ "$DRY_RUN" = 1 ]; then
        printf '  %s(dry-run)%s 从 ModelScope 下 %s → %s\n' "$C_DIM" "$C_OFF" "$repo" "$target"
        return 0
    fi
    if REPO="$repo" TARGET="$target" python3 - <<'PY'
import os
from modelscope import snapshot_download
snapshot_download(os.environ["REPO"], local_dir=os.environ["TARGET"])
PY
    then
        ok "$label ← ModelScope $repo"
    else
        bad "$label：ModelScope 下载失败（原始报错在上面；内网多半是被代理/证书拦下）"
        return 1
    fi
}

#: 目录里有没有**真权重**（空目录 / 半份权重都不算）
#: ⚠️ 本函数**只用于 qwen3asr / 强制对齐器**这两份权重（它俩在"老卡 + SenseVoice"与
#:    `--allow-missing-aligner` 两条路线上允许缺），所以错误走 `bad_model` —— 将来若拿它
#:    校验别的模型，要换成 `bad`，否则那些缺失会被误当成"可以降级"。
verify_weights() { # dir label
    local dir="$1" label="$2" n=0 size=""
    if [ ! -d "$dir" ]; then
        bad_model "$label：目录不存在 —— $dir"
        return 1
    fi
    if [ ! -f "$dir/config.json" ]; then
        bad_model "$label：缺 config.json（$dir）—— 目录建了，但权重没进来"
        return 1
    fi
    n="$(find "$dir" -maxdepth 1 -type f \
            \( -name '*.safetensors' -o -name '*.bin' -o -name '*.pt' -o -name '*.pth' \) \
            -size +1k 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$n" -lt 1 ]; then
        bad_model "$label：有 config.json 但**没有权重文件**（*.safetensors/*.bin/*.pt，>1KB）—— $dir"
        return 1
    fi
    size="$(du -sh "$dir" 2>/dev/null | cut -f1)"
    ok "$label：${n} 个权重文件，${size:-?}"
}

if [ "$SKIP_MODELS" = 1 ]; then
    warn "--skip-models：**没有校验模型卷** —— 起来之后每个模型都可能 model_failed"
else
    if [ "$USE_MODELSCOPE" = 1 ] && [ -n "$SRC" ]; then
        die "--from 与 --from-modelscope 只能给一个（要哪个来源自己挑）"
    fi

    if [ -n "$SRC" ]; then
        SRC="${SRC%/}"
        log "从源机拷模型：$SRC（先 HF 布局，再 ModelScope 缓存）"
        if s="$(first_src "$SRC/models/hub/models--Qwen--Qwen3-ASR-0.6B" \
                           "$SRC/hub/models--Qwen--Qwen3-ASR-0.6B" \
                           "$SRC/.cache/modelscope/models/Qwen--Qwen3-ASR-0.6B")"; then
            copy_tree "$s" "$ASR_DIR" "转写（$ASR_REPO）" || true
        else
            bad_model "源机 $SRC 下找不到 qwen3asr —— 找过："
            bad_model "  $SRC/models/hub/models--Qwen--Qwen3-ASR-0.6B"
            bad_model "  $SRC/hub/models--Qwen--Qwen3-ASR-0.6B"
            bad_model "  $SRC/.cache/modelscope/models/Qwen--Qwen3-ASR-0.6B"
        fi
        if s="$(first_src "$SRC/models/hub/models--Qwen--Qwen3-ForcedAligner-0.6B" \
                           "$SRC/hub/models--Qwen--Qwen3-ForcedAligner-0.6B" \
                           "$SRC/.cache/modelscope/models/Qwen--Qwen3-ForcedAligner-0.6B")"; then
            copy_tree "$s" "$ALIGNER_DIR" "强制对齐器（$ALIGNER_REPO）" || true
        else
            bad_model "源机 $SRC 下找不到强制对齐器 —— 找过："
            bad_model "  $SRC/models/hub/models--Qwen--Qwen3-ForcedAligner-0.6B"
            bad_model "  $SRC/.cache/modelscope/models/Qwen--Qwen3-ForcedAligner-0.6B"
        fi
        if s="$(first_src "$SRC/models/pyannote" "$SRC/pyannote")"; then
            copy_tree "$s" "$PYANNOTE_DIR" "说话人分离（pyannote 三件套）" || true
        else
            warn "源机 $SRC 下没有 pyannote —— /v1/diarize 会如实报模型不可用（不致命）"
        fi
        # SenseVoice：老卡（没有 bf16）的推荐引擎。**容器里 ModelScope 缓存不可用**
        # （见 docs/后端容器部署.md），所以它也得进模型卷：{模型根}/sensevoice/。
        if s="$(first_src "$SRC/models/sensevoice" "$SRC/sensevoice")"; then
            copy_tree "$s" "$SENSEVOICE_DIR" "SenseVoice（老卡推荐的转写引擎）" || true
        else
            log "源机 $SRC 下没有 sensevoice —— 只有 qwen3asr；老卡（Pascal/Volta）会没有可用的转写引擎"
        fi
    elif [ "$USE_MODELSCOPE" = 1 ]; then
        log "从 ModelScope 下模型（宿主要能上外网）"
        fetch_modelscope "$ASR_REPO" "$ASR_DIR" "转写（$ASR_REPO）" || true
        fetch_modelscope "$ALIGNER_REPO" "$ALIGNER_DIR" "强制对齐器（$ALIGNER_REPO）" || true
        if [ "$DRY_RUN" = 1 ]; then
            printf '  %s(dry-run)%s 下 pyannote 三件套 → %s\n' "$C_DIM" "$C_OFF" "$PYANNOTE_DIR"
        elif need_cmd python3 && python3 -c 'import modelscope' >/dev/null 2>&1; then
            if PYANNOTE_DIR="$PYANNOTE_DIR" PYANNOTE_ASSETS="$PYANNOTE_ASSETS" python3 - <<'PY'
import os
from modelscope import snapshot_download
root = os.environ["PYANNOTE_DIR"]
for line in os.environ["PYANNOTE_ASSETS"].splitlines():
    line = line.strip()
    if not line:
        continue
    repo, folder, files = line.split("|")
    snapshot_download(repo, local_dir=os.path.join(root, folder),
                      allow_patterns=[f for f in files.split(",") if f])
print("pyannote 下载完成")
PY
            then
                ok "说话人分离（pyannote 三件套）← ModelScope"
            else
                warn "pyannote 下载失败（原始报错在上面）—— /v1/diarize 会报模型不可用"
            fi
        fi
    else
        log "没给 --from / --from-modelscope：只校验现有模型卷（第二次跑就该是这样）"
    fi

    # ---- 校验（无论来源是什么都跑一遍）----
    ASR_OK=0
    ALIGNER_OK=0
    verify_weights "$ASR_DIR" "转写（$ASR_REPO）" && ASR_OK=1
    if verify_weights "$ALIGNER_DIR" "强制对齐器（$ALIGNER_REPO）"; then
        ALIGNER_OK=1
    elif [ "$ALLOW_MISSING_ALIGNER" = 1 ]; then
        warn "缺强制对齐器 —— 你显式给了 --allow-missing-aligner，所以只警告："
        warn "  supports: [asr.text, asr.timestamps] 从此是**假话**，timestamps 会退化成 none"
    fi
    if [ "$ALIGNER_OK" != 1 ] && [ "$ALLOW_MISSING_ALIGNER" != 1 ]; then
        bad_model "⚠️ 没有强制对齐器 = 服务端宣告的 supports: [asr.text, asr.timestamps] 是**假话**："
        bad_model "   句级时间戳只有带上它才有（没有它 funasr 只回一句"
        bad_model "   'return_time_stamps requires forced_aligner. Skipping timestamps.'）。"
        bad_model "   这时 timestamps 会一路退化成 none 并在客户端 estimated 兜底，"
        bad_model "   而 /v1/capabilities 与 /v1/health 都**不会**变红 —— 所以必须在这里报出来。"
        bad_model "   修法：把 $ALIGNER_REPO 放到 $ALIGNER_DIR（或加 --from / --from-modelscope）。"
        bad_model "   确实接受降级 → 加 --allow-missing-aligner。"
    fi

    # pyannote（可选：缺了只是 diarize / speaker.embed 不可用）
    PY_MISSING=""
    while IFS='|' read -r _repo folder files; do
        [ -n "${_repo:-}" ] || continue
        IFS=',' read -r -a _fl <<< "$files"
        for f in "${_fl[@]}"; do
            [ -s "$PYANNOTE_DIR/$folder/$f" ] || PY_MISSING="$PY_MISSING $folder/$f"
        done
    done <<< "$PYANNOTE_ASSETS"
    if [ -n "$PY_MISSING" ]; then
        warn "pyannote 不完整（缺：$PY_MISSING）"
        warn "  → /v1/diarize 与 /v1/speaker/embed 会报 model_failed（如实失败，不假装成功）"
    else
        ok "说话人分离（pyannote 三件套）齐全"
    fi

    # SenseVoice（可选，但**老卡上它是推荐的转写引擎**：Pascal/Volta/Turing 没有 bf16）
    SENSEVOICE_OK=0
    if [ -n "$(find "$SENSEVOICE_DIR" -maxdepth 3 -type f -name 'model.*' -size +1k 2>/dev/null | head -1)" ]; then
        SENSEVOICE_OK=1
        ok "SenseVoice 在位（$SENSEVOICE_DIR）"
    elif [ "$OLD_CARD" = 1 ] || [ "$NO_BF16" = 1 ]; then
        warn "老卡（sm_${GPU_CC:-?}）**建议用 SenseVoice**，但模型卷里没有它：$SENSEVOICE_DIR"
        warn "  （容器里 ModelScope 缓存不在卷上，所以必须显式放进模型卷）"
        warn "  没有它就只能试 qwen3asr，而那条路是按 bf16 加载的 —— 见文档「按显卡选规格」"
    fi

    if [ "$ASR_OK" != 1 ] || { [ "$ALIGNER_OK" != 1 ] && [ "$ALLOW_MISSING_ALIGNER" != 1 ]; }; then
        if [ "$OLD_CARD" = 1 ] && [ "$SENSEVOICE_OK" = 1 ]; then
            # 老卡的正解本来就可能是"只跑 SenseVoice"，所以这不是致命错误 —— 但必须说清
            # 下一步：specs 得跟着换，否则起容器之后 asr 照样 failed。
            warn "老卡 + SenseVoice 在位：qwen3asr/强制对齐器缺失只算警告。"
            warn "  但记得把配置里的 models.specs 换成 impl: sensevoice（模板见文档「按显卡选规格」），"
            warn "  否则服务端仍然按 qwen3asr 加载 → asr 一路 model_failed。"
            warn "  最快的一条：重跑本脚本时加 --asr-impl sensevoice（它会把那份 specs 写进生成的配置）。"
        elif [ "$DRY_RUN" = 1 ]; then
            warn "模型卷有问题（dry-run 继续；真跑会在这里以退出码 3 停下）"
        else
            bad "模型卷有问题 —— 停在这里（带着半个模型卷起容器只会得到一堆 model_failed）"
            exit 3
        fi
    fi

    # ---- "允许缺"的两条路线：把 qwen3asr/对齐器那几条 [错误] 从**退出码**里摘出去 ----
    # 只在下面两种情况成立时降级（它们都是文档认可的既定路线）：
    #   ① 老卡（sm_<7.5）+ SenseVoice 在位 → 转写走 SenseVoice（文档 10.4 B）；
    #   ② 显式给了 --allow-missing-aligner → 接受没有句级时间戳（文档 2.3）。
    # 不降级时它们仍然是错误（上面的 exit 3 或汇总里的非 0 退出码）。
    DOWNGRADE_OK=0
    if [ "$MODEL_ERRS" != 0 ]; then
        if [ "$OLD_CARD" = 1 ] && [ "$SENSEVOICE_OK" = 1 ]; then
            DOWNGRADE_OK=1
        elif [ "$ALIGNER_OK" != 1 ] && [ "$ALLOW_MISSING_ALIGNER" = 1 ]; then
            DOWNGRADE_OK=1
        fi
        if [ "$DOWNGRADE_OK" = 1 ]; then
            ERRORS=$((ERRORS - MODEL_ERRS))
            WARNINGS=$((WARNINGS + MODEL_ERRS))
            warn "上面 $MODEL_ERRS 条 [错误] 已**降级为警告**（不计入退出码）—— 走的是："
            if [ "$OLD_CARD" = 1 ] && [ "$SENSEVOICE_OK" = 1 ]; then
                warn "  ① 老卡（sm_${GPU_CC:-?}）+ SenseVoice 在位 → 转写引擎用 SenseVoice。"
            fi
            if [ "$ALLOW_MISSING_ALIGNER" = 1 ]; then
                warn "  ② 你给了 --allow-missing-aligner → 接受没有句级时间戳。"
            fi
            warn "  它们说的仍然是**实话**（那两份权重确实不在）—— 降级的只是退出码，不是事实。"
        fi
    fi
fi

if [ "$MODELS_ONLY" = 1 ]; then
    step "完成（--models-only）"
    printf '模型卷就绪：%s\n' "$MODELS_DIR"
    exit 0
fi

# =====================================================================
# 4. 状态卷 + 密钥
# =====================================================================
step "4/9 状态卷与密钥（$STATE_DIR、$ENV_FILE）"
run mkdir -p "$STATE_DIR"

env_upsert() { # KEY VALUE   有同名键就替换、没有就追加。**从不回显 VALUE**
    local key="$1" value="$2" tmp="$ENV_FILE.tmp.$$"
    if [ "$DRY_RUN" = 1 ]; then
        printf '  %s(dry-run)%s %s=<%s 字符> 写进 %s\n' "$C_DIM" "$C_OFF" "$key" \
            "${#value}" "$ENV_FILE"
        return 0
    fi
    mkdir -p "$(dirname "$ENV_FILE")" || return 1
    touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
    grep -v "^${key}=" "$ENV_FILE" >"$tmp" 2>/dev/null || true
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
    mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
}

existing_secret=""
if [ -f "$ENV_FILE" ]; then
    existing_secret="$(sed -n 's/^ECHO_JWT_SECRET=//p' "$ENV_FILE" | head -1)"
fi
if [ -n "$existing_secret" ]; then
    ok "沿用 $ENV_FILE 里已有的 ECHO_JWT_SECRET（**绝不重新生成**：换了它所有客户端立刻 401）"
elif need_cmd openssl; then
    env_upsert ECHO_JWT_SECRET "$(openssl rand -hex 32)"
    ok "已生成 ECHO_JWT_SECRET（32 字节随机 → $ENV_FILE，权限 600）"
elif need_cmd python3; then
    env_upsert ECHO_JWT_SECRET "$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
    ok "已生成 ECHO_JWT_SECRET（python3 secrets → $ENV_FILE，权限 600）"
else
    die "要生成 ECHO_JWT_SECRET，但既没有 openssl 也没有 python3 —— 装一个（apt-get install -y openssl）"
fi

if [ "$AUTH_ENABLED" = 1 ]; then
    env_upsert ECHO_AUTH_ENABLED true
    env_upsert ECHO_AUTH_MODE jwt
    ok "已把 ECHO_AUTH_ENABLED=true / ECHO_AUTH_MODE=jwt 写进 env（容器侧接线在第 6 步）"
else
    warn "--no-auth：**不打开鉴权**。任何能连到 8900 的人都能用这块 GPU。"
    warn "  只允许在隔离实验网上这么跑；一旦别人也能连，去掉 --no-auth 重跑。"
fi

if [ "$TLS_MODE" = "self-signed" ]; then
    env_upsert ECHO_HEALTH_SCHEME https
else
    env_upsert ECHO_HEALTH_SCHEME http
fi

# =====================================================================
# 5. TLS
# =====================================================================
step "5/9 TLS（模式：$TLS_MODE）"
CERT_IN_CONTAINER=/etc/echo/tls/server.crt
KEY_IN_CONTAINER=/etc/echo/tls/server.key
CONF_NEEDED=0
TLS_CERT_YAML=""
TLS_KEY_YAML=""

if [ "$TLS_MODE" = "self-signed" ]; then
    if [ -z "$SERVER_HOST" ]; then
        SERVER_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
        if [ -z "$SERVER_HOST" ]; then SERVER_HOST="$(hostname -f 2>/dev/null || hostname 2>/dev/null)"; fi
        [ -n "$SERVER_HOST" ] || die "探测不到本机地址 —— 请用 --server-host <客户端要连的地址>"
        log "自动探测到 --server-host $SERVER_HOST（不对就用 --server-host 指定）"
    fi
    if [ -s "$TLS_DIR/server.crt" ] && [ -s "$TLS_DIR/server.key" ]; then
        ok "沿用现有证书 $TLS_DIR/server.crt（要换就自己删掉这两个文件再跑）"
    else
        case "$SERVER_HOST" in
            [0-9]*.[0-9]*.[0-9]*.[0-9]*) san="IP:$SERVER_HOST" ;;
            *) san="DNS:$SERVER_HOST" ;;
        esac
        run mkdir -p "$TLS_DIR"
        if run openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
                -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" \
                -subj "/CN=$SERVER_HOST" -addext "subjectAltName=$san"; then
            run chmod 600 "$TLS_DIR/server.key"
            ok "自签证书：$TLS_DIR/server.crt（CN=$SERVER_HOST，SAN=$san，825 天）"
            log "客户端**不需要**装自签根：配对串里带证书指纹（fp=），客户端按指纹固定证书"
        else
            die "openssl 生成证书失败（原始报错在上面）—— 也可以改用 --tls proxy（前面放 TLS 终结）"
        fi
    fi
    CONF_NEEDED=1
    TLS_CERT_YAML="$CERT_IN_CONTAINER"
    TLS_KEY_YAML="$KEY_IN_CONTAINER"
elif [ "$TLS_MODE" = "proxy" ]; then
    log "前面放 TLS 终结：容器里**保持明文 http** —— server.tls.certfile/keyfile 两个都留空"
    log "  只填一个服务端会**启动就报错**（刻意不静默降级成 http —— 那会让你以为连的是 https）"
    log "  反代要把 X-Forwarded-For 原样转过来（审计与登录退避按它识别来源，见 admin._source_of）"
elif [ "$TLS_MODE" = "none" ]; then
    warn "--tls none：能力面明文 http。只适合隔离实验网，或前面已有别的加密层。"
else
    die "不认识的 --tls 值：$TLS_MODE（可选 self-signed | proxy | none）"
fi

[ -n "$VRAM_BUDGET_MB" ] && CONF_NEEDED=1

# --- 转写引擎（--asr-impl）：把 models.specs 写进生成的配置 ---
# 为什么脚本得管这件事：老卡（Pascal/Volta，没有 bf16）**必须**把 asr-long 的 impl 从
# 默认的 qwen3asr 换成 sensevoice（文档 10.3 / 10.4 B），否则服务端照旧去加载 qwen3asr →
# asr 一路 model_failed。而配置文件是**这个脚本每次重跑都会重写**的，
# 所以"照着文档手工改一行"在第二次跑脚本时就会被抹掉。
# ⚠️ `models.specs` 是**整列表替换**（server/settings.py:Config.specs），
#    只写 asr-long 会把 diarize / speaker-embed 一起抹掉 —— 所以三档一起写。
SPECS_YAML=""
if [ -n "$ASR_IMPL" ]; then
    CONF_NEEDED=1
    case "$ASR_IMPL" in
        sensevoice)
            _asr_est="${ASR_EST_VRAM_MB:-1200}"
            _asr_mv="sensevoice-small"
            _asr_sup="[asr.text]"
            _asr_note="SenseVoice 不给句级时间戳 → 这里刻意**没有** asr.timestamps"
            ;;
        qwen3asr)
            _asr_est="${ASR_EST_VRAM_MB:-4700}"
            _asr_mv="qwen3-asr-0.6b"
            _asr_sup="[asr.text, asr.timestamps]"
            _asr_note="qwen3asr + 强制对齐器（老卡没有 bf16，这条路只该给 sm_80+ 用）"
            ;;
        *)
            die "--asr-impl 只认识 sensevoice | qwen3asr（给的是：$ASR_IMPL）"
            ;;
    esac
    log "配置里会写 models.specs：asr-long.impl=$ASR_IMPL、est_vram_mb=$_asr_est（$_asr_note）"
    SPECS_YAML="$(printf '%s\n' \
        "models:" \
        "  # 这一段由 scripts/prepare-backend.sh --asr-impl 生成。**它是整列表替换**" \
        "  # server/engines.py:default_specs 的默认清单 —— 所以该写的档都要写在这里。" \
        "  device: cuda" \
        "  specs:" \
        "    - id: asr-long" \
        "      slot: asr.long" \
        "      impl: $ASR_IMPL" \
        "      resident: false            # 8 GB 卡上不要常驻（它会让 diarize 加载失败）" \
        "      max_concurrency: 1" \
        "      est_vram_mb: $_asr_est       # 先用占位数起服务，跑一段真音频后按 /v1/health 的 vram.usedMb 校准" \
        "      modelVersion: $_asr_mv" \
        "      supports: $_asr_sup")"
    if [ "$ASR_IMPL" = "sensevoice" ]; then
        # 老卡上**不写** diarize / speaker-embed：镜像里根本没有 pyannote
        # （pyannote.audio 4.x 要 torch>=2.8，而没有带 Pascal 的 torch>=2.8；
        #  3.x 又不认 diarize.py 传的 `plda=`）。写进去 = 宣告一个永远 model_failed 的槽，
        # 与"缺对齐器却宣告 asr.timestamps"是同一种假话。所以只留一段说明当注释。
        SPECS_TAIL="  # ⚠️ 老卡（SenseVoice 那一档）**刻意没有 diarize / speaker-embed**：镜像里
  #   没有 pyannote.audio —— 4.x 要求 torch>=2.8，而带 Pascal(sm_61) 的 torch 最高只到
  #   2.7.1+cu118；3.x 又没有 app/audio/diarize.py 要用的 plda= 形参（2026-09-28 实测）。
  #   所以 /v1/diarize 会干净地报「没有这个槽」，而不是先宣告再 model_failed。
  #   哪天真有能配 torch 2.7 的 pyannote，把这两档按 server/echo-server.example.yaml
  #   的形状加回来即可。"
    else
        SPECS_TAIL="$(printf '%s\n' \
            "    - id: diarize" \
            "      slot: diarize.turns" \
            "      impl: pyannote" \
            "      resident: false" \
            "      max_concurrency: 1" \
            "      est_vram_mb: 2600          # 占位估算（与 default_specs 一致）" \
            "      modelVersion: pyannote-3.1-wespeaker-v1" \
            "      vectorSpaceId: ws-resnet34-v1" \
            "      dim: 256" \
            "      supports: [diarize.embeddings]" \
            "    - id: speaker-embed" \
            "      slot: speaker.embed" \
            "      impl: pyannote-embed" \
            "      resident: false" \
            "      max_concurrency: 1" \
            "      est_vram_mb: 0" \
            "      modelVersion: wespeaker-voxceleb-resnet34-LM-v1" \
            "      vectorSpaceId: ws-resnet34-v1   # **必须**与 diarize 相同（向量才能互相比对）" \
            "      dim: 256")"
    fi
    SPECS_YAML="$SPECS_YAML
$SPECS_TAIL"
fi

# =====================================================================
# 6. compose override（配置挂载 / 鉴权接线）
# =====================================================================
step "6/9 生成 compose override（$OVERRIDE_YML）"

CONFIG_IN_CONTAINER=/etc/echo/server.yaml
if [ "$CONF_NEEDED" = 1 ]; then
    # ⚠️ 这个 heredoc 的分隔符**没有加引号** —— 所以正文里的 $VAR 会被展开（比如下面两个
    #    路径与显存预算）。**正文里不许出现反引号或 $(...)**：它们会被当成命令替换真的执行
    #    （第一版就在注释里写了 `--config`，于是这里报了一句莫名其妙的
    #    "--config: command not found"，注释也缺了一块）。要写这种记号就直接写，别加反引号。
    write_file_if_changed "$SERVER_YAML" 644 <<EOF
# 由 scripts/prepare-backend.sh 生成 —— **只写"必须按这台机器定"的项**。
# 其余全部仍走内置默认值 + 环境变量。--config 的优先级是：
#   DEFAULTS < 这份文件 < 环境变量（见 server/settings.py:load）。
server:
  # 两个都填才启用 https；只填一个**启动就报错**（刻意不静默降级成 http）。
  tls:
    certfile: "${TLS_CERT_YAML}"
    keyfile: "${TLS_KEY_YAML}"
  # 显存预算：按卡校准。0 = 不限。8 GB 卡建议 7000（桌面/显示还要留 ~1 GB）。
  # 怎么按自己的卡算见 docs/后端容器部署.md 第 9 节；torch 源与 bf16 见第 10 节。
  vram_budget_mb: ${VRAM_BUDGET_MB:-0}
${SPECS_YAML}
EOF
    if [ "$DRY_RUN" != 1 ]; then ok "配置：$SERVER_YAML"; fi
fi

OVERRIDE_NEEDED=0
[ "$CONF_NEEDED" = 1 ] && OVERRIDE_NEEDED=1
[ "$AUTH_ENABLED" = 1 ] && OVERRIDE_NEEDED=1
[ "$EXTRA" = 1 ] && OVERRIDE_NEEDED=1
[ -n "$TORCH_INDEX" ] && OVERRIDE_NEEDED=1
[ -n "$TORCH_VERSION" ] && OVERRIDE_NEEDED=1
[ -n "$PIP_INDEX" ] && OVERRIDE_NEEDED=1

if [ "$OVERRIDE_NEEDED" = 0 ]; then
    log "override 里没有要改的东西（没 TLS、没 vram 预算、也没开鉴权）—— 不生成它"
else
    {
        printf '%s\n' \
            "# 由 scripts/prepare-backend.sh 生成。**全部用绝对路径** ——" \
            "# 多份 -f 时相对路径的解析基准容易搞错，这里不给它机会。" \
            "services:" \
            "  backend:"
        # ---- 构建期参数（老卡的 CUDA 源就写在这里）----
        if [ "$EXTRA" = 1 ] || [ -n "$TORCH_INDEX" ] || [ -n "$TORCH_VERSION" ] || [ -n "$PIP_INDEX" ]; then
            printf '%s\n' "    build:" "      args:"
            [ "$EXTRA" = 1 ] && printf '%s\n' \
                "        ECHO_EXTRA: \"1\"                # 装上 torch 那组（不装则模型一律 model_failed）"
            [ -n "$TORCH_INDEX" ] && printf '%s\n' \
                "        ECHO_TORCH_INDEX: \"$TORCH_INDEX\""
            [ -n "$TORCH_VERSION" ] && printf '%s\n' \
                "        ECHO_TORCH_VERSION: \"$TORCH_VERSION\""
            # 普通依赖的 pip 源（**不是** torch 的源；见 Dockerfile 里那段注释）
            [ -n "$PIP_INDEX" ] && printf '%s\n' \
                "        ECHO_PIP_INDEX: \"$PIP_INDEX\""
        fi
        if [ "$CONF_NEEDED" = 1 ]; then
            printf '%s\n' \
                "    command: [\"python\", \"-m\", \"server.main\", \"--config\", \"$CONFIG_IN_CONTAINER\"]"
        fi
        if [ "$AUTH_ENABLED" = 1 ]; then
            printf '%s\n' \
                "    environment:" \
                "      # :? = 变量没传进来时 compose **直接报错退出**（而不是静默起一个不鉴权的服务）" \
                "      ECHO_AUTH_ENABLED: \"\${ECHO_AUTH_ENABLED:?没配 —— 用 --env-file $ENV_FILE}\"" \
                "      ECHO_AUTH_MODE: \"\${ECHO_AUTH_MODE:?没配 —— 见 $ENV_FILE}\"" \
                "      ECHO_JWT_SECRET: \"\${ECHO_JWT_SECRET:?没配 —— 跑 scripts/prepare-backend.sh 生成 $ENV_FILE}\""
        fi
        if [ "$CONF_NEEDED" = 1 ] || [ "$TLS_MODE" = "self-signed" ]; then
            printf '%s\n' \
                "    volumes:" \
                "      - $SERVER_YAML:$CONFIG_IN_CONTAINER:ro"
            if [ "$TLS_MODE" = "self-signed" ]; then
                printf '%s\n' \
                    "      - $TLS_DIR/server.crt:$CERT_IN_CONTAINER:ro" \
                    "      - $TLS_DIR/server.key:$KEY_IN_CONTAINER:ro"
            fi
        fi
    } | write_file_if_changed "$OVERRIDE_YML" 644
    if [ "$DRY_RUN" != 1 ]; then ok "override：$OVERRIDE_YML"; fi
fi

# =====================================================================
# 7. 起容器
# =====================================================================
step "7/9 起容器"
COMPOSE=($COMPOSE_BIN --env-file "$ENV_FILE" -f "$REPO_ROOT/$COMPOSE_FILE")
if [ "$OVERRIDE_NEEDED" = 1 ]; then
    COMPOSE+=(-f "$OVERRIDE_YML")
fi
log "命令：$(printf '%q ' "${COMPOSE[@]}")up -d --build"
log "（构建上下文是仓库根；ECHO_EXTRA / torch 的 CUDA 源都在 build.args 里）"
if [ -n "$TORCH_INDEX" ]; then
    log "torch 源：$TORCH_INDEX${TORCH_VERSION:+（钉 torch==$TORCH_VERSION）}"
fi
if [ -n "$PIP_INDEX" ]; then
    log "普通依赖的 pip 源：$PIP_INDEX（**torch 不走它**）"
fi

if [ "$EXTRA" != 1 ] && grep -q 'ECHO_EXTRA: "0"' "$REPO_ROOT/$COMPOSE_FILE" 2>/dev/null; then
    warn "compose 里 ECHO_EXTRA=\"0\"（你这轮也没给 --extra）—— 镜像**不含 torch/funasr/qwen-asr**，"
    warn "  转写与说话人分离都会 model_failed（那是如实失败，不是 bug）。"
    warn "  要真跑模型：加 --extra 重跑，或把 $COMPOSE_FILE 里那行改成 \"1\"（镜像大 3~5 GB、构建更久）。"
fi

if [ "$DO_UP" = 0 ]; then
    log "--no-up：不启动容器"
elif [ "$DRY_RUN" = 1 ]; then
    printf '  %s(dry-run)%s' "$C_DIM" "$C_OFF"; printf ' %q' "${COMPOSE[@]}"; printf ' up -d --build\n'
elif "${COMPOSE[@]}" up -d --build; then
    ok "容器已启动：$CONTAINER"
    log "等 8 秒再看健康状态与日志…"
    sleep 8
    printf '  %s--- docker compose logs --tail=40 %s ---%s\n' "$C_DIM" "$CONTAINER" "$C_OFF"
    "${COMPOSE[@]}" logs --tail=40 "$CONTAINER" 2>&1 | sed 's/^/    /' || true
    printf '  %s----------------------------------------%s\n' "$C_DIM" "$C_OFF"
    log "健康状态：$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)（start_period 60s 内是 starting，正常）"
    log "上面那两句告警是**预期**的：「管理面监听在 0.0.0.0:8901（不是回环）」——"
    log "  绑定地址是通配，但宿主只把它发布到回环（127.0.0.1:8901），所以别去消掉它。"
else
    bad "docker compose up 失败 —— 原始报错在上面。最常见三个原因（含看哪一行）："
    bad "  ① 宿主没装 nvidia-container-toolkit → 'could not select device driver ... [[gpu]]'"
    bad "  ② 端口被占：ss -ltnp | grep -E '8900|8901'（或改 compose 里的映射）"
    bad "  ③ 构建失败（网络/磁盘）→ 单独跑一次 build 看完整输出"
    bad "  详见 docs/后端容器部署.md 第 8 节"
    exit 2
fi

# =====================================================================
# 8. 建管理员
# =====================================================================
step "8/9 管理员账号（**只能命令行建**，管理面刻意不开账号管理）"
if [ "$DO_UP" = 0 ]; then
    log "--no-up：跳过（容器起来后再跑这一步）"
elif [ "$DRY_RUN" = 1 ]; then
    printf '  %s(dry-run)%s docker exec %s python -m server.main --list-admins\n' "$C_DIM" "$C_OFF" "$CONTAINER"
    printf '  %s(dry-run)%s 若还没有账号 → docker exec %s python -m server.main --new-admin %s\n' \
        "$C_DIM" "$C_OFF" "$CONTAINER" "$ADMIN_NAME"
else
    admins_out="$(docker exec "$CONTAINER" python -m server.main --list-admins 2>&1)" || true
    if printf '%s' "$admins_out" | grep -q '还没有管理员账号'; then
        log "还没有管理员账号 → 建一个（**口令只出现这一次**，当场存好）"
        if docker exec "$CONTAINER" python -m server.main --new-admin "$ADMIN_NAME"; then
            ok "管理员 $ADMIN_NAME 已建（口令在上面的输出里，只显示这一次）"
        else
            bad "--new-admin 失败（原始报错在上面）—— 管理面登录会一直失败"
        fi
    elif [ -n "$admins_out" ]; then
        ok "已有管理员账号 → **跳过** --new-admin（那条命令会重置同名账号的口令）"
        printf '%s\n' "$admins_out" | sed 's/^/    /'
        log "确实要重置口令：docker exec $CONTAINER python -m server.main --new-admin <名字>"
    else
        bad "读不到管理员清单（docker exec 没有输出）—— 容器起来了吗：docker ps -a"
    fi
fi

# =====================================================================
# 9. 验收
# =====================================================================
step "9/9 验收"
SCHEME=http
[ "$TLS_MODE" = "self-signed" ] && SCHEME=https
BASE_URL="$SCHEME://127.0.0.1:8900"
SMOKE_FAILED=0

http_get() { # url → stdout
    if need_cmd curl; then
        if [ "$SCHEME" = https ]; then curl -sk "$1"; else curl -s "$1"; fi
    elif need_cmd python3; then
        URL="$1" python3 - <<'PY'
import os, ssl, urllib.request
ctx = ssl._create_unverified_context()
print(urllib.request.urlopen(os.environ["URL"], timeout=8, context=ctx).read().decode())
PY
    else
        return 127
    fi
}

if [ "$DO_SMOKE" = 1 ] && [ "$DO_UP" = 1 ] && [ "$DRY_RUN" != 1 ]; then
    log "GET $BASE_URL/v1/health（免鉴权探针）"
    if body="$(http_get "$BASE_URL/v1/health")"; then
        if need_cmd python3; then
            printf '%s\n' "$body" | python3 -m json.tool 2>/dev/null | sed 's/^/    /' || \
                printf '%s\n' "$body" | sed 's/^/    /'
            if BODY="$body" python3 -c '
import json, os, sys
h = json.loads(os.environ["BODY"])
bad = []
if h.get("ok") is not True:
    bad.append("ok 不是 true")
if not isinstance(h.get("busy"), dict):
    bad.append("缺 busy 快照")
v = h.get("vram") or {}
if "budgetMb" not in v or "usedMb" not in v:
    bad.append("缺 vram.budgetMb/usedMb")
t = h.get("tmp") or {}
if t.get("files") != 0 or t.get("bytes") != 0:
    bad.append("空转时 tmp 不为 0（%s 文件 / %s 字节）= 泄漏信号" % (t.get("files"), t.get("bytes")))
print("health 期望值检查：%s（uptime=%ss, vram=%s, models=%s）"
      % ("通过" if not bad else "不符合预期 -> " + "; ".join(bad),
         h.get("uptimeSeconds"), v, h.get("models")))
sys.exit(0 if not bad else 1)
'; then
                ok "/v1/health 期望值符合（空转时 usedMb=0、tmp 归零都是正常的）"
            else
                warn "/v1/health 的期望值有偏差（见上面那行）—— 多数情况是临时文件没清干净"
            fi
        else
            printf '%s\n' "$body" | sed 's/^/    /'
            warn "宿主没有 python3 → 不自动核对期望值，请自己看上面的 JSON（见文档第 7 节的表）"
        fi
    else
        bad "连不上 $BASE_URL/v1/health"
        bad "  → 容器起来了吗：docker ps -a | grep $CONTAINER"
        bad "  → 端口发布对吗：docker port $CONTAINER"
    fi

    log "冒烟自测（--skip-inference：只验查询端点 + 拒绝路径 + 临时文件归零）"
    if need_cmd python3; then
        SMOKE=(python3 "$REPO_ROOT/scripts/smoke-echo-backend.py" --base-url "$BASE_URL" --skip-inference)
        [ "$SCHEME" = https ] && SMOKE+=(--insecure)
        # ⚠️ **开了鉴权就必须给凭据**（2026-09-28 真机实测）：不开鉴权时拒绝路径的探针
        # 验的是"这些错误码分得清"；开了鉴权而不给凭据，它们**全都**会拿到 401，
        # 于是这个脚本在自己的默认配置（auth 默认是开的）下永远报"验收没过"（退出码 4）。
        # 所以这里现发一张一次性配对码喂给它（`--pair-code` 会走完配对→换令牌→带令牌调用，
        # 顺便把鉴权链路也验了）。代价：每跑一次会多一个名为 smoke-test 的客户端。
        if [ "$AUTH_ENABLED" = 1 ] && [ "$DO_UP" = 1 ]; then
            _code="$(docker exec "$CONTAINER" python -m server.main \
                        --config "$CONFIG_IN_CONTAINER" --new-pairing-code \
                        --created-by prepare-backend 2>/dev/null \
                     | grep -oE 'code=[A-Za-z0-9_-]+' | head -1 | cut -d= -f2)"
            if [ -n "$_code" ]; then
                log "给冒烟发了一张一次性配对码（拒绝路径要带凭据才有意义）"
                SMOKE+=(--pair-code "$_code")
            else
                warn "发不出配对码（容器里的 CLI 没给码）—— 拒绝路径会在无凭据下全报 401，"
                warn "  那**不**代表服务端坏了；要看真实结果就跑完整版冒烟并自己给 --token/--pair-code"
            fi
        fi
        if "${SMOKE[@]}"; then
            ok "冒烟通过"
        else
            bad "冒烟有不符合预期的项（上面每项都打了 PASS/FAIL 与实测值）"
            SMOKE_FAILED=1
        fi
    else
        warn "宿主没有 python3 → 跳过冒烟。手动跑："
        warn "  python3 scripts/smoke-echo-backend.py --base-url $BASE_URL --skip-inference"
    fi
    printf '%s注意：--skip-inference **不加载模型**。要证模型真能用，跑完整版：%s\n' "$C_DIM" "$C_OFF"
    printf '    python3 scripts/smoke-echo-backend.py --base-url %s --wav <一段16k音频>\n' "$BASE_URL"
else
    log "跳过 HTTP 验收（--dry-run / --no-up / --skip-smoke 之一）"
fi

# =====================================================================
# 汇总
# =====================================================================
step "汇总"
if [ "$DO_UP" = 1 ] && [ "$DRY_RUN" != 1 ]; then
    printf '管理面（**只发布在宿主回环**）：http://127.0.0.1:8901/admin/\n'
    printf '远程运维更稳的姿势：ssh -L 8901:127.0.0.1:8901 <gpu-host>，再在本地浏览器开上面那个地址\n'
fi
printf '起容器（下次直接抄）：\n    %sup -d --build\n' "$(printf '%q ' "${COMPOSE[@]}")"
printf '停掉（卷留着，客户端凭据与审计都在里面）：\n    %sdown\n' "$(printf '%q ' "${COMPOSE[@]}")"
printf '日志：\n    %slogs --tail=200 %s\n' "$(printf '%q ' "${COMPOSE[@]}")" "$CONTAINER"

if [ "$WARNINGS" != 0 ]; then
    printf '\n%s有 %s 条警告（上面逐条 [警告]）—— 请自己判断是否接受%s\n' "$C_YEL" "$WARNINGS" "$C_OFF"
fi
if [ "$SMOKE_FAILED" = 1 ]; then
    printf '\n%s验收没过（冒烟有 FAIL 项）%s\n' "$C_RED" "$C_OFF"
    exit 4
fi
if [ "$ERRORS" != 0 ]; then
    printf '\n%s有 %s 处错误%s\n' "$C_RED" "$ERRORS" "$C_OFF"
    exit 1
fi
printf '\n%s准备完成%s（容器侧行为仍需真机确认：见 docs/后端容器部署.md 第 12 节）\n' "$C_GRN" "$C_OFF"
exit 0
