# -*- coding: utf-8 -*-
"""llm_router.py — 把 ECHO 的模型组注册成 DSH 的本地模型（ECHO AUTO）

为什么不用写 DSH 插件
--------------------
DSH 的 `@deepseek-ai/dsh-llm-pi-ai` 适配器**按请求**读取家目录里的 settings.yaml，
provider 路由集合变化会原子重新注册——新增/删除一条路由在下一个请求就生效，
不需要重启 DSH。所以 ECHO 只要做三件事：

  1. 读 `dsh-failover/config.json` 的 `groups`：每个组 = 对 DSH 暴露的一个模型；
  2. 在（每个存在的）DSH 家目录的 settings.yaml 的 `llm-pi-ai.providers` 下 upsert
     一条路由（displayName 取组名，baseURL 指向本机路由端口）；
  3. 在同一个家目录的 `.credentials.yaml` 的 `refs` 下确保有 `ECHO_ROUTER_TOKEN`
     （路由自己的令牌，DSH 请求会带上它；组内成员的真实密钥只由 ECHO 持有）。

**DSH 可能不止一个家目录**（2026-09-22 同事反馈"用户不一定两个都有"）
--------------------------------------------------------------
同一台机器上可能装着：

  * DSH 桌面版            → 家目录取 `DSH_HOME` 环境变量，缺省是用户家目录下的 .dsh
  * 标准版 harness        → 家目录取 `harnessHome` 设置，缺省是 {DATA}/harness
  * 两个都装 / 两个都没装

所以本模块不再假设"只有一个"：:func:`dsh_homes` 列出**实际存在**的家目录
（判据是里面已经有 settings.yaml —— DSH 首次运行会自己写下它），注册与令牌
都以这个列表为准：

  * 只装桌面版、只装标准版、两个都装 —— 都写到各自家目录，谁都不落下；
  * 两个都没有 —— 明确回报"没找到 DSH 家目录"，**绝不替没装的 DSH 造目录**
    （D25 的老纪律），路由本身照常运行（纪要走 ECHO 自己的 provider 直连）；
  * 令牌在每个家目录里保持**同一个值**：路由只认一个令牌，两份不一致时报 401。

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

#: 桌面版家目录：`DSH_HOME` 环境变量优先（2.0 的 dsh-home 隔离 D6 依赖这个覆盖点），
#: 取不到才回落到用户家目录下的 .dsh。
DSH_HOME = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
SETTINGS = DSH_HOME / "settings.yaml"
CREDENTIALS = DSH_HOME / ".credentials.yaml"

#: 家目录的种类标签（面板/日志里说人话用）
HOME_DESKTOP = "desktop"
HOME_HARNESS = "harness"

#: 模型能力声明：取组内成员的最小值（DSH 用它做上下文压缩与溢出判断）
DEFAULT_CONTEXT_WINDOW = 131072
DEFAULT_MAX_TOKENS = 8192

#: 把"去哪儿找凭据"写给路由进程（见 dsh-failover/proxy.py 的 _cred_paths）。
#: 家目录可能**后来才出现**（用户在面板里选中标准版，harness 家目录才被建出来），
#: 所以不能只靠启动时的环境变量——路由进程每请求读这个文件，热更新。
HOMES_FILE = BASE_DIR / "dsh-failover" / "homes.json"


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


# ---------------------------------------------------------------- 家目录
def harness_home():
    """标准版 harness 的家目录（`harnessHome` 设置，缺省 {DATA}/harness）。

    取不到（没装 agent 层 / 设置读不出来）时返回 ``None`` —— 调用方按"这处不存在"处理。
    """
    try:
        from app import harness_proc
        return Path(harness_proc.home())
    except Exception:
        return None


def _home_entry(kind: str, label: str, home) -> dict:
    home = Path(home)
    credentials = home / ".credentials.yaml"
    return {
        "kind": kind,
        "label": label,
        "home": home,
        "settings": home / "settings.yaml",
        "credentials": credentials,
        "has_credentials": credentials.is_file(),
    }


def dsh_homes() -> list:
    """**实际存在**的 DSH 家目录（桌面版在前、标准版在后；同一路径只算一处）。

    「存在」的判据是里面已经有 `settings.yaml` **或** `.credentials.yaml`：

    * `settings.yaml` —— 桌面版首次运行就会写下它；
    * `.credentials.yaml` —— **标准版 harness 只写这个、从不写 settings.yaml**。
      2026-09-25 同事实测报告 §4.3 F：判据原来只认 `settings.yaml`，于是"只装标准版"
      的机器 `dsh_homes()` 返回 `[]`，`sync()` 报"没找到 DSH 家目录"，**ECHO AUTO 100%
      注册不上**（`/api/failover/health` → `groups: []`），而 `boot.py` 的 15s×8 次重试
      救不了 —— 判据本身不可能满足。症状是"智能体链路静默失效"，只有装过桌面版的机器不受影响。

    两个都不在时仍然**不替它造**配置（D25：没装 agent 就不动别人家的配置文件），
    所以四种装法照旧自洽：只有桌面版 / 只有标准版 / 两个都有 / 两个都没有（返回空列表）。
    """
    out, seen = [], set()
    candidates = [(HOME_DESKTOP, "DSH 桌面版", DSH_HOME)]
    hh = harness_home()
    if hh is not None:
        candidates.append((HOME_HARNESS, "标准版 harness", hh))
    for kind, label, home in candidates:
        try:
            key = os.path.normcase(os.path.abspath(str(home)))
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        entry = _home_entry(kind, label, home)
        if entry["settings"].is_file() or entry["credentials"].is_file():
            out.append(entry)
    return out


def _write_homes_file(homes=None) -> bool:
    """把"去哪儿找凭据"写给路由进程（原子写；失败不影响注册）。"""
    homes = dsh_homes() if homes is None else homes
    try:
        HOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "homes": [str(h["home"]) for h in homes],
            "credentials": [str(h["credentials"]) for h in homes],
            "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = HOMES_FILE.with_name(HOMES_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(HOMES_FILE)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 令牌
def _read_token(path) -> str:
    """从一个凭据库里读 ECHO_ROUTER_TOKEN（正则足够，不引入 YAML 依赖）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""
    m = re.search(rf"^\s+{TOKEN_REF}\s*:\s*(\S+)\s*$", text, re.M)
    return m.group(1).strip().strip("\"'") if m else ""


def router_token() -> str:
    """路由令牌：**存在的家目录里第一个写了它的值**。

    为什么不再只看桌面版（2026-09-22）：同事不一定两个都装。只装标准版时凭据在
    {DATA}/harness 下，只读桌面版会读成空串 —— 令牌校验随之中止（路由对本机不校验），
    ECHO 自己那条纪要链路也会丢掉 Authorization。
    """
    for h in dsh_homes():
        tok = _read_token(h["credentials"])
        if tok:
            return tok
    # 家目录还没初始化（连 settings.yaml 都没有）时的兜底：仍认 DSH_HOME 指的那份
    return _read_token(CREDENTIALS)


def _backup(path) -> None:
    try:
        path = Path(path)
        if path.is_file():
            shutil.copy2(path, path.with_name(
                path.name + ".bak-echo-auto-" + time.strftime("%Y%m%d-%H%M%S")))
            prune_backups(path, ".bak-echo-auto-")
    except Exception:
        pass


def _write_token(path, token: str) -> tuple:
    """把一个家目录的凭据库里的 ECHO_ROUTER_TOKEN 写成 ``token``（没有就新建该行）。"""
    path = Path(path)
    if not path.is_file():
        return False, f"凭据文件不存在：{path}"
    _backup(path)
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    lines = text.splitlines()
    same = [i for i, ln in enumerate(lines) if re.match(rf"^\s+{TOKEN_REF}\s*:", ln)]
    if same:
        for i in same:
            lines[i] = f"  {TOKEN_REF}: {token}"
        text = "\n".join(lines) + "\n"
    elif "refs:" in text:
        # 插到 refs: 之后的第一行位置（refs 是两空格缩进的键值对）
        idx = next(i for i, ln in enumerate(lines) if ln.strip() == "refs:")
        lines.insert(idx + 1, f"  {TOKEN_REF}: {token}")
        text = "\n".join(lines) + "\n"
    else:
        text = text.rstrip("\n") + f"\nrefs:\n  {TOKEN_REF}: {token}\n"
    try:
        path.write_text(text, encoding="utf-8")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if _read_token(path) != token:
        return False, "写入凭据后回读失败"
    return True, "已写入凭据 refs"


def _ensure_token() -> tuple:
    """让**所有存在的家目录**里是同一个令牌。返回 ``(ok, detail)``。

    路由只认一个令牌（config.json 里组上的 `require_token`），而每个 DSH 家目录各读
    各的凭据库 —— 两份不一致时，另一个家目录里的模型必然 401。所以基准取第一个已存在
    的值（桌面版优先，升级时保持原值不变），都没有才新生成，再补齐/改写其余家目录。
    """
    homes = [h for h in dsh_homes() if h["has_credentials"]]
    have = {h["kind"]: _read_token(h["credentials"]) for h in homes}
    token = next((t for t in have.values() if t), "") or _read_token(CREDENTIALS)
    fresh = not token
    if fresh:
        token = secrets.token_urlsafe(32)
    if not homes:
        # 没有任何可写的凭据库：路由降级为"本机不校验令牌"，不是错误，别挡住后面的事
        return True, "没有可写的 DSH 凭据库（未装 DSH 或还没初始化）"
    fixed, bad = [], []
    for h in homes:
        if have.get(h["kind"]) == token:
            continue
        ok, detail = _write_token(h["credentials"], token)
        if ok:
            fixed.append(h["label"])
        else:
            bad.append(f"{h['label']}（{detail}）")
    if bad:
        return False, "令牌写入失败：" + "、".join(bad)
    if fixed:
        return True, ("已生成令牌并写入 " if fresh else "已把令牌统一到 ") + "、".join(fixed)
    return True, "令牌已就绪（%s）" % "、".join(h["label"] for h in homes)


# ---------------------------------------------------------------- settings.yaml
# 写盘前先留一份同名备份，但必须限量：不清理的话家目录会被
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


def _sync_settings_ruamel(path=None) -> bool:
    path = Path(path or SETTINGS)
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap
    except Exception:
        return False
    try:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096
        with open(path, encoding="utf-8") as f:
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
        with open(path, "w", encoding="utf-8") as f:
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


def _sync_settings_text(path=None) -> bool:
    """文本兜底：在 llm-pi-ai.providers 块的末尾插入/替换 echo-auto 段。"""
    path = Path(path or SETTINGS)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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
        path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
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
        path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
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
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return True


def _verify_settings(path=None) -> tuple:
    """写完回读校验：能解析 + echo-auto 路由在位 + models 非空。"""
    path = Path(path or SETTINGS)
    try:
        import yaml as pyyaml
        doc = pyyaml.safe_load(path.read_text(encoding="utf-8"))
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
def _current_route(path=None) -> dict:
    """读 settings.yaml 里现存的 echo-auto 路由（用于判断是否需要写）。"""
    path = Path(path or SETTINGS)
    try:
        import yaml as pyyaml
        doc = pyyaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ((doc.get("llm-pi-ai") or {}).get("providers") or {}).get(ROUTE_ID) or {}
    except Exception:
        return {}


#: YAML 1.1（pyyaml 就是它）把 `off:` / `on:` 读成**布尔键**，而我们写下去的是字符串
#: "off"。比较"路由是否已是最新"时必须归一化，否则**每次启动都判定为不同**、白写一遍
#: （历史上家目录里堆出上百份 .bak-echo-auto-*，这个坑有它一份"功劳"）。
_BOOL_KEYS = {False: "off", True: "on"}


def _canon(value):
    """把读回来的结构归一化成可比较的形式（键一律字符串，YAML 布尔键还原成 off/on）。"""
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if isinstance(key, bool):
                key = _BOOL_KEYS.get(key, str(key))
            out[str(key)] = _canon(val)
        return out
    if isinstance(value, list):
        return [_canon(v) for v in value]
    return value


def route_is_current(path, entry=None) -> bool:
    """那个家目录里的 echo-auto 路由是否已经等于待写内容（归一化后比较）。"""
    try:
        return _canon(_current_route(path)) == _canon(entry or _route_entry())
    except Exception:
        return False


def sync() -> tuple:
    """把模型组注册进**每个存在的** DSH 家目录（幂等）。返回 (ok, detail)，永不抛异常。

    四种装法都要自洽（2026-09-22 同事反馈"用户不一定两个都有"）：
    只有桌面版 / 只有标准版 / 两个都有 / 两个都没有。两个都没有时明确回报
    "没找到 DSH 家目录"，**不建**任何目录，路由本身照常可用。
    """
    try:
        homes = dsh_homes()
        _write_homes_file(homes)        # 顺手告诉路由进程去哪儿找凭据（热更新）
        if not homes:
            return False, "没找到 DSH 家目录（桌面版/标准版都没装或还没初始化），未注册"
        if not groups():
            return False, "dsh-failover/config.json 里没有 groups，未注册"
        ok, detail = _ensure_token()
        if not ok:
            return False, detail
        # 有 settings.yaml 但还没有 .credentials.yaml 的家目录 = 那台 DSH 还没初始化完。
        # 写进去也是 MISSING_CREDENTIAL（模型摆在那儿却选不动），不如跳过并说清楚。
        ready = [h for h in homes if h["has_credentials"]]
        missing = [h["label"] for h in homes if not h["has_credentials"]]
        if not ready:
            return False, ("有 DSH 家目录但都还没有凭据库（.credentials.yaml）：%s，未注册"
                           % "、".join(missing))
        entry = _route_entry()
        wrote, current, bad = [], [], []
        for h in ready:
            label = h["label"]
            try:
                if route_is_current(h["settings"], entry):
                    current.append(label)
                    continue
                _backup(h["settings"])
                wrote_ok = (_sync_settings_ruamel(h["settings"])
                            or _sync_settings_text(h["settings"]))
                vok, vdetail = _verify_settings(h["settings"])
                if wrote_ok and vok:
                    wrote.append(label)
                else:
                    bad.append("%s（%s）" % (label, vdetail if not vok else "写入失败"))
            except Exception as exc:
                bad.append("%s（%s: %s）" % (label, type(exc).__name__, exc))
        parts = []
        if wrote:
            parts.append("已注册到 " + "、".join(wrote))
        if current:
            parts.append("已是最新（" + "、".join(current) + "）")
        if missing:
            parts.append("跳过（还没初始化出凭据库）：" + "、".join(missing))
        if bad:
            parts.append("失败：" + "；".join(bad))
        return (not bad), "；".join(parts) + " → " + route_base_url()
    except Exception as exc:            # boot 阶段绝不因注册失败而中断
        return False, f"{type(exc).__name__}: {exc}"


def status() -> dict:
    """面板/诊断用：当前注册态（**逐家目录**，另保留旧的单家目录字段）。"""
    homes = dsh_homes()
    per = []
    for h in homes:
        route = _current_route(h["settings"])
        per.append({
            "kind": h["kind"],
            "label": h["label"],
            "home": str(h["home"]),
            "settings": str(h["settings"]),
            "credentials": str(h["credentials"]),
            "registered": bool(route),
            "has_token": bool(_read_token(h["credentials"])),
        })
    primary = per[0] if per else None
    route = next((_current_route(p["settings"]) for p in per if p["registered"]), None) or {}
    return {
        "registered": any(p["registered"] for p in per),
        "settings": primary["settings"] if primary else str(SETTINGS),
        "credentials": primary["credentials"] if primary else str(CREDENTIALS),
        "has_token": bool(router_token()),
        "route": route,
        "groups": groups(),
        "homes": per,
    }


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    else:
        ok, detail = sync()
        # 控制台可能是 GBK（Windows 的 cmd/终端），符号编码不了会让**退出码变 1**、
        # 看起来像注册失败 —— 打印前先降级成 ASCII。
        mark = "[ok] " if ok else "[fail] "
        try:
            print(mark + detail)
        except UnicodeEncodeError:
            print((mark + detail).encode("ascii", "replace").decode("ascii"))
        sys.exit(0 if ok else 1)
