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
VERSION="0.1.5-rc.2"
FROM_CACHE=0
CACHE_DIR=""
NODE_BIN=""
COMMAND_FILE=""
LOG_FILE=""

usage() {
  cat <<'EOF'
用法：harness-install-local.sh --dest <ECHO 安装目录> [选项]
  --dest <目录>        必填。ECHO 安装目录（标准版装到 <目录>/harness/dsh）
  --version <版本>     @deepseek-ai/dsh 版本，默认 0.1.5-rc.2
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
  local target="$1" pty=""
  if [ ! -s "$target/node_modules/@deepseek-ai/dsh/lib/bin.js" ]; then
    echo "lib/bin.js（缺或为空）"
  fi
  for pty in $(find "$target" -type d -name node-pty 2>/dev/null); do
    if [ ! -f "$pty/package.json" ] || [ ! -f "$pty/lib/index.js" ]; then
      echo "node-pty 不完整：$pty"
    fi
  done
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

if [ -s "$ENTRY" ]; then
  ok "标准版已经在本机（跳过下载）"
else
  mkdir -p "$TARGET"
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
