# -*- coding: utf-8 -*-
"""make_beeps.py — 生成/校验 assets/beeps/*.wav（提示音资产的可复现来源）

为什么要有这个脚本（2026-09-19）
--------------------------------
实测发现 `done.wav`（700 Hz / 90 ms）在用户机器上**三通路都听不到**，而 120 ms 以上的
`start/ok/ok2/err` 都听得到。用户拍板："换个提示音吧，时间更长一点"。
与其手工改一个 wav，不如把"提示音长什么样"写成参数表 —— 以后调时长/音高就是改一个
数字后重跑，而不是再猜。

参数表只对 `done` 是**权威**（它由本脚本生成）；其余四个是 1.x 时代留下的资产，
放在表里是为了"要重生成时有个同族参考"，默认**不覆盖**它们（它们的波形是用户听惯的）。
用 `--all` 才会全部重写（会改变那四个的声音，谨慎）。

用法：
    python scripts/make_beeps.py                 # 只看参数表与现有文件统计
    python scripts/make_beeps.py --write done    # 重生成 done.wav（推荐的改法）
    python scripts/make_beeps.py --write all     # 五个全重写（会改变 start/ok/ok2/err）
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEEPS_DIR = os.path.join(ROOT, "assets", "beeps")
SR = 44100

#: name -> 参数。tones = [(频率Hz, 时长s), ...]；gap = 音之间静音；peak = 峰值（0~1）
#: fade = 每段首尾淡入淡出（秒，防爆音）
#:
#: 2026-09-19 统一加长（用户："换个提示音吧，时间更长一点"）：
#: 原时长 90/120/120/180/250 ms，实测**最短的那个（done 90ms）三条通路全听不到**；
#: 现在全部 ≥ 0.22s、峰值统一 0.16。**音高保持不变**，所以"哪个音是什么事"的
#: 肌肉记忆不用重建（done 例外：原来是 700Hz/90ms，改成 880→660Hz 的下行两音）。
SPECS = {
    "start": dict(tones=[(1100, 0.220)], gap=0.0, peak=0.160, fade=0.008),
    "done":  dict(tones=[(880, 0.130), (660, 0.130)], gap=0.030, peak=0.160, fade=0.008),
    "ok":    dict(tones=[(1200, 0.220)], gap=0.0, peak=0.160, fade=0.008),
    "ok2":   dict(tones=[(1600, 0.220)], gap=0.0, peak=0.160, fade=0.008),
    "err":   dict(tones=[(300, 0.300)], gap=0.0, peak=0.160, fade=0.008),
}

#: 由本脚本生成的提示音（权威）。2026-09-19 起是**全部五个** —— 在此之前只有 done 是
#: 生成的，其余是 1.x 的手工资产；现在它们由参数表决定，旧波形可从 git 历史取回
#: （`git checkout <commit> -- assets/beeps/start.wav`）。
GENERATED = tuple(SPECS)


def synth(spec):
    import numpy as np
    chunks = []
    for i, (freq, dur) in enumerate(spec["tones"]):
        n = int(SR * dur)
        t = np.arange(n) / float(SR)
        tone = np.sin(2.0 * np.pi * freq * t)
        f = max(int(SR * spec.get("fade", 0.0)), 1)
        if n > 2 * f:
            env = np.ones(n)
            ramp = np.linspace(0.0, 1.0, f)
            env[:f] = ramp
            env[-f:] = ramp[::-1]
            tone = tone * env
        chunks.append(tone)
        if spec.get("gap") and i < len(spec["tones"]) - 1:
            chunks.append(np.zeros(int(SR * spec["gap"])))
    x = np.concatenate(chunks).astype("float32")
    x *= spec["peak"] / max(float(abs(x).max()), 1e-9)
    return x


def stats(path):
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(path, dtype="float32")
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
    return len(x) / float(sr), peak, (20.0 * np.log10(rms) if rms > 1e-9 else -999.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description="ECHO 提示音生成/校验")
    ap.add_argument("--write", metavar="NAME|all", default="",
                    help="写入哪个提示音（默认只打印统计，不写）")
    args = ap.parse_args(argv)

    if args.write:
        import soundfile as sf
        names = list(SPECS) if args.write == "all" else [args.write]
        for name in names:
            spec = SPECS.get(name)
            if not spec:
                print("unknown beep: %s (known: %s)" % (name, ", ".join(SPECS)))
                return 2
            path = os.path.join(BEEPS_DIR, name + ".wav")
            data = synth(spec)
            sf.write(path, data, SR, subtype="PCM_16")
            dur, peak, db = stats(path)
            print("wrote %-6s %5.3fs peak=%.3f rms=%.1fdB  tones=%s"
                  % (name, dur, peak, db,
                     "+".join("%gHz/%.0fms" % (f, d * 1000) for f, d in spec["tones"])))

    print("")
    print("%-6s %8s %7s %7s  %s" % ("name", "sec", "peak", "rms_dB", "source"))
    for name, spec in SPECS.items():
        path = os.path.join(BEEPS_DIR, name + ".wav")
        if not os.path.isfile(path):
            print("%-6s MISSING" % name)
            continue
        dur, peak, db = stats(path)
        print("%-6s %8.3f %7.3f %7.1f  %s"
              % (name, dur, peak, db, "generated" if name in GENERATED else "legacy"))
    print("")
    print("提示音最短时长 = %.3fs（实测 <120ms 会被老通路吞掉，见 docs/2.0-PROGRESS §34）"
          % min(stats(os.path.join(BEEPS_DIR, n + ".wav"))[0]
                for n in SPECS if os.path.isfile(os.path.join(BEEPS_DIR, n + ".wav"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
