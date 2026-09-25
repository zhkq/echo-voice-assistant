#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build-meeting-fixture.py — 打「真实会议测试夹具」包到 dist/

为什么要有这个脚本
------------------
与 `scripts/build_kit.py` 同一个理由（AGENTS.md 记着的那次事故）：手工组包必然漂移。
这个包的载荷里既有**真实音频**（54 MB，不能进 git），又有**要在别人机器上跑的脚本**
（`.ps1` 带 BOM 的编码纪律）。两件事都必须由脚本盯着，而不是靠人记步骤。

一把打完：
    python scripts/build-meeting-fixture.py
    → dist\\ECHO-meeting-fixture-<stamp>.zip

它做四件事：
  1. **自检交付源**（`delivery\\ECHO-meeting-fixture\\`）：
     * `导入.ps1` 必须 UTF-8 **带 BOM**（否则 WinPS 5.1 按 ANSI 解析 → 中文乱码/静默解析失败）；
     * `导入.ps1` 里嵌入的 Python 助手必须**纯 ASCII** 且能 `compile()` 通过；
     * `先读我.md` 必须是合法 UTF-8。
  2. **自检数据源**（`data\\meetings\\<会议>\\`）：wav 段名必须 `^\\d+\\.wav$`（会议链路的约定），
     `meta.json` 能解析且带 `durationSeconds`。
  3. **组包**：zip 条目**一律带顶层前缀** `ECHO-meeting-fixture/`
     （kit 那次踩坑：没有顶层目录，解出来是一堆散文件，而"把这个文件夹交给助手"的说明就落空了）。
     wav 用 STORED（PCM 压不动，省时间也省得白折腾），文本用 DEFLATED。
  4. **回读校验**：重新打开 zip 核对条目、体积、以及包内 `导入.ps1` 的 BOM 还在。

用法
----
    python scripts/build-meeting-fixture.py [--meeting 2026-09-20_10-48-26]
                                           [--out dist\\xxx.zip] [--stamp YYYYMMDD-HHMM]
                                           [--no-reference]
    python scripts/build-meeting-fixture.py --check      # 只报告 dist 里的包是不是过期
                                                        # 退出码 0=一致 2=过期 3=还没出过包

纪律：`app/` 只读 —— 本脚本不写任何源码，只读 delivery\\ 与 data\\meetings\\，只写 dist\\。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DELIVERY = os.path.join(ROOT, "delivery", "ECHO-meeting-fixture")
DIST = os.path.join(ROOT, "dist")
DEFAULT_MEETING = "2026-09-20_10-48-26"
TOP = "ECHO-meeting-fixture"           # zip 里的顶层目录名（也是交付给人的那层）
PS1 = "导入.ps1"
README = "先读我.md"
MANIFEST = "包内清单.txt"
REF_DIR = "参考-旧转写结果"
REF_FILES = ("transcript.md", "summary.md", "topics.md", "meeting_note.md")
WAV_RE = re.compile(r"^\d+\.wav$", re.I)
BOM = b"\xef\xbb\xbf"

REF_NOTE = """这份目录是「参考-旧转写结果」：这场会议在**源机器**上跑过一次的转写与纪要。

它**不会**被 导入.ps1 放进会议目录 —— 导入只放 3 个 wav + meta.json，
好让你在目标机上跑的是**真正的一次重新转写**（而不是看到别人的旧结果）。

用途只有一个：事后对比。跑完之后可以拿 transcript.md 比一比
  * 文字是否对得上（同一个人说的话、专有名词）；
  * 说话人分段是否合理（谁和谁被分成了同一个人、有没有把两个人合成一个）；
  * 时间轴是否连续（有没有整段漏掉）。

注意：旧结果本身也可能有错（它是一次自动转写的产物，说话人编号只是"这一场里的第 N 个人"，
不是人名）。对比时请以音频为准。
"""


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _crc32(path):
    import zlib
    crc = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def die(msg):
    print("[X] " + msg, file=sys.stderr)
    raise SystemExit(1)


# ------------------------------------------------------------------ 自检

def extract_embedded_python(text):
    """把 导入.ps1 里 `$script:PY_SRC = @'` … `'@` 之间的 Python 抠出来。"""
    marker = "$script:PY_SRC = @'"
    start = text.find(marker)
    if start < 0:
        die("%s 里找不到嵌入的 Python 助手（%s）" % (PS1, marker))
    start = text.find("\n", start) + 1
    end = text.find("\n'@", start)
    if end < 0:
        die("%s 里嵌入的 Python 助手没有结束标记 `'@`" % PS1)
    return text[start:end]


def check_delivery():
    problems = []
    for name in (PS1, README):
        p = os.path.join(DELIVERY, name)
        if not os.path.isfile(p):
            problems.append("缺文件：delivery\\ECHO-meeting-fixture\\" + name)
    if problems:
        die("；".join(problems) + "\n（交付源在 delivery\\ECHO-meeting-fixture\\，本脚本只打包不生成它）")

    raw = open(os.path.join(DELIVERY, PS1), "rb").read()
    if not raw.startswith(BOM):
        die("%s 缺 UTF-8 BOM —— WinPS 5.1 会按 ANSI 解析，中文乱码甚至静默解析失败" % PS1)
    body = raw[len(BOM):]
    if body.startswith(BOM):
        die("%s 有双重 BOM" % PS1)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        die("%s 不是合法 UTF-8：%s" % (PS1, exc))
    if not any(ord(ch) > 127 for ch in text):
        print("[!] %s 里没有非 ASCII 字符（BOM 现在是多余的；留着也无害）" % PS1)

    py_src = extract_embedded_python(text)
    bad = [ch for ch in py_src if ord(ch) > 127]
    if bad:
        die("嵌入的 Python 助手含非 ASCII 字符（%r…）—— 它经管道喂给 python，"
            "中文会在控制台编码上被打碎" % bad[:5])
    try:
        compile(py_src, "<embedded>", "exec")
    except SyntaxError as exc:
        die("嵌入的 Python 助手语法错误：%s" % exc)

    md = open(os.path.join(DELIVERY, README), "rb").read()
    try:
        md.decode("utf-8")
    except UnicodeDecodeError as exc:
        die("%s 不是合法 UTF-8：%s" % (README, exc))
    print("[OK] 交付源自检通过（%s 带 BOM；嵌入 Python 纯 ASCII 且可编译；%s 是 UTF-8）"
          % (PS1, README))
    return text


def check_source(meeting):
    src = os.path.join(ROOT, "data", "meetings", meeting)
    if not os.path.isdir(src):
        die("找不到会议目录：%s（用 --meeting 指定别的）" % src)
    wavs = sorted(f for f in os.listdir(src) if WAV_RE.match(f) and
                  os.path.isfile(os.path.join(src, f)))
    if not wavs:
        die("%s 里没有 ^\\d+\\.wav$ 的分段 —— 会议链路只认这种名字" % src)
    meta = os.path.join(src, "meta.json")
    if not os.path.isfile(meta):
        die("%s 里没有 meta.json（时长/段列表/配置快照都从它来）" % src)
    import json
    try:
        data = json.load(open(meta, "r", encoding="utf-8-sig"))
    except Exception as exc:
        die("meta.json 解析失败：%s" % exc)
    if not data.get("durationSeconds"):
        die("meta.json 里没有 durationSeconds —— 导入脚本靠它写会议时长")
    total = sum(os.path.getsize(os.path.join(src, w)) for w in wavs)
    print("[OK] 数据源自检通过：%s（%d 段，%.1f MB，meta 说 %.0f 秒）"
          % (meeting, len(wavs), total / 1024.0 / 1024.0, float(data["durationSeconds"])))
    return src, wavs


# ------------------------------------------------------------------ 组包

def stage_tree(meeting, with_reference=True):
    """把"包长什么样"落到一个临时目录（含 包内清单.txt），返回 (staging, files)。

    `files` = [(相对 zip 的路径, 绝对源路径, 是否压缩)]，顺序固定。
    """
    src, wavs = check_source(meeting)
    staging = tempfile.mkdtemp(prefix="echo-fixture-build-")
    files = []

    for name in (PS1, README):
        files.append(((TOP + "/" + name), os.path.join(DELIVERY, name), True))

    mdir = os.path.join(staging, "meeting", meeting)
    os.makedirs(mdir, exist_ok=True)
    for w in wavs:
        files.append((TOP + "/meeting/" + meeting + "/" + w, os.path.join(src, w), False))
    files.append((TOP + "/meeting/" + meeting + "/meta.json",
                  os.path.join(src, "meta.json"), True))

    if with_reference:
        rdir = os.path.join(staging, REF_DIR)
        os.makedirs(rdir, exist_ok=True)
        note = os.path.join(rdir, "说明.txt")
        with open(note, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(REF_NOTE)
        files.append((TOP + "/" + REF_DIR + "/说明.txt", note, True))
        for name in REF_FILES:
            p = os.path.join(src, name)
            if os.path.isfile(p):
                files.append((TOP + "/" + REF_DIR + "/" + name, p, True))

    # 清单：包内每个文件的 大小 + sha256（目标机上可以拿它核对 54 MB 有没有拷坏）
    #
    # **刻意不写生成时间**：写了的话每次重打的包内容都不一样，`--check` 就永远是"过期"
    # （那是 build_kit 那套约定要避免的假警报），而且同一个交付源重打两次应当时刻字节一致。
    lines = ["ECHO-meeting-fixture 包内清单（由 scripts/build-meeting-fixture.py 生成）",
             "会议：%s" % meeting,
             "",
             "%-46s %12s  %s" % ("文件", "字节", "sha256")]
    for rel, path, _c in files:
        lines.append("%-46s %12d  %s" % (rel, os.path.getsize(path), _sha256(path)))
    manifest = os.path.join(staging, MANIFEST)
    with open(manifest, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    files.append((TOP + "/" + MANIFEST, manifest, True))
    return staging, files


def _fixed_stamp(meeting):
    """zip 条目统一用这个时间戳 —— **同一份源重打两次必须字节一致**。

    为什么不用"每个文件自己的 mtime"：`包内清单.txt` 是**现生成**的，它的 mtime 就是"现在"，
    于是每打一次包 zip 的字节都不同 —— 那样"重打一次对一下 sha256"这种核对就没意义了。
    取 `meta.json` 的 mtime：它来自源数据、永远不变，也仍然是这场会的真实日期。
    """
    meta = os.path.join(ROOT, "data", "meetings", meeting, "meta.json")
    try:
        return time.localtime(os.path.getmtime(meta))[:6]
    except OSError:
        return (1980, 1, 1, 0, 0, 0)


def build(meeting, out, with_reference=True):
    staging, files = stage_tree(meeting, with_reference=with_reference)
    stamp = _fixed_stamp(meeting)
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".part"
        if os.path.exists(tmp):
            os.remove(tmp)
        with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
            for rel, path, compress in files:
                info = zipfile.ZipInfo(rel, date_time=stamp)
                info.compress_type = (zipfile.ZIP_DEFLATED if compress
                                      else zipfile.ZIP_STORED)
                info.external_attr = (0o644 & 0xFFFF) << 16
                with open(path, "rb") as src, zf.open(info, "w") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp, out)

        # 回读校验：条目、BOM、体积
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            bad = zf.testzip()
            if bad:
                die("zip 自检失败（CRC 对不上）：%s" % bad)
            tops = {n.split("/", 1)[0] for n in names}
            if tops != {TOP}:
                die("zip 条目缺顶层前缀：顶层是 %r（必须只有 %r）" % (sorted(tops), TOP))
            if (TOP + "/" + PS1) not in names:
                die("zip 里没有 %s/%s" % (TOP, PS1))
            inner = zf.read(TOP + "/" + PS1)
            if not inner.startswith(BOM):
                die("zip 里的 %s 丢了 BOM（打包过程中被改写？）" % PS1)
            inner_body = inner[len(BOM):]
            if inner_body.startswith(BOM):
                die("zip 里的 %s 有双重 BOM" % PS1)
            total = sum(zf.getinfo(n).file_size for n in names)
            wav_n = sum(1 for n in names if WAV_RE.match(os.path.basename(n)))
    finally:
        # 暂存目录只放"包内清单.txt"与参考说明这两样小东西（音频是按**绝对路径**直接写进
        # zip 的，不落暂存），但**也得删**：否则每打一次包就在 %TEMP% 留一个空壳目录。
        shutil.rmtree(staging, ignore_errors=True)

    print("")
    print("[OK] 出包：%s" % out)
    print("     体积 %.1f MB（解开后 %.1f MB），%d 个条目（其中 %d 个 wav 段）"
          % (os.path.getsize(out) / 1024.0 / 1024.0, total / 1024.0 / 1024.0, len(names), wav_n))
    print("     sha256：%s" % _sha256(out))
    print("     顶层目录：%s/（解压后是一个文件夹，直接交给测试机）" % TOP)
    return out


# ------------------------------------------------------------------ --check

def check_dist(meeting):
    if not os.path.isdir(DIST):
        print("[?] dist\\ 还不存在 —— 还没出过包")
        return 3
    zips = sorted((os.path.join(DIST, f) for f in os.listdir(DIST)
                   if f.startswith("ECHO-meeting-fixture-") and f.lower().endswith(".zip")),
                  key=os.path.getmtime)
    if not zips:
        print("[?] dist\\ 下没有 ECHO-meeting-fixture-*.zip —— 还没出过包")
        return 3
    latest = zips[-1]
    # 现在这份交付源/数据源应该长什么样（用 CRC 比：与 zip 条目里存的 CRC 同一个量）
    staging, files = stage_tree(meeting, with_reference=True)
    try:
        want = {rel: _crc32(path) for rel, path, _c in files}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    stale = []
    with zipfile.ZipFile(latest) as zf:
        have = {i.filename: i.CRC for i in zf.infolist()}
    for rel, crc in want.items():
        if rel not in have:
            stale.append("缺：" + rel)
        elif have[rel] != crc:
            stale.append("变了：" + rel)
    for rel in have:
        if rel not in want:
            stale.append("多了：" + rel)
    print("[i] 最新的包：%s（%.1f MB，%s）"
          % (latest, os.path.getsize(latest) / 1024.0 / 1024.0,
             time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(latest)))))
    if stale:
        for s in stale[:20]:
            print("    " + s)
        print("[!] 这个包与当前 delivery\\ / data\\ 不一致 —— 重跑一次本脚本再发出去")
        return 2
    print("[OK] 包与当前交付源、数据源一致")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="打 ECHO 真实会议测试夹具包")
    ap.add_argument("--meeting", default=DEFAULT_MEETING, help="data\\meetings 下的会议目录名")
    ap.add_argument("--out", default="", help="输出 zip 路径（默认 dist\\ECHO-meeting-fixture-<stamp>.zip）")
    ap.add_argument("--stamp", default="", help="文件名里的时间戳（默认当前时间 YYYYMMDD-HHMM）")
    ap.add_argument("--no-reference", action="store_true", help="不附 参考-旧转写结果\\")
    ap.add_argument("--check", action="store_true", help="只报告 dist 里的包是否过期")
    args = ap.parse_args(argv)

    if args.check:
        return check_dist(args.meeting)

    check_delivery()
    stamp = args.stamp or time.strftime("%Y%m%d-%H%M")
    out = args.out or os.path.join(DIST, "ECHO-meeting-fixture-%s.zip" % stamp)
    if not os.path.isabs(out):
        out = os.path.join(ROOT, out)
    build(args.meeting, out, with_reference=not args.no_reference)
    return 0


if __name__ == "__main__":
    sys.exit(main())
