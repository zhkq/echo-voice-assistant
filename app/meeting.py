# -*- coding: utf-8 -*-
"""meeting.py — ECHO 会议业务（录音 → 分段 → 转写 → 说话人分离 → 纪要）

编排（去掉旧实现的 http.server 耦合，数据全部入库）：
  start_meeting()   开录音线程（MeetingRecorder，按分钟分段）
  stop_meeting()    停录 → 后台转写（whisper 或 sensevoice）
                      → 可选 pyannote 说话人分离 → 写入 DB（lines/speakers）
                      → 导出 transcript.md → 可选请求 DSH 生成纪要
  regenerate_summary() / retranscribe_meeting()  手动重跑

文件布局：data/meetings/<2026-08-21_10-00-00>/{01.wav, meta.json, transcript.md, summary.md}
"""
import datetime
import json
import os
import re
import sys
import threading
import time

import app.db as db
from app.config import settings
from app.dsh import get_client
from app import paths, worklog
from app.audio.recorder import MeetingRecorder, resolve_input_device
from app.audio import stt as stt_mod
from app.audio import tts as tts_mod
from app import services

# 安装根由路径层给（含 ECHO_ROOT 覆盖）；会议目录一律走 meetings_dir()（D20/D21）。
BASE_DIR = paths.echo_root()
SAMPLE_RATE = 16000


# ---------------------------------------------------------------- 路径（2.0 / D20、D21）
# 1.x 里这是 import 期常量（`BASE_DIR/data/meetings`），用户改不了；2.0 起由配置项
# `meetingsDir` 决定（D20），且**每次调用都重新解析**（D21）。
#
# 为什么是函数而不是"模块级 __getattr__ + 常量名"：PEP 562 的模块 __getattr__ 只在
# **属性访问**（`meeting.meetings_dir()`）时生效，模块内部函数里的**裸名字**不会走它，
# 会直接 NameError。所以内部的 17 处引用统一改成调用 `meetings_dir()`，
# 跨模块的 `meeting.meetings_dir()` 也一并改成函数调用——不留兼容别名，
# 免得"有的地方跟着配置走、有的地方是 import 快照"这种半成品状态。
def meetings_dir() -> str:
    """当前生效的会议目录（用户可在面板里改，改了立刻生效）。"""
    from app import paths
    return paths.meetings_root()


def ensure_meetings_dir() -> str:
    """确保会议目录存在并返回它。录制/写纪要前调用（路径可能随时被用户改）。"""
    root = meetings_dir()
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        pass
    return root


# 保持 1.x"导入后目录就已存在"的行为；配置坏掉时不能因此炸掉 import。
try:
    ensure_meetings_dir()
except Exception:
    pass

# ---------------------------------------------------------------- 状态

_state = {
    "active": False,
    "folder": None,
    "recorder": None,
    "started_at": None,
    "level": 0.0,
    "error": "",
}
# 录音状态锁：start/stop 必须原子化 —— 否则并发 stop（如面板按钮双击/重复请求）
# 会同时通过 active 检查，造成重复转写 + 重复纪要（日志里出现过两次“停止录音”同秒）。
_state_lock = threading.RLock()
_retranscribing = {"set": set(), "lock": threading.Lock()}

# 转写进度：meeting_id -> {phase, seg_index, seg_total, percent, detail, updated_at}
_transcribe_progress = {}
_progress_lock = threading.Lock()


def _set_progress(meeting_id, **kw):
    with _progress_lock:
        _transcribe_progress[meeting_id] = {**_transcribe_progress.get(meeting_id, {}),
                                            **kw, "updated_at": time.time()}


def _clear_progress(meeting_id):
    with _progress_lock:
        _transcribe_progress.pop(meeting_id, None)


def transcribe_progress(meeting_id=None):
    """返回转写进度（无参会话返回全部）。"""
    with _progress_lock:
        if meeting_id is not None:
            p = _transcribe_progress.get(meeting_id)
            return dict(p) if p else None
        return {k: dict(v) for k, v in _transcribe_progress.items()}


def meeting_status():
    return {
        "active": _state["active"],
        "folder": os.path.basename(_state["folder"]) if _state["folder"] else None,
        "startedAt": _state["started_at"],
        "level": _state["level"],
        "error": _state["error"],
    }


def recover_orphaned_meetings():
    """启动恢复：进程重启后，数据库里残留的 recording/transcribing 会议
    已不可能仍在录/在转写（内存状态已丢失），统一标记为 interrupted。
    音频文件保留，可进会议详情手动「重新转写」。

    防误伤说明：重复实例（守护误判拉起、手动重复启动等）已在 app/main.py
    入口处通过「服务端口占用探测」拦截退出，根本走不到本函数 —— 能执行到
    这里的实例必然已成功绑定服务端口、是当前唯一的 ECHO 实例，因此 DB 里
    残留的 recording 会议一定是崩溃残留，可以安全标记。
    """
    try:
        n = 0
        for m in db.list_meetings(limit=500):
            if m["status"] in ("recording", "transcribing"):
                db.set_meeting_status_by_name(m["name"], "interrupted")
                n += 1
                db.add_log("warn", "meeting",
                           f"检测到中断的会议（进程重启），已标记 interrupted: {m['name']}")
        if n:
            db.add_log("info", "meeting", f"启动恢复：共标记 {n} 个中断会议")
    except Exception as e:
        db.add_log("warn", "meeting", f"会议状态恢复失败: {e}")


# ---------------------------------------------------------------- 录音

def start_meeting():
    with _state_lock:
        if _state["active"]:
            return False, "会议录音已在进行中"
        old = _state["recorder"]
        if old and old.thread and old.thread.is_alive():
            return False, "上次录音尚未退出，请稍后重试；持续异常请重启 ECHO"
        cfg = settings
        now = datetime.datetime.now()
        folder = os.path.join(meetings_dir(), now.strftime("%Y-%m-%d_%H-%M-%S"))
        os.makedirs(folder, exist_ok=True)

        meta = {
            "start": now.isoformat(timespec="seconds"),
            "config": {
                "sttModel": cfg.get("meetingSttModel", "small"),
                "sttDevice": cfg.get("device", "auto"),
                "segmentMinutes": cfg.get("meetingSegmentMinutes", 10),
                "autoSummarize": cfg.get("meetingAutoSummarize", True),
                "diarize": cfg.get("meetingDiarize", False),
            },
            "segments": [],
        }
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        meeting_id = db.create_meeting(
            os.path.basename(folder), started_at=meta["start"],
            stt_model=meta["config"]["sttModel"], stt_device=meta["config"]["sttDevice"],
            diarize=1 if meta["config"]["diarize"] else 0)

        # 2026-09-23（D1 单向抢占）：会议要开麦了，先请**正在收音**的语音指令让出。
        #
        # 为什么只允许这个方向：被中断的是一句 ≤30 秒的指令（重说一遍就行），
        # 而会议可能两小时 —— 反过来让指令打断会议就是白录一场
        # （AGENTS.md 里那次事故正是这类伤害）。见 docs/统一路由-模型能力与设备.md §3.6.1。
        #
        # 先等它自然收尾（多数情况用户已说完、几秒内就结束 → 用户无感、指令也不丢），
        # 超时才中止。麦克风只在收音期间被持有，转写/等 DSH 都不占麦。
        try:
            from app import assistant as _assistant
            if _assistant.yield_capture_for_meeting(timeout=3.0):
                db.add_log("info", "meeting",
                           "开始录音：上一条语音指令因让出麦克风被取消（等了 3 秒仍未收尾）")
        except Exception as e:                      # 抢占失败不该挡住会议
            db.add_log("warn", "meeting", f"让出麦克风的处理失败（不影响录音）：{e}")

        recorder = MeetingRecorder(
            folder,
            segment_minutes=int(cfg.get("meetingSegmentMinutes", 10)),
            device_id=resolve_input_device("meeting"),
            level_cb=lambda lv: _state.update(level=lv),
        )
        recorder.start()

        # 同步校验输入流是否真的打开：打不开就当场失败，避免界面显示"录音中"
        # 却一条音频都没录到（2026-09-16 空会议就是设备打不开后线程静默退出）。
        if not recorder.wait_started(timeout=6):
            err = recorder.error or "打开麦克风超时（设备被占用或权限不足）"
            stopped = recorder.stop()
            db.update_meeting(meeting_id, status="error")
            _state.update(active=False, folder=None, recorder=None if stopped else recorder,
                          started_at=None, level=0.0, error=err)
            db.add_log("error", "meeting", f"开始录音失败（{os.path.basename(folder)}）：{err}")
            return False, f"无法开始录音：{err}"

        _state.update(active=True, folder=folder, recorder=recorder,
                      started_at=meta["start"], error="")
        threading.Thread(target=_watch_recorder, args=(recorder,), daemon=True).start()
        db.add_event("meeting_started", {"meeting": os.path.basename(folder), "id": meeting_id})
        db.add_log("info", "meeting", f"开始录音: {os.path.basename(folder)}")
        return True, os.path.basename(folder)


def _watch_recorder(recorder):
    """An unexpected device failure must not leave the UI claiming it is recording."""
    recorder.thread.join()
    with _state_lock:
        if _state["recorder"] is recorder and _state["active"]:
            recorder.error = recorder.error or "录音意外结束"
            stop_meeting()


def stop_meeting():
    with _state_lock:
        if not _state["active"]:
            return False, "没有进行中的会议"
        recorder = _state["recorder"]
        folder = _state["folder"]
        if not recorder.stop():
            err = "麦克风尚未释放，录音正在停止；请勿重复开麦，持续异常请重启 ECHO"
            _state.update(error=err, level=0.0)
            return False, err
        _state.update(active=False, level=0.0, recorder=None)

        meta_path = os.path.join(folder, "meta.json")
        meta = _load_json(meta_path, {})
        # 段列表 = 本次录出来的 **∪ 目录里已有的 *.wav**。
        # 为什么把目录里那些也算上（2026-09-23）：把**另一段录音**（比如上一场被中断的）
        # 拷进本场目录当 `00.wav`，就应当本次一并转写 —— 用户在电话里就是这么预期的
        # （"拷进去是不是结束后就自动转了"）。只认录音器自己的内存清单时，拷进去的
        # 那段会被**静默忽略**（`_transcribe_impl` 优先用 meta["segments"]，非空就不看目录）。
        extra = [f for f in os.listdir(folder) if re.match(r"^\d+\.wav$", f)]
        segs = sorted(set(recorder.segments) | set(extra))
        meta["end"] = datetime.datetime.now().isoformat(timespec="seconds")
        meta["segments"] = segs
        meta["durationSeconds"] = sum(_wav_seconds(os.path.join(folder, s)) for s in segs)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        _state["folder"] = None

        meeting = db.get_meeting_by_name(os.path.basename(folder))
        if recorder.error and segs:
            if meeting:
                db.update_meeting(meeting["id"], ended_at=meta["end"],
                                  duration_seconds=meta["durationSeconds"],
                                  segments=len(segs), status="interrupted")
            err = f"录音中断：{recorder.error}；已保留音频，可手动重新转写"
            _state.update(error=err)
            db.add_event("meeting_stopped", {"meeting": os.path.basename(folder), "error": err})
            db.add_log("error", "meeting", err)
            return False, err
        if not segs:
            # 没录到任何音频：直接标 error，不再假装"转写中"（否则永远卡住，
            # 因为转写拿到 0 分段会立刻返回）。见 2026-09-16 的设备打开失败。
            err = (recorder.error if recorder else "") or "录音过程没有产生任何音频分段"
            if meeting:
                db.update_meeting(meeting["id"], ended_at=meta["end"],
                                  duration_seconds=0, segments=0, status="error")
            _state.update(error=err)
            db.add_event("meeting_stopped", {"meeting": os.path.basename(folder), "error": err})
            db.add_log("error", "meeting",
                       f"录音结束但无音频（{os.path.basename(folder)}）：{err}")
            return False, f"没有录到音频：{err}"

        if meeting:
            db.update_meeting(meeting["id"], ended_at=meta["end"],
                              duration_seconds=meta["durationSeconds"],
                              segments=len(segs), status="transcribing")

        # 后台转写（不阻塞）
        threading.Thread(target=_transcribe_meeting, args=(folder,), daemon=True).start()
        db.add_event("meeting_stopped", {"meeting": os.path.basename(folder)})
        db.add_log("info", "meeting", f"停止录音，开始转写: {os.path.basename(folder)}")
        return True, os.path.basename(folder)


# ---------------------------------------------------------------- 转写

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _wav_seconds(path):
    try:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0


def _assign_speakers(seg_rows, turns):
    """说话人分离结果匹配到转写行：按覆盖时长取最长说话人（已实测验证）。"""
    if not turns:
        return [(seg, s, e, "", txt) for seg, s, e, txt in seg_rows]
    turns = sorted(turns, key=lambda x: x[0])
    out = []
    for seg_idx, s_start, s_end, text in seg_rows:
        coverage = {}
        for start, end, spk in turns:
            ov = min(s_end, end) - max(s_start, start)
            if ov > 0:
                coverage[spk] = coverage.get(spk, 0.0) + ov
        if coverage:
            speaker = max(coverage, key=coverage.get)
        else:
            mid = (s_start + s_end) / 2.0
            speaker = min(turns, key=lambda x: min(abs(mid - x[0]), abs(mid - x[1])))[2]
        out.append((seg_idx, s_start, s_end, speaker, text))
    return out


def _ensure_speaker_column(rows):
    """把转写行统一成 5 元组 ``(seg, start, end, speaker, text)``。

    文本优先引擎（SenseVoice / Qwen3-ASR）与 whisper/provider 路径产出的都是 **4 元组**，
    说话人那一列由 `_assign_speakers` 补。这里保证"补过了"这件事**一定发生**：

    原来只有 `if diarize: ... else: ...` 的 else 分支（= 关闭分离）会补空说话人，
    分离**抛异常**时什么都不做，4 元组就一路进到 `db.add_lines`，报
    ``ValueError: not enough values to unpack (expected 5, got 4)`` ——
    前面几十分钟的转写成果全丢（2026-09-23 实测：运行时缺 speechbrain）。
    形状是 db 层的契约，不能靠"分离恰好成功"来维持。
    """
    out = []
    for row in rows:
        if len(row) == 5:
            out.append(tuple(row))
        else:
            seg, s, e, txt = row
            out.append((seg, s, e, "", txt))
    return out


def _align_sentences(sv_text, wsegs):
    """SenseVoice 整段文本（带标点）对齐 whisper 碎句时间轴，按标点切句。"""
    import difflib
    if not wsegs or not sv_text:
        return []
    w_chars, w_times = [], []
    for st, en, txt in wsegs:
        t = (txt or "").strip()
        if not t:
            continue
        n = len(t)
        for i, ch in enumerate(t):
            w_chars.append(ch)
            w_times.append(st + (en - st) * (i + 0.5) / n)
    if not w_chars:
        return []
    sv = list(sv_text)
    sm = difflib.SequenceMatcher(None, sv, w_chars, autojunk=False)
    tmap = {}
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                tmap[i1 + k] = w_times[j1 + k]
    sv_times = []
    last_i, last_t = -1, 0.0
    for i in range(len(sv)):
        if i in tmap:
            last_i, last_t = i, tmap[i]
            sv_times.append(tmap[i])
        else:
            nxt = None
            for j in range(i + 1, len(sv)):
                if j in tmap:
                    nxt = (j, tmap[j])
                    break
            if nxt and last_i >= 0 and nxt[0] != last_i:
                sv_times.append(last_t + (nxt[1] - last_t) * (i - last_i) / (nxt[0] - last_i))
            else:
                sv_times.append(last_t)
    sentences = []
    buf = []
    seg_start = 0.0
    for i, ch in enumerate(sv):
        if not buf:
            seg_start = sv_times[i]
        buf.append(ch)
        if ch in "。！？…":
            txt = "".join(buf).strip()
            if txt:
                sentences.append((seg_start, sv_times[i], txt))
            buf = []
    if buf:
        txt = "".join(buf).strip()
        if txt:
            sentences.append((seg_start, sv_times[-1], txt))
    return sentences


def _boot_meeting_stt(status, detail=""):
    """同步 boot 页 stt-meeting 组件状态（懒导入避免循环依赖）。"""
    try:
        import app.boot as boot
        boot.report("stt-meeting", status=status, detail=detail)
    except Exception:
        pass


def _boot_note_meeting_key():
    try:
        import app.boot as boot
        from app.audio import stt
        eng, model = stt.resolve_engine(settings.get("meetingSttModel", "sensevoice"))
        boot.note_stt_loaded("stt-meeting", stt.engine_key(eng, model))
    except Exception:
        pass


def _transcribe_meeting(folder):
    """后台转写主入口；任何异常写入日志，不静默丢失。"""
    name = os.path.basename(folder)
    meeting = db.get_meeting_by_name(name)
    mid = meeting["id"] if meeting else None
    _boot_meeting_stt("starting", f"转写中 {name}")
    try:
        _transcribe_impl(folder)
        if mid:
            _clear_progress(mid)
        services.report_meeting("idle", f"转写完成 {name}")
        _boot_note_meeting_key()
        _boot_meeting_stt("online", "转写完成 · 引擎已加载")
        tts_mod.beep_ok()          # 转写完成提示音（叮叮）
    except Exception as e:
        import traceback
        msg = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] " \
              f"转写异常: {e!r}\n{traceback.format_exc()}"
        db.add_log("error", "meeting", msg[:1500])
        if meeting:
            db.update_meeting(meeting["id"], status="error")
        if mid:
            _clear_progress(mid)
        services.report_meeting("error", f"转写失败 {name}")
        _boot_meeting_stt("failed", f"转写失败 {name}")
        tts_mod.play_beep("err")   # 转写失败提示音（咚）
        print(msg, file=sys.stderr)


def _fallback_sv_rows(sv, wmodel, seg_path, seg_idx, seg_min, cfg):
    """回退路径：whisper 时间戳骨架 + SenseVoice 文本字符级对齐切句（保留句级时间戳）。"""
    wsegs = []
    try:
        out, _info = stt_mod.transcribe_whisper(wmodel, seg_path, cfg.get("sttLanguage", "zh"))
        wsegs = [(s.start, s.end, s.text.strip()) for s in out]
    except Exception as e:
        print("whisper 时间戳骨架失败:", e, file=sys.stderr)
    sv_text = ""
    try:
        res = sv.generate(input=seg_path, cache={}, language="auto", use_itn=True, batch_size_s=60)
        if res:
            sv_text = re.sub(r"<\|[^|]*\|>", "", res[0].get("text", "") or "").strip()
    except Exception as e:
        print("SenseVoice 转写失败:", e, file=sys.stderr)
    sentences = _align_sentences(sv_text, wsegs) if (sv_text and wsegs) else []
    if not sentences and wsegs:
        sentences = [(st, en, txt) for st, en, txt in wsegs if txt]
    if not sentences and sv_text:
        sentences = [(0.0, seg_min * 60.0, sv_text)]
    return [(seg_idx, st, en, txt) for st, en, txt in sentences]


def _active_asr_provider():
    """会议转写是否走 provider（P5）。**只有用户显式配了 `providerAsr` 才返回实例**。

    判据本身在 `app.providers.asr_if_configured()`（命令口述转写也用它 —— 同一条规则
    只写一份）。这里只做日志与"不可用就回落本地引擎"的包装。
    """
    from app import providers as providers_mod
    inst, why = providers_mod.asr_if_configured()
    if inst is None and "未配置" not in why:
        db.add_log("warn", "meeting", why)
    return inst


def _asr_provider_id():
    from app.config import settings
    return str(settings.get("providerAsr", "") or "").strip()


def _split_provider_text(text, seg_dur):
    """把外部转写返回的整段文本按句切分，并按字数在段时长内均摊时间。

    外部/在线 ASR 只给整段文本（没有词级时间戳）。整段一行会让面板的"逐句跳转"失去意义，
    所以按中文句末标点切句、按字数比例分配起止时间 —— 时间不精确，但**顺序与位置对**，
    且明写在注释里（不假装它是精确时间戳）。
    """
    import re as _re
    text = (text or "").strip()
    if not text:
        return []
    parts = [p for p in _re.split(r"(?<=[。！？!?；;])", text) if p and p.strip()]
    if not parts:
        parts = [text]
    total_chars = sum(len(p) for p in parts) or 1
    out = []
    t = 0.0
    for p in parts:
        dur = max(float(seg_dur) * len(p) / total_chars, 0.05)
        out.append((round(t, 2), round(t + dur, 2), p.strip()))
        t += dur
    return out


def _transcribe_impl(folder):
    meta = _load_json(os.path.join(folder, "meta.json"), {})
    # 重新转写用「当前设置」，meta.json 快照仅作兜底（录音时的配置可能已过期）
    mcfg = meta.get("config", {}) or {}
    cfg = {
        "sttModel": settings.get("meetingSttModel", mcfg.get("sttModel", "small")),
        "sttDevice": settings.get("device", mcfg.get("sttDevice", "auto")),
        "sttLanguage": settings.get("sttLanguage", mcfg.get("sttLanguage", "zh")),
        "segmentMinutes": int(settings.get("meetingSegmentMinutes",
                                           mcfg.get("segmentMinutes", 10))),
        "autoSummarize": bool(settings.get("meetingAutoSummarize",
                                           mcfg.get("autoSummarize", True))),
        "diarize": bool(settings.get("meetingDiarize", mcfg.get("diarize", False))),
    }
    segs = sorted(meta.get("segments", []) or
                  [f for f in os.listdir(folder) if re.match(r"^\d+\.wav$", f)])
    meeting_name = os.path.basename(folder)
    meeting = db.get_meeting_by_name(meeting_name)
    if not segs:
        # 兜底：没有音频无法转写，状态不能停在 transcribing（会永远卡住）
        if meeting:
            db.update_meeting(meeting["id"], status="error")
            db.add_log("error", "meeting", f"没有音频分段，无法转写：{meeting_name}")
        return
    if not meeting:
        return
    meeting_id = meeting["id"]
    db.clear_meeting_lines(meeting_id)
    db.update_meeting(meeting_id, status="transcribing")

    # 进度初始化（面板据此显示第 N/M 段 + 阶段）
    seg_total = len(segs)
    _set_progress(meeting_id, phase="准备模型", seg_index=0, seg_total=seg_total,
                  percent=0, detail=f"共 {seg_total} 段")

    db_rows = []
    speaker_names = {}
    seg_min = int(cfg.get("segmentMinutes", 10))
    diarize = bool(cfg.get("diarize", False))

    registry = None
    if diarize:
        try:
            from app.audio.diarize import diarize_wav_full, SpeakerRegistry
            registry = SpeakerRegistry()
        except Exception as e:
            print("说话人分离模块不可用，跳过:", e, file=sys.stderr)
            diarize = False

    # 声纹识别（常用联系人，issue #6）：库里已有联系人样本时启用。
    # vp_names 整场累计「说话人N → 联系人名」（取相似度最高的一次），
    # vp_merges 记录同一联系人被分成多簇时的合并（大编号并进小编号）。
    vp_matcher = None
    vp_names = {}
    vp_merges = {}
    # 声纹判定统计：整场汇总成一行日志 —— 既避免"静默不认人"（真实故障看不出来），
    # 也是校准阈值/间隔的依据（最高相似度 + 未命中原因分布）。
    vp_stats = {"tried": 0, "hit": 0, "best": 0.0, "best_name": "", "miss": {}}
    if diarize:
        try:
            from app import voiceprint
            if voiceprint.enabled():
                vp_matcher = voiceprint.load_matcher()
                if vp_matcher:
                    db.add_log("debug", "voiceprint",
                               f"{meeting_name}：声纹库已加载"
                               f"（{db.count_voiceprint_contacts()} 位联系人）")
        except Exception as e:
            db.add_log("warn", "voiceprint", f"声纹库不可用，跳过自动识别：{e}")

    # 文本优先引擎（SenseVoice / Qwen3-ASR）：Qwen3-ASR 用 ForcedAligner 原生句子+时间戳；
    # SenseVoice 用 whisper 时间戳骨架 + 字符级对齐切句（保留句级时间戳）
    use_sv = cfg.get("sttModel") in ("sensevoice", "qwen3asr")
    sv_kind = cfg.get("sttModel")
    wmodel = None
    sv = None
    asr_provider = _active_asr_provider()          # P5：显式配了 providerAsr 才走在线/外部转写
    if asr_provider is not None:
        # 走 provider 时**不加载本地引擎**（省显存/省时间；也正是"没有 GPU 也能转写"的意义）
        db.add_log("info", "meeting", "本场转写走 provider（不加载本地模型）：%s"
                   % _asr_provider_id())
    elif use_sv:
        wmodel = stt_mod._get_whisper("small", cfg.get("sttDevice", "auto"))
        if sv_kind == "qwen3asr":
            sv = stt_mod._get_qwen3asr(cfg.get("sttDevice", "auto"),
                                       forced_aligner="Qwen/Qwen3-ForcedAligner-0.6B")
        else:
            sv = stt_mod._get_sensevoice(cfg.get("sttDevice", "auto"))
    else:
        wmodel = stt_mod._get_whisper(cfg.get("sttModel", "small"),
                                      cfg.get("sttDevice", "auto"))

    diarize_fail = ""      # 分离失败只记一次：8 段会议连说 8 遍会淹没日志
    for i, seg in enumerate(segs, start=1):
        seg_idx = int(seg.split(".")[0])
        seg_path = os.path.join(folder, seg)
        percent = round(i / seg_total * 100) if seg_total else 0
        _set_progress(meeting_id, phase="转写中", seg_index=i, seg_total=seg_total,
                      percent=percent, detail=f"第 {i}/{seg_total} 段 · {cfg.get('sttModel', '')}")
        seg_rows = []
        if asr_provider is not None:
            # P5：外部/在线转写。没有词级时间戳，所以服务端返回的文本在本段时长内
            # 按句切分、按字数均摊时间（比"整段一行"更接近本地引擎的输出形状）。
            try:
                out = asr_provider.transcribe(seg_path, lang=cfg.get("sttLanguage", "zh"))
                text = (out.get("text") or "").strip()
                if text:
                    seg_rows = [(seg_idx, st, en, txt) for st, en, txt in
                                _split_provider_text(text, _wav_seconds(seg_path) or seg_min * 60.0)]
                else:
                    # 空结果**显式留痕**：区分"这段没人说话"与"provider 出错"（§19 发现③）
                    why = out.get("reason") or "empty"
                    db.add_log("warn", "meeting",
                               f"{meeting_name} 第{i}段转写为空（{why}）——本段不写行")
            except Exception as e:
                db.add_log("error", "meeting",
                           f"{meeting_name} 第{i}段转写失败（provider）：{e}")
        elif use_sv:
            # Qwen3-ASR：优先用 ForcedAligner 原生时间戳（自然句子），失败回退 whisper 骨架对齐
            if sv_kind == "qwen3asr":
                lang_hint = stt_mod._LANG_MAP.get(str(cfg.get("sttLanguage", "zh")).lower(), None)
                _full_text, sentences = stt_mod._qwen3asr_sentences(sv, seg_path, lang_hint)
                if sentences:
                    seg_rows = [(seg_idx, st, en, txt) for st, en, txt in sentences]
                else:
                    seg_rows = _fallback_sv_rows(sv, wmodel, seg_path, seg_idx, seg_min, cfg)
            else:
                seg_rows = _fallback_sv_rows(sv, wmodel, seg_path, seg_idx, seg_min, cfg)
            if not seg_rows:
                # **静默零行是这套流程最贵的失败**：引擎抛异常只 print 到 stderr，
                # 库里一行不留，事后完全查不出"这场为什么是空的"
                # （2026-09-21 71 分钟那场就是这样）。
                db.add_log("warn", "meeting",
                           f"{meeting_name} 第{i}段没有转出任何文字"
                           f"（引擎 {sv_kind}；引擎异常详情见 data/logs/echo-server.log.err）")
        else:
            try:
                out, _info = stt_mod.transcribe_whisper(wmodel, seg_path, cfg.get("sttLanguage", "zh"))
                seg_rows = [(seg_idx, s.start, s.end, s.text.strip()) for s in out]
            except Exception as e:
                db.add_log("error", "meeting",
                           f"{meeting_name} 第{i}段 whisper 转写失败：{type(e).__name__}: {e}")
                print("转写失败:", e, file=sys.stderr)

        if diarize:
            _set_progress(meeting_id, phase="说话人分离", seg_index=i, seg_total=seg_total,
                          percent=percent, detail=f"第 {i}/{seg_total} 段 · 分离说话人")
            try:
                from app.audio.diarize import diarize_wav_full
                turns_raw, embs, labels = diarize_wav_full(seg_path)
                label_map = registry.map(embs, labels)
                key_map = {}
                for plabel, disp in label_map.items():
                    num = re.sub(r"\D", "", disp)
                    key = "S" + num
                    key_map[plabel] = key
                    speaker_names[key] = disp
                turns = [(s, e, key_map[spk]) for s, e, spk in turns_raw]
                seg_rows = _assign_speakers(seg_rows, turns)
                # 声纹识别：本段每个说话人找常用联系人，整场累计（取相似度最高的一次）
                if vp_matcher is not None:
                    try:
                        from app import voiceprint
                        for disp, m in voiceprint.identify(embs, labels, label_map,
                                                           vp_matcher).items():
                            vp_stats["tried"] += 1
                            if float(m["sim"]) > vp_stats["best"]:
                                vp_stats["best"] = float(m["sim"])
                                vp_stats["best_name"] = m.get("name") or ""
                            if not m["ok"]:
                                r = m.get("reason") or "?"
                                vp_stats["miss"][r] = vp_stats["miss"].get(r, 0) + 1
                                continue
                            vp_stats["hit"] += 1
                            old = vp_names.get(disp)
                            if old is None:
                                db.add_log("info", "voiceprint",
                                           f"{meeting_name} 第{i}段：{disp} → {m['name']}"
                                           f"（相似度 {m['sim']:.2f}，次优 {m['runner']:.2f}）")
                            if old is None or m["sim"] > old[1]:
                                vp_names[disp] = (m["name"], m["sim"])
                        if vp_names:
                            # 同人合并 + 把联系人名写进本场显示名（用户手改过的名字
                            # 由 replace_speakers 的「已有名优先」逻辑保留，不会被顶掉）
                            vp_merges = voiceprint.duplicate_merges(
                                {d: v[0] for d, v in vp_names.items()})
                            for disp, (nm, _sim) in vp_names.items():
                                tgt = vp_merges.get(disp, disp)
                                speaker_names["S" + re.sub(r"\D", "", tgt)] = nm
                    except Exception as e:
                        db.add_log("warn", "voiceprint", f"声纹识别失败（跳过本段）：{e}")
            except Exception as e:
                # 分离不可用不能连累整场转写：形状归一在下面统一做。失败原因也落库
                # （原来只 print 到 stderr，日志里查不到"为什么这场没有说话人"）。
                if not diarize_fail:
                    diarize_fail = f"{type(e).__name__}: {e}"
                    db.add_log("warn", "meeting",
                               f"{meeting_name} 说话人分离不可用，本场不标说话人：{diarize_fail}")
                print("说话人分离失败:", e, file=sys.stderr)

        # 形状归一必须在 extend 之前：分离成功给 5 元组，关闭/失败时这里是 4 元组，
        # 而 db.add_lines 只认 5 元组（见 _ensure_speaker_column 的说明）。
        seg_rows = _ensure_speaker_column(seg_rows)
        db_rows.extend(seg_rows)
        meta.setdefault("transcribed", []).append(seg)
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    # 声纹识别产物：① 同人说话人键合并（行改标到保留键）；② 留存各说话人平均声纹
    remap = ({"S" + re.sub(r"\D", "", d): "S" + re.sub(r"\D", "", t)
              for d, t in vp_merges.items()} if vp_merges else {})
    if vp_merges:
        db_rows = [(seg, s, e, remap.get(spk, spk), txt) for seg, s, e, spk, txt in db_rows]
        db.add_log("info", "voiceprint",
                   f"{meeting_name}：声纹识别合并同人说话人 {len(vp_merges)} 组"
                   f"（{'，'.join(f'{k}→{v}' for k, v in remap.items())}）")
    if diarize and registry is not None:
        try:
            from app import voiceprint
            # 声纹是生物特征：功能关闭时不留存任何样本（默认就是关，见 config.py 注释）。
            # 关闭态的代价：该场会议之后点「识别本场」需要重新转写（届时再开也一样）。
            if voiceprint.enabled():
                emb_map = {}
                for disp, (vec, cnt) in registry.snapshot().items():
                    key = "S" + re.sub(r"\D", "", disp)
                    if remap.get(key, key) != key:
                        # 该键已被并进别的说话人（行里已经没有它）：别再留"幽灵样本"，
                        # 否则声纹库里会出现指向不存在说话人的条目。
                        continue
                    blob, dim = voiceprint.pack(vec)
                    emb_map[key] = (blob, dim, cnt)
                if emb_map:
                    db.replace_speaker_embeddings(meeting_id, emb_map)
        except Exception as e:
            db.add_log("warn", "voiceprint", f"留存说话人声纹样本失败：{e}")

    if vp_stats["tried"]:
        # 整场一行汇总：认了没认、最高相似度多少、为什么没认 —— 校准阈值就看这行
        near = f"，最高相似度 {vp_stats['best']:.2f}"
        if vp_stats["best_name"]:
            near += f"（最接近「{vp_stats['best_name']}」）"
        miss_txt = "，".join(f"{k}×{v}" for k, v in sorted(vp_stats["miss"].items())) or "无"
        db.add_log("info", "voiceprint",
                   f"{meeting_name}：声纹判定 {vp_stats['tried']} 次，命中 {vp_stats['hit']} 次"
                   f"{near}；未命中：{miss_txt}（阈值/间隔可在 设置 → 会议 调整）")

    _set_progress(meeting_id, phase="整理结果", seg_index=seg_total, seg_total=seg_total,
                  percent=100, detail="写入数据库与导出转写文件")
    if speaker_names:
        db.replace_speakers(meeting_id, speaker_names)
    db.add_lines(meeting_id, db_rows)
    db.cleanup_empty_speakers(meeting_id)
    if db_rows:
        db.update_meeting(meeting_id, status="transcribed",
                          duration_seconds=meta.get("durationSeconds", 0),
                          segments=len(segs))
    else:
        # 「一行都没有也叫 transcribed」是假话：面板显示成功、纪要写着"没内容"，
        # 真正原因（引擎异常/依赖缺失）只在 stderr 里 —— 用户看到的是"成功了但空的"。
        # 2026-09-21 那场 71 分钟的会就是这么过去的（2026-09-23 复查发现）。
        reason = "没有转出任何文字（转写引擎失败，或这段录音确实没人说话）"
        db.update_meeting(meeting_id, status="error",
                          duration_seconds=meta.get("durationSeconds", 0),
                          segments=len(segs))
        db.add_log("error", "meeting", f"{meeting_name} 转写结束但一行文字都没有：{reason}")
        try:
            _cur = db.get_meeting(meeting_id)
            _old = ((_cur["notes"] if _cur is not None else "") or "")
        except Exception:
            _old = ""
        # notes 是用户的地盘：只在它空着的时候写，绝不覆盖用户写的东西
        if not _old.strip():
            db.update_meeting(meeting_id, notes=reason)
    export_transcript(meeting_id, folder)

    if cfg.get("autoSummarize", True) and db_rows:
        _set_progress(meeting_id, phase="生成纪要", seg_index=seg_total, seg_total=seg_total,
                      percent=100, detail="已请求生成纪要+议题分段")
        request_summary(meeting_id, folder)
        request_topic_segments(meeting_id, folder)


def export_transcript(meeting_id, folder=None):
    """从 DB 导出 transcript.md（按段分组，带段标题；行时间戳为会议绝对时间）。"""
    if folder is None:
        meeting = db.get_meeting(meeting_id)
        if not meeting:
            return
        folder = os.path.join(meetings_dir(), meeting["name"])
    meeting = db.get_meeting(meeting_id) or {}
    speakers = {s["label"]: s["name"] for s in db.get_speakers(meeting_id)}
    lines = db.get_lines(meeting_id)
    start_ts = meeting.get("started_at", "")
    seg_dur = _seg_duration_map(folder)
    out = [f"# 会议转写 {start_ts}", ""]
    if not lines:
        out.append("（未检测到有效语音）")
    else:
        # 按段分组；段起始 = 之前所有段的时长累计（绝对时间）
        by_seg = {}
        for ln in lines:
            by_seg.setdefault(ln["seg_index"], []).append(ln)
        abs_offset = 0.0
        for seg_idx in sorted(by_seg):
            seg_lines = by_seg[seg_idx]
            dur = seg_dur.get(seg_idx, seg_lines[-1]["end"] if seg_lines else 0)
            seg_abs_start = abs_offset
            seg_abs_end = abs_offset + dur
            out.append(f"## 第 {seg_idx} 段 [{_fmt_ts(seg_abs_start)} - {_fmt_ts(seg_abs_end)}]")
            out.append("")
            for ln in seg_lines:
                spk = speakers.get(ln["speaker_label"], "") or ""
                t_abs = seg_abs_start + float(ln["start"] or 0)
                prefix = f"[{spk}] " if spk else ""
                out.append(f"{prefix}[{_fmt_ts_full(t_abs)}] {ln['text']}")
            out.append("")
            abs_offset = seg_abs_end
    with open(os.path.join(folder, "transcript.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def _fmt_ts(sec):
    sec = int(sec or 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _fmt_ts_full(sec):
    sec = int(sec or 0)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _seg_duration_map(folder):
    """段号 -> 该段音频总时长（秒）。"""
    out = {}
    if not os.path.isdir(folder):
        return out
    for f in os.listdir(folder):
        m = re.match(r"^(\d+)\.wav$", f)
        if m:
            out[int(m.group(1))] = _wav_seconds(os.path.join(folder, f))
    return out


def build_segments(meeting_id):
    """按段聚合转写行，供前端"自然段"视图渲染。

    返回 [{index, duration, start, end, speakers:[{label,name,count}], lines:[...]}]
    """
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return []
    folder = os.path.join(meetings_dir(), meeting["name"])
    seg_dur = _seg_duration_map(folder)
    lines = db.get_lines(meeting_id)
    by_seg = {}
    for ln in lines:
        by_seg.setdefault(ln["seg_index"], []).append(ln)
    out = []
    for seg_idx in sorted(by_seg):
        seg_lines = by_seg[seg_idx]
        spk_count = {}
        for ln in seg_lines:
            lbl = ln["speaker_label"] or "?"
            spk_count[lbl] = spk_count.get(lbl, 0) + 1
        speakers = [{"label": k, "name": k, "count": v} for k, v in
                    sorted(spk_count.items(), key=lambda x: -x[1])]
        duration = seg_dur.get(seg_idx, 0)
        start = seg_lines[0]["start"] if seg_lines else 0
        end = seg_lines[-1]["end"] if seg_lines else duration
        out.append({
            "index": seg_idx,
            "duration": round(duration, 1),
            "start": round(start, 1),
            "end": round(end, 1),
            "speakers": speakers,
            "lines": seg_lines,
        })
    return out


# ---------------------------------------------------------------- 声纹（常用联系人）

def recognize_meeting_speakers(meeting_id):
    """用声纹库给本场重新认人（面板「说话人管理 → 声纹识别」）。

    只用转写时留存的声纹样本，不重新分离、不重新转写；识别到联系人后
    改写说话人名并重导出 transcript.md。返回 voiceprint.recognize_meeting 的结果。
    """
    from app import voiceprint
    res = voiceprint.recognize_meeting(meeting_id)
    if res.get("renamed") or res.get("merged"):
        export_transcript(meeting_id)
    return res


# ---------------------------------------------------------------- 纪要

# 会议纪要会话缓存：meeting_id -> sessionId。
# 语义（2026-09-15 定稿）：**一场会议一个会话**，本场的纪要、分段、语义分段、
# 工作日志归档全部发进它；下一场会议新建。内存缓存之外还落库（db.meeting_sessions），
# 这样 ECHO 重启后不变，归档环节（worklog）也能拿到同一个会话。
_MEETING_SESSIONS = {}
_MEETING_SESSIONS_LOCK = threading.Lock()
# 同一会议的纪要/分段/语义分段请求需串行执行：并发发往同一会话时，
# wait_for_reply 会都抢到第一个完成的回复（整场纪要），导致分段输出被覆盖。
_MEETING_LOCKS = {}
_MEETING_LOCKS_LOCK = threading.Lock()


def _session_key(meeting_id):
    """把会议标识统一成 meeting_sessions 的主键（会议名）。

    调用方给的是 db 主键 int 的地方（如 delete_meeting）和给会议名的地方（如
    纪要流程）都有，这里统一转换，避免两套键各自为政、映射对不上。
    """
    if isinstance(meeting_id, int):
        m = db.get_meeting(meeting_id)
        return (m or {}).get("name") or str(meeting_id)
    return str(meeting_id)


def _summary_session(client, meeting_id):
    """解析本场会议的纪要会话（一会话贯穿整场）。

    优先级：
      1. 内存缓存（最快路径）
      2. 数据库映射（ECHO 重启后仍指向同一会话）
      3. 新建：**在该会议工作区里建**，让 DSH 把它登记进工作区（侧栏归组）。
         关键：session/create 只认 workspaceId 或 cwd 之一；用 workspaceId 建
         才会被登记，用 cwd 建会落到「未分组」。
      未配置 meetingWorkspace 时退回固定的「纪要会话」（旧行为）。
    """
    meeting_id = _session_key(meeting_id)
    ws = (settings.get("meetingWorkspace", "") or "").strip()
    if not ws:
        return client.ensure_session("summary", name="纪要会话")

    with _MEETING_SESSIONS_LOCK:
        sid = _MEETING_SESSIONS.get(meeting_id)
    if sid:
        return sid

    row = db.get_meeting_session(meeting_id, agent=getattr(client, "name", ""))
    if row and row.get("session_id"):
        sid = row["session_id"]
        with _MEETING_SESSIONS_LOCK:
            _MEETING_SESSIONS[meeting_id] = sid
        db.touch_meeting_session(meeting_id)
        return sid

    # 新建：优先走工作区（保证出现在 DSH「会议工作区」分组里）
    sid, workspace_id, how = "", "", ""
    try:
        if getattr(client, "has_workspaces", lambda: False)():
            from app import workspaces as spaces_mod
            wid, created = client.ensure_workspace(
                ws, title=spaces_mod.title_for_path(ws))
            if wid:
                sid = client.create_session(workspace_id=wid)
                workspace_id = wid
                how = f"工作区 {wid}" + ("（新建）" if created else "（已存在）")
    except Exception as e:
        db.add_log("warn", "meeting", f"按工作区创建会议会话失败，回退 cwd 方式：{e}")
    if not sid:
        # 回退：老方式（会话可用，但 DSH 侧会落在「未分组」）
        sid = client.create_session(cwd=ws)
        how = "cwd（未登记工作区，侧栏可能显示未分组）"

    if sid:
        with _MEETING_SESSIONS_LOCK:
            _MEETING_SESSIONS[meeting_id] = sid
        try:
            db.upsert_meeting_session(meeting_id, sid, workspace_id,
                                      agent=getattr(client, "name", ""))
        except Exception as e:
            db.add_log("warn", "meeting", f"会议会话映射落库失败：{e}")
        db.add_log("info", "meeting",
                   f"本场会议新建 DSH 会话 {sid}（{how}）——纪要/分段/归档共用此会话")
    return sid


def _drop_summary_session(meeting_id, archive=True):
    """忘记本场会议的会话；archive=True 时同时在 DSH 侧归档该会话。

    归档后它不再出现在侧栏，也不会掉进「未分组」。
    """
    meeting_id = _session_key(meeting_id)
    with _MEETING_SESSIONS_LOCK:
        sid = _MEETING_SESSIONS.pop(meeting_id, None)
    row = None
    try:
        row = db.get_meeting_session(meeting_id)
    except Exception:
        row = None
    if not sid and row:
        sid = row.get("session_id") or ""
    if archive and sid:
        try:
            client = get_client()
            if getattr(client, "has_workspaces", lambda: False)():
                client.archive_session(sid, workspace_id=(row or {}).get("workspace_id") or "")
                db.add_log("info", "meeting", f"已归档会议会话 {sid}（{meeting_id}）")
        except Exception as e:
            db.add_log("warn", "meeting", f"归档会议会话失败（可忽略）：{e}")
    try:
        db.delete_meeting_session(meeting_id)
    except Exception:
        pass




def _quote_mm_text(raw):
    """mermaid 形状/标签文本：含半角括号等特殊字符但未用引号包裹时补双引号。
    已包裹（"..."）或无需包裹的原文原样返回。

    **引号标签里又嵌半角引号**是 LLM 的常见写法（`…过滤"他人说话"等…`）：内层引号会把
    标签提前闭合，mermaid 解析直接报错。老逻辑漏了这种——它只在"带括号"时才补引号，
    而这种文本里没有括号，于是一路放行（2026-09-20：最新一条会议的纪要就这么挂的）。
    处理办法：把**内层**引号换成 mermaid 实体 `#quot;`，渲染出来仍是 `"`。
    """
    t = raw.strip()
    if not t:
        return raw
    wrapped = len(t) >= 2 and t[0] == '"' and t[-1] == '"'
    if wrapped and t.count('"') == 2:
        return raw  # 已用引号包裹，且内部没有多余引号
    if wrapped and t.count('"') > 2:
        return '"' + t[1:-1].replace('"', "#quot;") + '"'
    if '"' in t:                       # 没加引号却带引号：同样会断解析，一并规范化
        return '"' + t.replace('"', "#quot;") + '"'
    if any(ch in t for ch in "()[]{}|"):
        return '"' + t + '"'
    return raw


def _scan_mm_shape(line, j, expect_close):
    """从 j 起扫描，直到栈空时遇到 expect_close，返回其索引；失败返回 None。
    括号只按嵌套配对，引号字符串整体跳过（视为文本一部分）。"""
    stack = []
    k = j
    n = len(line)
    while k < n:
        c = line[k]
        if c == '"':
            k2 = line.find('"', k + 1)
            if k2 == -1:
                return None
            k = k2 + 1
            continue
        if c in "([{":
            stack.append({"(": ")", "[": "]", "{": "}"}[c])
        elif c in ")]}":
            if stack and stack[-1] == c:
                stack.pop()
            elif stack:
                return None  # 括号不匹配，放弃（保守不改该行）
            elif c == expect_close:
                return k
            else:
                return None
        elif c == expect_close and not stack:
            return k  # 非括号闭符（如边标签 |...| 的 |）
        k += 1
    return None


def _fix_flowchart_line(line):
    """修复一行 flowchart/graph 代码：给节点形状 / 边标签文本中含括号、
    竖线等特殊字符却未加引号的片段补双引号（CY[初验(出验)证书] → CY["初验(出验)证书"]）。
    解析不确定时整行保持原样，绝不做破坏性修改。"""
    out = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == '"':
            j = line.find('"', i + 1)
            if j == -1:
                out.append(line[i:])
                break
            out.append(line[i:j + 1])
            i = j + 1
            continue
        if c == "|":  # 边标签 |...|
            j = _scan_mm_shape(line, i + 1, "|")
            if j is None:
                out.append(line[i:])
                break
            out.append("|" + _quote_mm_text(line[i + 1:j]) + "|")
            i = j + 1
            continue
        if c in "[({":
            nxt = line[i + 1] if i + 1 < n else ""
            if c == "[" and nxt == "(":
                close, tstart = ")", i + 2
            elif c == "[" and nxt == "[":
                close, tstart = "]", i + 2
            elif c == "(" and nxt == "[":
                close, tstart = "]", i + 2
            elif c == "(" and nxt == "(":
                close, tstart = ")", i + 2
            elif c == "{" and nxt == "{":
                close, tstart = "}", i + 2
            else:
                close, tstart = {"[": "]", "(": ")", "{": "}"}[c], i + 1
            k = _scan_mm_shape(line, tstart, close)
            if k is None:
                out.append(line[i:])
                break
            raw = line[tstart:k].strip()
            quoted = _quote_mm_text(raw)
            out.append(line[i:tstart] + quoted + line[k])
            i = k + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _fix_timeline_line(line):
    """修复一行 timeline 代码。

    2026-09-12 实测（用面板自带 mermaid 逐个渲染对照，见提交说明）：
      1) 标题必须是 `title: 文本`（冒号必填）。LLM 常写成 `title 文本`，缺冒号时
         解析器直接报 Expecting 'title',… got 'INVALID'。
      2) **周期文本里不能含冒号**：timeline 用 `:` 分隔"周期 : 事件"，所以
         `00:00:12 : 发起试音` 会被切错，报 Expecting 'period','event' got 'INVALID'。
         实测 `00.00.12 : …`（点号）与 `00-00-12 : …`（短横）都正常渲染，
         而 `{00:00:12}` / `"00:00:12"` 这类修饰写法不被支持。
    因此这里：补 title 的冒号；把行内"时间戳"（纯数字+冒号的序列，如 00:00:12）
    的冒号换成点号，其余部分（分隔符与事件文本里的冒号）保持不动。
    """
    m = re.match(r'^(\s*title)(\s+)(?!:)(.+)$', line)
    if m:
        line = f"{m.group(1)}: {m.group(3).strip()}"
    # 只替换"数字:数字(:数字…)"这种时间戳形态，避免误伤 "12:30 讨论" 之类的事件文本
    return re.sub(r'(?<![\d:])(\d{1,3}(?::\d{2}){1,3})(?![\d:])',
                  lambda mm: mm.group(1).replace(':', '.'), line)


def _fix_timeline_block(block_lines):
    """对整个 timeline 代码块做修复（逐行调用 _fix_timeline_line）。"""
    return [_fix_timeline_line(ln) for ln in block_lines]


def _sanitize_mermaid(md_text):
    """修复 LLM 生成的 markdown 中 mermaid 图表的渲染错误（防 Obsidian/网页报错）。
    处理 ```mermaid 代码块：
      - graph/flowchart：给含括号等符号的节点/边标签补引号（_fix_flowchart_line）
      - timeline：给缺冒号的 `title` 行补冒号（_fix_timeline_line）
    非代码区与其它图型的代码块原样保留。"""
    out_lines = []
    in_code = False
    kind_wait = False
    diagram_kind = ""
    for raw in md_text.split("\n"):
        line = raw
        s = line.strip()
        if s.startswith("```"):
            if not in_code:
                rest = s[3:].strip()
                if rest.startswith("mermaid"):
                    in_code = True
                    kind_wait = True
                    diagram_kind = ""
            else:
                in_code = False
                diagram_kind = ""
                kind_wait = False
            out_lines.append(line)
            continue
        if in_code:
            if kind_wait:
                kind_wait = False
                diagram_kind = s
            if re.match(r"^(graph|flowchart)\b", diagram_kind):
                out_lines.append(_fix_flowchart_line(line))
            elif re.match(r"^timeline\b", diagram_kind):
                out_lines.append(_fix_timeline_line(line))
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _meeting_parts(folder):
    """读取会议各记录来源，返回 (summary_src, topics_src, transcript_src)。
    summary_src：summary.md（markdown 纪要）；topics_src：topics.md（元数据 JSON）；
    transcript_src：transcript.md。缺失返回空串。"""
    def _read(name):
        p = os.path.join(folder, name)
        try:
            # with 块：不然大会议反复读会留下一堆未关闭句柄（门禁输出里的 ResourceWarning）
            with open(p, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    return _read("summary.md"), _read("topics.md"), _read("transcript.md")


def _meeting_full_text(summary_src, topics_src, transcript_src):
    """按「会议摘要（元数据） → 会议纪要（markdown） → 议题分段 → 转写详情」
    拼装完整纪要全文（写入归档 _会议纪要.md）。"""
    parts = []
    _t, _intro, abstract, segs = _parse_topics_meta(topics_src)
    if abstract:
        parts.append("# 会议摘要\n\n" + abstract)
    if summary_src:
        parts.append("# 会议纪要\n\n" + summary_src)
    if segs:
        parts.append("# 议题分段\n\n" + _topics_to_md(segs))
    if transcript_src:
        parts.append("# 转写详情\n\n" + transcript_src)
    return "\n\n---\n\n".join(parts)


def _clean_md(s):
    """去掉行内 markdown 强调/链接语法，保留正文文字。"""
    s = re.sub(r"!?\[\[([^\]|]*?)(?:\|[^\]]*?)?\]\]", r"\1", s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return s


def _clip_text(s, n):
    """截断到 n 字以内，优先在中文标点处断句，过长加省略号。"""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    for p in "。；；！？，、":
        idx = cut.rfind(p)
        if idx > (n - 1) * 0.5:
            return cut[:idx + 1]
    return cut + "…"


def _meeting_short_summary(content, max_len=140):
    """从纪要 markdown 提取 1~3 句简短会议摘要（供工作日志条目兜底使用）。
    优先级：会议目标 → 议题小节 → 主标题导语 → 首个实质行。返回单行纯文本。"""
    if not content:
        return ""
    text = _clean_md(re.sub(r"```.*?```", "", content, flags=re.S))  # 去代码块并清行内 markdown
    # 1) 会议目标 / 会议要解决的问题（**会议目标**：… 或 会议目标：…，已去 **）
    m = re.search(r"(?:会议目标|会议要解决的问题)\s*[：:]\s*([^\n]+)", text)
    if m:
        s = _clean_md(m.group(1)).strip()
        if len(s) >= 6:
            return _clip_text(s, max_len)
    # 2) 议题小节（## 一、议题 之类）下的要点前几条
    m2 = re.search(r"#{1,6}\s*[^\n]*议题[^\n]*\n(.*?)(?=\n#{1,6}|\Z)", text, re.S)
    if m2:
        items = []
        for ln in m2.group(1).split("\n"):
            s = _clean_md(ln).strip()
            if not s or re.match(r"^```", s):
                continue
            if s.startswith("#"):
                break
            s = re.sub(r"^[-*\d、\.]+|^[-*]\s*", "", s).strip()
            if len(s) >= 4:
                items.append(s)
            if len(items) >= 3:
                break
        if items:
            return _clip_text("；".join(items), max_len)
    # 3) 主标题（# 行）之后的导语正文：跳过时间/参会方元数据，取实质内容行
    body = []
    started = False
    for ln in text.split("\n"):
        if re.match(r"^#\s", ln):
            started = True
            continue
        if not started:
            continue
        if ln.strip().startswith("#"):
            break
        s = _clean_md(ln).strip()
        if not s:
            continue
        if re.match(r"^(会议时间|会议时长|参会方|参会人|参会|时间|地点|主持)", s):
            continue
        body.append(s)
        if len(body) >= 2:
            break
    if body:
        return _clip_text("；".join(body), max_len)
    # 4) 兜底：首个实质行
    for ln in text.split("\n"):
        s = _clean_md(ln).strip()
        if s and not s.startswith("#"):
            return _clip_text(s, max_len)
    return ""


# ---------------------------------------------------------------- 结构化输出解析
# 2026-09-11：两次 DSH 调用分工。
# 调用1（纪要）→ summary.md：纯 markdown 纪要（可含 Mermaid 图表），前端直接渲染。
# 调用2（元数据）→ topics.md：单个 JSON 对象，供列表/工作日志/主题分段/归档使用：
#   {"标题":"…","简介":"…","摘要":"…","分段":[{"标题","开始","结束","摘要"}, …]}
# DSH 可能在 JSON 前后夹带分析文本，_try_load_json 负责从中提取合法 JSON 块。

_JSON_DECODER = json.JSONDecoder()


def _try_load_json(text):
    """从回复文本中提取 JSON。DSH 可能在 JSON 前后附带分析/注释文本，
    直接 json.loads 整段会失败；这里用 raw_decode 扫描、提取每个合法 JSON 块，
    优先返回最后一个 dict/list（DSH 常先给草稿再给 finalize 版）。失败返回 None。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    if not s:
        return None
    try:
        return json.loads(s)  # 纯 JSON 快速路径
    except Exception:
        pass
    results = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in "{[":
            try:
                val, end = _JSON_DECODER.raw_decode(s, i)
                results.append(val)
                i = end
                continue
            except (json.JSONDecodeError, ValueError):
                pass
        i += 1
    if not results:
        return None
    # 优先挑含目标字段的 dict；否则最后一个 dict/list
    for r in reversed(results):
        if isinstance(r, dict) and any(k in r for k in ("标题", "分段", "会议名称", "摘要")):
            return r
    for r in reversed(results):
        if isinstance(r, (dict, list)):
            return r
    return results[-1]


def _parse_topics_meta(text):
    """解析 topics.md 的元数据 JSON。返回 (标题, 简介, 摘要, 分段列表) 或 (None,None,None,None)。
    分段列表元素为 {标题,开始,结束,摘要}；无有效 JSON 时各字段为 None/[]。"""
    obj = _try_load_json(text)
    if not isinstance(obj, dict):
        return None, None, None, None
    title = (obj.get("标题") or "").strip()
    intro = (obj.get("简介") or "").strip()
    abstract = (obj.get("摘要") or "").strip()
    segs = []
    raw = obj.get("分段")
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            segs.append({
                "标题": (it.get("标题") or "").strip(),
                "开始": str(it.get("开始") or "").strip(),
                "结束": str(it.get("结束") or "").strip(),
                "摘要": (it.get("摘要") or "").strip(),
            })
    return title, intro, abstract, segs or None


def _parse_topics_fields(text):
    """从 topics.md 提取议题分段列表（元数据 JSON 的“分段”字段）。
    无有效分段返回 None。兼容旧格式：若顶层就是数组也直接接受。"""
    title, intro, abstract, segs = _parse_topics_meta(text)
    if segs:
        return segs
    obj = _try_load_json(text)
    if isinstance(obj, list):  # 旧格式：顶层即数组
        out = []
        for it in obj:
            if not isinstance(it, dict):
                continue
            out.append({
                "标题": (it.get("标题") or "").strip(),
                "开始": str(it.get("开始") or "").strip(),
                "结束": str(it.get("结束") or "").strip(),
                "摘要": (it.get("摘要") or "").strip(),
            })
        return out or None
    return None


def _topics_to_md(topics):
    """把议题分段数组转成 markdown（供归档“议题分段”节与离线导出）。"""
    if not topics:
        return ""
    lines = []
    for i, t in enumerate(topics, 1):
        rng = f" [{t['开始']} - {t['结束']}]" if (t['开始'] or t['结束']) else ""
        lines.append(f"## {i}. {t['标题']}{rng}\n{t['摘要']}".rstrip())
        lines.append("")
    return "\n".join(lines).strip()


def _meeting_abstract(content):
    """会议摘要（工作日志/展示用）：从纪要 markdown 启发式提取（<200字）。
    结构化「摘要」字段由 topics 元数据提供，工作日志侧优先用 _meeting_summary_for。"""
    return _meeting_short_summary(content, 200)


def _meeting_summary_for(folder, fallback_content=""):
    """工作日志摘要：优先取 topics.md 元数据里的结构化「摘要」，否则回退启发式提取。"""
    try:
        _t, _intro, abstract, _segs = _parse_topics_meta(
            open(os.path.join(folder, "topics.md"), encoding="utf-8").read())
        if abstract:
            return _clip_text(abstract, 200)
    except OSError:
        pass
    return _meeting_short_summary(fallback_content)


def _local_short_title(content, max_len=24):
    """本地兜底：把整场纪要压缩成一行的开会主题短名（DSH 未给标记时用）。"""
    s = _meeting_short_summary(content, max_len)
    if not s:
        return ""
    s = re.sub(r"[。；，、：\s]+$", "", s.strip())
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip("。；，、： ") + "…"


def _meeting_title(meeting):
    """会议展示/日志标题：优先自动生成的简短名称，否则回退时间戳文件夹名。"""
    if not meeting:
        return ""
    t = (meeting.get("title") or "").strip()
    return t or meeting["name"]


def _meeting_date(meeting):
    """会议发生日期（YYYY-MM-DD）：优先取 started_at，解析失败回退今天。
    工作日志/例会归档都应落在会议当天，而不是用户下达归档命令的日期。"""
    raw = (meeting or {}).get("started_at") or ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw.replace("T", " ").strip())
    if m:
        try:
            datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
            return m.group(1)
        except ValueError:
            pass
    return datetime.date.today().strftime("%Y-%m-%d")


def _meeting_hour(meeting):
    """会议开始时刻（0-23），用于判定日志写入「上午/下午」节；取不到用当前时刻。"""
    raw = (meeting or {}).get("started_at") or ""
    m = re.search(r"[T ](\d{1,2}):\d{2}", raw.strip())
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return datetime.datetime.now().hour



def _refresh_archived_note(meeting_id):
    """纪要/元数据重新生成后，刷新本地归档 md（meeting_note.md）。

    解决"先生成的是占位版已归档、稍后真正成稿后本地 md 还是旧的"问题。
    只重写本地这一份材料文件；笔记库里已归档的内容是否更新，由用户的归档技能
    在下次归档时幂等覆盖决定——ECHO 不直接改笔记库（2026-09-12 起）。
    """
    try:
        meeting = db.get_meeting(meeting_id)
        if not meeting:
            return
        folder = os.path.join(meetings_dir(), meeting["name"])
        _s, _seg, _tr = _meeting_parts(folder)
        full_text = _meeting_full_text(_s, _seg, _tr)
        # 只有源里确实有实质纪要才重写，避免用更空的内容覆盖更全的
        if _looks_placeholder(_s) or not (_s or _seg or _tr):
            return
        path = worklog.export_note(meeting, folder, full_text)
        if path:
            db.add_log("info", "meeting",
                       f"会议 {_meeting_title(meeting)} 本地归档材料已刷新")
    except Exception as e:
        db.add_log("warn", "meeting", f"刷新本地归档材料失败: {e}")


def push_meeting_to_worklog(meeting_id, archive_hint=""):
    """把会议纪要归档到用户的笔记库——**委派给用户自己的归档技能**。

    ECHO 只做三件事：备齐材料（落一份自包含 md）、定位笔记库、把任务送进 DSH。
    写到哪个目录、日志什么格式、有哪些专项与例会，全部由技能决定。
    返回值 (ok, msg)，msg 是技能回的一句话（原样转给面板）。

    archive_hint 即面板「归档要求」自由文本，空则由技能按自身默认规则判断。
    """
    ok, why = worklog.ready()
    if not ok:
        return False, why
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    folder = os.path.join(meetings_dir(), meeting["name"])
    content = open(os.path.join(folder, "summary.md"), encoding="utf-8").read() \
        if os.path.isfile(os.path.join(folder, "summary.md")) else ""
    _summary, _segments, _transcript = _meeting_parts(folder)
    full_text = _meeting_full_text(_summary, _segments, _transcript)
    if not (full_text or content):
        return False, "暂无纪要，请先生成纪要"
    try:
        note_path = worklog.export_note(meeting, folder, full_text, content)
    except OSError as e:
        return False, f"导出纪要文件失败: {e}"
    if not note_path:
        return False, "暂无纪要内容可归档"
    db.add_log("info", "meeting",
               f"会议 {_meeting_title(meeting)} 归档委派：{note_path}")
    return worklog.delegate_archive(
        meeting, note_path, archive_hint=archive_hint,
        date_str=_meeting_date(meeting), hour=_meeting_hour(meeting))


def save_summary(meeting_id, content):
    """人工改写的纪要正文写回 summary.md（面板纪要页签的「编辑」）。

    与 request_summary 同一条落盘路径：summary.md 是纯 markdown 纪要，
    前端 mdToHtml 直接渲染，归档/离线导出也读它。
    注意：整场「重新生成纪要」会覆盖这里的人工修改（前端保存时已提示）。

    返回 (ok, msg)。空内容被拒绝——归档与「写工作日志」都按「有纪要」判定，
    清空等于把这场会议的纪要弄丢，要删请用 DELETE /api/meetings/{id}。
    """
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    text = (content or "").replace("\r\n", "\n").strip()
    if not text:
        return False, "纪要内容为空，未保存"
    folder = os.path.join(meetings_dir(), meeting["name"])
    if not os.path.isdir(folder):
        return False, "会议目录不存在"
    try:
        with open(os.path.join(folder, "summary.md"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError as e:
        return False, f"写入失败: {e}"
    db.add_log("info", "meeting", f"会议 {_meeting_title(meeting)} 纪要已人工编辑保存（{len(text)} 字）")
    return True, "纪要已保存"


def update_meeting_title(meeting_id, title):
    """人工改会议名：同时写 meetings.title 与 topics.md 的「标题」。

    两处都要写——面板列表/顶栏读数据库，工作日志「会议纪要：<名称>」与归档
    读 topics.md 元数据；只改一处会出现"标题不一致"。

    返回 (ok, title, msg)；标题为空视为清空自定义命名，回落到 m.name。
    """
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "", "会议不存在"
    name = (title or "").strip()[:40]     # 与自动命名同一口径（见 _spawn_summary_waiter 的 title[:40]）
    try:
        db.update_meeting(meeting_id, title=name)
    except Exception as e:
        return False, "", f"保存会议名称失败: {e}"
    # topics.md 同步只影响归档/分段展示，失败不回滚数据库（面板已有新名字）
    folder = os.path.join(meetings_dir(), meeting["name"])
    path = os.path.join(folder, "topics.md")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                obj = _try_load_json(f.read())
            if isinstance(obj, dict):
                obj["标题"] = name
                with open(path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
        except Exception as e:
            db.add_log("warn", "meeting", f"topics.md 标题同步失败（数据库已更新）: {e}")
    db.add_log("info", "meeting", f"会议已改名：{name or meeting['name']}")
    return True, name, "会议名称已保存" if name else "已清空自定义名称"


#: 纪要的**格式要求**（两条路共用：agent 路径与直连 LLM 路径）。
#: 放在这里而不是各写一遍 —— Mermaid 的坑是实测踩出来的（PROGRESS §27 那批），
#: 复制一份就等于将来只修好一条路。
_SUMMARY_REQUIREMENTS = (
    "要求：生成完整纪要：**会议背景/议题 → 各议题关键讨论与结论 → 待办事项及责任人**。\n"
    "**尽量用 Mermaid 图表表达结构与流程**：如 flowchart 表达分工/流程、"
    "timeline 表达时间线/进度、sequenceDiagram 表达协作时序；Mermaid 代码用 "
    "```mermaid 代码块标注。\n"
    "**Mermaid 规范**：flowchart/graph 中节点文本与连线标签若含括号等符号，"
    "必须用双引号包裹文本（如 CY[\"初验(出验)证书\"]、|\"中验(待签)\"|），"
    "否则图表无法渲染；文本内不要出现未闭合引号。"
    "**引号标签里不要再出现半角双引号**（2026-09-20 实测：最新一条会议的纪要因此整张图报错）："
    "要引用别人的话，用全角引号或书名号——写成 “他人说话” 或 「他人说话」，"
    "不要写成 过滤\"他人说话\"等，后者会把标签提前闭合。\n"
    "**timeline 专项规范**（2026-09-12 实测：这两点写错整张图直接报错）："
    "1) 标题必须写成 `title: 文本`（冒号不可省）；"
    "2) **周期文本里不能含冒号**——timeline 用 `:` 分隔「周期 : 事件」，"
    "所以时间戳要写成点号形式 `00.00.12 : 发起试音`（或 `00-00-12`），"
    "不要写 `00:00:12 : 发起试音`。"
)

#: 直连 LLM 时内联的转写文本上限（字符）。超了要**明说被截断**，不能悄悄丢内容。
_PROVIDER_TRANSCRIPT_LIMIT = 120000


def _llm_provider_for_summary():
    """纪要要走直连 LLM 时用的 provider 实例；取不到返回 ``(None, 原因)``（P5）。"""
    from app import providers as providers_mod
    try:
        pid, inst = providers_mod.active("llm")
    except Exception as e:
        return None, "没有可用的 LLM provider（%s）" % e
    return inst, pid


def direct_llm_decision():
    """纪要是否走**直连 LLM provider**？返回 ``(bool, 原因)``。

    判据（2026-09-20 按"会议纪要只用 agent"的定调重写）：
      1. **agent（DSH）可用 → 一律走 agent**。纪要不是"孤立地调一次大模型"：同一场会议里
         分段与归档走的是同一个会话（日志原话"纪要/分段/归档共用此会话"），而归档还依赖
         agent 的 skill 机制 —— 只要 agent 在，就不该把它绕过去；
      2. agent 不可用而 LLM provider 就绪 → 直连兜底（P5 的承诺："不装 agent 也能出纪要"）；
      3. 其它 → 保持 agent 路径（由原路径报错，不静默走一条没配好的路）。

    **为什么删掉了原来"用户显式选了 `providerLlm` 就优先直连"那条判据**：
    `providerLlm` 的默认值是 `""`，而 `""` 与 `"echo-auto"` 的**生效 provider 完全一样**
    （`providers.default_id("llm")` 就是 `echo-auto`）。原判据看的是"原始设置非空"，于是
    "在面板里显式选了 ECHO AUTO"这个动作会**静默把纪要从 agent 切到直连** —— 实测踩到过
    （2026-09-19_19-18-25 那场：19:19 走 agent，次日 02:04 变成直连）。同一个 provider
    不该有两种路由，而且用户选路由时并没想到会顺手关掉纪要的 agent 路径。

    只读判断，不做任何副作用；探测失败一律按"不走直连"处理（宁可退回老路）。
    """
    try:
        from app import manager
        agent_ok = bool(manager.dsh_ready())
    except Exception:
        agent_ok = False
    if agent_ok:
        return False, "agent 可用（DSH 就绪）"
    inst, pid = _llm_provider_for_summary()
    if inst is None:
        return False, "agent 不可用且没有可用的 LLM provider"
    return True, "agent 不可用（DSH 未就绪），改用 LLM provider %s" % pid


def _provider_summary_text(folder):
    """把整场会议的转写**内联**成一段文本（直连 LLM 没有文件读取能力）。

    用 `_meeting_parts()` 汇总；超长时截断并**在文末显式说明**（不能让模型以为这就是全部）。
    """
    try:
        summary_src, topics_src, transcript_src = _meeting_parts(folder)
        text = _meeting_full_text(summary_src, topics_src, transcript_src)
    except Exception as e:
        raise RuntimeError("读取会议材料失败：%s" % e) from None
    text = (text or "").strip()
    if not text:
        raise RuntimeError("会议目录里没有可用的转写文本（transcript.md 为空或缺失）")
    if len(text) > _PROVIDER_TRANSCRIPT_LIMIT:
        original_len = len(text)
        text = text[:_PROVIDER_TRANSCRIPT_LIMIT] + (
            "\n\n【注意】以上内容因长度限制被截断（原文 %d 字），"
            "纪要需基于已给出的部分，并在开头注明「材料被截断」。" % original_len)
    return text


def _spawn_provider_summary(meeting_id, folder, out_name="summary.md", extra=""):
    """后台线程：把转写内联交给 LLM provider，写成纪要文件（P5）。

    与 agent 路径共用同一套**落盘纪律**：回复过短或疑似占位话就**不回写**
    （保留已有文件），失败写 warn 日志 —— 宁可留空让人重试，也不要把占位当纪要存下来。
    """
    def _run():
        with _MEETING_LOCKS_LOCK:
            lock = _MEETING_LOCKS.setdefault(meeting_id, threading.Lock())
        with lock:
            try:
                inst, pid = _llm_provider_for_summary()
                if inst is None:
                    db.add_log("error", "meeting", "直连纪要失败：%s" % pid)
                    return
                text = _provider_summary_text(folder)
                prompt = ("以下是会议转写（按片段组织，行内带[绝对时间戳]）：\n\n" + text +
                          "\n\n" + _SUMMARY_REQUIREMENTS + "\n"
                          "**输出约束：只输出纪要 markdown 全文**，不要寒暄、不要解释过程。")
                if extra:
                    prompt += "\n\n追加要求：%s" % extra
                db.add_log("info", "meeting",
                           f"已请求纪要（provider={pid}，{os.path.basename(folder)}）")
                reply = inst.chat([{"role": "system", "content": "你是会议纪要助手。"},
                                   {"role": "user", "content": prompt}], timeout=300)
                path = os.path.join(folder, out_name)
                had_old = os.path.isfile(path)
                old = ""
                if had_old:
                    try:
                        old = open(path, encoding="utf-8").read().strip()
                    except OSError:
                        old = ""
                if not (reply and len(reply.strip()) > 10):
                    db.add_log("warn", "meeting", "纪要 provider 回复过短，本次不回写")
                    return
                if _looks_placeholder(reply):
                    db.add_log("warn", "meeting",
                               "纪要 provider 回复疑似占位/意图话（非实质纪要），本次不回写"
                               f"{'，保留原文件' if had_old and len(old) > 30 else '（无旧有效内容）'}")
                    return
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(reply)
                db.add_log("info", "meeting",
                           f"纪要已生成（provider={pid}）：{os.path.basename(path)}")
            except Exception as e:
                # 直连路径的失败必须留痕：否则面板上表现为"点了没反应"（1.x 的老毛病）
                db.add_log("error", "meeting", f"直连纪要失败：{e}")
    threading.Thread(target=_run, daemon=True, name="summary-provider").start()


def request_summary(meeting_id, folder=None, extra=""):
    """生成整场会议纪要（纯 markdown，可含 Mermaid 图表），后台线程写 summary.md。

    两条路（P5 起，2026-09-20 起**以 agent 为准**）：
      * **agent 路径**（默认，只要 DSH 就绪就走它）：把 transcript.md 的**路径**交给 DSH，
        由它用 read 工具读并撰写；分段与归档共用这个会话；
      * **直连 LLM 路径**（兜底）：**agent 用不了**（DSH 未就绪）而 LLM provider 就绪时，
        把转写**内联**喂给 LLM provider —— 这样"不装 agent 也能出纪要"（P5 的验收点）。
    标题/简介/摘要/分段由第二次调用（request_topic_segments）以 JSON 提供（那次**只走 agent**）。

    ``extra`` = 追加要求（面板「重新生成」里填的那种）。**两条路都必须带上它** ——
    2026-09-19 修：以前 `regenerate_summary` 把它记进 summary_runs 却没往下传，
    等于"追加要求"从来没生效过。
    """
    if folder is None:
        meeting = db.get_meeting(meeting_id)
        folder = os.path.join(meetings_dir(), meeting["name"])
    meeting = db.get_meeting(meeting_id)
    use_direct, why = direct_llm_decision()
    if use_direct:
        db.add_log("info", "meeting", "纪要走直连 LLM：%s" % why)
        _spawn_provider_summary(meeting_id, folder, extra=extra)
        return True
    transcript = os.path.join(folder, "transcript.md").replace("\\", "/")
    text = (f"任务：基于会议转写文件生成会议纪要（markdown 格式）。\n"
            f"步骤：1) 用 read 工具读取文件 \"{transcript}\"（已按片段组织，"
            f"每片有 \"## 第 N 段 [起-止]\" 标题，行内带[绝对时间戳]）。\n"
            f"2) " + _SUMMARY_REQUIREMENTS + "\n"
            f"**输出约束：你只能在最终回复中输出纪要全文（markdown），"
            f"这是唯一的交付方式。严禁调用 write 或任何写文件工具。**")
    if extra:
        text += "\n\n追加要求：%s" % extra
    _spawn_summary_waiter(meeting_id, folder, "summary.md", text, "纪要")
    return True


def request_topic_segments(meeting_id, folder=None):
    """请 DSH 输出结构化会议元数据 JSON（第二次调用，替代原分段+语义分段两次调用）：
    标题、简介、摘要、议题分段，供会议列表/工作日志/前端主题分段/归档使用。

    DSH 在回复中只输出一个 JSON 对象，后台线程等待回复并写入 topics.md：
      {
        "标题": "…(<20字)",
        "简介": "…(一句话)",
        "摘要": "…(<200字)",
        "分段": [
          {"标题": "…", "开始": "mm:ss", "结束": "mm:ss", "摘要": "…"},
          ...
        ]
      }
    """
    if folder is None:
        meeting = db.get_meeting(meeting_id)
        folder = os.path.join(meetings_dir(), meeting["name"])
    meeting = db.get_meeting(meeting_id)
    transcript = os.path.join(folder, "transcript.md").replace("\\", "/")
    text = (f"任务：通读会议转写全文，输出本次会议的结构化元数据 JSON"
            f"（供会议列表/工作日志/主题分段展示使用）。\n"
            f"1) 用 read 工具读取转写文件 \"{transcript}\"（每行带[绝对时间戳]，"
            f"已按音频片段分节，但不代表议题边界）。\n"
            f"2) 输出一个 JSON 对象，含 4 个字段：\n"
            f"   - 标题：一句话（不超过 20 个汉字）概括本次会议主题，"
            f"不带标点结尾、不加引号（如：多模态能力共享中心方案评审）。\n"
            f"   - 简介：一句话（40 字内）介绍会议性质与目的。\n"
            f"   - 摘要：整个会议的内容摘要，不超过 200 个汉字。\n"
            f"   - 分段：按讨论主题/阶段划分 **3~10 个议题段**，每段为一个对象，含："
            f"标题、起止绝对时间（取该段最早和最晚的时间戳，格式 mm:ss 或 h:mm:ss）、"
            f"以及 2~3 句摘要（该议题讨论的核心内容与结论）。\n"
            f"**输出约束：你只能在最终回复中输出一个**合法的 JSON 对象**，形如：\n"
            f"{{\"标题\":\"会议主题\",\"简介\":\"…\",\"摘要\":\"…\","
            f"\"分段\":[{{\"标题\":\"议题一\",\"开始\":\"00:00\",\"结束\":\"05:12\","
            f"\"摘要\":\"…\"}},{{\"标题\":\"议题二\",\"开始\":\"05:12\",\"结束\":\"09:40\","
            f"\"摘要\":\"…\"}}]}}\n"
            f"禁止在 JSON 外输出任何说明文字、禁止调用 write 或任何写文件工具。**")
    _spawn_summary_waiter(meeting_id, folder, "topics.md", text, "议题分段")
    return True


def _looks_placeholder(text):
    """判断 DSH 回复是否为「占位/意图」而非真实纪要正文。

    若 DSH 只回了一句诸如“I'll read the transcript file first.”、
    “正在读取转写文件…”这类意图/进度话，却没有任何实质章节/要点，
    把它当成纪要写盘会造成「真纪要被占位符顶掉、之后展示不出来」。
    """
    t = (text or "").strip()
    if len(t) >= 80:
        return False  # 长度够，视为有实质内容，不再误判
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    plain = [ln for ln in lines if not ln.lstrip().startswith(("#", "```"))]
    has_body = sum(1 for ln in plain if len(_clean_md(ln)) >= 6)
    if has_body >= 2:
        return False  # 有两行以上实质话，视为真内容
    # 仅剩少量文本 → 命中“意图/进度”句则判为占位
    pats = re.compile(
        r"^(I(?:'|\u2019)?ll|i will|let(\u2019s|\u2018s|\u2019)?\s+me|正在|我需要|请稍等|"
        r"先|让我|马上|先读|读一下|待我|稍等|ok|好的).{0,40}$",
        re.I)
    return bool(pats.search(t))


def _spawn_summary_waiter(meeting_id, folder, out_name, prompt_text, label):
    """发 prompt 到纪要会话（工作区每次会议新会话 / 或固定会话），后台线程等回复并写入文件。

    同一会议的多个纪要请求（整场/分段/语义分段）串行执行，防止并发同会话时
    wait_for_reply 抢到同一个回复导致输出串台。

    写入前做「占位符/超短」校验：若 DSH 只回了意图话而没给实质纪要，则**不回写**
    （保留已有的正确文件；若还没有旧文件或旧文件同样为空壳，则留待人工重试），
    避免把占位符当真纪要持久化并用于后续展示/归档。
    """
    def _run():
        with _MEETING_LOCKS_LOCK:
            lock = _MEETING_LOCKS.setdefault(meeting_id, threading.Lock())
        with lock:
            try:
                client = get_client()
                sid = _summary_session(client, meeting_id)
                if not sid:
                    db.add_log("error", "meeting", "无纪要会话，跳过自动纪要")
                    return
                client.clear_stuck(sid)
                client.prompt(sid, prompt_text, mode="queue")
                db.add_log("info", "meeting", f"已请求{label} ({os.path.basename(folder)})")
                reply, _done = client.wait_for_reply(sid, timeout=240, poll=2)
                path = os.path.join(folder, out_name)
                had_old = os.path.isfile(path)
                old = ""
                if had_old:
                    try:
                        old = open(path, encoding="utf-8").read().strip()
                    except OSError:
                        old = ""
                if not (reply and len(reply.strip()) > 10):
                    db.add_log("warn", "meeting", f"{label} 超时未收到回复")
                    return
                if _looks_placeholder(reply):
                    db.add_log("warn", "meeting",
                               f"{label} 回复疑似占位/意图话（非实质纪要），本次不回写"
                               f"{'，保留原文件' if had_old and len(old) > 30 else '（无旧有效内容）'}")
                    return
                content_raw = reply.strip()
                # 写盘内容：调用1（summary.md）为 markdown 纪要；调用2（topics.md）为元数据 JSON
                if out_name == "topics.md":
                    obj = _try_load_json(content_raw)
                    if obj is not None:
                        content = json.dumps(obj, ensure_ascii=False, indent=2)
                    else:
                        content = content_raw + "\n"  # 未能提取 JSON 时按原文回写（可人工修正）
                else:
                    content = _sanitize_mermaid(content_raw + "\n")  # markdown 纪要
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                db.add_log("info", "meeting", f"{label}已写入 {out_name}（{len(content)} 字，"
                           f"{'JSON' if out_name == 'topics.md' and obj is not None else '文本'}）")
                # 元数据成稿后：用结构化「标题」字段写入 meetings.title，
                # 会议列表与工作日志「会议纪要：<名称>」用它，更清晰（2026-09-10 起）。
                if out_name == "topics.md":
                    _t, _intro, _abs, _segs = _parse_topics_meta(content)
                    title = (_t or _local_short_title(
                        open(os.path.join(folder, "summary.md"), encoding="utf-8").read()
                        if os.path.isfile(os.path.join(folder, "summary.md")) else "")).strip()
                    if title:
                        try:
                            db.update_meeting(meeting_id, title=title[:40])
                            db.add_log("info", "meeting", f"会议已自动命名：{title}")
                        except Exception as e:
                            db.add_log("warn", "meeting", f"保存会议名称失败: {e}")
                    # 纪要/元数据重新生成后，刷新本地归档材料（meeting_note.md），
                    # 让后续归档拿到的是成稿版而不是先前的占位版。
                    _refresh_archived_note(meeting_id)
            except Exception as e:
                db.add_log("error", "meeting", f"{label} 生成失败: {e}")
    threading.Thread(target=_run, daemon=True).start()


def regenerate_summary(meeting_id, extra_prompt=""):
    """重新生成纪要 + 分段摘要（可追加要求）。"""
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    folder = os.path.join(meetings_dir(), meeting["name"])
    if not os.path.isfile(os.path.join(folder, "transcript.md")):
        return False, "转写文件不存在，无法生成纪要"
    run_id = db.add_summary_run(meeting_id, extra_prompt)
    if extra_prompt:
        # 追加要求时只重生成整场纪要（带要求），议题分段保持。
        # 2026-09-19 修：以前没把 extra_prompt 传下去 —— 那个"追加要求"框填了也没用。
        ok = request_summary(meeting_id, folder, extra=extra_prompt)
    else:
        ok1 = request_summary(meeting_id, folder)
        ok2 = request_topic_segments(meeting_id, folder)
        ok = ok1 and ok2
    db.finish_summary_run(run_id, "done" if ok else "failed")
    return ok, ("已发送纪要+议题分段生成请求" if ok
                else "发送请求失败（agent 与 LLM provider 都不可用？）")


def retranscribe_meeting(meeting_id):
    """手动重新转写一场会议（后台，并发保护）。"""
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    name = meeting["name"]
    folder = os.path.join(meetings_dir(), name)
    segs = [f for f in os.listdir(folder) if re.match(r"^\d+\.wav$", f)] if os.path.isdir(folder) else []
    if not segs:
        return False, "该会议没有音频片段，无法转写"
    with _retranscribing["lock"]:
        if name in _retranscribing["set"]:
            return False, "该会议已在重新转写中，请稍候"
        if _state["active"] and os.path.basename(_state["folder"] or "") == name:
            return False, "该会议正在录音中，结束后再重新转写"
        _retranscribing["set"].add(name)

    def _run():
        try:
            _transcribe_meeting(folder)
        finally:
            with _retranscribing["lock"]:
                _retranscribing["set"].discard(name)

    threading.Thread(target=_run, daemon=True).start()
    return True, f"已开始重新转写（{name}）"


# ---------------------------------------------------------------- 查询

def get_meeting_detail(meeting_id):
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return None
    folder = os.path.join(meetings_dir(), meeting["name"])
    return {
        **meeting,
        "folder": folder,
        "speakers": db.get_speakers(meeting_id),
        "lines": db.get_lines(meeting_id),
        "summaries": db.get_summary_runs(meeting_id),
        "hasTranscript": os.path.isfile(os.path.join(folder, "transcript.md")),
        "hasSummary": os.path.isfile(os.path.join(folder, "summary.md")),
    }


def delete_meeting(meeting_id):
    """删除会议：DB 记录 + （可选）音频文件。"""
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    name = meeting["name"]
    if _state["active"] and os.path.basename(_state["folder"] or "") == name:
        return False, "该会议正在录音中，不能删除"
    keep_audio = settings.get("meetingKeepRawAudio", True)
    if not keep_audio:
        import shutil
        folder = os.path.join(meetings_dir(), name)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
    # 注意顺序：_drop_summary_session 需要用会议名去查映射表，若先删了会议记录
    # 就拿不到 name（_session_key 会退化成 id 字符串），映射和会话都会清不掉。
    _drop_summary_session(name)
    db.delete_meeting(meeting_id)
    db.add_log("info", "meeting", f"已删除会议 {name}")
    return True, "已删除"


def clean_short_meetings(max_seconds=120):
    """清理时长 ≤ max_seconds 的会议（DB + 音频/转写文件，彻底删除）。

    返回被删除的会议列表 [{id, name, duration}]；正在录音的会议跳过。
    """
    import shutil
    removed = []
    for m in db.list_meetings(limit=1000):
        dur = m.get("duration_seconds") or 0
        if dur > max_seconds:
            continue
        name = m["name"]
        if _state["active"] and os.path.basename(_state["folder"] or "") == name:
            continue   # 正在录音，跳过
        folder = os.path.join(meetings_dir(), name)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        # 同 delete_meeting：先清会话映射（需要会议名），再删会议记录
        _drop_summary_session(name)
        db.delete_meeting(m["id"])
        removed.append({"id": m["id"], "name": name, "duration": dur})
    if removed:
        db.add_log("info", "meeting", f"已清理 {len(removed)} 个短会议（≤{max_seconds}s）")
    return removed


def import_transcript_fallback():
    """（预留）旧会议迁移：历史 transcript.md 的首次导入。旧数据不随本仓库提供，暂不实现。"""
    pass
