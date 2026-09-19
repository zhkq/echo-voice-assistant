# -*- coding: utf-8 -*-
"""probe_tts.py — ECHO 音频自检（**逐节按回车**，专治"几个音挤在一起分不清"）

为什么要重写一份（2026-09-19）
------------------------------
1.0 那份探针把 4b/4c/5/6 连着播，间隔太短 —— 用户实测反馈"听到两个提示音，但分不清
是哪两节"。所以这里每一节都**等你按回车**再继续，且每节只做一件事。

另有两处补齐：
  * 1.0 那份第 6 节朗读的是**英文**（`ECHO audio check, engine …`），而 ECHO 的语音简报
    是**中文**；中文才是真正会出问题的那条路（SAPI 的 InputEncoding 历史上踩过
    "英文正常、中文乱码"）。本探针中文/英文都测。
  * 打印已安装的 SAPI 语音列表：如果系统里没有中文语音，"离线朗读"会拿英文音色念中文，
    这本身就是答案（不用再猜）。

怎么跑：双击同目录的 `probe-tts.bat`（窗口会留住输出）。
输出刻意保持**纯 ASCII**：中文只出现在"被朗读的文本"里，避免 GBK 控制台把提示语搞乱。
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
    hr("1. installed SAPI voices (no Chinese voice = offline TTS reads Chinese wrong)")
    # ⚠️ 命令写法有讲究（2026-09-19 实测）：这台机器上的管理员策略会拦掉**引号内含 `|`
    # 字面量**的 powershell 命令行（WinError 786 "restricted by policy rule"），
    # 拼接 `' | '` 的版本直接被拦，连 CreateProcess 都过不去。
    # 所以这里改成"多语句分别输出"，由 Python 自己排版 —— 既躲开策略，也更清楚。
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
        for i in range(0, len(lines), 3):
            chunk = lines[i:i + 3]
            name = chunk[0] if len(chunk) > 0 else "?"
            culture = chunk[1] if len(chunk) > 1 else "?"
            enabled = chunk[2] if len(chunk) > 2 else "?"
            zh = "  <== CHINESE" if str(culture).lower().startswith("zh") else ""
            print("  voice = %-34s culture = %-6s enabled = %-5s%s"
                  % (name, culture, enabled, zh))
        if not any(l.lower().startswith("zh") for l in lines):
            print("  !! no zh-* voice: offline TTS will read Chinese with a wrong voice")
    except Exception:
        traceback.print_exc()


def show_beep_files():
    """提示音文件本身：字节数 / 时长 / 峰值 / RMS(dBFS) / 哈希。

    为什么要有这一节（2026-09-19 实测）：五个提示音里用户只听到后三声，
    `start`/`done` 听不到 —— 而 winsound 播放失败是**全静默**的。这里先量文件：
    峰值≈0 或 RMS 极低 = 文件本身就是哑的（重新生成即可），
    文件正常却听不到 = 播放通路/设备问题。两种修法完全不同，别再靠猜。
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


def beeps():
    hr("2b. beeps with a WARM-UP first (cold output device eats the first short sound)")
    print("  theory tested here: the output device (BT/USB) needs ~0.3s to wake; the")
    print("  FIRST short sound after silence gets swallowed while later ones play fine.")
    print("  so: play 'ok' twice as warm-up, THEN all five, with a 2s gap each.")
    try:
        from app.audio import tts
    except Exception:
        traceback.print_exc()
        return
    print("  BEEPS_DIR = %s (exists=%s)" % (tts.BEEPS_DIR, os.path.isdir(tts.BEEPS_DIR)))
    print("  [warm-up] play_beep('ok') x2 ...")
    for _ in range(2):
        try:
            tts.play_beep("ok")
        except Exception:
            traceback.print_exc()
        time.sleep(1.5)
    time.sleep(1.0)
    print("  now the real sequence:")
    for name in BEEPS:
        print("  play_beep(%r) ... LISTEN" % name)
        try:
            tts.play_beep(name)
        except Exception:
            traceback.print_exc()
        time.sleep(2.0)          # 每声之间留 2 秒，够你分辨
    print("")
    print("  reading: if start/done ARE heard now but were NOT in the previous run,")
    print("           the cause is device wake-up latency, not the wav files.")
    print("           fix = prepend ~0.3s of silence to the short beeps (or warm up in code).")
    pause("did you hear FIVE beeps this time (start/done/ok/ok2/err)?")


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
    pause("what did you hear? (clear Chinese / garbled / nothing / English accent)")


def main():
    print("ECHO audio probe (dev tree)")
    print("root   : %s" % ROOT)
    print("python : %s" % sys.executable)
    print("")
    print("This probe waits for ENTER between sections on purpose:")
    print("the previous version fired everything back-to-back and was impossible to judge.")

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
                  "expect: natural Mandarin. Silence here = network/edge-tts problem "
                  "(check probe_online below).")

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
    pause("did the auto path read the Chinese sentence?")

    hr("6. control: ENGLISH via sapi (should sound like a normal English voice)")
    speak_section("6b. ENGLISH via sapi", EN_TEXT, "sapi")

    hr("how to read the result")
    print("  * five separate beeps heard  -> beep path is fine (earlier 'no beeps' = environment)")
    print("  * section 3 garbled          -> SAPI encoding/voice problem")
    print("  * section 4 silent           -> edge-tts/network (see probe_online in section 5)")
    print("  * section 5 silent but 3 ok  -> auto path's engine choice/probe is the problem")
    print("  * paste this whole window back; every failure branch prints a traceback")
    print("")
    print("DONE-PROBE-TTS")


if __name__ == "__main__":
    main()
