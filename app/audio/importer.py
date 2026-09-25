# -*- coding: utf-8 -*-
"""importer.py — 把"用户手里的一段录音"变成会议链路认得的音频段。

## 为什么需要这个模块（而 `api._audio_to_wav16k` 不够）

`api.py` 里那个 `_audio_to_wav16k()` 是给 `/api/stt/transcribe`（单文件转写）用的：
它 `sf.read()` **把整个文件读进内存**，并且假定调用方已经知道这个文件能读。
导入录音这条路有三件它不管的事：

  1. **格式闸门**：`m4a`/`aac` 本机（soundfile 0.14 + libsndfile 1.2.2）**读不了**，
     必须**响亮拒绝**并说清"需要 ffmpeg、请先转成 wav/mp3" —— 不许静默失败，
     也不许在半路留下一个空会议。
  2. **分块**：50 MB+ 的会议录音不许整段进内存（上传那一侧分块落**临时文件**，
     这一侧分块解码 → 单声道 → 重采样 → 写 16-bit PCM WAV）。
  3. **重采样走了哪条路要说清楚**：`soxr` 流式 → `soxr` 一次性 →
     `scipy.signal.resample_poly` → 线性插值兜底，用了哪个**写进日志与 meta.json**。

sherpa **只吃 RIFF/WAV**（喂 flac/ogg 会报 `file does not start with RIFF id`），
会议链路里到处是 `^\\d+\\.wav$`（`_transcribe_impl` / `_seg_duration_map` /
`retranscribe_meeting`），所以输出**只能是** 16 kHz / 单声道 / 16-bit PCM 的 `.wav`。

## 实测（2026-09-25，本机 venv）

    soundfile 0.14.0 + libsndfile 1.2.2：WAV / FLAC / MP3 / OGG 都能读
    soxr 0.5.0.post1：`ResampleStream` 分块结果与一次性 `soxr.resample` **逐样本相同**

所以「支持 wav/flac/mp3/ogg、不支持 m4a/aac」不是估计，是本机实测出来的能力边界。
"""
from __future__ import annotations

import os
import wave
from collections import namedtuple

#: 会议链路的唯一目标格式（sherpa 只吃 RIFF/WAV；整条链路按 `\\d+.wav` 认段）。
TARGET_RATE = 16000
TARGET_CHANNELS = 1
TARGET_SUBTYPE = "PCM_16"
#: 16-bit PCM 的采样字节数（`wave` 报的是字节，不是位）。
TARGET_SAMPLE_WIDTH = 2

#: 本机实测**读得了**的容器（`sf.available_formats()` 的子集，取用户真会拿到的那几个）。
SUPPORTED_FORMATS = ("WAV", "FLAC", "MP3", "OGG")

#: 本机**读不了**、且用户最常撞上的容器/编码。撞上就响亮拒绝，并给出下一步。
#: `M4A`/`MP4` 是 AAC 的家；`AAC` 裸流 libsndfile 也不认；`OPUS` 单独列出来是因为
#: "能不能读"取决于 libsndfile 编进来的编解码器，这台机器上不保证 —— 说不准的事
#: 就当"读不了"报，宁可让用户转一次（转一次是有损但**明确**的，静默失败不是）。
UNSUPPORTED_FORMATS = ("M4A", "AAC", "MP4", "WMA", "AMR", "OPUS", "AC3")

#: 用扩展名就能**提前**判死的那几个（省得白读一遍大文件）；判死的理由与上面同一条。
UNUSABLE_EXTS = (".m4a", ".m4b", ".m4p", ".aac", ".mp4", ".wma", ".amr", ".opus", ".ac3")

#: 分块解码的默认块大小（帧）。**这个数就是"不整段进内存"的实现**：
#: 16 kHz 单声道 float32 下 65536 帧 ≈ 256 KB/块（44.1 kHz 立体声 ≈ 1 MB/块）。
DEFAULT_BLOCK_FRAMES = 65536

#: 写出去的块大小（帧）—— 与解码分开，便于大文件时把 write 的抖动摊平。
WRITE_BLOCK_FRAMES = 65536

#: 重采样走过的路（写进日志与 meta.json，用户与排障都看这个字段）。
RESAMPLE_NONE = "none"          #: 本来就是 16 kHz，没重采样
RESAMPLE_SOXR_STREAM = "soxr-stream"
RESAMPLE_SOXR = "soxr"
RESAMPLE_SCIPY = "scipy-resample_poly"
RESAMPLE_LINEAR = "linear"

#: 给人看的说法（面板/日志里直接显示这一列，不要显示英文键）。
RESAMPLE_LABELS = {
    RESAMPLE_NONE: "无需重采样（本来就是 16 kHz）",
    RESAMPLE_SOXR_STREAM: "soxr 流式重采样（分块，内存恒定）",
    RESAMPLE_SOXR: "soxr 一次性重采样（整段在内存里）",
    RESAMPLE_SCIPY: "scipy.signal.resample_poly",
    RESAMPLE_LINEAR: "线性插值（兜底，质量最低）",
}


class ImportAudioError(Exception):
    """音频导入失败 —— `str(e)` 是**给人看的一句话**（面板原样显示，不许自己编）。

    为什么用异常而不是返回 `(False, msg)`：调用链上有很多失败点（空文件 / 读不了 /
    打开失败 / 写出错 / 磁盘满），返回值形式的错误**总有人忘了检查**；而且失败时
    "产物必须不存在"（会议记录、会议目录、半截的 wav）需要**一层统一的清理出口**。
    """


#: 一次转换的结果。
#:   src            源文件路径（可能是个临时文件）
#:   filename       **给人看的**原始文件名（`src` 是临时文件时也要能显示用户认识的名字）
#:   dst            目标的 16k 单声道 16-bit wav
#:   seconds        时长（秒，按输出帧数算 —— 与会议链路里 `_wav_seconds()` 同一口径）
#:   frames         输出帧数
#:   dst_bytes      输出字节数（落库的 `audio_bytes` 用它）
#:   src_format     源容器（WAV/FLAC/MP3/OGG…，soundfile 的原样值）
#:   src_rate       源采样率（如实记录，meta.json 要写）
#:   src_channels   源声道数（如实记录）
#:   src_subtype    源编码（PCM_16/PCM_24/MP3/…）
#:   resampled      重采样走过的路（RESAMPLE_* 之一）
#:   block_frames   实际使用的分块大小（用例据此断言"没整段进内存"）
ConversionResult = namedtuple(
    "ConversionResult",
    "src filename dst seconds frames dst_bytes src_format src_rate src_channels "
    "src_subtype resampled block_frames")


# ------------------------------------------------------------------ 名字与格式判定
def _ext(filename):
    return os.path.splitext(str(filename or ""))[1].lower()


def display_name(filename):
    """原始文件名（只取最后一段）：报错文案与 meta.json 都用它。"""
    return os.path.basename(str(filename or "").replace("\\", "/")) or "（未命名）"


def unsupported_reason(filename, fmt=""):
    """读不了的格式 → 一句**要素齐全**的人话（哪个格式 / 为什么 / 怎么办）。

    三要素缺一不可（与本项目 `meeting.unsupported_engine_reason()` 同一条纪律）：
      * 只说"不支持" → 用户不知道是文件坏了还是格式不行；
      * 只说"要用 ffmpeg" → 用户不知道 ffmpeg 是干什么的、为什么要它；
      * 不给下一步 → 用户只能原样重试一次。
    """
    who = (fmt or "").upper() or "未知格式"
    return (
        "音频「%s」的格式 %s 本机读不了：它需要 ffmpeg 解码，而 ECHO 不依赖 ffmpeg。"
        "请先把它转成 wav 或 mp3 再导入"
        "（例如 ffmpeg -i 输入.m4a -ac 1 -ar 16000 输出.wav）。"
        "本机**可以**直接导入的格式：wav、flac、mp3、ogg。"
        % (display_name(filename), who))


def unusable_extension_reason(filename):
    """扩展名一眼就是"本机读不了"的那几个（m4a/aac/mp4…）→ 提前判死的那句话。

    为什么值得单独一条、而不是等着解码失败：`m4a` 是手机录音的**头号格式**，
    用户最容易撞上。提前拒绝既省掉一次白读大文件，报错文案也才能**点名格式**
    —— 解码失败的报错是 libsndfile 的 `Format not recognised.`，认不出是 m4a。
    """
    return unsupported_reason(filename, _ext(filename).lstrip(".").upper())


def short_error(exc):
    """异常 → 一行短原因（libsndfile 的报错很长，面板与日志行上要能读）。"""
    text = str(exc or "").strip().replace("\n", " ")
    return (text or type(exc).__name__)[:300]


def _probe(path, filename):
    """打开一次、拿基本信息；读不了就直接抛 `ImportAudioError`（带真原因 + 下一步）。

    这一层是"假音频"的**唯一**判据：内容不是音频 → soundfile 抛 `LibsndfileError`，
    我们把**它自己说的话**带出去（`Format not recognised.` 这类），而不是编一句"导入失败"。
    """
    import soundfile as sf
    try:
        info = sf.info(path)
    except Exception as exc:
        raise ImportAudioError(
            "「%s」不是能识别的音频文件（解码失败：%s）。"
            "请确认它是完整的 wav/flac/mp3/ogg；m4a/aac 需要先用 ffmpeg 转换。"
            % (display_name(filename), short_error(exc)))
    frames = int(info.frames or 0)
    if frames <= 0:
        raise ImportAudioError(
            "「%s」里没有任何音频数据（解码出 0 帧）——空文件或只有文件头，"
            "不能当会议音频导入。" % display_name(filename))
    fmt = str(info.format or "").upper()
    if fmt in UNSUPPORTED_FORMATS or fmt not in SUPPORTED_FORMATS:
        raise ImportAudioError(unsupported_reason(filename, fmt))
    return info, frames


# ------------------------------------------------------------------ 分块解码
def iter_mono_blocks(path, block_frames=DEFAULT_BLOCK_FRAMES):
    """分块读 → 单声道 float32。

    **这是"不整段进内存"的落点**：`sf.SoundFile.blocks()` 每次最多给 `block_frames` 帧，
    立体声在这里就地平均成单声道（手机/会议录音多是 2 声道 → 内存再减半）。
    `always_2d=True` 是为了让单声道文件也有 `(n, 1)` 的形状（否则 `mean(axis=1)` 会走错轴）。

    ⚠️ **绝不能给 `blocks()` 传 `fill_value`**（2026-09-25 实测踩到）：传了之后
    **最后一块会被补齐到整块大小**，于是"读到的总帧数"比文件实际帧数多 ——
    16 kHz 的 1 秒文件（16000 帧）配默认块大小能报出 65536 帧（**多出 4 倍的静音**），
    导进去的会议会莫名其妙变长、转写时间轴全错。不传 `fill_value` 时
    `blocks()` 的帧数之和**恰好等于** `info.frames`（同一次实测）。
    """
    import numpy as np
    import soundfile as sf
    with sf.SoundFile(path, "r") as fh:
        for block in fh.blocks(blocksize=int(block_frames), dtype="float32",
                               always_2d=True):
            if block.shape[1] > 1:
                mono = block.mean(axis=1, keepdims=True)
            else:
                mono = block
            yield np.ascontiguousarray(mono[:, 0], dtype="float32")


def tagged(blocks):
    """给块序列打上 `is_last`：**先读一块、留着下一块**，就知道手里这块是不是最后一块。

    为什么要打标：`soxr.ResampleStream` 靠 `last=True` 收尾（把滤波器尾巴吐出来）。
    猜错（漏了 `last`）的代价是结尾少几个样本 —— 听不出来，但会让"输出帧数"与
    "时长×采样率"差一点点，那种误差日后没法解释。
    """
    it = iter(blocks)
    try:
        pending = next(it)
    except StopIteration:
        return
    for nxt in it:
        yield pending, False
        pending = nxt
    yield pending, True


# ------------------------------------------------------------------ 重采样（四条路）
def _resample_stream_seq(blocks, src_rate):
    """路 ①：`soxr.ResampleStream` 分块重采样 —— **内存恒定**（首选）。

    为什么首选：`soxr` 是既有依赖（`api._audio_to_wav16k` 与录音链路都在用），
    而 `ResampleStream` 允许"解码一块 → 重采样一块 → 写一块"串起来，整段音频**从不到内存**。
    """
    import soxr
    stream = soxr.ResampleStream(src_rate, TARGET_RATE, TARGET_CHANNELS, dtype="float32")
    for block, is_last in blocks:
        out = stream.resample_chunk(block, last=is_last)
        if out is not None and len(out):
            yield out


def _resample_oneshot(mono, src_rate):
    """路 ②：`soxr.resample` —— 整段在内存里（分块那条路用不了时的次选，质量同级）。"""
    import soxr
    return soxr.resample(mono, src_rate, TARGET_RATE)


def _resample_scipy(mono, src_rate):
    """路 ③：`scipy.signal.resample_poly` —— 既有重依赖（requirements 里有 scipy）。

    用 `Fraction` 约成整数比：`resample_poly` 只吃整数 up/down，而 `16000/44100`
    不能直接当参数（44.1 kHz → 16000 是 160/441）。
    """
    from fractions import Fraction
    from scipy.signal import resample_poly
    frac = Fraction(TARGET_RATE, int(src_rate)).limit_denominator(1000)
    return resample_poly(mono, frac.numerator, frac.denominator).astype("float32")


def _resample_linear(mono, src_rate):
    """路 ④：线性插值 —— **最后兜底**（soxr 与 scipy 都不可用时）。

    会丢高频（16 kHz 的奈奎斯特是 8 kHz，而它不做抗混叠），所以它一旦出现在日志里就
    意味着"这台机器缺 soxr 和 scipy"、转写质量可能下降 —— 这正是要把重采样路径
    写进日志的原因（不然这种降级没有任何痕迹）。
    """
    import numpy as np
    n_out = int(round(len(mono) * float(TARGET_RATE) / float(src_rate)))
    if n_out <= 0:
        return np.zeros(0, dtype="float32")
    x_old = np.arange(len(mono), dtype="float64")
    x_new = np.linspace(0.0, max(len(mono) - 1, 0), n_out, dtype="float64")
    return np.interp(x_new, x_old, mono.astype("float64")).astype("float32")


def _collect_oneshot(src, block_frames, fn, src_rate):
    """整段读进内存再交给 `fn` 重采样（路 ②③④ 共用）—— 只在流式那条走不了时才用。"""
    import numpy as np
    parts = list(iter_mono_blocks(src, block_frames))
    mono = np.concatenate(parts) if parts else np.zeros(0, dtype="float32")
    del parts
    return np.asarray(fn(mono, src_rate), dtype="float32")


# ------------------------------------------------------------------ 写出
def _write_all(out, blocks):
    """把块序列写进 `out`，返回写入的帧数（按 `WRITE_BLOCK_FRAMES` 攒批，别一块一写）。"""
    import numpy as np
    n = 0
    buf, pending = [], 0
    for block in blocks:
        if block is None or len(block) == 0:
            continue
        buf.append(np.asarray(block, dtype="float32"))
        pending += len(block)
        if pending >= WRITE_BLOCK_FRAMES:
            out.write(np.concatenate(buf))
            n += pending
            buf, pending = [], 0
    if pending:
        out.write(np.concatenate(buf))
        n += pending
    return n


def _unlink(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def convert_to_16k_mono(src, dst, *, filename="", block_frames=DEFAULT_BLOCK_FRAMES,
                        logger=None):
    """把受支持的音频转成会议链路认得的 16 kHz / 单声道 / 16-bit PCM WAV。

    参数：
      src / dst      源与目标路径（**流式**：源分块读，目标分块写）
      filename       给报错文案与 meta 用的原始文件名（`src` 可能是临时文件，名字没意义）
      block_frames   分块大小（用例靠它断言"没整段进内存"）
      logger         可选 `logger(level, message)`；调用方传 `db.add_log` 的包装

    返回 `ConversionResult`；任何失败抛 `ImportAudioError`（人话），**并保证 dst 不残留**。

    走哪条重采样路会**写进 logger** —— 用户问"这段录音转得准不准"时，日志里得有答案。
    """
    def note(msg, level="info"):
        if logger:
            try:
                logger(level, msg)
            except Exception:
                pass

    if not os.path.isfile(src):
        raise ImportAudioError("上传的临时文件不见了（%s）——请重新导入。" % src)
    if _ext(filename) in UNUSABLE_EXTS:
        # **扩展名闸门在最前面**：m4a/aac 是手机录音的头号格式，用户最容易撞上。
        # 不提前判死的话，报出来的是 libsndfile 那句 `Format not recognised.`
        # —— 它认不出"这是 m4a"，用户也就得不到"装/用 ffmpeg 转一下"这条出路。
        raise ImportAudioError(unusable_extension_reason(filename))
    if os.path.getsize(src) == 0:
        raise ImportAudioError("「%s」是空文件（0 字节），没有音频可导入。"
                               % display_name(filename))

    info, _frames = _probe(src, filename)
    src_rate = int(info.samplerate or 0)
    src_channels = int(info.channels or 1)
    src_subtype = str(info.subtype or "")
    src_format = str(info.format or "").upper()
    if src_rate <= 0:
        raise ImportAudioError("「%s」的采样率读不出来（%r），无法转换。"
                               % (display_name(filename), src_rate))

    if _ext(filename) in UNUSABLE_EXTS:
        # 到这里说明**解码居然成功了**（例如 .opus 里其实是 ogg）：扩展名说读不了、
        # 内容却读得出来。以内容为准放行，但要把这件事说出来 —— 静默接受一个
        # "扩展名与内容不符"的文件，日后没人查得出它从哪来。
        # 注：**扩展名直接判死的那种已经在函数开头挡掉了**，走不到这里；
        # 这一条留给"扩展名可疑、内容却是受支持容器"的情形（例如 .ac3 里其实是 wav）。
        note("音频 %s 的扩展名 %s 本机不支持，但内容能解码（容器 %s）——按内容处理"
             % (display_name(filename), _ext(filename), src_format), "warn")

    method = RESAMPLE_NONE
    total = 0
    scratch = dst + ".part"
    _unlink(scratch)
    try:
        if src_rate == TARGET_RATE:
            # 同采样率：只做单声道 + 重编码（PCM_16），不重采样。
            total = _convert_once(src, scratch, None, block_frames)
            _replace(scratch, dst)
        else:
            stages = (
                (RESAMPLE_SOXR_STREAM, "soxr 流式"),
                (RESAMPLE_SOXR, "soxr 一次性"),
                (RESAMPLE_SCIPY, "scipy.signal.resample_poly"),
                (RESAMPLE_LINEAR, "线性插值兜底"),
            )
            for name, label in stages:
                try:
                    # **每条路各写一个临时文件**：上一条路写了一半才炸的话，
                    # 重试的那条从零开始，不会把半截数据接在后面。
                    total = _convert_once(src, scratch, name, block_frames, src_rate)
                except ImportError as exc:
                    # **只有缺依赖才换下一条路**：其它异常（坏文件、磁盘满）必须原样
                    # 抛出去 —— 换条路重采样救不了磁盘满，只会把真正的原因盖掉
                    # （那正是本项目最忌讳的"自己编一个原因"）。
                    note("重采样路径 %s 不可用（%s），改走下一条"
                         % (label, short_error(exc)), "warn")
                    _unlink(scratch)
                    continue
                method = name
                _replace(scratch, dst)
                break
            else:
                raise ImportAudioError(
                    "本机没有任何可用的重采样实现（soxr 与 scipy 都不在），"
                    "无法把 %s Hz 转成 %d Hz。请先安装 soxr（pip install soxr）后重试。"
                    % (src_rate, TARGET_RATE))
    finally:
        _unlink(scratch)

    if not total:
        _unlink(dst)
        raise ImportAudioError("「%s」转换之后没有任何音频数据（%d 帧）——无法导入。"
                               % (display_name(filename), total))
    try:
        check_target_wav(dst)
    except ImportAudioError:
        _unlink(dst)
        raise

    note("音频导入：%s → %s（源 %s %.0f Hz %d 声道 %s；重采样=%s）"
         % (display_name(filename), os.path.basename(dst), src_format, src_rate,
            src_channels, src_subtype or "?", RESAMPLE_LABELS.get(method, method)))
    return ConversionResult(src=src, filename=display_name(filename), dst=dst,
                            seconds=total / float(TARGET_RATE), frames=total,
                            dst_bytes=_size(dst), src_format=src_format,
                            src_rate=src_rate, src_channels=src_channels,
                            src_subtype=src_subtype, resampled=method,
                            block_frames=int(block_frames))


def _replace(src, dst):
    """把临时产物挪到最终位置（同一目录，`os.replace` 是原子的）。"""
    os.replace(src, dst)


def _resample_seq(src, src_rate, method, block_frames):
    """按 `method` 产出"已重采样"的块序列（只有 `RESAMPLE_SOXR_STREAM` 是分块的）。"""
    if method == RESAMPLE_SOXR_STREAM:
        return _resample_stream_seq(tagged(iter_mono_blocks(src, block_frames)), src_rate)
    fn = {RESAMPLE_SOXR: _resample_oneshot,
          RESAMPLE_SCIPY: _resample_scipy,
          RESAMPLE_LINEAR: _resample_linear}[method]
    return iter([_collect_oneshot(src, block_frames, fn, src_rate)])


def _convert_once(src, out_path, method, block_frames, src_rate=0):
    """跑一次转换、写 `out_path`，返回写入帧数。

    `method=None` = 不重采样（源就是 16 kHz，只做单声道 + PCM_16）。
    每次调用**新建**输出文件（`"w"` 截断写），所以失败重试不会把半截数据接在后面。
    """
    import soundfile as sf
    with sf.SoundFile(out_path, "w", samplerate=TARGET_RATE, channels=TARGET_CHANNELS,
                      subtype=TARGET_SUBTYPE, format="WAV") as out:
        if method is None:
            return _write_all(out, iter_mono_blocks(src, block_frames))
        return _write_all(out, _resample_seq(src, src_rate, method, block_frames))


# ------------------------------------------------------------------ 读回校验
def describe_wav(path):
    """读一个 wav 的**真参数**：`(rate, channels, sample_width_bytes, frames)`。

    刻意用标准库 `wave` 而不是 soundfile：`wave` 读的就是 RIFF 头里的 `fmt ` 块
    —— 那正是 sherpa / whisper 要的几个数字，而且它**不依赖任何第三方库**。
    "产物到底是不是 16 kHz 单声道 16-bit"这件事，该由最不挑剔的读者来回答。
    """
    with wave.open(path, "rb") as w:
        return (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes())


def check_target_wav(path):
    """产物自检：不是 16 kHz / 单声道 / 16-bit 就抛 `ImportAudioError`。

    为什么要在**写完当场**验一遍：写出去的东西是给 sherpa/whisper 读的，参数错了的
    现象是"转写没声音/乱码"，而那时离导入已经很远，查起来极贵。
    """
    rate, channels, width, frames = describe_wav(path)
    if (rate, channels, width) != (TARGET_RATE, TARGET_CHANNELS, TARGET_SAMPLE_WIDTH):
        raise ImportAudioError(
            "转换结果不合要求：%s 是 %d Hz / %d 声道 / %d 字节采样（要求 %d Hz / %d 声道 / "
            "16-bit）。这是 ECHO 的 bug，请把日志里那条『音频导入』发回来。"
            % (os.path.basename(path), rate, channels, width, TARGET_RATE, TARGET_CHANNELS))
    if frames <= 0:
        raise ImportAudioError("转换结果 %s 里没有帧。" % os.path.basename(path))
    return (rate, channels, width, frames)
