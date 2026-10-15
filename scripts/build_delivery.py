# -*- coding: utf-8 -*-
"""组一份**交付资料夹**：一个脚本 + 1~3 个 zip（用户 2026-10-01 定的交付形态）。

用户原话：**"以后我交付给新用户的时候就是有一个脚本加 1-3 个 zip 文件，用户只要执行
这个脚本就完成解压安装等工作"**。

所以这里出的东西不再是"一个 285 MB 的 kit zip（同事还得先解压）"，而是：

    dist/ECHO-delivery-<stamp>/
      装我.cmd                        ← 双击这一个（ASCII、CRLF）
      ECHO-kit-<stamp>.zip            ← 客户端主包（脚本会自己解开，见下）
      ECHO-backend-portable-<…>.zip   ← 后端薄包（可选，20 MB：源码 + 解释器）
      ECHO-backend-offline-<…>.zip    ← 后端离线包（可选，几 GB：依赖已装好 → 零下载）
      先读我.md                        ← 给同事看的说明
      清单.txt                         ← 里面有什么、每个 zip 干什么用、后端那两个问题怎么答
    dist/ECHO-delivery-<stamp>.zip     ← 上面那个资料夹的 zip（`--no-zip` 可跳过）

**离线包是个开关（`--no-offline`，2026-10-16 加）**：`dist/` 里躺着后端离线包（3.1 GB）时，
默认**带**它 —— 那一层 ≈3.4 GB；但"纯客户端"那档（先配对别人的 GPU）本应只有 ≈432 MB。
所以给了 `--no-offline`：**只影响这一层放不放离线包**，`清单.txt` 会如实写"这一层不含离线包"。
两个场景里只有 `client` 能配它：`full-local` 的语义是"**零下载**"（必须带离线包 + 权重），
两个一起给就是自相矛盾 —— **报错**，不静默忽略其中一个。

**为什么脚本能"自己解压"**：`装我.cmd`（模板 `delivery/kit-install.cmd`）有两种形态 ——
旁边已经解开了 `echo-core\\` 就直接装；只有 `ECHO-kit-*.zip` 就 `tar -xf` 到
`.\\echo-kit\\` 再装。所以同事不需要右键解压，双击就完事。

**后端那两个 zip 为什么摆在脚本旁边**：安装流程最后会问一句"后端怎么来"，选"本机跑"时
`-BackendDir` 指的就是脚本这一层 —— 有离线包就**直接复制启用（零下载）**，只有薄包就
把它放好并**触发下载**，两个都没有就如实说一句（以后在面板里配）。

自检（都在**发出去之前**炸，别留到同事手上）：
  ① `装我.cmd` 是 ASCII 且被统一成 CRLF（复用 `build_kit.copy_kit_cmd`，非 ASCII 直接抛）；
  ② kit zip 在、且里面**真的有** `装我.cmd`（那是 `verify_kit` 对 Windows 的要求）；
  ③ 两个后端 zip 的名字至少命中 `app/backend_fetch.py` 里的一条 glob ——
     **出包侧与安装期认包**就是靠这一条焊在一起的（名字命中不了，面板那侧就找不到它）。
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_kit                                    # noqa: E402

DIST = build_kit.DIST
DELIVERY = build_kit.DELIVERY
PKG_PREFIX = "ECHO-delivery"
README_TEMPLATE = DELIVERY / "kit-readme-win.md"
FORWARD = "装我.cmd"

#: 后端两个 zip 的**文件名模式**：与 `app/backend_fetch.py` 的两组 glob 同源（见 `consumer_globs()`，
#: 不从那儿 import 是因为出包脚本不该依赖 app 的运行时环境；对不上时自检会说清）。
BACKEND_THIN_GLOBS = ("ECHO-backend-portable-*.zip", "ECHO-backend-portable*.zip",
                      "*本机GPU后端包*.zip", "*后端包*.zip")
BACKEND_OFFLINE_GLOBS = ("ECHO-backend-offline-*.zip", "ECHO-backend-offline*.zip",
                         "*后端离线包*.zip", "*backend-offline*.zip")


class DeliveryError(RuntimeError):
    pass


def consumer_globs() -> tuple[list[str], list[str]]:
    """从 `app/backend_fetch.py` **读出**那两组 glob（出包侧与安装期认包的唯一连接点）。

    直接 import 那个模块会把它的一串依赖（paths / credentials / pairing…）都拉进来，
    出包脚本不该有那种副作用；而这两组是**常量字面量**，源码里读出来最稳（同一手法见
    `tests/test_build_offline_pack.py::BundleContractWithInstallAll`：名字就是契约）。
    读不到就退回本文件顶上那两份 —— 那时至少不会静默跳过这条自检。
    """
    src = ROOT / "app" / "backend_fetch.py"
    try:
        text = src.read_text(encoding="utf-8")
    except OSError:
        return list(BACKEND_THIN_GLOBS), list(BACKEND_OFFLINE_GLOBS)
    out: list[list[str]] = []
    for name in ("PACKAGE_GLOBS", "OFFLINE_GLOBS"):
        m = re.search(r"%s[^=]*=\s*\((.*?)\)" % name, text, re.S)
        if not m:
            out.append([])
            continue
        out.append(re.findall(r'"([^"]+)"', m.group(1)))
    thin, offline = out
    return (thin or list(BACKEND_THIN_GLOBS)), (offline or list(BACKEND_OFFLINE_GLOBS))


def matches_any(name: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in globs)


def looks_like_backend_package(zip_path: Path) -> bool:
    """内容判据（与 `app/backend_fetch.py::_is_backend_package` 同一条）：zip 里有
    `<顶层>/server/requirements.txt`。**只读中央目录**，不解开。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                parts = [p for p in name.replace("\\", "/").split("/") if p]
                if len(parts) >= 3 and parts[1] == "server" and parts[2] == "requirements.txt":
                    return True
    except (OSError, zipfile.BadZipFile):
        return False
    return False


#: 这些**不是**"本机跑后端"那条路的东西（别拿它们报"名字认不出来"）：
#:   `ECHO-backend-kit-*` = 容器交付（Docker/compose 那条路），`ECHO-main-*` = 客户端主包，
#:   `ECHO-models-*` = 权重，`ECHO-delivery-*` = 本脚本自己的产物。
_NOT_BACKEND_PORTABLE = ("ECHO-backend-kit-", "ECHO-main-", "ECHO-models-", "ECHO-delivery-")


def strays_in(dist: Path, known: tuple[list[str], list[str]]) -> list[Path]:
    """`dist` 里"看着是后端包、名字却认不出来"的 zip。

    为什么值得专门找：名字对不上时，安装期（`backend_fetch`）与面板都**找不到它**，而
    这一切**不报错** —— 交付里少了后端包、同事装完才发现"起本机后端"要下 3 GB。
    交付汇总目录里那份被人工改名成 `3-本机GPU后端包-20MB.zip` 就是这么来的，所以这一条
    必须在**发出去之前**响。
    """
    thin, offline = known
    out: list[Path] = []
    for pat in ("*.zip", "*/*.zip"):
        for p in sorted(dist.glob(pat)):
            if not p.is_file() or p.name.startswith(_NOT_BACKEND_PORTABLE):
                continue
            if matches_any(p.name, thin) or matches_any(p.name, offline):
                continue
            if looks_like_backend_package(p):
                out.append(p)
    return out


def newest_matching(dist: Path, globs: tuple[str, ...]) -> Path | None:
    """在 `dist/` 与它的**下一层**里找最新的一个（后端包常带自己的子目录，如
    `dist/delivery-clean/ECHO-backend-portable-clean-room.zip`）。"""
    hits: list[Path] = []
    for pat in globs:
        hits += [p for p in dist.glob(pat) if p.is_file()]
        hits += [p for p in dist.glob("*/" + pat) if p.is_file()]
    return sorted(hits, key=lambda p: p.name)[-1] if hits else None


def zip_has_entry(zip_path: Path, entry: str) -> bool:
    """zip 里有没有这一条（**带不带顶层前缀都认**：`装我.cmd` 与 `<包名>/装我.cmd`）。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                parts = [p for p in name.replace("\\", "/").split("/") if p]
                if parts and parts[-1] == entry:
                    return True
    except (OSError, zipfile.BadZipFile):
        return False
    return False


# ---------------------------------------------------------------- 交付标准（2026-10-05 固化）

#: 客户端包**必须**带的载荷（"免下载"的凭据）：`bundle/wheels` 给 pip `--no-index`；
#: `bundle/models/sherpa-onnx-streaming` 是语音指令那条路的流式模型（189 MB）。
#: 为什么要判它：2026-10-05 那次就是 kit 从 285 MB 掉成 7 MB（没带载荷）**没人拦**，
#: 同事装的时候变成全量联网下载。
BUNDLE_REQUIRED_ENTRIES = ("bundle/wheels", "bundle/models/sherpa-onnx-streaming")

#: 交付场景（用户 2026-10-05 定）：`client` = 客户端包 + 后端薄包（先配对别人的 GPU）；
#: `full-local` = 再加后端离线包，且它**必须自带权重** —— 否则"那台机器零下载"就是空话。
SCENARIOS = ("client", "full-local")

#: 手机触点（Android）的 APK —— 交付层里**要带上**（用户 2026-10-15："apk 也放到交付包里"）。
#:
#: 现在只有 debug 包是有意的：`android/README.md` §构建 就是 `:app:assembleDebug`，
#: 产物 `android/app/build/outputs/apk/debug/app-debug.apk`（约 17 MB，能直接装到手机）。
#: 带包的判据分两层：① `dist\ECHO-android-*.apk`（出过包就用最新那份）；② 退回 Gradle 产物目录
#: （debug 优先、release 其次，最后兜底扫 `outputs\` 下任何 apk）。
#: **找不到不算错**：交付层照样出，只是 清单.txt 里不会有那一行（手机包本来是可选的触点）。
APK_DIST_GLOB = "ECHO-android-*.apk"
APK_GRADLE_OUTPUTS = ("android/app/build/outputs/apk/debug/app-debug.apk",
                      "android/app/build/outputs/apk/release/app-release.apk")
APK_DEST_PREFIX = "ECHO-android-"

#: 标准配方（帮助文本与报错里共用同一份，别抄第二遍）
STANDARD_HOWTO = (
    "标准做法（2026-10-05 定）：客户端包要自带载荷，否则同事装的时候要联网下运行时/依赖/模型。\n"
    "     出法：\n"
    "       python scripts\\build_offline_pack.py --bundle --out dist\\_bundle\n"
    "       python scripts\\build_kit.py --platforms win "
    "--bundle-from dist\\_bundle\\ECHO-bundle-<stamp>\\bundle\n"
    "     再把 --kit 指到那份**带 bundle** 的 kit（名字仍是 ECHO-kit-<日期>-<时分>.zip）。")


def zip_top_level(zip_path: Path) -> str:
    """zip 的顶层目录名（kit 的条目都带 `<kit 名>/` 前缀）。读不动就回空串。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n.replace("\\", "/") for n in zf.namelist() if n.strip("/")]
    except (OSError, zipfile.BadZipFile):
        return ""
    return names[0].split("/")[0] if names else ""


def bundle_problems(kit: Path) -> list[str]:
    """kit 里**缺哪些载荷**（空表 = 合格）。只读中央目录，不解开。"""
    top = zip_top_level(kit)
    if not top:
        return list(BUNDLE_REQUIRED_ENTRIES)
    try:
        with zipfile.ZipFile(kit) as zf:
            entries = [n.replace("\\", "/") for n in zf.namelist()]
    except (OSError, zipfile.BadZipFile):
        return list(BUNDLE_REQUIRED_ENTRIES)
    return [rel for rel in BUNDLE_REQUIRED_ENTRIES
            if not any(e.startswith("%s/%s/" % (top, rel)) for e in entries)]


def offline_pack_has_weights(zip_path: Path) -> bool:
    """后端离线包里**有没有权重**。

    `build_backend_portable.py --models-from …` 会把权重放在**包根**的 `models/` 下；
    `full-local` 的语义是"那台机器零下载"，所以要判这一条（不带就拒绝，除非放行）。
    """
    top = zip_top_level(zip_path)
    if not top:
        return False
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return any(n.replace("\\", "/").startswith("%s/models/" % top)
                       for n in zf.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def write_manifest(dest: Path, kit: Path, thin: Path | None, offline: Path | None,
                   apk: Path | None = None, no_offline: bool = False) -> None:
    lines = [
        "这份资料夹里有什么（双击 `装我.cmd` 就装，不用手工解压）",
        "=" * 56,
        "",
        "装我.cmd                     双击它。只问一句「装到哪个目录」，然后全自动。",
        "%-28s 客户端主包（脚本会自己解到 .\\echo-kit\\）" % kit.name,
    ]
    if offline:
        lines.append("%-28s 后端**离线包**：依赖已经装好了，本机跑后端 = **零下载**"
                     % offline.name)
    if thin:
        lines.append("%-28s 后端**薄包**：只带解释器，依赖要么从离线包装、要么联网下"
                     % thin.name)
    if apk:
        lines.append("%-28s 手机触点 APK（Android）：拷到手机点一下装，配对后当遥控器用"
                     % apk.name)
    if not offline and no_offline:
        # 清单是同事**唯一**的"这层里有什么"的凭据：不含离线包就必须写出来。
        # 不然他会照着"本机自己跑 = 零下载"去装，然后在网速里等 3 GB（假话比缺件更贵）。
        lines.append("（这一层**不含后端离线包**：装了它**本机跑后端要联网下依赖** ≈3 GB"
                     "—— 纯客户端那档足够）")
    if not thin and not offline:
        lines.append("（没有后端 zip：装完在面板「能力 → 起本机后端」里配）")
    if no_offline:
        lines.append("                              要「零下载」就重出一层（去掉 --no-offline，"
                     "那层 ≈3.4 GB），或把 ECHO-backend-offline-*.zip 放进这一层")
    lines += [
        "先读我.md                     就是这份说明的展开版",
        "",
        "安装最后会问一句「后端（会议转写那台 GPU 机器）怎么来」：",
        "  1) 用别人给的后端 → 贴配对串（echo://pair?host=…&code=…）",
        "  2) 本机自己跑     → 用这个资料夹里的后端 zip（有离线包就零下载）",
        "  3) 先不配         → 以后在面板「能力」页签里弄",
        "",
        "装完面板地址会在窗口里打印出来（默认 http://127.0.0.1:8970/）。",
        "装的过程中窗口一直开着；出错也不会一闪而过，把那段输出发回来即可。",
        "",
    ]
    (dest / "清单.txt").write_text("\n".join(lines), encoding="utf-8")


def default_kit_zip(dist: Path) -> Path | None:
    """默认用哪个客户端主包：**先走 `build_kit.kit_zip_for`**（它把 win 与 macos 分得很清，
    那次"拿 mac 包往 Windows 部署"的事故就是它兜住的），**它挑不到时**再按名字兜一层。

    为什么要有兜底：`kit_zip_for` 要求"同名目录 + 同名 zip"成对（`assemble_kit` 就是那么出的），
    而交付时可能目录已经删了、只留 zip —— 那种情况下按名字挑（并排除 `-macos-`）比直接失败有用。
    """
    zip_path = build_kit.kit_zip_for(dist, build_kit.PLATFORMS[0])
    if zip_path is not None:
        return zip_path
    import re
    hits = [p for p in dist.glob("ECHO-kit-*.zip")
            if p.is_file() and re.fullmatch(r"ECHO-kit-\d{8}-\d{4}\.zip", p.name)]
    return sorted(hits, key=lambda p: p.name)[-1] if hits else None


def default_apk(dist: Path) -> Path | None:
    """带哪个手机触点 APK（没有就 None —— 交付层照样出，只是不带手机包）。

    顺序：`dist` 里版本化的 `ECHO-android-*.apk` → Gradle 的 debug/release 产物 →
    `android/app/build/outputs/` 下任何 apk（Gradle 升级换了路径也不至于静默丢掉）。
    """
    hits = [p for p in dist.glob(APK_DIST_GLOB) if p.is_file()]
    if hits:
        return max(hits, key=lambda p: (p.stat().st_mtime, p.name))
    for rel in APK_GRADLE_OUTPUTS:
        p = ROOT / rel
        if p.is_file():
            return p
    outs = ROOT / "android" / "app" / "build" / "outputs"
    any_apk = [p for p in outs.rglob("*.apk") if p.is_file()] if outs.is_dir() else []
    return max(any_apk, key=lambda p: p.stat().st_mtime) if any_apk else None


def apk_dest_name(apk: Path) -> str:
    """交付层里那份 APK 的名字：**带上来源文件名**，一眼看得出是哪次构建的产物。

    `app-debug.apk` → `ECHO-android-app-debug.apk`；已经是 `ECHO-` 开头的原样保留。
    """
    return apk.name if apk.name.startswith("ECHO-") else APK_DEST_PREFIX + apk.name


def assemble(stamp: str, dist: Path, kit_zip: Path | None = None,
             backend_zip: Path | None = None, offline_zip: Path | None = None,
             apk_zip: Path | None = None, no_apk: bool = False,
             make_the_zip: bool = True, out_dir: Path | None = None,
             scenario: str = "client", allow_no_bundle: bool = False,
             allow_downloads: bool = False, no_offline: bool = False
             ) -> tuple[Path, Path | None]:
    """组出一层交付资料夹（+ 可选的外层 zip）→ ``(dir, zip 或 None)``。失败**响亮**、不留半个包。

    ``out_dir``（用户 2026-10-01 要的形态）：把这一层建在**用户的交付目录**下，而不是 `dist/` ——
    `--out D:\\ECHO-delivery` 得到 `D:\\ECHO-delivery\\ECHO-delivery-<stamp>\\…`。
    **那层带日期/时间的目录名是刻意的**（用户原话："加一层交付日期编码，区分不同版本"）：
    同一个交付目录里可以并存好几版，同事拿到哪一个一眼能看出是哪天的。

    ``no_offline``：**这一层**不放后端离线包（纯客户端那档：`dist/` 里躺着 3.1 GB 的离线包时，
    默认会一起带上 → 整层 ≈3.4 GB，而"先配对别人的 GPU"那档本来只要 ≈432 MB）。
    它**只管这一层放不放文件**，别的行为一字不变；`清单.txt` 会如实写"这一层不含离线包"
    （否则清单就成了"说有、目录里没有"的假话）。与 `full-local` **互斥**（见下面的报错）。
    """
    # --no-offline 与 full-local 是**互相矛盾**的两个要求：full-local 的语义就是
    # "零下载"（必须带离线包 + 权重），而 --no-offline 要求不带。这里**明确报错**，
    # 不静默丢掉其中一个 —— 静默的结果一定是"同事拿到一层装不上的包"（最贵的那种错）。
    if no_offline and scenario == "full-local":
        raise DeliveryError(
            "--no-offline 与 --scenario full-local **互相矛盾**：full-local 的语义是"
            "「那台机器零下载」（必须带后端离线包 + 权重），\n"
            "     而 --no-offline 要求这一层**不带**离线包 —— 两个要求不可能同时满足。\n"
            "     二选一：纯客户端那档就 --scenario client --no-offline（≈432 MB）；\n"
            "     要零下载就别给 --no-offline（≈3.4 GB，再带权重就是 full-local ≈8 GB）。")
    kit = kit_zip or default_kit_zip(dist)
    if kit is None or not kit.is_file():
        raise DeliveryError(
            "没有客户端主包（kit zip）。先跑 `python scripts\\build_kit.py`，"
            "或用 --kit 指定一个 —— 资料夹里少了它，装我.cmd 就成了空壳。")
    # `--no-offline` 就是**在这里把离线包摘掉**：后面所有分支（自检 / 复制 / 清单）看的是同一个
    # `offline`，所以不必到处加 if —— 也就不会漏掉某一处、留下"清单说有、目录里没有"的假话。
    thin = backend_zip or newest_matching(dist, BACKEND_THIN_GLOBS)
    offline = None if no_offline else (offline_zip or newest_matching(dist, BACKEND_OFFLINE_GLOBS))
    apk = None if no_apk else (apk_zip or default_apk(dist))
    if apk is not None and not apk.is_file():
        raise DeliveryError("给的 APK 不存在：%s" % apk)
    if apk is None and not no_apk:
        print("[!] 没找到手机触点 APK（dist\\ECHO-android-*.apk 或 "
              "android\\app\\build\\outputs\\…\\*.apk）—— 这一层不带手机包。\n"
              "     要带上就先打一个：cd android && .\\gradlew.bat :app:assembleDebug")

    # ---- 自检（在写任何东西之前）
    launcher = DELIVERY / build_kit.KIT_CMD_TEMPLATE
    if not launcher.is_file():
        raise DeliveryError("交付模板不在：%s" % launcher)
    if not zip_has_entry(kit, "install-all.ps1"):
        raise DeliveryError("这个 kit zip 里没有 install-all.ps1：%s（包不完整？）" % kit.name)
    if not zip_has_entry(kit, FORWARD):
        raise DeliveryError("这个 kit zip 里没有 %s：%s" % (FORWARD, kit.name))

    # ---- ① 客户端包**必须自带载荷**（2026-10-05 固化的标准）
    missing = bundle_problems(kit)
    if missing:
        msg = ("这个 kit **没带载荷**（缺 %s）：交付出去的话，同事装的时候要联网下载"
               "运行时/依赖/模型 —— 2026-10-05 那次回退就是这样。\n     %s"
               % ("、".join(missing), STANDARD_HOWTO))
        if not allow_no_bundle:
            raise DeliveryError(msg + "\n     （确实要出「裸包」排障，就加 --allow-no-bundle。）")
        print("[!] " + msg)

    # ---- ② full-local：连后端一起给，且那份离线包**必须自带权重**（不然不叫零下载）
    if scenario == "full-local":
        if offline is None:
            raise DeliveryError(
                "full-local 场景要**后端离线包**（ECHO-backend-offline-*.zip），dist 里没有。\n"
                "     先出：python scripts\\build_backend_portable.py …（薄包 / 离线包）")
        if not offline_pack_has_weights(offline):
            msg = ("这份后端离线包**不带权重**（%s）：它只有运行时+依赖，模型要联网下（约 4.7 GB）。\n"
                   "     要「那台机器零下载」就带上权重重出："
                   "python scripts\\build_backend_portable.py --models-from <模型目录> …"
                   % offline.name)
            if not allow_downloads:
                raise DeliveryError(msg + "\n     （确定允许它联网下模型就加 --allow-downloads。）")
            print("[!] " + msg)
    thin_globs, offline_globs = consumer_globs()
    for path, globs, what in ((thin, thin_globs, "薄包"), (offline, offline_globs, "离线包")):
        if path is None:
            continue
        if not matches_any(path.name, globs):
            raise DeliveryError(
                "%s的名字**认不出来**：%s（安装期按这些模式找：%s）—— 改个名再出，"
                "否则面板那侧找不到它。" % (what, path.name, "、".join(globs)))
    # 名字认不出来 = 面板找不到它，而这一切**不报错** —— 所以宁可在这里拦住（见 `strays_in`）。
    stray = strays_in(dist, (thin_globs, offline_globs))
    if stray:
        raise DeliveryError(
            "这几个 zip 看着是后端包、名字却认不出来：%s。\n"
            "     改名叫 `ECHO-backend-portable-<…>.zip`（薄包）或 "
            "`ECHO-backend-offline-<…>.zip`（离线包）\n"
            "     —— 安装期就是按这两个模式找的（认不出来的话面板那侧永远找不到它）。"
            % "、".join(p.name for p in stray))

    # 交付那一层：名字里带日期/时间（区分版本）。父目录由调用方给（`--out` 是用户的交付目录）。
    parent = (out_dir or dist)
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / ("%s-%s" % (PKG_PREFIX, stamp))
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        build_kit.copy_kit_cmd(launcher, dest / FORWARD)      # ASCII + CRLF 都在里面炸
        shutil.copy2(kit, dest / kit.name)
        if thin:
            shutil.copy2(thin, dest / thin.name)
        if offline:
            shutil.copy2(offline, dest / offline.name)
        apk_dest = None
        if apk:
            apk_dest = dest / apk_dest_name(apk)
            shutil.copy2(apk, apk_dest)
        if README_TEMPLATE.is_file():
            shutil.copy2(README_TEMPLATE, dest / build_kit.KIT_README)
        write_manifest(dest, kit, thin, offline, apk_dest, no_offline=no_offline)
        out_zip = parent / ("%s-%s.zip" % (PKG_PREFIX, stamp)) if make_the_zip else None
        if out_zip is not None:
            build_kit.make_zip(dest, out_zip, dest.name)      # 条目带顶层前缀（缺了会散成一堆）
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)               # 不留半个包
        raise
    return dest, out_zip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="组一份交付资料夹（一个脚本 + 1~3 个 zip + 手机触点 APK）")
    ap.add_argument("--dist", default=str(DIST))
    ap.add_argument("--stamp", default="")
    ap.add_argument("--kit", default="", help="客户端 kit zip（默认取 dist 里最新的 Windows 那份）")
    ap.add_argument("--backend", default="", help="后端薄包 zip（默认取 dist 里最新的）")
    ap.add_argument("--backend-offline", default="", help="后端离线包 zip（默认取 dist 里最新的）")
    ap.add_argument("--no-zip", action="store_true", help="只出资料夹，不再套一层 zip")
    ap.add_argument("--scenario", choices=SCENARIOS, default="client",
                    help="交付场景：client=客户端包（自带载荷）+后端薄包；"
                         "full-local=再加后端离线包（要求它自带权重，做到零下载）")
    ap.add_argument("--allow-no-bundle", action="store_true",
                    help="允许出没有 bundle 的裸 kit（只给排障用；正式交付不要用）")
    ap.add_argument("--allow-downloads", action="store_true",
                    help="full-local 时允许后端离线包不带权重（那台机器要联网下模型）")
    ap.add_argument("--no-offline", action="store_true",
                    help="这一层**不带**后端离线包 —— 纯客户端那档用（先配对别人的 GPU）。"
                         "为什么要有它：dist 里有离线包（3.1 GB）时默认会一起带上，"
                         "整层就从 ≈432 MB 变成 ≈3.4 GB，而「纯客户端」根本用不到那 3 GB；"
                         "以前只能手工把离线包移出 dist（手工活，必漂）。"
                         "它只管这一层放不放文件，别的行为一字不变，"
                         "清单.txt 会如实写「这一层不含离线包」。"
                         "**不能与 --scenario full-local 同时给**：那个场景的语义是"
                         "「零下载」（必须带离线包 + 权重），两个一起给是矛盾的，会直接报错")
    ap.add_argument("--apk", default="", metavar="PATH",
                    help="手机触点 APK（默认取 dist\\ECHO-android-*.apk；没有就取 "
                         "android\\app\\build\\outputs 下最新的那份）")
    ap.add_argument("--no-apk", action="store_true",
                    help="交付层里不放手机触点 APK")
    ap.add_argument("--out", default="", metavar="DIR",
                    help="把交付那一层建在 DIR 下（用户的交付目录，如 D:\\ECHO-delivery）："
                         "得到 DIR\\ECHO-delivery-<日期>-<时分>\\装我.cmd + 客户端包 + 后端薄包。"
                         "**那层日期是刻意加的**，同一个目录里能并存好几版、一眼看出是哪天的；"
                         "不给就建在 --dist 下（本地归档）")
    args = ap.parse_args(argv)

    import time
    stamp = args.stamp or time.strftime("%Y%m%d-%H%M")
    dist = Path(args.dist).resolve()
    dist.mkdir(parents=True, exist_ok=True)
    try:
        dest, out_zip = assemble(
            stamp, dist,
            scenario=args.scenario,
            allow_no_bundle=bool(args.allow_no_bundle),
            allow_downloads=bool(args.allow_downloads),
            no_offline=bool(args.no_offline),
            kit_zip=Path(args.kit).resolve() if args.kit else None,
            backend_zip=Path(args.backend).resolve() if args.backend else None,
            offline_zip=Path(args.backend_offline).resolve() if args.backend_offline else None,
            apk_zip=Path(args.apk).resolve() if args.apk else None,
            no_apk=bool(args.no_apk),
            make_the_zip=not args.no_zip,
            out_dir=Path(args.out) if args.out else None)
    except DeliveryError as e:
        print("[x] %s" % e)
        return build_kit.EXIT_ERROR
    if args.out:
        print("[ok] 已建在交付目录下（这一层带日期，用来区分版本）：%s" % dest)
    else:
        print("[ok] 交付资料夹：%s" % dest)
    for p in sorted(dest.iterdir()):
        if p.is_file():
            print("       %-34s %8.1f MB" % (p.name, p.stat().st_size / (1 << 20)))
    if out_zip:
        print("[ok] 打包：%s（%.1f MB）" % (out_zip, out_zip.stat().st_size / (1 << 20)))
    print("     交给同事：把这一层（或那个 zip）给他，让他双击 %s。" % FORWARD)
    return build_kit.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
