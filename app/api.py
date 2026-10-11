# -*- coding: utf-8 -*-
"""api.py — ECHO REST API（面板 / 手机 App / DSH skill / CLI 的统一入口）

鉴权：`apiAuthEnabled=false`（默认）时全开放（此时只绑回环，见 `app/netguard.py`）；
开启后**网上来的**请求都要 `Authorization: Bearer <token>`（`api_keys` 表），
而**本机回环**的请求仍然不必带令牌 —— 否则本地面板自己就瞎了（面板是静态页，没有令牌可带，
于是连"生成配对码"都点不动）。判据见 `optional_auth()`。
"""
import json
import os
import re
import tempfile
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel
from starlette.background import BackgroundTask

import app.db as db
from app import __version__ as _ECHO_VERSION
from app.config import settings
from app import assistant, manager, meeting, netguard, phone_pair, ports, runtime, services, worklog
from app.audio import recorder
from app.audio import stt as stt_mod
from app.audio import tts as tts_mod
from app.pathutil import safe_under as _safe_under

router = APIRouter(prefix="/api")


def _echo_version():
    """ECHO 版本号（唯一权威来源是 app/__init__.py:__version__）。

    以前这里和 app/main.py 各写一份字面量，发版时对不上号 —— 面板显示的版本
    与 tag / 交付包名不一致，报障时无法确认用户跑的是哪一版。见 REFACTOR-PLAN §13.3。
    """
    return _ECHO_VERSION


# ---------------------------------------------------------------- 音频转 16k wav（外部转写用）
def _audio_to_wav16k(src_path, dst_path):
    """任意音频（wav/mp3/flac…）→ 16kHz 单声道 PCM wav。"""
    import soundfile as sf
    import soxr
    data, sr = sf.read(src_path, dtype="float32", always_2d=True)
    if data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    mono = data[:, 0]
    if sr != 16000:
        mono = soxr.resample(mono, sr, 16000)
    sf.write(dst_path, mono, 16000, subtype="PCM_16")


# ---------------------------------------------------------------- 鉴权
#: 网上来的调用**一律拒收**的动作（手机 / 手表触点，设计 §6-A 的"默认拒绝"）。
#: 判据是"**这次调用不是从本机发起的**"，不是"令牌叫什么名字" —— 换个名字就能绕过的判据不算判据。
#: 清单**刻意保持短**：每一条都要能说出"手机凭什么不该做这件事"。
#: （`clean-short` 与 `settings/reset` 是同两类的**批量/一键**版本 ——
#:   只挡单个删除/单键修改，等于留了两个更狠的入口。）
_NETWORK_DENIED = (
    ("POST", re.compile(r"^/api/control/echo/stop$"), "不允许从手机/手表停掉 ECHO"),
    ("POST", re.compile(r"^/api/system/restart$"), "不允许从手机/手表重启 ECHO"),
    ("DELETE", re.compile(r"^/api/meetings/[^/]+$"), "不允许从手机/手表删除会议"),
    ("POST", re.compile(r"^/api/meetings/clean-short$"), "不允许从手机/手表批量删会议"),
    ("PUT", re.compile(r"^/api/settings$"), "不允许从手机/手表改设置"),
    ("POST", re.compile(r"^/api/settings/reset$"), "不允许从手机/手表重置设置"),
)


def _is_loopback_call(request: Request) -> bool:
    """这次请求**确实来自本机**（Host 是回环 **且** 对端是回环）。

    为什么两条都要判（2026-10-09 手机触点）：

      * 只看 Host：DNS Rebinding 会骗过它（恶意域名解析到本机，浏览器发的 Host 是那个域名）——
        那种请求会先被 `netguard.local_only_guard` 拦掉，这里是第二道；
      * 只看对端：同机上任何进程都算"本机" —— 本机进程就是这台机器的用户自己，可以接受。

    判不出对端时按"**不是**本机"处理（见 `netguard.is_loopback_peer()` 的 fail-closed 说明）。
    """
    host = request.headers.get("host", "")
    peer = getattr(getattr(request, "client", None), "host", "") or ""
    if not netguard.is_loopback_host(host):
        return False
    return netguard.is_loopback_peer(peer)


def _network_denied(request: Request) -> str:
    """网上来的调用是否命中拒收清单；命中就回一句能直接显示给用户的原因。"""
    path = request.url.path.rstrip("/") or "/"
    for method, pattern, why in _NETWORK_DENIED:
        if request.method == method and pattern.match(path):
            return "forbidden: %s（本机面板不受影响）" % why
    return ""


def optional_auth(request: Request, authorization: str = Header(default="")):
    """默认档（`apiAuthEnabled=false`）全开放；打开后要求 Bearer 令牌。

    **本机调用豁免**（2026-10-09 手机触点）：从回环发起的请求不必带令牌。不这么做的话，
    `serverBindMode=lan` 强制打开鉴权之后**本地面板自己就瞎了**（面板是静态页，没有令牌可带），
    用户连"生成配对码"都点不动 —— 而这一项要挡的本来就是**网上的人**。
    判据只有一处：`_is_loopback_call()`。

    **网上来的调用**另有一张拒收清单（`_NETWORK_DENIED`）：手机 / 手表这类触点
    不该能停服、重启、删会议、改设置 —— 令牌丢了也不至于把机器搞没。
    """
    if not settings.get("apiAuthEnabled", False):
        return None
    from_this_machine = _is_loopback_call(request)
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    row = db.verify_api_key(token) if token else None
    if row is None and from_this_machine:
        return None                      # 本机 + 不带令牌 → 放行（回环本来就是本机用户）
    if not row:
        raise HTTPException(status_code=401, detail="无效或缺失 API 密钥")
    if not from_this_machine:
        denied = _network_denied(request)
        if denied:
            raise HTTPException(status_code=403, detail=denied)
    return row


# ---------------------------------------------------------------- 路径安全
# 2026-09-13 安全审计 HIGH-1：所有"由请求参数拼出来的文件路径"都必须过这里。
# os.path.join 本身不做任何净化：第二个参数是绝对路径时会**整段替换**前面的目录，
# 含 `..` 时也会逃出去。所以拼完必须用 realpath 复核最终落点。
_MEETING_FILE_KINDS = {"transcript", "topics", "summary"}


def _meeting_dirname(name) -> str:
    """会议目录名：只取最后一段，杜绝 name 里混入分隔符或 ..（库里的值正常不会）。"""
    return os.path.basename(str(name or "").replace("\\", "/").rstrip("/"))


# ---------------------------------------------------------------- 模型
class CommandIn(BaseModel):
    text: str
    source: str = "web"
    workspace: str = ""
    session_id: str = ""


class CaptureIn(BaseModel):
    source: str = "api"


class TtsIn(BaseModel):
    """`POST /api/tts` 的入参（手机 / 手表触点）。

    格式**不由调用方选**：edge-tts 出 mp3、离线引擎出 wav，响应头的 `Content-Type` 会说清
    （手机侧两个都能播）。**不加 `format` 参数**是故意的 —— 一个改不动的旋钮比没有更坏。
    """
    text: str = ""
    #: 留空 = 用设置里的 `ttsEngine`；显式给 `off` → **409**（与"合成失败"分开）
    engine: str = ""


class DailyReviewStartIn(BaseModel):
    force_new: bool = False


class DailyReviewSubmitIn(BaseModel):
    text: str
    force_new: bool = False


class SettingsIn(BaseModel):
    values: dict


class SpeakerRenameIn(BaseModel):
    label: str
    name: str


class SpeakerMergeIn(BaseModel):
    source: str
    target: str


class LineUpdateIn(BaseModel):
    text: str


class LineKindIn(BaseModel):
    kind: str


class SummaryRegenIn(BaseModel):
    extra: str = ""


class SummarySaveIn(BaseModel):
    """人工编辑纪要正文（面板纪要页签「编辑」→ 保存）。"""
    content: str


class MeetingTitleIn(BaseModel):
    """人工改会议名；空串＝清空自定义命名，回落时间戳文件名。"""
    title: str = ""


class CleanShortIn(BaseModel):
    max_minutes: float = 2


class ModelDownloadIn(BaseModel):
    id: str
    force: bool = False      # 已就绪时只有 force=True 才重下（界面上的「重新下载」）


class ModelCleanupIn(BaseModel):
    """清理请求：**必须点名**（先预览、后确认）。`days` 省略时用设置 `modelCleanupDays`。"""
    ids: list = []
    days: int = 0


class ModelPinIn(BaseModel):
    """「保留」钉子：钉住 = 永久不列入清理建议。"""
    id: str
    pinned: bool = True


class WorklogIn(BaseModel):
    # 面板「归档要求」自由文本；旧字段名 project 继续兼容（同一个语义位置）
    archive_hint: str = ""
    project: str = ""


class VoiceprintEnrollIn(BaseModel):
    """把某场会议某说话人的声音入库到联系人名下。"""
    meeting_id: int
    label: str
    name: str


class KeyCreateIn(BaseModel):
    name: str = "mobile"


class PhoneClaimIn(BaseModel):
    """设备用一张**一次性配对码**换令牌（手机 / 手表触点）。

    `name` 是设备自报的名字，只用来在面板的「已配设备」列表里认出"这是哪一台"
    （默认 `mobile`，与 `KeyCreateIn.name` 同一个默认值）。
    """
    code: str = ""
    name: str = "mobile"


class PairBackendIn(BaseModel):
    """配对一台 ECHO 能力后端。

    `client_name` 是**对端自报的名字**，服务端只在配对码上没写名字时才用它
    （管理员在码上填的名字是资产清点，不能被自报的盖掉，见服务端 §7.4）。
    """
    base_url: str = ""
    code: str = ""
    client_name: str = ""
    # 配对串里的 `fp=sha256:…`（设计 §7.5 ①）。给了就必须对上 —— 对不上直接拒绝配对，
    # 那才是"防中间人"的那一步；留空 = TOFU。
    fingerprint: str = ""


class PairLocalIn(BaseModel):
    """**本机自动配对**（方案 1）。`path` 只在排障时给 —— 正常留空，走约定位置。"""
    path: str = ""


class BackendStartIn(BaseModel):
    """「起本机后端」（批 1d）点下去时的选项。

    `replace_pairing`：这台机器已经配对到**别的**后端时，要不要覆盖那个配对。
    默认 **False** —— 那可能是人家正在用的 GPU，悄悄换掉的表现是"我连的 GPU 忽然变了"。
    """
    replace_pairing: bool = False
    #: 显存预算（MB，0 = 不限制）。写进生成的 `server.yaml`；小卡上必须填，见实施方案 §6-4。
    vram_budget_mb: int = 0
    device: str = "cuda"


# ---------------------------------------------------------------- 能力路由（3.0）
#
# 这几个端点背后的活都在 `app/capability_admin.py`。刻意**不在 api.py 里算**：
# 页面要的是一份"后端们现在是什么样"的快照，而这份快照的每一格都来自后端自己的
# `describe()` —— 在路由层再算一遍就会出现两种说法（见那个模块开头的说明）。

@router.get("/capability")
def api_capability(_auth=Depends(optional_auth)):
    """「能力路由」页签的全部数据：配对状态 + 每个后端 + 每个用途选的哪个后端。"""
    from app import capability_admin
    return capability_admin.view()


@router.post("/capability/probe")
def api_capability_probe(_auth=Depends(optional_auth)):
    """立即重问一遍所有后端（面板上的「刷新」）。

    与 `GET /capability` 的区别只有一个：**这次真的去问**（不吃 30 秒的缓存）。
    失败**不报错** —— 后端连不上正是这个按钮要告诉人的事，它会体现在 `describe()` 里。
    """
    from app import capability_admin
    ok, payload = capability_admin.probe()
    if not ok:
        raise HTTPException(status_code=503, detail=payload.get("error") or "刷新失败")
    return payload


@router.post("/capability/pair")
def api_capability_pair(body: PairBackendIn, _auth=Depends(optional_auth)):
    """用配对码换凭据并落盘（`{DATA}/backend.json`，Windows 走 DPAPI）。

    **失败回 400 + 一句人话**，不是 500：配对码抄错、机器没开、码用过了，
    这些都是日常，界面要能直接显示出来。
    """
    from app import capability_admin
    ok, message = capability_admin.pair(body.base_url, body.code,
                                        client_name=body.client_name,
                                        cert_fingerprint=body.fingerprint)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "pair": capability_admin.pair_view()}


@router.post("/capability/pair-local")
def api_capability_pair_local(body: Optional[PairLocalIn] = None,
                              _auth=Depends(optional_auth)):
    """**方案 1：本机自动配对**（2026-09-28）。

    同机装了后端时，客户端不该让人抄配对码：后端启动会把配对信息写在自己状态目录里，
    这里读它 → 走**原来那条** `pairing.pair()` → 落 `{DATA}/backend.json`。
    产物与手工粘贴配对串**逐字一致**（同一套凭据信封、同一个 TLS 指纹固定）。

    失败仍回 400 + 一句人话：找不到文件、文件过期、后端没跑起来，都是日常。
    """
    from app import capability_admin
    ok, message = capability_admin.pair_local((body.path if body else "") or "")
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "pair": capability_admin.pair_view(),
            "local": capability_admin.local_pair_view()}


@router.post("/capability/unpair")
def api_capability_unpair(_auth=Depends(optional_auth)):
    """解除配对（只忘掉本机凭据）。"""
    from app import capability_admin
    ok, message = capability_admin.unpair()
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "pair": capability_admin.pair_view()}


# ---------------------------------------------------------------- 「起本机后端」（批 1d）
#
# 背后是 `app/backend_admin.py`（状态整理 + 后台线程里的编排）。三个端点的分工：
#   GET  .../backend        现在什么状态、能点什么、为什么不能点
#   POST .../backend/start  开始（**立刻返回**，进度在 job 里，面板轮询 GET 看它长）
#   POST .../backend/stop   停 **ECHO 自己起的** 那个（手工起的实例不动）

@router.get("/capability/backend")
def api_capability_backend(_auth=Depends(optional_auth)):
    """「起本机后端」卡片要的全部状态（含正在跑的那次任务的进度）。"""
    from app import backend_admin
    return backend_admin.view()


@router.get("/capability/backend/plan")
def api_capability_backend_plan(force: bool = False, _auth=Depends(optional_auth)):
    """**只读计划**：这台机器该走哪条路（容器 / 扩展包）、哪一档、多大、缺什么、
    要不要联网、先验哪一步（实施方案 §2：先给人看，再点开始）。

    `force=1` 不吃缓存（面板上那个"重新探一遍"）。它只读、不动任何东西，所以随时可问。
    """
    from app import backend_env
    return backend_env.plan(force=bool(force))


@router.post("/capability/backend/start")
def api_capability_backend_start(body: Optional[BackendStartIn] = None,
                                 _auth=Depends(optional_auth)):
    """开始起本机后端。**不等它做完** —— 进度看 `GET /api/capability/backend`。"""
    from app import backend_admin
    data = body or BackendStartIn()
    ok, message = backend_admin.start(replace_pairing=bool(data.replace_pairing),
                                      vram_budget_mb=int(data.vram_budget_mb or 0),
                                      device=str(data.device or "cuda"))
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "backend": backend_admin.view()}


@router.post("/capability/backend/stop")
def api_capability_backend_stop(_auth=Depends(optional_auth)):
    """停掉 ECHO 自己起的那个后端（手工起的实例一个字节都不动）。"""
    from app import backend_admin
    # 2026-10-11：**明确停 = 听用户的** —— 通知看门狗别在 30 秒后又把它拉回来。
    # 没有这一条会有两个后果：① 用户点「停止后端」被无声推翻（过一会儿它又起来了）；
    # ② 按 AGENTS.md 的纪律"跑门禁前先停后端"时，8900/8901 抢不到空 → 门禁根本没法跑。
    try:
        from app import backend_watch
        backend_watch.pause("你点了「停止后端」")
    except Exception:
        pass
    ok, message = backend_admin.stop()
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "backend": backend_admin.view()}


@router.post("/capability/backend/take-over")
def api_capability_backend_take_over(body: Optional[BackendStartIn] = None,
                                     _auth=Depends(optional_auth)):
    """**接管后端**：停掉占着端口的「另一棵树的后端」，换成当前这棵树自己的。

    为什么单开一个端点而不是复用 start（2026-10-06 用户报的 bug）：开发版与稳定版
    轮流跑、**共用 8900/8901**，而切实例时后端不会跟着换 —— 于是新树起来后端口上
    跑的还是旧树的后端，转写拿 `unauthorized`，面板却说「待启动」。
    这个端点做的是"停旧 → 起自己的 → 配对照旧跳过（不重新配对）"。
    **只停命令行证明是 ECHO 后端、且目录属于别的树的那个**；不明进程绝不动手。
    """
    from app import backend_admin
    data = body or BackendStartIn()
    ok, message = backend_admin.take_over(vram_budget_mb=int(data.vram_budget_mb or 0),
                                          device=str(data.device or "cuda"))
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "backend": backend_admin.view()}


@router.post("/capability/backend/ready")
def api_capability_backend_ready(diarize: bool = False, _auth=Depends(optional_auth)):
    """**三层就绪自测**（批 3）：`/v1/health` → `/v1/ready` → **一次真实 `/v1/asr`**。

    最后那一层是这一档存在的理由：只有它挡得住"health 全绿、每个 /v1/asr 都 503"
    那个坑（权重缺失 / torch 与 torchaudio 的 CUDA ABI 不符）。
    `diarize=1` 时顺带问一次分离，**失败不算整体失败**（老卡本来就没有这一档）。

    失败回 400 + 那句话（面板照原样显示），并且**结论照样进 `view()["ready"]`**。
    """
    from app import backend_admin
    ok, message = backend_admin.ready_probe(diarize=bool(diarize))
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "ready": backend_admin.last_ready()}


@router.get("/capability/backend/compose")
def api_capability_backend_compose(_auth=Depends(optional_auth)):
    """**预览**容器路的 `compose.yaml`（批 4）：不写盘、不起容器，只给人看。

    返回渲染出来的原文 + 起它的命令 + **必须做的验收**（本机 `compose config`；
    **另一台机器** `curl http://<本机 IP>:8900/v1/health` 必须连不上 —— 那才是
    "只绑了回环"的判据，实施方案 §5 的批 4 验收写的就是它）。
    """
    from app import backend_docker
    return {"available": backend_docker.available(),
            "path": backend_docker.compose_path(),
            "text": backend_docker.render_compose(),
            "commands": backend_docker.commands(),
            "verify": backend_docker.verify_notes()}


@router.post("/capability/backend/compose")
def api_capability_backend_compose_write(_auth=Depends(optional_auth)):
    """把容器路的 `compose.yaml` **写下来**（内容变了先备份）。不起容器。"""
    from app import backend_docker
    info = backend_docker.write_compose()
    if not info.get("ok"):
        raise HTTPException(status_code=400, detail=info.get("detail") or "写 compose 失败")
    return {"ok": True, "message": info["detail"], "path": info["path"],
            "commands": backend_docker.commands(), "verify": backend_docker.verify_notes()}


# ---------------------------------------------------------------- 状态与配置
@router.get("/status")
def api_status(_auth=Depends(optional_auth)):
    from app import agents
    selected = agents.selected_name()
    dsh_ok = manager.dsh_ready()
    if selected and selected != "dsh":
        # 用户选的是**别的**智能体：不要拿桌面版的探活结果去覆盖组件状态。
        # 那会把 boot 写好的「未使用」又改回 offline，于是 /api/status 与 /api/boot/status
        # 对同一件事两种说法（2026-09-22 同事反馈 3.4）；折叠条也从这里取状态。
        services.report_dsh("skipped",
                            "未使用（你选的是 %s）" % agents.meta(selected)["displayName"])
    else:
        services.report_dsh("online" if dsh_ok else "offline",
                            "API 可访问" if dsh_ok else "未运行")
    components = services.snapshot()
    # 2026-10-11（用户实测的"状态格陈旧 online 谎报"）：**harness 的状态行要在读取时校准一次**。
    # 上面那两行的设计是"状态取自组件表、不在这里探活"（对，高频轮询 + codebuddy 那种
    # 探一次就起一个进程的适配器不能被拖进来），但 harness 的探活只是**一次 HTTP 探测**
    # （`online()`，1 秒超时、不起进程）。不校准的后果是：**别人把 harness 停了**
    # （切换器切树 / 手工杀 / 它自己崩了）之后没有任何人重写那一行，于是 43199 明明空着，
    # 面板与折叠条还一直显示"运行中" —— 用户实测撞到过。
    # ⚠️ 只有 harness 走这条（dsh 的探活已经在上面 `report_dsh` 里做过；codebuddy 绝不能碰）。
    if selected == "harness":
        try:
            from app import harness_proc
            harness_proc.sync_status()
            components = services.snapshot()
        except Exception:
            pass                       # 校准失败就用旧值，绝不因为一句话的状态把接口弄挂
    # 当前选中的智能体：折叠条/面板据此显示"我用的那个"，而不是永远盯着 DSH Desktop。
    # 状态取自组件表（boot 与各适配器往里写）；**不在这里探活** —— 这个接口被高频轮询，
    # 而 codebuddy 那种"每次调用起一个进程"的适配器探一次就是要起进程。
    agent_info = dict(agents.meta(selected))
    mine = next((c for c in components if c.get("name") == selected), None)
    agent_info["status"] = (mine or {}).get("status") or "unknown"
    agent_info["detail"] = (mine or {}).get("detail") or ""
    agent_info["online"] = agent_info["status"] == "online"
    st = meeting.meeting_status()
    # 转写引擎加载状态（detail 展示）
    stt_st = stt_mod.engine_status()
    loaded_desc = ", ".join(f"{e.get('engine')}" for e in stt_st["loaded"]) or "未加载"
    services.report_stt("online" if stt_st["loaded"] else "ready",
                        f"{loaded_desc} · {stt_st['device']}")
    return {
        "components": components,
        # 顶层 dsh = DSH **桌面版**适配器。skipped 表示"用户选了别的智能体、这个没在用"，
        # 让消费者能区分"它没选"与"它坏了"（折叠条曾把前者显示成红灯 DSH×）。
        "dsh": {"online": dsh_ok, "skipped": bool(selected and selected != "dsh")},
        "agent": agent_info,
        "stt": stt_st,
        "meeting": st,
        "busy": assistant.is_busy(),
        "busyOwner": assistant._busy_owner["name"],
        # 启动期自愈留痕（如清理被强杀留下的 WAL）：面板只提示一次，见 web/app.js
        "startupNotes": db.startup_notes(),
        # 命令流当前阶段（listening/transcribing/running）：面板据此给"说话"按钮做动效
        "busyPhase": assistant._busy_owner.get("phase"),
        "uptime": services.uptime(),
        "version": _echo_version(),
    }


# ---------------------------------------------------------------- 模型路由（只读转发 + 组管理）
# 路由进程地址不在此处写死：由 app/router_admin.py 经 llm_router.route_base_url()
# 在调用时按 dsh-failover/config.json 的 port 求值。未运行时不报错，返回 offline 结构。


@router.get("/failover/health")
def api_failover_health(_auth=Depends(optional_auth)):
    """路由进程 /health 的只读转发（端点名沿用旧称，面板小卡片与折叠条在用）。"""
    from app import router_admin
    return router_admin.health()


class RouterMembersIn(BaseModel):
    members: list


class RouterMetaIn(BaseModel):
    display_name: str = ""


@router.get("/router/status")
def api_router_status(_auth=Depends(optional_auth)):
    """模型路由总览：组成员（配置+健康）、DSH 注册态、可勾选候选模型。"""
    from app import router_admin
    return router_admin.members_view()


@router.put("/router/members")
def api_router_members(body: RouterMembersIn, _auth=Depends(optional_auth)):
    """保存模型组成员（列表顺序即优先级）→ 热重载路由 → 同步注册进 DSH。"""
    from app import router_admin
    ok, detail = router_admin.save_members(body.members)
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "message": detail}


@router.put("/router/meta")
def api_router_meta(body: RouterMetaIn, _auth=Depends(optional_auth)):
    """改模型组显示名（= DSH 里看到的模型名）。"""
    from app import router_admin
    ok, detail = router_admin.save_group_meta(display_name=body.display_name.strip() or None)
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "message": detail}


@router.post("/router/probe")
def api_router_probe(_auth=Depends(optional_auth)):
    """立即探测所有成员（不等下一轮后台探测）。"""
    from app import router_admin
    ok, detail = router_admin.probe_now()
    if not ok:
        raise HTTPException(status_code=503, detail=f"路由未运行或探测失败：{detail}")
    return {"ok": True, "groups": detail.get("groups", [])}


@router.post("/router/reload")
def api_router_reload(_auth=Depends(optional_auth)):
    """让路由进程重读 config.json（改配置后不必重启）。"""
    from app import router_admin
    ok, detail = router_admin.reload_router()
    if not ok:
        raise HTTPException(status_code=503, detail=f"路由未运行或重载失败：{detail}")
    return {"ok": True, "message": detail}


@router.post("/router/register")
def api_router_register(_auth=Depends(optional_auth)):
    """手动把当前模型组注册进 DSH（等于 ECHO 启动时自动做的那一步）。"""
    from app import router_admin
    ok, detail = router_admin.register()
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "message": detail}


@router.get("/settings")
def get_settings(_auth=Depends(optional_auth)):
    """配置项列表 + 智能体状态。

    「智能体」分组的配置项在 config.py 里标了 hidden=True，不会出现在 settings 里，
    由面板的智能体表格渲染（数据来自下面的 agents 字段）。
    """
    items = settings.all()
    agents_payload = None
    try:
        from app import agents
        agents_payload = agents.list_agents()
    except Exception:
        pass
    return {"settings": items, "agents": agents_payload}


@router.put("/settings")
def put_settings(body: SettingsIn, _auth=Depends(optional_auth)):
    updated = settings.update(body.values)
    # 配置变更后的联动：wake / router / 智能体-harness。
    # **抽到 app/settings_effects.py**：这段原来只长在这里，于是不走这个 HTTP 接口的写入
    # 都享受不到它 —— 向导执行相就是调 `settings.update()` 的，实测"在向导里选了标准版"
    # 从来没把 harness 拉起来（2026-09-20）。
    from app import settings_effects
    effects = settings_effects.apply(updated)
    for eff in effects:
        if eff["scope"] == "router" and not eff["ok"]:
            # 路由配置没应用上要明确失败（原来就是 400）；其余联动只记日志，
            # 选择已经生效，只是服务/进程没起来 —— 面板的「检测」会显示原因。
            raise HTTPException(status_code=400, detail=f"路由配置未能应用：{eff['detail']}")
        if not eff["ok"]:
            print("[api] %s 联动未成功: %s" % (eff["scope"], eff["detail"]))
    # effects 一起回给面板：像「选的转写引擎没装」这种，用户必须**当场**知道，
    # 不然要等说第一句命令才遇到静默失败（2026-09-23 事故）。
    return {"ok": True, "updated": updated, "effects": effects}


@router.get("/agents")
def get_agents(probe: bool = False, _auth=Depends(optional_auth)):
    """智能体列表：启用态 + 可用性。

    probe=1 时做重探测（CodeBuddy 会真的执行一次 CLI --version），
    以便面板上的「检测」按钮给出确定结论。
    """
    from app import agents
    items = agents.list_agents(probe=probe)
    return {
        "agents": items,
        "active": next((a["name"] for a in items if a["active"]), agents.DEFAULT_AGENT),
        "options": agents.product_options(),
    }


@router.post("/harness/browser")
def post_harness_browser(_auth=Depends(optional_auth)):
    """在浏览器里打开独立 harness 的 Web 界面（仪表盘「超级助理」名字后那个小图标）。

    **登录一律走签名 Cookie，先写 Cookie 再开页面**（2026-10-07 用户实测后定的）：

    为什么不再用 `?token=…`：token **跟着那个进程**，ECHO 每重启一次 harness 就换一枚。
    旧 token 打开的页面**能加载、但鉴权不过** —— 侧栏没有「工作区」、没有会话列表，
    而且**没有任何提示**，用户只能看到一片空白（连查两轮才定位到这条）。
    签名 Cookie 的密钥在**文件**里（`<家目录>/.credentials.yaml`），**不随重启失效**，
    所以一次点开长期可用。

    仍由**服务端**打开、而不是把凭据下发给页面：Cookie 是密钥，经接口下发等于把它
    写进前端内存与浏览器历史。响应里只回地址，不回凭据。
    """
    from app import harness_proc
    if not harness_proc.online():
        if not harness_proc.requested():
            return {"ok": False,
                    "message": "标准版 harness 没在运行：在 设置 → 智能体 里选中"
                               "「标准版 harness」，ECHO 会自动拉起它"}
        return {"ok": False, "message": "独立 harness 没在监听 %s（看 data/logs/harness.log）"
                                        % harness_proc.base_url()}
    url = harness_proc.base_url() + "/"

    # ① 正路：密钥铸 Cookie → 独立 profile 的 Edge 注入 Cookie 后打开
    cookie = harness_proc.secret_cookie()
    detail = ""
    if cookie:
        from app import browser_open
        ok, detail = browser_open.open_with_cookie(url, cookie)
        if ok:
            return {"ok": True, "url": url,
                    "message": "已打开 %s（已自动登录）" % url}
    else:
        detail = "读不到 harness 家目录里的 browser-session 密钥（%s）" % harness_proc.home()

    # ② 退路：拿不到密钥时，仍然可以用**验证过的** token URL 打开（会随重启失效）
    from app import platform as echo_platform
    tok = harness_proc.token()
    if not harness_proc.token_works(tok):
        tok, note = harness_proc.ensure_token()
        if not harness_proc.token_works(tok):
            return {"ok": False,
                    "message": "打不开：%s；也拿不到能用的登录 token（%s）"
                               % (detail or "Cookie 注入失败", note or "已失效")}
    if echo_platform.shell_open(url + "?token=%s" % tok):
        return {"ok": True, "url": url,
                "message": "已打开 %s（用登录 token；重启 ECHO 后需再点一次）" % url}
    return {"ok": False, "message": "打开浏览器失败（%s）%s"
                                    % (url, ("；%s" % detail) if detail else "")}


@router.post("/settings/reset")
def reset_settings(key: str = "", _auth=Depends(optional_auth)):
    settings.reset(key or None)
    return {"ok": True}


# ---------------------------------------------------------------- 命令
@router.post("/assistant/command")
def post_command(body: CommandIn, _auth=Depends(optional_auth)):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="命令为空")
    ok, msg = assistant.send_text(body.text.strip(), source=body.source,
                                  workspace=body.workspace or None,
                                  session_id=body.session_id or None)
    return {"ok": ok, "message": msg}


@router.post("/assistant/voice-command")
async def assistant_voice_command(request: Request,
                                  engine: str = "",
                                  lang: str = "",
                                  _auth=Depends(optional_auth)):
    """**裸 wav → 一句话**（手机 / 手表的一步到位入口）：转写 → 下发给智能体。

    为什么要有它（设计 §3 新增 2）：手机侧两步也能拼
    （`POST /api/stt/transcribe` + `POST /api/assistant/command`），但多一次往返；
    这里在**服务端**把那两步串起来，**不新增业务逻辑**。

    约定与能力后端一致（AGENTS.md 记过这条坑）：**裸音频 body + `Content-Type: audio/wav`**，
    **不是** multipart（那在这套里会得到 415）。

    `source` 记 `"mobile"` —— 库里注释与面板翻译表早就备好这个值，手机/手表是第一处真用它的人。
    回包给 `commandId`：手机拿到就能去 `GET /api/commands?limit=` 轮询那一条的 status
    （设计 §7 的关键时序）。
    """
    body = await request.body()
    if not body:
        raise HTTPException(status_code=422,
                            detail="请求体为空：请把 wav 字节直接当 body 发（Content-Type: audio/wav）")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="音频不能超过 512 MB")
    tmp_in = tmp_wav = ""
    try:
        fd, tmp_in = tempfile.mkstemp(prefix="echo-voice-", suffix=".bin")
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        fd2, tmp_wav = tempfile.mkstemp(prefix="echo-voice-", suffix=".wav")
        os.close(fd2)
        from starlette.concurrency import run_in_threadpool
        try:
            await run_in_threadpool(_audio_to_wav16k, tmp_in, tmp_wav)
        except Exception as exc:
            # **入参不对就说入参不对**（2026-10-09，用例逮到的）：multipart 传进来的 body
            # 不是音频字节，`_audio_to_wav16k` 会报 "Format not recognised" —— 若把它当
            # "转写失败(502)"报出去，调用方会去查引擎，而真正该改的是请求形状。
            raise HTTPException(
                status_code=422,
                detail="音频解码失败（%s）：请把 wav 字节**直接当 body** 发"
                       "（Content-Type: audio/wav），不要用 multipart/form-data" % type(exc).__name__)
        text = await run_in_threadpool(
            stt_mod.transcribe, tmp_wav,
            engine or str(settings.get("sttModel", "sensevoice") or "sensevoice"),
            "small", lang or str(settings.get("sttLanguage", "zh") or "zh"), "auto")
    except HTTPException:
        raise
    except Exception as exc:
        db.add_log("warn", "api", "voice-command 转写失败：%s: %s" % (type(exc).__name__, exc))
        raise HTTPException(status_code=502,
                            detail="转写失败：%s: %s" % (type(exc).__name__, exc))
    finally:
        for p in (tmp_in, tmp_wav):
            try:
                if p:
                    os.remove(p)
            except OSError:
                pass
    if not (text or "").strip():
        raise HTTPException(status_code=422, detail="未能识别出文本（音频过短或无语音）")
    ok, msg = assistant.send_text(text.strip(), source="mobile")
    cmd_id = None
    try:
        rows = db.list_commands(limit=1)
        if rows:
            cmd_id = rows[0].get("id")
    except Exception:
        pass
    return {"ok": bool(ok), "message": msg, "text": text.strip(),
            "commandId": cmd_id, "source": "mobile"}


@router.get("/dsh/targets")
def get_dsh_targets(_auth=Depends(optional_auth)):
    """命令发送目标：工作区列表 + 会话列表（供前端下拉选择）。

    数据源是**当前选中的智能体**（2026-09-19 修正：原来是固定 DSH Desktop，于是面板里
    选了独立 harness，下拉还列着桌面版的会话）。取不到时**不报 5xx**：面板要能照常显示
    「默认」选项并把原因说出来（例如独立 harness 还没起来 / 没填 token）。
    """
    from app.dsh import get_client, DshError
    try:
        client = get_client()
        agent = getattr(client, "name", "")
        workspaces = client.list_workspaces()
        sessions = client.list_sessions_for()
    except DshError as e:
        return {"workspaces": [], "sessions": [], "agent": agent if "agent" in dir() else "",
                "note": str(e)}
    except Exception as e:                       # 兜底：未知异常也只降级，不让下拉炸掉
        return {"workspaces": [], "sessions": [], "agent": "", "note": str(e)}
    return {"workspaces": workspaces, "sessions": sessions, "agent": agent}


@router.post("/dsh/workspaces/ensure")
def post_dsh_workspaces_ensure(_auth=Depends(optional_auth)):
    """建立/补齐两个默认分组：「会议空间」与「指令空间」。

    安装技能在装完、智能体起来之后调一次 —— 这样用户第一次打开 DSH 侧栏就看到
    两个分组，而不是等开完第一场会 / 说第一句指令才冒出来。幂等：已存在的原样返回，
    用户自己改过名的工作区一律不动（见 app/workspaces.py）。
    """
    from app import workspaces as spaces_mod
    report = spaces_mod.ensure_spaces()
    bad = [it for it in report if it["action"] in ("failed", "no-agent")]
    return {"ok": not bad, "spaces": report,
            "note": "" if not bad else "；".join("%s：%s" % (b["label"], b["detail"]) for b in bad)}


@router.post("/assistant/capture")
def post_capture(body: CaptureIn, _auth=Depends(optional_auth)):
    ok = assistant.capture(body.source)
    return {"ok": ok, "message": "已开始录音命令流" if ok else "已有命令流进行中"}


@router.get("/assistant/busy")
def get_busy(_auth=Depends(optional_auth)):
    return {"busy": assistant.is_busy(), "owner": assistant._busy_owner["name"]}


# ---------------------------------------------------------------- 每日回顾
# 面板侧的三个入口。**语音那条路由**（车里说"我们来回顾今天"）走 assistant 的回顾模式，
# 不经过这里 —— 这里只让用户能在面板上把"今天这条会话"准备好、看状态、或者中途喊停。
@router.get("/daily-review")
def get_daily_review(_auth=Depends(optional_auth)):
    """回顾状态：开关、就绪原因、今天那条会话、笔记库与工作区。

    只读：**不建会话、不改 DSH 权限**（那两件事有副作用，不该被一次 GET 触发）。
    """
    from app import daily_review
    st = daily_review.status()
    st["running"] = assistant.review_running()
    return st


@router.post("/daily-review/start")
def post_daily_review_start(body: DailyReviewStartIn = None, _auth=Depends(optional_auth)):
    """准备今天的回顾会话（幂等）。`force_new=true` 时当天也重建。"""
    from app import daily_review
    force = bool(body and body.force_new)
    res = daily_review.start(force_new=force)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "回顾未就绪")
    return res


@router.post("/daily-review/submit")
def post_daily_review_submit(body: DailyReviewSubmitIn, _auth=Depends(optional_auth)):
    """把一段文本当作"今日口述"提交给回顾会话（面板调试与文本兜底用）。

    返回里的 `spoken` 就是**会被念出来的那句话** —— 面板拿它显示，用户能当场看到
    "车里将听到什么"，这是调播报长度最直接的入口。
    """
    from app import daily_review
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="口述内容为空")
    out = daily_review.submit(text, force_new=bool(body.force_new))
    if not out.get("ok"):
        # 400 还是 200：这里刻意**返回 200 + ok=false**，因为"没等到回复"不是客户端错误，
        # 而且面板要能显示 spoken（那句给用户听的话）。
        return out
    return out


@router.post("/daily-review/stop")
def post_daily_review_stop(_auth=Depends(optional_auth)):
    """停掉正在进行的语音回顾（等价于用户说"结束回顾"）。"""
    stopped = assistant.stop_review()
    return {"ok": True, "stopped": stopped}


@router.post("/daily-review/go")
def post_daily_review_go(_auth=Depends(optional_auth)):
    """**真的开始一场回顾**（面板上那个按钮）—— 与 `/start` 不是一回事。

    2026-10-07 用户实测："我点了开始回顾，提示了回顾已经开始但是没有其他反应了"。

    真因：`/start` 只**建当天那条会话**（`daily_review.start()`），什么都不会跑；
    真正进入回顾模式（开麦克风、一轮轮录口述、念播报）的是 `assistant.start_review()`
    —— 而面板**两个**按钮都只调了 `/start`。所以用户看到一句提示之后，
    既没有录音、也没有任何下一步。**"准备好会话"与"开始回顾"是两个动作，
    按钮要的是后者。**

    返回里带 `running`，面板据此显示"正在回顾"而不是干等。

    **已知边界**：助手正忙（正在跑一条命令 / 正在录音 / 正在开会议）时进不去 ——
    这与语音那条路（喊"我们来回顾今天"）是**同一个** `_set_busy` 闸，不许绕过；
    它会念一句"我这会儿正忙"，面板用 `busyOwner` 说清是谁占着。
    """
    from app import daily_review
    # **先挡"正在录会议"**：回顾要独占麦克风，而 `record_command` 直接开设备、不排他 ——
    # 真在开会时点这个按钮，会等到第一轮录音才失败，用户看到的是"点了、没反应"
    # （2026-10-07 那次现场的体感）。这类冲突要在**按下按钮时就**说清。
    # 判据与"重启 ECHO 前先看有没有在录音"是同一条（`meeting_status()["active"]`）。
    try:
        from app import meeting
        if meeting.meeting_status().get("active"):
            return {"ok": False, "running": False,
                    "error": "正在开会议录音，回顾要独占麦克风 —— 先结束录音再来。"}
    except Exception:
        pass
    res = daily_review.start()
    res["running"] = False
    if not res.get("ok"):
        return res
    res["started"] = bool(assistant.start_review(source="web"))
    res["running"] = assistant.review_running()
    res["busyOwner"] = assistant._busy_owner["name"]
    if not res["running"]:
        res["error"] = ("助手现在正忙（%s），没进回顾模式 —— 等它忙完再点一次。"
                        % (assistant._busy_owner["name"] or "未知"))
    return res


# ---- 回顾历史（2026-10-06）：给仪表盘那张卡与「回顾历史」页签 ----
#
# 数据来自 `commands` 表里 `source='review'` 的行（见 `daily_review._persist` 的说明：
# 复用它而不是另建表 —— 字段正好对，且已有索引与分页）。一轮回顾 = 一行。
@router.get("/daily-review/summary")
def get_daily_review_summary(_auth=Depends(optional_auth)):
    """仪表盘那张卡要的**一小口**：今天做没做、最近一次是什么。

    刻意与 `/daily-review/history` 分开：卡片每次刷新都会问，让它读几百行历史不合适。
    """
    from app import daily_review
    return daily_review.history_summary()


@router.get("/daily-review/history")
def get_daily_review_history(limit: int = 60, _auth=Depends(optional_auth)):
    """回顾历史**按天**倒序（每天一行：轮数、最后一次时间、当天最新播报）。"""
    from app import daily_review
    return daily_review.history_list(limit=limit)


@router.get("/daily-review/history/{date}")
def get_daily_review_history_detail(date: str, _auth=Depends(optional_auth)):
    """某一天的回顾详情：每一轮的原文 + 播报 + 状态，以及当天工作日志的路径。

    日期坏格式返回 **200 + ok=false**（与 `/daily-review/submit` 同一个口径：
    那不是客户端错误，面板要能拿到 `error` 直接显示）。
    """
    from app import daily_review
    return daily_review.history_detail(date)


# ---------------------------------------------------------------- 服务运维
@router.get("/models")
def get_models(_auth=Depends(optional_auth)):
    """模型清单：每个功能需要哪些模型、多大、装到哪、怎么获取，以及本地就绪状态与下载进度。

    只读检测 + 任务状态（app/modelinfo.py），GET 不会触发任何下载；面板「设置 → 模型」据此渲染。
    2026-09-26 起每项还带**使用情况**（`lastUsedAt` / `useCount` / `pinned` / `inUse`）——
    它是「本地能力」区"默认展开哪些"与「清理」卡"哪些不许删"的唯一判据（见 app/model_usage.py）。
    """
    from app import modelinfo
    return {"items": modelinfo.inventory(), "jobs": modelinfo.jobs()}


@router.get("/models/cleanup/preview")
def get_model_cleanup_preview(days: int = 0, _auth=Depends(optional_auth)):
    """清理**预览**：每项的名称/占用/上次使用/是否在用 + 建议删哪些。**一个字节都不动盘。**

    `days` 省略（0）时用设置 `modelCleanupDays`（出厂 90 天）。
    """
    from app import model_cleanup
    return model_cleanup.preview(days=days or None)


@router.post("/models/cleanup")
def post_model_cleanup(body: ModelCleanupIn, _auth=Depends(optional_auth)):
    """按点名删除模型（**先预览、后确认**的那一步）。

    只删 `body.ids` 里点名的那些，并且**每一项都重新过一遍保护判据**（在用 / 保留钉子 /
    当前配置选中的一律拒绝，原因如实回）。回报里带每个模型释放的精确字节数与失败原因 ——
    "删了什么、释放多少"不许含糊（用户要求如实回报）。
    """
    from app import model_cleanup
    return model_cleanup.execute(body.ids, days=body.days or None)


@router.post("/models/pin")
def post_model_pin(body: ModelPinIn, _auth=Depends(optional_auth)):
    """给模型打/摘「保留」钉子（永久不列入清理建议）。"""
    from app import model_cleanup
    return model_cleanup.pin(body.id, pinned=bool(body.pinned))


@router.post("/models/download")
def post_model_download(body: ModelDownloadIn, _auth=Depends(optional_auth)):
    """下载指定模型（后台线程，立即返回；进度用 GET /api/models 轮询）。

    ⚠️ 2026-10-07 更正一句**过时的注释**：这里原来写着"pyannote 只提供复制命令，
    不支持从此接口触发下载" —— 那是 HF gated 时代的说法。现在 pyannote 条目是
    `source="modelscope"`、`downloadable` 也没关，`_download_worker` 里**有它的分支**
    （`download_pyannote()` 走 ModelScope 匿名下三件套），所以这个接口**能下**它。

    只有 `source="copy"` 或 `downloadable=False` 的条目才仍返回拷贝说明。

    **被"缺依赖"拒掉时，把"怎么修"一起返回**（同事 2026-09-25 实测）：只有一句
    "缺依赖"时用户不知道该装什么、也不知道装完要回来再点一次下载。所以这里多给三个
    字段（老调用方只看 ``ok`` / ``message``，不受影响）：

      * ``detail``         —— 整段话（原因 + 安装命令 + 下一步），可直接展示；
      * ``reason``         —— 原因一句话；
      * ``installCommand`` —— 那条可粘贴的 pip 安装命令；
      * ``nextStep``       —— 下一步点什么。
    """
    from app import modelinfo
    mid = body.id.strip()
    ok, msg = modelinfo.start_download(mid, force=bool(body.force))
    out = {"ok": ok, "message": msg}
    if not ok:
        try:
            prob = modelinfo.dependency_problem(mid)
        except Exception:
            prob = {}
        if prob:
            out.update(detail=prob["message"], reason=prob["reason"],
                       installCommand=prob["installCommand"], nextStep=prob["nextStep"],
                       nextAction="install")
    return out


@router.post("/system/restart")
def post_restart(_auth=Depends(optional_auth)):
    """重启 ECHO 服务（面板 设置 → 服务 的按钮）。

    本身不阻塞：真正的停/起交给脱离进程组的 scripts/restart-echo.ps1，
    面板收到 ok 后轮询 /api/status 等它回来。
    """
    if assistant.is_busy():
        return {"ok": False, "message": "有命令正在处理中，请稍后再重启"}
    try:
        from app import meeting
        st = meeting.meeting_status()
        if st.get("active"):
            return {"ok": False, "message": "正在录音中，请先结束录音"}
    except Exception:
        pass
    ok, msg = runtime.restart_echo()
    return {"ok": ok, "message": msg}


# ---- 「历史 → 指令历史」的读面 ----------------------------------------------
# 列表条目里 `meta` 是**字符串**（库里的 JSON）。面板要的是"发送当时走的是哪个智能体"，
# 让它去 JSON.parse 一个字符串字段是没必要的耦合，所以在这里摊平成一个字段。
# 老记录没有它 → 空串，面板据此**一个字都不显示**（不编"应该走的是 X"）。

def _command_row(row):
    """`commands` 的一行 → 面板条目（把 `meta` 里的展示字段摊平）。"""
    out = dict(row)
    meta = {}
    try:
        parsed = json.loads(out.get("meta") or "{}")
        if isinstance(parsed, dict):
            meta = parsed
    except Exception:
        meta = {}
    out["backend"] = str(meta.get("backend") or "")
    out["intent"] = str(meta.get("intent") or "")
    # `meta` 原文仍原样带着（别处可能要看；删字段是另一种"改契约"）
    return out


@router.get("/commands")
def get_commands(limit: int = 100, offset: int = 0, q: str = "", since: str = "",
                 _auth=Depends(optional_auth)):
    """命令历史（面板「历史 → 指令历史」）。

    `limit`/`offset` 是**分页**（「加载更多」按 offset 追加，不是把几千条一次画出来）；
    `q` 是按关键词过滤，`since`（`YYYY-MM-DD HH:MM:SS`，本地时间）是按时间过滤。
    过滤在 **SQL 里**做，`total` 因此是"过滤后一共几条" —— 面板据此决定还要不要给
    「加载更多」，不用自己数。
    """
    items = [_command_row(r) for r in
             db.list_commands(limit=min(limit, 500), offset=max(offset, 0), q=q, since=since)]
    return {"total": db.count_commands(q=q, since=since), "items": items}


@router.delete("/commands")
def del_commands(_auth=Depends(optional_auth)):
    db.clear_commands()
    return {"ok": True}


@router.get("/commands/{cmd_id}/session")
def get_command_session(cmd_id: int, limit: int = 12, _auth=Depends(optional_auth)):
    """某条命令落在了哪个 DSH 会话，以及该会话最近几轮对话（面板「历史 → 看会话」）。

    为什么由 ECHO 读：DSH 的 Web UI 没有"按会话直达"的 URL，跳不过去；而 ECHO 有签名 Cookie 的
    RPC 通道，能直接把会话内容取回来渲染。

    这是**硬限制**，已对标准版 harness 源码层面复核（dsh-web-frontend 0.1.5-rc.2，桌面版与
    标准版共用同一套前端 bundle）：app 与 vendor bundle 都不含 location.search / location.hash /
    URLSearchParams / sessionStorage / location.pathname，即前端不解析任何 query/hash 参数来定位
    会话。别再去试"拼一个会话 URL 跳过去"——这条路不存在，面板内渲染是唯一可行的查看方式。
    """
    row = db.get_command(cmd_id)
    if not row:
        raise HTTPException(status_code=404, detail="命令不存在")
    sid = (row.get("session_id") or "").strip()
    if not sid:
        return {"ok": True, "session_id": "", "messages": [], "title": "", "cwd": "",
                "exists": False, "running": False,
                "message": "这条命令没有关联会话（发送失败，或当时没拿到会话）"}

    from app.dsh import get_client, DshError
    from app.assistant import strip_injections

    client = get_client()
    info, err = {}, ""
    try:
        for it in client.list_sessions_for():
            if it.get("sessionId") == sid:
                info = it
                break
    except DshError as e:
        err = str(e)
    except Exception as e:                     # 非 DSH 后端 / 网络异常都不该 500
        err = str(e)

    msgs, anchored = [], False
    try:
        n = max(1, min(int(limit or 12), 50))
        if hasattr(client, "recent_messages"):
            # 带上命令原文 → 定位到"这一轮"，而不是永远看会话尾部
            res = client.recent_messages(sid, limit=n, anchor_text=row.get("text") or "")
            if isinstance(res, dict):
                msgs = res.get("messages") or []
                anchored = bool(res.get("anchored"))
            else:
                msgs = res or []
        else:
            err = err or "当前智能体不支持读取会话内容"
    except DshError as e:
        err = str(e)
    except Exception as e:
        err = str(e)

    out = []
    for m in msgs:
        text = m.get("text") or ""
        # 用户那侧带着 ECHO 附加的环境信息与回复要求，回看时摘掉
        if m.get("role") == "user":
            text = strip_injections(text)
        out.append({"role": m.get("role") or "user", "seq": m.get("seq"), "text": text})
    return {"ok": not err, "session_id": sid,
            "title": info.get("title") or "", "cwd": info.get("cwd") or "",
            "exists": bool(info), "running": bool(info.get("running")),
            "messages": out, "error": err, "anchored": anchored}


# ---------------------------------------------------------------- 会话
@router.get("/sessions")
def get_sessions(_auth=Depends(optional_auth)):
    return {"items": db.list_sessions()}


# ---------------------------------------------------------------- 音频
@router.get("/audio/devices")
def get_audio_devices(_auth=Depends(optional_auth)):
    try:
        return {"devices": recorder.list_input_devices(),
                "default": recorder.default_input_device()}
    except Exception as e:
        return {"devices": [], "error": str(e)}


@router.get("/audio/level")
def get_audio_level(_auth=Depends(optional_auth)):
    """当前麦克风电平（面板波形）。

    **不再开采样流**：会议录音中直接读录音器（MeetingRecorder）的实时电平
    （录音线程每 0.2s 更新一次），空闲返回 0。

    历史问题：此前用 sd.rec() 每次请求都开关一个 PortAudio 录音流，而面板
    每 ~2s 轮询一次、多个面板窗口还会并发请求 → 高频开关 WASAPI 流触发
    PortAudio 原生堆损坏，导致进程崩溃（ntdll 0xc0000005 / 0xc0000374，
    faulthandler 栈定位到本函数）。已弃用采样方式。
    """
    try:
        st = meeting.meeting_status()
        if st.get("active"):
            return {"level": min(1.0, float(st.get("level") or 0.0))}
    except Exception:
        pass
    # 语音命令收音阶段：同一个电平回调（record_command 的 level_cb），同样不开新采样流
    try:
        lv = assistant.capture_level()
        if lv > 0:
            return {"level": min(1.0, lv)}
    except Exception:
        pass
    return {"level": 0.0}


# ---------------------------------------------------------------- 转写服务（对外 API）
# 其他应用可上传音频调用本地转写（无需直接操作模型）：
#   curl -F "file=@a.mp3" -F "engine=qwen3asr" http://127.0.0.1:8970/api/stt/transcribe
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _save_upload_wav(file: UploadFile):
    """保存上传文件并转成 16k wav 临时文件，返回路径。"""
    import uuid
    from starlette.concurrency import run_in_threadpool
    tag = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    # 注意：上传文件名可能也是 .wav，tmp_in 必须与 tmp_wav 不同名（否则删除输入会误删输出）
    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    tmp_in = os.path.join(tempfile.gettempdir(), f"echo-up{tag}.in{suffix}")
    tmp_wav = os.path.join(tempfile.gettempdir(), f"echo-up{tag}.wav")
    try:
        total = 0
        with open(tmp_in, "wb") as f:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="音频文件不能超过 512 MB")
                f.write(chunk)
        try:
            await run_in_threadpool(_audio_to_wav16k, tmp_in, tmp_wav)
        except Exception:
            try:
                os.remove(tmp_wav)
            except OSError:
                pass
            raise
    finally:
        try:
            os.remove(tmp_in)
        except OSError:
            pass
    return tmp_wav


@router.get("/stt/status")
def stt_status(_auth=Depends(optional_auth)):
    """转写引擎加载状态（已加载引擎 / 设备）。"""
    st = stt_mod.engine_status()
    return {"loaded": st["loaded"], "device": st["device"], "cuda": st["cuda"]}


@router.post("/stt/transcribe")
async def stt_transcribe(file: UploadFile = File(...),
                         engine: str = Form("sensevoice"),
                         model: str = Form("small"),
                         lang: str = Form("zh"),
                         device: str = Form("auto"),
                         _auth=Depends(optional_auth)):
    """上传音频（wav/mp3/flac…）→ 转写文本。

    engine: sensevoice|qwen3asr|sherpa|tiny/base/small/medium/large
    """
    from starlette.concurrency import run_in_threadpool
    wav = await _save_upload_wav(file)
    try:
        db.add_log("debug", "api", f"stt.transcribe wav={wav} exists={os.path.isfile(wav)} engine={engine}")
        text = await run_in_threadpool(stt_mod.transcribe, wav, engine, model, lang, device)
        db.add_log("debug", "api", f"stt.transcribe result len={len(text)}")
    finally:
        try:
            os.remove(wav)
        except Exception:
            pass
    if not text:
        raise HTTPException(status_code=422, detail="未能识别出文本（音频过短或无语音）")
    return {"text": text, "engine": engine, "model": model}


@router.post("/stt/sentences")
async def stt_sentences(file: UploadFile = File(...),
                        engine: str = Form("qwen3asr"),
                        model: str = Form("0.6B"),
                        lang: str = Form("zh"),
                        device: str = Form("auto"),
                        _auth=Depends(optional_auth)):
    """上传音频 → 带时间戳的句子列表（qwen3asr 用 ForcedAligner 原生句子）。

    返回: {"text": 全文, "sentences": [{"start": 秒, "end": 秒, "text": ...}]}
    """
    from starlette.concurrency import run_in_threadpool
    wav = await _save_upload_wav(file)
    try:
        if engine == "qwen3asr":
            def _q():
                m = stt_mod._get_qwen3asr(device, f"Qwen/Qwen3-ASR-{model}",
                                          forced_aligner="Qwen/Qwen3-ForcedAligner-0.6B")
                return stt_mod._qwen3asr_sentences(m, wav, stt_mod._LANG_MAP.get(lang.lower(), None))
            try:
                text, sents = await run_in_threadpool(_q)
            except Exception as e:
                import traceback
                db.add_log("error", "api", f"qwen3asr sentences 失败: {e}\n{traceback.format_exc()[:500]}")
                raise HTTPException(status_code=500, detail=f"转写引擎错误: {e}")
            return {"text": text,
                    "sentences": [{"start": round(s, 2), "end": round(e, 2), "text": t}
                                  for s, e, t in sents]}
        text = await run_in_threadpool(stt_mod.transcribe, wav, engine, model, lang, device)
        return {"text": text, "sentences": [{"start": 0, "end": 0, "text": text}]}
    finally:
        try:
            os.remove(wav)
        except Exception:
            pass


# ---------------------------------------------------------------- 会议
@router.post("/meeting/start")
def meeting_start(_auth=Depends(optional_auth)):
    ok, msg = meeting.start_meeting()
    services.report_meeting("active" if ok else "idle", msg)
    return {"ok": ok, "message": msg}


@router.post("/meeting/stop")
def meeting_stop(_auth=Depends(optional_auth)):
    ok, msg = meeting.stop_meeting()
    services.report_meeting("transcribing" if ok else "idle", msg)
    return {"ok": ok, "message": msg}


@router.get("/meeting/status")
def meeting_status(_auth=Depends(optional_auth)):
    return meeting.meeting_status()


@router.get("/transcribe/status")
def transcribe_status(_auth=Depends(optional_auth)):
    """当前转写任务进度：{meeting_id: {phase, seg_index, seg_total, percent, detail, updated_at}}"""
    return meeting.transcribe_progress()


@router.post("/meetings/import")
async def meetings_import(
        files: List[UploadFile] = File(...),
        title: str = Form(""),
        start: str = Form(""),
        notes: str = Form(""),
        block_frames: int = Form(0),
        _auth=Depends(optional_auth)):
    """导入 1..N 个音频文件为**一场会议**（顺序即分段顺序），导入成功后自动开始转写。

    为什么是 multipart 而不是 JSON+本地路径：录音在**用户的机器/手机**上，面板是浏览器，
    能给出的只有文件本身。路径式接口还会让"导入"变成"任意文件读取"。

    字段：
      files        1..N 个音频文件（**表单里出现的顺序 = 分段顺序**，前端按用户选的顺序 append）
      title        可选标题（写 `meetings.title`）
      start        可选会议时间（`YYYY-MM-DD HH:MM[:SS]` / ISO；空=现在）
      notes        可选备注（写 `meetings.notes`）
      block_frames 可选：转码分块大小（0 = 用 `importer.DEFAULT_BLOCK_FRAMES`）。
                   **留这个口子是为了可验**：用例传一个小值就能断言"分块解码"真的发生了
                   （见 `tests/test_meeting_import.py` 的大文件用例），而不是靠读代码相信。

    支持的格式：**wav / flac / mp3 / ogg**（本机 soundfile + libsndfile 实测可读）。
    `m4a`/`aac` **明确拒绝**（HTTP 400 + 一句"需要 ffmpeg、请先转成 wav/mp3"的原因）——
    不许假装支持，也不许静默失败。详见 `app/audio/importer.py`。

    ## 为什么是"同步转码 + 后台转写"，而不是整条异步

    异步（立刻返回、后台转码）有两个问题：
      * 用户看不到"上传/转换"的真实进度，而 50 MB 的录音转换要几十秒；
      * 转码失败时那条会议记录/目录**已经建出来了** —— 而"导入失败不许留下垃圾目录"
        是这次需求里的硬要求。所以：**先把每个文件转码成 16k 单声道 wav 落进会议目录，
        全部成功之后才 `db.create_meeting()`**；这期间请求保持打开（前端有上传进度条），
        转换本身也不占事件循环（`run_in_threadpool`）。

    返回: {"ok", "name", "id", "files", "seconds", "message"}；`id` 供前端接着轮询
    既有的 `GET /api/transcribe/status`（**不新造一套进度 UI**）。
    """
    from starlette.concurrency import run_in_threadpool
    from app.audio import importer as imp

    picked = [f for f in (files or []) if f is not None]
    if not picked:
        raise HTTPException(status_code=400, detail="没有收到任何音频文件")

    tmps = []
    try:
        for f in picked:
            name = _upload_name(f.filename)
            # 扩展名一眼读不了的（m4a/aac/…）**在读字节之前**就拒绝：省掉一次白传
            # 几百兆，而且用户拿到的是"这个格式要 ffmpeg"，不是 libsndfile 那句
            # 认不出格式的 `Format not recognised.`（见 importer.unusable_extension_reason）。
            if os.path.splitext(name)[1].lower() in imp.UNUSABLE_EXTS:
                raise HTTPException(status_code=400,
                                    detail=imp.unusable_extension_reason(name))
            # **分块落临时文件**：`UploadFile.read(1MB)` 一块一块写，50 MB+ 的录音
            # 不会有任何一刻整段在内存里（`MAX_UPLOAD_BYTES` 是明面上的上限，
            # 超了当场 413，不让它写满用户的临时盘）。
            tmp = _new_upload_tmp(name)
            tmps.append(tmp)
            total = 0
            with open(tmp, "wb") as out:
                while True:
                    chunk = await f.read(UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="「%s」超过 %d MB，不能导入（请先切成几段再导入）"
                                   % (name, MAX_UPLOAD_BYTES // (1024 * 1024)))
                    out.write(chunk)
            if total == 0:
                raise HTTPException(status_code=400,
                                    detail="「%s」是空文件（0 字节），没有音频可导入。" % name)
            db.add_log("debug", "api",
                       "meetings.import 收到 %s（%d 字节）" % (name, total))
        # 转码 + 建库 + 起转写线程都在这里；失败会带上**真原因**回来。
        pairs = list(zip(tmps, [ _upload_name(f.filename) for f in picked]))
        kw = {}
        if int(block_frames or 0) > 0:
            kw["block_frames"] = int(block_frames)
        res = await run_in_threadpool(_import_uploads, pairs, title, start, notes, kw)
    finally:
        for tmp in tmps:
            try:
                os.remove(tmp)
            except OSError:
                pass

    ok, payload = res
    if not ok:
        # 业务性失败**带原因**（面板只显示后端给的真原因，不许自己编）。
        # 走 400 而不是 200+ok=false：这是"你给的文件不行"，与 `/api/models/download`
        # 那种"排队/开关"型失败不同，前端据此区分"要重新选文件"与"稍后重试"。
        raise HTTPException(status_code=400, detail=payload)
    # 把这一场的**真实**数字（时长/段数/重采样路径）一并回给前端：提示条上要能说
    # "已导入 2 段、共 3.1 分钟"，而不用再拉一次列表。
    name = payload
    row = db.get_meeting_by_name(name) or {}
    return {"ok": True, "name": name, "id": row.get("id"),
            "files": int(row.get("segments") or 0),
            "seconds": float(row.get("duration_seconds") or 0.0),
            "message": "已导入 %d 段音频（%.1f 分钟），正在转写"
                       % (int(row.get("segments") or 0),
                          float(row.get("duration_seconds") or 0.0) / 60.0)}


def _upload_name(raw):
    """上传文件名 → 只取最后一段（报错文案与 meta.json 都显示它，不显示临时名）。"""
    return os.path.basename(str(raw or "").replace("\\", "/")) or "（未命名）"


def _new_upload_tmp(name):
    """给一个上传文件开一个临时落点（**同一目录一条路**，便于 50 MB 分块写）。"""
    import uuid
    suffix = os.path.splitext(name)[1] or ".bin"
    fn = "echo-imp%d-%s%s" % (os.getpid(), uuid.uuid4().hex[:10], suffix)
    return os.path.join(tempfile.gettempdir(), fn)


def _import_uploads(pairs, title, start, notes, kw):
    """（线程池里跑）`[(临时路径, 原始名)]` → `meeting.import_meeting()`。

    为什么要把"临时路径 / 原始名"拆成两份：转码读的是临时文件，而**报错文案与
    `meta.json` 的 `importedFrom` 必须是用户认识的那个名字**（`echo-imp1234-ab12.m4a`
    这种临时名对用户毫无意义，也会把"哪个文件失败了"这件事变得没法查）。
    所以路径走 `files`、显示名走 `display_names`，一一对应。
    """
    names = [n for _p, n in pairs]
    paths = [p for p, _n in pairs]
    return meeting.import_meeting(paths, title=title, start=start, notes=notes,
                                  display_names=names, **kw)


@router.get("/meetings")
def get_meetings(limit: int = 100, offset: int = 0, _auth=Depends(optional_auth)):
    items = db.list_meetings(limit=min(limit, 500), offset=max(offset, 0))
    # 说话人**一次查询**喂整页（不是每场一条 SQL）。列表卡片显示"👥 3 人"。
    # 字段名叫 `speakerNames` 而**不叫** `speakers`：详情接口的 `speakers` 是
    # `db.get_speakers()` 的原始行（[{label, name, …}]），两个接口同名不同形，
    # 迟早有人拿 `m.speakers[0].name` 去读列表那份、静默拿到 undefined。
    speaker_names = db.speaker_names_by_meeting()
    # 时间轴档位的中文翻译**只有 capability_admin 一份**（面板不抄词汇表），
    # 循环外取一次就够 —— 别在循环里重复 import。
    from app import capability_admin
    # 补充 has_summary 等轻量展示字段（列表信息展示优化，2026-09-10）
    for it in items:
        folder = os.path.join(meeting.meetings_dir(), it["name"])
        it["has_summary"] = os.path.isfile(os.path.join(folder, "summary.md"))
        # 「已压缩」标记（2026-09-26）：列表卡片要显示
        # "原 149.0 MB → 现 76.0 MB（省 49%）"。**只读 meta.json**，不重算、不估算
        # （估算值只在压缩前的预览里出现，两个数绝不能混）。
        # 失败时留 `None`：老会议根本没有这个块，面板据此不显示任何标记。
        try:
            it["compression"] = meeting.compression_info(it["name"])
        except Exception:
            it["compression"] = None
        it["speakerNames"] = speaker_names.get(it["id"], [])
        # 「这一场正在转写吗」（2026-09-28）—— 面板据此把「重新转写」按钮禁掉、
        # 文案改成「转写中…」（`web/meeting.html`），列表卡片也用它决定画不画进度条。
        #
        # 判据只有 `meeting.transcribe_busy_reason()` 一份（进程内标记 ∪ 库里的 status），
        # 所以**刷新页面、换个标签页、重开详情页，按钮状态都还是对的** ——
        # 这一点是这次 bug 的要害：只看前端本地变量的修法，刷新一下就退回原样。
        it["transcribing"] = meeting.is_transcribing(it["name"])
        # 「转写档位」（2026-09-26，历史 → 会议历史列表）：exact / aligned / estimated。
        # 与详情页的 `capability.timestampsLabel` **同一份翻译、同一份快照**
        # （`capability_admin.timestamps_summary`），没记过的老会议是 None → 不显示。
        try:
            it["timestamps"] = capability_admin.timestamps_summary(
                meeting.meeting_meta(it["name"]).get("timestampsKinds"))
        except Exception:
            it["timestamps"] = None
    return {"items": items}


# ---- 历史音频无损压缩（FLAC）：**必须声明在 `/meetings/{mid}` 之前** ----
# FastAPI 按声明顺序匹配：`/meetings/compress/preview` 先撞上的会是 `{mid}: int`，
# 于是 `"compress"` 过不了 int 校验 → **422**，而静态路由永远到不了。
# 这不是风格问题，是"接口 422 却查不出为什么"的那类坑，所以三条一起放在这里。

@router.get("/meetings/compress/preview")
def meetings_compress_preview(limit: int = 500, _auth=Depends(optional_auth)):
    """「先算给你看」：可压缩 N 场 / 能省 X（**只读，一个字节都不写**）。

    字段见 `meeting.compression_preview()`。刻意与"开始压缩"分成两个端点：
    用户点开入口时**必须先看到数字**再决定要不要动手（这也是需求里点名的那一步）。
    """
    return meeting.compression_preview(limit=limit)


@router.post("/meetings/compress")
def meetings_compress(limit: int = 500, _auth=Depends(optional_auth)):
    """用户确认之后**真的动手**（后台线程；进度见 `/meetings/compress/status`）。

    返回 `{ok, message}`：正在跑时 `ok=false` 且 `message` 说清"已有任务在跑"，
    与 `/api/meeting/start|stop` 同一套"业务性失败也是 200"的既有约定。
    """
    ok, msg = meeting.compress_meetings(limit=limit)
    return {"ok": ok, "message": msg}


@router.get("/meetings/compress/status")
def meetings_compress_status(_auth=Depends(optional_auth)):
    """压缩进度 + 最近一次的结果汇总（与转写进度**分开**，互不顶掉对方的进度条）。"""
    return meeting.compress_progress()


@router.get("/meetings/{mid}")
def get_meeting(mid: int, _auth=Depends(optional_auth)):
    detail = meeting.get_meeting_detail(mid)
    if not detail:
        raise HTTPException(status_code=404, detail="会议不存在")
    # DB 里的 segments 是**段数**（整数，转写时写入）；前端详情还要按段渲染，
    # 所以这里用分段数组覆盖它。但直接覆盖会把段数弄丢：历史会议转写行已不在库
    # （只剩 transcript.md 文件）时 build_segments 返回空数组，面板就会显示「0 段」。
    # 因此先把原段数搬到 segment_count 再覆盖（2026-09-16）。
    detail["segment_count"] = detail.get("segments") or 0
    detail["segments"] = meeting.build_segments(mid)
    folder = os.path.join(meeting.meetings_dir(), detail["name"])
    detail["hasSegments"] = os.path.isfile(os.path.join(folder, "topics.md"))
    # 3.0：这场会**实际**用了哪个后端、跳过了谁、为什么（录音时写进 meta.json 的快照）。
    # 翻译成面板能直接渲染的形状放在 capability_admin —— 槽/后端/原因的中文名那里
    # 已经有一份，别在面板的 JS 里再抄一份词汇表。没有这段信息时是 None（老会议）。
    from app import capability_admin
    meta = meeting.meeting_meta(detail["name"])
    # `run` 是"这一场到底做了什么/在等什么"（转写是不是本机引擎出的、在不在等后端）。
    # 它**不属于** `capability` 计划（走本机那条路的会议压根没有计划），但面板要看到，
    # 所以由 `execution_summary` 单独翻好，再一并交给 `plan_summary` 出**同一个出口**。
    detail["capability"] = capability_admin.plan_summary(
        meta.get("capability"), meta.get("timestampsKinds"), meta.get("diarize"),
        capability_admin.execution_summary(meta.get("diarize"),
                                           meta.get("transcribeEngine"),
                                           meta.get("transcribeFallbackReason")))
    return detail


@router.get("/meetings/{mid}/audio")
def meeting_audio(mid: int, seg: int = 1, _auth=Depends(optional_auth)):
    """返回某段录音（支持 Range，供前端同步播放）。

    2026-09-26：历史音频可能已经被**无损压成 FLAC**（`01.flac`，原件已删）。
    面板那边不许知道这件事 —— 这条接口一律回 **WAV**：

      * 是 `.wav` → 直接 `FileResponse`（与改动前逐字一致，不复制、不占额外磁盘）；
      * 是 `.flac` → **用时解码**成临时 WAV 再回（浏览器对 `audio/flac` 的支持
        并不一致，而我们已经有一条可靠的解码路，没必要把它交给浏览器赌）。

    临时文件在这里**不立刻删**（它正被 HTTP 流式读，删了播放会中途断），但也不
    等一小时：删的动作挂成响应的 `BackgroundTask`，**响应发完就跑**（见下面的
    `_drop_temp_decode`）。此前只有 `audiofile.gc_temp()`（下次解码时顺手扫，TTL
    1 小时）与"进程退出"两道闸，而后者当时**并不存在**（`cleanup_registered()`
    没有任何调用方，2026-09-26 复查发现并接上）—— 长跑的 ECHO 会把 `%TEMP%` 攒住。

    `seg` 是 int、`{seg:02d}` 不会带分隔符；会议目录名仍走 `_safe_under` 兜底
    （万一库里的 name 被写进奇怪值，也不至于跑到会议目录之外）。
    """
    m = db.get_meeting(mid)
    if not m:
        raise HTTPException(status_code=404, detail="会议不存在")
    folder = _safe_under(meeting.meetings_dir(), _meeting_dirname(m["name"]),
                         allow_base=True)
    if not folder:
        raise HTTPException(status_code=404, detail="音频段不存在")
    from app.audio import audiofile
    path = audiofile.resolve_segment(folder, seg)
    if not path:
        raise HTTPException(status_code=404, detail="音频段不存在")
    from fastapi.responses import FileResponse
    if not audiofile.is_flac(path):
        return FileResponse(path, media_type="audio/wav", filename=f"seg{seg:02d}.wav")
    src_name = os.path.basename(path)
    try:
        temp_wav = audiofile.decode_to_wav(path)
    except audiofile.CompressionError as e:
        raise HTTPException(status_code=500,
                            detail="这段音频（%s）解不开，无法播放：%s" % (src_name, e))
    # **日志留痕**（需求原话："临时文件用完即删并在日志说明"）：解了哪一段、多大、
    # 什么时候删。没有这一行，日后翻日志只看得到"播放了"，看不出 %TEMP% 里为什么要
    # 多一个 19 MB 的文件。
    db.add_log("info", "meeting",
               "面板播放第 %d 段：%s 是 FLAC 归档，用时解码成临时 WAV（%.1f MB）"
               "交给浏览器（它只认 RIFF/WAV）——响应发完即删"
               % (seg, src_name, audiofile.stat_bytes(temp_wav) / 1048576.0))
    return FileResponse(temp_wav, media_type="audio/wav", filename=f"seg{seg:02d}.wav",
                        background=BackgroundTask(_drop_temp_decode, temp_wav))


def _drop_temp_decode(path):
    """`FileResponse` 发完之后的收尾：删掉临时解码 WAV（`audiofile` 那边的登记一起撤）。

    为什么不直接在 `meeting_audio` 里 `os.remove`：`FileResponse` 是**流式**的，
    return 那一刻字节还没发完。Starlette 在响应全部发完之后才跑 background task，
    这才是"用完即删"的正确落点。

    删不掉**绝不影响**用户：这次播放已经成功，只留一条 warn（`drop_temp` 自己不抛）。
    """
    try:
        from app.audio import audiofile
        if not audiofile.drop_temp(path):
            db.add_log("warn", "meeting",
                       "临时解码文件没删掉（%s）——下次解码时 gc_temp() 会兜底"
                       % os.path.basename(path))
    except Exception as e:
        try:
            db.add_log("warn", "meeting", "临时解码文件收尾异常：%s" % e)
        except Exception:
            pass


@router.delete("/meetings/{mid}")
def del_meeting(mid: int, _auth=Depends(optional_auth)):
    ok, msg = meeting.delete_meeting(mid)
    return {"ok": ok, "message": msg}


@router.post("/meetings/clean-short")
def clean_short_meetings(body: CleanShortIn, _auth=Depends(optional_auth)):
    """清理时长 ≤ N 分钟的会议（含音频文件）。默认 2 分钟。"""
    max_seconds = max(10, int(round(body.max_minutes * 60)))
    removed = meeting.clean_short_meetings(max_seconds=max_seconds)
    return {"ok": True, "count": len(removed), "removed": removed}


@router.post("/meetings/{mid}/speaker/rename")
def speaker_rename(mid: int, body: SpeakerRenameIn, _auth=Depends(optional_auth)):
    """说话人改名；改名为联系人（非默认名）时按「改名即入库」开关自动把声纹入库。

    2026-09-26 概念纠正：**识别是标配**（没有开关），所以这里唯一看的是
    `voiceprintAutoEnroll`（默认关）—— 关着时**绝不**自动写库，用户要入库就点
    「说话人管理」里的「声纹入库」按钮（`POST /voiceprints/enroll`）。
    """
    m = db.get_meeting(mid)
    if not m:
        raise HTTPException(status_code=404, detail="会议不存在")
    db.rename_speaker(mid, body.label, body.name)
    msg = ""
    try:
        from app import voiceprint
        if (voiceprint.auto_enroll()
                and not voiceprint.is_default_name(body.name, body.label)):
            _ok, msg = voiceprint.enroll_from_meeting(mid, body.label, body.name)
    except Exception as e:
        db.add_log("warn", "voiceprint", f"重命名后的声纹入库失败：{e}")
        msg = "声纹入库失败（见日志），改名已生效"
    meeting.export_transcript(mid)
    return {"ok": True, "message": msg}


@router.post("/meetings/{mid}/speaker/recognize")
def speaker_recognize(mid: int, _auth=Depends(optional_auth)):
    """用声纹库给本场说话人重新认人（只用转写时留存的样本，不重新分离/转写）。"""
    m = db.get_meeting(mid)
    if not m:
        raise HTTPException(status_code=404, detail="会议不存在")
    return meeting.recognize_meeting_speakers(mid)


@router.post("/meetings/{mid}/speaker/merge")
def speaker_merge(mid: int, body: SpeakerMergeIn, _auth=Depends(optional_auth)):
    m = db.get_meeting(mid)
    if not m:
        raise HTTPException(status_code=404, detail="会议不存在")
    if body.source == body.target:
        raise HTTPException(status_code=400, detail="不能合并到自身")
    db.merge_speakers(mid, body.source, body.target)
    meeting.export_transcript(mid)
    return {"ok": True}


@router.post("/meetings/{mid}/line/{line_id}")
def line_update(mid: int, line_id: int, body: LineUpdateIn, _auth=Depends(optional_auth)):
    db.update_line(line_id, body.text)
    meeting.export_transcript(mid)
    return {"ok": True}


@router.post("/meetings/{mid}/line/{line_id}/kind")
def line_kind(mid: int, line_id: int, body: LineKindIn, _auth=Depends(optional_auth)):
    db.set_line_kind(line_id, body.kind)
    return {"ok": True}


@router.post("/meetings/{mid}/summary/regenerate")
def summary_regen(mid: int, body: SummaryRegenIn, _auth=Depends(optional_auth)):
    ok, msg = meeting.regenerate_summary(mid, body.extra)
    return {"ok": ok, "message": msg}


@router.post("/meetings/{mid}/worklog")
def meeting_worklog(mid: int, body: WorklogIn, _auth=Depends(optional_auth)):
    """把会议纪要归档到笔记库：委派用户自己的归档技能完成（见 docs/worklog.md）。"""
    hint = body.archive_hint or body.project or ""
    ok, msg = meeting.push_meeting_to_worklog(mid, archive_hint=hint)
    return {"ok": ok, "message": msg}


@router.get("/worklog/status")
def worklog_status(_auth=Depends(optional_auth)):
    """归档可用性（面板据此决定「写工作日志」是否可点）。"""
    ok, reason = worklog.ready()
    # 注：`mode` 已随 worklogMode(=off 与 worklogEnabled 重复的开关) 于 2026-09-19 弃用，
    # 这里不再暴露该字段，前端也不读它（修复 /worklog/status 500）。
    return {"ready": ok, "reason": reason,
            "enabled": worklog.enabled(),
            "vault": worklog.vault_root()}


@router.post("/meetings/{mid}/retranscribe")
def meeting_retranscribe(mid: int, _auth=Depends(optional_auth)):
    """重新转写一场会议（后台线程）。

    **这一场已经在转写 → 409 Conflict**：两个转写线程并发跑同一场会互相覆盖同一份
    `transcript.md` / `meta.json`，所以这道闸在后端、**按会议**判重 ——
    不依赖前端把按钮禁掉（那是体验，不是防线）。

    响应体是本站统一的标准错误形状（与其余 30 处 `HTTPException` 一致）：

        {"detail": "这场会议正在转写中，请等它完成"}

    判据见 `meeting.transcribe_busy_reason()`（进程内标记 ∪ 库里的 status）。
    **另一场会议不受影响** —— 跨会议的并发上限仍由既有那套管（`max_concurrent`），
    两件事不混在一起。
    """
    try:
        ok, msg = meeting.retranscribe_meeting(mid)
    except meeting.MeetingTranscribeBusy as e:
        raise HTTPException(status_code=409, detail=e.message)
    return {"ok": ok, "message": msg}


@router.get("/meetings/{mid}/file")
def meeting_file(mid: int, kind: str = "transcript", _auth=Depends(optional_auth)):
    """kind=transcript|topics|summary → 返回对应文件原文。
    kind=summary → summary.md（markdown 纪要，前端 mdToHtml 直接渲染）；
    kind=topics → topics.md（元数据 JSON，前端解析标题/简介/摘要/分段）。
    兼容旧格式：summary.md 若为老结构化 JSON 则拆包成「摘要+纪要」markdown。
    返回 {"exists", "content"}。

    安全（2026-09-13 HIGH-1）：kind 以前没有任何校验就直接拼进路径，`os.path.join` 遇到
    `..` 或绝对路径会整段逃出会议目录 ——
        GET /api/meetings/1/file?kind=../../../../Users/<用户名>/Documents/机密备忘
        GET /api/meetings/1/file?kind=C:/Users/<用户名>/Obsidian库/工作日志/...
    因为后缀固定拼 `.md`，对全是 .md 的笔记库等于"任意文件读取"。现在三重收口：
    白名单 kind、目录名取 basename、最终路径必须仍在会议根目录内（realpath 判定）。
    """
    if kind not in _MEETING_FILE_KINDS:
        raise HTTPException(status_code=400, detail="kind 不合法")
    m = db.get_meeting(mid)
    if not m:
        raise HTTPException(status_code=404, detail="会议不存在")
    path = _safe_under(meeting.meetings_dir(), _meeting_dirname(m["name"]), f"{kind}.md")
    if not path:
        raise HTTPException(status_code=400, detail="非法路径")
    if not os.path.isfile(path):
        return {"exists": False, "content": ""}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if kind == "summary":
        obj = meeting._try_load_json(content)
        if isinstance(obj, dict) and (obj.get("会议纪要") or obj.get("会议摘要")):
            # 旧版结构化 JSON（会议名称/会议摘要/会议纪要）→ 拼成 markdown
            abstract = (obj.get("会议摘要") or "").strip()
            minutes = (obj.get("会议纪要") or "").strip()
            sec = (f"# 会议摘要\n\n{abstract}\n\n---\n\n{minutes}" if abstract else minutes)
            return {"exists": True, "content": sec}
    return {"exists": True, "content": content}


@router.post("/meetings/{mid}/summary")
def meeting_summary_save(mid: int, body: SummarySaveIn, _auth=Depends(optional_auth)):
    """人工编辑纪要正文 → 写回会议目录的 summary.md（面板纪要页签「编辑」）。

    只改正文：标题/摘要/分段仍在 topics.md（标题见 POST /meetings/{mid}/title）。
    业务性失败按本文件约定走 200 + ok=false（空内容、会议不存在等）。
    """
    ok, msg = meeting.save_summary(mid, body.content)
    return {"ok": ok, "message": msg}


@router.post("/meetings/{mid}/title")
def meeting_title_save(mid: int, body: MeetingTitleIn, _auth=Depends(optional_auth)):
    """人工改会议名：meetings.title + topics.md 的「标题」一起写，避免两处不一致。"""
    ok, title, msg = meeting.update_meeting_title(mid, body.title)
    return {"ok": ok, "title": title, "message": msg}


# ---------------------------------------------------------------- 存储路径（2.0 / P1、D20、D21）
# 面板「环境体检」页的数据面 + 「迁移已有会议」动作。
# 语义与 /api/models 等既有端点一致：业务性失败走 HTTP 200 + {"ok": false, "error"/"message"}。

@router.get("/paths/env")
def api_paths_env(_auth=Depends(optional_auth)):
    """环境体检：四类根、存在性/可写性、磁盘余量、当前端口、会议目录计数。

    任何一项取不到都不抛异常——配置坏掉时这个页面恰恰最需要能打开。
    """
    from app import pathadmin
    return pathadmin.env_report()


class MigrateMeetingsIn(BaseModel):
    #: 目标目录。留空 = 用配置里的 meetingsDir。
    target: str = ""
    #: 源目录。留空 = 当前生效的会议目录。
    #: 面板改配置时应当**显式传旧值**——改完配置后"当前目录"已经是新目录了，
    #: 那时再迁移会得到"无需迁移"（这是正确且安全的回答，但用户想搬的其实是旧目录）。
    source: str = ""
    dryRun: bool = False


@router.post("/paths/migrate-meetings")
def api_paths_migrate_meetings(body: MigrateMeetingsIn, _auth=Depends(optional_auth)):
    """把已有会议搬到 ``target``：只搬会议目录格式的目录，目标同名则跳过不覆盖。

    ``dryRun=true`` 只报告不动手（面板先用它给用户看"会搬哪些、跳过哪些"）。
    """
    from app import pathadmin, paths
    target = (body.target or "").strip()
    if not target:
        if not paths.active_roots()["meetingsConfigured"]:
            return {"ok": False, "error": "没有指定目标目录，配置里也没有设置 meetingsDir",
                    "source": "", "target": "", "moved": [], "skipped": [], "failed": [], "others": []}
        target = paths.meetings_root()
    return pathadmin.migrate_meetings(target, source=(body.source or "").strip() or None,
                                      dry_run=bool(body.dryRun))


# ---------------------------------------------------------------- 组件清单（2.0 / P2、D22、D23）
# 与 1.x 的 /api/models **并存**：老的模型面板与技能继续用 /api/models，新的「组件」页签用这里。

@router.get("/components")
def api_components(platform: str = "", includeBlocked: bool = False,
                   _auth=Depends(optional_auth)):
    """组件清单：平台过滤后的组件 + 就绪状态（面板「组件」页签的数据面）。

    ``platform`` 可覆盖（默认当前平台，便于预览/测试其他平台）；
    ``includeBlocked=true`` 时把不适用于本平台的组件也带回来并给出 ``blockedReason``
    —— D24 要求向导里"显示但禁用并说明原因"，不隐藏。
    """
    from app import components
    return components.catalog(platform=platform or None, include_blocked=bool(includeBlocked))


# ---------------------------------------------------------------- 首装向导（D23/D24）
# "选"与"装"分开：这三个端点只做**只读体检**与**写用户自己的计划文件**，
# 不下载、不写设置（设计 docs/向导-分步设计.md §1）。真正的下载/配置写入在执行相，
# 复用现有的 /api/models/download 与 /api/settings。

@router.get("/wizard/env")
def api_wizard_env(_auth=Depends(optional_auth)):
    """向导的"检查你的电脑"：三处位置与空间、显卡、网络、麦克风、Node、智能体状态。

    **永不 500**：任何一项探测失败都登记进报告（``note`` 字段），因为环境坏掉的时候，
    这个页面恰恰最需要能打开。
    """
    from app import wizard
    return wizard.environment_report()


class WizardPlanIn(BaseModel):
    plan: dict = {}


@router.get("/wizard/plan")
def api_wizard_plan(_auth=Depends(optional_auth)):
    """读向导计划（决策相的产物）。文件缺失/损坏都返回默认骨架。"""
    from app import wizard
    return {"plan": wizard.load_plan(), "states": list(wizard.PLAN_STATES)}


@router.put("/wizard/plan")
def api_wizard_plan_put(body: WizardPlanIn, _auth=Depends(optional_auth)):
    """写向导计划（原子替换，只写 data/wizard-plan.json）。

    只接受 ``state`` 与 ``choices`` 这类"用户的选择"；**设置与下载都不在这里发生**，
    这样用户随时能改、随时能退，不会留下半装状态。
    """
    from app import wizard
    return {"plan": wizard.save_plan(body.plan or {})}


class WizardChoicesIn(BaseModel):
    choices: dict = {}


class InstallReportIn(BaseModel):
    """技能装完登记的内容（结构由技能决定，服务端只做**宽松**校验）。

    典型字段：``engines``（列表）、``wake`` / ``diarize``（布尔）、``agent``（名字）、
    ``models``（列表）、``dirs``（三处路径）、``versions``（python/echo 版本）、``notes``。
    服务端不强制 schema：技能可能先于 ECHO 升级，多写字段不该被拒。
    """
    report: dict = {}


@router.post("/wizard/preview")
def api_wizard_preview(body: WizardChoicesIn, _auth=Depends(optional_auth)):
    """确认页的数据：把选择展开成"将下载什么、合计多大、将写哪些设置"。

    **纯计算**（除了把状态记成 reviewing）：用户在这一页还能返回改，什么都还没落地。
    """
    from app import wizard
    built = wizard.build_plan(body.choices or {})
    plan = wizard.load_plan()
    plan["state"] = "reviewing"
    plan["choices"] = dict(body.choices or {})
    plan["built"] = built
    wizard.save_plan(plan)
    return {"plan": built}


@router.post("/wizard/execute")
def api_wizard_execute(body: WizardChoicesIn, _auth=Depends(optional_auth)):
    """执行相：**先写配置，再依次触发下载**；已就绪的跳过，单项失败不中断其余。

    这里不再问任何问题（设计 §1）—— 请求本身就是"确认页点下开始"那一下。
    """
    from app import wizard
    return {"result": wizard.execute_plan(choices=body.choices or {})}


@router.get("/wizard/state")
def api_wizard_state(_auth=Depends(optional_auth)):
    """执行相的状态：每项 排队中 / 正在下载 / 好了 / 没成 / 已跳过 + 总进度。

    复用 ``modelinfo.jobs()`` 的真实进度，所以关掉面板再打开也能接着看。
    """
    from app import wizard
    return wizard.execution_state()


@router.get("/wizard/first-run")
def api_wizard_first_run(_auth=Depends(optional_auth)):
    """**首次启用向导**该不该自动弹（面板启动时就问它）。

    **刻意做成最轻的一个接口**：不能顺手拉 ``/api/wizard/env``（那个要探网，1.5 s 起）。
    `shouldOnboard` 由 `install_state.onboarding()` 一处判定 —— 面板**不许**再自己算一遍：
      * 首次安装（含"技能刚装完"的全新机器）→ 弹；
      * **升级安装 → 不弹**（直接进工作状态，向导入口只在「设置」里）。

    `firstRun` 保留旧的窄判据（只看旧向导那个文件），只为兼容；**新代码请用
    `shouldOnboard`** —— 拿 `firstRun` 当"该不该弹向导"正是 2026-09-21 那次回归。
    """
    from app import install_state, wizard
    info = install_state.onboarding()
    return dict(info, firstRun=wizard.first_run(), installed=wizard.installed_path())


@router.post("/wizard/finalize")
def api_wizard_finalize(_auth=Depends(optional_auth)):
    """向导走到末页时调用：写 ``data/installed-components.json``（执行后的真值）。

    写完 ``first_run()`` 就变 False，面板不再自动进向导 —— 这就是"向导走过一次"的凭据。
    """
    from app import wizard
    return {"installed": wizard.finalize()}


# ---------------------------------------------------------------- 安装状态（技能优先，2026-09-21）
# 安装现在由**技能**在用户自己的 agent 里完成（docs/安装-技能优先.md）。这两个接口是技能与
# 面板的**共用真值**：技能装完登记（/report），面板读它决定"要不要提示还没装完"（/state）。
# 刻意不叫 wizard/*：向导只是众多调用方之一，安装状态本身与向导无关。


@router.get("/install/state")
def api_install_state(_auth=Depends(optional_auth)):
    """这台机器装成什么样了：登记过没有 / 选了什么 / 还缺什么。

    面板启动时问它（比 ``/api/wizard/first-run`` 重，但**不再强制进向导**，所以不必卡在
    800ms 的赛跑里）。技能也用它做最终自检 —— 一套判断两处复用。
    """
    from app import install_state
    return install_state.state()


@router.post("/install/report")
def api_install_report(body: InstallReportIn, _auth=Depends(optional_auth)):
    """技能装完登记："我装了哪些、什么版本、装在哪"。

    **不写设置值**（报告是明文，可能被拷来拷去）：只记组件、模型、版本、目录与人话备注。
    写完 ``install_state.declared()`` 就为真 —— 面板不再提示"还没装完"，也不再自动进向导。
    """
    from app import install_state
    saved = install_state.save_report(body.report or {})
    return {"ok": True, "saved": saved, "state": install_state.state()}


class CheckVaultIn(BaseModel):
    """S2 校验用：用户（或选择器）给的目录。"""
    path: str = ""


class PickFolderIn(BaseModel):
    """「浏览…」的入参：窗口标题 + 起始目录（都可有可无）。"""
    title: str = ""
    initial: str = ""


class InstallModeIn(BaseModel):
    mode: str = ""
    note: str = ""


@router.get("/install/mode")
def api_install_mode_get(_auth=Depends(optional_auth)):
    """这次是**全新安装**还是**升级安装**（`fresh` / `upgrade`；没登记过是空串）。

    为什么要有它（2026-10-10）：面板据此决定**要不要弹首次启用向导** ——
    全新装要弹（新用户得配笔记库与三个技能），升级装不弹（直接进工作状态）。
    判据只能来自安装器：它才知道自己是装进空目录还是覆盖已有目录。
    """
    from app import install_state
    return {"mode": install_state.install_mode(),
            "firstRunDone": install_state.first_run_done(),
            "existing": install_state.existing_content()}


@router.post("/install/mode")
def api_install_mode_put(body: InstallModeIn, _auth=Depends(optional_auth)):
    """安装器登记安装模式（`fresh` / `upgrade`）。装**之前**探测、装完写。

    安装器也可以直接写 ``<数据根>/install-mode.json``（同一个文件、同样格式）——
    那条路不依赖服务已经起来，是无人值守安装的主路。
    """
    from app import install_state
    try:
        saved = install_state.save_install_mode(body.mode, note=body.note)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "saved": saved, "onboarding": install_state.onboarding()}


# ---------------------------------------------------------------- 随包技能（2026-10-11）
# 随包的技能跟着**代码树**走（`<代码目录>/.dsh/skills/`），而 agent 从 **`DSH_HOME/skills/`**
# 读技能 —— 两个不同目录，所以必须有人"搬"一次。安装器与首次启用向导都调这里，
# **只补缺、绝不覆盖**（用户可能自己调过同名技能）。


@router.get("/skills/setup")
def api_skills_status(_auth=Depends(optional_auth)):
    """随包技能在各 DSH 家目录里的现状（**只读**）。"""
    from app import skills_setup
    return skills_setup.status()


@router.post("/skills/setup")
def api_skills_install(_auth=Depends(optional_auth)):
    """把随包技能补进各 DSH 家目录的 `skills/`（幂等；已存在的一律跳过）。"""
    from app import skills_setup
    return skills_setup.install()


# ---------------------------------------------------------------- 原生选择器（2026-10-11）
# 首次启用向导 S2 要用户指 Obsidian 笔记库 —— 手敲一长串路径很难受，给一个「浏览…」。
# **只有回环调用能给**（面板在本机；在别人连过来的会话里弹窗是错的），
# **取消不是失败**，超时/没有图形会话/不支持的系统都优雅失败（绝不 500、绝不挂住）。
# 细节与那四条约束见 `app/dialog.py` 的模块说明。


@router.post("/dialog/pick-folder")
def api_pick_folder(body: PickFolderIn, request: Request, _auth=Depends(optional_auth)):
    """弹一个"选文件夹"的原生窗口，把用户选的结果回给面板。"""
    if not _is_loopback_call(request):
        raise HTTPException(status_code=403, detail="这个接口只给本机面板用（选择器只能在本机弹）")
    from app import dialog
    return dialog.pick_folder(title=body.title, initial=body.initial)



# ---------------------------------------------------------------- S1 的「下一步」（2026-10-11）
# 用户口径：首次启用向导 S1 点「下一步」要**测试模型是否可用**。
# 为什么不能只回"配置已保存"：`available(probe=...)` 只证明进程/凭据在，**不证明模型答得出来**
# （2026-10-10 实测：harness 在跑、凭据也对，但会话开在别棵树的工作区上照样失败）。
# 唯一判据就是"让当前智能体答一句话"，所以这里真开一个会话、真问一句。
# ⚠️ 这条会**花钱**（一次极短的模型调用），面板必须把这件事告诉用户。

#: 极短的探针提示词 —— 既要便宜，又要让用户一眼看懂"模型真的答了"。
TEST_AGENT_PROMPT = "请只回复两个字：可用"
TEST_AGENT_TIMEOUT = 45


@router.post("/wizard/test-agent")
def api_wizard_test_agent(_auth=Depends(optional_auth)):
    """让**当前智能体**答一句话，作为"模型可用"的判据。

    返回 ``{ok, agent, reply, done, elapsed, reason, message}``：
    ``reason`` 为 ``no-agent`` / ``unavailable`` / ``session`` / ``error`` / ``no-reply``；
    失败一律**不抛**（面板要拿它显示人话，而不是一句 500）。
    """
    import time as _time
    from app import agents as _agents

    name = ""
    try:
        name = str(_agents.active_name() or "")
    except Exception:
        name = ""
    try:
        a = _agents.active_agent()
    except Exception as e:
        return {"ok": False, "agent": name, "reason": "no-agent",
                "message": "拿不到当前智能体：%s" % str(e)[:160]}
    if a is None:
        return {"ok": False, "agent": name, "reason": "no-agent",
                "message": "还没有可用的智能体 —— 先到 设置 → 智能体 里选一个（标准版 harness 或 DSH 桌面版）"}

    # 先看"在不在"，省得白等几十秒（注：这一步**不带** probe，probe 有副作用）
    try:
        ok, why = a.available()
        if not ok:
            return {"ok": False, "agent": name, "reason": "unavailable",
                    "message": str(why or "当前智能体不可用")[:300]}
    except Exception:
        pass                                     # 问不出来就往下试，别因为探针坏了挡住测试

    t0 = _time.monotonic()
    try:
        sid = a.create_session()
    except Exception as e:
        return {"ok": False, "agent": name, "reason": "session",
                "message": "开会话失败：%s" % str(e)[:200]}
    try:
        reply, done = a.ask(sid, TEST_AGENT_PROMPT, timeout=TEST_AGENT_TIMEOUT, poll=0.5)
    except Exception as e:
        return {"ok": False, "agent": name, "reason": "error",
                "message": "提问失败：%s" % str(e)[:200]}
    elapsed = round(_time.monotonic() - t0, 1)
    text = str(reply or "").strip()
    if not text:
        return {"ok": False, "agent": name, "reason": "no-reply", "done": bool(done),
                "elapsed": elapsed,
                "message": "智能体没有回话（等 %.0f 秒）—— 多半是模型/命令还没配好；"
                           "可以到 设置 → 智能体 点「检测」看细节" % elapsed}
    return {"ok": True, "agent": name, "reply": text[:200], "done": bool(done),
            "elapsed": elapsed, "reason": "", "message": ""}


# ---------------------------------------------------------------- 首次启用向导（2026-10-11）
# S2 的「浏览…」选完目录要**校验一下**（服务端才知道那个目录里有没有 .obsidian）；
# 走完三步要**登记一次**（以后不再自动弹）。两件都是只读/轻量，且失败一律不抛。


@router.post("/dialog/check-vault")
def api_check_vault(body: CheckVaultIn, _auth=Depends(optional_auth)):
    """看一眼这个目录像不像 Obsidian 库（**只读**；判不出来也照样能用）。

    `isVault` 只表示"里面有 `.obsidian`"。**不是库也能用**（我们按目录放笔记）——
    所以这不是"校验失败"，面板不该因此拦住用户。
    """
    import os as _os
    path = str(body.path or "").strip()
    if not path:
        return {"ok": False, "isVault": False, "message": "还没填路径"}
    exists = _os.path.isdir(path)
    is_vault = bool(exists and _os.path.isdir(_os.path.join(path, ".obsidian")))
    note = ""
    if not exists:
        note = "这个路径不是一个目录（也可能还没建）"
    elif not is_vault:
        note = "里面没有 .obsidian —— 不是 Obsidian 库也能用，我们会直接按目录放笔记"
    return {"ok": True, "exists": exists, "isVault": is_vault, "path": path, "message": note}


@router.post("/wizard/first-run")
def api_wizard_mark_first_run(_auth=Depends(optional_auth)):
    """登记"首次启用向导走过一次"（`first-run.json`）。走完三步才调 —— 之后不再自动弹。"""
    from app import install_state
    saved = install_state.mark_first_run_done()
    return {"ok": True, "saved": saved, "onboarding": install_state.onboarding()}




# ---------------------------------------------------------------- 能力 provider（P5 / D25）
# ASR / LLM / TTS 三类能力的统一清单：谁在生效、是不是要出网（egress）、就绪与否。
# 与 /api/components 的分工：**components = 装什么**（模型/引擎/运行时的安装与就绪），
# **providers = 用哪个**（能干活的能力实现，含在线服务）。P5 的"配一个在线 LLM 就能出纪要"
# 就是靠 providerLlm 选择 ECHO AUTO 实现的。

@router.get("/providers")
def api_providers(ready: bool = False, _auth=Depends(optional_auth)):
    """provider 清单（默认不探测，只列清单；``ready=true`` 时才做就绪探测）。

    就绪探测会碰网络（例如 edge-tts 的在线探针）与依赖（funasr 之类），所以默认关掉 ——
    面板按需传 ``ready=true``，避免每次打开设置页都打一次外网。
    **响应里不含任何凭据**（令牌只留在进程内，见 app/providers/router.py 的说明）。
    """
    from app import providers as providers_mod
    return providers_mod.catalog(ready=bool(ready))


@router.get("/providers/presets")
def api_provider_presets(_auth=Depends(optional_auth)):
    """在线服务预设（面板"一键填入"用）。

    **只有公开信息**：厂商公开地址与常见模型名。不含密钥、不含单位内网地址
    （"内网网关"那条 base_url 留空，由用户按部署文档填）。
    """
    from app.providers import presets as presets_mod
    return presets_mod.catalog()


@router.get("/providers/config")
def api_provider_config(_auth=Depends(optional_auth)):
    """provider 相关的配置项（给面板「能力 provider」卡片编辑用）。

    这些键在 `DEFAULTS` 里标了 `hidden=True` —— 即**不再出现在通用设置表单**里：
    "同一个功能两套界面"是用户在实测里直接指出的设计问题（2026-09-19），
    所以统一由卡片承载，这里把它们单独取出来，**沿用同一套凭据遮罩**
    （secret → value 置空 + hasValue，真实值永不出接口）。

    额外带上 `ttsEngine`（只读展示用）：TTS 的"本地/在线/关闭"与它本来就是同一个开关，
    卡片不再放第二个下拉（`providerTts` 已弃用并折叠进 ttsEngine，见 config.DEPRECATION_MIGRATIONS）。
    """
    from app.config import CARD_CONFIG_KEYS, DEFAULTS, _effective_options
    from app.config import settings as _s
    rows = []
    for key, meta in DEFAULTS.items():
        if meta.get("deprecated"):
            continue
        if meta.get("grp") != "provider" and key not in CARD_CONFIG_KEYS:
            continue
        value = _s.get(key, meta["value"])
        row = {"key": key, "grp": meta["grp"], "label": meta["label"],
               "description": meta["description"], "value_type": meta["value_type"],
               "options": list(meta.get("options", [])),
               # 平台声明的候选项优先（D11：macOS 的离线朗读是 say 不是 sapi）
               "platform_options": list(_effective_options(key, meta)),
               "read_only": key in CARD_CONFIG_KEYS and meta.get("grp") != "provider"}
        if meta.get("secret"):
            row["secret"] = True
            row["hasValue"] = bool(str(value or "").strip())
            row["value"] = ""
        else:
            row["value"] = value
        rows.append(row)
    return {"settings": rows, "group": "provider"}


# ---------------------------------------------------------------- 声纹库（常用联系人）
# 入库两条路，都要用户主动：① 会议里把说话人改名为联系人（需开 voiceprintAutoEnroll）；
# ② 「说话人管理」里的「声纹入库」按钮（POST /voiceprints/enroll，**与开关无关**）。
# **识别（认人）没有开关**：会议转写一定会拿库里的样本比对（库空则静默无结果）。
# 库里的样本可在面板「说话人管理」查看/删除，这里是对应的 REST 入口。
#
# 隐私边界：样本只落在本机 `data/echo.db`（`voiceprints` 表），不出网。
#
# 返回约定：**业务性失败**（库里没有这个联系人、会议没有声纹样本…）一律
# `HTTP 200 + {"ok": false, "message": "人话原因"}`，与既有的 /api/meeting/start|stop、
# /api/models/download 等端点保持一致（面板/手机 App/技能都按 ok 字段判成败）；
# 只有参数校验、鉴权这类框架级错误才走 4xx（FastAPI 校验 422 / optional_auth 的 401）。

@router.get("/voiceprints")
def get_voiceprints(_auth=Depends(optional_auth)):
    """声纹库：联系人 + 样本列表 + 当前生效参数（面板「说话人管理」渲染）。

    `enabled` 是**恒为真**的兼容字段：识别是标配（2026-09-26）。老面板读它，
    保留是为了不把"面板以为识别被关了"这种假象造出来；新页面改用 `autoEnroll` 说话。
    """
    from app import voiceprint
    thr, margin = voiceprint.thresholds()
    stats = voiceprint.library_stats()
    return {"items": voiceprint.library_view(),
            "enabled": voiceprint.recognition_available(),
            "autoEnroll": voiceprint.auto_enroll(), "threshold": thr, "margin": margin,
            "contacts": stats["contacts"], "total": stats["samples"]}


# ---------------------------------------------------------------- 说话人聚合建议
# 面板「说话人管理」里、声纹库列表**下方**那一块（2026-10-07 用户要求）。
# 背景：pyannote 是**按场独立**聚类的，同一个人在不同会议里拿到互不相干的标签
# （这场 S1、下场 S3），一场里也可能被切成两簇。库里有每场每个说话人的平均嵌入，
# 所以"这些人是不是同一个"可以先算出来，再用**试听**让用户确认、一次改名入库。

@router.get("/speakers/agg/suggestions")
def speaker_agg_suggestions(threshold: float = 0.0, limit: int = 8,
                            _auth=Depends(optional_auth)):
    """跨会议说话人聚合建议（只读；`threshold<=0` 用与声纹识别同一个阈值）。

    只**建议**，不自动合并 —— 认人这件事必须由用户拍板（也要能先试听）。
    """
    from app import speaker_agg
    return speaker_agg.suggestions(threshold=(threshold or None), limit=limit)


class SpeakerAggApplyIn(BaseModel):
    """把建议里勾中的几个说话人并成一个联系人（并**自动入库**）。"""
    members: List[dict] = []
    name: str = ""


@router.post("/speakers/agg/apply")
def speaker_agg_apply(body: SpeakerAggApplyIn, _auth=Depends(optional_auth)):
    """把若干"同一个人"的说话人并成一个联系人：**逐场改名 + 声纹入库**。

    与会议详情页的"改名"**刻意不同**：那一处的改名叫「去个名」，用户常常只是随手看一眼；
    这里点在"聚合建议"里、还先试听过，**意图明确就是要建联系人**，
    所以这里**无论如何都入库**（不看 `voiceprintAutoEnroll`）——
    否则用户按提示改完名、声纹库里却没有，下一次还得再认一遍。
    """
    from app import speaker_agg
    ok, msg, detail = speaker_agg.apply(body.members, body.name, enroll=True)
    return {"ok": ok, "message": msg, "detail": detail}


@router.post("/voiceprints/enroll")
def voiceprint_enroll(body: VoiceprintEnrollIn, _auth=Depends(optional_auth)):
    """把某场会议某说话人的声纹入库（改名自动入库之外的显式入口）。"""
    from app import voiceprint
    ok, msg = voiceprint.enroll_from_meeting(body.meeting_id, body.label.strip(), body.name)
    return {"ok": ok, "message": msg}


@router.delete("/voiceprints/{vid}")
def voiceprint_delete(vid: int, _auth=Depends(optional_auth)):
    """删除单条声纹样本。"""
    from app import voiceprint
    ok, msg = voiceprint.delete_sample(vid)
    return {"ok": ok, "message": msg}


@router.delete("/voiceprints")
def voiceprint_delete_name(name: str = "", _auth=Depends(optional_auth)):
    """按联系人删除其全部声纹样本（name 必填）。"""
    from app import voiceprint
    ok, msg = voiceprint.delete_contact(name)
    return {"ok": ok, "message": msg}


def _speaker_clip(meeting_name: str, label: str, filename: str = "audition.wav"):
    """切出"某场会议里某个说话人"的一小段音频，回一个可播放的文件。

    **抽出来共用**（2026-10-07）：声纹试听（按 vid）与聚合建议试听（按 meeting+label）
    要的是同一件事，复制第二份必然漂移。调用方负责把 vid / meeting_id 解析成
    `(meeting_name, label)`。

    返回 ``FileResponse`` 或 ``{"ok": False, "reason": ...}``（业务性失败一律 HTTP 200，
    与 `/voiceprints` 那套约定一致：面板要能如实显示"为什么听不了"）。
    """
    from fastapi.responses import FileResponse
    from app.audio import audiofile
    import soundfile as sf

    name = str(meeting_name or "")
    label = str(label or "")
    if not name:
        return {"ok": False, "reason": "没记源会议，无法试听"}
    folder = os.path.join(meeting.meetings_dir(), name)
    if not os.path.isdir(folder):
        return {"ok": False, "reason": "源会议目录已清理（%s）" % name}

    # 找"这个说话人"的第一行 → 知道在第几段、段内什么时间
    seg_index, start, end = 0, 0.0, 0.0
    mtg = db.get_meeting_by_name(name)
    if mtg:
        for ln in db.get_lines(mtg["id"]):
            if (str(ln.get("speaker_label") or "") == label
                    and float(ln.get("end") or 0) > float(ln.get("start") or 0)):
                seg_index = int(ln.get("seg_index") or 0)
                start, end = float(ln.get("start") or 0.0), float(ln.get("end") or 0.0)
                break
    path = audiofile.resolve_segment(folder, seg_index) if seg_index else None
    if not path:
        segs = audiofile.list_segments(folder)
        path = segs[0] if segs else None
    if not path:
        return {"ok": False, "reason": "源音频文件已清理（%s）" % name}
    if audiofile.is_flac(path):
        path = audiofile.decode_to_wav(path)

    # 切 12 秒以内（带一点前后留白）；时间对不上就退化成"这一段的前 12 秒"，
    # 宁可听到人声，也不要因为时间戳问题回一个空文件。
    CAP, PAD = 12.0, 0.25
    try:
        info = sf.info(path)
    except Exception as exc:
        return {"ok": False, "reason": "源音频读不了（%s）：%s" % (name, audiofile._short(exc))}
    a = max(0.0, start - PAD)
    b = min(info.duration, (end or (start + CAP)) + PAD)
    if b - a < 0.3 or b - a > CAP * 2:
        a, b = 0.0, min(info.duration, CAP)

    tmp = tempfile.mkdtemp(prefix="echo-vp-audition-")
    out = os.path.join(tmp, "audition.wav")
    with sf.SoundFile(path) as fh:
        fh.seek(int(a * fh.samplerate))
        data = fh.read(int((b - a) * fh.samplerate), dtype="float32")
    sf.write(out, data, info.samplerate)
    return FileResponse(out, media_type="audio/wav", filename=filename,
                        background=BackgroundTask(audiofile.remove_tree, tmp))


@router.get("/speakers/agg/audition")
def speaker_agg_audition(meeting_id: int, label: str = "", _auth=Depends(optional_auth)):
    """试听"聚合建议里的某一条"（按会议 + 说话人标签，不需要它已经入库）。

    为什么需要它：聚合建议是**入库之前**的事 —— 用户要先听一下"这两个是不是同一个人"，
    才决定并成一个联系人。按 vid 的那条试听只能听**已入库**的样本，覆盖不到这里。
    """
    m = db.get_meeting(meeting_id)
    if not m:
        return {"ok": False, "reason": "会议不存在"}
    return _speaker_clip(str(m.get("name") or ""), label,
                         filename="speaker-%s-%s.wav" % (meeting_id, label or "x"))


@router.get("/voiceprints/{vid}/audition")
def voiceprint_audition(vid: int, _auth=Depends(optional_auth)):
    """试听一条声纹：把**源会议里那个说话人的那一段**切出来，回一个 wav。

    为什么不是整段会议音频：会议按 `meetingSegmentMinutes` 切段（默认 10 分钟），
    整段放出来根本听不出是谁。真正的切片逻辑在 `_speaker_clip()`（与聚合建议试听共用）。
    """
    row = db.get_voiceprint(vid)
    if not row:
        return {"ok": False, "reason": "样本不存在"}
    return _speaker_clip(str(row.get("meeting_name") or ""),
                         str(row.get("source_label") or ""),
                         filename="voiceprint-%d.wav" % vid)


# ---------------------------------------------------------------- 控制
@router.post("/control/dsh/start")
def control_dsh_start(_auth=Depends(optional_auth)):
    ok, msg = manager.dsh_start()
    return {"ok": ok, "message": msg}


@router.post("/control/dsh/stop")
def control_dsh_stop(_auth=Depends(optional_auth)):
    ok, msg = manager.dsh_stop()
    return {"ok": ok, "message": msg}


@router.post("/control/hotkey/start")
def control_hotkey_start(_auth=Depends(optional_auth)):
    ok, msg = runtime.start_hotkey()
    return {"ok": ok, "message": msg}


@router.post("/control/panel/toggle")
def control_panel_toggle(_auth=Depends(optional_auth)):
    """切换仪表盘（与 panelHotkey 等价）。

    面板/脚本/其他触点都可以用它验证边条：已开则收起为窄条，再调一次展开。
    """
    ok = runtime.toggle_sidebar()
    return {"ok": bool(ok), "message": "已切换仪表盘" if ok else "切换失败（详见服务日志）"}


@router.post("/control/hotkey/stop")
def control_hotkey_stop(_auth=Depends(optional_auth)):
    ok, msg = runtime.stop_hotkey()
    return {"ok": ok, "message": msg}


@router.post("/control/wake/start")
def control_wake_start(_auth=Depends(optional_auth)):
    ok, msg = runtime.start_wake()
    return {"ok": ok, "message": msg}


@router.post("/control/wake/stop")
def control_wake_stop(_auth=Depends(optional_auth)):
    ok, msg = runtime.stop_wake()
    return {"ok": ok, "message": msg}


@router.post("/control/echo/stop")
def control_echo_stop(_auth=Depends(optional_auth)):
    """关闭 ECHO 服务（面板 仪表盘 → 操作区 的「关闭 ECHO」）。

    与 `/system/restart` **同一把闸**（2026-10-02）：正在处理的命令、正在录的会议都不许关 ——
    以前这个端点谁都拦，脚本/面板一调就停，而"录音到一半被停"正是 2026-09-23 那次事故的形状。
    """
    if assistant.is_busy():
        return {"ok": False, "message": "有命令正在处理中，请稍后再关闭"}
    try:
        from app import meeting
        st = meeting.meeting_status()
        if st.get("active"):
            return {"ok": False, "message": "正在录音中，请先结束录音"}
    except Exception:
        pass
    import threading
    threading.Timer(0.5, manager.echo_stop_self).start()
    return {"ok": True, "message": "ECHO 服务即将关闭"}


@router.post("/control/stt/unload")
def control_stt_unload(_auth=Depends(optional_auth)):
    """卸载全部转写引擎（释放显存）。"""
    stt_mod.reset_engines()
    return {"ok": True, "message": "转写引擎已卸载，显存已释放"}


@router.post("/control/tts/test")
def control_tts_test(_auth=Depends(optional_auth)):
    """语音合成测试播报。走 provider 门面（P5）：按 ttsEngine 选择实现后朗读。"""
    from app import providers as providers_mod
    providers_mod.speak_async("你好，我是 ECHO 语音助手，当前语音合成正常。")
    return {"ok": True, "message": "已开始测试播报"}


@router.post("/tts")
def post_tts(body: TtsIn, _auth=Depends(optional_auth)):
    """合成一句话 → **音频字节**（`audio/mpeg` 或 `audio/wav`）。

    与上面 `POST /api/control/tts/test` 的区别：那条是"在**本机喇叭**播"，这条是"**把字节给你**"
    —— 手机 / 手表自己播（设计 §3 新增 3）。两条共用 `app/audio/tts.py` 的同一套合成逻辑。

    失败**必须留痕**（`warn/tts`），并且分三档说清：
      * **422** = 文本为空；
      * **409** = **朗读被关掉**（`ttsEngine=off`）—— "我关了"与"坏了"不能混为一谈
        （这正是设计里那条判据：静默成功最坏）；
      * **502** = 合成真的失败（附原因）。
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text 不能为空")
    engine = (body.engine or "").strip() or str(settings.get("ttsEngine", "auto") or "auto")
    try:
        mime, data = tts_mod.synthesize(text, engine=engine)
    except tts_mod.TtsOff:
        raise HTTPException(status_code=409,
                            detail="朗读被关掉了（ttsEngine=off）：打开它，或显式指定别的引擎")
    except tts_mod.TtsError as exc:
        db.add_log("warn", "tts", "合成失败（engine=%s）：%s" % (engine, exc))
        raise HTTPException(status_code=502, detail="合成失败：%s" % exc)
    if not data:
        db.add_log("warn", "tts", "合成返回空音频（engine=%s）" % engine)
        raise HTTPException(status_code=502, detail="合成返回空音频")
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "no-store", "X-Echo-Tts-Engine": engine})


@router.post("/control/mic/test")
def control_mic_test(device: int = -1, purpose: str = "command",
                     _auth=Depends(optional_auth)):
    """麦克风测试：统一占用保护；macOS 音频操作在可超时回收的子进程内。

    `device` 不给（-1）就按 `purpose` 解析：默认测**指令**那个麦
    （`command` / `meeting`），这样"指令麦"和"会议麦"能分别试。
    """
    import numpy as np
    want = int(device) if int(device) >= 0 else recorder.resolve_input_device(purpose)
    try:
        with recorder.input_stream(want) as stream:
            data, _ = stream.read(int(16000 * 1.0))
            rms = float(np.sqrt(np.mean((data.astype(np.float32) / 32768.0) ** 2)))
            return {"ok": True, "device": stream.device, "rms": round(rms, 4),
                    "purpose": purpose, "requested": want}
    except Exception as e:
        db.add_log("error", "api", f"mic test 失败: {e}")
        return {"ok": False, "error": str(e), "purpose": purpose, "requested": want}


# ---------------------------------------------------------------- 启动编排
@router.get("/boot/status")
def boot_status(_auth=Depends(optional_auth)):
    import app.boot as boot
    return boot.snapshot()


@router.post("/boot/component/{cid}/start")
def boot_component_start(cid: str, _auth=Depends(optional_auth)):
    import app.boot as boot
    ok, msg = boot.start_component(cid)
    return {"ok": ok, "message": msg}


@router.post("/boot/component/{cid}/stop")
def boot_component_stop(cid: str, _auth=Depends(optional_auth)):
    import app.boot as boot
    ok, msg = boot.stop_component(cid)
    return {"ok": ok, "message": msg}


# ---------------------------------------------------------------- 日志 / 事件
@router.get("/logs")
def get_logs(limit: int = 200, level: str = "", source: str = "", _auth=Depends(optional_auth)):
    return {"items": db.list_logs(limit=min(limit, 1000), level=level, source=source)}


@router.get("/events")
def get_events(limit: int = 100, _auth=Depends(optional_auth)):
    return {"items": db.list_events(limit=min(limit, 500))}


# ---------------------------------------------------------------- 手机 / 手表触点：配对
# 设计：docs/手机触点-App设计.md §3/§4。2026-10-09 改成"**短码 → 兑换**"两步：
# 令牌不再出现在二维码里（拍到截图 = 拿到一把无寿命的凭据），而**没有摄像头**的设备
# （HUAWEI WATCH 4 Pro）也照样能配。理由与算术见 app/phone_pair.py 顶部。

def _phone_base_url() -> str:
    """该告诉手机 / 手表连哪个地址（**没有可用地址就回空串，不许编一个**）。

    端口取 `ports.active_port()`（**实际**在听的那个），不是配置里的首选值：
    端口让过位的时候（Windows 保留段 / 被占用），写配置值会让手机连到没人听的端口上，
    现象是"配对成功但一直连不上"（2026-09-20 面板就踩过同一个坑）。
    """
    host = netguard.preferred_address()
    if not host:
        return ""
    port = ports.active_port(int(settings.get("serverPort", 8970) or 8970), db.DATA_DIR)
    return "http://%s:%d" % (host, port) if port else ""


def _pair_state() -> dict:
    """配对这件事的**全部现状**（面板一处取全，免得三个接口各画一半）。"""
    return {
        "bindMode": str(settings.get("serverBindMode", "loopback") or "loopback"),
        "lanHost": str(settings.get("serverLanHost", "") or ""),
        "addresses": netguard.local_addresses(),
        "baseUrl": _phone_base_url(),
        "pending": phone_pair.pending(),
        "lockedSeconds": phone_pair.locked_for(),
        "apiAuthEnabled": bool(settings.get("apiAuthEnabled", False)),
    }


@router.get("/pair/phone")
def phone_pair_state(_auth=Depends(optional_auth)):
    """面板用：现在能不能配对、该显示哪个码与哪个地址。**只读，不改任何状态。**"""
    return _pair_state()


@router.post("/pair/phone")
def phone_pair_issue(request: Request, _auth=Depends(optional_auth)):
    """生成一张一次性配对码（**只有坐在这台机器前的人能做**）。

    为什么必须回环（设计 §3 新增 1）：配对码的语义是"给设备发钥匙"，
    能发钥匙的必须是这台机器的主人。网上来的请求**即使带着有效令牌**也不给新码 ——
    否则一把泄露的令牌就能无限复制凭据。

    为什么 loopback 档不给生成：档位没开时手机根本连不上，生成一张用不了的码只会让用户
    以为"配好了却连不上"（这正是本仓库最忌讳的那种错）。**如实说原因，指到该改哪一项。**
    """
    if not _is_loopback_call(request):
        raise HTTPException(status_code=403,
                            detail="配对码只能在 ECHO 这台机器上生成（本机面板）")
    if not netguard.lan_mode_enabled():
        raise HTTPException(status_code=409,
                            detail="「允许局域网访问」还没开，手机连不上这台机器："
                                   "请把它切成 lan（会自动打开 API 鉴权）并重启 ECHO")
    base = _phone_base_url()
    if not base:
        raise HTTPException(status_code=409,
                            detail="本机没有可用的私有地址：多网卡机器请在设置里手选"
                                   "「对手机公布的地址」（编一个地址给手机 = 它一定连不上）")
    issued = phone_pair.issue()
    return {"ok": True, "code": issued["code"], "expiresAt": issued["expiresAt"],
            "ttlSeconds": issued["ttlSeconds"], "baseUrl": base,
            "addresses": netguard.local_addresses(),
            "pairString": "echo://phone?host=%s&code=%s" % (base.split("://", 1)[-1],
                                                            issued["code"])}


@router.post("/pair/phone/claim")
def phone_pair_claim(body: PhoneClaimIn):
    """设备用配对码换令牌。**唯一一个不带令牌的接口**（也是唯一能从网上调的配对口）。

    守卫是三条（算术见 `app/phone_pair.py` 顶部）：码只有 6 位、5 分钟寿命、用掉即删，
    外加失败限次（连错 20 次锁 5 分钟）。**故意不做"回环限制"** ——
    那会让手机 / 手表根本连不上，而这个接口存在的前提就是"设备在网络上"。

    令牌只在这里、只回这一次：库里存的是 sha256，之后无从取回（丢了就删掉重配）。
    """
    name = (body.name or "").strip() or "mobile"
    ok, why = phone_pair.claim(body.code)
    if not ok:
        raise HTTPException(status_code=403, detail=why)
    row = db.add_api_key_row(name, scopes=["device"])
    return {"ok": True, "token": row.get("token", ""), "keyId": row.get("id"),
            "name": row.get("name", name), "baseUrl": _phone_base_url(),
            "note": "令牌只出现这一次，请设备侧立刻存进安全存储"}


# ---------------------------------------------------------------- API 密钥（移动端预留）
# 安全（2026-09-13 MEDIUM-2）：明文 token 只在创建时返回这一次，库里存 sha256(token)。
@router.post("/keys")
def create_key(body: KeyCreateIn, _auth=Depends(optional_auth)):
    """创建密钥。**返回的 token 只出现这一次**，之后无从取回（丢了就删掉重建）。"""
    token = db.add_api_key(body.name)
    return {"ok": True, "token": token, "name": body.name}


@router.get("/keys")
def list_keys(_auth=Depends(optional_auth)):
    """列出密钥元数据（id/名称/权限/启用/时间）。**不回 token，也不回哈希**。"""
    return {"items": db.list_api_keys()}


@router.delete("/keys/{kid}")
def delete_key(kid: int, _auth=Depends(optional_auth)):
    db.delete_api_key(kid)
    return {"ok": True}


@router.get("/instance")
def api_instance(_auth=Depends(optional_auth)):
    """这台 ECHO 是哪个实例（dev / stable / 未知）—— 只读，给面板挂牌子用。

    2026-10-04：两个面板长得一样，用户分不清开的是哪一棵。名字来自本机的切换器配置
    （`~/.echo-instances.json`）；客户机上没有 → 名字为空 → 面板不挂牌子（交付物不受影响）。
    """
    from app import instance_id
    return instance_id.info()
