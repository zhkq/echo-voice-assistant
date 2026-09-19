# -*- coding: utf-8 -*-
"""mic_beep_check.py — 用麦克风客观判定"扬声器到底有没有发出这一声"。

为什么需要它（2026-09-19）：`done.wav` 三条播放通路（winsound / sounddevice / 垫静音）
用户都说听不到，而这两条通路播 start/ok/ok2/err 都能听到；文件频谱与电平又都正常。
所以必须绕开"人耳"这道主观环节：**播的时候用麦克风录**，比较各提示音在录音里的能量。

判据：
  * start/ok/err 录到明显能量，done 没有 -> 扬声器确实没发出 done（文件/硬件交互问题）
  * 五个都录到相当的能量        -> 声音发出去了，问题在听觉/路由（耳机与扬声器不同路由）

注意：麦克风离扬声器有距离、还有环境噪声与可能的 AGC，所以**只看相对值**，
并且与"静音基线"对比。每个音播两遍取较大值，减少随机性。
"""
import os
import sys
import time

import numpy as np
import sounddevice as sd

ROOT = r"C:\echo-dev"
sys.path.insert(0, ROOT)

MIC_CANDIDATES = [1, 9, 5]          # MME 麦克风阵列 / WASAPI 麦克风 / DirectSound


def record(dev, play, dur=0.8, sr=44100):
    """录 dur 秒（期间 play()），返回 (peak, rms)。"""
    frames = []
    with sd.InputStream(samplerate=sr, channels=1, device=dev, dtype="float32") as st:
        time.sleep(0.15)
        play()
        t0 = time.time()
        while time.time() - t0 < dur:
            data, _ = st.read(int(sr * 0.05))
            arr = np.asarray(data).reshape(-1)
            frames.append(arr)
    x = np.concatenate(frames) if frames else np.zeros(1, dtype="float32")
    return float(np.max(np.abs(x))), float(np.sqrt(np.mean(x * x)))


def main():
    from app.audio import tts

    print("mic beep check (objective): does the speaker actually emit each beep?")
    dev = None
    for cand in MIC_CANDIDATES:
        try:
            with sd.InputStream(samplerate=44100, channels=1, device=cand, dtype="float32"):
                dev = cand
                break
        except Exception as e:
            print("  mic %d unusable: %s" % (cand, str(e)[:60]))
    if dev is None:
        print("no usable microphone - cannot measure")
        return
    print("mic device %d: %s" % (dev, sd.query_devices(dev)["name"][:40]))

    base_p, base_r = record(dev, lambda: None)
    print("  baseline (silence)   peak=%.5f rms=%.6f" % (base_p, base_r))
    results = {}
    for name in ("start", "done", "ok", "ok2", "err"):
        best = (0.0, 0.0)
        for _ in range(2):                       # 每个音两遍取较大值
            p, r = record(dev, lambda n=name: tts.play_beep(n))
            if p > best[0]:
                best = (p, r)
            time.sleep(0.3)
        results[name] = best
        ratio = best[0] / max(base_p, 1e-6)
        flag = "  <== NOTHING ABOVE BASELINE" if best[0] < base_p * 1.5 else ""
        print("  play_beep(%-5s)     peak=%.5f rms=%.6f  (x%.1f baseline)%s"
              % (name, best[0], best[1], ratio, flag))
    print("")
    print("done vs start: peak ratio = %.2f"
          % (results["done"][0] / max(results["start"][0], 1e-9)))
    print("DONE-MIC-CHECK")


if __name__ == "__main__":
    main()
