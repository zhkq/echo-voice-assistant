# -*- coding: utf-8 -*-
"""api.py — ECHO REST API（面板 / 手机 App / DSH skill / CLI 的统一入口）

鉴权：settings.apiAuthEnabled=false（默认）时全开放（仅本机）；
开启后除 /api/status 外均要求 `Authorization: Bearer <token>`（api_keys 表）。
"""
import os
import tempfile

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

import app.db as db
from app.config import settings
from app import assistant, manager, meeting, runtime, services, worklog
from app.audio import recorder
from app.audio import stt as stt_mod
from app.audio import tts as tts_mod
from app.pathutil import safe_under as _safe_under

router = APIRouter(prefix="/api")


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


class GuardLogIn(BaseModel):
    """echo-host 守护进程关键事件上报（面板「守护进程关键事件」栏数据源）。

    支持单条 {level, message} 或批量 {events: [{level, message}, ...]}。
    正常刷屏信息（ECHO stderr 转发、健康检查等）由 echo-host 侧过滤，不上报。
    """
    level: str = "info"
    message: str = ""
    events: list[dict] | None = None


# ---------------------------------------------------------------- 状态与配置
@router.get("/status")
def api_status(_auth=Depends(optional_auth)):
    dsh_ok = manager.dsh_ready()
    services.report_dsh("online" if dsh_ok else "offline",
                        "API 可访问" if dsh_ok else "未运行")
    st = meeting.meeting_status()
    # 转写引擎加载状态（detail 展示）
    stt_st = stt_mod.engine_status()
    loaded_desc = ", ".join(f"{e.get('engine')}" for e in stt_st["loaded"]) or "未加载"
    services.report_stt("online" if stt_st["loaded"] else "ready",
                        f"{loaded_desc} · {stt_st['device']}")
    return {
        "components": services.snapshot(),
        "dsh": {"online": dsh_ok},
        "stt": stt_st,
        "meeting": st,
        "busy": assistant.is_busy(),
        "busyOwner": assistant._busy_owner["name"],
        # 命令流当前阶段（listening/transcribing/running）：面板据此给"说话"按钮做动效
        "busyPhase": assistant._busy_owner.get("phase"),
        "uptime": services.uptime(),
        "version": "0.1.0",
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
    # 配置变更后的联动
    if any(k.startswith("wake") for k in updated):
        runtime.stop_wake()
        if settings.get("wakeEnabled", False):
            runtime.start_wake()
    if any(k.startswith("router") for k in updated):
        # 路由相关项（探测间隔/组名/自动注册）落到 dsh-failover/config.json 并热重载
        from app import router_admin
        ok, detail = router_admin.apply_settings(updated)
        if not ok:
            raise HTTPException(status_code=400, detail=f"路由配置未能应用：{detail}")
    # 智能体相关项：清实例缓存，让新选择/新路径立即生效
    if any(k.startswith("agent") for k in updated):
        try:
            from app import agents
            agents.reset()
        except Exception:
            pass
    return {"ok": True, "updated": updated}


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
    """命令发送目标：工作区列表 + 会话列表（供前端下拉选择）。"""
    from app.dsh import get_client, DshError
    client = get_client()
    try:
        workspaces = client.list_workspaces()
        sessions = client.list_sessions_for()
    except DshError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"workspaces": workspaces, "sessions": sessions}


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
    """
    from app import modelinfo
    ok, msg = modelinfo.start_download(body.id.strip(), force=bool(body.force))
    return {"ok": ok, "message": msg}


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


@router.get("/meetings")
def get_meetings(limit: int = 100, offset: int = 0, _auth=Depends(optional_auth)):
    items = db.list_meetings(limit=min(limit, 500), offset=max(offset, 0))
    # 补充 has_summary 等轻量展示字段（列表信息展示优化，2026-09-10）
    for it in items:
        folder = os.path.join(meeting.MEETINGS_DIR, it["name"])
        it["has_summary"] = os.path.isfile(os.path.join(folder, "summary.md"))
    return {"items": items}


@router.get("/meetings/{mid}")
def get_meeting(mid: int, _auth=Depends(optional_auth)):
    detail = meeting.get_meeting_detail(mid)
    if not detail:
        raise HTTPException(status_code=404, detail="会议不存在")
    detail["segments"] = meeting.build_segments(mid)
    folder = os.path.join(meeting.MEETINGS_DIR, detail["name"])
    detail["hasSegments"] = os.path.isfile(os.path.join(folder, "topics.md"))
    return detail


@router.get("/meetings/{mid}/audio")
def meeting_audio(mid: int, seg: int = 1, _auth=Depends(optional_auth)):
    """返回某段录音 wav（支持 Range，供前端同步播放）。

    `seg` 是 int、`{seg:02d}` 不会带分隔符；会议目录名仍走 _safe_under 兜底
    （万一库里的 name 被写进奇怪值，也不至于跑到会议目录之外）。
    """
    m = db.get_meeting(mid)
    if not m:
        raise HTTPException(status_code=404, detail="会议不存在")
    path = _safe_under(meeting.MEETINGS_DIR, _meeting_dirname(m["name"]), f"{seg:02d}.wav")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="音频段不存在")
    from fastapi.responses import FileResponse
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
    return {"ready": ok, "reason": reason,
            "enabled": worklog.enabled(), "mode": worklog.mode(),
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
    path = _safe_under(meeting.MEETINGS_DIR, _meeting_dirname(m["name"]), f"{kind}.md")
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
    """语音合成测试播报。"""
    tts_mod.speak_async("你好，我是 ECHO 语音助手，当前语音合成正常。",
                        settings.get("ttsEngine", "auto"))
    return {"ok": True, "message": "已开始测试播报"}


@router.post("/control/mic/test")
def control_mic_test(_auth=Depends(optional_auth)):
    """麦克风测试：在服务进程内打开录音 1 秒，返回设备与电平（诊断用）。"""
    import numpy as np
    try:
        with recorder._open_input(int(settings.get("inputDeviceId", -1))) as stream:
            data, _ = stream.read(int(16000 * 1.0))
            rms = float(np.sqrt(np.mean((data.astype(np.float32) / 32768.0) ** 2)))
            return {"ok": True, "device": stream.device, "rms": round(rms, 4)}
    except Exception as e:
        db.add_log("error", "api", f"mic test 失败: {e}")
        return {"ok": False, "error": str(e)}


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


@router.post("/guard/log")
def guard_log(body: GuardLogIn, _auth=Depends(optional_auth)):
    """接收 echo-host 守护进程的关键事件并入库（source=guard）。

    仅本机 echo-host 调用；面板「启动」页签的「守护进程关键事件」栏
    通过 GET /api/logs?source=guard 读取。
    """
    allowed = {"info", "warn", "error"}
    items = body.events if body.events is not None else [
        {"level": body.level, "message": body.message}]
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        msg = str(it.get("message", "")).strip()
        if not msg:
            continue
        lv = str(it.get("level", "info")).lower()
        if lv not in allowed:
            lv = "info"
        db.add_log(lv, "guard", msg)
        n += 1
    return {"ok": True, "count": n}


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
