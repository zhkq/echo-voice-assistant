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
from app.audio import recorder
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

# ---------------------------------------------------------------- 让出麦克风（D1 单向抢占）
#
# 2026-09-23：会议开始录音时，如果语音指令正占着**会议要用的那个设备**，要让出。
#
# **为什么只允许这一个方向**（见 docs/统一路由-模型能力与设备.md §3.6.1）：
#   会议 → 指令：被中断的是一句 ≤30 秒的语音指令，代价是"重说一遍" → 可以；
#   指令 → 会议：被中断的可能是两小时的录音，代价是"白录一场" → 绝对不行。
# 判据是"被中断的代价"，不是"谁优先级高"。
#
# 麦克风只在 record_command 期间被持有（转写、发 DSH 都不占麦），
# 所以"指令正占着麦"的窗口就是**录那几秒** —— 先等它自然收尾，超时才中止。
_capture_stop = threading.Event()     # 置位 = 请当前采集立刻停止（record_command 会在帧边界返回）
_capture_active = threading.Event()   # 置位 = 此刻**正持有麦克风**（只覆盖 record_command）


def cancel_capture(reason="开始会议录音"):
    """请求中止当前语音指令的采集（目前只有"会议要开麦"这一条路会调）。

    返回 True 表示真的发出了中止请求（当时确实在采集）。
    """
    if not _capture_active.is_set():
        return False
    _capture_stop.set()
    try:
        db.add_log("info", "assistant", f"语音指令已取消：{reason}（让出麦克风）")
    except Exception:
        pass
    return True


def yield_capture_for_meeting(timeout=3.0):
    """会议要开麦了：先等指令**自然收尾**，超时才中止它。

    等一等是划算的：多数情况用户已经说完、采集就在收尾（几秒），
    这样**用户无感、那句指令也不丢**；只有真说了很长的话才动中止。

    返回 True 表示"等不及、中止了"（调用方可以据此提示）。
    """
    if not _capture_active.is_set():
        return False
    deadline = time.time() + max(0.0, float(timeout))
    while _capture_active.is_set() and time.time() < deadline:
        time.sleep(0.05)
    if not _capture_active.is_set():
        return False                      # 自己收尾了，指令没丢
    return cancel_capture("开始会议录音")

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
    if not _busy.acquire(blocking=False):
        return False
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


#: `_transcribe_command()` 的第三个返回值 —— 决定调用方怎么跟人说话
ASR_OK = "ok"          # 拿到文本
ASR_EMPTY = "empty"    # 引擎正常，但这段没听到内容（该提示"再说一次"）
ASR_ERROR = "error"    # 引擎/依赖/模型出问题（该提示"引擎没装/报错"，**不能**说"没听清"）


def _transcribe_via_backend(wav, cfg, lang="auto"):
    """用**能力后端**转写（与会议转写同一个引擎）—— 回顾那条路用它。

    返回与本地引擎同样的形状 `{"text","status","detail"}`（`status` 取 `stt_mod.TRANSCRIBE_*`），
    这样 `_transcribe_command` 的上层判断一行都不用改。

    **为什么值得单独走这条**（用户 2026-10-07 的要求）：回顾每轮是**成段的自述**，
    而本地 sherpa 是给短口令调的小流式模型 —— 实测同一段话出来是
    "今天天的美丽每日回回国回顾我今天今天主要的工作就是…"（重复、错字），
    交给 DSH 整理的原文质量直接受影响。会议那条路用的后端 Qwen3-ASR 明显更准。

    **失败一律回落本机**（不抛）：后端没起来 / 没配对 / 显存不够时，回顾仍然要能用 ——
    用户可能正是"为了省显存没开后端"才用它，那时报错等于把功能关掉。
    """
    try:
        from app.capabilities import echo_server as _echo_backend
    except Exception as e:                                     # pragma: no cover - 导入护栏
        return {"text": "", "status": stt_mod.TRANSCRIBE_ERROR,
                "detail": "后端客户端导入失败：%s" % e}
    try:
        if not _echo_backend.configured():
            return {"text": "", "status": stt_mod.TRANSCRIBE_ERROR,
                    "detail": "能力后端没配对、也没在设置里填地址"}
        client = _echo_backend.client_from_settings()
        if client is None:
            return {"text": "", "status": stt_mod.TRANSCRIBE_ERROR,
                    "detail": "拿不到能力后端客户端"}
        res = client.transcribe(wav, lang=lang, variant="long")
        text = " ".join((getattr(res, "text", "") or "").split())
        if not text:
            return {"text": "", "status": stt_mod.TRANSCRIBE_EMPTY,
                    "detail": "后端返回空文本"}
        return {"text": text, "status": stt_mod.TRANSCRIBE_OK,
                "detail": "backend=%s" % getattr(res, "backend_id", "echo-server")}
    except Exception as e:
        # 回落由调用方做；这里如实说"后端没成"，不假装成功
        return {"text": "", "status": stt_mod.TRANSCRIBE_ERROR,
                "detail": "能力后端转写失败：%s: %s" % (type(e).__name__, e)}


def review_stt_prefer(cfg=None):
    """回顾转写走哪条路 —— 由设置 `dailyReviewSttBackend` 决定（默认走能力后端）。

    单独抽出来是为了**一处判据、可测**：面板/日志/`_review_worker` 都问它，
    免得"设置说的是 A、代码走的是 B"这种漂移（今天已经在别处踩过一次）。
    """
    cfg = cfg or settings
    try:
        val = str(cfg.get("dailyReviewSttBackend", "echo-server") or "").strip()
    except Exception:
        return "backend"
    # 只有显式选「本机」才不走后端；空值/未知值都按默认（后端）处理 ——
    # 默认值的语义是"跟会议同一个引擎"，而设置项写错时不该悄悄退回低质量那条路。
    return "local" if val == "local" else "backend"


def _transcribe_command(wav, cfg, prefer=""):
    """命令转写：显式配了 `providerAsr` 就走 provider（P5），否则走本地引擎。

    `prefer="backend"` 时**先试能力后端**（与会议同一个引擎），失败回落本机 ——
    只有每日回顾用它（设置项 `dailyReviewSttBackend`），语音指令那条路保持原样
    （要的是低延迟，不该等后端）。参数名不叫 `engine`：下面 `engine` 是
    `sttModel` 的值，同名会把这次偏好悄悄覆盖掉。

    返回 ``(text, note, status)``：`note` 说明"走了谁 / 为什么没有文本"，供日志；
    `status` ∈ ``ok/empty/error``。

    为什么要分 status（2026-09-23 事故）：稳定版 runtime-core 里没装 `sherpa_onnx`，
    命令转写每次都抛 ModuleNotFoundError 返回空串，而写进日志的只有一句
    「转写为空（engine=sherpa）」—— 读起来像"没听清"，面板不报、用户以为录音坏了，
    真正的根因（缺 pip 包）一个字都没露。判据与会议转写**共用** `transcribe_ex()`
    （它就是为了把"这段没人说话"和"引擎挂了"分开才加的，见 §19 发现③）。
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
            return text, note, ASR_OK if text else ASR_EMPTY
        except Exception as e:
            return "", "provider=%s 失败：%s" % (why, e), ASR_ERROR
    # 回顾那条路：**先用能力后端**（与会议同一个引擎）。失败**回落本机**，并把
    # "后端为什么不成"写进 note —— 否则日志里只剩"engine=sherpa"，
    # 看不出"本来想走后端但没走成"（那正是排障时要问的第一件事）。
    backend_note = ""
    if prefer == "backend":
        be = _transcribe_via_backend(wav, cfg, lang=cfg.get("sttLanguage", "zh"))
        be_text = " ".join((be.get("text") or "").split())
        if be.get("status") == stt_mod.TRANSCRIBE_OK and be_text:
            return be_text, str(be.get("detail") or "能力后端"), ASR_OK
        backend_note = "后端没成（%s），回落本机；" % (be.get("detail") or be.get("status"))
        db.add_log("warn", "assistant",
                   "回顾转写走后端没成功，已回落本机：%s" % (be.get("detail") or be.get("status")))
    engine = cfg.get("sttModel", "sensevoice-onnx")
    # ⚠️ 这里以前是**手工 if/elif**，只认 sensevoice / sherpa，其余一律落到 whisper ——
    # 于是 2026-10-08 把默认值改成 `sensevoice-onnx` 之后，**语音指令会被当成 whisper**
    # （而 whisper 权重早就删了）→ 每次转写都失败，且报的是"engine=whisper"这种误导信息。
    # 现在改成调**唯一权威**的映射函数 `stt.resolve_engine()`：新加引擎只需要在
    # `stt.py` 里加一个分支，这里不会再漏。
    stt_engine, stt_model = stt_mod.resolve_engine(engine)
    try:
        res = stt_mod.transcribe_ex(wav, engine=stt_engine, model=stt_model,
                                    lang=cfg.get("sttLanguage", "zh"),
                                    device=cfg.get("device", "auto"))
    except Exception as e:                     # transcribe_ex() 自己不抛，这里只是护栏
        return "", backend_note + "engine=%s 失败：%s" % (stt_engine, e), ASR_ERROR
    text = " ".join((res.get("text") or "").split())
    note = backend_note + "engine=%s status=%s" % (stt_engine, res.get("status"))
    if res.get("detail"):
        note += " detail=%s" % res["detail"]
    if res.get("status") != stt_mod.TRANSCRIBE_OK or not text:
        return text, note, ASR_ERROR if res.get("status") == stt_mod.TRANSCRIBE_ERROR else ASR_EMPTY
    return text, note, ASR_OK


#: 引擎的 pip 模块名 → 包名（`pip install` 用包名，报错里出现的是模块名）
_ASR_MODULE_PKGS = (("sherpa_onnx", "sherpa-onnx"), ("faster_whisper", "faster-whisper"),
                    ("funasr", "funasr"), ("qwen_asr", "qwen-asr"), ("ctranslate2", "ctranslate2"))


def asr_failure_hint(note):
    """把转写失败的原因翻成**一句用户能照着做的话**（桌面通知里用）。

    `note` 是给日志的原始串（可能含 `ModuleNotFoundError: No module named 'sherpa_onnx'`）；
    对用户直接抛这句等于没说 —— 要告诉他缺什么、去哪儿装。
    """
    low = str(note or "").lower()
    for mod, pkg in _ASR_MODULE_PKGS:
        if mod in low:
            return "转写引擎没装（缺 %s）：面板「能力」页那行有可复制的安装命令" % pkg
    if "文件不存在" in low or "no such file" in low:
        return "录音文件没生成，请再说一次"
    if "out of memory" in low or "显存" in low:
        return "转写引擎显存不足：可换成 CPU 或更小的模型（设置 → 模型）"
    return "转写引擎报错，命令没发出去：%s" % str(note or "")[:120]


def _capture_worker(source):
    cfg = settings
    try:
        db.add_log("info", "assistant", f"命令流开始 (source={source})")
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        if cfg.get("beepOnStart", True):
            tts_mod.play_beep("start")

        wav = os.path.join(CAPTURES_DIR, f"voice-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.wav")
        set_phase("listening")
        _capture_stop.clear()
        _capture_active.set()      # 从这里到 finally：正持有麦克风（会议侧据此决定等还是中止）
        try:
            ok = record_command(
                wav,
                max_ms=int(cfg.get("maxRecordMs", 30000)),
                silence_threshold=float(cfg.get("silenceThreshold", 0.012)),
                hangover_ms=int(cfg.get("silenceHangoverMs", 1100)),
                no_speech_abort_ms=int(cfg.get("noSpeechAbortMs", 4000)),
                device_id=recorder.resolve_input_device("command"),
                # 复用录音器已有的电平回调（不开额外采样流，避免历史上 PortAudio 崩溃问题）
                level_cb=lambda v: _capture_level.__setitem__("value", float(v)),
                # D1：会议要开麦时置位它，采集会在下一帧返回（不再等静音收尾）
                stop_event=_capture_stop,
            )
        finally:
            _capture_active.clear()
        set_phase("transcribing")
        if cfg.get("beepOnDone", True):
            tts_mod.play_beep("done")
        if not ok:
            if _capture_stop.is_set():
                # 被"会议要开麦"中止的：这句指令本来就不该继续，**不要说"没听清"**
                # （那句话会误导用户以为是识别问题，他会再说一遍，然后又撞上会议）
                db.add_log("info", "assistant", "语音指令已取消（让出麦克风给会议录音）")
                return
            db.add_log("warn", "assistant",
                       f"未检测到有效语音（设备={recorder.resolve_input_device('command')}，"
                       f"阈值={cfg.get('silenceThreshold', 0.012)}）")
            tts_mod.play_beep("err")
            if cfg.get("notifyOnSend", True):
                notify("ECHO", "没听清，请再说一次")
            return
        db.add_log("info", "assistant", f"录音完成: {os.path.basename(wav)}")

        text, note, status = _transcribe_command(wav, cfg)
        if not text:
            tts_mod.play_beep("err")
            if status == ASR_ERROR:
                # 引擎/依赖/模型的问题：**必须说出原因**。说成"没听清"会把用户
                # 引到麦克风上去查，而真凶是没装的 pip 包（2026-09-23 事故）。
                db.add_log("error", "assistant", f"命令转写失败（{note}）")
                if cfg.get("notifyOnSend", True):
                    notify("ECHO", asr_failure_hint(note))
            else:
                db.add_log("warn", "assistant", f"转写为空（{note}）")
                if cfg.get("notifyOnSend", True):
                    notify("ECHO", "没听清，请再说一次")
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

        # 回顾意图分流（2026-10-06）：说"我们来回顾今天"就切进回顾模式。
        # **放在会议之后**：两者都是"模式切换"型口令，而会议那两条是既有行为，
        # 不能因为新增回顾而改变匹配优先级（"开始记录"不该被回顾抢走）。
        if wants_review_start(text):
            # 先把当前这条命令流结掉，再把 busy 让给回顾线程 ——
            # 否则 `start_review` 里的 `_set_busy` 会因为我们自己占着锁而失败。
            db.add_log("info", "assistant", f"识别到回顾意图：{text[:40]}")
            _release_busy()
            if not start_review(source):
                # 起不来（重复触发/未就绪）：`start_review` 已经用语音说明了原因，
                # 这里只需要把 busy 状态收干净
                db.add_log("warn", "assistant", "回顾模式未能启动")
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


# ================================================================== 每日回顾模式
#
# 与单轮 `capture()` 的区别（这是本模式存在的唯一理由）：
#   * **一次触发、多轮对话**：录音 → 提交 → 朗读反馈 → 再录音……直到用户说"结束回顾"；
#   * 每轮把"原始转写"整段交给 DSH 的 daily-review 技能，ECHO **不改写**；
#   * 只朗读技能声明的【播报】段（`daily_review.extract_broadcast` 三层兜底），
#     语音反馈必须短 —— 用户 2026-10-06 明确要求。
#
# 并发：整场回顾占住 `_busy`（`phase` 用 "review"），期间媒体键/唤醒触发的普通命令
# 会被 `_set_busy` 挡掉。这是有意的：回顾期间麦克风是"给它用的"，插一条别的命令
# 只会把两边的录音搅在一起。
#
# 麦克风：复用普通命令那条路的 `record_command`（前台独占），只是把时长上限与
# 静音窗口换成回顾自己的设置项。**播报期间不收音**（TTS 不占麦锁，但本模式的循环
# 是"录完才播"，天然避让）—— 不做 AEC，见设计文档 §7.1。
_review_running = threading.Event()
_review_stop = threading.Event()      # 置位 = 请本场回顾收尾退出


def review_running() -> bool:
    """本场语音回顾是否正在进行（面板/API 用）。"""
    return _review_running.is_set()


def wants_review_start(text) -> bool:
    """这句话是不是"我们来回顾今天"。"""
    try:
        from app import daily_review
        return daily_review.wants_start(text)
    except Exception:
        return False


def start_review(source="wake") -> bool:
    """进入每日回顾模式（后台线程，立即返回）。

    与 `capture()` 一样是"触发即返回"；重复触発只提示不重入。
    """
    from app import daily_review
    ok, why = daily_review.ready()
    if not ok:
        # 没配好就**当场说出来**：用户已经在车里等着说话了，
        # 沉默是最差的结果（他会以为坏了，然后反复喊唤醒词）。
        db.add_log("warn", "assistant", f"每日回顾不可用：{why}")
        try:
            providers_mod.speak_text("回顾还没配好，你回面板看一眼设置。", timeout=15)
        except Exception:
            pass
        return False
    if _review_running.is_set():
        print("[assistant] 回顾已在进行中，忽略本次触发")
        return False
    if not _set_busy("review"):
        try:
            providers_mod.speak_text("我这会儿正忙，等我说完再来。", timeout=15)
        except Exception:
            pass
        return False
    _review_stop.clear()
    _review_running.set()
    threading.Thread(target=_review_worker, args=(source,), daemon=True).start()
    return True


def stop_review() -> bool:
    """请求结束本场回顾（用户说"结束回顾"或面板按停）。返回是否真的停了一场。"""
    if not _review_running.is_set():
        return False
    _review_stop.set()
    # 正在录音时也要让它立刻松麦（record_command 在帧边界检查 stop_event）
    _capture_stop.set()
    return True


def _review_speak(text, timeout=60):
    """朗读一句（失败只记日志，不打断整场回顾）。

    **最后一道长度闸**：`daily_review.extract_broadcast` 已经按 `dailyReviewBroadcastChars`
    截过，但这里是"无论如何都不会念长稿"的兜底 —— 用户 2026-10-06 明确要求语音反馈简短，
    而这条链路上任何一环（技能输出异常、设置被改成很大的值）都不该让车里听到一整篇。

    上限取 **`min(dailyReviewBroadcastChars, maxBriefChars)`**：
      * 用 `dailyReviewBroadcastChars` 是因为"回顾的播报预算"就该由回顾自己定；
      * 再夹一层 `maxBriefChars` 是为了尊重用户对"整机简报长度"的总设定；
      * 两者都取小 —— 任何一个被调到很大都不该让兜底失效（本用例就是被这条逮住的）。
    """
    if not text:
        return
    try:
        from app import daily_review
        spoken = str(text).strip()
        cap = min(daily_review.broadcast_limit_chars(),
                  max(60, int(settings.get("maxBriefChars", 200) or 200)))
        if len(spoken) > cap:
            cut = spoken[:cap]
            dot = max(cut.rfind("。"), cut.rfind("？"), cut.rfind("！"),
                      cut.rfind("?"), cut.rfind("!"))
            spoken = (cut[:dot + 1] if dot > cap // 2 else cut + "……")
            db.add_log("warn", "assistant",
                       f"回顾播报稿超过 {cap} 字，已截断（原文 {len(text)} 字）")
        providers_mod.speak_text(spoken, timeout=timeout)
    except Exception as e:
        db.add_log("warn", "assistant", f"回顾朗读失败：{type(e).__name__}: {e}")


def _review_worker(source):
    """回顾模式主循环：录一轮 → 交给 DSH → 念播报 → 再来一轮，直到用户喊停。"""
    from app import daily_review

    cfg = settings
    turns = 0
    try:
        db.add_log("info", "assistant", f"每日回顾开始 (source={source})")
        os.makedirs(CAPTURES_DIR, exist_ok=True)

        # 会话与权限**在开场前**准备好：这两步要几秒，放在第一轮之后会让用户干等
        client = get_client()
        acc_ok, acc_why = daily_review.ensure_access(client)
        if not acc_ok:
            db.add_log("warn", "assistant", f"回顾会话权限校正未成功：{acc_why}")
        sid, wid, info = daily_review.ensure_session(client)
        if not sid:
            db.add_log("error", "assistant", "回顾会话没能建立")
            _review_speak("回顾会话没建起来，你回面板看一眼。")
            return
        db.add_log("info", "assistant", f"今天的回顾会话：{sid}（工作区 {info.get('workspace')}）")

        if cfg.get("beepOnStart", True):
            tts_mod.play_beep("start")
        _review_speak("好，开始今天的回顾，你讲。")

        while not _review_stop.is_set():
            turns += 1
            wav = os.path.join(
                CAPTURES_DIR,
                f"review-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.wav")
            set_phase("listening")
            _capture_stop.clear()
            _capture_active.set()
            try:
                ok = record_command(
                    wav,
                    max_ms=daily_review.record_limit_ms(),
                    silence_threshold=float(cfg.get("silenceThreshold", 0.012)),
                    hangover_ms=daily_review.silence_ms(),
                    no_speech_abort_ms=int(cfg.get("noSpeechAbortMs", 4000)),
                    device_id=recorder.resolve_input_device("command"),
                    level_cb=lambda v: _capture_level.__setitem__("value", float(v)),
                    stop_event=_capture_stop,
                )
            finally:
                _capture_active.clear()
            set_phase("transcribing")
            if cfg.get("beepOnDone", True):
                tts_mod.play_beep("done")

            if _review_stop.is_set():
                break
            if not ok:
                # 这一轮没听到内容：提示一次，继续等他讲（**不退出整场**）
                db.add_log("info", "assistant", f"回顾第 {turns} 轮没听到内容")
                if turns == 1:
                    _review_speak("我没听到，你再说一次。")
                else:
                    _review_speak("这一轮我没听清，接着说，或者说结束回顾。")
                continue

            # 回顾这轮：默认走**能力后端**（与会议同一个引擎）—— 成段自述用短口令那个
            # 小流式模型效果差（实测有明显重复/错字，直接拖累 DSH 整理出的原文）。
            text, note, status = _transcribe_command(wav, cfg, prefer=review_stt_prefer(cfg))
            if not text:
                if status == ASR_ERROR:
                    db.add_log("error", "assistant", f"回顾转写失败（{note}）")
                    _review_speak(asr_failure_hint(note))
                    break
                db.add_log("warn", "assistant", f"回顾转写为空（{note}）")
                _review_speak("这一轮我没听清，接着说。")
                continue
            db.add_log("info", "assistant", f"回顾第 {turns} 轮识别：{text[:80]}（{note}）")
            print(f"[assistant] 回顾识别: {text}")

            # 结束口令：这一轮的内容**不再提交**（用户是在说"停"，不是口述内容）
            if daily_review.wants_stop(text):
                db.add_log("info", "assistant", "用户要求结束回顾")
                break

            set_phase("running")
            out = daily_review.submit(text, client=client)
            spoken = out.get("spoken") or ""
            if out.get("ok"):
                db.add_log("info", "assistant",
                           f"回顾第 {turns} 轮完成（{out.get('seconds')}s，"
                           f"播报来源={out.get('source')}）")
            else:
                db.add_log("warn", "assistant",
                           f"回顾第 {turns} 轮失败：{out.get('error')}")
            _review_speak(spoken)
            # 下一轮前清掉"让出麦克风"的残留标记，避免上一轮的 stop 影响下一轮录音
            _capture_stop.clear()

        # 收尾：用户主动喊停才说结束语（异常退出不说，免得听起来像成功了）
        if _review_stop.is_set():
            _review_speak("好，今天的回顾就到这儿。")
        db.add_log("info", "assistant", f"每日回顾结束（共 {turns} 轮）")
    except Exception as e:
        db.add_log("error", "assistant", f"每日回顾异常：{type(e).__name__}: {e}")
        _review_speak("回顾出了点问题，你回面板看一眼。")
    finally:
        _review_running.clear()
        _capture_stop.clear()
        set_phase(None)
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


def _agent_snapshot():
    """**发送当时**用的是哪个智能体（显示名）。取不到就空串 —— 不猜。

    为什么要落进 `commands.meta`：面板「历史 → 指令历史」要显示"这条走的是哪个后端"。
    这是个**快照**，与会议 `meta.json` 里那份能力快照同一条纪律 —— 用户以后换了智能体，
    历史记录仍该说"当时走的是谁"，而不是拿现在的配置去解释过去。
    """
    try:
        from app import agents
        return str(agents.meta(agents.active_name()).get("displayName") or "")
    except Exception:
        return ""


def _dispatch(text, source, workspace=None, session_id=None):
    """发送到 DSH + 等回复 + 简报 + 历史。"""
    cfg = settings
    set_phase("running")   # 已进入"发给 DSH 等回复"阶段（打字命令直接从这一步开始）
    client = get_client()
    # 语音路径（媒体键/唤醒/麦克风按钮）不带目标 → 沿用面板「命令目标」下拉保存的选择
    if not workspace and not session_id:
        workspace, session_id = _configured_command_target(client)
    meta = {"workspace": workspace, "session_id": session_id}
    backend = _agent_snapshot()
    if backend:
        meta["backend"] = backend
    cmd_id = db.add_command(text, source=source, status="pending", meta=meta)
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
