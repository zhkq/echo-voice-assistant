# -*- coding: utf-8 -*-
"""管理动作的**共用实现**（设计 §8.4 的写面，2026-09-25 管理面开写时抽出）。

## 为什么要有这个文件

"发一张配对码 / 撤销一个客户端 / 轮换一个 secret / 改 scopes / 改配额 / 禁用"
这几件事原来只写在命令行里（`main._admin_cli`）。管理面开了写端点之后，
如果那边照着再写一遍，就是**同一件事两份实现**：命令行改了统一化、审计、宽限期，
网页那条路不会跟着改 —— 而两道出口看起来做的是同一件事，漂移了没人会发现。

所以判断与落库只留在这里一份，两个调用方各自只负责"怎么问、怎么打印 / 怎么回 JSON"：

| 调用方 | 在哪 | 它的额外职责 |
|---|---|---|
| 命令行 | `server/main.py::_admin_cli` | 打印成人话；自己开库（另起一个进程） |
| 管理面写端点 | `server/admin.py` | 校验请求体 / 二次确认 / 审计；用的是**能力面同一个** `Auth`（缓存当场失效） |

**为什么不直接 import `main`**：`main.py` 顶层 import 了 `admin`（lifespan 里要起管理面），
反过来 import 会成环。而且这三个模块的职责本来就不同 —— `main` 是进程入口，
`admin` 是管理面的 HTTP 层，`ops` 才是"对客户端与配对码做什么"。

## 一句必须记住的话

`issue_pairing_code()` / `rotate_client_secret()` 的返回值里带着**明文秘密**
（配对码、新 secret）。它**只应该被立刻打印或放进这一次的 HTTP 响应** ——
不落库、不写日志、不缓存、不再二次回显。库里永远只有哈希（设计 §7.4 约定 2 / §8.5）。
"""
from __future__ import annotations

import hashlib
import ssl
import time
from typing import Any, Dict, Optional

from server import auth as auth_mod
from server import errors


# ---------------------------------------------------------------- 小工具

def normalize_scopes(raw: str) -> str:
    """把 `"asr,diarize"` / `"asr diarize"` / `"asr  diarize"` 统一成空格分隔。

    存的格式就是 `auth._check_scope` 里 `.split()` 认的那种；不统一的话，
    命令行上的 `--set-scopes cli-x asr,diarize` 会存成一个**永远匹配不上任何槽**的
    字符串 —— 表现是"我明明给了权限却全 403"，很难查。管理面的表单同样会带逗号。
    """
    parts = [p for p in str(raw or "").replace(",", " ").split() if p]
    return " ".join(parts)


def cert_fingerprint(certfile: str) -> str:
    """证书指纹 `sha256:<hex>`（与客户端 `pairing.fingerprint_of` 同一算法）。

    为什么服务端自己写一遍、不 import 客户端的：`server/` 是**可以单独部署**的那一半
    （设计 §1），能少依赖一层就少一层。代价是"两处算同一个东西"可能漂移 ——
    所以有一条跨层用例拿同一张证书比两边的输出（`test_server_contract.CertFingerprintTests`）。

    读不到 / 解不开一律返回空串：调用方把空串当作"没有指纹可给"，**不编一个假的**。
    """
    try:
        with open(certfile, "rb") as fh:
            pem = fh.read().decode("utf-8", "replace")
        der = ssl.PEM_cert_to_DER_cert(pem)
    except Exception:
        return ""
    if not der:
        return ""
    return "sha256:" + hashlib.sha256(der).hexdigest()


def advertised_host(listen: str, tls: bool) -> tuple:
    """把 `server.listen` 变成**客户端能用**的 `scheme://host:port`。返回 `(地址, 备注)`。

    两处不能照抄 `listen`：

    * **通配地址对客户端没有意义**。出厂默认就是 `0.0.0.0:8900`；把这个抄进配对串，
      同事粘出来的结果是一句"连不上"，而且完全看不出是地址的错。所以这里探测本机地址，
      探不到就留一个**占位符**让人自己填 —— 宁可让人补一次，也不猜一个错的。
    * **配了 TLS 就必须写 `https://`**。客户端对不带 scheme 的地址默认按 http 处理
      （`pairing.normalize_base_url`），在 https 服务端上同样是"连不上"。
    """
    raw = str(listen or "")
    host, _, port = raw.rpartition(":")
    if not port:
        host, port = raw, ""
    host = host.strip()
    note = ""
    if host in ("", "0.0.0.0", "::", "[::]", "*"):
        guess = ""
        try:
            import socket
            guess = socket.gethostbyname(socket.gethostname())
        except Exception:                                    # pragma: no cover - 兜底
            guess = ""
        host = guess or "<这台后端的主机名或IP>"
        note = ("服务端监听的是通配地址 %s —— 上面这个地址是**本机探测到**的；"
                "同事连不上就换成他们能访问到的那台机器的主机名或 IP。" % (raw or "(空)"))
    return "%s://%s:%s" % ("https" if tls else "http", host, port), note


def pairing_string(cfg, code: str, ttl_s: Optional[float] = None) -> Dict[str, Any]:
    """一个裸配对码 → 那一整串 `echo://pair?host=…&code=…&fp=…`（设计 §7.5 ①）。

    **配对串只在 `server/ops.py` 这一处拼** —— 命令行与管理面都从这里拿，
    否则"网页上发出来的串"和"命令行发出来的串"迟早不一样（指纹少一个、
    scheme 少一个都会让客户端连不上，而且看不出原因）。
    """
    fp = cert_fingerprint(str(cfg.get("server.tls.certfile", "") or ""))
    addr, note = advertised_host(str(cfg.get("server.listen", "")), bool(fp))
    suffix = ("&fp=" + fp) if fp else ""
    ttl = float(ttl_s if ttl_s else cfg.get("auth.pairing_ttl_s", 900))
    return {"url": "echo://pair?host=%s&code=%s%s" % (addr, code, suffix),
            "note": note, "fingerprint": fp, "ttlSeconds": int(ttl)}


# ---------------------------------------------------------------- 配对码

def issue_pairing_code(cfg, auth, *, name: str = "", scopes: str = "",
                       ttl_s: Optional[float] = None, created_by: str = "") -> Dict[str, Any]:
    """发一张一次性配对码。**明文只在这个返回值里出现这一次。**

    返回里除了明文，还带上：
      * `id` —— 这张码在库里的身份（`code_hash`，作废时用它命名）；
      * `url` —— 可以直接粘给同事的整串（含证书指纹）；
      * `expiresAt` / `ttlSeconds` —— 面板与命令行都要显示剩余时间。
    """
    store = auth.store
    # 顺手把过期的清掉：这里本来就是**写路径**，而"待用配对码"这个数字要能自证
    # （设计 §8.4 说那一页与实际库不一致就是 bug）。清不掉不影响发码。
    try:
        store.sweep_pairing_codes()
    except Exception:
        pass
    code = auth.create_pairing_code(created_by=str(created_by or ""), name=str(name or ""),
                                    scopes=normalize_scopes(scopes), ttl_s=ttl_s)
    built = pairing_string(cfg, code, ttl_s=ttl_s)
    return {
        "id": auth_mod.hash_pairing_code(code),   # 同一个确定性哈希（无随机盐），与库里那张码逐字相同
        "code": code,
        "url": built["url"],
        "note": built["note"],
        "fingerprint": built["fingerprint"],
        "ttlSeconds": built["ttlSeconds"],
        "expiresAt": time.time() + float(built["ttlSeconds"]),
        "name": str(name or ""),
        "scopes": normalize_scopes(scopes),
        "createdBy": str(created_by or ""),
    }


def revoke_pairing_code(store, code_id: str) -> Dict[str, Any]:
    """作废一张**未使用**的码。不存在（用掉了 / 已经作废）就是 404，不当成功。"""
    if store is None:
        raise errors.auth_misconfigured("服务端没有可用的鉴权库")
    if not store.delete_pairing_code(str(code_id or "")):
        raise errors.EchoError(404, "pairing_code_not_found",
                               "没有这张待用的配对码（可能已经被用掉或作废）",
                               detail=str(code_id or ""))
    return {"ok": True, "id": str(code_id or "")}


def pending_pairing_codes(store, now: Optional[float] = None) -> list:
    """待用码清单（给管理面）。**明文永远不在这里** —— 库里只有哈希。"""
    if store is None:
        return []
    now = time.time() if now is None else float(now)
    out = []
    for r in store.pairing_codes():
        left = float(r.get("expires_at") or 0) - now
        out.append({
            "id": str(r.get("code_hash") or ""),
            "name": str(r.get("name") or ""),
            "scopes": str(r.get("scopes") or ""),
            "createdBy": str(r.get("created_by") or ""),
            "createdAt": float(r.get("created_at") or 0),
            "expiresAt": float(r.get("expires_at") or 0),
            "remainingSeconds": max(0, int(left)),
            "expired": left <= 0,
        })
    return out


# ---------------------------------------------------------------- 客户端

def _require_client(auth, client_id: str) -> Dict[str, Any]:
    """这个客户端存在吗。不存在就 404 —— 别让"改了个不存在的 id"看起来像成功。

    命令行曾经吃过这个亏：`store.revoke` 对不存在的行返回 0，
    于是打印"已撤销 xxx（token_version → 0）"，看着像成功了。
    """
    row = auth.store.client(client_id)
    if row is None:
        raise errors.EchoError(404, "client_not_found", "没有这个客户端",
                               detail=str(client_id or ""))
    return row


def revoke_client(cfg, auth, client_id: str) -> Dict[str, Any]:
    """撤销 = `token_version + 1`。**这是撤销能立即生效的全部机制**（设计 §7.5 ④）。

    `auth.cache.revoke()` **先落库、再让本地缓存失效**：
    管理面与能力面在同一个进程里（共用同一个 `Auth`），所以这条路**当场**生效；
    命令行是另一个进程，它改的库由 `RevocationWatcher` 在
    `auth.revoke_poll_s`（默认 5 秒）内发现 —— 两者都如实回报，不混成一句"立即"。
    """
    _require_client(auth, client_id)
    version = auth.cache.revoke(client_id)
    if not version:
        raise errors.EchoError(404, "client_not_found", "没有这个客户端", detail=str(client_id))
    return {"ok": True, "clientId": str(client_id), "tokenVersion": int(version),
            "revokePollSeconds": float(cfg.get("auth.revoke_poll_s", 5))}


def set_client_disabled(cfg, auth, client_id: str, disabled: bool) -> Dict[str, Any]:
    """禁用 / 启用。

    **`disabled` 与 `token_version` 是两回事**：禁用只是把那一位置 1（客户端收到
    403「认识你但不许用」），启用把它置回 0 之后**它手上那个令牌仍然有效**
    （版本号没变、scopes 没变），不需要重新换令牌。
    """
    _require_client(auth, client_id)
    auth.set_disabled(client_id, bool(disabled))
    row = auth.store.client(client_id) or {}
    return {"ok": True, "clientId": str(client_id), "disabled": bool(disabled),
            "tokenVersion": int(row.get("token_version") or 0),
            "tokenStillValid": not bool(disabled)}


def set_client_scopes(cfg, auth, client_id: str, scopes: str) -> Dict[str, Any]:
    """改 scopes。**下一个请求就生效**（鉴权读库里的行，不读 JWT 里的声明）。"""
    _require_client(auth, client_id)
    normalized = normalize_scopes(scopes)
    auth.set_scopes(client_id, normalized)
    return {"ok": True, "clientId": str(client_id), "scopes": normalized,
            "scopesList": normalized.split()}


def set_client_quota(cfg, auth, client_id: str, daily_audio_minutes: float) -> Dict[str, Any]:
    """改每日音频分钟数上限（0 = 用全局默认）。

    **不清零今天的已用量**：额度按自然日算（`server/quota.py`），
    改上限不该变成"送你一次重置"。
    """
    _require_client(auth, client_id)
    minutes = max(0.0, float(daily_audio_minutes or 0.0))
    auth.set_quota(client_id, minutes)
    return {"ok": True, "clientId": str(client_id), "dailyAudioMinutes": minutes,
            "usedMinutesTodayCleared": False}


def rotate_client_secret(cfg, auth, client_id: str, grace_hours: float = 0.0) -> Dict[str, Any]:
    """换 secret。**新明文只在返回值里出现这一次。**

    **总是**把 `token_version` +1：任何已发出的令牌立刻失效。
    宽限期（`grace_hours > 0`）是"给旧 **secret** 一条活路"，不是"给旧令牌"——
    它**不能用于 secret 泄漏**（旧 secret 照样进得来），只用于例行轮换不打断客户端。
    """
    row = _require_client(auth, client_id)
    secret = auth.rotate_secret(client_id, grace_hours=float(grace_hours or 0.0))
    after = auth.store.client(client_id) or {}
    out = {"ok": True, "clientId": str(client_id), "secret": secret,
           "tokenVersion": int(after.get("token_version") or 0),
           "graceHours": float(grace_hours or 0.0),
           "name": str(row.get("name") or ""), "scopes": str(row.get("scopes") or "")}
    if out["graceHours"] > 0:
        out["graceSeconds"] = max(0, int(float(after.get("prev_secret_expires_at") or 0)
                                        - time.time()))
    return out
