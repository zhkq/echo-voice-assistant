# -*- coding: utf-8 -*-
"""本机自配对文件 —— 会议转写方案 1 的握手（2026-09-28 定，见 3.0 总览 §6.6）。

## 为什么需要它

ECHO 拆成"客户端 + 能力后端"两段之后，"本机跑得动"这件事的实质就是
**本机装了一个后端**（形态 1：单机双进程）。那种情况下不该让人抄配对码 ——
配对串是给**另一台**机器准备的。同机有文件系统，握手用文件就够：

    后端启动 → 写 `{state_root}/local-pair.json`
    客户端   → 读它 → 把里面的码喂给**原来那条** `pairing.pair()` → 落 `{DATA}/backend.json`

**复用而不是另造**：文件里的 `url` 就是命令行 / 管理面那串
`echo://pair?host=…&code=…&fp=…`（`ops.pairing_string()` 是**唯一**拼法），
而客户端做的也只是"把配对码交给既有配对函数"。于是"抄配对串"与"本机自动配对"
在客户端**产物完全一样**（同一个 `{DATA}/backend.json`，同一套凭据信封与 TLS 指纹固定）。

## 文件里放什么

* 放：`baseUrl`（客户端 `pair()` 要的 `http(s)://127.0.0.1:<port>`）、`url`（整串，
  给人看 / 排障）、`code`、`fingerprint`、`note`、`scopes`、`serverId`、
  `createdAt`、`expiresAt`、`source`。
* **不放 clientId / secret**：凭据仍然只落在客户端那一侧（Windows 走 DPAPI）。
  这里那张码是**一次性**的 —— 被换成凭据之后服务端库里那张就删了（既有语义）。

## 凭什么敢把码写在文件里

它落在**用户自己的状态目录**里（**鉴权库旁边**，默认 `server.state_root`），权限按本用户收紧
（POSIX `0600`，Windows 靠用户目录 ACL）。同机能读到它的进程，本来也能读到客户端
那份 `backend.json` —— 没有引入新的暴露面。而"回环免令牌"那种做法会引入新暴露面
（本机任何进程都能白用这块 GPU），所以没选它。

## 刷新与过期

**每次后端启动重发一张**（覆盖旧的）。TTL 默认 **7 天**（`auth.local_pair_ttl_s`）——
比默认的 900 秒宽，因为这个文件就是"以后随时来配"用的；真要长期不用，
重启一次后端即可换新。`read()` 与客户端都会把**过期**如实说出来，
而不是让人对着一个死文件猜。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict, Optional

from server import ops as ops_mod

#: 文件名固定（客户端按同一个名字在候选目录里找）。
FILENAME = "local-pair.json"

#: 默认 TTL：7 天。见文件头"刷新与过期"。
DEFAULT_TTL_S = 7 * 24 * 3600

#: 发给本机客户端的默认 scopes（能力面那几个槽）。
DEFAULT_SCOPES = "asr diarize embed"


def _state_dir(cfg) -> str:
    """本机配对文件该落在哪个目录：**跟着鉴权库走**（`auth.db` 显式给了就用它的目录）。

    为什么不是直接 `state_root`：这个文件与鉴权库是**同一件事**（配对）的耐久状态，
    保留策略也一致；而更要紧的是 —— `auth.db` 是测试/多实例**唯一已经隔离好的那个口**
    （`tests/test_server_contract.py::_cfg` 的注释就是"在唯一的配置入口上修"）。
    绑在 `state_root` 上会让每个进 lifespan 的用例都往开发机真实的
    `{ECHO}/data/server-state/` 里写一张新的配对码 —— 那正是 AGENTS.md 记过的那种事故。
    """
    explicit_db = str(cfg.get("auth.db", "") or "").strip()
    if explicit_db and explicit_db != ":memory:":
        return os.path.dirname(os.path.abspath(explicit_db))
    root = str(getattr(cfg, "state_root", "") or "").strip()
    if not root:
        raise RuntimeError("服务端配置里取不到 state_root —— 本机配对文件没地方写")
    return root


def path(cfg) -> str:
    """`{状态目录}/local-pair.json`。状态目录见 `_state_dir()`（默认 `server.state_root`）。"""
    return os.path.join(_state_dir(cfg), FILENAME)


def local_base_url(cfg) -> str:
    """本机客户端该连的地址：`http(s)://127.0.0.1:<端口>`。

    **不用 `advertised_host()`**：那个是给**别的机器**用的（监听通配地址时会换成
    探测到的网卡地址）。同机就应该走回环 —— 它不受"服务端 listen 是 0.0.0.0"
    影响，也不受防火墙对网段规则的影响。

    端口取 `server.listen` 的端口部分；容器里发布到宿主时端口一般一致，
    不一致（或想指到别的地址）时用 `server.local_pair_base_url` 覆盖。
    """
    forced = str(cfg.get("server.local_pair_base_url", "") or "").strip()
    if forced:
        return forced.rstrip("/")
    listen = str(cfg.get("server.listen", "") or "")
    port = listen.rsplit(":", 1)[-1].strip() or "8900"
    tls = bool(ops_mod.cert_fingerprint(str(cfg.get("server.tls.certfile", "") or "")))
    return "%s://127.0.0.1:%s" % ("https" if tls else "http", port)


def _atomic_write(target: str, text: str) -> None:
    """先写同目录临时文件再 `os.replace` —— 半个文件比没有文件更糟。"""
    d = os.path.dirname(target) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".local-pair-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o600)          # POSIX：只有本用户可读
        except Exception:
            pass                                # Windows：靠用户目录 ACL，不强求
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def _current_client_id() -> str:
    """读**客户端自己那份**凭据里的 `client_id`（`{DATA}/backend.json`）。

    用途：本机配对是"已知机器换凭据"，发码时带上它，兑换时就**复用同一个身份**
    （`Auth.redeem()` 里那条 reuse 分支）—— 这是"一台机器一个 client_id"的根治点。

    做成**尽力而为**：读不到就返回空串（照旧新建一个客户端）。
    容器形态下后端读不到客户端的文件系统，那时自然退化成旧行为 ——
    **不能因为读不到就让配对失败**（本机配对的可用性比身份复用重要）。
    """
    try:
        import json

        from app import paths as app_paths
    except Exception:
        return ""
    for cand in (os.path.join(app_paths.data_root(), "backend.json"),
                 os.path.join(app_paths.data_root(), "data", "backend.json")):
        try:
            with open(cand, "r", encoding="utf-8") as fh:
                body = json.load(fh)
            cid = str(body.get("client_id") or "").strip()
            if cid:
                return cid
        except Exception:
            continue
    return ""


def _local_client_already_paired(cfg) -> bool:
    """**不要用这个函数**（保留为反面教材，见下）。

    它想回答"本机客户端是不是已经有凭据了"，于是去读**客户端那侧**的
    `{DATA}/backend.json`。这在单机形态成立，但**后端可以跑在容器里**（另一套文件系统，
    见 `docs/后端容器-一键起-实施方案.md`）—— 那时它永远读不到，判据恒为 False，
    于是"配对完还有待用码"这个问题会**在容器形态下原样复现**，而且更难查。

    正确的落点在服务端能看见的那一侧：**待用列表本身**（见 `ops.pending_pairing_codes`
    对 `local-pair` 的处理）。所以这个函数不再被调用。
    """
    return False


def _reusable_code(cfg, auth) -> Optional[Dict[str, Any]]:
    """能不能**复用上一张**本机配对码？能就返回它（含明文），否则 ``None``。

    为什么要复用（2026-10-06，用户报的现场）：原来每次后端启动都 `publish()` 一张新码，
    而客户端那边 `pair_if_needed()` 一旦判定"已配对到本机回环地址"就**直接跳过** ——
    它压根不会来兑换这张码。于是**每启动一次后端就多一张没人用的码**，管理面
    「待用配对码」那一页跟着涨（本机实测：dev 攒了 16 张），而那页按设计本该是
    "与实际库一致"的自证页（`store.sweep_pairing_codes()` 的注释就是为这个写的）。

    判据（两条都成立才复用）：
      1. 上一次写下的那张码**仍在库里**（没被兑换走 —— 兑换时行会被删掉）；
      2. 它**还没过期**。
    不成立就照旧发新的（客户端凭据坏了/被清掉时会真的来兑换，那时必须有一张新码）。

    复用时**重写文件**（刷新 `createdAt`），但**不延长 TTL** —— 保持"7 天后必须重启
    换新"这条既有性质，免得码在文件里无限续期。
    """
    prev = read(cfg)
    if not isinstance(prev, dict):
        return None
    code = str(prev.get("code") or "").strip()
    if not code:
        return None
    try:
        exp = float(prev.get("expiresAt") or 0)
    except Exception:
        exp = 0.0
    if exp <= 0 or exp <= time.time():
        return None
    try:
        from server import auth as auth_mod
        want = auth_mod.hash_pairing_code(code)
        store = auth.store
        live = {str(r.get("code_hash") or "") for r in store.pairing_codes()}
    except Exception:
        return None
    if want not in live:
        return None
    return {
        "source": "local",
        "serverId": str(cfg.get("server.id", "") or ""),
        "baseUrl": str(prev.get("baseUrl") or local_base_url(cfg)),
        "url": str(prev.get("url") or ""),
        "code": code,
        "fingerprint": str(prev.get("fingerprint") or ""),
        "note": str(prev.get("note") or ""),
        "scopes": str(prev.get("scopes") or DEFAULT_SCOPES),
        "createdAt": time.time(),
        "expiresAt": exp,
    }


def publish(cfg, auth, *, scopes: str = DEFAULT_SCOPES, client_name: str = "本机客户端",
            ttl_s: Optional[float] = None, created_by: str = "local-pair") -> Dict[str, Any]:
    """发一张本机配对码并写到文件。返回文件内容（**含明文码**，只此一次）。

    调用方：`server/main.py` 的 lifespan（每次启动一张）。失败**不该拦住启动** ——
    所以调用方自己 try/except 并只写日志（这个文件是"方便"，不是"必需"）。

    ⚠️ **每次启动"叫一次"，不等于每次启动"发一张新码"**（2026-10-06 改）：
    上一张还在库里且没过期时**复用它**（见 `_reusable_code`）。否则每启动一次就多一张
    没人兑换的码，"待用配对码"那页会随启动次数增长 —— 而它本该是自证页。

    ## 这条路上**不需要** `server.advertised_host`

    （2026-09-30 用户定的口径）本机后端的正解是：监听 **`127.0.0.1:8900`**（只回环，
    这台机器自己用），配对走这个文件，地址就是构造出来的回环地址 —— 与"同事能不能
    连到这台的局域网 IP"是两个不相干的问题。

    所以这里**显式**把地址定成 `local_base_url(cfg)`：即使有人为了共享形态配了
    `ECHO_ADVERTISED_HOST=10.100.0.24`，本机那份文件里的串也仍然是回环地址。
    不这么做的话，同一份配置在两种形态之间会打架 —— 而症状是"本机自动配对突然连不上
    了（它拿着一个本机不该走的网卡地址）"，很难联想到是共享形态那一项在起作用。
    """
    reused = _reusable_code(cfg, auth)
    if reused is not None:
        _atomic_write(path(cfg), json.dumps(reused, ensure_ascii=False, indent=2) + "\n")
        return reused
    try:
        ttl = float(ttl_s if ttl_s else cfg.get("auth.local_pair_ttl_s", DEFAULT_TTL_S))
    except Exception:
        ttl = float(DEFAULT_TTL_S)
    loopback = local_base_url(cfg)
    info = ops_mod.issue_pairing_code(cfg, auth, name=str(client_name or ""),
                                      scopes=str(scopes or ""), ttl_s=ttl,
                                      created_by=str(created_by or ""),
                                      advertised=loopback,
                                      client_id=_current_client_id())
    body = {
        "source": "local",
        "serverId": str(cfg.get("server.id", "") or ""),
        "baseUrl": loopback,
        "url": info["url"],
        "code": info["code"],
        "fingerprint": info["fingerprint"],
        "note": info["note"],
        "scopes": info["scopes"],
        "createdAt": time.time(),
        "expiresAt": float(info["expiresAt"]),
    }
    _atomic_write(path(cfg), json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    return body


def read(cfg) -> Optional[Dict[str, Any]]:
    """读文件（不存在 / 坏 JSON 都返回 `None`，不抛 —— 调用方只需要"有没有"）。"""
    try:
        with open(path(cfg), "r", encoding="utf-8") as f:
            body = json.load(f)
        return body if isinstance(body, dict) else None
    except Exception:
        return None


def clear(cfg) -> bool:
    """删掉文件（解绑 / 排障用）。服务端那一行客户端记录**不删** —— 那是管理员的账。"""
    try:
        os.unlink(path(cfg))
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def expired(body: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    """码过期了吗（没有 `expiresAt` 的旧文件按"没过期"处理 —— 让服务端去拒）。"""
    if not isinstance(body, dict):
        return False
    try:
        exp = float(body.get("expiresAt") or 0)
    except Exception:
        return False
    if exp <= 0:
        return False
    return float(now if now is not None else time.time()) > exp
