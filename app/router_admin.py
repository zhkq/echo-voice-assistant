# -*- coding: utf-8 -*-
"""router_admin.py — 模型路由的管理面（给面板「模型路由」页签与设置项用）

职责三件事：

  1. **候选发现**：读 DSH 的 `~/.dsh/settings.yaml`，把 DSH 里已有的模型供应商/模型
     摊平成"可勾选的候选成员"（内置 deepseek 官方路由 + 用户自己配的每一条 pi-ai 路由）。
     这样用户在 ECHO 里勾几个模型就能组成模型组，不用手抄 URL 和模型 id。
  2. **组成员增删改**：写回 `dsh-failover/config.json` 的 `groups.<echo-auto>`，
     并让路由进程热重载（`POST /admin/reload`），最后同步注册进 DSH。
  3. **健康聚合**：把路由的 `/health`、DSH 注册态、config.json 里的组定义合成一份
     给面板的 JSON（密钥永不返回，只回"该凭据引用是否已登记"）。

所有对外函数都不抛异常：面板拿到的永远是结构化结果 + 人话 detail。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from app import llm_router

CONFIG: Path = llm_router.ROUTER_CONFIG
GROUP_ID: str = llm_router.ROUTE_ID
# 路由基址在调用时求值（端口以 dsh-failover/config.json 为准）。此前是模块级常量，
# 导入时即固化，改端口必须重启 ECHO 才生效。
HTTP_TIMEOUT = 8.0

# dsh-llm-deepseek 的内置默认目录（settings.yaml 里没有 llm-deepseek 段时用它）
_DEEPSEEK_DEFAULTS = [
    {"id": "deepseek-flash", "name": "DeepSeek-Flash"},
    {"id": "deepseek-v4-flash", "name": "DeepSeek-V4-Flash"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro"},
    {"id": "deepseek-v4-flash-vision-exp", "name": "DeepSeek-V4-Flash-Vision"},
]
_DEEPSEEK_CTX = 1000000        # 与 dsh-llm-deepseek 的 DEFAULT_* 对齐
_DEEPSEEK_MAXTOK = 256000
_DEFAULT_CTX = 131072          # pi-ai 路由没写能力时按 dsh-llm-pi-ai 的默认值
_DEFAULT_MAXTOK = 8192
# 路由组的默认开关：ECHO AUTO 用「关不上就换人」的策略
_GROUP_DEFAULTS = {
    "require_token": True,
    "failover_on_status": [401, 402, 403, 404, 429],
    "failover_on_5xx": True,
}


# ---------------------------------------------------------------- config.json 读写
def load() -> dict:
    try:
        return json.loads(CONFIG.read_bytes().decode("utf-8-sig"))
    except Exception:
        return {}


def _save(cfg: dict) -> tuple:
    """备份 + 原子写 config.json（备份只保留最近 BACKUP_KEEP 份）。"""
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        if CONFIG.is_file():
            shutil.copy2(CONFIG, CONFIG.with_name(
                CONFIG.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S")))
            llm_router.prune_backups(CONFIG, ".bak-")
        tmp = CONFIG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(CONFIG)
        return True, "已保存"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def group_config() -> dict:
    return ((load().get("groups") or {}).get(GROUP_ID) or {})


# ---------------------------------------------------------------- DSH 侧：候选模型
def _dsh_settings() -> dict:
    try:
        import yaml as pyyaml
        return pyyaml.safe_load(llm_router.SETTINGS.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _cred_refs() -> set:
    """凭据库里已登记的 ref 名（只判有无，不读值）。"""
    try:
        import yaml as pyyaml
        doc = pyyaml.safe_load(llm_router.CREDENTIALS.read_text(encoding="utf-8")) or {}
        return set((doc.get("refs") or {}).keys())
    except Exception:
        return set()


def _has_key(cred: str, refs: set) -> bool:
    if not cred:
        return False
    return cred in refs or bool(os.environ.get(cred) or os.environ.get("FAILOVER_" + cred))


def _cand(provider: str, display: str, model_id: str, model_name: str, *, base_url: str,
          credential: str, refs: set, headers: dict = None, body_mode: str = "passthrough",
          context_window: int = _DEFAULT_CTX, max_tokens: int = _DEFAULT_MAXTOK,
          note: str = "") -> dict:
    return {
        "key": f"{provider}::{model_id}",
        "provider": provider,
        "provider_display": display,
        "model": model_id,
        "model_name": model_name or model_id,
        "base_url": (base_url or "").rstrip("/"),
        "credential": credential or "",
        "has_key": _has_key(credential, refs),
        "headers": headers or {},
        "body_mode": body_mode,
        "context_window": int(context_window or _DEFAULT_CTX),
        "max_tokens": int(max_tokens or _DEFAULT_MAXTOK),
        "note": note,
    }


def candidates() -> list:
    """DSH 里可用的模型 → 候选成员列表（供面板勾选）。"""
    doc = _dsh_settings()
    refs = _cred_refs()
    out: list = []

    # ① DSH 内置的 DeepSeek 官方路由（dsh-llm-deepseek；可被 settings.yaml 的 llm-deepseek 段覆盖）
    ds = doc.get("llm-deepseek") or {}
    ds_base = ds.get("baseURL") or "https://api.deepseek.com"
    ds_cred = ds.get("apiKeyEnv") or "DEEPSEEK_API_KEY"
    ds_models = ds.get("models") or _DEEPSEEK_DEFAULTS
    for m in ds_models:
        mid = m.get("id") if isinstance(m, dict) else str(m)
        if not mid:
            continue
        out.append(_cand(
            "deepseek-official", "DeepSeek 官方", mid,
            (m.get("name") if isinstance(m, dict) else "") or mid,
            base_url=ds_base, credential=ds_cred, refs=refs, body_mode="openai-safe",
            context_window=(m.get("contextWindow") if isinstance(m, dict) else 0) or _DEEPSEEK_CTX,
            max_tokens=(m.get("maxTokens") if isinstance(m, dict) else 0) or _DEEPSEEK_MAXTOK,
        ))

    # ② 用户自己在 DSH 里配的 pi-ai 路由（settings.yaml → llm-pi-ai.providers.*）
    provs = ((doc.get("llm-pi-ai") or {}).get("providers") or {})
    for pid, prov in provs.items():
        if pid == GROUP_ID:                      # 别把路由自己当成员
            continue
        if not isinstance(prov, dict):
            continue
        base = (prov.get("baseURL") or "").strip()
        disp = prov.get("displayName") or pid
        cred = prov.get("apiKeyEnv") or ""
        headers = {k: v for k, v in (prov.get("headers") or {}).items() if isinstance(v, str)}
        compat = prov.get("compat") or {}
        # 自建/内网网关（chat-template 思考格式）多半吃自家扩展字段 → 原样转发；
        # 目录型/官方路由 → 只发标准 OpenAI 字段（换通道时才不会被网关判非法参数）
        mode = "passthrough" if compat.get("thinkingFormat") == "chat-template" else "openai-safe"
        if not base:
            note = "该路由没有 baseURL（目录型路由由 DSH 内部拼接），暂不能作为路由成员"
        elif base.startswith(llm_router.route_base_url()):
            continue                              # 指向本机路由自己 → 跳过，避免套娃
        else:
            note = ""
        models = prov.get("models") or []
        if not models:
            out.append(_cand(pid, disp, "", "(该路由未声明模型)", base_url=base, credential=cred,
                             refs=refs, headers=headers, body_mode=mode, note=note))
            continue
        for m in models:
            mid = m.get("id") if isinstance(m, dict) else str(m)
            if not mid:
                continue
            out.append(_cand(
                pid, disp, mid, (m.get("name") if isinstance(m, dict) else "") or mid,
                base_url=base, credential=cred, refs=refs, headers=headers, body_mode=mode,
                context_window=(m.get("contextWindow") if isinstance(m, dict) else 0) or _DEFAULT_CTX,
                max_tokens=(m.get("maxTokens") if isinstance(m, dict) else 0) or _DEFAULT_MAXTOK,
                note=note,
            ))
    return out


# ---------------------------------------------------------------- 路由健康
def _request(path: str, payload: dict = None, timeout: float = HTTP_TIMEOUT) -> tuple:
    """调路由（管理接口自动带路由令牌）。返回 (ok, data_or_err)。"""
    url = llm_router.route_base_url() + path
    data = json.dumps(payload or {}).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    token = llm_router.router_token()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return False, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def health() -> dict:
    """路由进程的 /health；进程没起来时给出同构的 offline 结构（面板不必判空）。"""
    ok, data = _request("/health")
    if not ok:
        return {"ok": False, "proxy_online": False, "error": str(data),
                "groups": [], "routes": {}}
    data["ok"] = True
    data["proxy_online"] = True
    return data


def reload_router() -> tuple:
    ok, data = _request("/admin/reload", {})
    if not ok:
        return False, str(data)
    return bool(data.get("ok")), str(data.get("detail") or "")


def probe_now() -> tuple:
    ok, data = _request("/admin/probe", {}, timeout=40.0)
    if not ok:
        return False, str(data)
    return True, data


# ---------------------------------------------------------------- 组成员视图
def _member_key(m: dict) -> str:
    return f"{(m.get('base_url') or '').rstrip('/')}|{m.get('model') or ''}"


def members_view() -> dict:
    """面板用：配置里的组成员 + 健康表 + 候选匹配，合成一张表。"""
    g = group_config()
    h = health()
    hg = next((x for x in (h.get("groups") or []) if x.get("id") == GROUP_ID), {}) or {}
    hmembers = hg.get("members") or []
    cands = candidates()
    cby_key = {_member_key(c): c for c in cands}
    rows = []
    for i, m in enumerate(g.get("members") or [], start=1):
        key = _member_key(m)
        c = cby_key.get(key) or {}
        hm = hmembers[i - 1] if i - 1 < len(hmembers) else {}
        rows.append({
            "priority": int(m.get("priority", i)),
            "name": m.get("name") or f"成员 {i}",
            "enabled": bool(m.get("enabled", True)),
            "base_url": m.get("base_url") or "",
            "model": m.get("model") or "",
            "credential": m.get("credential") or "",
            "headers": m.get("headers") or {},
            "body_mode": m.get("body_mode") or "passthrough",
            "context_window": int(m.get("context_window", _DEFAULT_CTX)),
            "max_tokens": int(m.get("max_tokens", _DEFAULT_MAXTOK)),
            "candidate_key": c.get("key") or "",
            "has_key": bool(hm.get("has_token")) if hm else _has_key(m.get("credential") or "", _cred_refs()),
            "health": {
                "state": hm.get("state", "unknown"),
                "reachable": hm.get("reachable"),
                "detail": hm.get("detail", ""),
                "ok": hm.get("ok", 0),
                "fail": hm.get("fail", 0),
                "last_ttfb_ms": hm.get("last_ttfb_ms"),
                "last_probe_at": hm.get("last_probe_at", ""),
                "last_error": hm.get("last_error", ""),
                "last_used_at": hm.get("last_used_at", ""),
            },
        })
    return {
        "group": {
            "id": GROUP_ID,
            "display_name": g.get("display_name") or "ECHO AUTO",
            "context_window": int(g.get("context_window", _DEFAULT_CTX)),
            "max_tokens": int(g.get("max_tokens", _DEFAULT_MAXTOK)),
            "require_token": bool(g.get("require_token", True)),
            "failover_on_5xx": bool(g.get("failover_on_5xx", True)),
        },
        "members": rows,
        "router": {"online": bool(h.get("proxy_online")), "error": h.get("error", ""),
                   "url": llm_router.route_base_url(),
                   "routes": h.get("routes") or {}},
        "registration": registration(),
        "candidates": cands,
    }


# ---------------------------------------------------------------- 写回
def _merge_members(members: list) -> list:
    """把面板传来的成员列表整理成 config.json 的结构（顺序即优先级）。

    带 candidate_key 的成员以**服务端候选定义**为准（base_url/凭据/请求头/请求体模式/
    能力都从 DSH 配置现读），前端只负责决定"选谁、叫什么、启不启用"——避免前端漏字段
    把 `headers: {userId: ...}` 这种必需头丢掉。
    """
    cby_key = {c["key"]: c for c in candidates()}
    out = []
    for i, m in enumerate(members or [], start=1):
        ck = str(m.get("candidate_key") or "").strip()
        cand = cby_key.get(ck)
        base = str((cand or m).get("base_url") or "").strip().rstrip("/")
        model = str((cand or m).get("model") or "").strip()
        if not base or not model:
            continue
        if not base.startswith(("http://", "https://")):
            raise ValueError(f"成员 {i} 的 base_url 必须以 http(s):// 开头：{base}")
        name = str(m.get("name") or "").strip()
        if not name:
            name = (f"{cand['provider_display']} / {cand['model_name']}" if cand
                    else f"成员 {i}")
        headers = dict((cand or {}).get("headers") or {})
        headers.update({k: v for k, v in (m.get("headers") or {}).items() if v not in (None, "")})
        out.append({
            "name": name,
            "priority": i,                     # 列表顺序就是优先级，避免前后端两套序号打架
            "enabled": bool(m.get("enabled", True)),
            "base_url": base,
            "model": model,
            "credential": str((cand or m).get("credential") or "").strip(),
            "headers": headers,
            "body_mode": "openai-safe" if (cand or m).get("body_mode") == "openai-safe" else "passthrough",
            "context_window": int((cand or m).get("context_window") or _DEFAULT_CTX),
            "max_tokens": int((cand or m).get("max_tokens") or _DEFAULT_MAXTOK),
        })
    if not out:
        raise ValueError("模型组至少要有一个成员（含 base_url 与 model）")
    return out


def _group_caps(members: list) -> tuple:
    """对 DSH 声明的最小能力：取启用成员的最小值（避免把超长请求发给弱成员）。"""
    act = [m for m in members if m.get("enabled", True)] or members
    return (min(int(m.get("context_window") or _DEFAULT_CTX) for m in act),
            min(int(m.get("max_tokens") or _DEFAULT_MAXTOK) for m in act))


def save_members(members: list, *, sync: bool = True) -> tuple:
    """保存模型组成员（顺序 = 优先级）→ 热重载路由 → 同步注册进 DSH。"""
    try:
        norm = _merge_members(members)
    except ValueError as exc:
        return False, str(exc)
    cfg = load()
    cfg.setdefault("groups", {})
    old = cfg["groups"].get(GROUP_ID) or {}
    ctx, maxtok = _group_caps(norm)
    group = dict(old)                      # 保留 _comment_* 与用户自定义键
    group.update({
        "display_name": old.get("display_name") or "ECHO AUTO",
        "require_token": bool(old.get("require_token", _GROUP_DEFAULTS["require_token"])),
        "failover_on_status": old.get("failover_on_status", _GROUP_DEFAULTS["failover_on_status"]),
        "failover_on_5xx": bool(old.get("failover_on_5xx", _GROUP_DEFAULTS["failover_on_5xx"])),
        "context_window": ctx,
        "max_tokens": maxtok,
        "members": norm,
    })
    cfg["groups"][GROUP_ID] = group
    ok, detail = _save(cfg)
    if not ok:
        return False, f"写 config.json 失败：{detail}"
    rok, rdetail = reload_router()
    messages = [f"已保存 {len(norm)} 个成员", f"路由重载：{rdetail}" if rok else f"路由重载失败：{rdetail}"]
    if sync:
        sok, sdetail = llm_router.sync()
        messages.append(sdetail if sok else f"注册 DSH 失败：{sdetail}")
    return True, "；".join(messages)


def save_group_meta(display_name: str = None, context_window=None, max_tokens=None) -> tuple:
    cfg = load()
    g = (cfg.get("groups") or {}).get(GROUP_ID)
    if not g:
        return False, "config.json 里没有 echo-auto 组"
    if display_name:
        g["display_name"] = display_name
    if context_window:
        g["context_window"] = int(context_window)
    if max_tokens:
        g["max_tokens"] = int(max_tokens)
    ok, detail = _save(cfg)
    if not ok:
        return False, detail
    reload_router()
    return llm_router.sync()


# ---------------------------------------------------------------- DSH 注册态
def registration() -> dict:
    st = llm_router.status()
    route = st.get("route") or {}
    return {
        "registered": st.get("registered", False),
        "has_token": st.get("has_token", False),
        "base_url": (route.get("baseURL") if isinstance(route, dict) else "") or "",
        "models": len((route.get("models") if isinstance(route, dict) else []) or []),
        "settings": st.get("settings", ""),
    }


def register() -> tuple:
    """手动把当前模型组注册进 DSH。"""
    return llm_router.sync()


# ---------------------------------------------------------------- 设置联动
def apply_settings(updated: list) -> tuple:
    """设置页改了路由相关项后的联动。

    映射到 config.json 的键：
      routerProbeInterval   → probe_interval（热重载即生效）
      routerFirstByteTimeout→ first_byte_timeout（热重载即生效：成员会重建）
      routerBreakerThreshold/Cooldown → breaker_threshold / breaker_cooldown（热重载即生效）
      routerConnectTimeout  → connect_timeout（挂在 httpx client 上，**要重启路由进程**）
      routerDisplayName     → 组名（同时重新注册进 DSH）

    返回 (ok, detail)；没有相关改动时返回 (True, "")。
    """
    keys = [k for k in (updated or []) if k.startswith("router")]
    if not keys:
        return True, ""
    from app.config import settings as _s
    cfg = load()
    msgs, touched = [], False
    mapping = {
        "routerProbeInterval": ("probe_interval", float),
        "routerFirstByteTimeout": ("first_byte_timeout", float),
        "routerConnectTimeout": ("connect_timeout", float),
        "routerBreakerThreshold": ("breaker_threshold", int),
        "routerBreakerCooldown": ("breaker_cooldown", float),
    }
    for skey, (ckey, caster) in mapping.items():
        if skey in keys:
            try:
                cfg[ckey] = caster(_s.get(skey))
                touched = True
                msgs.append(f"{ckey}={cfg[ckey]}")
            except (TypeError, ValueError):
                pass
    if "routerConnectTimeout" in keys:
        msgs.append("（连接超时需重启模型路由进程）")
    if touched:
        ok, detail = _save(cfg)
        if not ok:
            return False, detail
        ok, rdetail = reload_router()
        msgs.append(f"重载：{rdetail}" if ok else f"重载失败：{rdetail}")
    if "routerDisplayName" in keys:
        name = (_s.get("routerDisplayName", "") or "").strip() or "ECHO AUTO"
        ok, detail = save_group_meta(display_name=name)
        msgs.append(detail if ok else f"组名同步失败：{detail}")
    return True, "；".join(m for m in msgs if m)
