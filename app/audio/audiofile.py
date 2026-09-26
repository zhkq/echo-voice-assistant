# -*- coding: utf-8 -*-
"""audiofile.py — 会议音频段的**归档格式**与**用时解码**（历史音频无损压成 FLAC）。

## 为什么要有这个模块

历史会议把音频存成 `<会议目录>\\01.wav`、`02.wav`…（16 kHz / 单声道 / 16-bit PCM）。
实测（2026-09-26，本机 venv：`soundfile 0.14.0 + libsndfile 1.2.2`）：

    WAV → FLAC(PCM_16) → WAV：**逐样本相等**，体积 1.92 MB → 937 KB（49%）
    WAV → FLAC(PCM_16) → 同一引擎转写：**文本逐字不变**

所以归档改存 FLAC 是**无损**的，省下约一半磁盘。但 **sherpa 只认 RIFF/WAV**
（直接喂 FLAC/Opus 会报 `file does not start with RIFF id`），whisper / SenseVoice /
pyannote 也不能假定认 FLAC。因此定下两条铁律：

  1. **盘上存 FLAC**（`01.flac`，与 `01.wav` 同名不同后缀，现有读取逻辑按段号发现它）；
  2. **用时才解码**（读到 `.flac` → 解成临时 WAV → 交给既有代码 → 用完即删）。

为什么不做 Opus：实测同一段真会议、同一引擎，FLAC 转写**逐字不变**而 Opus 文本
差异 29% —— 省 89% 但转写内容变了，这个交换用户明确否掉了。

## 本模块提供什么

* 段发现与解析：`list_segments()` / `segment_files()` / `resolve_segment()` /
  `audio_path()` —— 会议链路（转写、分离、面板播放）**只用这一份**"段从哪来"的规则；
* 用时解码：`decode_to_wav()` / `decoded()`（上下文管理器，退出即删临时文件）；
* 无损压缩：`compress_segment()` / `compress_meeting()` / `scan_meetings()`；
* 判据与文案：`CompressionError`（人话原因）、`plan_for_meeting()`。

## 与 `importer.py` 的分工

`importer.py` 管"用户手里的录音 → 会议认得的 16k wav"（**导入**那条路）。
本模块管"会议认得的 wav ↔ 归档 flac"（**存量**那条路）。两者都靠 soundfile，
但目标相反：导入是把任意格式**归一**，本模块是把归一后的东西**无损收起**。

## 不写进这里的东西

`app/audio/stt.py` 是另一个任务在改的文件，**本模块一行都不碰它**：解码助手放在这里，
调用方（`app/meeting.py`）自己把临时 WAV 路径交给引擎。引擎无关性是刻意设计的
—— 不要指望各引擎自己支持 FLAC。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import wave
from contextlib import contextmanager

# ---------------------------------------------------------------- 常量与闸门

#: 归档格式的扩展名（小写，带点）。
FLAC_EXT = ".flac"
#: 会议链路的原生扩展名。
WAV_EXT = ".wav"
#: 段文件名：纯数字 + 扩展名（`01.wav` / `12.flac`）。
SEGMENT_RE = re.compile(r"^(\d+)\.(wav|flac)$", re.IGNORECASE)
#: 只看 wav 的（压缩的输入、`meta["segments"]` 的兼容形状）。
WAV_RE = re.compile(r"^\d+\.wav$", re.IGNORECASE)

#: FLAC 的编码档位（`soundfile` 的 subtype）。会议音频是 16-bit PCM，所以 PCM_16
#: 就是**无损**；更高位深的源（24/32-bit）才需要往上升一档。
FLAC_SUBTYPE = "PCM_16"
#: 位深高于 16 的源必须用这一档，否则会**丢位深**（那就不是无损压缩了）。
FLAC_SUBTYPE_HIRES = "PCM_24"
#: 15 位以下才用 8-bit 档（会议录音不会出现，留作出错时的兜底判断）。
FLAC_SUBTYPE_LOW = "PCM_S8"

#: 压缩后真正落地的新扩展名（前端/日志里要显示它）。
ARCHIVE_EXT = FLAC_EXT

#: 临时解码目录（惰性创建）。**可被测试替换**（`patch.object(audiofile, "TEMP_DECODE_DIR", …)`）。
#: 放在系统临时目录下、带固定前缀，只为本进程服务；`gc_temp()` 会清掉陈旧的残留。
TEMP_DECODE_DIR = os.path.join(tempfile.gettempdir(), "echo-audio-decode")
#: 临时解码文件的保质期（秒）。超过它、又不是本进程正在用的，`gc_temp()` 可以删。
TEMP_TTL_SECONDS = 3600.0
#: 临时文件名前缀（`gc_temp` 只删这个前缀的，绝不碰系统临时目录里别人的文件）。
TEMP_PREFIX = "echo-dec-"

#: 压缩时读回校验的抽样块大小（帧）。整段 `float64` 比会撑爆内存，
#: 所以：**逐样本比**在分块里做（不整段进内存），块大小与 importer 的分块一致。
VERIFY_BLOCK_FRAMES = 65536

#: 段号（int）。`01.wav` / `01.flac` / `1.WAV` 都归到同一个段号。
_RESERVED_NAMES = ("meta.json", "transcript.md", "summary.md", "topics.md")


class CompressionError(Exception):
    """音频压缩/解码失败 —— `str(e)` 是**给人看的一句话**（面板与日志原样显示）。

    与 `importer.ImportAudioError` 同一条纪律：失败原因必须是**要素齐全的人话**
    （哪个文件 / 为什么 / 原件还在不在），绝不许调用方自己编一句"压缩失败"。
    """


def human_size(n):
    """字节 → 人话（`149.0 MB`）。面板与日志共用，避免两处各写一套舍入。"""
    b = float(n or 0)
    if b < 1024:
        return "%d B" % int(b)
    for unit in ("KB", "MB", "GB", "TB"):
        b /= 1024.0
        if b < 1024 or unit == "TB":
            return "%.1f %s" % (b, unit)
    return "%.1f TB" % b


def saving_percent(before, after):
    """省了多少（百分比，整数）。`before<=0` 时返回 0（除零不炸）。"""
    if not before or before <= 0 or after >= before:
        return 0
    return int(round((before - after) * 100.0 / before))


# ---------------------------------------------------------------- 段发现
def segment_files(folder):
    """目录里的音频段文件名（`01.wav` / `02.flac`…），按段号排序。

    同一段号同时存在两种后缀时**两种都返回**（`01.flac` 在前）—— 调用方要能看出
    "这里有重复"。正常流程不会出现（压缩成功即删原件），但"压缩中途崩了"会。
    """
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if SEGMENT_RE.match(name):
            out.append(name)
    out.sort(key=lambda n: (int(n.split(".")[0]), 0 if n.lower().endswith(FLAC_EXT) else 1))
    return out


def list_segments(folder):
    """目录里的段号（**去重、升序**）。会议链路按它决定"这一场有几段"。"""
    seen = []
    for name in segment_files(folder):
        idx = int(name.split(".")[0])
        if idx not in seen:
            seen.append(idx)
    return seen


def resolve_segment(folder, seg_index):
    """段号 → 盘上实际存在的**优先**音频文件路径（都没有 = 空串）。

    **优先 `.wav`**：压缩是"先写 `.flac`、校验通过、再删 `.wav`"，所以两个都在
    意味着**压缩还没收尾**（或收尾失败）—— 这时候必须以原件为准，用 `.flac`
    会读到一份尚未被认可的产物。
    """
    stem = "%02d" % int(seg_index)
    for ext in (WAV_EXT, FLAC_EXT):
        path = os.path.join(folder, stem + ext)
        if os.path.isfile(path):
            return path
    # 段号可能超过两位（`100.wav`），再按目录实际名字找一遍
    for name in segment_files(folder):
        if int(name.split(".")[0]) == int(seg_index):
            return os.path.join(folder, name)
    return ""


def is_flac(path):
    return str(path or "").lower().endswith(FLAC_EXT)


def _is_wav(path):
    return str(path or "").lower().endswith(WAV_EXT)


# ---------------------------------------------------------------- 时长（引擎无关）
def wav_seconds(path):
    """WAV 时长（秒）；读不了返回 0.0（与 `meeting._wav_seconds` 同一口径）。

    刻意用标准库 `wave`：它读的就是 RIFF 头里的 `fmt `/`data` 块，不依赖第三方库，
    而且"产物到底是不是 16 kHz 单声道 16-bit"该由最不挑剔的读者回答。
    """
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate() or 0
            return (w.getnframes() / float(rate)) if rate else 0.0
    except Exception:
        return 0.0


def audio_seconds(path):
    """任意受支持音频的时长（秒）；读不了返回 0.0。

    WAV 走标准库（快、无依赖），其它容器走 soundfile —— 转写/播放那条路要按它算
    时间轴均摊，**不能因为换了归档格式就退化成 0**。
    """
    if _is_wav(path):
        return wav_seconds(path)
    try:
        import soundfile as sf
        info = sf.info(path)
        rate = int(info.samplerate or 0)
        return (int(info.frames or 0) / float(rate)) if rate else 0.0
    except Exception:
        return 0.0


def probe(path):
    """`(rate, channels, subtype, frames, format)`；读不了抛 `CompressionError`（人话）。

    为什么把 5 个值一起返回：压缩前后要**逐项**比（采样率/声道/位深/帧数），
    任何一项不同都说明不是无损，绝不能只看"文件打得开"。
    """
    try:
        import soundfile as sf
        info = sf.info(path)
    except Exception as exc:
        raise CompressionError(
            "音频「%s」读不出来（%s）。文件可能损坏、被截断，或不是音频。"
            % (os.path.basename(path), _short(exc)))
    return (int(info.samplerate or 0), int(info.channels or 0), str(info.subtype or ""),
            int(info.frames or 0), str(info.format or "").upper())


def _short(exc):
    text = str(exc or "").strip().replace("\n", " ")
    return (text or type(exc).__name__)[:300]


# ---------------------------------------------------------------- 容器完整性
#: RIFF/WAVE 里两个关键 chunk。`data` 的**声明长度**与文件实际长度对不上，
#: 就是"被截断"的判据 —— soundfile 在这种情况下**照样能读**（它读够能读的部分，
#: 甚至把声明长度报成帧数），于是"压出来一个时长看着对、内容却短了的 flac"
#: 会一路通过校验。实测（2026-09-26）：把 1 秒的 wav 砍掉一半，
#: `sf.info()` 仍报 16000 帧，而 `wave.getnframes()` 同样报 16000
#: （`wave` 信的就是头里的声明）—— 两者都发现不了，必须自己数字节。
_RIFF_CHUNKS_OF_INTEREST = (b"fmt ", b"data")


def riff_data_check(path):
    """RIFF/WAVE 里 `data` 块的**声明长度 vs 文件实际长度**。

    返回 `(declared, actual)`；不是 RIFF/WAVE、或结构读不通时返回 `(0, 0)`（= 不判）。
    `declared > actual` 就是截断 —— 调用方据此**拒绝压缩**（保留原件 + 报错）。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return 0, 0
            pos = 12
            declared = 0
            while pos + 8 <= size:
                fh.seek(pos)
                hdr = fh.read(8)
                if len(hdr) < 8:
                    break
                cid, clen = hdr[:4], int.from_bytes(hdr[4:8], "little")
                if cid == b"data":
                    declared = clen
                    break
                pos += 8 + clen + (clen & 1)      # chunk 是偶数字节对齐
            return declared, max(size - (pos + 8), 0)
    except Exception:
        return 0, 0


def container_problem(path):
    """容器层面的**硬伤** → 一句人话；没问题返回空串。

    目前只处理一件事，但它正是"看起来能压、其实已经坏了"的那一类：
    `data` 声明长度 > 文件里实际有的字节数 = **文件被截断**。
    这里必须挡住的理由：截断的段压出来会是一个"能打开、帧数还写着原来那么多、
    实际声音缺了一半"的 flac —— 而那比直接报错危险得多（用户以为压好了，
    日后重转才发现内容少了）。所以宁可不压。
    """
    declared, actual = riff_data_check(path)
    if declared and declared > actual:
        return ("文件被截断（WAV 头声明 %d 字节音频数据，文件里只有 %d 字节）"
                "—— 不压缩，原件保留" % (declared, actual))
    return ""


# ---------------------------------------------------------------- 用时解码
def _temp_dir():
    os.makedirs(TEMP_DECODE_DIR, exist_ok=True)
    return TEMP_DECODE_DIR


def gc_temp(max_age=TEMP_TTL_SECONDS):
    """清掉陈旧的临时解码文件（超过 `max_age` 秒）。

    为什么需要它：解码产物是**给引擎读完就没用**的中间物，但进程被硬杀时来不及删。
    每次真正解码前顺手扫一遍（目录很小），比养一个后台清理线程划算得多。
    **只删本模块自己那个前缀/目录**，绝不碰系统临时目录里别人的文件。
    """
    removed = 0
    now = time.time()
    try:
        for name in os.listdir(TEMP_DECODE_DIR):
            if not name.startswith(TEMP_PREFIX):
                continue
            path = os.path.join(TEMP_DECODE_DIR, name)
            try:
                if (now - os.path.getmtime(path)) > max_age:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    except OSError:
        return 0
    return removed


def decode_to_wav(src, *, dst=None, cleanup=True):
    """把任意受支持音频（重点是 FLAC）解成 **16 kHz/单声道/16-bit PCM 的临时 WAV**。

    参数：
      src      源文件（`.flac` / `.wav` / 其它 libsndfile 认得的容器）
      dst      目标路径；留空则在临时目录里造一个唯一名
      cleanup  为真时：先顺手 `gc_temp()`，并登记进程退出时删除

    返回目标路径。**失败抛 `CompressionError`（人话）**，且不留下半截文件。

    ⚠️ 为什么一律重编码成 16-bit PCM 而不是"直接复制 flac 的字节"：引擎吃的是
    RIFF/WAV，而中间这段唯一的用途就是喂引擎；重编码一次换来的是**一条路**
    （whisper / SenseVoice / sherpa / pyannote / 面板播放 都读同一种文件），
    而不是"每个引擎各自试一遍能不能读 FLAC"。
    """
    import soundfile as sf

    if not os.path.isfile(src):
        raise CompressionError("音频文件不存在：%s" % src)
    if cleanup:
        gc_temp()
    if not dst:
        tag = "%s%d-%s" % (TEMP_PREFIX, os.getpid(), os.urandom(4).hex())
        dst = os.path.join(_temp_dir(), tag + WAV_EXT)
    part = dst + ".part"
    _unlink(part)
    try:
        total = 0
        with sf.SoundFile(src, "r") as fh:
            rate = int(fh.samplerate or 0)
            channels = int(fh.channels or 1)
            if rate <= 0:
                raise CompressionError("音频「%s」的采样率读不出来（%r）。"
                                       % (os.path.basename(src), rate))
            with sf.SoundFile(part, "w", samplerate=rate, channels=channels,
                              subtype=FLAC_SUBTYPE, format="WAV") as out:
                for block in fh.blocks(blocksize=VERIFY_BLOCK_FRAMES, dtype="int16",
                                       always_2d=True):
                    if block.shape[1] != channels:
                        # 形状与头不符：宁可当场炸，也不要写出一份声道错位的 wav
                        raise CompressionError(
                            "音频「%s」解码时声道数变成 %d（头里写的是 %d），文件可能损坏。"
                            % (os.path.basename(src), block.shape[1], channels))
                    out.write(block)
                    total += len(block)
        if total <= 0:
            raise CompressionError("音频「%s」解码出 0 帧（空文件或只有文件头）。"
                                   % os.path.basename(src))
        os.replace(part, dst)
    except CompressionError:
        _unlink(part)
        _unlink(dst)
        raise
    except Exception as exc:
        _unlink(part)
        _unlink(dst)
        raise CompressionError("音频「%s」解码失败：%s" % (os.path.basename(src), _short(exc)))
    if cleanup:
        _register_temp(dst)
    return dst


@contextmanager
def decoded(src, **kw):
    """`with decoded(path) as wav_path:` —— 是 wav 就原样给（不复制），
    是 flac 就解成临时 wav，**退出时删掉临时文件**。

    这是"用时解码"的落点：调用方拿到的一定是一个 **WAV 路径**，
    既有的引擎代码一行都不用改（引擎无关性）。
    """
    if _is_wav(src) or not is_flac(src):
        yield src
        return
    path = decode_to_wav(src, **kw)
    try:
        yield path
    finally:
        _unlink(path)
        _unregister_temp(path)


@contextmanager
def decoded_segments(items):
    """一批 `(seg_index, path)` → `[(seg_index, wav_path)]`，退出时统一删临时文件。

    一次转写要处理 1..N 段；逐段用 `decoded()` 也行，但那样每段都会 `gc_temp()` 扫
    一遍目录（N 段就扫 N 次）。这里只扫一次、只收集一次删除清单。
    """
    items = list(items)
    temps = []
    try:
        out = []
        for idx, path in items:
            if is_flac(path):
                wav_path = decode_to_wav(path)
                temps.append(wav_path)
                out.append((idx, wav_path))
            else:
                out.append((idx, path))
        yield out
    finally:
        for path in temps:
            _unlink(path)
            _unregister_temp(path)


#: 进程退出时要删的临时文件（硬杀进程会漏，`gc_temp()` 下次兜底）。
_TEMP_REGISTRY = set()


def _register_temp(path):
    _TEMP_REGISTRY.add(path)


def _unregister_temp(path):
    _TEMP_REGISTRY.discard(path)


def cleanup_registered():
    """删掉本进程登记过的临时解码文件（测试与 shutdown 用）。

    ⚠️ 谁在调它：`app/main.py` 的 lifespan 关闭段。此前它**只被定义、没有任何调用方**
    （2026-09-26 复查发现）—— 而 `api.meeting_audio` 的注释里写着"退出时的清理兜底"，
    那句当时是假的。播放那一路现在改成**响应发完立刻删**（`drop_temp()` +
    `BackgroundTask`），这个函数退化成"进程被杀/异常退出时的第二道闸"。
    """
    for path in list(_TEMP_REGISTRY):
        _unlink(path)
        _unregister_temp(path)


def drop_temp(path):
    """删掉一个临时解码文件**并撤掉登记**；删干净返回 True。

    给"流式回完之后才敢删"那一类调用方用（`api.meeting_audio` 的 `FileResponse`
    background task）：`decoded()` 是 `with` 退出即删，但 HTTP 那条路不能——文件
    正在被流式读，return 之前删掉会让播放中途断掉。

    与 `decoded()` 的 finally 同一条纪律：**只删调用方自己的那个路径**，
    删不掉也**不抛**（播放已经成功了，删不掉只该留一条日志）。
    """
    _unlink(path)
    _unregister_temp(path)
    return not os.path.exists(path)


def _unlink(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- 无损压缩
def _flac_subtype_for(subtype):
    """源 subtype → FLAC 编码档位（**必须是真正无损的那一档**）。

    * `PCM_16` / `PCM_U8` / `PCM_S8` → `PCM_16`（8-bit 装进 16-bit 仍是无损）
    * `PCM_24` → `PCM_24`；`PCM_32` / `FLOAT` / `DOUBLE` → `PCM_24`
      （FLAC 没有 32-bit 整数与浮点；遇到这种源**不做无损压缩**，见 `can_compress`）
    """
    s = str(subtype or "").upper()
    if s in ("PCM_16", "PCM_S8", "PCM_U8", "PCM_S16", "PCM_U16"):
        return FLAC_SUBTYPE
    if s in ("PCM_24", "PCM_S24", "PCM_U24"):
        return FLAC_SUBTYPE_HIRES
    return ""


def can_compress(path):
    """这个 wav 能不能**无损**压成 FLAC。返回 `(ok, reason)`（reason 空 = 能）。

    为什么先问一遍而不是直接压：有些源（32-bit / 浮点 WAV、ADPCM、μ-law）
    libsndfile 要么压不了、要么压出来**不是无损**。那种文件留着不动才是对的
    —— "省了空间但内容变了"是这个功能唯一不能接受的结果。
    """
    if not os.path.isfile(path):
        return False, "文件不存在"
    if not _is_wav(path):
        return False, "不是 wav（已经是归档格式或不是音频段）"
    bad = container_problem(path)
    if bad:
        return False, bad
    try:
        rate, channels, subtype, frames, fmt = probe(path)
    except CompressionError as exc:
        return False, str(exc)
    if fmt and fmt not in ("WAV", "WAVEX", "RF64"):
        return False, "容器是 %s（不是 WAV）" % fmt
    if not _flac_subtype_for(subtype):
        return False, "编码是 %s，FLAC 没有对应的无损档位" % (subtype or "?")
    if rate <= 0 or channels <= 0 or frames <= 0:
        return False, "参数不完整（%d Hz / %d 声道 / %d 帧）" % (rate, channels, frames)
    return True, ""


def compare_lossless(wav_path, flac_path, block_frames=VERIFY_BLOCK_FRAMES):
    """逐项比 `wav` 与 `flac` 解码后的内容。返回 `(ok, reason)`。

    比四件事（**缺一不可**）：
      1. 采样率 / 声道 / 帧数（头里那几个数）—— 少了任何一项时长就变了；
      2. 位深档位（源 24-bit 压成 16-bit 会**静默丢精度**）；
      3. 帧数按 `VERIFY_BLOCK_FRAMES` 分块、**逐样本 `int64` 精确比**（和不为零即失败）。

    为什么用整数比较而不是 `float32`：`float32` 只有 24 bit 尾数，16/24-bit PCM
    在整段比较里可能"看起来相等"而掩盖真正的差异；整数比较没有这个问题
    （soundfile 的 `dtype` 只认 `float32/float64/int16/int32`，24-bit PCM 用 `int32`
    读是**精确**的 —— 它是容器位宽，不是"降成 32 位再比"）。

    为什么分块：几十分钟的 16 kHz 音频整段进内存是 ~200 MB 级别，会议链路里
    已经因为"整段进内存"踩过坑（见 `importer` 的注释），这里不再重复。
    """
    import numpy as np
    import soundfile as sf

    try:
        wa, wch, wsub, wframes, _wf = probe(wav_path)
        fa, fch, fsub, fframes, _ff = probe(flac_path)
    except CompressionError as exc:
        return False, str(exc)
    if (wa, wch) != (fa, fch):
        return False, ("参数不一致：wav %d Hz/%d 声道，flac %d Hz/%d 声道"
                       % (wa, wch, fa, fch))
    if int(wframes) != int(fframes):
        return False, ("帧数不一致：wav %d 帧，flac %d 帧（时长会变）"
                       % (int(wframes), int(fframes)))
    if wframes <= 0:
        return False, "wav 里没有帧"
    want = _flac_subtype_for(wsub)
    if not want:
        return False, "wav 的编码是 %s，FLAC 没有对应的无损档位" % (wsub or "?")
    if str(fsub or "").upper() != want:
        return False, ("位深档位不一致：源 %s 需要 %s，产物是 %s（会丢精度）"
                       % (wsub or "?", want, fsub or "?"))
    try:
        with sf.SoundFile(wav_path, "r") as a, sf.SoundFile(flac_path, "r") as b:
            pos = 0
            while True:
                ba = a.read(block_frames, dtype="int32", always_2d=True)
                bb = b.read(block_frames, dtype="int32", always_2d=True)
                if len(ba) != len(bb):
                    return False, ("分块长度不一致（第 %d 帧起：wav %d / flac %d）"
                                   % (pos, len(ba), len(bb)))
                if not len(ba):
                    break
                if ba.shape != bb.shape:
                    return False, "形状不一致（第 %d 帧：%s vs %s）" % (pos, ba.shape, bb.shape)
                diff = int(np.abs(ba - bb).max()) if ba.size else 0
                if diff:
                    return False, ("第 %d 帧起样本不同（最大差 %d）—— 不是无损压缩"
                                   % (pos, diff))
                pos += len(ba)
    except CompressionError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, "比读失败：%s" % _short(exc)
    return True, ""


def _rms_delta(wav_path, flac_path, block_frames=VERIFY_BLOCK_FRAMES):
    """两条音轨的 RMS 差（float64，越小越像）。比读已经逐样本相等时它必然 ~0 ——
    留着它是为了让日志里有一行**独立算出来的**波形指标（人看数字比看"相等"踏实）。"""
    import numpy as np
    import soundfile as sf
    acc = 0.0
    n = 0
    with sf.SoundFile(wav_path, "r") as a, sf.SoundFile(flac_path, "r") as b:
        while True:
            ba = a.read(block_frames, dtype="float64", always_2d=True)
            bb = b.read(block_frames, dtype="float64", always_2d=True)
            if not len(ba) or not len(bb):
                break
            d = ba - bb
            acc = max(acc, float(np.sqrt(np.mean(d * d))) if d.size else 0.0)
            n += 1
    return acc


def compress_segment(wav_path, *, subtype="", keep_raw=False, verify=True):
    """把一个段 wav **无损**压成同名 `.flac`；返回一份结果字典。

    返回（全部字段都在，失败也返回，不抛 —— 调用方要"保留原件 + 明确报错"）：

        ok            True = flac 已落地且校验通过
        wav/flac      路径
        before/after  字节数
        deleted       是否删掉了原件
        verified      校验结论（"lossless" / 空）
        reason        失败原因（人话）

    安全顺序（**一条都不能少**，这是本功能的核心约束）：

        1. 读源参数 → 选无损档位（判不了就**不动**它）
        2. 编码到 `xx.flac.part`（同名不同后缀，避免半截 flac 被当成完成品）
        3. **读回**：帧数/采样率/声道/位深逐项比 + 分块逐样本比
        4. 校验通过才 `os.replace` 成 `xx.flac`
        5. 通过之后**才**删原件；`keep_raw=True` 时**永不删**

    任何一步不过：删掉 `.part`、**保留原件**、`ok=False` 且 `reason` 说清哪里不过。
    """
    before = _size(wav_path)
    flac_path = os.path.splitext(wav_path)[0] + FLAC_EXT
    out = {"ok": False, "wav": wav_path, "flac": flac_path, "before": before,
           "after": 0, "deleted": False, "verified": "", "reason": ""}
    if not os.path.isfile(wav_path):
        out["reason"] = "源文件不存在"
        return out
    ok, why = can_compress(wav_path)
    if not ok:
        out["reason"] = why
        return out
    if os.path.isfile(flac_path) and os.path.getsize(flac_path) > 0:
        # 幂等：已经有 flac 了 —— 不重复压、不报错（但**也不删 wav**：两个都在
        # 说明上一次收尾没完成，由调用方按"校验通过才删"的规则处理）。
        out["ok"] = True
        out["after"] = _size(flac_path)
        out["verified"] = "existing"
        return out

    rate, channels, src_subtype, frames, _fmt = probe(wav_path)
    want = subtype or _flac_subtype_for(src_subtype)
    part = flac_path + ".part"
    _unlink(part)
    try:
        import soundfile as sf
        with sf.SoundFile(wav_path, "r") as fh:
            with sf.SoundFile(part, "w", samplerate=rate, channels=channels,
                              subtype=want, format="FLAC") as out_fh:
                for block in fh.blocks(blocksize=VERIFY_BLOCK_FRAMES, dtype="int16",
                                       always_2d=True):
                    out_fh.write(block)
        if _size(part) <= 0:
            raise CompressionError("写出的 flac 是空文件")
        if verify:
            vok, vwhy = compare_lossless(wav_path, part)
            if not vok:
                raise CompressionError("读回校验不通过：%s" % vwhy)
            out["verified"] = "lossless"
        os.replace(part, flac_path)
    except CompressionError as exc:
        _unlink(part)
        out["reason"] = str(exc)
        return out
    except Exception as exc:
        _unlink(part)
        out["reason"] = "压缩失败：%s" % _short(exc)
        return out

    out["ok"] = True
    out["after"] = _size(flac_path)
    rms = 0.0
    try:
        rms = _rms_delta(wav_path, flac_path)
    except Exception:
        rms = -1.0
    out["rmsDelta"] = rms
    if not keep_raw:
        # 走到这里 = flac 已落地且**逐样本校验通过** —— 删原件是安全的。
        try:
            os.remove(wav_path)
            out["deleted"] = True
        except OSError as exc:
            out["reason"] = "原件删除失败（flac 已就绪）：%s" % _short(exc)
    return out


def plan_for_meeting(folder, *, keep_raw=False):
    """一场会议的压缩计划（**纯计算，不写盘**）。

    返回：
        compressible   还能压的 wav 路径列表
        skipped        跳过的 `(文件名, 为什么)`（已压缩 / **坏了** / 不划算）
        broken         跳过的里面属于"**文件本身有问题**"的那些（截断/读不出参数）。
                       与 `skipped` 分开是刻意的：`skipped` 里"已经是 flac 了"是**正常**
                       情况（幂等），而"这段坏了"必须让整场报 `ok=False` 并把原因
                       交给用户 —— 只报"这一场没问题"，用户永远不会知道有段坏在盘上。
        before         这些 wav 的字节数合计
        estimate       按 FLAC 经验比例估的产物字节数（省多少看它）
        already        目录里已经有 flac 的段数（幂等判据）
        compressed     这一场是否已经是"已压缩"状态
        error          整体性错误（目录都读不了时才非空）
    """
    out = {"compressible": [], "skipped": [], "broken": [], "before": 0, "estimate": 0,
           "already": 0, "compressed": False, "error": ""}
    if not os.path.isdir(folder):
        out["error"] = "会议目录不存在"
        return out
    names = segment_files(folder)
    wavs = [n for n in names if n.lower().endswith(WAV_EXT)]
    flacs = [n for n in names if n.lower().endswith(FLAC_EXT)]
    out["already"] = len(flacs)
    stems_flac = {os.path.splitext(n)[0] for n in flacs}
    for name in wavs:
        path = os.path.join(folder, name)
        size = _size(path)
        if os.path.splitext(name)[0] in stems_flac:
            # 两个都在 = 上次收尾没完成；不重复压，但也**不删**（交给压缩任务收尾）
            out["skipped"].append((name, "同段已有 flac（上次收尾未完成）"))
            continue
        ok, why = can_compress(path)
        if not ok:
            out["skipped"].append((name, why))
            # 判据只有一条：**文件有硬伤**（截断 / 读不出参数 / 不是 WAV）。
            # 措辞上的"格式不支持"（32-bit / 浮点 wav）也归这里 —— 那同样是
            # "这段压不了"，用户要看见它。
            out["broken"].append((name, why))
            continue
        out["compressible"].append(path)
        out["before"] += size
    #: FLAC 的经验比例：本机实测 60 秒语音 1.92 MB → 937 KB，即 ~49%。
    #: 这是**预览用的估算**，实际数字在压缩完成之后按真实字节回报，
    #: 面板上"估算"与"实际"必须分得清（不然用户会以为预览骗了他）。
    out["estimate"] = int(round(out["before"] * 0.51))
    out["compressed"] = bool(flacs) and not wavs
    return out


def scan_meetings(items, *, keep_raw=False):
    """一批 `(meeting_name, folder)` → 汇总"可压缩 N 场 / 能省 X"（**只读，不写盘**）。

    返回 `{"meetings": [...], "count": N, "beforeBytes": …, "estimateBytes": …,
           "alreadyBytes": …, "alreadyMeetings": N}`。
    **不碰任何文件** —— 面板点「看看能省多少」时走的就是它。
    """
    out = {"meetings": [], "count": 0, "beforeBytes": 0, "estimateBytes": 0,
           "alreadyBytes": 0, "alreadyMeetings": 0}
    for name, folder in items:
        plan = plan_for_meeting(folder, keep_raw=keep_raw)
        entry = {"name": name, "folder": folder, "compressible": len(plan["compressible"]),
                 "beforeBytes": plan["before"], "estimateBytes": plan["estimate"],
                 "alreadySegments": plan["already"], "compressed": plan["compressed"],
                 "skipped": [{"file": f, "reason": r} for f, r in plan["skipped"]],
                 "broken": [{"file": f, "reason": r} for f, r in plan["broken"]],
                 "error": plan["error"]}
        # 已压缩的会议：把**真实**的压缩前/后字节从 meta.json 里带出来，
        # 面板才能显示"原 149 MB → 现 76 MB（省 49%）"而不是再估一次。
        meta = _read_meta(folder)
        comp = meta.get("compression") if isinstance(meta.get("compression"), dict) else None
        if comp:
            entry["compressedBytes"] = {
                "before": int(comp.get("beforeBytes") or 0),
                "after": int(comp.get("afterBytes") or 0),
                "percent": int(comp.get("savedPercent") or 0),
                "deleted": bool(comp.get("deletedRaw")),
                "at": comp.get("at") or "",
            }
            out["alreadyBytes"] += int(comp.get("afterBytes") or 0)
        if plan["compressible"]:
            out["count"] += 1
            out["beforeBytes"] += plan["before"]
            out["estimateBytes"] += plan["estimate"]
        if plan["compressed"]:
            out["alreadyMeetings"] += 1
        out["meetings"].append(entry)
    return out


def _read_meta(folder):
    import json
    try:
        with open(os.path.join(folder, "meta.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_meta(folder, meta):
    """把 `meta.json` 写回（原子替换：先写 `.part` 再 replace）。

    为什么要原子：压缩与转写**可能同时读**这个文件（面板轮询详情、转写在写它的
    `transcribed` 列表）。半截 JSON 会让"这场会用了哪个后端"这类字段整个读不出来。
    """
    import json
    path = os.path.join(folder, "meta.json")
    part = path + ".part"
    try:
        with open(part, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        os.replace(part, path)
    except Exception:
        _unlink(part)
        raise


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def stat_bytes(path):
    """公开的大小读取（日志/预览用）。"""
    return _size(path)


def dir_bytes(folder, regex=None):
    """目录里匹配 `regex` 的文件字节合计（不给 regex = 全部普通文件）。"""
    total = 0
    try:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            if regex is not None and not regex.match(name):
                continue
            total += _size(path)
    except OSError:
        return 0
    return total


def remove_tree(path):
    """删掉一个目录（压缩失败回滚用；**只在确认是自己刚建的临时目录时调用**）。"""
    shutil.rmtree(path, ignore_errors=True)
