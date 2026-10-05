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

#: CUDA 版 torch/torchaudio 的索引模板（`%s` 填 `cu126` / `cu118` / `cu128`）。**按顺序试**。
#:
#: 为什么要一串而不是一个（2026-10-01 真机实测）：SJTU 那个索引的**页面**能打开（200、而且是
#: 标准 PEP503/691 JSON 索引），但 pip 的**大文件下载**会间歇性撞
#: `[SSL] record layer failure`（同一台机器上，随包解释器的 pip 与仓库 venv 的 pip **都**撞过）。
#: 只有一个源时，用户看到的就是"装 torch 失败"。
#:
#: ⚠️ **别再往这里加"看着像镜像"的地址**：阿里云的 `mirrors.aliyun.com/pytorch-wheels/cu128/`
#: 实测是个**下载网页**（`text/html`），不是 PEP503 索引 —— pip 拿去会得到
#: `Could not find a version that satisfies the requirement torch (from versions: none)`，
#: 白等一轮还给出"包不存在"这种错判。判据：`Accept: application/vnd.pypi.simple.v1+json`
#: 时它得回 JSON（SJTU 与官方源都回，实测）。
TORCH_INDEX_TMPLS: Tuple[str, ...] = (
    "https://mirror.sjtu.edu.cn/pytorch-wheels/%s/",
    "https://download.pytorch.org/whl/%s",
)
#: 兼容旧名字（面板/文档/用例里提过 `TORCH_INDEX_TMPL`）：就是上面**第一个**（主源）。
TORCH_INDEX_TMPL = TORCH_INDEX_TMPLS[0]

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
#: **后端离线包**（2026-10-01 加）的认法。它就是"薄包 + **已经装好依赖的 `runtime/`**"：
#: 同一个 zip 里既有 `server/`（薄包那一半）也有装齐 fastapi/torch/funasr 的 `runtime/`。
#: 判据因此是"薄包的内容判据 **且** 里面有 `runtime/`"（见 `find_offline_zip`）。
#:
#: 为什么做成"薄包的超集"而不是另立一种包：`extract_package()` 的白名单、`_package_root()`
#: 的定位、`_is_backend_package()` 的内容判据全都原样复用 —— 少一套结构就少一套漂移。
#:
#: 为什么需要它（用户的交付形态）：一键路原来**只会从国内源现装**那 3 GB 依赖，交付给新用户
#: 时"点一下就完成安装"在那一步会退化成"等十几分钟下载"。有了离线包：**有就直接复制启用
#: （零下载），没有才触发下载**（用户 2026-10-01 拍板的口径）。
OFFLINE_GLOBS: Tuple[str, ...] = (
    "ECHO-backend-offline-*.zip",
    "ECHO-backend-offline*.zip",
    "*后端离线包*.zip",
    "*backend-offline*.zip",
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
    """在本机哪些目录里找薄包/离线包 zip（顺序 = 优先级）。**只读**：不建目录、不写盘。"""
    base = backend_setup.backend_root()
    home = os.path.expanduser("~")
    install_root = os.path.dirname(base)
    cands = [
        str(os.environ.get("ECHO_KIT_ROOT") or "").strip(),   # 安装器若知道资料夹在哪，它最该先看
        base,                                                 # {backend} 本身
        install_root,                                         # 安装根 —— 汇总目录常在这一层
        os.path.join(base, "bundle"),                         # kit 的载荷目录
        os.path.join(install_root, "bundle"),
        # 交付目录（2026-10-01 加）：用户给同事的形态是"一个脚本 + 1~3 个 zip"，那几份 zip 就
        # 摆在安装根里或安装根旁边的一个交付资料夹里（实测那台是 `D:\\ECHO` 配 `D:\\ECHO-delivery`）。
        os.path.join(install_root, "交付"),
        os.path.join(install_root, "delivery"),
        os.path.join(install_root, "dist"),
        os.path.join(install_root, "kit"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
    ]
    # 安装根的**同级**目录里那些看着像交付资料的（只看名字那一层，不做递归）：
    # 全盘递归既慢又容易捡到不相干的 zip，而"脚本和 zip 摆在一起"就只有这两三层。
    parent = os.path.dirname(install_root)
    if parent and os.path.isdir(parent):
        for pat in ("*delivery*", "*交付*", "*ECHO-kit*", "*后端包*"):
            for path in sorted(glob.glob(os.path.join(parent, pat))):
                if not os.path.isdir(path):
                    continue
                cands.append(path)
                # **交付目录里还有一层带日期的版本目录**（用户 2026-10-01 定的形态：
                # `<交付目录>\ECHO-delivery-<日期>-<时分>\{装我.cmd, ECHO-kit-*.zip, ECHO-backend-*.zip}`）。
                # 少了这一层会变成："包明明就在旁边，ECHO 却说没有、让你去下载"
                # —— 安装期是 `-BackendDir` 直接指到那一层才没露馅，事后自己再解包就找不到了。
                for sub in sorted(glob.glob(os.path.join(path, "*")))[:40]:
                    if os.path.isdir(sub):
                        cands.append(sub)
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


def _has_runtime_dir(names: List[str]) -> bool:
    """目录表里有没有 `<顶层>/runtime/`（= "这个包自己带运行时"）。"""
    for name in names:
        parts = [p for p in name.replace("\\", "/").split("/") if p]
        if len(parts) >= 2 and parts[1] == "runtime":
            return True
    return False


def _has_installed_deps(names: List[str]) -> bool:
    """目录表里 `<顶层>/runtime/` 下**装过依赖**吗（判据：site-packages 里有 fastapi）。

    为什么"有 `runtime/`"还不够：薄包也带 `runtime/`（只有解释器 + 标准库）。判"这是离线包"
    必须看**依赖装没装** —— 认错的表现是"复制启用完了还要下 3 GB"，比不认更让人困惑。
    为什么认 `fastapi` 而不是 `torch`：`fastapi` 是 `SERVER_IMPORTS` 里那个"必须能 import"
    的探针，而且它在 site-packages 里**总是在**；torch 的目录布局跨平台差异更大。
    布局不写死：只要求"路径里同时有 `site-packages` 与以 `fastapi` 开头的那一层"
    （Windows 是 `Lib/site-packages`，POSIX 是 `lib/python3.x/site-packages`）。
    """
    for name in names:
        parts = [p for p in name.replace("\\", "/").split("/") if p]
        if len(parts) < 3 or parts[1] != "runtime":
            continue
        if "site-packages" not in parts:
            continue
        if any(p == "fastapi" or p.startswith("fastapi-") for p in parts):
            return True
    return False


def _zip_names(zip_path: str) -> List[str]:
    """zip 的目录表（**只读中央目录，不解开**）；读不了就给空表。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return list(zf.namelist())
    except Exception:
        return []


def _is_offline_pack(zip_path: str) -> bool:
    """**后端离线包**的判据：像薄包 **且** `runtime/` 里装过依赖（两条都要）。"""
    names = _zip_names(zip_path)
    if not names:
        return False
    return (_has_runtime_dir(names) and _has_installed_deps(names)
            and _is_backend_package(zip_path))


def find_offline_zip() -> str:
    """在常见落点里找**后端离线包**（薄包 + 装好依赖的 `runtime/`）→ 路径；没有 = 空串。

    **名字只用来排序，判据全在内容上**（2026-10-01 定）：`OFFLINE_GLOBS` 命中的先看，
    没命中就按普通薄包的 `PACKAGE_GLOBS` 再看一遍 —— 因为
    `scripts/build_backend_portable.py --runtime-from` 产的**厚包**名字仍是
    `ECHO-backend-portable-*`，要求出包脚本改名才能被认出来是没必要的耦合
    （反过来："名字像离线包但里面只有解释器"照样不认，见 `_has_installed_deps`）。
    """
    for d in search_dirs():
        for pat in OFFLINE_GLOBS:
            for path in sorted(glob.glob(os.path.join(d, pat))):
                if _is_offline_pack(path):
                    return os.path.abspath(path)
        for pat in PACKAGE_GLOBS:
            for path in sorted(glob.glob(os.path.join(d, pat))):
                if _is_offline_pack(path):
                    return os.path.abspath(path)
    return ""


def find_package_zip() -> str:
    """在本机常见落点里找薄包 zip → 路径；没有 = 空串。

    **离线包优先**（2026-10-01）：它同时满足"薄包"与"依赖"两件事，先解开它就能让后面
    「取运行时」那一步直接过（零下载）。找不到离线包才退回普通薄包（只带解释器，
    依赖随后从国内源现装）。
    """
    offline = find_offline_zip()
    if offline:
        return offline
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
    """薄包这一步的**只读**计划（面板「取薄包」那一格 + `implemented` 的判据）。

    2026-10-01 加 `offline`：本机有没有**后端离线包**（薄包 + 装好依赖的 `runtime/`）。
    它决定「取运行时」那一格该说"零下载"还是"要从国内源下几 GB"—— 面板照 `plan()` 的
    `approxDownloadGB`/`headline` 显示，所以这两个数必须跟着它走。
    """
    root = backend_setup.backend_root()
    ready = os.path.isfile(os.path.join(root, "server", "requirements.txt"))
    explicit = package_source()
    found = "" if (ready or explicit) else find_package_zip()
    offline = find_offline_zip()
    if ready:
        headline = "薄包已解开：%s" % root
    elif explicit:
        headline = ("会按你指的去取薄包：%s（%s）"
                    % (explicit, "下载后解开" if explicit.lower().startswith("http")
                       else "就地解开"))
    elif found:
        headline = ("本机找到%s：%s —— 点下去会先解开它"
                    % ("后端离线包" if found == offline and offline else "薄包", found))
    else:
        headline = ("薄包还没有：本机这几个地方都没找到 %s-*.zip（%s）"
                    % (PACKAGE_PREFIX, "、".join(search_dirs()) or "没有可看的目录"))
    return {"ready": ready, "root": root, "explicit": explicit, "source": explicit or found,
            "search": search_dirs(), "log": package_log_path(),
            "offline": {"found": bool(offline), "path": offline,
                        "name": os.path.basename(offline) if offline else "",
                        "globs": list(OFFLINE_GLOBS)},
            "willFetch": bool(ready or explicit or found), "headline": headline}


def ensure_package(*, on_step: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """**第 −1 步：把薄包弄到 `{backend}`** → `(ok, detail)`。**永不抛。**

    顺序（每一步都写进 `{backend}/logs/fetch-package.log`）：

      ① 已经解开（`server/requirements.txt` 在）→ 成功；
      ② 显式指的（设置 `capabilityBackendPackage` / 环境变量 `ECHO_BACKEND_PACKAGE`）：
         路径 → 就地解开；http(s) → 先下载再解开；
      ③ 本机常见落点里有 zip → 解开。**离线包优先**（`find_package_zip()` 先找
         `find_offline_zip()`）：它同时带来"薄包那一半"与"装好依赖的 `runtime/`"，
         解一个包就把两件事都办了（零下载）；
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
    """**主源**（面板/计划里显示它；实际安装会按 `torch_indexes()` 依次试）。"""
    return TORCH_INDEX_TMPL % str(variant or "cu126").strip()


def torch_indexes(variant: str) -> List[str]:
    """torch/torchaudio 的索引**按顺序试**（第一个通了就不再往下）。"""
    v = str(variant or "cu126").strip()
    return [t % v for t in TORCH_INDEX_TMPLS]


def requirements(variant: str) -> Dict[str, Any]:
    """这一步要装什么（**分两阶段**，因为索引不同）→ 面板/日志共用一份。"""
    v = str(variant or "cu126").strip()
    base = os.path.join(backend_setup.backend_root(), "server", "requirements.txt")
    pin = VARIANT_TORCH.get(v, "")
    torch_pkgs = ["torch" + ("==%s" % pin if pin else ""),
                  "torchaudio" + ("==%s" % pin if pin else "")]
    return {"variant": v, "torchIndex": torch_index(v), "torchIndexes": torch_indexes(v),
            "torch": torch_pkgs,
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


# ---------------------------------------------------------------- 让解释器"能装东西"
# 2026-10-01 真机实测（薄包那台，两条叠在一起）：
#   ① 薄包的 `runtime/` 是**搬过来的 uv 托管 CPython**，里面留着 `Lib/EXTERNALLY-MANAGED`
#      —— PEP 668 的标记，**pip 一律拒绝安装**（`This environment is externally managed`）；
#   ② 那个 pip 本身还是**残缺**的（`No module named 'pip._internal.models'`，少了一整个子包）。
# 两条都会让"点一下就装依赖"失败，而报错长得像"网不通"或"包不存在" —— 所以这一步在装依赖**之前**
# 就地修好，并把原因写进面板。修复**全离线**：用 `ensurepip` 自带的那两个 wheel。
EXTERNALLY_MANAGED = "EXTERNALLY-MANAGED"


def runtime_root_for(exe: str) -> str:
    """从解释器路径反推**运行时根**：`runtime/python.exe`、`runtime/Scripts/python.exe`、
    `runtime/bin/python3` 都指回那个 `runtime/`（与 `backend_proc.PYTHON_RELS` 一一对应）。"""
    d = os.path.dirname(os.path.abspath(exe))
    if os.path.basename(d).lower() in ("scripts", "bin"):
        d = os.path.dirname(d)
    return d


def external_markers(exe: str) -> List[str]:
    """这个运行时里所有 `EXTERNALLY-MANAGED` 标记的路径（可能为空）。**只读。**

    按 `normcase(realpath)` 去重 —— Windows 上 `Lib\` 与 `lib\` 是**同一个目录**，
    不去重的话同一份文件会被列两次（面板上看着像有两个标记，删除时第二个还必然失败）。
    """
    root = runtime_root_for(exe)
    out: List[str] = []
    seen = set()
    for pat in (os.path.join(root, EXTERNALLY_MANAGED),
                os.path.join(root, "Lib", EXTERNALLY_MANAGED),
                os.path.join(root, "lib", EXTERNALLY_MANAGED),
                os.path.join(root, "lib", "python3.*", EXTERNALLY_MANAGED),
                os.path.join(root, "lib", "python3.*", "site-packages", EXTERNALLY_MANAGED)):
        for path in sorted(glob.glob(pat)):
            key = os.path.normcase(os.path.realpath(path))
            if key not in seen:
                seen.add(key)
                out.append(path)
    return out


def unmark_externally_managed(exe: str) -> List[str]:
    """删掉那些标记 → 返回删掉的路径。**永不抛**（删不掉就当没删，后面 pip 会如实报错）。"""
    removed: List[str] = []
    for path in external_markers(exe):
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def pip_works(exe: str, timeout: float = 120.0) -> Tuple[bool, str]:
    """``<exe> -m pip --version`` 跑不跑得通 → ``(ok, 原文)``。"""
    try:
        proc = subprocess.run([str(exe), "-m", "pip", "--version"],
                              capture_output=True, text=True, timeout=timeout, env=_pip_env())
    except Exception as e:
        return False, "跑 pip 失败：%s" % e
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return (proc.returncode == 0), out


#: 用 `ensurepip` 自带的那两个 wheel **离线**把 pip/setuptools 装回去。
#:
#: 为什么不能直接 `-m ensurepip --upgrade`（实测它也会失败）：ensurepip 把两个 wheel 放到
#: sys.path **最前面**再 `runpy` 跑 pip，但它**不删**site-packages 里那个残缺的 pip ——
#: 于是导入仍可能落到残缺那份上。这里显式 `--force-reinstall`，且用 `ensurepip._bundled`
#: 那个**真实目录**当 `--find-links`（`sys.path` 也直接指向 wheel 文件，实测这样导入稳）。
_PIP_BOOTSTRAP = (
    "import glob, os, runpy, sys\n"
    "import ensurepip\n"
    "d = os.path.join(os.path.dirname(ensurepip.__file__), '_bundled')\n"
    "sys.path[:0] = sorted(glob.glob(os.path.join(d, '*.whl')))\n"
    "sys.argv = ['pip', 'install', '--no-index', '--find-links', d, '--upgrade',\n"
    "            '--force-reinstall', 'pip', 'setuptools']\n"
    "runpy.run_module('pip', run_name='__main__', alter_sys=True)\n"
)


def ensure_pip(exe: str, *, on_step: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """**让这个解释器的 pip 真能用** → ``(ok, detail)``。**永不抛。**

    顺序：① 摘掉 PEP 668 标记；② `pip --version` 跑不通 → 用自带 wheel 离线装回 pip 与 setuptools；
    ③ 再验一次。为什么值得单独一步：这一步失败时**后端根本装不上依赖**，而它的报错最容易被
    误读成"网络问题"（`externally managed` / `No module named pip._internal.models` 都不提"包没装"）。
    """
    exe = str(exe or "")
    if not exe or not os.path.isfile(exe):
        return False, "没有解释器：%s" % (exe or "（空）")
    removed = unmark_externally_managed(exe)
    if removed and on_step:
        on_step("摘掉 PEP 668 标记（uv 托管的 CPython 会留它，pip 会拒绝安装）：%s"
                % "、".join(os.path.basename(p) for p in removed))
    ok, out = pip_works(exe)
    if ok:
        return True, "pip 可用（%s）" % (out.splitlines() or [""])[0].strip()
    tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-3:])
    if on_step:
        on_step("这个解释器的 pip 不能用了（%s）→ 用自带的 wheel 离线装回 pip" % (tail or "无输出"))
    try:
        proc = subprocess.run([exe, "-c", _PIP_BOOTSTRAP],
                              capture_output=True, text=True, timeout=PIP_TIMEOUT_S, env=_pip_env())
    except Exception as e:
        return False, "修 pip 时炸了：%s（原报错：%s）" % (e, tail)
    ok2, out2 = pip_works(exe)
    if ok2:
        return True, "pip 已就地修好（%s）" % (out2.splitlines() or [""])[0].strip()
    tail2 = "\n".join([ln for ln in ((proc.stdout or "") + (proc.stderr or "")).splitlines()
                       if ln.strip()][-5:])
    return False, ("这个解释器的 pip 用不了，也没能就地修好。\n原报错：%s\n修的时候：%s"
                   % (tail, tail2 or "（无输出）"))


def _pip_env() -> Dict[str, str]:
    """跑 pip 时的子进程环境：**强制 UTF-8 模式**。

    为什么（2026-10-01 真机实测，第一次真跑"装运行时"才炸）：

        pip install -r server/requirements.txt
        UnicodeDecodeError: 'gbk' codec can't decode byte 0xab in position 17

    `server/requirements.txt` 长期是「UTF-8 中文注释 + 没有 BOM、没有 PEP263 声明」，而 pip 的
    `_internal/utils/encoding.py::auto_decode()` 在两者都没有时**按 locale 解码**（中文 Windows =
    cp936）→ 撞上 UTF-8 字节就崩。报错完全看不出是编码问题，看着像"包坏了"或"网不通"。
    `PYTHONUTF8=1`（PEP 540）让 `locale.getpreferredencoding()` 变 UTF-8，**连已经发出去、
    带着旧文件的包也能装** —— 这一条管的是"别人手上的包"，所以**不能只靠去改文件**。
    （文件本身也补了 `# -*- coding: utf-8 -*-`，双保险；`tests/test_install_entry.py::
    RequirementsFilesAreLocaleSafe` 盯着。）
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_pip(exe: str, args: List[str], label: str,
             on_step: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """跑一条 pip，**输出同时落日志**（面板只显示最后几行 + 原文尾部）。"""
    cmd = [exe, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
           "--no-warn-script-location"] + args
    if on_step:
        on_step(label)
    _write_log([], "\n===== %s =====\n$ %s" % (time.strftime("%H:%M:%S"), " ".join(cmd)))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S,
                              env=_pip_env())
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

    ⚠️ **"有解释器"不等于"运行时能用"**（2026-10-01 真机修的那条）：薄包**只带解释器**，
    依赖要靠这一步装。所以第 ⓪ 步的跳过判据是 `backend_env.check_server_deps`
    （解释器 **+ `import fastapi, uvicorn` 过**），**不是** `backend_proc.python_exe()`
    —— 后者会让"解释器在、依赖没装"的那台机器直接跳过装依赖，后端一起来就
    `ModuleNotFoundError: No module named 'fastapi'` 退出。
    """
    v = str(variant or "cu126").strip()
    need = requirements(v)
    # ⓪ **已经能用就什么都不做**（判据见 docstring 末尾那段 ⚠️）。放在最前面是因为
    #    连薄包都不用去取 —— 运行时真能跑时，`server/requirements.txt` 与它无关。
    exe = backend_proc.python_exe()
    if exe:
        deps = backend_env.check_server_deps(exe)
        # **带了 torch 的运行时，还得看 torch 真的 import 得动**（2026-10-01 真机事故）。
        # 离线包的打包过滤曾经按目录名递归剪，把 `torch/utils/data`、`transformers/models`、
        # `funasr/models` 剪掉了；而 `check_server_deps()` 只探 fastapi/uvicorn，
        # 于是那份**残包**被判成"运行时已在"，永远不重新解包 —— 后端只能报
        # "模型要求 GPU，但 torch 看不到 CUDA"（**把打包问题说成显卡问题**），用户无路可走。
        # 判据：`site-packages/torch` 在 → `import torch, torch.utils.data` 就必须过；
        # 不过就当"没装完"往下走（第 ① 步会用本机离线包重解一份，零下载）。
        if deps["ok"]:
            broken = backend_env.check_packed_torch(exe)
            if not broken.get("ok"):
                if on_step:
                    on_step("运行时里的 torch 不完整（%s）→ 重新取运行时" % broken.get("error"))
                deps = {"ok": False, "error": broken.get("error")}
        if deps["ok"]:
            return True, "运行时已在（解释器 + %s 都能 import）：%s" % (
                "、".join(backend_env.SERVER_IMPORTS), exe)
        if on_step:
            # 这一行很重要：旧版本在这里会记「运行时已在」然后跳过装依赖，
            # 用户看到的是"后端起来后立刻退出了"。现在如实说"只装了一半"。
            on_step("运行时只装了一半（解释器在、%s import 不过）→ 按国内源补齐"
                    % "、".join(backend_env.SERVER_IMPORTS))
    # ① 先看**本机有没有后端离线包**（2026-10-01）：有就直接复制启用 —— **零下载**。
    #    顺序放在"取薄包 + pip"之前，因为离线包一旦解开，薄包那一半与依赖那一半就都到位了。
    #    （`find_package_zip()` 也会优先挑离线包，但那条路只在 `baseFileExists` 为假时才走；
    #     "薄包在、依赖不在、离线包也在"这一档只有这里能救 —— 而那正是用户的现场。）
    offline = find_offline_zip()
    if offline:
        if on_step:
            on_step("取运行时：本机有后端离线包 %s —— 直接解开，不下载"
                    % os.path.basename(offline))
        ok, detail = extract_package(offline, on_step=on_step)
        if not ok:
            # 离线包坏了**不该**把整件事判死：说出来，然后照旧走国内源下载。
            if on_step:
                on_step("离线包没能启用（%s）→ 改走国内源下载" % detail)
        else:
            exe2 = backend_proc.python_exe()
            deps2 = backend_env.check_server_deps(exe2) if exe2 else {"ok": False, "error": ""}
            if deps2["ok"]:
                return True, "运行时已在（本机离线包 %s，零下载）：%s" % (
                    os.path.basename(offline), exe2)
            if on_step:
                on_step("离线包解开了，但运行时还是不能用（%s）→ 改走国内源下载"
                        % (deps2.get("error") or "").strip()[:200])
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
    # ②' 装依赖**之前**先让解释器"能装东西"：摘 PEP 668 标记 + 必要时离线装回 pip。
    #     放在最前面是因为它失败时后面每一步都必然失败，而它自己的报错最像"网络问题"。
    ok, detail = ensure_pip(exe, on_step=on_step)
    if not ok:
        return False, detail
    steps: List[str] = []
    if need["note"]:
        steps.append(need["note"])

    # ① torch + torchaudio：**同一条 pip、同一个 CUDA 索引**（纪律 1）。
    #    索引**按顺序试**（`torch_indexes`）：只有一个源时，源那侧抖一下（实测 SJTU 会间歇性
    #    撞 `[SSL] record layer failure`）用户看到的就是"装 torch 失败" —— 换一个源就好。
    last = ""
    for index in torch_indexes(v):
        ok, detail = _run_pip(exe, need["torch"] + ["--index-url", index],
                              "装 torch/torchaudio（%s）" % index, on_step)
        if ok:
            last = detail
            break
        last = detail
    else:
        return False, last

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

    含**两个**载荷的账：先取薄包（`package`），再装运行时 —— 因为
    `backend_env.plan()['implemented']` 与面板那几行都靠它说清楚"点下去到底会发生什么"。

    ``approxDownloadGB`` 与 ``headline`` 是**说实话的那两个数**（2026-10-01）：本机有后端
    离线包时它们是 `0` 与"用本机离线包（零下载）"；没有才是"3 GB / 从国内源装"。
    面板照这两个字段显示，所以**别把它们写成常量** —— 那会让"有离线包"的机器看到一句假的
    "要下 3 GB"，正是本次要修的那类"说错原因"。
    """
    need = requirements(variant)
    pkg = package_plan()
    offline = pkg["offline"]
    return {"variant": need["variant"], "torchIndex": need["torchIndex"],
            "torchIndexes": need["torchIndexes"],
            "torch": need["torch"], "extras": need["extras"],
            "pipIndexes": need["pipIndexes"], "note": need["note"],
            "runtimeDir": runtime_dir(), "log": log_path(),
            "package": pkg,
            "packageReady": pkg["ready"], "packageSource": pkg["source"],
            "packageSearch": pkg["search"], "packageLog": pkg["log"],
            "offline": offline,
            "willFetch": pkg["willFetch"],          # 整条路现在能不能一键走完
            "python": backend_proc.python_exe(),
            "sourceReady": need["baseFileExists"],
            # 估：torch+torchaudio+nvidia 库（实测本机 6 GB 解压后）；有离线包 = 0。
            "approxDownloadGB": 0.0 if offline["found"] else 3.0,
            "headline": ("用本机那个后端离线包（%s）—— **零下载**，解开就能用"
                         % (offline["name"] or offline["path"])) if offline["found"]
                        else ("从国内源装运行时（torch/torchaudio 走 %s（不通就换下一个），"
                              "其余走清华/阿里 PyPI）" % need["torchIndex"])}
