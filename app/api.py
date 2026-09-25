# -*- coding: utf-8 -*-
"""api.py — ECHO REST API（面板 / 手机 App / DSH skill / CLI 的统一入口）

鉴权：settings.apiAuthEnabled=false（默认）时全开放（仅本机）；
开启后除 /api/status 外均要求 `Authorization: Bearer <token>`（api_keys 表）。
"""
import os
import tempfile
from typing import List

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

import app.db as db
from app import __version__ as _ECHO_VERSION
from app.config import settings
from app import assistant, manager, meeting, runtime, services, worklog
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
def optional_auth(authorization: str = Header(default="")):
    if not settings.get("apiAuthEnabled", False):
        return None
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    row = db.verify_api_key(token) if token else None
    if not row:
        raise HTTPException(status_code=401, detail="无效或缺失 API 密钥")
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


@router.post("/capability/unpair")
def api_capability_unpair(_auth=Depends(optional_auth)):
    """解除配对（只忘掉本机凭据）。"""
    from app import capability_admin
    ok, message = capability_admin.unpair()
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "pair": capability_admin.pair_view()}


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

    为什么由**服务端**打开、而不是把 URL 交给页面：harness 的登录靠启动时那条带
    `?token=…` 的 URL，token 是密钥 —— 经接口下发等于把它写进浏览器历史和前端内存。
    这里服务端拼好 URL 直接调系统 shell，响应只回**不含 token** 的地址供提示。
    """
    from app import harness_proc
    from app import platform as echo_platform
    if not harness_proc.online():
        if not harness_proc.requested():
            return {"ok": False,
                    "message": "标准版 harness 没在运行：在 设置 → 智能体 里选中"
                               "「标准版 harness」，ECHO 会自动拉起它"}
        return {"ok": False, "message": "独立 harness 没在监听 %s（看 data/logs/harness.log）"
                                        % harness_proc.base_url()}
    tok = harness_proc.token()
    note = ""
    if not tok:
        # 手里没有 token（例如实例是上一轮 ECHO 拉起的）→ 让它重启一次换一枚新的：
        # 浏览器必须带 token 才能真正进界面（我们那枚密钥 Cookie 给不了浏览器）。
        tok, note = harness_proc.ensure_token()
    if not harness_proc.online():
        return {"ok": False, "message": "独立 harness 没在监听 %s（%s）"
                                        % (harness_proc.base_url(), note or "看 data/logs/harness.log")}
    url = harness_proc.base_url() + ("/?token=%s" % tok if tok else "/")
    if not echo_platform.shell_open(url):
        return {"ok": False, "message": "打开浏览器失败（%s）" % harness_proc.base_url()}
    msg = "已在浏览器打开 %s" % harness_proc.base_url()
    if not tok:
        msg += "（没拿到登录 token：%s）" % (note or "建议在设置里填一次")
    return {"ok": True, "url": harness_proc.base_url(), "message": msg}


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


# ---------------------------------------------------------------- 服务运维
@router.get("/models")
def get_models(_auth=Depends(optional_auth)):
    """模型清单：每个功能需要哪些模型、多大、装到哪、怎么获取，以及本地就绪状态与下载进度。

    只读检测 + 任务状态（app/modelinfo.py），GET 不会触发任何下载；面板「设置 → 模型」据此渲染。
    """
    from app import modelinfo
    return {"items": modelinfo.inventory(), "jobs": modelinfo.jobs()}


@router.post("/models/download")
def post_model_download(body: ModelDownloadIn, _auth=Depends(optional_auth)):
    """下载指定模型（后台线程，立即返回；进度用 GET /api/models 轮询）。

    pyannote 只提供复制命令，不支持从此接口触发下载。
    source=copy 的模型仍返回拷贝说明。

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


@router.get("/commands")
def get_commands(limit: int = 100, offset: int = 0, _auth=Depends(optional_auth)):
    return {"total": db.count_commands(),
            "items": db.list_commands(limit=min(limit, 500), offset=max(offset, 0))}


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
    detail["capability"] = capability_admin.plan_summary(
        meta.get("capability"), meta.get("timestampsKinds"))
    return detail


@router.get("/meetings/{mid}/audio")
def meeting_audio(mid: int, seg: int = 1, _auth=Depends(optional_auth)):
    """返回某段录音（支持 Range，供前端同步播放）。

    2026-09-26：历史音频可能已经被**无损压成 FLAC**（`01.flac`，原件已删）。
    面板那边不许知道这件事 —— 这条接口一律回 **WAV**：

      * 是 `.wav` → 直接 `FileResponse`（与改动前逐字一致，不复制、不占额外磁盘）；
      * 是 `.flac` → **用时解码**成临时 WAV 再回（浏览器对 `audio/flac` 的支持
        并不一致，而我们已经有一条可靠的解码路，没必要把它交给浏览器赌）。

    临时文件不在这里删：它是**正在被 HTTP 流式读**的文件，删了会让播放中途断掉。
    它落在 `audiofile.TEMP_DECODE_DIR`，由 `audiofile.gc_temp()`（下次解码时顺手扫）
    与退出时的清理兜底 —— 这一点在代码注释里写明，免得日后有人以为它是泄漏。

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
    if audiofile.is_flac(path):
        try:
            path = audiofile.decode_to_wav(path)
        except audiofile.CompressionError as e:
            raise HTTPException(status_code=500,
                                detail="这段音频（%s）解不开，无法播放：%s"
                                       % (os.path.basename(path), e))
    return FileResponse(path, media_type="audio/wav", filename=f"seg{seg:02d}.wav")


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
    """说话人改名；改名为联系人（非默认名）时按设置自动把声纹入库。"""
    m = db.get_meeting(mid)
    if not m:
        raise HTTPException(status_code=404, detail="会议不存在")
    db.rename_speaker(mid, body.label, body.name)
    msg = ""
    try:
        from app import voiceprint
        if (voiceprint.enabled() and voiceprint.auto_enroll()
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
    ok, msg = meeting.retranscribe_meeting(mid)
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
    """是不是首装（还没写过 ``data/installed-components.json``）。

    **刻意做成最轻的一个接口**：面板启动时就要问它，所以这里只做一次
    ``os.path.isfile`` —— 不能顺手拉 ``/api/wizard/env``（那个要探网，1.5 s 起）。
    面板据此自动进向导（设计 §0/§1：首装必须进向导，不得跳过）。
    """
    from app import wizard
    return {"firstRun": wizard.first_run(), "installed": wizard.installed_path()}


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
# 会议里把说话人改名为联系人即自动入库（voiceprintAutoEnroll）；
# 库里的样本可在面板「说话人管理」查看/删除，这里是对应的 REST 入口。
#
# 返回约定：**业务性失败**（库里没有这个联系人、会议没有声纹样本、开关没开…）一律
# `HTTP 200 + {"ok": false, "message": "人话原因"}`，与既有的 /api/meeting/start|stop、
# /api/models/download 等端点保持一致（面板/手机 App/技能都按 ok 字段判成败）；
# 只有参数校验、鉴权这类框架级错误才走 4xx（FastAPI 校验 422 / optional_auth 的 401）。

@router.get("/voiceprints")
def get_voiceprints(_auth=Depends(optional_auth)):
    """声纹库：联系人 + 样本列表 + 当前生效参数（面板「说话人管理」渲染）。"""
    from app import voiceprint
    thr, margin = voiceprint.thresholds()
    stats = voiceprint.library_stats()
    return {"items": voiceprint.library_view(), "enabled": voiceprint.enabled(),
            "autoEnroll": voiceprint.auto_enroll(), "threshold": thr, "margin": margin,
            "contacts": stats["contacts"], "total": stats["samples"]}


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
    import threading
    threading.Timer(0.5, manager.echo_stop_self).start()
    return {"ok": True, "message": "ECHO 服务即将停止"}


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
