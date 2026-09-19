# -*- coding: utf-8 -*-
"""llm_router.py — 把 ECHO 的模型组注册成 DSH Desktop 的本地模型（ECHO AUTO）

为什么不用写 DSH 插件
--------------------
DSH 的 `@deepseek-ai/dsh-llm-pi-ai` 适配器**按请求**读取 `~/.dsh/settings.yaml`，
provider 路由集合变化会原子重新注册——新增/删除一条路由在下一个请求就生效，
不需要重启 DSH。所以 ECHO 只要做三件事：

  1. 读 `dsh-failover/config.json` 的 `groups`：每个组 = 对 DSH 暴露的一个模型；
  2. 在 `~/.dsh/settings.yaml` 的 `llm-pi-ai.providers` 下 upsert 一条路由
     （displayName 取组名，baseURL 指向本机 8899）；
  3. 在 `~/.dsh/.credentials.yaml` 的 `refs` 下确保有 `ECHO_ROUTER_TOKEN`
     （路由自己的令牌，DSH 请求会带上它；组内成员的真实密钥只由 ECHO 持有）。

写文件一律先备份（同名备份只保留最近 `BACKUP_KEEP` 份，见 `prune_backups`）；
有 ruamel.yaml 就往返写入（保留用户注释与顺序），没有就退回纯文本插入（只动自己那一段）。
任何异常都不抛出——boot 阶段不能因为注册失败而挂掉。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from app import paths

# 安装根由路径层给（含 ECHO_ROOT 覆盖）；这里保留 Path 形态，后续用 `/` 拼路径。
BASE_DIR = Path(paths.echo_root())
ROUTER_CONFIG = BASE_DIR / "dsh-failover" / "config.json"
ROUTER_HOST = "127.0.0.1"
# 路由监听端口由 dsh-failover/config.json 的 "port" 决定（默认见 ROUTER_DEFAULT_PORT）。
# 2026-09-14 起不再写死：Windows 动态端口段（默认 1024-15000）会被 Hyper-V/WSL 划为
# 保留段且每次重启漂移，落在其中的端口会 bind 失败（Errno 13）。改配置即可迁移端口。
ROUTER_DEFAULT_PORT = 8899
ROUTE_ID = "echo-auto"                 # settings.yaml 里的 provider 键名
TOKEN_REF = "ECHO_ROUTER_TOKEN"        # 凭据 ref 名（DSH 用它做 apiKeyEnv）

DSH_HOME = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
SETTINGS = DSH_HOME / "settings.yaml"
CREDENTIALS = DSH_HOME / ".credentials.yaml"

# 模型能力声明：取组内成员的最小值（DSH 用它做上下文压缩与溢出判断）
DEFAULT_CONTEXT_WINDOW = 131072
DEFAULT_MAX_TOKENS = 8192


# ---------------------------------------------------------------- 配置读取
def _router_config() -> dict:
    try:
        return json.loads(ROUTER_CONFIG.read_bytes().decode("utf-8-sig"))
    except Exception:
        return {}


def groups() -> list:
    """要暴露给 DSH 的模型组（一个组 = 一个可选模型）。"""
    cfg = _router_config()
    out = []
    for gid, g in (cfg.get("groups") or {}).items():
        out.append({
            "id": gid,
            "display_name": g.get("display_name") or gid,
            "context_window": int(g.get("context_window", DEFAULT_CONTEXT_WINDOW)),
            "max_tokens": int(g.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "members": [
                {
                    "name": m.get("name") or f"{gid}-{i}",
                    "priority": int(m.get("priority", i)),
                    "model": m.get("model", ""),
                    "base_url": m.get("base_url", ""),
                    "credential": m.get("credential", ""),
                }
                for i, m in enumerate(g.get("members") or [], start=1)
            ],
        })
    return out


def route_port() -> int:
    """路由监听端口：优先取 config.json 的 port，取不到用默认值。"""
    try:
        return int(_router_config().get("port") or ROUTER_DEFAULT_PORT)
    except Exception:
        return ROUTER_DEFAULT_PORT


def route_base_url() -> str:
    return f"http://{ROUTER_HOST}:{route_port()}"


# ---------------------------------------------------------------- 令牌
def router_token() -> str:
    """读 DSH 凭据库里的 ECHO_ROUTER_TOKEN（组内令牌校验用）。"""
    try:
        text = CREDENTIALS.read_text(encoding="utf-8")
    except Exception:
        return ""
    m = re.search(rf"^\s+{TOKEN_REF}\s*:\s*(\S+)\s*$", text, re.M)
    return m.group(1).strip().strip("\"'") if m else ""


def _ensure_token() -> tuple:
    """确保凭据库里有 ECHO_ROUTER_TOKEN；没有就生成一个。"""
    if router_token():
        return True, "已存在"
    token = secrets.token_urlsafe(32)
    ok, detail = _write_credentials_token(token)
    return (True, "已生成") if ok else (False, detail)


def _write_credentials_token(token: str) -> tuple:
    if not CREDENTIALS.is_file():
        return False, f"凭据文件不存在：{CREDENTIALS}"
    _backup(CREDENTIALS)
    text = CREDENTIALS.read_text(encoding="utf-8")
    if "refs:" not in text:
        text = text.rstrip("\n") + f"\nrefs:\n  {TOKEN_REF}: {token}\n"
    else:
        # 插到 refs: 之后的第一行位置（refs 是两空格缩进的键值对）
        lines = text.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.strip() == "refs:")
        lines.insert(idx + 1, f"  {TOKEN_REF}: {token}")
        text = "\n".join(lines) + "\n"
    CREDENTIALS.write_text(text, encoding="utf-8")
    if router_token() != token:
        return False, "写入凭据后回读失败"
    return True, "已写入凭据 refs"


# ---------------------------------------------------------------- settings.yaml
# 写盘前先留一份同名备份，但必须限量：不清理的话 ~/.dsh 会被
# settings.yaml.bak-echo-auto-* 淹掉（2026-09-18 实测累积到 100 份；其中还有一份
# .credentials.yaml.bak-echo-auto-* 长期保留着内网网关令牌的旧明文副本）。
# 备份后缀是 %Y%m%d-%H%M%S，但历史上存在 .bak-maxtokens-* 这类人工命名，
# 所以按 mtime 排序而不是按文件名，只保留最近 BACKUP_KEEP 份。
BACKUP_KEEP = 5


def prune_backups(path: Path, prefix: str) -> None:
    """把 `<path><prefix>*` 的历史备份裁剪到最近 BACKUP_KEEP 份。永不抛异常。"""
    try:
        old = sorted(
            (p for p in path.parent.glob(path.name + prefix + "*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        if len(old) > BACKUP_KEEP:
            for p in old[:len(old) - BACKUP_KEEP]:
                try:
                    p.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def _backup(path: Path) -> None:
    try:
        if path.is_file():
            shutil.copy2(path, path.with_name(path.name + ".bak-echo-auto-" + time.strftime("%Y%m%d-%H%M%S")))
            prune_backups(path, ".bak-echo-auto-")
    except Exception:
        pass


def _route_entry() -> dict:
    """构造 llm-pi-ai.providers.<ROUTE_ID> 的内容（与联通路由保持同一套兼容开关）。"""
    gs = groups()
    models = [{
        "id": g["id"],
        "name": g["display_name"],
        "contextWindow": g["context_window"],
        "maxTokens": g["max_tokens"],
        "reasoningEfforts": {"off": None, "high": "high", "max": "max"},
        "compat": {"thinkingFormat": "chat-template", "supportsDeveloperRole": False},
    } for g in gs]
    return {
        "displayName": "ECHO AUTO（本机模型组）",
        "apiKeyEnv": TOKEN_REF,
        "api": "openai-completions",
        "baseURL": route_base_url(),
        "retryPolicy": {"mode": "normal", "maxRetries": 2},
        "compat": {"thinkingFormat": "chat-template", "supportsDeveloperRole": False},
        "models": models,
    }


def _dump_yaml(data, path: Path) -> bool:
    """用 ruamel 往返写入（保留注释）；不可用时返回 False 交给文本兜底。"""
    try:
        from ruamel.yaml import YAML
    except Exception:
        return False
    try:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)
        return True
    except Exception:
        return False


def _sync_settings_ruamel() -> bool:
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap
    except Exception:
        return False
    try:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096
        with open(SETTINGS, encoding="utf-8") as f:
            doc = yaml.load(f)
        if doc is None:
            doc = CommentedMap()
        sec = doc.get("llm-pi-ai")
        if sec is None:
            sec = CommentedMap()
            doc["llm-pi-ai"] = sec
        provs = sec.get("providers")
        if provs is None:
            provs = CommentedMap()
            sec["providers"] = provs
        provs[ROUTE_ID] = _route_entry()
        with open(SETTINGS, "w", encoding="utf-8") as f:
            yaml.dump(doc, f)
        return True
    except Exception as exc:
        print(f"[llm-router] ruamel 写入失败，改用文本插入: {exc}")
        return False


def _render_block(entry: dict, indent: int = 4) -> list:
    """把路由条目渲染成 YAML 文本行（只用于文本兜底路径）。"""
    pad = " " * indent
    out = [f"{pad}{ROUTE_ID}:"]
    out.append(f"{pad}  displayName: {entry['displayName']}")
    out.append(f"{pad}  apiKeyEnv: {TOKEN_REF}")
    out.append(f"{pad}  api: openai-completions")
    out.append(f"{pad}  baseURL: {entry['baseURL']}")
    out.append(f"{pad}  retryPolicy:")
    out.append(f"{pad}    mode: normal")
    out.append(f"{pad}    maxRetries: 2")
    out.append(f"{pad}  compat:")
    out.append(f"{pad}    thinkingFormat: chat-template")
    out.append(f"{pad}    supportsDeveloperRole: false")
    out.append(f"{pad}  models:")
    for m in entry["models"]:
        out.append(f"{pad}    - id: {m['id']}")
        out.append(f"{pad}      name: {m['name']}")
        out.append(f"{pad}      contextWindow: {m['contextWindow']}")
        out.append(f"{pad}      maxTokens: {m['maxTokens']}")
        out.append(f"{pad}      reasoningEfforts: {{off: null, high: high, max: max}}")
        out.append(f"{pad}      compat: {{thinkingFormat: chat-template, supportsDeveloperRole: false}}")
    return out


def _sync_settings_text() -> bool:
    """文本兜底：在 llm-pi-ai.providers 块的末尾插入/替换 echo-auto 段。"""
    try:
        lines = SETTINGS.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False

    def indent_of(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    # 1) llm-pi-ai: 顶层键
    top = next((i for i, ln in enumerate(lines) if ln.startswith("llm-pi-ai:")), None)
    if top is None:
        # DSH 首次配置 pi-ai 时还没有这个段。只在文件末尾追加
        # 我们自己的 provider，不改动现有顶层配置。
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["llm-pi-ai:", "  providers:"])
        lines.extend(_render_block(_route_entry(), indent=4))
        SETTINGS.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        return True
    section_end = len(lines)
    for i in range(top + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.lstrip().startswith("#") and indent_of(ln) == 0:
            section_end = i
            break
    # 2) 其下的 providers:
    prov = None
    for i in range(top + 1, section_end):
        ln = lines[i]
        if (ln.strip() and indent_of(ln) == 2
                and not ln.lstrip().startswith("#")
                and ln.strip().startswith("providers:")):
            prov = i
            break
    if prov is None:
        # llm-pi-ai 段已存在但还没有 providers：在该段末尾新建，
        # 保留段内其他选项。
        block = ["  providers:"] + _render_block(_route_entry(), indent=4) + [""]
        lines[section_end:section_end] = block
        SETTINGS.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        return True
    # 3) providers 块结束位置
    end = len(lines)
    for i in range(prov + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.lstrip().startswith("#") and indent_of(ln) <= 2:
            end = i
            break
    # 4) 删掉已有的 echo-auto 段（含其缩进 4 的子块）
    start = None
    for i in range(prov + 1, end):
        if lines[i].startswith("    " + ROUTE_ID + ":"):
            start = i
            break
    if start is not None:
        stop = end
        for i in range(start + 1, end):
            ln = lines[i]
            if ln.strip() and not ln.lstrip().startswith("#") and indent_of(ln) <= 4:
                stop = i
                break
        del lines[start:stop]
        end -= (stop - start)
    # 5) 插入新段
    block = _render_block(_route_entry(), indent=4)
    insert_at = end
    while insert_at > prov + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines[insert_at:insert_at] = block + [""]
    SETTINGS.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return True


def _verify_settings() -> tuple:
    """写完回读校验：能解析 + echo-auto 路由在位 + models 非空。"""
    try:
        import yaml as pyyaml
        doc = pyyaml.safe_load(SETTINGS.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"settings.yaml 解析失败：{type(exc).__name__}: {exc}"
    try:
        prov = doc["llm-pi-ai"]["providers"][ROUTE_ID]
        assert prov["models"], "models 为空"
        assert prov["baseURL"] and prov["api"], "缺 api/baseURL"
    except Exception as exc:
        return False, f"路由校验失败：{exc}"
    return True, f"{len(prov['models'])} 个模型 · {prov['baseURL']}"


# ---------------------------------------------------------------- 对外
def _current_route() -> dict:
    """读 settings.yaml 里现存的 echo-auto 路由（用于判断是否需要写）。"""
    try:
        import yaml as pyyaml
        doc = pyyaml.safe_load(SETTINGS.read_text(encoding="utf-8")) or {}
        return ((doc.get("llm-pi-ai") or {}).get("providers") or {}).get(ROUTE_ID) or {}
    except Exception:
        return {}


def sync() -> tuple:
    """把模型组注册进 DSH（幂等）。返回 (ok, detail)，永不抛异常。"""
    try:
        if not SETTINGS.is_file():
            return False, f"未找到 DSH 配置：{SETTINGS}"
        if not groups():
            return False, "dsh-failover/config.json 里没有 groups，未注册"
        ok, detail = _ensure_token()
        if not ok:
            return False, detail
        if _current_route() == _route_entry():
            return True, f"已是最新（{ROUTE_ID} → {route_base_url()}）"
        _backup(SETTINGS)
        wrote = _sync_settings_ruamel()
        if not wrote:
            wrote = _sync_settings_text()
        if not wrote and not SETTINGS.read_text(encoding="utf-8").count(f"    {ROUTE_ID}:"):
            return False, "写入 settings.yaml 失败"
        ok, detail = _verify_settings()
        if not ok:
            return False, detail
        gs = groups()
        names = "、".join(g["display_name"] for g in gs)
        return True, f"已注册 {ROUTE_ID}（{names}）→ {route_base_url()} · {detail}"
    except Exception as exc:            # boot 阶段绝不因注册失败而中断
        return False, f"{type(exc).__name__}: {exc}"


def status() -> dict:
    """面板/诊断用：当前注册态。"""
    try:
        import yaml as pyyaml
        doc = pyyaml.safe_load(SETTINGS.read_text(encoding="utf-8")) or {}
        prov = (doc.get("llm-pi-ai") or {}).get("providers", {}).get(ROUTE_ID)
    except Exception:
        prov = None
    return {
        "registered": bool(prov),
        "settings": str(SETTINGS),
        "credentials": str(CREDENTIALS),
        "has_token": bool(router_token()),
        "route": prov or None,
        "groups": groups(),
    }


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    else:
        ok, detail = sync()
        print(("✅ " if ok else "❌ ") + detail)
        sys.exit(0 if ok else 1)
