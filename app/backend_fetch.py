# -*- coding: utf-8 -*-
"""薄包那条路的**取载荷**：① 把**薄包**弄到 `{backend}/`，② 按国内源把 torch/torchaudio/依赖装进 `{backend}/runtime`。

用户 2026-09-30 拍板：**默认 = 薄包 + 国内可下载**（"通用包 / 卡包"两段式以后再说）。
所以薄包本身只有源码 + 配置模板 + 安装脚本 + CPython（可选），**几 GB 的 torch 不随包走** ——
由这一步从**国内镜像**取。国内源在**这台机器所在网络**实测可达（2026-09-30）：

| 用途 | 源 | 实测 |
|---|---|---|
| 普通依赖（fastapi/uvicorn/funasr/transformers/pyannote…） | 清华 PyPI `https://pypi.tuna.tsinghua.edu.cn/simple`、阿里 `https://mirrors.aliyun.com/pypi/simple/` | 200 ✅ |
| **CUDA 版 torch/torchaudio** | SJTU `https://mirror.sjtu.edu.cn/pytorch-wheels/<cuXXX>/`（标准 PEP503 索引） | 200 ✅（`cu128` 实测） |

## 两条纪律，都是从真事故里来的

1. **torch 与 torchaudio 必须同一条 pip、同一个索引**（`server/Dockerfile` 的构建期校验就是这条）。
   分开装、或让 torchaudio 从普通 PyPI 被依赖顺带拉进来，会拿到**无标签/cu13x** 那份 →
   `import torchaudio` 崩 → funasr(SenseVoice) 模型加载也失败 → **每个 `/v1/asr` 都 503 model_failed**。
   所以这里是**两阶段**：① 只装 torch+torchaudio（CUDA 索引）；② 再装其余（PyPI 镜像）。
2. **装完必须自检**（`backend_env.check_torch_abi` + `import funasr`）——**在安装期炸，不要留到运行时**。
   返回的 `detail` 里带 pip 的**原文**，不加工。

**永不抛**：返回 `(ok, detail)`；每一步的输出都落到 `{backend}/logs/fetch-*.log`，
面板/端点只显示"到哪一步了 + 失败原因原文"。

## 第 −1 步：**取薄包**（2026-09-30 接进来的）

在这之前 `plan()` 只能说"薄包解好之后，一次点击就够了" —— 因为**薄包本身**要人工解到
`{backend}`。现在这一步也在一键路里（`ensure_package()`）：

    {backend}/server/requirements.txt 在？   → 已经解开，成功
    否则：设置 `capabilityBackendPackage` / 环境变量 `ECHO_BACKEND_PACKAGE`
          （zip 路径 → 就地解开；http(s) → 先下载再解开）
    否则：本机常见落点里找 `ECHO-backend-portable-*.zip`（安装根、它的上一层、bundle\、
          Downloads/Desktop/Documents；`ECHO_KIT_ROOT` 优先）→ 解开
    否则：**如实说"看过哪些地方、怎么给它"**（不猜、不瞎下）

**为什么先找本机、再谈下载**：薄包只有 20 MB，现实里它就在同事手边（汇总目录里那份
`3-本机GPU后端包-20MB.zip` 与客户端 kit 并排放着）。能就地拿到就不该逼谁去填 URL；
URL 只是给"东西在别的机器上"留的口子。**认包靠内容**（`server/requirements.txt` 在不在），
不靠文件名 —— 那份交付件被人手工改过名。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from app import backend_proc, backend_env, backend_setup

#: 普通依赖的 PyPI 镜像（按顺序试）。**国内优先**，因为这条路就是为国内网络定的。
PIP_INDEXES: Tuple[str, ...] = ("https://pypi.tuna.tsinghua.edu.cn/simple",
                                "https://mirrors.aliyun.com/pypi/simple/")

#: CUDA 版 torch/torchaudio 的索引模板（按变体填 `cu126` / `cu118` / `cu128`）。
#: SJTU 那个是**标准 PEP503 索引**（实测 200），所以能直接当 `--index-url` 用。
TORCH_INDEX_TMPL = "https://mirror.sjtu.edu.cn/pytorch-wheels/%s/"

#: 每个变体额外要装的（口径照 `server/Dockerfile` 的 `ECHO_EXTRA` 层：
#: 老卡装不了 pyannote，所以 cu118 刻意只装 funasr 那条链）。
VARIANT_EXTRAS: Dict[str, Tuple[str, ...]] = {
    "cu126": ("funasr", "qwen-asr", "transformers", "pyannote.audio", "speechbrain"),
    "cu118": ("funasr", "transformers"),
    "cu128": ("funasr", "qwen-asr", "transformers", "pyannote.audio", "speechbrain"),
}

#: torch 版本钉法：老卡（Pascal/Volta）在 cu118 上最后一版是 2.7.1（Dockerfile 同款）；
#: 新卡不钉（让索引给最新）。
VARIANT_TORCH: Dict[str, str] = {"cu118": "2.7.1", "cu126": "", "cu128": ""}

#: 老卡那档的 pyannote 装不上（4.x 要新 torch），如实说明而不是让它装一半炸掉。
VARIANT_NOTES: Dict[str, str] = {
    "cu118": "老卡档只装转写（funasr/SenseVoice）：pyannote 4.x 要 torch>=2.8，"
             "而 cu118 上 Pascal 的最后一版是 2.7.1 —— 这一档没有说话人分离。",
}

LOG_NAME = "fetch-runtime.log"
#: pip 的超时给足（几 GB，国内源实测速度可接受；这里只兜底"卡死"）。
PIP_TIMEOUT_S = 3600.0


# ---------------------------------------------------------------- 第 −1 步：取薄包
# 「取运行时」之前还差一步：**薄包本身**要在 `{backend}/` 下解开（判据 = `server/requirements.txt`）。
# 2026-09-30 之前这一步是人工的（"把薄包解到 `{echoBase}/backend`"）；这里把它接进同一趟活，
# `plan()` 因此可以说"一次点击就够了"。
#
# 为什么**先找本机、再谈下载**：薄包只有 20 MB，现实里它就在同事手边（汇总目录里那份
# `3-本机GPU后端包-20MB.zip` 与客户端 kit 并排放着）。能就地拿到就不该逼谁去填一个 URL；
# **URL 只是给"东西在别的机器上"留的口子**。

PACKAGE_PREFIX = "ECHO-backend-portable"
#: 认哪些文件名。名字只用来"快速筛"，**真正的判据是内容**（`_is_backend_package`）——
#: 交付汇总目录里那份被人工改名成了 `3-本机GPU后端包-20MB.zip`，所以名字放宽到"后端包"。
PACKAGE_GLOBS: Tuple[str, ...] = (
    "ECHO-backend-portable-*.zip",
    "ECHO-backend-portable*.zip",
    "*本机GPU后端包*.zip",
    "*后端包*.zip",
)
#: 显式指定的那个 key（设置 > 环境变量）：zip 的路径，或 http(s) 地址。
#: 留空 = 只在本机常见落点里找（见 `search_dirs()`）。
PACKAGE_SETTING = "capabilityBackendPackage"
PACKAGE_ENV = "ECHO_BACKEND_PACKAGE"
PACKAGE_LOG = "fetch-package.log"
#: 解开时**只搬这些顶层项**（白名单）。为什么是白名单而不是"整包倒进去"：包解在
#: `{backend}` 里，而那里同时住着 `server.yaml`（我们生成的配置）、`state/`（鉴权库与本机
#: 配对文件）、`cache/`、`logs/`、`tmp/` —— 整包倒进去迟早会拿包里的模板覆盖掉用户的配置。
PACKAGE_DIR_ITEMS: Tuple[str, ...] = ("app", "server", "runtime", "wheels", "scripts", "models")
PACKAGE_FILE_ITEMS: Tuple[str, ...] = ("server.yaml.tmpl", "sources.json", "manifest.json",
                                       "先读我.md", "MODELS-INCLUDED.txt")
#: 下载超时（20 MB，给足；这里只兜底"卡死"）。
PACKAGE_TIMEOUT_S = 600.0


def package_log_path() -> str:
    return os.path.join(backend_setup.backend_root(), "logs", PACKAGE_LOG)


def _log_package(text: str) -> None:
    path = package_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def _setting(key, default=None):
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:                                            # pragma: no cover - 兜底
        return default


def package_source() -> str:
    """用户**显式**指的那个（设置 `capabilityBackendPackage` > 环境变量）→ 路径或 URL。

    空 = 没指 —— 那就只在本机常见落点里找。**这是"下载"的唯一入口**：没有地址就不下载，
    绝不去猜一个网络位置（猜错的表现是"下回来一个不相干的 zip"）。
    """
    for value in (_setting(PACKAGE_SETTING, ""), os.environ.get(PACKAGE_ENV, "")):
        text = str(value or "").strip().strip('"')
        if text:
            return text
    return ""


def search_dirs() -> List[str]:
    """在本机哪些目录里找薄包 zip（顺序 = 优先级）。**只读**：不建目录、不写盘。"""
    base = backend_setup.backend_root()
    home = os.path.expanduser("~")
    cands = [
        str(os.environ.get("ECHO_KIT_ROOT") or "").strip(),   # 安装器若知道资料夹在哪，它最该先看
        base,                                                 # {backend} 本身
        os.path.dirname(base),                                # 安装根 —— 汇总目录常在这一层
        os.path.join(base, "bundle"),                         # kit 的载荷目录
        os.path.join(os.path.dirname(base), "bundle"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
    ]
    out: List[str] = []
    for d in cands:
        if d and os.path.isdir(d) and os.path.abspath(d) not in out:
            out.append(os.path.abspath(d))
    return out


def _is_backend_package(zip_path: str) -> bool:
    """内容判据：zip 里有 `<顶层>/server/requirements.txt`（或 manifest 自报 backend-portable）。

    为什么不能只看文件名：交付汇总目录里那份被人改过名；反过来，一个名字对得上但内容残缺的
    zip 也不该被当成"能用的薄包"（那样只会在几分钟后以"pip 找不到东西"失败）。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n.replace("\\", "/") for n in zf.namelist()]
            for name in names:
                parts = [p for p in name.split("/") if p]
                if len(parts) >= 3 and parts[1] == "server" and parts[2] == "requirements.txt":
                    return True
            for name in names:
                parts = [p for p in name.split("/") if p]
                if len(parts) == 2 and parts[1] == "manifest.json":
                    try:
                        data = json.loads(zf.read(name).decode("utf-8"))
                    except Exception:
                        continue
                    if str(data.get("package") or "") == "backend-portable":
                        return True
    except Exception:
        return False
    return False


def find_package_zip() -> str:
    """在本机常见落点里找薄包 zip → 路径；没有 = 空串。"""
    for d in search_dirs():
        for pat in PACKAGE_GLOBS:
            for path in sorted(glob.glob(os.path.join(d, pat))):
                if _is_backend_package(path):
                    return os.path.abspath(path)
    return ""


def _safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
    """解 zip 时挡掉越界条目（`..` / 绝对路径 / 盘符）。

    薄包也是**外部输入**（同事拷来拷去、还可能从网上下），不该有任何一条能把文件写到
    `dest` 之外 —— `ZipFile.extractall` 在这件事上不该被信任。
    """
    dest_abs = os.path.abspath(dest)
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if name.startswith("/") or ".." in parts:
            raise ValueError("包里有个不安全的路径：%s" % info.filename)
        target = os.path.abspath(os.path.join(dest_abs, *parts))
        if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
            raise ValueError("包里有个越界的路径：%s" % info.filename)
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as fh:
            shutil.copyfileobj(src, fh)


def _package_root(tmp: str) -> str:
    """解开后的**载荷根**：单层顶层目录里的那一份，或者就是 `tmp` 本身。"""
    if os.path.isfile(os.path.join(tmp, "server", "requirements.txt")):
        return tmp
    try:
        names = sorted(os.listdir(tmp))
    except OSError:
        return ""
    for name in names:
        cand = os.path.join(tmp, name)
        if os.path.isdir(cand) and os.path.isfile(
                os.path.join(cand, "server", "requirements.txt")):
            return cand
    return ""


def extract_package(zip_path: str, *, on_step: Optional[Callable[[str], None]] = None
                    ) -> Tuple[bool, str]:
    """把薄包解开到 `{backend}` → `(ok, detail)`。**永不抛、只动白名单里的那几项。**"""
    if on_step:
        on_step("取薄包：解开 %s" % os.path.basename(zip_path))
    root = backend_setup.backend_root()
    tmp = os.path.join(root, "tmp", "pkg-%s" % time.strftime("%H%M%S"))
    try:
        os.makedirs(tmp, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, tmp)
        payload = _package_root(tmp)
        if not payload:
            return False, "这个 zip 里没有薄包（找不到 server/requirements.txt）：%s" % zip_path
        for name in PACKAGE_DIR_ITEMS:
            src = os.path.join(payload, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(root, name), dirs_exist_ok=True)
        for name in PACKAGE_FILE_ITEMS:
            src = os.path.join(payload, name)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(root, name))
    except Exception as e:
        return False, "解开薄包失败：%s（%s）" % (e, zip_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    need = os.path.join(root, "server", "requirements.txt")
    if not os.path.isfile(need):
        return False, "解开了但没看到 %s —— 包的结构不对" % need
    _log_package("已解开薄包：%s → %s" % (zip_path, root))
    if on_step:
        on_step("薄包已就位：%s" % root)
    return True, "薄包已解开到 %s（来自 %s）" % (root, zip_path)


def download_package(url: str, *, on_step: Optional[Callable[[str], None]] = None
                     ) -> Tuple[str, str]:
    """把薄包从 http(s) 下到 `{backend}/tmp/` → `(path, error)`。**只认 http(s)。**"""
    if not str(url).lower().startswith(("http://", "https://")):
        return "", "只认 http(s) 地址，别的 scheme 不下载：%s" % url
    root = backend_setup.backend_root()
    name = str(url).split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "backend-portable.zip"
    if not name.lower().endswith(".zip"):
        name += ".zip"
    dest = os.path.join(root, "tmp", name)
    if on_step:
        on_step("取薄包：下载 %s" % url)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except Exception as e:
        return "", "建不了临时目录（%s）：%s" % (os.path.dirname(dest), e)
    part = dest + ".part"
    try:
        with urllib.request.urlopen(url, timeout=PACKAGE_TIMEOUT_S) as resp, \
                open(part, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        os.replace(part, dest)
    except Exception as e:
        try:
            os.remove(part)
        except OSError:
            pass
        return "", "下载薄包失败：%s（%s）" % (e, url)
    _log_package("已下载薄包：%s → %s" % (url, dest))
    return dest, ""


def package_plan() -> Dict[str, Any]:
    """薄包这一步的**只读**计划（面板「取薄包」那一格 + `implemented` 的判据）。"""
    root = backend_setup.backend_root()
    ready = os.path.isfile(os.path.join(root, "server", "requirements.txt"))
    explicit = package_source()
    found = "" if (ready or explicit) else find_package_zip()
    if ready:
        headline = "薄包已解开：%s" % root
    elif explicit:
        headline = ("会按你指的去取薄包：%s（%s）"
                    % (explicit, "下载后解开" if explicit.lower().startswith("http")
                       else "就地解开"))
    elif found:
        headline = "本机找到薄包：%s —— 点下去会先解开它" % found
    else:
        headline = ("薄包还没有：本机这几个地方都没找到 %s-*.zip（%s）"
                    % (PACKAGE_PREFIX, "、".join(search_dirs()) or "没有可看的目录"))
    return {"ready": ready, "root": root, "explicit": explicit, "source": explicit or found,
            "search": search_dirs(), "log": package_log_path(),
            "willFetch": bool(ready or explicit or found), "headline": headline}


def ensure_package(*, on_step: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """**第 −1 步：把薄包弄到 `{backend}`** → `(ok, detail)`。**永不抛。**

    顺序（每一步都写进 `{backend}/logs/fetch-package.log`）：

      ① 已经解开（`server/requirements.txt` 在）→ 成功；
      ② 显式指的（设置 `capabilityBackendPackage` / 环境变量 `ECHO_BACKEND_PACKAGE`）：
         路径 → 就地解开；http(s) → 先下载再解开；
      ③ 本机常见落点里有薄包 zip → 解开；
      ④ 都没有 → **如实说"看过哪些地方、怎么给它"**（不猜、不瞎下）。
    """
    root = backend_setup.backend_root()
    if os.path.isfile(os.path.join(root, "server", "requirements.txt")):
        return True, "薄包已在：%s" % root
    src = package_source()
    if src:
        if src.lower().startswith(("http://", "https://")):
            path, err = download_package(src, on_step=on_step)
            if not path:
                return False, err
        else:
            path = os.path.abspath(os.path.expanduser(src))
            if not os.path.isfile(path):
                return False, "设置 %s 指的薄包不在：%s" % (PACKAGE_SETTING, path)
            if not _is_backend_package(path):
                return False, "这个文件不像薄包（里面没有 server/requirements.txt）：%s" % path
        return extract_package(path, on_step=on_step)
    path = find_package_zip()
    if path:
        return extract_package(path, on_step=on_step)
    return False, ("薄包还没有：%s 这几个地方都没找到 %s-*.zip。把它放到其中之一，"
                   "或在设置 `%s`（或环境变量 %s）里填 zip 的路径 / 下载地址。"
                   % ("、".join(search_dirs()) or "（没有可看的目录）", PACKAGE_PREFIX,
                      PACKAGE_SETTING, PACKAGE_ENV))


def runtime_dir() -> str:
    return os.path.join(backend_setup.backend_root(), "runtime")


def log_path() -> str:
    return os.path.join(backend_setup.backend_root(), "logs", LOG_NAME)


def torch_index(variant: str) -> str:
    return TORCH_INDEX_TMPL % str(variant or "cu126").strip()


def requirements(variant: str) -> Dict[str, Any]:
    """这一步要装什么（**分两阶段**，因为索引不同）→ 面板/日志共用一份。"""
    v = str(variant or "cu126").strip()
    base = os.path.join(backend_setup.backend_root(), "server", "requirements.txt")
    pin = VARIANT_TORCH.get(v, "")
    torch_pkgs = ["torch" + ("==%s" % pin if pin else ""),
                  "torchaudio" + ("==%s" % pin if pin else "")]
    return {"variant": v, "torchIndex": torch_index(v), "torch": torch_pkgs,
            "extras": list(VARIANT_EXTRAS.get(v, ())), "baseFile": base,
            "baseFileExists": os.path.isfile(base),
            "note": VARIANT_NOTES.get(v, ""),
            "pipIndexes": list(PIP_INDEXES)}


def _write_log(lines: List[str], chunk: str) -> None:
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(chunk if chunk.endswith("\n") else chunk + "\n")
    except Exception:
        pass


def _run_pip(exe: str, args: List[str], label: str,
             on_step: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """跑一条 pip，**输出同时落日志**（面板只显示最后几行 + 原文尾部）。"""
    cmd = [exe, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
           "--no-warn-script-location"] + args
    if on_step:
        on_step(label)
    _write_log([], "\n===== %s =====\n$ %s" % (time.strftime("%H:%M:%S"), " ".join(cmd)))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S)
    except Exception as e:
        detail = "%s 失败：%s" % (label, e)
        _write_log([], detail)
        return False, detail
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    _write_log([], out[-4000:])
    if proc.returncode != 0:
        tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-6:])
        return False, "%s 失败（pip 退出码 %d）：\n%s\n（完整输出：%s）" % (
            label, proc.returncode, tail, log_path())
    return True, "%s 完成" % label


def ensure_runtime(variant: str = "", *, on_step: Optional[Callable[[str], None]] = None,
                   fetch_package: bool = True) -> Tuple[bool, str]:
    """把运行时装进 `{backend}/runtime` → ``(ok, detail)``。**永不抛。**

    前提：**薄包已经解开**（`{backend}/server/requirements.txt` 在）且**有解释器**——
    解释器可以来自随包的 `runtime/`，也可以由调用方（`backend_setup.start(python=…)`）指定。

    ``fetch_package``（2026-09-30）：薄包还没解开时**先把它取来**（`ensure_package()`：
    本机找 / 按你指的路径或 URL 取）。**顺序很重要**：解释器可能就在薄包自带的
    `runtime/` 里 —— 先问"有没有解释器"再问"薄包在不在"，会把"薄包还没到"误报成
    "薄包里没有解释器"，把人引去装 Python（那条路根本不需要）。
    """
    v = str(variant or "cu126").strip()
    need = requirements(v)
    if fetch_package and not need["baseFileExists"]:
        ok, detail = ensure_package(on_step=on_step)
        if not ok:
            return False, detail
        need = requirements(v)                 # 薄包刚解开：baseFile 现在应该在
    exe = backend_proc.python_exe()
    if not exe:
        return False, ("薄包里没有解释器，也没有可用的 Python —— 薄包应当随包带一份 CPython"
                       "（`runtime/Scripts/python.exe`、`runtime/python.exe`）；"
                       "没带的话先在目标机装 Python 3.11。")
    if not need["baseFileExists"]:
        return False, ("薄包还没解开：找不到 %s —— 先用「起本机后端」取一次薄包"
                       "（或手工解到 %s）。" % (need["baseFile"], backend_setup.backend_root()))
    steps: List[str] = []
    if need["note"]:
        steps.append(need["note"])

    # ① torch + torchaudio：**同一条 pip、同一个 CUDA 索引**（纪律 1）
    torch_args = need["torch"] + ["--index-url", need["torchIndex"]]
    ok, detail = _run_pip(exe, torch_args, "装 torch/torchaudio（%s）" % need["torchIndex"],
                          on_step)
    if not ok:
        return False, detail

    # ② 其余依赖：走 PyPI 镜像（按顺序试，第一个不通换下一个）
    rest = need["extras"] + (["-r", need["baseFile"]] if need["baseFileExists"] else [])
    last = ""
    for index in need["pipIndexes"]:
        ok, detail = _run_pip(exe, rest + ["--index-url", index],
                              "装其余依赖（%s）" % index, on_step)
        if ok:
            last = detail
            break
        last = detail
    else:
        return False, last

    # ③ 装完**立刻自检**（纪律 2）：ABI + funasr 能不能 import
    if on_step:
        on_step("自检：torch/torchaudio 的 ABI + funasr 导入")
    abi = backend_env.check_torch_abi(exe)
    if not abi.get("ok"):
        return False, "装完了但 ABI 自检没过：%s" % (abi.get("error") or abi)
    try:
        probe = subprocess.run([exe, "-c", "import funasr; print(funasr.__version__)"],
                               capture_output=True, text=True, timeout=180)
        if probe.returncode != 0:
            tail = ((probe.stdout or "") + (probe.stderr or "")).strip().splitlines()[-4:]
            return False, ("torch/torchaudio 装好了，但 `import funasr` 失败：\n%s"
                           % "\n".join(tail))
    except Exception as e:                                        # pragma: no cover - 兜底
        return False, "自检 funasr 时炸了：%s" % e
    note = ("；%s" % abi.get("note")) if abi.get("note") else ""
    return True, ("运行时装好了：torch %s / torchaudio %s%s（日志：%s）"
                  % (abi.get("torch"), abi.get("torchaudio"), note, log_path()))


def plan(variant: str = "") -> Dict[str, Any]:
    """这一步**会做什么**（只读，给面板"计划"那格用）。

    含**两个**载荷的账：先取薄包（`package`），再按国内源装运行时 —— 因为
    `backend_env.plan()['implemented']` 与面板那几行都靠它说清楚"点下去到底会发生什么"。
    """
    need = requirements(variant)
    pkg = package_plan()
    return {"variant": need["variant"], "torchIndex": need["torchIndex"],
            "torch": need["torch"], "extras": need["extras"],
            "pipIndexes": need["pipIndexes"], "note": need["note"],
            "runtimeDir": runtime_dir(), "log": log_path(),
            "package": pkg,
            "packageReady": pkg["ready"], "packageSource": pkg["source"],
            "packageSearch": pkg["search"], "packageLog": pkg["log"],
            "willFetch": pkg["willFetch"],          # 整条路现在能不能一键走完
            "python": backend_proc.python_exe(),
            "sourceReady": need["baseFileExists"],
            "approxDownloadGB": 3.0,   # 估：torch+torchaudio+nvidia 库（实测本机 6 GB 解压后）
            "headline": "从国内源装运行时（torch/torchaudio 走 %s，其余走清华/阿里 PyPI）"
                        % need["torchIndex"]}
