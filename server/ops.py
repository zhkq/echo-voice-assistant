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
import re
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

    ⚠️ **这个函数只看 `listen`，不看配置**。真正发配对码那条路走
    `resolve_advertised(cfg)`（它优先用 `server.advertised_host`）——
    两个名字分开留着，是因为这个签名是既有的跨层契约，而且"只按监听地址算"这件事
    本身还是对的（`localpair` 与旧调用方要的就是它）。
    """
    return _probe_advertised(str(listen or ""), bool(tls))


def _probe_advertised(raw: str, tls: bool) -> tuple:
    """`_probe` 的实现体（旧 `advertised_host` 的原样搬迁）。"""
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
        # 2026-09-30：容器里探测到的就是 Docker 网桥地址（实测 172.18.0.2），
        # 同事拿到那种串**必然连不上**。根治办法是配一项"对外公布地址"，所以这里
        # 除了保留上面那句如实提示，还要把**怎么修**说出口（配置键 + 环境变量名）。
        note += ("（对外服务时请配上 `server.advertised_host` 或环境变量 "
                 "`ECHO_ADVERTISED_HOST=<同事能访问到的 IP>`，就不会再靠探测。）")
    return "%s://%s:%s" % ("https" if tls else "http", host, port), note


#: `normalize_advertised_host` 认的形状：`host` / `host:port` / `[v6]` / `[v6]:port`。
#: **刻意窄**：认不出来就报错，不猜（猜错的代价是"同事照着串连不上"，而且看不出原因）。
_ADVERTISED_RE = re.compile(r"^(?P<host>\[[0-9A-Fa-f:.]+\]|[^\s:/\[\]]+)(?::(?P<port>\d+))?$")


def normalize_advertised_host(raw: Any) -> str:
    """`"http://10.100.0.24:8900/"` → `"10.100.0.24:8900"`（**归一化，非法值抛错**）。

    ## 接受的格式（这是**契约**，有用例钉着）

    | 写法 | 归一化成 | 说明 |
    |---|---|---|
    | `10.100.0.24` | `10.100.0.24` | 只给主机名/IP，端口留给调用方补（默认用 `server.listen` 的端口） |
    | `10.100.0.24:8900` | `10.100.0.24:8900` | 连端口一起给了 |
    | `http://10.100.0.24:8900` | `10.100.0.24:8900` | scheme 与结尾的 `/`、`?`、`#` 一并剥掉 |
    | `[fe80::1]:8900` | `[fe80::1]:8900` | IPv6 必须带方括号（带的端口才不歧义） |

    ## 三条明确的行为（都不是"随手定的"）

    1. **scheme 只被剥掉、不被采纳**。发出去的串用 `http` 还是 `https` **由 TLS 决定**
       （`server.tls.certfile` 配了就是 https，见 `_probe_advertised`）。理由：让
       `https://…` 在这里"提前生效"等于把 TLS 开关藏在地址串里 —— 那么
       "填了 https 但没配证书"就会发出一个连不上的串，而现象看着像客户端坏了。
    2. **空值返回空串**（= "没配"），**不是报错** —— 没配要走自动探测那条老路。
    3. **认不出来就抛 `ValueError`**（中文原因）。在配置里这是个手改的字面量：
       静默忽略会让人以为配了、其实还在探测（正是这次要修的 bug 的形态）。
       调用方把它翻成 400（管理面）/ 启动即报错（配置）/ 中文报错（命令行）。

    ## 这一项是"每次发码时公布的地址"，不是一次性全局常量

    同事要连 → 局域网 IP；后端就跑在客户端这台机器上 → `127.0.0.1`
    （本机永远连得上，换网络/换 IP 都不受影响）；同一台后端两边都服务 →
    **发码时各写各的**（`issue_pairing_code(..., advertised=...)`）。
    配对串本身带 host，所以两张码互不影响 —— 而且**串就是文本，手改 host 就能用**
    （刻意没做签名/校验，见 `pairing_string`）。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # scheme 剥掉（认不出的 scheme 一律当没写 scheme：`[^\s:/\[\]]+` 兜住剩下的部分，
    # 所以 `ftp://x` 会得到 `x` 的 host —— 与 `http://x` 同一条路，行为一致）。
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("?", 1)[0].split("#", 1)[0].strip().rstrip("/").strip()
    if not text:
        raise ValueError("对外公布地址里只有 scheme、没有主机名或 IP（给的是 %r）" % (raw,))
    m = _ADVERTISED_RE.match(text)
    if not m:
        raise ValueError("对外公布地址认不出来：%r。给它 主机名/IP、"
                         "`IP:端口`、或 `http://IP:端口` 这样的写法（IPv6 要带方括号）。"
                         % (raw,))
    host, port = m.group("host"), m.group("port")
    if port is not None and not (1 <= int(port) <= 65535):
        raise ValueError("对外公布地址里的端口要在 1 ~ 65535 之间（给的是 %s）" % port)
    return host + (":" + port if port else "")


def resolve_advertised(cfg, *, advertised: Any = None) -> tuple:
    """**发配对串时那条路**：`cfg`（+ 可选的这一次的覆盖）→ `(地址, 备注)`。

    优先级（与 `server/limits.py` 一个口径：管理面配过的值由 `apply_stored()`
    先写进 `cfg.raw`，所以这里只看 `cfg`）：

    | 顺序 | 来源 | 备注里怎么说 |
    |---|---|---|
    | 1 | 这一次的 `advertised=`（**每次发码各写各的**） | 空（调用方自己解释） |
    | 2 | `server.advertised_host`（配置 / 环境变量 / 管理面） | 空 |
    | 3 | **自动探测**（`server.listen` 是通配地址时） | 那句如实的提示，**一个字都不删** |

    ⚠️ 第 3 档**不许**改成"猜一个局域网 IP"：后端就跑在客户端这台机器上时
    （本机后端）那种猜法**必然猜错**（该给 `127.0.0.1`）。没配就如实说"这是探测到的"。
    """
    tls = bool(str(cfg.get("server.tls.certfile", "") or ""))
    # 这一次的覆盖先过一遍同样的归一化（不合法**当场报错** —— 半路静默退回全局值
    # 会让发码的人以为"我指定了"，而串里其实是别的地址）。
    chosen = ""
    if advertised not in (None, ""):
        try:
            chosen = normalize_advertised_host(advertised)
        except ValueError as exc:
            raise errors.bad_request("这一次的对外地址不合法：%s" % exc)
    from_cfg = False
    if not chosen:
        try:
            chosen = normalize_advertised_host(cfg.get("server.advertised_host", ""))
        except ValueError as exc:
            raise errors.EchoError(
                500, "config_invalid",
                "server.advertised_host 配错了：%s。改成 主机名/IP、`IP:端口` 或 "
                "`http://IP:端口`（IPv6 带方括号），或者留空让它自动探测。" % exc,
                detail=str(cfg.get("server.advertised_host", "")))
        from_cfg = bool(chosen)
    if not chosen:
        return _probe_advertised(str(cfg.get("server.listen", "") or ""), tls)
    # `normalize_advertised_host` 给的结果一定带方括号或只有一个冒号，所以这里按端口
    # **是不是纯数字**来切 —— 不能只看"有没有冒号"：`10.100.0.24` 本来就没有端口，
    # 用 `rpartition` 硬切会得到 `("", "10.100.0.24")`（那会拼出 `http://:10.100.0.24`）。
    host, port = chosen, ""
    head, sep, tail = chosen.rpartition(":")
    if sep and tail.isdigit():
        host, port = head, tail
    if not port:
        # 没带端口 → 用服务端自己的监听端口（客户端要连的就是那个端口）。
        try:
            port = str(int(cfg.get("server.listen", "").rsplit(":", 1)[1]))
        except (IndexError, ValueError):
            port = "8900"                  # 与 `Config.port` 的兜底同一个值
    return "%s://%s:%s" % ("https" if tls else "http", host, port), ""


def pairing_string(cfg, code: str, ttl_s: Optional[float] = None,
                   advertised: Any = None) -> Dict[str, Any]:
    """一个裸配对码 → 那一整串 `echo://pair?host=…&code=…&fp=…`（设计 §7.5 ①）。

    **配对串只在 `server/ops.py` 这一处拼** —— 命令行与管理面都从这里拿，
    否则"网页上发出来的串"和"命令行发出来的串"迟早不一样（指纹少一个、
    scheme 少一个都会让客户端连不上，而且看不出原因）。

    ## host 从哪来（2026-09-30 修的 bug）

    `server.advertised_host`（配置 / `ECHO_ADVERTISED_HOST` / 管理面配过的那份）**配了就用**；
    没配就保持原来的自动探测 —— 而探测在容器里拿到的是 **Docker 网桥地址**（实测
    `172.18.0.2`），同事必然连不上。优先级与那句如实提示见 `resolve_advertised()`。

    `advertised=` 是**这一次的覆盖**：同一台后端同时服务本机与远程时，
    本机那张码给 `127.0.0.1`、远程那张给局域网 IP（两张码互不影响）。

    ## 串是**文本**，手改 host 就能用

    刻意**不做签名、不做校验、不落库**：运维手上那份串只要能连通就行 ——
    把它做成"校验过的形式"就意味着"地址写错了只能重新发一张码"，
    而这恰恰是最常见的情形（换网络、换 IP、把本机码转给同事）。
    """
    fp = cert_fingerprint(str(cfg.get("server.tls.certfile", "") or ""))
    addr, note = resolve_advertised(cfg, advertised=advertised)
    suffix = ("&fp=" + fp) if fp else ""
    ttl = float(ttl_s if ttl_s else cfg.get("auth.pairing_ttl_s", 900))
    return {"url": "echo://pair?host=%s&code=%s%s" % (addr, code, suffix),
            "note": note, "fingerprint": fp, "ttlSeconds": int(ttl)}


# ---------------------------------------------------------------- 配对码

#: 本机自动配对那张码的 `created_by` 签名。**待用列表据此把它排除** ——
#: 它是机器对机器的握手凭据，不是给人抄的（见 `pending_pairing_codes` 的说明）。
#: 与 `server/localpair.py::publish(created_by=...)` 的默认值必须一致。
LOCAL_PAIR_CREATED_BY = "local-pair"


def issue_pairing_code(cfg, auth, *, name: str = "", scopes: str = "",
                       ttl_s: Optional[float] = None, created_by: str = "",
                       advertised: Any = None, client_id: str = "") -> Dict[str, Any]:
    """发一张一次性配对码。**明文只在这个返回值里出现这一次。**

    返回里除了明文，还带上：
      * `id` —— 这张码在库里的身份（`code_hash`，作废时用它命名）；
      * `url` —— 可以直接粘给同事的整串（含证书指纹）；
      * `expiresAt` / `ttlSeconds` —— 面板与命令行都要显示剩余时间。

    `advertised` = **这一次发码想公布的地址**（覆盖 `server.advertised_host`）——
    同一个后端同时服务本机与远程时，两张码各写各的。

    `client_id`（2026-10-06）：这张码发给**一个已知客户端** —— 兑换时复用那个身份、
    只轮换 secret（见 `Auth.redeem()`），而不是新建一行。空 = 照旧新建。
    """
    store = auth.store
    # 顺手把过期的清掉：这里本来就是**写路径**，而"待用配对码"这个数字要能自证
    # （设计 §8.4 说那一页与实际库不一致就是 bug）。清不掉不影响发码。
    try:
        store.sweep_pairing_codes()
    except Exception:
        pass
    code = auth.create_pairing_code(created_by=str(created_by or ""), name=str(name or ""),
                                    scopes=normalize_scopes(scopes), ttl_s=ttl_s,
                                    client_id=str(client_id or ""))
    built = pairing_string(cfg, code, ttl_s=ttl_s, advertised=advertised)
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
    """**待用 = 真正可用**：只列「未消费 **且** 未过期」的码。

    **这一份清单是两个出口共用的**（管理面 `GET /admin/api/pairing-codes` 与命令行
    `--list-codes`）—— 两边各自写一遍判据的话，"剩余多久 / 谁发的"迟早对不上。

    两个条件分别是怎么落地的：

    * **未消费**：结构性的，不需要在这里再判一次 —— `store.take_pairing_code()`
      在 `/v1/pair` 兑换成功的那一刻就把那一行**删掉**（"用掉即删"，设计 §8.5）。
      也就是说"已用过的码留在待用表里"这件事**不可能**来自这张表。
      消费的留痕在审计里（`auth.PAIR_REDEEM_ACTION`），**不**在这张表里。
    * **未过期**：必须在这里过滤（2026-09-29 用户实测的 bug）。过期的码本来就有
      "下一次发码顺手清理"（`ops.issue_pairing_code` 里那一步），但那一步只在**发码**
      时发生 —— 一张 13:25 到期的码，如果之后没人再发码，它会一直挂在表里，
      而"剩余"那一列渲染出来就是「已过期」/「剩余 0.0 分钟」：
      **标题写着「待用」、内容写着「已过期」，自相矛盾**。
      判据只有一份：`auth.pairing_code_expired()`（`Auth.redeem()` 用的是同一个），
      所以"列表里说还能用"与"兑换时认不认"永远是同一个答案。

    `remainingSeconds` **最少报 1 秒**（不报 0）：列出来的码一定还能用，而 0 会让
    页面按"已过期"渲染（`left(0)` 那个分支）—— 又是同一处自相矛盾。

    ## 本机自动配对的码**不算待用**（2026-10-06 用户给的判据）

    用户原话："配对完也不应该是待用状态……**发出后没有被用的码才叫待用**"。

    本机那张码（`created_by == "local-pair"`）是**机器对机器**的握手凭据，不是给人抄的：
    客户端一旦拿到凭据就**直接跳过配对**（`app/backend_setup.py::pair_if_needed()` 的
    跳过分支），**永远不会来兑换它**。所以它挂在"待用"里是假的 —— 那一页该显示的是
    "有人拿到、还没用掉"的码。

    而且它**不是**"没用过的码"：写进 `local-pair.json` 那一刻它就已经被这台机器"用"了
    （那个文件本身就是它的消费凭据），只是没走 `/v1/pair` 那条删除路径。
    复用逻辑（`localpair._reusable_code`）保证同一时刻至多一张，所以这里滤掉不会
    让数量失真，只会让那一页回到它该有的语义。

    人工发的码（命令行 `--new-pairing-code`、管理面发码）`created_by` 是 `cli` / 管理员
    用户名，**不受影响** —— 那些正是该被"待用"追踪的。
    """
    if store is None:
        return []
    now = time.time() if now is None else float(now)
    out = []
    for r in store.pairing_codes():
        if auth_mod.pairing_code_expired(r, now):
            continue                       # 过期的不进"待用"（留着由下次发码清理）
        if str(r.get("created_by") or "") == LOCAL_PAIR_CREATED_BY:
            # 本机自动配对用的那张：不是给人在待用列表里操作的（见上面那段）。
            continue
        left = float(r.get("expires_at") or 0) - now
        out.append({
            "id": str(r.get("code_hash") or ""),
            "name": str(r.get("name") or ""),
            "scopes": str(r.get("scopes") or ""),
            "createdBy": str(r.get("created_by") or ""),
            "createdAt": float(r.get("created_at") or 0),
            "expiresAt": float(r.get("expires_at") or 0),
            "remainingSeconds": max(1, int(left)),
            # 这个字段现在**恒为 False**（过期的已经被上面那行滤掉了）。
            # 留着是为了不破坏两个出口读它的既有约定（页面的徽章分支），
            # 而且"列出来的一定没过期"这件事在响应里是**明说**的，不靠调用方自己推。
            "expired": False,
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
