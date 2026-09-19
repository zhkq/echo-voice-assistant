# -*- coding: utf-8 -*-
"""paths.py — ECHO 2.0 的统一路径解析层（P1；对应 D18–D21）

为什么要有这一层
----------------
1.x 里业务路径是**import 期常量**，散落在各模块：

    stt.py:21      MODELS_DIR = BASE_DIR/models
    meeting.py:31  MEETINGS_DIR = BASE_DIR/data/meetings
    db.py:37       DATA_DIR = BASE_DIR/data

于是"会议目录 / 模型目录"用户改不了，data 布局也被写死在安装目录下（macOS 上不该
往 .app 里写）。2.0 把四类根分开（D18/D20）：

    {ECHO}      代码所在目录（安装目录），只读语义
    {DATA}      系统数据（echo.db / logs / pid），平台相关
    {MEETINGS}  会议录音与纪要，**用户可指定**
    {MODELS}    模型权重，**用户可指定**

约定（新代码请遵守，P1 起逐步收拢旧代码）
------------------------------------------
1. **只有本模块**做 `~` 展开、占位符替换、相对路径补全、平台默认值判定。
   其它模块一律通过 ``data_root() / meetings_root() / models_root() / resolve()`` 取路径。
2. **不要在任何模块顶层缓存这些函数的返回值**（那等于把 1.x 的 import 期常量换个地方）。
   需要缓存就用 ``app/config.py`` 的配置项 + 失效通知，或在函数内临时取。
3. 配置值支持 ``~``、``{ECHO}``、``{DATA}``；也允许 ``{MEETINGS}``/``{MODELS}`` 出现在
   **别的**配置项里（如 worklog 提示文案），但禁止它们互相自引用。
4. 越界校验统一走 ``pathutil.safe_under(base, *parts)``，而 ``base`` 应当是**请求时**
   解析出来的根（D21），不是模块常量。
5. 目录校验与"危险位置"判定见 ``validate_dir()``；是否强制 ASCII 由 S11 的结论决定，
   因此这里只给 ``ascii_warning()``，由调用方（面板/预检）决定拦不拦。
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

# ------------------------------------------------------------------ 根目录

def echo_root() -> str:
    """代码所在目录（安装目录）。

    默认**由 ``__file__`` 推导**——"代码在哪，根就在哪"，这就是全系统内部一律
    相对根书写的前提：整棵树挪到任何地方都自洽，没有任何一处写死安装路径。

    环境变量 ``ECHO_ROOT`` 可覆盖，用于打包分发（程序目录只读、数据放别处）与
    多实例/测试。**刻意只做成环境变量，不做面板配置项**：安装根配错的后果是全盘
    静默跑偏（模型找不到、数据写错地方、门禁测的不是这棵树），
    ``scripts/startup.ps1`` 里"某棵树悄悄用了另一棵树的 venv"就是这个事故类型。
    用户该配的是**数据类**目录：``ECHO_DATA`` / ``meetingsDir`` / ``modelsDir``。
    """
    override = os.environ.get("ECHO_ROOT")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _platform_defaults() -> dict:
    """当前平台的默认值（见 app/platform/）。测试可直接替换本函数。"""
    try:
        from app import platform as _p
        return _p.defaults()
    except Exception:
        return {}


def data_root() -> str:
    """系统数据根（D18）。

    Windows：``{ECHO}/data``（与 1.x 一致，不惊动老用户）
    macOS  ：``~/Library/Application Support/ECHO``（.app 内不可写，且不应写）
    其它   ：XDG ``$XDG_DATA_HOME/ECHO``

    平台默认值由 ``app/platform/<os>/env.py`` 提供——本模块**不含平台分支**（D10–D12）。
    环境变量 ``ECHO_DATA`` 优先，测试与多实例隔离都用它。
    """
    override = os.environ.get("ECHO_DATA")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    spec = _platform_defaults().get("dataDir") or "{ECHO}/data"
    # 注意：这里**不能**用 expand()——expand 的占位符表里含 {DATA}，而 {DATA} 就是
    # data_root() 自己，会无限递归。只展开 ~ 与 {ECHO}。
    return os.path.normpath(os.path.expanduser(spec).replace("{ECHO}", echo_root()))


# ------------------------------------------------------------------ 占位符

def _placeholder_map(extra: Optional[dict] = None) -> dict:
    m = {"ECHO": echo_root(), "DATA": data_root()}
    if extra:
        m.update({k: v for k, v in extra.items() if v})
    return m


def expand(spec: str, extra: Optional[dict] = None) -> str:
    """展开 ``~`` 与 ``{ECHO}``/``{DATA}``（以及 ``extra`` 里的键）。

    只做展开，不做绝对化——``resolve()`` 才补全相对路径。
    """
    if spec is None:
        return ""
    out = os.path.expanduser(str(spec).strip())
    for key, val in _placeholder_map(extra).items():
        out = out.replace("{%s}" % key, val)
    return out


def resolve(spec: str, *, base: Optional[str] = None, extra: Optional[dict] = None) -> str:
    """把配置里的路径写法解析成绝对路径。

    * 空值 → ``""``（调用方决定回落到哪个默认值）
    * 支持 ``~`` 与占位符
    * 相对路径相对 ``base``（默认：ECHO 根）补全
    * 含 ``..`` 段一律拒绝（配置里不该出现，出现即为错写或试探）
    """
    text = expand(spec, extra)
    if not text:
        return ""
    if ".." in text.replace("\\", "/").split("/"):
        raise ValueError("path must not contain '..': %r" % spec)
    if not os.path.isabs(text):
        text = os.path.join(base or echo_root(), text)
    return os.path.normpath(text)


# ------------------------------------------------------------------ 四类根

def _settings_get(name: str) -> str:
    """读配置项（惰性导入，避免 config ↔ paths 循环导入）。测试可替换本函数。"""
    try:
        from app import config
        return str(config.settings.get(name) or "")
    except Exception:
        return ""


def meetings_root() -> str:
    """会议录音与纪要根（D20：用户可指定）。

    未配置 → ``{DATA}/meetings``。
    """
    spec = _settings_get("meetingsDir")
    resolved = resolve(spec) if spec else ""
    return resolved or os.path.join(data_root(), "meetings")


def models_root() -> str:
    """模型权重根（D20：用户可指定）。未配置 → ``{ECHO}/models``。"""
    spec = _settings_get("modelsDir")
    resolved = resolve(spec) if spec else ""
    return resolved or os.path.join(echo_root(), "models")


def hf_home() -> str:
    """HuggingFace 缓存根（D30）。用户**显式配置**了 modelsDir 时返回 ``models_root()``，
    否则返回空串。

    **空串是刻意的“无操作”信号**：默认安装下 HF_HOME 已经是 ``{ECHO}/models``，与
    ``models_root()`` 逐字相同，覆盖它没有收益；而 HF 缓存根一旦指错，huggingface
    会找不到已下好的权重并**重新下载**（几 GB）。所以只有用户真的把 modelsDir 配到
    别处时才覆盖（D30：保证默认安装零行为变化）。
    """
    return models_root() if _settings_get("modelsDir") else ""


def active_roots() -> dict:
    """当前生效的四类根 + 配置里是不是用户指定的。面板"环境体检"页与诊断用它。

    **任何一项取不到都不抛异常**：配置坏掉时，体检页恰恰是最需要能打开的那个页面。
    """
    def safe(fn, default=""):
        try:
            return fn()
        except Exception:
            return default

    return {
        "echo": safe(echo_root),
        "data": safe(data_root),
        "meetings": safe(meetings_root),
        "models": safe(models_root),
        "meetingsConfigured": bool(safe(lambda: _settings_get("meetingsDir"))),
        "modelsConfigured": bool(safe(lambda: _settings_get("modelsDir"))),
    }


# ------------------------------------------------------------------ 校验

def is_ascii(path: str) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except (UnicodeEncodeError, AttributeError):
        return False


def ascii_warning(path: str) -> str:
    """非 ASCII 路径的提示（**是否拦截由调用方决定**，见 S11）。"""
    if is_ascii(path):
        return ""
    return "路径含非 ASCII 字符：部分推理引擎/工具对中文路径支持不一致，若遇诡异失败可换纯英文路径"


def _dangerous(path: str) -> str:
    """返回危险原因，空串表示不是危险位置。

    平台相关的"禁区前缀"由 ``app/platform/<os>/env.py`` 提供（D10–D12）。
    """
    real = os.path.normpath(os.path.abspath(path))
    drive, tail = os.path.splitdrive(real)
    if not tail.strip("\\/") or real == drive + os.sep:
        return "不能是磁盘根目录"
    parts = [p.lower() for p in real.replace("/", "\\").split("\\") if p]
    home = [p.lower() for p in os.path.expanduser("~").replace("/", "\\").split("\\") if p]
    if parts == home:
        return "不能是用户主目录本身"
    try:
        from app import platform as _p
        prefixes = _p.dangerous_prefixes()
    except Exception:
        prefixes = []
    for prefix, why in prefixes:
        if not prefix:
            continue
        if os.path.normcase(real).startswith(os.path.normcase(os.path.abspath(prefix))):
            return why
    return ""


def validate_dir(path: str, *, create: bool = False, require_ascii: bool = False) -> Tuple[bool, str]:
    """校验一个"用户可指定的目录"（D21）。返回 ``(ok, reason)``，``reason`` 为空表示通过。

    * 必须是绝对路径（相对路径先过 ``resolve()``）
    * 拒绝磁盘根 / 主目录本身 / 系统目录 / Program Files
    * 已存在时必须是目录，且可写（用一次"建了就删"的探针，不依赖平台权限模型）
    * ``create=True`` 时按需创建父目录
    * ``require_ascii=True`` 时非 ASCII 直接拒绝；默认只由 ``ascii_warning()`` 提示
    """
    if not path or not str(path).strip():
        return False, "路径不能为空"
    try:
        real = resolve(str(path))
    except ValueError as exc:
        return False, str(exc)
    if not os.path.isabs(real):
        return False, "必须是绝对路径"
    bad = _dangerous(real)
    if bad:
        return False, bad
    if require_ascii and not is_ascii(real):
        return False, ascii_warning(real)
    if os.path.exists(real) and not os.path.isdir(real):
        return False, "目标已存在但不是目录"
    if not os.path.isdir(real):
        if not create:
            return False, "目录不存在"
        try:
            os.makedirs(real, exist_ok=True)
        except OSError as exc:
            return False, "无法创建目录：%s" % exc
    probe = os.path.join(real, ".echo-write-probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        os.remove(probe)
    except OSError as exc:
        return False, "目录不可写：%s" % exc
    return True, ""


def preflight() -> dict:
    """环境体检：四类根的位置、存在性、可写性、磁盘余量。

    **任何异常都不抛**——面板要靠它渲染体检页，配置/环境坏掉时更得能显示出来。
    """
    import shutil

    def safe(fn):
        try:
            return fn()
        except Exception:
            return ""

    out = {"roots": [], "ok": True}
    pairs = (("ECHO", echo_root()), ("DATA", data_root()),
             ("MEETINGS", safe(meetings_root)), ("MODELS", safe(models_root)))
    for name, path in pairs:
        item = {"name": name, "path": path, "exists": bool(path) and os.path.isdir(path),
                "ascii": is_ascii(path), "writable": False, "freeGB": None, "note": ""}
        if not path:
            item["note"] = "无法解析该根（配置或环境异常）"
            out["roots"].append(item)
            if name in ("ECHO", "DATA"):
                out["ok"] = False
            continue
        try:
            ok, why = validate_dir(path, create=False)
            item["writable"] = ok
            if not ok:
                item["note"] = why
                # 会议/模型目录"还不存在"是正常的（首次录音/首次下模型才会建），
                # 只有 ECHO/DATA 不可写才算体检失败。
                if name in ("ECHO", "DATA"):
                    out["ok"] = False
        except Exception as exc:                      # 体检自己不能挂
            item["note"] = "%s: %s" % (type(exc).__name__, exc)
        try:
            anchor = path if os.path.isdir(path) else os.path.dirname(path)
            while anchor and not os.path.isdir(anchor):
                anchor = os.path.dirname(anchor)
            if anchor:
                item["freeGB"] = round(shutil.disk_usage(anchor).free / (1024 ** 3), 1)
        except Exception:
            pass
        out["roots"].append(item)
    return out
