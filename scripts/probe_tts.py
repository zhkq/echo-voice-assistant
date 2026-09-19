# -*- coding: utf-8 -*-
"""probe_tts.py — ECHO 音频自检（**每一声单独播、单独确认**，最后自动汇总）

为什么要这么写（2026-09-19 实测反馈，两轮）
------------------------------------------
1.0 那份探针把 4b/4c/5/6 连着播 —— 用户："听到两个提示音，但分不清是哪两节"。
本探针第一版改成"每节按回车"，用户又反馈："热身两声后加个回车确认，后面每声后面都
单独加确认，不然不知道播的是哪个"。
所以现在：**先热身 2 声（不带提问）→ 回车 → 每一首都单独播、播完立刻问 y/n**，
最后打印一张结果表 —— 你只要把表贴回来即可，不用记。

另有两处补齐：
  * 1.0 那份第 6 节朗读的是**英文**，而 ECHO 的语音简报是**中文**；中文才是历史上
    真出过问题的路（SAPI InputEncoding：英文正常、中文乱码）。本探针两者都测。
  * 打印已安装 SAPI 音色：没有 zh-* 音色时"离线朗读"会拿英文音色念中文，这本身就是答案。

⚠️ 命令写法有讲究：这台机器上的管理员策略会拦掉**引号内含 `|` 字面量**的 powershell
命令行（WinError 786 "restricted by policy rule"）。所以下面一律用"多语句分别输出、
由 Python 排版"的写法（ECHO 生产代码本身不含这种写法，实测通过）。

怎么跑：双击同目录的 `probe-tts.bat`。输出刻意保持**纯 ASCII**。
"""
import os
import subprocess
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ZH_TEXT = "回声音频自检，中文朗读第二句，一二三四五。"
EN_TEXT = "ECHO audio check, English line."
BEEPS = ("start", "done", "ok", "ok2", "err")

#: 结果汇总：(标签, 是否听到)。最后统一打印，方便整段贴回。
RESULTS = []


class _Tee:
    """把 stdout 同时写到日志文件 —— 结果不用手抄，直接读文件。

    起因：用户跑完探针后要"贴回结果"，窗口一关就没了。落盘之后任何人（包括自动化）
    都能直接读 `data/logs/probe-tts-last.txt`，不必依赖人工复制。
    """

    def __init__(self, path):
        self.path = path
        self.fh = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.fh = open(path, "w", encoding="utf-8")
        except Exception:
            self.fh = None

    def write(self, text):
        try:
            sys.__stdout__.write(text)
        except Exception:
            pass
        if self.fh:
            try:
                self.fh.write(text)
                self.fh.flush()
            except Exception:
                pass
        return len(text)

    def flush(self):
        for stream in (sys.__stdout__, self.fh):
            try:
                if stream:
                    stream.flush()
            except Exception:
                pass

    def close(self):
        try:
            if self.fh:
                self.fh.close()
        except Exception:
            pass


def hr(title):
    print("")
    print("=" * 70)
    print("== %s" % title)
    print("=" * 70)


def pause(msg="press ENTER for the next section..."):
    try:
        input("  --> %s " % msg)
    except EOFError:
        time.sleep(1.0)


def ask_heard(label):
    """问"这一声听到了吗"，返回 True/False（默认没听到，回车即否）。"""
    try:
        ans = input("      did you HEAR [%s]?  y = yes / ENTER = no : " % label)
    except EOFError:
        ans = ""
    heard = str(ans).strip().lower() in ("y", "yes", "1")
    RESULTS.append((label, heard))
    print("      recorded: %s = %s" % (label, "HEARD" if heard else "NOT heard"))
    return heard


def show_settings():
    hr("0. ECHO switches (was ECHO even asked to make a sound?)")
    import sqlite3
    db_path = os.path.join(ROOT, "data", "echo.db")
    print("  db: %s (exists=%s)" % (db_path, os.path.isfile(db_path)))
    keys = ("beepOnStart", "beepOnDone", "beepOnSend", "voiceBrief", "voiceConfirm",
            "ttsEngine", "maxBriefChars")
    try:
        import json
        conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        for k in keys:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            print("  %-14s = %r" % (k, json.loads(row[0]) if row else None))
        conn.close()
    except Exception:
        traceback.print_exc()


def show_voices():
    hr("1. installed SAPI voices (no zh-* voice = offline TTS reads Chinese wrong)")
    # 见文件头：引号内含 "|" 的命令行会被管理员策略拦，所以这里分语句输出。
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name; "
          "$_.VoiceInfo.Culture.Name; $_.Enabled }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        if not lines:
            print("  (none reported) rc=%s err=%r" % (r.returncode, (r.stderr or "")[:200]))
            return
        for i in range(0, len(lines), 3):
            name, culture, enabled = (lines[i:i + 3] + ["?", "?"])[:3]
            zh = "  <== CHINESE" if str(culture).lower().startswith("zh") else ""
            print("  voice = %-34s culture = %-6s enabled = %-5s%s"
                  % (name, culture, enabled, zh))
        if not any(l.lower().startswith("zh") for l in lines):
            print("  !! no zh-* voice: offline TTS will read Chinese with a wrong voice")
    except Exception:
        traceback.print_exc()


def show_beep_files():
    """提示音文件本身：字节数 / 时长 / 峰值 / RMS(dBFS) / 哈希。

    为什么要有这一节：五个提示音里用户只听到后三声 —— 而 winsound 的失败是**全静默**的。
    先量文件：峰值≈0 或 RMS 极低 = 文件哑了（重新生成即可）；文件正常却听不到 =
    播放时机/设备问题。两种修法完全不同，别靠猜。
    （实测结论：五个文件都正常，且 start.wav 与 ok.wav 是同一段波形。）
    """
    hr("2a. beep files themselves (silent file vs silent playback)")
    import wave
    beeps = os.path.join(ROOT, "assets", "beeps")
    if not os.path.isdir(beeps):
        print("  MISSING dir: %s" % beeps)
        return
    try:
        import hashlib
        import numpy as np
        import soundfile as sf
    except Exception:
        traceback.print_exc()
        return
    print("  %-6s %8s %8s %7s %8s %8s  %s"
          % ("name", "bytes", "frames", "sec", "peak", "rms_dB", "sha256:12"))
    for name in BEEPS:
        path = os.path.join(beeps, name + ".wav")
        if not os.path.isfile(path):
            print("  %-6s MISSING" % name)
            continue
        try:
            data, sr = sf.read(path, dtype="float32")
            peak = float(np.max(np.abs(data))) if len(data) else 0.0
            rms = float(np.sqrt(np.mean(data * data))) if len(data) else 0.0
            db = 20.0 * np.log10(rms) if rms > 1e-9 else -999.0
            digest = hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]
            with wave.open(path, "rb") as w:
                ch, width = w.getnchannels(), w.getsampwidth()
            flag = "  <== SILENT!" if peak < 0.01 else ""
            print("  %-6s %8d %8d %7.3f %8.4f %8.1f  %s  %dch/%dbit%s"
                  % (name, os.path.getsize(path), len(data), len(data) / float(sr),
                     peak, db, digest, ch, width * 8, flag))
        except Exception:
            print("  %-6s UNREADABLE" % name)
            traceback.print_exc()


def _beep_sequence(play, ask, warmup=2, gap=2.0):
    """逐声播 + 逐声确认（逻辑独立出来，便于不开声地自测）。

    play(name) 播放；ask(label) 返回 bool。返回 [(name, heard), ...]。
    """
    if warmup:
        print("  [warm-up] playing 'ok' x%d WITHOUT questions (wakes the output device)" % warmup)
        for _ in range(warmup):
            play("ok")
            time.sleep(1.5)
        time.sleep(1.0)
        # 纯回车确认（不计入结果）：用户要求"热身两声后加个回车确认"
        pause("warm-up done. Press ENTER to start the real sequence (5 beeps, one by one)")
    out = []
    for i, name in enumerate(BEEPS, 1):
        print("")
        print("  >>> now playing #%d of %d: %s" % (i, len(BEEPS), name))
        play(name)
        time.sleep(gap)
        out.append((name, ask(name)))
    return out


def beeps():
    hr("2b. the five beeps: ONE AT A TIME, each with its own y/n confirmation")
    print("  order: %s" % " -> ".join(BEEPS))
    print("  rule : answer y only if you heard THAT one (ENTER = not heard)")
    try:
        from app.audio import tts
    except Exception:
        traceback.print_exc()
        return
    print("  BEEPS_DIR = %s (exists=%s)" % (tts.BEEPS_DIR, os.path.isdir(tts.BEEPS_DIR)))
    res = _beep_sequence(lambda n: tts.play_beep(n), ask_heard)
    heard = [n for n, ok in res if ok]
    missed = [n for n, ok in res if not ok]
    print("")
    print("  beep result: heard=%s missed=%s" % (heard or "none", missed or "none"))
    if missed and len(missed) < len(BEEPS):
        print("  -> PARTIAL: some play, some do not (timing/device, not files)")
    elif missed:
        print("  -> NONE heard: playback path or device is dead")
    else:
        print("  -> ALL heard: beep path is fine")


def speak_section(title, text, engine, note=""):
    hr(title)
    if note:
        print("  %s" % note)
    print("  text to speak (engine=%s): %s" % (engine, text))
    try:
        from app.audio import tts
    except Exception:
        traceback.print_exc()
        return
    t0 = time.time()
    try:
        ok = tts.speak(text, engine)
        print("  speak() -> %r   (%.1fs)" % (ok, time.time() - t0))
    except Exception:
        traceback.print_exc()
    ask_heard("%s (%s)" % (title.split(".", 1)[-1].strip(), engine))


def repeat_one_beep(name, times=3, gap=3.0):
    """单曲重复 N 次：用来把"某个提示音偶尔被吞"定性。

    为什么要重复：`done.wav` 只有 90 ms，一次没听到既可能是"用户没注意"，
    也可能是"这条通路就是吞短的"。重复 3 次、每次单独问 y/n，就能分开：
    * 3/3 都没听到 -> 确定性失败（改文件：给短提示音前置静音 / 加长）
    * 1~2 次听到   -> 偶发（设备/时序），修法偏向"预热"
    """
    hr("R. repeat one beep: %s x%d (gap %.0fs, one y/n per play)" % (name, times, gap))
    try:
        from app.audio import tts
    except Exception:
        traceback.print_exc()
        return
    print("  warm-up: play 'ok' twice without questions")
    for _ in range(2):
        tts.play_beep("ok")
        time.sleep(1.5)
    time.sleep(1.0)
    pause("warm-up done. Press ENTER to start")
    for i in range(1, times + 1):
        print("")
        print("  >>> %s  play #%d of %d" % (name, i, times))
        played = tts.play_beep(name)
        print("      play_beep() returned %r  (False = 根本没播出去)" % played)
        time.sleep(gap)
        ask_heard("%s #%d" % (name, i))
    heard = sum(1 for label, ok in RESULTS if label.startswith(name) and ok)
    print("")
    print("  result: %d/%d heard" % (heard, times))


def main():
    # 单曲重复模式：probe-tts.bat --beep done --times 3
    argv = sys.argv[1:]
    if "--beep" in argv:
        name = argv[argv.index("--beep") + 1] if len(argv) > argv.index("--beep") + 1 else "done"
        times = 3
        if "--times" in argv:
            try:
                times = int(argv[argv.index("--times") + 1])
            except Exception:
                times = 3
        log_path = os.path.join(ROOT, "data", "logs", "probe-tts-last.txt")
        tee = _Tee(log_path)
        if tee.fh:
            sys.stdout = tee
        print("ECHO audio probe - repeat mode (dev tree)")
        print("root   : %s" % ROOT)
        print("log    : %s" % log_path)
        repeat_one_beep(name, times)
        hr("SUMMARY (paste this back)")
        for label, heard in RESULTS:
            print("  %-24s %s" % (label, "HEARD" if heard else "NOT heard"))
        print("")
        print("DONE-PROBE-TTS")
        return

    log_path = os.path.join(ROOT, "data", "logs", "probe-tts-last.txt")
    tee = _Tee(log_path)
    if tee.fh:
        sys.stdout = tee
    print("ECHO audio probe (dev tree)")
    print("root   : %s" % ROOT)
    print("python : %s" % sys.executable)
    print("log    : %s%s" % (log_path, "" if tee.fh else "  (could not open - not logged!)"))
    print("")
    print("This probe plays ONE sound at a time and asks y/n after EACH one,")
    print("then prints a summary table at the end. Everything is also written to")
    print("the log file above, so the results survive closing this window.")

    show_settings()
    pause()

    show_voices()
    pause()

    show_beep_files()
    pause()

    beeps()

    speak_section("3. CHINESE via sapi (offline; the fallback ECHO briefs use)",
                  ZH_TEXT, "sapi",
                  "expect: natural Mandarin. 'English accent reading Chinese' = wrong voice.")

    speak_section("4. CHINESE via edge-tts (online; the preferred engine)",
                  ZH_TEXT, "edge-tts",
                  "expect: natural Mandarin. Silence = network/edge-tts problem.")

    hr("5. online probe + CHINESE via auto (what ECHO actually uses)")
    try:
        from app.audio import tts
        print("  probe_online(force=True) = %r" % (tts.probe_online(force=True),))
        print("  tts_online_status()      = %r" % (tts.tts_online_status(),))
        print("  text to speak: %s" % ZH_TEXT)
        t0 = time.time()
        ok = tts.speak(ZH_TEXT, "auto")
        print("  speak(engine=auto) -> %r   (%.1fs)" % (ok, time.time() - t0))
    except Exception:
        traceback.print_exc()
    ask_heard("CHINESE via auto")

    speak_section("6. control: ENGLISH via sapi", EN_TEXT, "sapi")

    hr("SUMMARY (paste this back)")
    for label, heard in RESULTS:
        print("  %-46s %s" % (label, "HEARD" if heard else "NOT heard"))
    print("")
    print("log file: %s" % log_path)

    hr("how to read the result")
    print("  * beeps: ALL heard            -> beep path fine (earlier 'no beeps' = environment)")
    print("  * beeps: first ones missed    -> output device wake-up; fix = warm up / lead-in silence")
    print("  * section 3 garbled/wrong tone-> SAPI voice/encoding (zh voice list is in section 1)")
    print("  * section 4 silent            -> edge-tts/network (see probe_online in section 5)")
    print("  * section 5 silent but 3 ok   -> the auto path's engine choice is the problem")
    print("")
    print("DONE-PROBE-TTS")


if __name__ == "__main__":
    main()
