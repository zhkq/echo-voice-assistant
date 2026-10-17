# -*- coding: utf-8 -*-
"""ECHO 能力后端（服务端）的客户端适配器。

它对服务端的了解**只有 `/v1/*` 那份协议**（`docs/ECHO能力后端-服务端设计.md` §6）。
不 import 服务端的任何代码，也不假设它是 Python —— 这是"前后分离"的意义所在。

## 三个容易做错的地方

**① 槽要取交集，别把服务端的槽名原样搬来。**
服务端报的是**它自己模型的主槽**（`asr.long` / `asr.text` / `diarize.turns`…）
加上 `supports`。其中 `asr.long` **不是客户端的词汇**（客户端只说 `asr.text` /
`asr.timestamps` / `asr.streaming`，见 `base.SLOTS`）。所以这里 `provides` 取的是
`capabilities.slots ∩ base.SLOTS` —— 不然路由会拿一个没人认识的槽去派活。

**② 失败原因从服务端的 `code` 翻，不从 HTTP 状态猜。**
"503" 可能是 `server_busy`（该退避重试）也可能是 `model_failed`（该换后端）——
光看状态码分不出来，而这两种的处理**完全相反**。服务端 §6.3 存在的意义就是这个。

**③ 大 body 出错时错误体可能读不到**（服务端已修，但客户端仍要兜住）。
服务端在"读 body 之前"拒掉请求时不会读我们的上传；那边已经加了抽干
（`audio.drain`），但**网络中间还有反代**。所以读不到 body 时按状态码兜底成
`offline`/`error`，而不是崩在 `json.loads` 上。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Set, Tuple

from app.capabilities.base import (
    BACKEND_ECHO_SERVER,
    SERVER_CODE_TO_REASON,
    SKIP_REASONS,
    SOURCE_LAN,
    SOURCE_LOCAL,
    SLOTS,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    DiarizeResult,
    EmbedResult,
    error_from_server,
)
from app.capabilities import pairing

#: `capabilities` 缓存多久。探测是"面板与路由都要看"的东西，
#: 但**每次调用都问一次**又太吵 —— 30 秒是个折中（面板刷新默认 3 秒，够用）。
CAPS_TTL_S = 30.0

#: 请求超时。上传与推理分开：上传看网速，推理看模型（服务端默认上限 900 秒）。
UPLOAD_TIMEOUT_S = 120.0
INFER_TIMEOUT_S = 900.0

#: **探测**（`GET /v1/capabilities`）一次最多等多久。
#:
#: 为什么不是 15 秒（原来是 15）：`capabilities` 只是一份**静态元数据**，正常响应在
#: 毫秒级到一秒级；15 秒是从"上传/推理"那一档抄过来的数字，对一次元数据探测没有意义。
#: 真机现场（2026-10-17 13:18:54 停会 → 13:19:09 报错）就是这条 15 秒：停止录音后的
#: 转写被这一次探测卡了**整 15 秒**，然后被当成"后端不可用"。
#: 5 秒对"LAN 上的一台 GPU 机回一份 JSON"是宽裕的（正常 < 0.1 秒），
#: 又不至于让一次抖动把用户晾在那里。
CAPS_TIMEOUT_S = 5.0

#: 一次探测最多试几次。**一次超时 ≠ 不可用**（真机上同一台后端 48 秒后手动重转就成功了，
#: 而它全程 online）—— 所以至少要有**一次**快速重试，把"这一下抖了"和"它就是不回"分开。
#:
#: 为什么只重试 1 次（共 2 次）：会议停止后的转写**卡在这一步上**，重试次数越多，
#: "明确连不上"的那种情况就等得越久（2 次 × 5 秒已经 10 秒）。而真正需要多次重试的
#: 抖动已经被下一条"沿用上次已知能力"兜住了 —— 没必要把延迟叠在每一次上。
CAPS_ATTEMPTS = 2

#: 两次探测之间等多久。**很短**：要区分的是"这一下抖了"与"它就是不回"，
#: 等太久没有意义（抖动是秒级的；彻底不可用时多等只是让用户多等）。
CAPS_RETRY_DELAY_S = 0.4

#: **上一次成功拿到的能力集**最多沿用多久（秒）。
#:
#: 为什么需要"沿用"：`meeting.py` 每轮转写都**新建一个 router**（`build_default_router()`），
#: 于是实例自带的 `provides` 每轮都是空的 —— 探测失败一次就等于"这台后端什么都不提供"，
#: 哪怕它刚才还在正常转写。真机那场的 "waiting-backend" 就是这么来的。
#:
#: 为什么给一个上限（而不是永远沿用）：一台**真的已经关掉**的后端，不能因为"上周它还在"
#: 就一直被当成可用 —— 那样这一场会逐段去撞推理超时，而用户永远等不到一句"待转写"。
#: 30 分钟：一段会议音频通常是 10 分钟，够覆盖一次抖动/一次面板误点；再久就不可信了
#: （能力集是后端自己宣告的，它换了模型/换了 scopes 我们也不知道）。
CAPS_STALE_MAX_S = 1800.0

#: 探测失败之后那次**健康探针**（`GET /v1/health`）最多等多久。
#: 它只有一个用途：区分"能力探测这一下没成，但后端活着"与"它现在真的连不上"。
#: 免凭据、体量极小，所以比能力探测更短也可以接受。
HEALTH_TIMEOUT_S = 5.0

#: 探测结果的**状态词**（`describe()` 会原样下发给面板，`/api/capability` 里能看到）。
#:
#: 这几个词存在的唯一理由：**"没问到它"与"它明确不提供"不是一件事**。
#: 混成一个"什么都不提供"，用户就会被引去换后端/改配置，而真因可能只是这一次网络抖了。
CAPS_STATE_OK = "ok"              # 探测成功，且它声明了至少一个我们认得的槽
CAPS_STATE_EMPTY = "empty"        # 探测**成功**，但它明确不提供任何（我们认得的）槽
CAPS_STATE_STALE = "stale"        # 探测失败，沿用上次已知能力（健康探针通 → 这一轮照常用）
CAPS_STATE_OFFLINE = "offline"    # 探测失败且健康探针也失败 → 这一轮**不派活**给它
CAPS_STATE_UNKNOWN = "unknown"    # 探测失败且没有可沿用的已知能力（首次就失败）

#: 进程级的"上次已知能力"。键是 `(base_url, 凭据身份)`。
#:
#: 为什么放在模块级而不是实例上：`build_default_router()` 每轮都造新实例
#: （会议转写、面板每次打开都会各造一个），实例级缓存**永远不可能命中**。
#: 这条缓存只做一件事：**探测失败时的兜底**（成功时不用它，免得让"能力"莫名其妙地旧 30 秒）。
#: 键里带凭据身份是因为服务端会按 client scopes 决定报哪些槽 ——
#: 换一个客户端/换一个手填令牌，能力集本来就可能不同，不能混着沿用。
_CAPS_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_CAPS_LOCK = threading.Lock()
#: 缓存最多留几条（一个进程里通常只有一台后端；给用例与换地址留点余量）。
_CAPS_CACHE_MAX = 8


def reset_caps_cache() -> None:
    """清掉"上次已知能力"的进程级缓存。

    **用例之间必须互相隔离**（否则上一条用例的成功探测会让下一条用例"探测失败"的场景
    永远走不到兜底分支）；排障时也可以用它把状态清干净再复现。
    """
    with _CAPS_LOCK:
        _CAPS_CACHE.clear()


def _caps_cache_key(base_url: str, creds: Any, token: str) -> Tuple[str, str]:
    """这份能力集属于"哪台后端的哪个身份"。"""
    who = "manual-token" if token else str(getattr(creds, "client_id", "") or "")
    return (str(base_url or "").rstrip("/"), who)


def _caps_cache_get(key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
    with _CAPS_LOCK:
        entry = _CAPS_CACHE.get(key)
        return dict(entry) if entry else None


def _caps_cache_put(key: Tuple[str, str], snapshot: Dict[str, Any]) -> None:
    with _CAPS_LOCK:
        if key not in _CAPS_CACHE and len(_CAPS_CACHE) >= _CAPS_CACHE_MAX:
            oldest = min(_CAPS_CACHE, key=lambda k: float(_CAPS_CACHE[k].get("at") or 0.0))
            _CAPS_CACHE.pop(oldest, None)
        _CAPS_CACHE[key] = dict(snapshot)


def _log_probe(level: str, message: str) -> None:
    """把探测失败（或"沿用旧能力"）写进日志。**只写元数据**：地址、耗时、原因。

    为什么非写不可：真机上那 15 秒只有"面板报错"这一个痕迹，日志里**一个字节都没有**
    （`_caps_error` 只存在内存里、随后实例就被丢了）—— 于是只能靠"15 秒 + 代码里的
    timeout=15"去**推断**根因。失败原因必须能从日志里查到，下一次才不用猜。

    失败是常态（后端可能本来就关着），所以这里**永不抛**：日志写不进去不该连累转写。
    """
    try:
        from app import db
        db.add_log(level, "capability", str(message)[:2000])
    except Exception:
        pass


def _setting(key, default=None):
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:
        return default


def _is_loopback(url: str) -> bool:
    """这个地址是不是打到本机（回环）？

    **判据只有一处**：`app.netlocal.is_loopback()`（认 `127.0.0.1` / `localhost` / `::1`
    与整个 `127.0.0.0/8`，也认 IPv6 方括号与大小写）。这里不再写第二套主机名匹配 ——
    "两处各写一遍"正是这类判据漂移的来源。判不了（空地址/坏 URL）返回 False：
    那按**非本机**算（保守），与 `source` 的兜底同一个方向。永不抛。
    """
    try:
        from app import netlocal
        return bool(netlocal.is_loopback(url))
    except Exception:
        return False


def _creds():
    """本机的配对凭据；没配/坏了返回 None（`credentials.load` 永不抛）。"""
    try:
        from app.capabilities import credentials
        return credentials.load()
    except Exception:
        return None


def _base_url_from_settings(*, use_credentials: bool = True) -> str:
    """后端地址：**设置里填的优先**，没填就用配对时记下的那个。

    顺序这么定是因为两种来源的语义不同：设置里的是"运营让我连这台"，
    凭据里的是"我配对时连的是这台"。前者是显式配置，该赢。

    `use_credentials=False`（= 构造时传了 `creds=False`）连凭据都不看 —— 那是
    "就当本机没配对"这条断言的唯一正确读法（见 `__init__` 的注释）。
    """
    url = str(_setting("capabilityEchoServerUrl", "") or "").strip().rstrip("/")
    if url:
        return url
    if not use_credentials:
        return ""
    c = _creds()
    return str(getattr(c, "base_url", "") or "").strip().rstrip("/")


def _token_from_settings() -> str:
    """设置里手填的 Bearer 令牌（服务端 `auth.mode=token` 的静态令牌，
    或者临时贴一个 JWT 进去调试用）。

    ⚠️ **手动填的令牌不参与自动续期**：它不是我们换来的，也就不知道它的期限，
    静默拿凭据去换一个反而会把人的调试意图搅乱。401 时如实报错。
    """
    jwt = str(_setting("capabilityEchoServerToken", "") or "").strip()
    if jwt:
        return jwt
    return str(_setting("capabilityEchoServerStaticToken", "") or "").strip()


def backend_endpoint(slot: str = "") -> tuple:
    """`(后端地址, Authorization 头)` —— 给**不是能力调用**的模块复用（如主机侧中继）。

    为什么要有这个公共入口：中继也要连同一台后端、用同一套凭据，而"地址从哪来、
    令牌怎么换、401 怎么重试"这三件事**只该有一份实现**（`_base_url_from_settings`
    + `_auth_header`）。中继自己再写一遍，两份迟早会漂移 —— 而漂移的表现是
    "能力调用好好的、中继却 401"，查起来很贵。

    返回空 Authorization 是**合法**的（服务端可能没开鉴权）；调用方不要自己猜。
    """
    client = EchoServerClient()
    return client.base_url, client._auth_header(slot=slot)


class EchoServerClient(CapabilityClient):
    """服务端的一个连接。构造时不联网 —— 联网发生在 `refresh()` / 第一次调用。

    令牌的来源按这个顺序定（**先到先得，不合并**）：

    1. 构造参数 `token` / 设置里手填的 `capabilityEchoServerToken`、`…StaticToken`
       —— 别人给的，客户端**不知道它的期限，所以不替它续期**；
    2. 本机凭据（`{DATA}/backend.json`，配对得来）—— 过期就自动换，401 还会再换一次。

    顺序把"手填"放在前面，是为了让排障时能拿一个令牌把凭据那一路整个绕开。
    """

    backend_id = BACKEND_ECHO_SERVER

    @property
    def source(self) -> str:
        """这个后端**属于哪一档** —— 它是 privacy 约束的判据（2026-09-29 用户拍板 A）。

        * **回环地址**（`127.0.0.1` / `localhost` / `[::1]` / 整个 `127.0.0.0/8`）→ `SOURCE_LOCAL`：
          音频一个字节都没离开这台机器，所以「允许音频去哪 = 不出机」**不该**把它挡住。
          「帮我起本机后端」生成的就是这一档（只绑回环，见 `app/backend_setup.py`）。
        * **其它地址** → `SOURCE_LAN`（原行为：单位内网那台 GPU 机）。

        为什么不沿用"ECHO 后端一律算内网"的旧口径：那会把**本机的**后端判成"出机"，
        于是最在意隐私的人（把许可设成「不出机」）反而用不了它 —— 而"要用本机后端就得
        把许可放宽到内网"是**反向激励**：许可一放宽，真正的内网后端也跟着被放行。

        ⚠️ **已知边界（写在这里，因为它没法从地址上看出来）**：地址分不出"本机服务"与
        "本机隧道"。把远端后端用 `ssh -L 18900:远端:8900` 映射到本机回环、再填
        `http://127.0.0.1:18900` 时，这里会判成本机（于是「不出机」放行），
        而音频其实经隧道去了另一台机器。**用隧道的人请把「允许音频去哪」设成「内网」**
        （面板与向导里都写着这一句）。要更硬的判据只能让**后端自己宣告**它在哪台机器
        （`/v1/health` 已经有 `adminUrl` 这类宣告），那是另一件事。

        **故意不给 setter**：以前它是类属性、谁都能改；现在它是**从地址算出来的事实**，
        赋值只会造出"source 与实际地址不一致"的静默假象（要模拟别的档请换 base_url）。
        """
        return SOURCE_LOCAL if _is_loopback(self.base_url) else SOURCE_LAN

    def __init__(self, base_url: str = "", token: str = "", *,
                 backend_id: str = "", timeout_infer: float = INFER_TIMEOUT_S,
                 creds: Optional[Any] = None):
        # `creds=None` 表示"自己去读一次"；显式传 `False` 表示"就当本机没配对"
        # —— 连**地址**都不许从凭据里来。原来只挡住了 `self._creds`，地址照旧从凭据文件
        # 读出来，于是这句开关名不副实：`EchoServerClient(base_url="")` 到底算不算"配了"，
        # 取决于**跑它的那台机器有没有配过对**（2026-09-24 在这台机器上真配了一次之后，
        # `test_not_configured_is_absent_and_says_so` 当场变红，根因就是这里）。
        use_creds = creds is not False
        self.base_url = (base_url or _base_url_from_settings(
            use_credentials=use_creds)).rstrip("/")
        self._token = token or _token_from_settings()
        self._creds = _creds() if creds is None else (creds or None)
        if backend_id:
            self.backend_id = backend_id
        self.timeout_infer = float(timeout_infer)
        self.provides: frozenset = frozenset()
        self.vector_space_id = ""
        self.models: tuple = ()
        self.limits: Dict[str, Any] = {}
        self.server_name = ""
        self._caps_at = 0.0
        self._caps_error = ""
        #: 探测状态（见 `CAPS_STATE_*`）。面板与日志据此分清"没问到"与"明确不提供"。
        self._caps_state = ""
        #: 这次探测实际试了几次（重试过就是 2）。
        self._caps_attempts = 0
        #: 这一轮**敢不敢**派活给它。默认 True：`provides` 被外部显式填过（用例、或将来
        #: 别的能力来源）时，`supports()` 应当照旧可用 —— 只有"探测说它现在连不上"才翻成
        #: False（见 `supports()`）。
        self._caps_usable = True
        #: 最近一次 `/v1/health` 的原文（**只有元数据，没有任何内容**）。
        #: 「能力」卡的状态行（版本 / 显存 / 模型 / 今日额度）与「打开后端管理面」的
        #: 深链都读它 —— 后端自己的信息由后端宣告，客户端不猜。
        self._health: Dict[str, Any] = {}

    # ---------------------------------------------------------------- 探测

    @property
    def caps_state(self) -> str:
        """这一份能力集是怎么来的（`ok`/`empty`/`stale`/`offline`/`unknown`）。"""
        return self._caps_state

    @property
    def caps_error(self) -> str:
        """这次探测**为什么**没成（成功时是空串）。面板与日志都读它。"""
        return self._caps_error

    def supports(self, slot: str) -> bool:
        """这一轮**能不能**把这一槽派给它。

        与 `provides` 分开是必须的，因为两者回答的是不同的问题：
          * `provides` = "它**上次说**自己能干什么"（面板要如实显示，不能因为一次探测
            失败就改口说"它什么都不提供"）；
          * 这里 = "这一轮**敢不敢**把活交给它"。

        探测失败、健康探针也说连不上时（`offline`），这里必须是 False —— 否则一场 9 段的
        会议会把音频一段段发出去，每段都等满推理超时（900 秒），比"待转写"糟得多。
        而"探测失败但后端活着"（`stale`）时必须照旧 True：那正是本次要修的那种误报。
        """
        return bool(self._caps_usable) and slot in self.provides

    def _caps_key(self) -> Tuple[str, str]:
        return _caps_cache_key(self.base_url, self._creds, self._token)

    def _apply_caps(self, snapshot: Dict[str, Any]) -> None:
        """把一份能力快照写回这个实例（成功探测与"沿用上次已知"走同一条路）。"""
        self.provides = frozenset(snapshot.get("provides") or ())
        self.vector_space_id = str(snapshot.get("vector_space_id") or "")
        self.models = tuple(snapshot.get("models") or ())
        self.limits = dict(snapshot.get("limits") or {})
        self.server_name = str(snapshot.get("server_name") or "")
        self._caps_at = float(snapshot.get("at") or 0.0)

    def _snapshot(self) -> Dict[str, Any]:
        """当前能力集的快照（写进进程级缓存；`at` 是**成功**那一刻）。"""
        return {"provides": self.provides, "vector_space_id": self.vector_space_id,
                "models": self.models, "limits": dict(self.limits),
                "server_name": self.server_name, "at": time.time()}

    def _parse_caps(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """`/v1/capabilities` 的响应 → 能力快照（**只翻译，不带时间戳**）。"""
        snap: Dict[str, Any] = {}
        snap["server_name"] = str((body.get("server") or {}).get("id") or "")
        snap["limits"] = dict(body.get("limits") or {})
        snap["models"] = tuple(body.get("models") or [])

        # ① 槽取交集：服务端的 `asr.long` 之类**不是**客户端的词汇
        raw_slots: Set[str] = set((body.get("slots") or {}).keys())
        snap["provides"] = frozenset(raw_slots & set(SLOTS))

        # 向量空间：把**所有**产出向量的模型都收进来。
        # 服务端保证它们只有一个值（它有护栏测试钉着）；万一不是，
        # 我们取那个"唯一值"，多于一个就**拒绝提供向量类槽** ——
        # 因为"不确定属于哪个空间"的向量拿去做余弦相似度会**认错人且不报错**。
        spaces = {str(m.get("vectorSpaceId") or "") for m in snap["models"]
                  if m.get("vectorSpaceId")}
        if len(spaces) == 1:
            snap["vector_space_id"] = spaces.pop()
        elif len(spaces) > 1:
            snap["vector_space_id"] = ""
            snap["provides"] = frozenset(s for s in snap["provides"]
                                         if not s.startswith(("diarize.", "speaker.")))
        else:
            snap["vector_space_id"] = ""
        return snap

    def _health_ok(self) -> bool:
        """后端**现在**还活着吗（`/v1/health`，免凭据的轻探针）。

        只在能力探测失败之后用来做一次区分：它通了 → 能力探测那一下是抖动；
        它也不通 → 这一轮真的没有可用的后端（要如实报，但不能拿旧能力去盲派活）。
        """
        try:
            return bool(self.ready())
        except Exception:                                   # pragma: no cover - ready 不抛
            return False

    def refresh(self, force: bool = False) -> bool:
        """拉一次 `GET /v1/capabilities`。返回"拿到了吗"。**不抛异常。**

        拿不到不算致命：`ready()` 会返回 False，路由会按"这一轮用不上它"跳过。
        把探测失败做成异常，会让"面板想显示一下后端状态"变成一件要 try/except 的事。

        ## 这里的判据（2026-10-17 真机事故后改写，四条都要）

        1. **一次超时 ≠ 不可用**：一次元数据探测失败**不足以**宣告这台后端不可用
           （真机上同一台后端 48 秒后手动重转就成功了，而它全程 online）。
           所以先快速重试（`CAPS_ATTEMPTS`），再谈"用不用得上"。
        2. **不把 `provides` 清空**：失败时沿用**上一次成功**拿到的能力集
           （进程级缓存，见 `_CAPS_CACHE`）。原来那句 `self.provides = frozenset()`
           是这条事故的直接成因：一次探测失败 → "它什么都不提供" → 会议被记成
           `waiting-backend`。
        3. **沿用要有前提**：上次成功不能太旧（`CAPS_STALE_MAX_S`），而且要先过健康探针。
           不设前提的话，"永远沿用"等于**永远不报** —— 一台真关掉的后端会被一直当成可用，
           会议逐段撞推理超时，用户永远等不到"待转写"这句实话。
        4. **区分"没问到"与"它明确不提供"**：`_caps_state` 里
           `unknown`/`offline`/`stale` 是"没问到"（分别是 没得沿用 / 连不上 / 沿用了旧值），
           `empty` 是"问到了、它就是没有"，`ok` 是问到了且有。面板与日志都能看到。
        """
        if not self.base_url:
            self._caps_state = CAPS_STATE_UNKNOWN
            self._caps_error = "没配地址"
            self._caps_usable = False
            return False
        if not force and (time.time() - self._caps_at) < CAPS_TTL_S and self.provides:
            return True

        started = time.time()
        errors = []
        attempts = 0
        for attempt in range(1, CAPS_ATTEMPTS + 1):
            attempts = attempt
            try:
                status, body = self._request("GET", "/v1/capabilities",
                                             timeout=CAPS_TIMEOUT_S)
            except CapabilityError as e:
                # **每一次失败都落日志**，哪怕下一次就成功了：真机上那 15 秒
                # 在日志里一个字节都没留下，根因只能靠推断（"15 秒 + 代码 timeout=15"）。
                # 这一次的失败原因必须查得到。
                errors.append(str(e))
                _log_probe("warn", "能力探测第 %d/%d 次失败 base=%s：%s"
                           % (attempt, CAPS_ATTEMPTS, self.base_url, e))
            except Exception as e:                          # pragma: no cover - 兜底
                errors.append("%s: %s" % (type(e).__name__, e))
                _log_probe("warn", "能力探测第 %d/%d 次失败 base=%s：%s: %s"
                           % (attempt, CAPS_ATTEMPTS, self.base_url,
                              type(e).__name__, e))
            else:
                if status == 200 and isinstance(body, dict):
                    snap = self._parse_caps(body)
                    snap["at"] = time.time()
                    self._apply_caps(snap)
                    # 记进进程级缓存：下一轮（新实例）探测失败时这就是"上次已知能力"。
                    _caps_cache_put(self._caps_key(), self._snapshot())
                    self._caps_attempts = attempts
                    self._caps_usable = True
                    self._caps_error = ""
                    self._caps_state = (CAPS_STATE_OK if self.provides
                                        else CAPS_STATE_EMPTY)
                    return True
                errors.append("capabilities 回了 %s" % status)
                _log_probe("warn", "能力探测第 %d/%d 次失败 base=%s：capabilities 回了 %s"
                           % (attempt, CAPS_ATTEMPTS, self.base_url, status))
            if attempt < CAPS_ATTEMPTS:
                time.sleep(CAPS_RETRY_DELAY_S)

        # ---------------- 到这里就是"没问到"：重试过了，仍然失败 ----------------
        # **不在这里清空 provides**（原因见 docstring 第 2 条）。
        spent = time.time() - started
        why = errors[-1] if errors else "未知原因"
        self._caps_attempts = attempts
        self._caps_error = "能力探测失败（%d 次，%.1f 秒）：%s" % (attempts, spent, why)

        cached = _caps_cache_get(self._caps_key())
        age = (time.time() - float(cached.get("at") or 0.0)) if cached else -1.0
        if cached and 0.0 <= age <= CAPS_STALE_MAX_S:
            # 有可沿用的上次已知能力：先写回去（**provides 不清空**），再看它还活不活着。
            self._apply_caps(cached)
            if self._health_ok():
                # 后端活着，只是能力探测这一下没成 → 这一轮照常派活。
                self._caps_state = CAPS_STATE_STALE
                self._caps_usable = True
                self._caps_error += ("；沿用 %.0f 秒前的已知能力（健康探针仍通，"
                                     "这一轮照常派活）" % age)
                _log_probe("warn", "能力探测失败，沿用上次已知能力 base=%s：%s"
                           % (self.base_url, self._caps_error))
                return False
            # 连不上 → 不派活（否则每段音频都要等满推理超时）。
            self._caps_state = CAPS_STATE_OFFLINE
            self._caps_usable = False
            self._caps_error += "；健康探针也连不上（这一轮不把活派给它）"
        else:
            # 首次就失败，或上次成功已经太旧（旧到不可信）→ 状态是**未知**，
            # 不是"它不提供"。`provides` 保留上一次的值只为**如实描述**，
            # 但 `supports()` 会因 `_caps_usable=False` 而拒绝派活。
            self._caps_state = CAPS_STATE_UNKNOWN
            self._caps_usable = False
            if cached:
                self._apply_caps(cached)
                self._caps_error += ("；上次成功是 %.0f 分钟前，已超过 %.0f 分钟的沿用上限"
                                     % (age / 60.0, CAPS_STALE_MAX_S / 60.0))
            else:
                self._caps_error += "；没有可沿用的已知能力（首次探测就失败）"
        _log_probe("warn", "能力探测失败 base=%s：%s"
                   % (self.base_url, self._caps_error))
        return False

    def ready(self) -> Optional[bool]:
        if not self.base_url:
            return False
        try:
            status, body = self._request("GET", "/v1/health", timeout=HEALTH_TIMEOUT_S)
        except CapabilityError:
            # **连不上就把上次那份状态清掉**：拿旧数字当现状是"最绿的一种假"。
            self._health = {}
            return False
        except Exception:
            return None
        if status == 200 and isinstance(body, dict) and body.get("ok"):
            # 留下原文：面板的状态行与"打开后端管理面"深链都靠它（`describe()` 会带出去）。
            # 探针本身是**免凭据**的（设计如此），所以这里拿到的也只有元数据。
            self._health = dict(body)
            return True
        self._health = {}
        return False

    def describe(self) -> Dict[str, Any]:
        out = super().describe()
        out.update({"baseUrl": self.base_url, "serverName": self.server_name,
                    "limits": self.limits, "capsError": self._caps_error,
                    # 能力探测**是怎么来的**：`ok`/`empty` 是"问到了"，
                    # `stale`/`offline`/`unknown` 是"没问到"（分别对应 沿用旧值 /
                    # 连不上 / 没得沿用）。面板与 `/api/capability` 读这两个字段
                    # 才分得清"它明确不提供"与"这一次没问到它"（2026-10-17 事故）。
                    "capsState": self._caps_state or CAPS_STATE_UNKNOWN,
                    "capsAttempts": int(self._caps_attempts or 0),
                    "capsUsable": bool(self._caps_usable),
                    # 上次成功探测是多久以前（秒；0 表示从没成功过）——
                    # "沿用了旧能力"这句话得说得出来有多旧。
                    "capsAgeSeconds": (int(max(0.0, time.time() - self._caps_at))
                                       if self._caps_at else 0),
                    "paired": self._creds is not None,
                    "auth": "manual" if self._token else
                            ("paired" if self._creds is not None else "none"),
                    # 后端自己的状态（版本/显存/模型/配额/管理面入口）—— 面板直接渲染
                    "health": dict(self._health or {})})
        return out

    # ---------------------------------------------------------------- 调用

    def transcribe(self, wav, *, lang="auto", want_timestamps=False,
                   variant="long", **kw) -> AsrResult:
        q = "?variant=%s%s" % ("short" if variant == "short" else "long",
                               "&timestamps=1" if want_timestamps else "")
        if lang and lang != "auto":
            q += "&lang=%s" % urllib.parse.quote(str(lang))
        body = self._post_audio("/v1/asr" + q, wav, slot="asr.text")
        return AsrResult.from_server(body, self.backend_id)

    def diarize(self, wav, *, max_speakers=None, **kw) -> DiarizeResult:
        q = "?mode=segment"
        if max_speakers:
            q += "&maxSpeakers=%d" % int(max_speakers)
        body = self._post_audio("/v1/diarize" + q, wav, slot="diarize.turns")
        return DiarizeResult.from_server(body, self.backend_id)

    def embed(self, wav, *, count=1, **kw) -> EmbedResult:
        body = self._post_audio("/v1/speaker/embed?count=%d" % max(1, int(count or 1)),
                                wav, slot="speaker.embed")
        return EmbedResult.from_server(body, self.backend_id)

    # ---------------------------------------------------------------- 凭据

    def _renewable(self) -> bool:
        """这一路是"本机配对得来的凭据"吗 —— 只有它才谈得上续期。"""
        if self._token:
            return False
        c = self._creds
        return bool(c is not None and c.client_id and c.secret)

    def _auth_header(self, *, slot: str = "") -> str:
        """这次请求带什么 `Authorization`，没有就返回空串（服务端可能没开鉴权）。"""
        if self._token:
            return "Bearer " + _header_safe(self._token, "手填的令牌", self.backend_id, slot)
        if self._renewable():
            try:
                token = pairing.ensure_token(self._creds, timeout=pairing.PAIR_TIMEOUT_S)
            except pairing.PairingError as e:
                # **不静默降级成"不带令牌"**：那会把"凭据不能用了"表现成服务端的 401，
                # 而服务端那句"缺少 Bearer 令牌"完全指不到真正的原因。
                raise _pairing_failure(e, self.backend_id, slot) from None
            return "Bearer " + _header_safe(token, "换来的令牌", self.backend_id, slot)
        return ""

    def _force_renew(self, *, slot: str = "") -> None:
        """把令牌当成已失效，强制换一个（401 之后用）。失败就抛分类好的错。"""
        try:
            pairing.ensure_token(self._creds, skew_s=float("inf"),
                                 timeout=pairing.PAIR_TIMEOUT_S)
        except pairing.PairingError as e:
            raise _pairing_failure(e, self.backend_id, slot) from None

    # ---------------------------------------------------------------- HTTP

    def _post_audio(self, path: str, wav: str, *, slot: str) -> Dict[str, Any]:
        """把音频**原样**（raw body）传上去 —— 与服务端 §5.4 的约定一致。

        不用 multipart：那会让服务端把大文件交给框架 spool 到磁盘，
        而且那个文件**由框架创建、它的清理看不到它**（服务端 §5.4 的理由）。
        我们自己读文件、自己发 octet-stream，服务端那边一路是"边收边算"。
        """
        import os
        if not os.path.isfile(wav):
            raise CapabilityError("open-failed", "文件不存在: %s" % wav,
                                  backend_id=self.backend_id, slot=slot)
        with open(wav, "rb") as fh:
            data = fh.read()
        status, body = self._request("POST", path, data=data,
                                     content_type="audio/wav",
                                     timeout=self.timeout_infer, slot=slot)
        if status != 200 or not isinstance(body, dict):
            raise error_from_server(body, status, self.backend_id, slot)
        return body

    def _ssl_context(self, *, slot: str = ""):
        """https 时的固定证书上下文；http 时返回 `None`。

        **https 而没有固定证书 → 直接抛**（`blocked`），绝不静默跳过校验：
        那等于把中间人放进来，而且从现象上完全看不出来（"能用"）。
        """
        if not pairing.is_https(self.base_url):
            return None
        try:
            return pairing.pinned_context(getattr(self._creds, "cert_pem", "") or "",
                                          what="ECHO 后端")
        except pairing.PairingError as e:
            raise CapabilityError("blocked", str(e), code="blocked",
                                  backend_id=self.backend_id, slot=slot) from None

    def _request(self, method: str, path: str, *, data: Optional[bytes] = None,
                 content_type: str = "", timeout: float = 30.0,
                 slot: str = "", retry_auth: bool = True) -> tuple:
        """发一个请求。**失败一律抛 `CapabilityError`**（带分类），不返回空。"""
        if not self.base_url:
            raise CapabilityError("absent", "没配 ECHO 后端地址",
                                  backend_id=self.backend_id, slot=slot)
        url = self.base_url + path
        req = urllib.request.Request(url, data=data, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        auth = self._auth_header(slot=slot)
        if auth:
            req.add_header("Authorization", auth)
        # 让服务端日志与客户端日志能用同一个 id 对上（服务端 §7.3）
        req.add_header("X-Request-Id", _new_request_id())
        context = self._ssl_context(slot=slot)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                raw = resp.read()
                return resp.status, _maybe_json(raw)
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:
                # ③ 大 body 早退时**读不到错误体**（连接已被关）。状态码还在，
                #    按状态码兜底 —— 不能在这里崩掉。
                pass
            payload = _maybe_json(raw)
            if e.code == 401 and retry_auth and self._renewable():
                # 手上那个令牌被拒了，但本机凭据还在 → **换一次再试一次**。
                #
                # 什么时候真会走到这里：令牌还没到我们以为的过期时间，服务端那边
                # 已经不认了 —— 管理员 `--rotate-secret`（`token_version` +1）、
                # 或者内网两台机器的时钟差了几分钟。这两种都是"再换一个就好"。
                #
                # **只试一次**：真是 secret 失效的话，再换也换不来，
                # 无限重试只会把一次失败变成一个转不出来的循环。
                self._force_renew(slot=slot)
                return self._request(method, path, data=data, content_type=content_type,
                                     timeout=timeout, slot=slot, retry_auth=False)
            if isinstance(payload, dict) and payload.get("code"):
                raise error_from_server(payload, e.code, self.backend_id, slot) from None
            raise CapabilityError(
                "offline" if e.code in (502, 503, 504) else "error",
                "HTTP %s（读不到错误体，可能是反代/网关回的）" % e.code,
                code="", backend_id=self.backend_id, slot=slot,
                status=e.code) from None
        except CapabilityError:
            raise
        except Exception as e:
            raise CapabilityError("offline", "连不上 %s（%s）" % (self.base_url, e),
                                  backend_id=self.backend_id, slot=slot) from None


def _header_safe(token: str, what: str, backend_id: str, slot: str) -> str:
    """令牌里混进非 ASCII / 控制字符时，**在这里就说清楚**。

    这不是多余的校验：HTTP 头只能是 latin-1。让一个带中文的令牌走到 `urlopen` 里，
    抛出来的是 `'latin-1' codec can't encode characters…`，而我们的兜底会把它报成
    "连不上 <地址>" —— 一个**指错方向的诊断**（网络好好的，是令牌抄错了）。
    实测踩到过（用例里我随手写了句中文件当令牌）。
    """
    bad = [ch for ch in token if not (0x20 < ord(ch) < 0x7f)]
    if bad:
        raise CapabilityError(
            "blocked",
            "%s 里有不能放进 HTTP 头的字符（%r…）：多半是复制时带进了中文、全角空格或换行。"
            "重新复制一次" % (what, "".join(bad[:3])),
            code="bad_request", backend_id=backend_id, slot=slot)
    return token


def _pairing_failure(e, backend_id: str, slot: str) -> CapabilityError:
    """换令牌失败 → 路由能用的 `CapabilityError`。

    **这里是"reason 与 code 各管一件事"的又一个实例**（见 `base.SERVER_CODE_TO_REASON`）：
    换令牌失败有两种完全不同的成因，而它们对路由的意义相反 ——

      * `offline`：**那台机器不可达** → 这一轮该跳过它（可重试）；
      * `unauthorized`/`forbidden`：**凭据不被接受** → 跳过它，并且**别重试**，
        要去让人重新配对。把它当成 offline 会让客户端一直拿废凭据去撞。

    所以 `PairingError.code` 只要本身就是权威词汇（`absent`/`offline`）就直接用，
    否则过 `SERVER_CODE_TO_REASON` 那张表翻；两张表都不认识的一律 `error`
    —— 宁可信少一点，也不按猜出来的原因做"换后端/不重试"这种有副作用的决定。
    """
    code = str(getattr(e, "code", "") or "")
    reason = code if code in SKIP_REASONS else SERVER_CODE_TO_REASON.get(code, "error")
    hint = "—— 需要重新配对" if reason == "blocked" else "—— 这一轮先用别的后端"
    return CapabilityError(reason,
                           "后端凭据不能用：%s%s" % (e, hint),
                           code=code or "unauthorized",
                           backend_id=backend_id, slot=slot,
                           status=int(getattr(e, "status", 0) or 0),
                           retry_after=getattr(e, "retry_after", None))


def _maybe_json(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def _new_request_id() -> str:
    import uuid
    return uuid.uuid4().hex[:16]


# `urllib.parse.quote` 要用到，但只在这一个地方 —— 放模块底下省一次 import 开销
import urllib.parse  # noqa: E402


def configured() -> bool:
    """本机**配没配**这个后端：设置里填了地址，**或者配对过**。

    与"连得上吗"无关 —— 它回答的是"这台机器到底认不认一个能力后端"。
    为什么要一个公开入口：`app/meeting.py` 要判断"该不该为'退回了本机'留一句话"。
    以前那里只看 `capabilityEchoServerUrl` 非不非空，而"只配对、什么都没配"
    从 §2.7 起就是一种**能用状态** —— 于是那种机器掉了后端会一声不响地退回本机。
    """
    return bool(_base_url_from_settings())


def client_from_settings() -> Optional[EchoServerClient]:
    """按设置造一个客户端；**没配地址就返回 None**（不是造一个必然失败的）。

    地址的来源有两个（设置里填的、配对时记下的），`_base_url_from_settings` 已经
    把顺序定好了 —— 所以"只配对、什么都没配"也是一种能用状态。
    """
    url = _base_url_from_settings()
    if not url:
        return None
    return EchoServerClient(url)
