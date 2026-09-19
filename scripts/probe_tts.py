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
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.GetInstalledVoices() | ForEach-Object { "
          "$_.VoiceInfo.Name + ' | ' + $_.VoiceInfo.Culture.Name + ' | enabled=' + $_.Enabled }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        for line in (r.stdout or "").splitlines():
            if line.strip():
                print("  %s" % line.strip())
        if not (r.stdout or "").strip():
            print("  (none reported) rc=%s err=%r" % (r.returncode, r.stderr[:200]))
    except Exception:
        traceback.print_exc()


def beeps():
    hr("2. the five beeps, ONE AT A TIME (this is the code path ECHO uses)")
    print("  each line below is played via app.audio.tts.play_beep() -> platform seam")
    try:
        from app.audio import tts
    except Exception:
        traceback.print_exc()
        return
    print("  BEEPS_DIR = %s (exists=%s)" % (tts.BEEPS_DIR, os.path.isdir(tts.BEEPS_DIR)))
    for name in BEEPS:
        print("  play_beep(%r) ... LISTEN" % name)
        try:
            tts.play_beep(name)
        except Exception:
            traceback.print_exc()
        time.sleep(2.0)          # 每声之间留 2 秒，够你分辨
    pause("did you hear FIVE beeps (start/done/ok/ok2/err)?")


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
