# -*- coding: utf-8 -*-
"""loopback_beep.py — 客观判定：提示音到底有没有从输出设备发出来。

⚠️ 本机两种回环**都不可用**（2026-09-19 实测，勿当可用工具）：
  * WDM-KS `扬声器 (Realtek HD Audio output with SST)`（索引 12/16/18）：
    PortAudio 打不开，`PaErrorCode -9999`（WDM-KS 是独占访问）；
  * WASAPI loopback：本机 sounddevice 的 `WasapiSettings` **没有 loopback 参数**
    （`got an unexpected keyword argument 'loopback'`），需要更新的 PortAudio。
留在这里是为了将来升级 sounddevice/PortAudio 后能直接重试；当前替代手段是
`probe_tts.py --ab <name>`：同一个文件走三条播放通路（接缝/sounddevice/垫静音），
由人耳做 A/B/C 对照。

原设计思路（若回环可用）：打开 Realtek 的 WDM-KS 回环输入（把扬声器输出当录音源），
录底噪 → 用**和产品完全相同的通路**播一个提示音 → 量录到的峰值/RMS，start 作对照。
"""
import os
import sys
import time

import numpy as np
import sounddevice as sd

sys.path.insert(0, r"C:\echo-dev")
ROOT = r"C:\echo-dev"
BEEPS = os.path.join(ROOT, "assets", "beeps")

LOOPBACK_CANDIDATES = [12, 16, 18]      # WDM-KS: 扬声器 output with SST / Stereo input


def measure(dev, play, dur=0.9, sr=44100, extra=None):
    """录 dur 秒，期间调用 play()；返回 (peak, rms)。"""
    frames = []
    with sd.InputStream(samplerate=sr, channels=1, device=dev, dtype="float32",
                        extra_settings=extra) as st:
        time.sleep(0.15)                      # 让流稳定
        play()
        t0 = time.time()
        while time.time() - t0 < dur:
            data, _ = st.read(int(sr * 0.05))
            frames.append(np.asarray(data).reshape(-1, 1) if np.asarray(data).ndim == 1
                          else data.copy())
    x = np.concatenate(frames) if frames else np.zeros(1, dtype="float32")
    return float(np.max(np.abs(x))), float(np.sqrt(np.mean(x * x)))


def silent():
    pass


def main():
    print("loopback beep measurement")
    try:
        from app.audio import tts
    except Exception as e:
        print("import tts failed:", e)
        return

    # WASAPI loopback：把**输出设备**当回环输入打开（PortAudio 支持
    # extra_settings=WasapiSettings(loopback=True)）。WDM-KS 那两条回环开不了
    # （PaErrorCode -9999，WDM-KS 是独占访问）。
    targets = []
    for i, d in enumerate(sd.query_devices()):
        api = sd.query_hostapis(d["hostapi"])["name"]
        if d["max_output_channels"] > 0 and "WASAPI" in api:
            targets.append(i)
    print("WASAPI output devices:", [(i, sd.query_devices(i)["name"][:38]) for i in targets])
    for dev in targets:
        info = sd.query_devices(dev)
        sr = int(info["default_samplerate"])
        try:
            extra = sd.WasapiSettings(loopback=True)
        except Exception as e:
            print("  dev %d: no WasapiSettings (%s)" % (dev, e))
            continue
        try:
            with sd.InputStream(samplerate=sr, channels=min(2, info["max_output_channels"]),
                                device=dev, dtype="float32", extra_settings=extra):
                pass
        except Exception as e:
            print("  dev %d: cannot open loopback (%s)" % (dev, str(e)[:70]))
            continue
        print("")
        print("=== WASAPI loopback dev %d: %s (sr=%d) ===" % (dev, info["name"][:38], sr))
        p, r = measure(dev, silent, sr=sr, extra=extra)
        print("  baseline (silence)   peak=%.5f rms=%.6f" % (p, r))
        for name in ("start", "done", "ok", "err"):
            p, r = measure(dev, lambda n=name: tts.play_beep(n), sr=sr, extra=extra)
            flag = "  <== NOTHING ON THE WIRE" if p < 0.005 else ""
            print("  play_beep(%-5s)     peak=%.5f rms=%.6f%s" % (name, p, r, flag))
            time.sleep(0.4)
    print("")
    print("DONE-LOOPBACK")


if __name__ == "__main__":
    main()
