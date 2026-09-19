# -*- coding: utf-8 -*-
"""assistant.py — ECHO 命令流水线

链路（沿用早期实现验证过的逻辑，已去掉 PowerShell 依赖）：
  触发（热键/媒体键/唤醒/面板/API/skill）
    → 提示音开始 → 录音（静音自动停） → 提示音停录
    → 本地转写 → 剥离唤醒前缀 → 会议意图分类（开始/结束录音）
    → 语音复述确认 → 发送到目标 DSH 会话（面板「命令目标」选中的工作区/会话；
      没选就是 ECHO 默认命令会话，queue 模式）
    → 轮询会话历史等最终回复 → 生成语音简报朗读 → 写命令历史

并发控制：同一时刻只允许一个命令流（busy 标志）。
"""
import os
import re
import threading
import time

import app.db as db
from app import paths
from app import providers as providers_mod
from app.config import settings
from app.dsh import get_client, DshError
from app.audio import stt as stt_mod
from app.audio import tts as tts_mod
from app.audio.recorder import record_command

BASE_DIR = paths.echo_root()
# 录音落盘目录走**数据根**（不是安装目录下的 data/）：macOS 的数据根是
# ~/Library/Application Support/ECHO，往 .app 里写是 D18 明确不允许的。
CAPTURES_DIR = os.path.join(paths.data_root(), "captures")

_busy = threading.Lock()
# name：谁占着（web/hotkey/rail/skill…）；phase：做到哪一步了，给面板做按钮动效用
#   listening 正在收音 | transcribing 转写中 | running 已发给 DSH 等回复
_busy_owner = {"name": None, "phase": None}
# 收音阶段的实时电平（0~1），由 record_command 的 level_cb 更新，面板据此画波形
_capture_level = {"value": 0.0}

# 等 DSH 回复的上限（秒）。2026-09-17 之前是 90 秒：语音问"查一下/解释一下"这类
# 需要读代码的问题时，DSH 还在作答就被判超时，回复被丢掉（命令状态仍是 done，
# 但 reply/brief 为空、也没有语音简报）。放宽到 300 秒。
REPLY_TIMEOUT_S = 300


def set_phase(phase):
    """记录当前阶段（面板按钮动效 + 波形用）。"""
    _busy_owner["phase"] = phase
    if phase != "listening":
        _capture_level["value"] = 0.0


def capture_level():
    """收音阶段的实时电平；非收音阶段恒为 0。"""
    return float(_capture_level["value"]) if _busy_owner.get("phase") == "listening" else 0.0

# 唤醒前缀（语音转写后剥离）。基础别名 + 用户配置的唤醒词（含"X帮我"变体）。
_PREFIX_BASE = ["小尼小尼", "小尼帮我", "小尼", "尼欧尼欧", "尼欧帮我", "尼欧", "帮我"]


def _prefix_re():
    entries = []   # (字符长度, 正则片段)，按长度降序保证长前缀优先匹配
    for p in _PREFIX_BASE:
        entries.append((len(p), re.escape(p)))
    for kw in settings.get("wakeKeywords", []) or []:
        core = re.sub(r"[，,、\s]+", "", str(kw).strip())
        if not core:
            continue
        for variant in (core, core + "帮我"):
            if len(variant) == 1:
                pat = re.escape(variant)
            else:
                # 允许唤醒词各字之间夹杂标点/空格（"嘿，尼欧" 与 "嘿尼欧" 都命中）
                pat = r"[，,、\s]*".join(re.escape(ch) for ch in variant)
            entries.append((len(variant), pat))
    seen = set()
    pats = []
    for _ln, pat in sorted(entries, key=lambda x: -x[0]):
        if pat not in seen:
            seen.add(pat)
            pats.append(pat)
    return re.compile(r"^\s*(" + "|".join(pats) + r")[，,、\s]*")
# 会议意图
MTG_START_RE = re.compile(r"^(记录会议|开始记录|记录一下|开始会议|开会|会议模式|记一下|记录|开始录音|录会议)")
MTG_STOP_RE = re.compile(r"^(结束录音|停止录音|停录|结束会议录音|停止会议|停一下|停止记录|结束会议)")


# ---------------------------------------------------------------- 文本处理

def strip_prefix(text):
    pr = _prefix_re()
    return pr.sub("", text, count=1) if pr.match(text) else text


def build_env_context():
    """构造发送给 DSH 的环境上下文（当前时间 + 所在地）。

    DSH 会话对'现在几点 / 今天星期几 / 本地天气'没有概念，
    命令前附带一小段事实，让问答有依据（同类思路）。
    关闭 sendEnvContext 或所在地留空时不附加。
    """
    if not settings.get("sendEnvContext", True):
        return ""
    import datetime
    now = datetime.datetime.now()
    week = "一二三四五六日"[now.weekday()]
    loc = (settings.get("userLocation", "") or "").strip()
    parts = [f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M')}（星期{week}）"]
    if loc:
        parts.append(f"用户所在地：{loc}")
    return "【环境信息】" + "；".join(parts) + "。"


def build_reply_requirement():
    """拼在命令末尾的"极简回复"要求（用户 2026-09-12 要求）。

    为什么要附这段：会话里的完整过程/细节用户会自己回 DSH 看，
    ECHO 这边只负责"一句话结论 + 语音播报"，避免把长报告念出来。
    文案与字数上限都可在设置页改（minimalReply / minimalReplyChars / minimalReplyHint）。
    """
    if not settings.get("minimalReply", True):
        return ""
    tpl = (settings.get("minimalReplyHint", "") or "").strip()
    if not tpl:
        return ""
    try:
        chars = int(settings.get("minimalReplyChars", 60) or 60)
    except (TypeError, ValueError):
        chars = 60
    return tpl.replace("{chars}", str(max(10, chars)))


def _build_confirm(text, max_len=18):
    """生成简短语音确认：'好的，我这就去<命令开头>'；长命令截断到语义边界。"""
    cleaned = re.sub(r"^(请|帮我|请帮我|麻烦帮我|麻烦你帮我|帮我一下|请帮我一下)[，,、\s]*",
                     "", text or "").strip()
    if not cleaned:
        cleaned = text or ""
    if len(cleaned) > max_len:
        cut = cleaned[:max_len]
        for i in range(len(cut) - 1, 0, -1):
            if cut[i] in "，。、；！？":
                cut = cut[:i]
                break
        cleaned = cut + "等"
    return "好的，我这就去" + cleaned


def classify_meeting_intent(text):
    """返回 'start' / 'stop' / None。"""
    if MTG_START_RE.match(text):
        return "start"
    if MTG_STOP_RE.match(text):
        return "stop"
    return None


def _clean_line(t):
    if not t:
        return ""
    t = re.sub(r"```[\s\S]*?```", "，", t)
    t = re.sub(r"`[^`]*`", "", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"#{1,6}\s*", "", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"^\s*[-*+]\s*", "", t)
    t = re.sub(r"^\s*\d+[\.、]\s*", "", t)
    t = re.sub(r"[|\\/]", "，", t)
    t = re.sub(r"[_~^]", "", t)
    t = re.sub(r"[^\u0000-\uFFFF]", "", t)
    t = t.replace("℃", "度")
    return t.strip()


# 详情段落的分隔标记：用户 2026-09-12 起要求回复格式为
#   <极简结论一句话>
#   详情如下：
#   <详细内容>
# 语音只读"极简结论"那一段，详情留给面板/会话看，不念出来。
_DETAIL_SPLIT_RE = re.compile(
    r"\n\s*(?:详情如下|详细如下|细节如下|以下是详情|以下为详情|详情[:：]|详细信息[:：]?)\s*[:：]?")


def conclusion_only(reply):
    """截取最终回复里的"极简结论"部分（"详情如下："之前的首段）。

    找不到详情标记时退化为第一段（空行前）——避免把整篇详情当结论念出来；
    若连空行都没有（旧格式的单行回复），返回全文，由调用方按字数上限截断。
    """
    if not reply:
        return ""
    m = _DETAIL_SPLIT_RE.search(reply)
    if m:
        return reply[:m.start()].strip()
    return re.split(r"\n\s*\n", reply, maxsplit=1)[0].strip()


def brief_text(reply, max_chars=200):
    """从 DSH 最终回复提取语音简报：只取"极简结论"段（已实测验证的清洗规则）。"""
    if not reply:
        return ""
    parts = []
    for ln in re.split(r"\r?\n", conclusion_only(reply)):
        s = _clean_line(ln)
        if not s:
            continue
        if re.match(r"^(来源|参考|数据来源|链接|来自|via)[：:]?", s):
            continue
        if re.match(r"^[，。、；：！？\-\s]+$", s):
            continue
        parts.append(s)
    text = ("，".join(parts)
            .replace("，，", "，").replace("，，", "，")
            .lstrip("，").replace("：，", "：").replace("，。", "。").replace("；，", "；"))
    if len(text) > max_chars:
        cut = text[:max_chars]
        last_dot = max(cut.rfind("。"), cut.rfind("，"))
        if last_dot > int(max_chars * 0.5):
            cut = cut[:last_dot + 1]
        text = cut.rstrip("，。") + "。以上是简要汇报。"
    return text.strip()


# ---------------------------------------------------------------- 通知

def notify(title, text):
    """桌面通知（后台线程，不阻塞）。实现是平台差异，收在接缝里：

    Windows = PowerShell NotifyIcon 气泡；macOS = ``osascript display notification``。
    """
    from app import platform as echo_platform

    def _run():
        try:
            echo_platform.notify(str(title), str(text))
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------- 命令流程

def is_busy():
    return _busy.locked()


def _set_busy(name):
    if _busy.locked():
        return False
    _busy.acquire()
    _busy_owner["name"] = name
    return True


def _release_busy():
    if _busy.locked():
        _busy.release()
        _busy_owner["name"] = None
        _busy_owner["phase"] = None
        _capture_level["value"] = 0.0


def capture(source="hotkey"):
    """热键/媒体键/唤醒触发的完整录音命令流（后台线程执行，立即返回）。"""
    if not _set_busy(source):
        print("[assistant] 已有命令流进行中，忽略本次触发")
        tts_mod.play_beep("err")
        return False
    threading.Thread(target=_capture_worker, args=(source,), daemon=True).start()
    return True


def _transcribe_command(wav, cfg):
    """命令转写：显式配了 `providerAsr` 就走 provider（P5），否则走本地引擎。

    返回 ``(text, note)``：`note` 说明"走了谁 / 为什么没有文本"，供调用方写日志
    （§19 发现③：空结果不许与"引擎挂了"混成一个空串）。

    为什么抽成函数：命令口述与会议转写必须用**同一条判据**
    （`providers.asr_if_configured()`，只写一份），而且抽出来才能不开麦克风就测它。
    """
    from app import providers as providers_mod
    provider, why = providers_mod.asr_if_configured()
    if provider is not None:
        try:
            out = provider.transcribe(wav, lang=cfg.get("sttLanguage", "zh"))
            text = " ".join((out.get("text") or "").split())
            note = "provider=%s" % why
            if out.get("reason"):
                note += " reason=%s" % out["reason"]
            return text, note
        except Exception as e:
            return "", "provider=%s 失败：%s" % (why, e)
    engine = cfg.get("sttModel", "sensevoice")
    stt_engine, stt_model = "whisper", engine
    if engine == "sensevoice":
        stt_engine = "sensevoice"
    elif engine == "sherpa":
        stt_engine = "sherpa"
    try:
        text = stt_mod.transcribe(wav, engine=stt_engine, model=stt_model,
                                  lang=cfg.get("sttLanguage", "zh"),
                                  device=cfg.get("device", "auto"))
    except Exception as e:
        return "", "engine=%s 失败：%s" % (stt_engine, e)
    return " ".join((text or "").split()), "engine=%s" % stt_engine


def _capture_worker(source):
    cfg = settings
    try:
        db.add_log("info", "assistant", f"命令流开始 (source={source})")
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        if cfg.get("beepOnStart", True):
            tts_mod.play_beep("start")

        wav = os.path.join(CAPTURES_DIR, f"voice-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.wav")
        set_phase("listening")
        ok = record_command(
            wav,
            max_ms=int(cfg.get("maxRecordMs", 30000)),
            silence_threshold=float(cfg.get("silenceThreshold", 0.012)),
            hangover_ms=int(cfg.get("silenceHangoverMs", 1100)),
            no_speech_abort_ms=int(cfg.get("noSpeechAbortMs", 4000)),
            device_id=int(cfg.get("inputDeviceId", -1)),
            # 复用录音器已有的电平回调（不开额外采样流，避免历史上 PortAudio 崩溃问题）
            level_cb=lambda v: _capture_level.__setitem__("value", float(v)),
        )
        set_phase("transcribing")
        if cfg.get("beepOnDone", True):
            tts_mod.play_beep("done")
        if not ok:
            db.add_log("warn", "assistant",
                       f"未检测到有效语音（设备={cfg.get('inputDeviceId', -1)}，"
                       f"阈值={cfg.get('silenceThreshold', 0.012)}）")
            tts_mod.play_beep("err")
            if cfg.get("notifyOnSend", True):
                notify("ECHO", "没听清，请再说一次")
            return
        db.add_log("info", "assistant", f"录音完成: {os.path.basename(wav)}")

        text, note = _transcribe_command(wav, cfg)
        if not text:
            db.add_log("warn", "assistant", f"转写为空（{note}）")
            tts_mod.play_beep("err")
            return
        db.add_log("info", "assistant", f"识别: {text[:60]}（{note}）")
        print(f"[assistant] 识别: {text}")

        # 剥离唤醒前缀
        stripped = strip_prefix(text)
        text = stripped if stripped else text

        # 会议意图分流
        intent = classify_meeting_intent(text)
        if intent:
            _handle_meeting_intent(text, intent, source)
            return

        # 语音路径的目标 = 面板「命令目标」下拉保存的选择（没选就是默认命令会话）
        target = _configured_command_target(get_client())

        # 语音复述确认（简短复述要做什么 + 目标，播完再发送）
        if cfg.get("voiceConfirm", True):
            confirm = _build_confirm(text)
            providers_mod.speak_text(confirm + _target_hint(*target), timeout=15)

        _dispatch(text, source, *target)
    finally:
        _release_busy()


def send_text(text, source="web", workspace=None, session_id=None):
    """直接发送文本命令（面板/API/skill 入口；不录音）。返回 (ok, message)。

    workspace/session_id 用于指定命令发送目标（工作区/具体对话）；
    都不给时沿用面板「命令目标」下拉保存的设置，没设置才回到 ECHO 默认命令会话。
    """
    if not _set_busy(source):
        return False, "已有命令流进行中，请稍候"
    try:
        threading.Thread(target=_dispatch, args=(text, source, workspace, session_id), daemon=True).start()
        return True, "已提交"
    finally:
        _release_busy()


def _configured_command_target(client):
    """读取「命令目标」设置（面板仪表盘那两个下拉会自动写入）。

    返回 (workspace, session_id)；都没配置时返回 (None, None) → 走默认命令会话。
    只对 DSH 后端生效（CodeBuddy CLI 之类没有"工作区/会话"概念）。
    配置的会话若已被归档/删除 → 丢掉会话，回退到"该工作区自动"。
    """
    if not hasattr(client, "list_sessions"):
        return None, None
    ws = str(settings.get("commandTargetWorkspace", "") or "").strip()
    sid = str(settings.get("commandTargetSession", "") or "").strip()
    if sid:
        try:
            known = {it.get("sessionId") for it in client.list_sessions()}
        except Exception:
            known = None
        if known is not None and sid not in known:
            db.add_log("warn", "assistant",
                       f"命令目标会话已不存在（{sid}），本次回退到「该工作区自动」")
            sid = ""
    return (ws or None), (sid or None)


def _target_hint(workspace=None, session_id=None):
    """语音复述确认时附带的目标提示（只在配置了目标时才念）。

    入参用 _configured_command_target() 校验后的结果，避免"会话已失效"时念错。
    """
    ws = str(workspace or "").strip()
    sid = str(session_id or "").strip()
    if not (ws or sid):
        return ""
    name = os.path.basename(ws.rstrip("\\/")) if ws else ""
    if sid:
        return f"，发到「{name}」的指定会话" if name else "，发到指定会话"
    return f"，发到「{name}」"


# 发给 DSH 时附加的两行提示（见 build_env_context / build_reply_requirement）。
# 面板「命令历史 → 看会话」回看原文时要摘掉，否则每次指令都拖着一行环境信息 + 一段要求。
_INJECT_PREFIXES = ("【环境信息】", "【回复要求】")


def strip_injections(text):
    """去掉 ECHO 自己附加的环境信息行与极简回复要求行（只删整行，用户原话不动）。"""
    if not text:
        return ""
    lines = [ln for ln in str(text).splitlines()
             if not ln.lstrip().startswith(_INJECT_PREFIXES)]
    return "\n".join(lines).strip()


def _dispatch(text, source, workspace=None, session_id=None):
    """发送到 DSH + 等回复 + 简报 + 历史。"""
    cfg = settings
    set_phase("running")   # 已进入"发给 DSH 等回复"阶段（打字命令直接从这一步开始）
    client = get_client()
    # 语音路径（媒体键/唤醒/麦克风按钮）不带目标 → 沿用面板「命令目标」下拉保存的选择
    if not workspace and not session_id:
        workspace, session_id = _configured_command_target(client)
    cmd_id = db.add_command(text, source=source, status="pending",
                            meta={"workspace": workspace, "session_id": session_id} if (workspace or session_id) else None)
    db.add_event("command_received", {"id": cmd_id, "text": text, "source": source})
    db.add_log("info", "assistant", f"命令[{source}]: {text}"
               + (f" → 工作区={workspace}" if workspace else "")
               + (f" → 会话={session_id}" if session_id else ""))

    if not client.ping():
        db.update_command(cmd_id, status="failed", error="DSH 未运行")
        db.add_log("error", "assistant", "DSH 未运行，命令未发送")
        tts_mod.play_beep("err")
        if cfg.get("notifyOnSend", True):
            notify("ECHO", "DSH 未运行，命令未发送")
        return False

    try:
        if workspace or session_id:
            # 显式指定目标（工作区/具体会话）：不使用默认会话轮换策略
            sid = client.resolve_target_session(workspace=workspace, session_id=session_id)
        else:
            # 默认命令会话：空闲超时且未要求延续上一话题时自动轮换新会话
            sid = client.ensure_command_session(text)
        if not sid:
            sid = client.ensure_session("command", name="命令会话")
    except DshError as e:
        db.update_command(cmd_id, status="failed", error=str(e))
        tts_mod.play_beep("err")
        return False
    if not sid:
        db.update_command(cmd_id, status="failed", error="无法获取 DSH 会话")
        tts_mod.play_beep("err")
        return False

    # 会话若被卡住（上一轮停在交互提问/超长工具），先取消再发，保证命令可进
    client.clear_stuck(sid)

    t0 = time.time()
    db.update_command(cmd_id, status="sent", session_id=sid)
    try:
        # 顺序：环境信息（事实前提）→ 用户原话 → 极简回复要求（贴近生成位置，模型更容易遵守）
        ctx = build_env_context()
        req = build_reply_requirement()
        prompt_text = "\n".join(x for x in (ctx, text, req) if x)
        client.prompt(sid, prompt_text, mode="queue")
        if req:
            db.add_log("info", "assistant", f"已附极简回复要求（上限 {settings.get('minimalReplyChars', 60)} 字）")
    except DshError as e:
        db.update_command(cmd_id, status="failed", error=str(e))
        db.add_log("error", "assistant", f"发送失败: {e}")
        tts_mod.play_beep("err")
        return False

    db.add_event("command_sent", {"id": cmd_id, "session": sid})
    if cfg.get("beepOnSend", True):
        tts_mod.beep_ok()
    if cfg.get("notifyOnSend", True):
        notify("ECHO", f"已发送: {text}")

    # 等回复 + 语音简报
    reply, _done = client.wait_for_reply(sid, timeout=REPLY_TIMEOUT_S, poll=0.5)
    duration = int((time.time() - t0) * 1000)
    if reply:
        brief = brief_text(reply, max_chars=int(cfg.get("maxBriefChars", 200)))
        db.update_command(cmd_id, status="done", reply=reply, brief=brief,
                          duration_ms=duration)
        db.add_log("info", "assistant", f"完成({duration}ms)，简报: {brief[:80]}")
        db.add_event("command_done", {"id": cmd_id})
        if cfg.get("voiceBrief", True) and brief:
            providers_mod.speak_async(brief)
        return True
    db.update_command(cmd_id, status="done", reply="", brief="", duration_ms=duration)
    db.add_log("warn", "assistant", f"{REPLY_TIMEOUT_S} 秒内未收到助手回复")
    return True


def _handle_meeting_intent(text, intent, source):
    """会议意图：开始/结束会议录音（复用 meeting 模块）。"""
    from app import meeting
    if intent == "start":
        ok, msg = meeting.start_meeting()
        tts_mod.play_beep("start" if ok else "done")
    else:
        ok, msg = meeting.stop_meeting()
        tts_mod.play_beep("send" if ok else "done")
    cmd_id = db.add_command(text, source=source, status="done",
                            meta={"kind": "meeting", "intent": intent})
    db.update_command(cmd_id, reply=msg)
    db.add_log("info", "assistant", f"会议命令[{intent}]: {msg}")
    if ok and intent == "start":
        providers_mod.speak_async("开始录音")
    elif ok and intent == "stop":
        providers_mod.speak_async("录音已结束")
