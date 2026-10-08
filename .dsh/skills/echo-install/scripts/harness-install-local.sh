#!/usr/bin/env bash
# =====================================================================
# harness-install-local.sh - 把 DSH 标准版装到 <安装目录>/harness/dsh（本地永久入口）
#
# 谁在用它：`echo-install-components.sh` 的 prepare_agent；也可以单独跑（修装坏的标准版）。
# 与 Windows 的 harness-install-local.ps1 **一一对应**（两条路都要同样行为）。
#
# 为什么需要它（2026-09-22 实测，见 AGENTS.md）：
#   * `npx -y @deepseek-ai/dsh web` 冷启动 **2 分 10 秒**，直连本地 bin.js 只要 **9 秒**；
#   * 而 `npm install @deepseek-ai/dsh@0.1.5-rc.2` **曾经**在公共 registry 上装不下来：
#     它的依赖图里有个子包被写成 ^0.1.5-rc.3，而那个子包的 rc.3 当时从没发布过 ->
#     `ETARGET No matching version found for ...documentpreview@^0.1.5-rc.3`。
#     **2026-09-22 晚同事复测：rc.3 系列已发布，npm 这条路现在是通的** —— 所以第 ③ 条
#     目前用不上，但保留：registry 上这种依赖图事故会复发，而 npx 慢是必然的。
#     老脚本当初遇到装不上只会回退 npx，于是用户那边每次冷启动都慢两分钟。
#
# 三条路依次试：
#   1) 已经装好（bin.js 非空）-> 直接用；
#   2) npm install @deepseek-ai/dsh@<版本>（--from-cache 时跳过）；
#   3) **从 npx 缓存复制**同版本那份整树 —— 缓存里没有时先用 npx 把缓存填上再复制
#      （全新机器的 _npx 缓存是空的）；版本不一致会明确告警，仍可用。
#
# 成功：最后一行 stdout 打印 `HARNESS_COMMAND=<node 全路径> <bin.js 全路径> web`；
#       传了 --command-file 就同时写进那个文件。
# 失败：退出码 1，并说清下一步（调用方据此回退 npx）。
#
# 约定：macOS 自带 bash 3.2 —— 不许用 mapfile/readarray/&>>/${x^^}；文件必须 LF 行尾。
# =====================================================================
set -u

DEST=""
# 留空 = 自动取 npm 上的 latest（2026-10-08 用户要求：装最新版，别写死旧版本）。
VERSION=""
UPGRADE=0
FROM_CACHE=0
CACHE_DIR=""
NODE_BIN=""
COMMAND_FILE=""
LOG_FILE=""

usage() {
  cat <<'EOF'
用法：harness-install-local.sh --dest <ECHO 安装目录> [选项]
  --dest <目录>        必填。ECHO 安装目录（标准版装到 <目录>/harness/dsh）
  --version <版本>     @deepseek-ai/dsh 版本；**默认自动取 npm 上的 latest**
  --upgrade            已装好也强制重装（= 升级入口；配合 --version 升/降到指定版本）
  --from-cache         跳过 npm，直接从 npx 缓存复制（离线 / registry 坏掉时用）
  --cache-dir <目录>   显式指定 npx 缓存根（默认问 `npm config get cache`）
  --node <路径>        显式指定 node（默认自己找）
  --command-file <路径> 成功后把启动命令写进这个文件
  --log-file <路径>    每一步同时追加到这个日志（安装器的 install-*.log）
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dest)         DEST="$2"; shift 2 ;;
    --version)      VERSION="$2"; shift 2 ;;
    --upgrade)      UPGRADE=1; shift ;;
    --from-cache)   FROM_CACHE=1; shift ;;
    --cache-dir)    CACHE_DIR="$2"; shift 2 ;;
    --node)         NODE_BIN="$2"; shift 2 ;;
    --command-file) COMMAND_FILE="$2"; shift 2 ;;
    --log-file)     LOG_FILE="$2"; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$DEST" ]; then
  echo "--dest 必填" >&2
  usage >&2
  exit 2
fi
if [ ! -d "$DEST" ]; then
  echo "安装目录不存在：$DEST" >&2
  exit 2
fi
DEST="$(cd "$DEST" && pwd)"

_log() {
  [ -n "$LOG_FILE" ] || return 0
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >> "$LOG_FILE" 2>/dev/null || true
}
say()  { echo "  $*"; _log "  $*"; }
ok()   { echo "  [ok]   $*"; _log "[ok]   $*"; }
warn() { echo "  [warn] $*"; _log "[warn] $*"; }
err()  { echo "  [fail] $*" >&2; _log "[fail] $*"; }

#: 查不到 npm registry 时的兜底版本（只是兜底，不是"我们要装的版本"）。
#  全新机器上 npm 可能还没配好源 / 断网，这时不能因为"查不到最新版"就整个失败。
FALLBACK_VERSION="0.2.0-rc.2"

# 取 npm 上 @deepseek-ai/dsh 的 dist-tags.latest。
# 为什么认 dist-tag 不自己排 semver：预发布号（rc/alpha）的排序很绕，而且
# "哪个是给用户的"是仓库方的判断（dist-tag），不该由我们猜。查不到返回空串。
resolve_latest_version() {
  command -v npm >/dev/null 2>&1 || return 0
  local v
  v="$(npm view @deepseek-ai/dsh dist-tags.latest 2>/dev/null | head -1 | tr -d '"\r[:space:]')"
  case "$v" in
    [0-9]*.[0-9]*.[0-9]*) printf '%s' "$v" ;;
    *) : ;;
  esac
}

#: 把版本号排成可比较的数字串（只用于"有没有更新版"的提示，不做依赖解析）。
#  正式版给 stage=9、预发布按 alpha<beta<rc<其它 排，序号补零对齐后按字典序比。
version_rank() {
  local v="$1" major minor patch stage pre tag
  major="$(printf '%s' "$v" | sed -n 's/^\([0-9]*\)\..*/\1/p')"
  minor="$(printf '%s' "$v" | sed -n 's/^[0-9]*\.\([0-9]*\)\..*/\1/p')"
  patch="$(printf '%s' "$v" | sed -n 's/^[0-9]*\.[0-9]*\.\([0-9]*\).*/\1/p')"
  [ -n "$major" ] || major=0
  [ -n "$minor" ] || minor=0
  [ -n "$patch" ] || patch=0
  stage=9
  pre=0
  tag="$(printf '%s' "$v" | sed -n 's/^[0-9]*\.[0-9]*\.[0-9]*-\([A-Za-z]*\).*/\1/p' | tr 'A-Z' 'a-z')"
  if [ -n "$tag" ]; then
    case "$tag" in
      alpha) stage=0 ;;
      beta)  stage=1 ;;
      rc)    stage=2 ;;
      *)     stage=3 ;;
    esac
    pre="$(printf '%s' "$v" | sed -n 's/.*\.\([0-9]*\)$/\1/p')"
    [ -n "$pre" ] || pre=0
  fi
  printf '%03d.%03d.%03d.%d.%03d' "$major" "$minor" "$patch" "$stage" "$pre"
}

# 已装版本是否比目标旧（= 有新版可升）。
is_newer_version() {
  [ -n "$1" ] || return 1
  [ -n "$2" ] || return 0
  [ "$(version_rank "$1")" \> "$(version_rank "$2")" ]
}

resolve_node() {
  if [ -n "$NODE_BIN" ] && [ -x "$NODE_BIN" ]; then echo "$NODE_BIN"; return 0; fi
  if command -v node >/dev/null 2>&1; then command -v node; return 0; fi
  local d
  for d in /opt/homebrew/bin /usr/local/bin "$HOME/.volta/bin" "$HOME/.nvm/versions/node"/*/bin; do
    if [ -x "$d/node" ]; then echo "$d/node"; return 0; fi
  done
  echo ""
}

# 返回"不完整"的条目（每行一条）；空 = 完好
tree_broken() {
  local target="$1" pty="" out="" code=""
  if [ ! -s "$target/node_modules/@deepseek-ai/dsh/lib/bin.js" ]; then
    echo "lib/bin.js（缺或为空）"
  fi
  for pty in $(find "$target" -type d -name node-pty 2>/dev/null); do
    if [ ! -f "$pty/package.json" ] || [ ! -f "$pty/lib/index.js" ]; then
      echo "node-pty 不完整：$pty"
    fi
  done
  # **冒烟测试**：真把模块图加载一遍（`node bin.js --version`，跑完即退、不起服务）。
  # 为什么非要有它（2026-09-22 同事反馈 3.2，Windows 侧同步）：上面几条只验"文件在不在"，
  # 而 npm 安装被中途打断会留下**目录在、子目录整片没有**的残树 —— 实测 zod@4.6.5 装着、
  # package.json 也在，但整个 v4/ 缺失，报 `ERR_MODULE_NOT_FOUND: …zod/v4/classic/external.js`；
  # 上面几条**全过**，于是"已装好"快路径把坏树当好的用，之后每次启动都失败。
  if [ -s "$target/node_modules/@deepseek-ai/dsh/lib/bin.js" ]; then
    out="$("$NODE" "$target/node_modules/@deepseek-ai/dsh/lib/bin.js" --version 2>&1)"
    code=$?
    if [ "$code" -ne 0 ]; then
      echo "跑不起来（node bin.js --version 退出码 $code）：$(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
    fi
  fi
}

# 在 npx 缓存里找装好的 dsh 树：输出 "版本<TAB>node_modules 路径"，按 mtime 新到旧
cache_trees() {
  local roots="" r="" pkg="" ver="" nm="" ts=""
  if [ -n "$CACHE_DIR" ]; then
    roots="$CACHE_DIR"
  else
    if command -v npm >/dev/null 2>&1; then
      r="$(npm config get cache 2>/dev/null)"
      [ -n "$r" ] && roots="$r"
    fi
    [ -n "$roots" ] || roots="$HOME/.npm"
  fi
  for r in $roots "$HOME/.npm"; do
    [ -d "$r" ] || continue
    for pkg in "$r"/_npx/*/node_modules/@deepseek-ai/dsh/package.json; do
      [ -f "$pkg" ] || continue
      ver="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$pkg" | head -1)"
      nm="$(dirname "$(dirname "$(dirname "$pkg")")")"
      ts="$(stat -f '%m' "$pkg" 2>/dev/null || stat -c '%Y' "$pkg" 2>/dev/null || echo 0)"
      printf '%s\t%s\t%s\n' "$ts" "$ver" "$nm"
    done
  done | sort -rn | cut -f2-
}

# 全新机器上 _npx 缓存是**空的**（2026-09-22 同事实测：装之前刚清过缓存）→ 第 ③ 条无物可复制。
# 这里主动把缓存填一次：`npx --yes --package=<包> -- node --version` —— `--package` 会**先把包
# 装进 npx 自己的缓存**，然后跑一条必然立刻退出的命令。比"起一次 web 再杀掉"干净得多：
# 不用挑空闲端口（更不能占 43199），不用管进程回收。
#
# 能救 / 不能救：npm install **到目标目录**失败、但 npx 自建缓存能成（本地原因）时能救；
# registry 真坏的时候 npx 背后还是 npm，一样装不上 —— 那是"没有可用的下载源"，只能回退 npx 慢跑。
fill_cache() {
  local npx=""
  npx="$(command -v npx 2>/dev/null || true)"
  [ -n "$npx" ] || return 1
  say "  缓存是空的 —— 让 npx 先把这份装进它自己的缓存（要 1-2 分钟）..."
  "$npx" --yes "--package=@deepseek-ai/dsh@$VERSION" -- node --version 2>&1 | sed 's/^/      /' || true
  [ -n "$(cache_trees)" ]
}

# ---- 版本：默认取 npm 上的 latest（2026-10-08 用户要求：别写死旧版本）----
if [ -z "$VERSION" ]; then
  _latest="$(resolve_latest_version)"
  if [ -n "$_latest" ]; then
    VERSION="$_latest"
    say "npm 上的最新版（latest）：$VERSION"
  else
    VERSION="$FALLBACK_VERSION"
    warn "查不到 npm registry 上的最新版，退回内置版本 $VERSION"
    warn "  （想指定版本：--version <版本号>；想升级：--upgrade）"
  fi
fi

say "安装目录：$DEST"
say "标准版版本：$VERSION"

NODE="$(resolve_node)"
if [ -z "$NODE" ]; then
  err "没找到 node —— 标准版 harness 需要 Node.js（brew install node），装了再重跑"
  exit 1
fi
ok "node: $NODE"

TARGET="$DEST/harness/dsh"
ENTRY="$TARGET/node_modules/@deepseek-ai/dsh/lib/bin.js"

if [ -s "$ENTRY" ] && [ -z "$(tree_broken "$TARGET")" ]; then
  # "装好了"不能只看 bin.js 在不在 —— 必须过完整性自检 + 冒烟测试（见 tree_broken 的注释）
  # ---- 「装好了」不等于「装的是你要的那个版本」（2026-10-08 升级支持）----
  # 原来这里直接 ok 跳过 —— 于是升级永远静默无效。现在读已装版本分三种情况：
  #   ① 同版本 → 真跳过；② 不同 + --upgrade → 删整树重装；③ 不同 + 没 --upgrade → 如实提示。
  INSTALLED="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$TARGET/node_modules/@deepseek-ai/dsh/package.json" 2>/dev/null | head -1)"
  [ -n "$INSTALLED" ] || INSTALLED="（读不到）"
  if [ "$INSTALLED" = "$VERSION" ]; then
    ok "标准版已经在本机，版本 $INSTALLED（跳过下载；完整性自检 + 冒烟测试都过）"
  elif [ "$UPGRADE" = "1" ]; then
    warn "升级标准版：$INSTALLED → $VERSION（按 --upgrade 删整树重装）"
    rm -rf "$TARGET/node_modules" "$TARGET/package-lock.json" 2>/dev/null || true
    NEED_INSTALL=1
  else
    ok "标准版已经在本机，版本 $INSTALLED（完整性自检 + 冒烟测试都过）"
    if is_newer_version "$VERSION" "$INSTALLED"; then
      say "  注意：registry 上还有更新的版本 $VERSION（本机 $INSTALLED）"
      say "  升级到最新：加 --upgrade 重跑本脚本"
      say "  升级到指定版本：--upgrade --version <版本号>"
    fi
  fi
fi
if [ ! -s "$ENTRY" ] || [ -n "$(tree_broken "$TARGET")" ] || [ "${NEED_INSTALL:-0}" = "1" ]; then
  mkdir -p "$TARGET"
  if [ -s "$ENTRY" ]; then
    # bin.js 在却没过自检 = 上次装残了 → **整树删掉重装**：npm 只按版本号判断"这个包已装"，
    # 不会去修缺失的子目录（重跑 install 只会说 changed N packages）；package-lock 也可能
    # 被写残，残留会让下次 install 直接报 Invalid/Missing，一起删掉才干净。
    warn "检测到上次装残了（bin.js 在但跑不起来）—— 删掉整树重装"
    rm -rf "$TARGET/node_modules" "$TARGET/package-lock.json" 2>/dev/null || true
  fi
  if [ ! -f "$TARGET/package.json" ]; then
    printf '{"name":"echo-harness","private":true}\n' > "$TARGET/package.json"
  fi

  if [ "$FROM_CACHE" = "1" ]; then
    say "按要求跳过 npm，直接用 npx 缓存"
  elif command -v npm >/dev/null 2>&1; then
    say "用 npm 安装 @deepseek-ai/dsh@$VERSION（一次性，可能要几分钟；请勿中断）..."
    ( cd "$TARGET" && npm install "@deepseek-ai/dsh@$VERSION" --no-audit --no-fund 2>&1 | sed 's/^/      /' ) || true
  else
    warn "没找到 npm —— 跳过 npm，直接试 npx 缓存复制"
  fi

  if [ ! -s "$ENTRY" ]; then
    warn "npm 这条路没装上（公共 registry 上这个版本的依赖图可能是坏的，见 AGENTS.md）"
    say "  改用 npx 缓存里那份已经能跑的树 ..."
    PICK=""
    VER=""
    while IFS="$(printf '\t')" read -r v p; do
      [ -n "$p" ] || continue
      if [ -z "$PICK" ]; then PICK="$p"; VER="$v"; fi
      if [ "$v" = "$VERSION" ]; then PICK="$p"; VER="$v"; break; fi
    done <<EOF
$(cache_trees)
EOF
    if [ -z "$PICK" ]; then
      # 全新机器上缓存往往是空的（同事实测）→ 先自己填一次再找
      if fill_cache; then
        while IFS="$(printf '\t')" read -r v p; do
          [ -n "$p" ] || continue
          if [ -z "$PICK" ]; then PICK="$p"; VER="$v"; fi
          if [ "$v" = "$VERSION" ]; then PICK="$p"; VER="$v"; break; fi
        done <<EOF
$(cache_trees)
EOF
      fi
    fi
    if [ -z "$PICK" ]; then
      err "npx 缓存里也没有可用的标准版 —— 回退 npx：首次启动要多等 1-2 分钟"
      say "  想装本地入口：先跑一次 npx -y @deepseek-ai/dsh web（把缓存填上）再重跑本脚本"
      exit 1
    fi
    [ "$VER" = "$VERSION" ] || warn "缓存里是 $VER（要的是 $VERSION）—— 版本不同，但比 npx 快得多"
    say "  缓存树：$PICK（版本 $VER）"
    rm -rf "$TARGET/node_modules" 2>/dev/null || true
    if ! cp -R "$PICK" "$TARGET/node_modules"; then
      err "从缓存复制失败：$PICK -> $TARGET/node_modules"
      exit 1
    fi
    ok "已从 npx 缓存复制"
  fi
fi

BROKEN="$(tree_broken "$TARGET")"
if [ -n "$BROKEN" ]; then
  err "标准版安装不完整：$(printf '%s' "$BROKEN" | tr '\n' ' ')"
  say "  修法：删掉 $TARGET 后重跑本脚本"
  exit 1
fi

CMD="\"$NODE\" \"$ENTRY\" web"
ok "标准版已就绪（本地永久安装，冷启动约 10 秒）"
say "  启动命令：$CMD"
if [ -n "$COMMAND_FILE" ]; then
  printf '%s' "$CMD" > "$COMMAND_FILE" 2>/dev/null || true
fi
echo "HARNESS_COMMAND=$CMD"
exit 0
