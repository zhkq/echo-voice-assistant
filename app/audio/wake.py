# -*- coding: utf-8 -*-
"""wake.py — ECHO 语音唤醒（流式 ASR 文字匹配为主，KWS 回退）

唤醒检测两种实现：
  1. 流式 ASR（sherpa-onnx zipformer transducer，models/sherpa-onnx-streaming）：
     边说边转写，把识别文本与唤醒词匹配 —— 抗口音/自然语音，推荐。
     实测：KWS 对自然语音的"嘿尼欧/小尼小尼"识别不了（需标准带调发音），
     而流式 ASR 能正确转写并命中。
  2. KWS（models/wakeword/kws-zh-en-3m）：关键词 spotting，模型缺失时回退。
"""
import os
import re
import threading
import time
from collections import deque

from app import paths

SR = 16000
BLOCK = 1280  # 80ms


def kws_model_dir() -> str:
    """KWS 权重目录：**跟随 modelsDir**（D20/D21）。

    原来这里是 ``{ECHO}/models/wakeword/kws-zh-en-3m`` 的**模块级常量**——用户把
    modelsDir 指到别处时，唤醒词仍去安装目录找，与 config 里 modelsDir 的承诺
    （"whisper / SenseVoice / 唤醒词 / pyannote…"）不符，是真 bug。
    用函数不用常量，理由同 ``audio/stt.py::models_dir()``：请求时解析。
    """
    return os.path.join(paths.models_root(), "wakeword", "kws-zh-en-3m")


#: `wakeEngine` 取值 -> 日志/面板里显示的名字（与 config.py 的 options 一一对应）
ENGINE_LABELS = {"sherpa": "sherpa 流式 ASR", "kws": "KWS 关键词 spotting"}


def engine_label(value=None):
    """当前唤醒实现的人话名字；未给值时读配置，未知取值按实现里的默认（sherpa）算。"""
    if value is None:
        try:
            from app.config import settings as _s
            value = _s.get("wakeEngine", "sherpa")
        except Exception:
            value = "sherpa"
    return ENGINE_LABELS.get(str(value or "").strip().lower(), ENGINE_LABELS["sherpa"])


def _norm(s):
    """归一化识别文本：小写、去空白标点（保留中日韩/字母数字）。"""
    s = (s or "").lower()
    return re.sub(r"[\s，。、,.!?！？·\-'\"（）()\[\]「」『』:：;；_~、]+", "", s)


#: `pypinyin` 缺失时**只警告一次**（它决定拼音层是否可用；缺了会静默退化）。
_PINYIN_WARNED = threading.Event()


def pinyin_available():
    """`pypinyin` 能不能用（**客户机上它曾经一直是 False**，见下）。

    2026-10-08 实测查到的事故：`pypinyin` **从未写进任何依赖清单**
    （`requirements-core.txt` / `requirements.txt` / `pyproject.toml` 都没有），
    而 `_pinyin()` 用 `except` 把导入失败**静默吞掉**。后果分两层：
      * dev 机（venv 里恰好有）能唤醒、**客户机上唤不醒**，且**没有任何报错**；
      * 退化后 `_pinyin()` 返回**整段文本一个元素**，拼音容错层等于死代码。
    修法两条一起上：① 依赖清单补 `pypinyin`（根治）；② 这里如实暴露状态 +
    `_StreamDetector` 走"逐字子串"兜底（让**已经发出去的旧装机**不至于完全唤不醒）。
    """
    try:
        import pypinyin                                            # noqa: F401
        return True
    except Exception:
        return False


def _pinyin(text):
    """文本 -> 拼音音节列表（无声调）。非中文按原样小写保留。

    ⚠️ 返回**是不是拼音**取决于 `pypinyin` 在不在（见 `pinyin_available`）。
    没有它时返回 `[整段小写文本]` —— 所以调用方**不能假设这是音节序列**，
    必须先用 `pinyin_available()` 判（`_StreamDetector.hit()` 就是这么做的）。
    """
    try:
        from pypinyin import lazy_pinyin
        return [s for s in lazy_pinyin(str(text or "")) if s]
    except Exception:
        if _PINYIN_WARNED is not None and not _PINYIN_WARNED.is_set():
            _PINYIN_WARNED.set()
            print("[wake] 没装 pypinyin —— 拼音容错层不可用（只剩逐字匹配）。"
                  "装它：pip install pypinyin（依赖清单已补，见 requirements-core.txt）",
                  flush=True)
        return [str(text or "").lower()]


def _syl_match(a, b):
    """音节模糊匹配：完全相等或编辑距离 <= 1（如 yuan≈yun、fang≈fan）。"""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = j = diff = 0
    while i < len(shorter) and j < len(longer):
        if shorter[i] != longer[j]:
            diff += 1
            if diff > 1:
                return False
            j += 1
        else:
            i += 1
            j += 1
    return diff + (len(longer) - j) <= 1


def _subseq(needle, haystack):
    """（已退役，保留给排障用）`needle` 是否为 `haystack` 的**子序列**（容忍插入）。

    ⚠️ **不要在新代码里用它做唤醒判据**（2026-10-08）：它容许"音节按顺序出现过"，
    中间可以夹任意无关音节 —— 实测这是误唤醒的主因。唤醒判据请用 `_contiguous()`。
    """
    i = 0
    for h in haystack:
        if i < len(needle) and _syl_match(h, needle[i]):
            i += 1
    return i == len(needle)


def _contiguous(needle, haystack):
    """`needle` 是否为 `haystack` 里**一段连续**的音节序列；命中时返回 `(起, 止)`。

    与 `_subseq()` 的唯一区别，也是关键区别：**音节之间不容许插入**。
      旧判据（子序列）：`回声回声` = hui sheng hui sheng，
        识别成 "hui … sheng … hui … sheng"（中间夹任意内容）**也算命中**
        —— 日常对话里 `会/回`、`生/声/省` 高频出现，于是极易误触发。
      新判据（连续）：这 4 个音节必须**挨着**出现，"开了hui议sheng产hui报sheng"这类不再命中。

    逐音节仍容许**一个**近音/同音位（`_syl_match`，如 yuan≈yun），因为识别器对
    同音字经常给错字面 —— 那是"同一句话的另一种写法"，不是"另一个词"。

    返回 `(起, 止)` 而不是布尔：调用方可以据此判定"命中的这段是否贴近句首/停顿"
    （见 `_StreamDetector.hit()` 的说明），这是进一步压误唤醒的抓手。
    """
    if not needle or not haystack or len(needle) > len(haystack):
        return None
    n = len(needle)
    for i in range(len(haystack) - n + 1):
        if all(_syl_match(haystack[i + k], needle[k]) for k in range(n)):
            return (i, i + n)
    return None


class _StreamDetector:
    """流式 ASR + 文字/拼音匹配唤醒检测。

    匹配策略（由严到松，命中即触发）：
      1) 精确文本匹配（含同音别名）；
      2) **连续**拼音音节匹配（`_contiguous`）—— 唤醒词的音节必须挨着出现。

    **2026-10-08 改（用户报"误识频率很高"）**：原来第 2 条用的是 `_subseq()`
    （子序列：音节按顺序出现过即可、中间容许任意插入）。唤醒词「回声回声」只有
    4 个音节、且 `会/回/汇`、`生/声/省/胜` 在日常对话里高频出现，于是
    "只要这段对话里先后出现过这几个音"就会唤醒 —— 这是误唤醒的主因。
    换成 `_contiguous()` 之后，那 4 个音节必须**连续**出现。

    ⚠️ **路过的坑（别再试）**：我一度想加"命中的片段必须贴近句首"这道闸门
    （利用 `_contiguous` 返回的区间）。它**在真实流式文本上不可靠**：识别结果是
    **累积**的，`lead` 常常是 0（起音被切出一个独立音节、或 reset 粒度比预想细），
    于是**正常喊唤醒词也会被挡掉** —— 拿"漏唤醒"换"少误唤醒"，方向错了。
    要再压误唤醒，正确做法是**多帧确认**（连续 X 帧命中），那是下一步的事
    （现有 `wakeConfirmX/N` 两个设置项**读了但从未使用**，见 `_run_impl`）。
    """

    def __init__(self, rec, keywords):
        self.rec = rec
        self.keywords = [str(k).strip() for k in keywords if str(k).strip()]
        self.kw_norm = [_norm(k) for k in self.keywords]
        self.kw_pinyin = [_pinyin(k) for k in self.keywords]
        self.stream = rec.create_stream()

    def feed(self, xf):
        self.stream.accept_waveform(SR, xf)
        while self.rec.is_ready(self.stream):
            self.rec.decode_stream(self.stream)

    def _reset(self):
        try:
            self.rec.reset(self.stream)
        except Exception:
            pass

    def reset(self):
        self._reset()

    def hit(self):
        try:
            text = _norm(self.rec.get_result(self.stream) or "")
        except Exception:
            text = ""
        if not text:
            return False
        for kw in self.kw_norm:
            if kw and kw in text:
                self._reset()
                return True
        # ---- 拼音这条路：**只在 pypinyin 可用时**才走 ----
        #  没有 pypinyin 时 `_pinyin()` 返回的是 `[整段小写文本]`（一个元素），
        #  拿它做音节匹配既无意义、又会让"连续匹配"**永远返回 None**
        #  （needle 好几个音节 > haystack 一个元素）→ **唤醒彻底失效**。
        #  所以退化时改走"逐字子串"：精度差，但**至少能唤醒**，而且状态是可见的
        #  （`pinyin_available()` + 那条一次性告警）。根治见 requirements-core.txt。
        if not pinyin_available():
            return False        # 逐字匹配已在上面 `kw in text` 做过，这里不重复
        rp = _pinyin(text)
        for kp in self.kw_pinyin:
            if not kp or len(kp) < 2:
                continue
            # 连续匹配即可（**不再要求贴句首**：那个代理判据在累积文本上会把正常
            # 唤醒也挡掉，见类注释里"路过的坑"）。
            if _contiguous(kp, rp) is not None:
                self._reset()
                return True
        return False


class _KwsDetector:
    """KWS（拼音关键词）唤醒检测。"""

    def __init__(self, kws, stream):
        self.kws = kws
        self.stream = stream

    def feed(self, xf):
        self.stream.accept_waveform(SR, xf)
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)

    def hit(self):
        r = self.kws.get_result(self.stream)
        if r != "":
            self.kws.reset_stream(self.stream)
            return True
        return False

    def reset(self):
        self.kws.reset_stream(self.stream)


class WakeListener(threading.Thread):
    """常驻唤醒监听线程。on_wake 在命中且通过确认/冷却后调用（后台线程）。"""

    def __init__(self, settings_get, on_wake=None, daemon=True):
        super().__init__(daemon=daemon)
        self.settings_get = settings_get      # settings.get 函数（实时读 DB 缓存）
        self.on_wake = on_wake or (lambda: None)
        # 注意：不要定义 _started/_stop 等与 threading.Thread 内部同名的属性！
        self._stop_flag = threading.Event()
        self.error = ""

    def shutdown(self):
        self._stop_flag.set()

    def _keywords(self):
        """KWS 用：配置中文唤醒词 -> sherpa KWS 拼音格式（声母+带调韵母）。

        '小尼小尼' -> 'x iǎo n í x iǎo n í @小尼小尼'
        """
        kws = self.settings_get("wakeKeywords", ["小尼小尼"]) or ["小尼小尼"]
        parts = []
        for kw in kws:
            kw = str(kw).strip()
            if not kw:
                continue
            parts.append(f"{_to_kws_pinyin(kw)} @{kw}")
        return parts

    def _make_detector(self):
        """按 `wakeEngine` 选唤醒实现（`sherpa` 流式 ASR 文字匹配 / `kws` 关键词 spotting）。

        2026-09-19 审计发现：`wakeEngine` 这个设置项**面板上能选、后端从没读过**
        （判定 PANEL-ONLY），而且选项里的 `openwakeword` 从来没有实现过。现在两者都修：
        选项改成真实存在的两种实现，后端按它选路；历史配置里存着 `openwakeword`
        之类的未知值时按默认（sherpa 优先、失败回退 KWS）走，不会因为一个配置值起不来。
        """
        mode = str(self.settings_get("wakeEngine", "sherpa") or "sherpa").strip().lower()
        if mode == "kws":
            print("[wake] wakeEngine=kws：只用关键词 spotting", flush=True)
            return self._make_kws_detector()
        if mode not in ("sherpa", "stream", "auto"):
            print(f"[wake] wakeEngine={mode!r} 不是已知实现，按默认 sherpa 处理", flush=True)
        # 默认：流式 ASR 文字匹配，模型缺失时回退 KWS
        try:
            return self._make_stream_detector()
        except Exception as e:
            print(f"[wake] 流式 ASR 不可用: {e}，回退 KWS", flush=True)
        return self._make_kws_detector()

    def _make_stream_detector(self):
        """流式 ASR（sherpa-onnx zipformer）：识别文字再与唤醒词匹配。"""
        kw_texts = self._kw_texts()
        from app.audio import stt
        rec = stt._get_sherpa()
        print(f"[wake] 流式 ASR 唤醒就绪，唤醒词={kw_texts}", flush=True)
        return _StreamDetector(rec, kw_texts)

    def _kw_texts(self):
        """唤醒词 + 别名（去空白、去空项）。"""
        kw_texts = [str(k).strip() for k in
                    (self.settings_get("wakeKeywords", ["小尼小尼"]) or ["小尼小尼"])]
        kw_texts += [str(k).strip() for k in
                     (self.settings_get("wakeAliases", []) or [])]
        kw_texts = [k for k in kw_texts if k]
        if not kw_texts:
            raise ValueError("唤醒词为空")
        return kw_texts

    def _make_kws_detector(self):
        """KWS（sherpa-onnx KeywordSpotter）：直接用唤醒词做关键词 spotting。"""
        import sherpa_onnx
        import tempfile as _tf
        kws_dir = kws_model_dir()
        enc = os.path.join(kws_dir, "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx")
        dec = os.path.join(kws_dir, "decoder-epoch-13-avg-2-chunk-8-left-64.onnx")
        joi = os.path.join(kws_dir, "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx")
        tok = os.path.join(kws_dir, "tokens.txt")
        if not all(os.path.isfile(p) for p in (enc, dec, joi, tok)):
            raise FileNotFoundError(f"KWS 模型未就绪: {kws_dir}")
        kw_lines = self._keywords()
        _fd, kwfile = _tf.mkstemp(suffix=".keywords.txt", text=True)
        try:
            with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                _f.write("\n".join(kw_lines) + "\n")
            kws = sherpa_onnx.KeywordSpotter(
                tokens=tok, encoder=enc, decoder=dec, joiner=joi,
                keywords_file=kwfile,
                num_threads=2, provider="cpu",
                keywords_score=1.0,
                keywords_threshold=float(self.settings_get("wakeThreshold", 0.5)),
            )
            stream = kws.create_stream()
        finally:
            try:
                os.remove(kwfile)
            except Exception:
                pass
        print("[wake] KWS 就绪，开始监听唤醒词", flush=True)
        return _KwsDetector(kws, stream)

    def run(self):
        try:
            self._run_impl()
        except Exception as e:
            import traceback
            self.error = traceback.format_exc()
            print(f"[wake] 唤醒监听崩溃: {e}", flush=True)

    def _run_impl(self):
        from app.audio.mic import input_stream, MicrophoneBusy
        import numpy as np

        detector = self._make_detector()

        from app.audio.recorder import resolve_input_device
        _dev = resolve_input_device("command")   # 唤醒属于"指令"侧，与指令同一个麦
        device = _dev if _dev >= 0 else None
        # device=None → sounddevice 自动使用系统默认输入设备（实测正确落到可用麦克风）。
        # 切勿用 sd.default.device[1] 硬取索引：本机该索引可能指向扬声器/失效设备导致打开失败。
        print(f"[wake] 输入设备: {device if device is not None else '系统默认'}", flush=True)

        cooldown = float(self.settings_get("wakeCooldownSec", 3))
        confirm_x = int(self.settings_get("wakeConfirmX", 2))
        confirm_n = int(self.settings_get("wakeConfirmN", 3))
        silence_floor = float(self.settings_get("wakeSilenceFloor", 60))
        recent = deque(maxlen=6)
        last_fire = 0.0
        paused = bool(self.settings_get("wakePaused", True))
        silent_run = 0

        while not self._stop_flag.is_set():
            # 实时读配置（暂停/确认帧变化即时生效）
            try:
                paused = bool(self.settings_get("wakePaused", paused))
                confirm_x = int(self.settings_get("wakeConfirmX", confirm_x))
                confirm_n = int(self.settings_get("wakeConfirmN", confirm_n))
            except Exception:
                pass
            if paused:
                time.sleep(0.5)
                continue
            try:
                fire = False
                #: 诊断（2026-10-09）：`ECHO_WAKE_DEBUG=1` 时每 ~2s 打一行"这一段的
                #: 帧数 / rms 分布 / 过门控帧数 / 识别文本"。**默认关闭** —— 为什么需要它：
                #: "麦打开了但永不命中"这个症状，代码里**每一步都没有日志**
                #: （门控丢帧不打、识别出的文本不打），于是只能靠猜。开着它就能一次看清
                #: 是"音频没进来"、"门控全丢"还是"进来但识别不出字"。
                _dbg = str(os.environ.get("ECHO_WAKE_DEBUG", "")).strip() not in ("", "0", "false")
                if _dbg:
                    print("[wake-dbg] 开始采集（每 2 秒一行统计）", flush=True)
                _dbg_n = _dbg_pass = 0
                _dbg_rms = 0.0
                _dbg_next = time.time() + 2.0
                with input_stream(device if device is not None else -1,
                                  blocksize=BLOCK, background=True) as mic:
                    print(f"[wake] 麦克风就绪 (device={mic.device})", flush=True)
                    while not self._stop_flag.is_set():
                        if self.settings_get("wakePaused", True):
                            break
                        x, _ = mic.read(BLOCK)
                        x = x.reshape(-1)
                        rms = float(np.sqrt((x.astype(np.float32) ** 2).mean()))
                        if _dbg:
                            _dbg_n += 1
                            _dbg_rms = max(_dbg_rms, rms)
                            if rms >= silence_floor:
                                _dbg_pass += 1
                        hit = False
                        if rms >= silence_floor:
                            silent_run = 0
                            xf = x.astype(np.float32) / 32768.0
                            detector.feed(xf)
                            hit = detector.hit()
                        else:
                            silent_run += 1
                            # 静音约 1.6s 后重置识别流，避免历史文本无限累积导致误触发
                            if silent_run > 20:
                                silent_run = 0
                                detector.reset()
                        if _dbg and time.time() >= _dbg_next:
                            try:
                                _txt = _norm(getattr(detector, "rec", None)
                                             and detector.rec.get_result(detector.stream) or "")
                            except Exception:
                                _txt = "（读不到）"
                            print("[wake-dbg] 帧=%d 过门控=%d(%.0f%%) 峰值rms=%.1f "
                                  "门限=%.0f 识别文本=%r"
                                  % (_dbg_n, _dbg_pass,
                                     100.0 * _dbg_pass / max(_dbg_n, 1),
                                     _dbg_rms, silence_floor, _txt), flush=True)
                            _dbg_n = _dbg_pass = 0
                            _dbg_rms = 0.0
                            _dbg_next = time.time() + 2.0
                        recent.append(hit)
                        if not hit:
                            continue
                        # 拼音匹配本身较精确，命中即触发（靠 cooldown 防重复），
                        # 不做多帧确认——流式检测器命中后会重置，单次命中无法凑够多帧。
                        now = time.time()
                        if now - last_fire < cooldown:
                            continue
                        last_fire = now
                        print("[wake] 唤醒词命中 -> 触发命令录音")
                        fire = True
                        break
                # Release the input stream before starting command capture.
                if fire and not self._stop_flag.is_set():
                    try:
                        self.on_wake()
                    except Exception as e:
                        print(f"[wake] 回调异常: {e}")
            except MicrophoneBusy:
                self._stop_flag.wait(0.2)
            except Exception as e:
                print(f"[wake] 麦克风监听中断，3 秒后重连: {e}")
                self._stop_flag.wait(3)


def _to_kws_pinyin(text):
    """中文文本 -> sherpa KWS 拼音（声母+带调韵母，空格分隔）。"""
    try:
        from pypinyin import Style, pinyin
        out = []
        for ch in text:
            if not ('\u4e00' <= ch <= '\u9fff'):
                continue
            init = pinyin(ch, style=Style.INITIALS)[0][0]
            fin = pinyin(ch, style=Style.FINALS_TONE)[0][0]
            out.append(f"{init} {fin}" if init else fin)
        return " ".join(out)
    except Exception as e:
        print(f"[wake] 拼音转换失败: {e}")
        return text


def run_once(seconds=30, callback=None):
    """调试/测试用：跑 seconds 秒。"""
    from app.config import settings
    wl = WakeListener(settings.get, on_wake=callback or (lambda: print("唤醒!")))
    wl.start()
    time.sleep(seconds)
    wl.shutdown()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30)
    args = ap.parse_args()
    run_once(args.seconds)
