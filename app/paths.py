# -*- coding: utf-8 -*-
"""paths.py — ECHO 的统一路径解析层（P1；对应 D18–D21；3.0 加安装根布局）

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

3.0 再往上加一层**安装根**（`3.0-设计总览与组件关系.md` §2.2）：`{echoBase}` 下面
六个兄弟目录，各管一类东西，谁都不许塞进 `echo-core/`：

    {echoBase}/
    ├── echo-core/   ECHO 代码 + 面板 + 随包脚本   → 升级**整体覆盖**
    ├── data/        echo.db / logs / pid / token  → 必须保留
    ├── models/      模型权重（几 GB）             → 必须保留
    ├── dsh/
    │   ├── app/     DSH 标准版本体（本地安装那份）
    │   └── home/    DSH_HOME（skills / 会话 / settings.yaml / storages）
    ├── meeting/     会议数据 **并**作「会议空间」工作区
    └── aide/        指令数据 **并**作「指令空间」工作区

**老装机（扁平布局）必须继续能用**：没有安装根时，下面每个 `*_root()` 都逐字回落到
2.0 的路径（`{DATA}/meetings`、`{ECHO}/models`、`{ECHO}/data/command`、`{ECHO}/harness/dsh`），
一个字节都不挪。判据见 `echo_base()`。

约定（新代码请遵守，P1 起逐步收拢旧代码）
------------------------------------------
1. **只有本模块**做 `~` 展开、占位符替换、相对路径补全、平台默认值判定。
   其它模块一律通过 ``data_root() / meetings_root() / models_root() / resolve()`` 取路径。
2. **不要在任何模块顶层缓存这些函数的返回值**（那等于把 1.x 的 import 期常量换个地方）。
   需要缓存就用 ``app/config.py`` 的配置项 + 失效通知，或在函数内临时取。
3. 配置值支持 ``~``、``{ECHO}``、``{ECHO_BASE}``、``{DATA}``；也允许 ``{MEETINGS}``/``{MODELS}``
   出现在**别的**配置项里（如 worklog 提示文案），但禁止它们互相自引用。
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


# ------------------------------------------------- 安装根（{echoBase}）与六个兄弟目录

#: 安装根下六个兄弟目录（设计 §2.2）。**名字是契约**：迁移表按它写。
BASE_DIRS = {
    "core": "echo-core",
    "data": "data",
    "models": "models",
    "dsh": "dsh",
    "meeting": "meeting",
    "aide": "aide",
}


def echo_base() -> str:
    """安装根 ``{echoBase}``。**空串 = 老式扁平安装**（代码就在安装根下）。

    判定顺序（都不写死安装路径）：

    1. 环境变量 ``ECHO_BASE``（测试、多实例、非常规布局）；
    2. **代码目录叫 `echo-core`** → 它的父目录就是安装根。布局**自描述**，新装机
       不必配任何东西：把 `echo-core/` 放到哪儿，六个兄弟目录就跟到哪儿；
    3. 其余一律返回空串 —— 老装机（代码直接放在安装根下，例如 `D:\\ECHO\\app`）
       与新机器上的开发树都属于这一类，**行为与 2.0 逐字一致**。

    为什么不做成面板设置项：与 ``echo_root()`` 同一个理由 —— 安装根配错的后果是全盘
    静默跑偏，而"代码目录叫什么名字"这件事本身就足以判定了，不需要人再告诉它一遍。
    """
    override = os.environ.get("ECHO_BASE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    root = echo_root()
    if os.path.basename(root).lower() == BASE_DIRS["core"]:
        return os.path.dirname(root)
    return ""


def base_dir(name: str) -> str:
    """安装根下的某个兄弟目录。**没有安装根时返回空串** —— 调用方据此回落到老路径。"""
    if name not in BASE_DIRS:
        raise KeyError("未知的兄弟目录：%r（可选：%s）" % (name, ", ".join(sorted(BASE_DIRS))))
    base = echo_base()
    return os.path.join(base, BASE_DIRS[name]) if base else ""


def data_root() -> str:
    """系统数据根（D18）。

    新布局：``{echoBase}/data``（三个平台一样 —— 安装根是用户挑的那个盘）。
    老布局：Windows ``{ECHO}/data``；macOS ``~/Library/Application Support/ECHO``；
    其它 XDG ``$XDG_DATA_HOME/ECHO``（平台默认值由 ``app/platform/<os>/env.py`` 提供，
    本模块**不含平台分支**，D10–D12）。

    环境变量 ``ECHO_DATA`` 优先，测试与多实例隔离都用它。
    """
    override = os.environ.get("ECHO_DATA")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = echo_base()
    if base:
        return os.path.join(base, BASE_DIRS["data"])
    spec = _platform_defaults().get("dataDir") or "{ECHO}/data"
    # 注意：这里**不能**用 expand()——expand 的占位符表里含 {DATA}，而 {DATA} 就是
    # data_root() 自己，会无限递归。只展开 ~ 与 {ECHO}。
    return os.path.normpath(os.path.expanduser(spec).replace("{ECHO}", echo_root()))


def dsh_root() -> str:
    """DSH **本体**（本地永久安装的那份）：``{echoBase}/dsh/app``。

    老布局：``{ECHO}/harness/dsh``（2.0/3.0 一直是这样，别搬 —— 搬了等于让老装机
    重新下一遍 223 MB）。
    """
    d = base_dir("dsh")
    return os.path.join(d, "app") if d else os.path.join(echo_root(), "harness", "dsh")


def dsh_home_root() -> str:
    """**DSH_HOME**（skills / 会话 / settings.yaml / storages）：``{echoBase}/dsh/home``。

    为什么与本体并排、而不是塞进 `aide/`：DSH 会把自己的会话存储当工作区内容，
    放进工作区等于让 agent 读到自己的存档（设计 §2.2 的 A/B 两案，选了 A）。
    老布局：``{DATA}/harness``。
    """
    d = base_dir("dsh")
    return os.path.join(d, "home") if d else os.path.join(data_root(), "harness")


def meetings_root() -> str:
    """会议录音与纪要根（D20：用户可指定）。它**同时**是「会议空间」工作区。

    未配置 → 新布局 ``{echoBase}/meeting``；老布局 ``{DATA}/meetings``。
    3.0 起 `meetingsDir` 与 `meetingWorkspace` **合并成一项** —— 见 `workspaces.space_specs()`：
    两条路到达同一目录从"巧合"变成不变量（改了会议目录，DSH 工作区跟着走）。
    """
    spec = _settings_get("meetingsDir")
    resolved = resolve(spec) if spec else ""
    if resolved:
        return resolved
    return base_dir("meeting") or os.path.join(data_root(), "meetings")


#: 指令工作区的**出厂值** —— 它们的意思是"自动挑"，不是某个具体路径。
#: `{ECHO}/data/command` 是 2.0 的出厂值（老装机的库里存的就是这个字符串），
#: `{ECHO_BASE}/aide` 是新布局的出厂值。两个都当"用户没配过"。
_WORKSPACE_FACTORY_VALUES = ("", "{ECHO}/data/command", "{ECHO_BASE}/aide")


def command_root() -> str:
    """指令空间：新布局 ``{echoBase}/aide``，老布局仍 `{ECHO}/data/command`（**不搬家**）。

    为什么要按**值**判断"配没配过"：设置库里存的是字符串，老装机存的就是
    `{ECHO}/data/command`。只看"非空"的话，老装机一升级工作区就跳到 `{ECHO}/aide` ——
    老会话留在原处、新会话跑到新目录，用户看到的是"侧栏里多了一个空分组"，
    而没有任何地方会报错。
    """
    spec = str(_settings_get("commandWorkspace") or "").strip()
    if spec not in _WORKSPACE_FACTORY_VALUES:
        return resolve(spec) or ""
    return base_dir("aide") or os.path.join(echo_root(), "data", "command")


def models_root() -> str:
    """模型权重根（D20：用户可指定）。

    未配置 → 新布局 ``{echoBase}/models``；老布局 ``{ECHO}/models``。
    """
    spec = _settings_get("modelsDir")
    resolved = resolve(spec) if spec else ""
    return resolved or base_dir("models") or os.path.join(echo_root(), "models")


#: 「会议空间」工作区的出厂值（与 `meetingWorkspace` 那一项的 DEFAULTS 对应）
_MEETING_SPACE_FACTORY_VALUES = ("", "{ECHO}/data/meetings", "{ECHO_BASE}/meeting",
                                 "{DATA}/meetings")


def meeting_space_root() -> str:
    """「会议空间」工作区目录 —— **就是会议数据目录**（3.0 把两项合并成一项）。

    设计 `§2.2` 把它写成一条规则：**`meeting/` 同时是数据目录与工作区**。原来的两条设置
    （`meetingsDir` 管文件、`meetingWorkspace` 管 DSH 会话登记到哪）"今天恰好指向同一个
    目录"，而用户一旦按说明去改 `meetingsDir`，DSH 工作区仍停在老路径 ——
    **DSH 就看不到会议文件了，而且没有任何地方会报错**。现在这条不变量由本函数保证。

    **老装机兼容**：只有当 `meetingsDir` 没配、而 `meetingWorkspace` **被改过**
    （不是出厂值）时才用后者 —— 那种用户我们不该搬他的会话。
    """
    if not str(_settings_get("meetingsDir") or "").strip():
        spec = str(_settings_get("meetingWorkspace") or "").strip()
        if spec not in _MEETING_SPACE_FACTORY_VALUES:
            return resolve(spec) or ""
    return meetings_root()



# ------------------------------------------------------------------ 占位符

def _placeholder_map(extra: Optional[dict] = None) -> dict:
    # `{ECHO_BASE}` 在老布局下与 `{ECHO}` 同义：出厂值里写 `{ECHO_BASE}/aide` 时，
    # 老装机至少能解析出一个合理的绝对路径（而不会留着一个没展开的花括号）。
    # 真正的"新老分派"在各 `*_root()` 里（见那一节）。
    m = {"ECHO": echo_root(), "ECHO_BASE": echo_base() or echo_root(),
         "DATA": data_root()}
    if extra:
        m.update({k: v for k, v in extra.items() if v})
    return m


def expand(spec: str, extra: Optional[dict] = None) -> str:
    """展开 ``~`` 与 ``{ECHO}``/``{ECHO_BASE}``/``{DATA}``（以及 ``extra`` 里的键）。

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


# ------------------------------------------------------------------ HF 缓存根

def hf_home() -> str:
    """HuggingFace 缓存根（D30）。用户**显式配置**了 modelsDir 时返回 ``models_root()``，
    否则返回空串（"不动"）。

    ⚠️ **前提变了（3.0 安装根）**：D30 那会儿"默认安装下 HF_HOME 已经是 ``{ECHO}/models``"，
    所以"不动"就等于正确。新布局下代码根是 `{echoBase}/echo-core`，**HF_HOME 的兜底值
    必须由 `modelinfo` 用 `models_root()` 设**（它已经是了）—— 这里只负责"用户显式配了
    modelsDir 就覆盖掉"。两边都不许写死 `{ECHO}/models`：那会把几 GB 权重下进代码目录，
    而 `models_root()` 去别处找（2026-09-24 清点出来的头号风险，见 `modelinfo` 的注释）。
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
        "echoBase": safe(echo_base),
        "echo": safe(echo_root),
        "data": safe(data_root),
        "meetings": safe(meetings_root),
        "models": safe(models_root),
        "aide": safe(command_root),
        "dshInstall": safe(dsh_root),
        "dshHome": safe(dsh_home_root),
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
    # 3.0 起把安装根与 dsh/aide 也列出来：布局变了之后，"我的东西到底在哪"必须一眼看到
    # （面板「环境体检」用的就是这份）。**只有 ECHO/DATA 不可写才算体检失败** ——
    # 会议/模型/指令/DSH 目录"还不存在"是正常的（首次录音/首次下模型/首次装 DSH 才建）。
    pairs = (("ECHO_BASE", safe(echo_base)), ("ECHO", echo_root()),
             ("DATA", safe(data_root)), ("MEETINGS", safe(meetings_root)),
             ("MODELS", safe(models_root)), ("AIDE", safe(command_root)),
             ("DSH", safe(dsh_root)), ("DSH_HOME", safe(dsh_home_root)))
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
