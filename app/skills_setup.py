# -*- coding: utf-8 -*-
"""skills_setup.py — 把**随包分发**的技能与**家目录说明文件**装进各 DSH 家目录。

为什么需要它（2026-10-11 用户指出的坑）
--------------------------------------
随包的技能本来是跟着**代码树**走的（主包白名单里有 `.dsh`，所以安装后它们在
`<代码目录>/.dsh/skills/<名字>/`），而 ECHO 的 agent 是从 **`DSH_HOME/skills/`** 读技能的：
标准版 harness 的 `DSH_HOME` 是 `{echoBase}/dsh/home`（老布局 `{DATA}/harness`），
桌面版是 `~/.dsh`。**这是两个不同的目录** —— 于是新装的机器上，连随包的
`meeting-record` 其实都**没生效**（"新用户安装后没有 skill"的根因）。

为什么还要装 `AGENTS.md`（2026-10-16 用户要求"把机制内置进默认安装"）
--------------------------------------------------------------------
同一天用户把「**专有名词与姓名纠错机制**」落在两个 **`AGENTS.md`** 上：
`$DSH_HOME/AGENTS.md`（agent **每个会话自动加载**的全局指令）与会议工作区的
`{MEETINGS}/AGENTS.md`。技能是"**被点名才加载**"的，而机制要"**默认就在**"——
所以它必须由安装阶段写进家目录，否则新机器上又是「技能装了、机制没有」（与上面同形的一个坑）。
源文件在 `<代码目录>/.dsh/home-defaults/**`（刻意**不叫 `AGENTS.md`**：否则 DSH 会把
"模板"当成本仓库的活指令加载）。

三条铁律
--------
1. **只补缺、绝不覆盖**：用户可能自己精心调过 `daily-review`、也可能自己写了一份
   `AGENTS.md`（本仓库作者就是这样）。目标已存在就跳过，并把"保留了你那份"如实报出来。
2. **只装随包清单里的**（:data:`SHIPPED` / :data:`HOME_FILES` / :data:`WORKSPACE_FILES`）：
   不要把用户自己放进 `skills/` 或家目录的任何东西当素材。
3. **家目录的 `AGENTS.md` 比技能更严**：那是**用户自己的指令文件**，所以只有
   "目标不存在"或"内容逐字等于**我们自己写下的旧版本**"（:data:`PREVIOUS_DEFAULTS`）才动它 ——
   用户改过一个字就不再是我们那份（见 :func:`_install_one`）。
"""
import os
import shutil

from app import paths

#: 随包分发的技能：`.dsh/skills/<名字>/` → `<DSH_HOME>/skills/<名字>/`
#:
#: 清单刻意**短**：只放"功能直接相关、且不含个人/单位信息"的骨架。
#: `obsidian-worklog` / `wecom-file-to-obsidian` 那两个**故意不分发** —— 它们把作者本人的
#: 姓名、OneDrive 路径与单位会议类型写死在正文里（公开仓库不能带这些）。
SHIPPED = ("meeting-record", "daily-review", "meeting-archive")


# ---------------------------------------------------------------- 家目录说明文件（AGENTS.md）
#
# 2026-10-16：用户把「专有名词与姓名纠错机制」落在 `$DSH_HOME/AGENTS.md`（**每个会话自动加载**）
# 与会议工作区的 `AGENTS.md` 上 —— 目标机器上这两个文件**本来不存在**，所以必须由安装阶段写。
# 源文件在 `<代码目录>/.dsh/home-defaults/**`，源文件**故意不叫 `AGENTS.md`**：名字一叫
# `AGENTS.md`，DSH 就会把"模板"当成本仓库的活指令加载（本轮实测过，确实会加载）。
#
#: 清单项：(报给用户的名字, 源文件相对 home-defaults, 目标相对目录, 作用)
HOME_FILES = (
    ("agents-md", "AGENTS.default.md", "AGENTS.md",
     "DSH 家目录全局指令（每个会话自动加载）"),
)

#: 装进**会议工作区**（`paths.meetings_root()`）的说明文件 —— **只一处**，不是每个家目录一份。
WORKSPACE_FILES = (
    ("meeting-agents-md", os.path.join("meeting", "AGENTS.default.md"), "AGENTS.md",
     "会议工作区指令（在会议目录里做转写/纪要/JSON 时加载）"),
)

#: 我们写下的文件第一行长这样（`vN` 是**内容版本**）。它的用途**只是**让人/面板看清
#: "这份是内置默认"（:func:`_is_builtin`）；**不拿它当覆盖判据** —— 判据是内容，见下面。
BUILTIN_MARK = "<!-- echo-builtin:"

#: 「我们自己写下的**旧版本**」全文 —— **只有与这里逐字（换行归一后）相同**才会被升级。
#:
#: 为什么判据是**内容**而不是第一行标记：用户完全可能就在这份文件上直接加自己的口径 ——
#: 他动过一个字，这份就不再是"我们那份"了（哪怕标记还在）。只认逐字相同 = 宁可少升一次，
#: 也**绝不覆盖用户的话**。
#:
#: 将来改默认内容时：把**上一版全文**拷进对应元组（新增一条，**别删旧的** —— 老机器靠它认自己那份）。
#: 版本 1 是初版，所以现在还没有旧版本；升级这条路由 `tests/test_home_defaults.py` 用注入的
#: 假旧版本钉住（不靠"以后再说"）。
PREVIOUS_DEFAULTS = {
    # "agents-md": ("<上一版 AGENTS.default.md 的全文>",),
    # "meeting-agents-md": ("<上一版 …>",),
}


def code_skills_root() -> str:
    """技能**源**目录：`<安装根>/.dsh/skills`。

    委托给 `paths.shipped_skills_root()` —— **安装根只能由 pp/paths.py 推导**（D29），
    别在这里用 `__file__` 或写死盘符（`tests/test_path_seam.py` 会红）。
    """
    return paths.shipped_skills_root()


def target_homes() -> list:
    """要装进哪些 DSH 家目录。

    两处都要照顾，因为 ECHO 两个智能体都可能被选中：
      * `llm_router.dsh_homes()` —— **实际存在**的家目录（桌面版 / 标准版；判据是里面
        已经有 `settings.yaml` 或 `.credentials.yaml`，见那边的说明）；
      * `harness_proc.home()` —— ECHO 自己拉起的那个 harness 的家目录：它**可能还没建**
        （第一次要用才建），所以不能只依赖上面那个列表。
    去重后返回（同一个路径只算一处）。
    """
    out, seen = [], set()

    def add(p):
        p = str(p or "").strip()
        if p and os.path.normcase(os.path.abspath(p)) not in seen:
            seen.add(os.path.normcase(os.path.abspath(p)))
            out.append(p)

    try:
        from app import llm_router
        for h in llm_router.dsh_homes():
            add((h or {}).get("home"))
    except Exception:
        pass
    try:
        from app import harness_proc
        add(harness_proc.home())
    except Exception:
        pass
    return out


def _sources() -> dict:
    """随包清单里**真的有**的那些（名字 → 源目录）。没有的单独报出来，不静默跳过。"""
    root = code_skills_root()
    out = {}
    for name in SHIPPED:
        d = os.path.join(root, name)
        if os.path.isdir(d):
            out[name] = d
    return out


def all_skills(home: str) -> list:
    """这个 DSH 家目录里**所有**技能名（不只是随包清单里的）。

    为什么要它（2026-10-11 用户反馈）：向导说"保留你自己已有的 3 项"，而用户翻开 DSH 看到
    **4 个**技能 —— 第 4 个（如 `diagnose-windows-sandbox-acl`）是 DSH 那边的，不在我们的清单里。
    "3 项"本身没错，但不说清"还有哪些不是我们的"就会让人以为漏装了。所以如实列出来。
    """
    out = []
    try:
        root = os.path.join(home, "skills")
        if os.path.isdir(root):
            out = sorted(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)))
    except Exception:
        out = []
    return out


def status() -> dict:
    """**只读**：每个家目录里，这几个技能各自是"已就位"还是"缺"；说明文件另附一块。"""
    src = _sources()
    homes = target_homes()
    rows = []
    for home in homes:
        per = {}
        for name in SHIPPED:
            d = os.path.join(home, "skills", name)
            per[name] = "ok" if os.path.isdir(d) else "missing"
        every = all_skills(home)
        rows.append({"home": home, "skills": per,
                     "missing": [n for n, v in per.items() if v == "missing"],
                     "all": every,
                     "others": [n for n in every if n not in SHIPPED]})
    return {
        "source": code_skills_root(),
        "shipped": list(SHIPPED),
        "packed": sorted(src),          # 这里少一个就说明**包没打全**，面板会说出来
        "homes": rows,
        "missing": sorted({n for r in rows for n in r["missing"]}),
        # 说明文件那一块（2026-10-16）：`missing` 只报技能，**不动**上面的语义；
        # 说明文件的缺失在 `files.missing` 里 —— 面板要显示就显示，不显示也不影响既有契约。
        "files": status_files(),
    }


# ------------------------------------------------ 家目录 / 工作区说明文件的读写
#
# 为什么整体放在这里、不新开一个模块：**"随包内容 → DSH 家目录"只有一个搬运工**
# （`/api/skills/setup` 一个入口，安装器与首次启用向导两处调用）。再开一个模块，就得再多一个
# 接口、再多一处调用点 —— 那正是"四处撒"。所以模块名保持 `skills_setup`，职责在文档里写明。


def defaults_root() -> str:
    """说明文件的**源**目录（委托 paths，见 D29）：`shipped_home_defaults_root()`。

    （这里刻意不在字符串里写出那个"点 dsh"字面量 —— `tests/test_dsh_home_coupling.py`
    盯着"app/ 里写死它的位置总数不许变多"，多一处就红。）
    """
    return paths.shipped_home_defaults_root()


def _workspace_dirs() -> list:
    """会议工作区（`{MEETINGS}`）—— 只一处，不是每个家目录一份。

    **只在 3.0 安装布局里做**（判据 `paths.echo_base()` 非空）：那种布局下 `{echoBase}/meeting`
    按定义就是「会议空间」工作区（见 `paths.meetings_root()` 的说明），放一份工作区说明是它的本分；
    老布局（扁平树）的会议目录是 `{DATA}/meetings` —— **纯数据目录**，不往里塞指令文件
    （老机器上机制照样生效：全局那份在家目录 `AGENTS.md` 里，每个会话都会加载）。

    这条判据还有第二个、更硬的作用（2026-10-16 实测）：`tests/test_skills_setup.py` 只隔离了
    `target_homes`，**没有**隔离会议目录 —— 少了这道闸，跑一遍既有用例就会在开发机的真实
    `data/meetings/` 里留下一个 `AGENTS.md`。这正是这个仓库反复记过的"**测试碰真实状态**"
    （test 不许往真实数据目录写东西）。会议目录**用户可指定**（`meetingsDir`），
    所以在测试里必须显式打桩，不能靠"它大概不存在"。
    """
    try:
        if not paths.echo_base():
            return []
        return [p for p in [paths.meetings_root()] if str(p or "").strip()]
    except Exception:
        return []


def _norm(text) -> str:
    """比较用归一：**只统一换行**（`\\r\\n` → `\\n`）。

    为什么只归一换行：这份文件在 Windows 上很容易被别的编辑器/命令存成 CRLF，而"换行不同"
    表达不了任何用户意图 —— 不归一的话，一次 CRLF 就会让我们**永远**升不了自己写下的默认。
    **除换行以外的任何一个字节不同**都算"用户改过"，一律不动（宁可少升一次，也不覆盖用户的话）。
    """
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _is_builtin(text) -> bool:
    """第一行是不是我们的标记。**只用于如实报告**（"这份是内置默认，你改过"），不作覆盖判据。"""
    for line in _norm(text).split("\n"):
        if line.strip():
            return line.strip().startswith(BUILTIN_MARK)
    return False


def _write_text(target: str, text: str) -> None:
    """原子写、**不写 BOM**、**一律 LF**（跨平台一致，免得下次比较被换行骗）。

    `.tmp` 后缀刻意不用 `.echo-tmp` 之外的花样：写完 `os.replace` 原子替换 —— 中途失败不会
    留下半份文件（半份 `AGENTS.md` 会被 agent 当指令读）。
    """
    folder = os.path.dirname(target)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = target + ".echo-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, target)


def _install_one(target: str, text: str, name: str):
    """把一个默认文件落到 ``target``；返回 ``(动作, 原因)``。

    动作（四种，全部**不覆盖用户内容**）：
      * ``installed`` —— 目标原本不存在；
      * ``unchanged`` —— 内容已是当前默认（**幂等**：不重写、不动 mtime、不产生第二份）；
      * ``updated``   —— 内容逐字等于 :data:`PREVIOUS_DEFAULTS` 里的某一版（**我们自己那份的旧版本**）；
      * ``kept``      —— 其余一律**保留**（用户的、或用户改过的我们那份）。
    """
    try:
        with open(target, encoding="utf-8-sig") as fh:      # 容忍 BOM：别的工具可能加了 BOM
            cur = fh.read()
    except FileNotFoundError:
        cur = None
    except OSError as e:
        return "error", "读不了已存在的文件（%s）" % e
    if cur is None:
        _write_text(target, text)
        return "installed", "原本不存在"
    if _norm(cur) == _norm(text):
        return "unchanged", "已是当前默认（幂等：不重写、不动 mtime）"
    for old in PREVIOUS_DEFAULTS.get(name, ()):
        if _norm(cur) == _norm(old):
            _write_text(target, text)
            return "updated", "是我们自己写下的旧版本，已升级"
    return "kept", ("保留你自己那份%s"
                    % ("（这份是内置默认，你改过）" if _is_builtin(cur) else ""))


def _file_jobs() -> list:
    """要做的搬运活：``[{name, what, src, targets:[目录…]}]``。

    源文件**不存在**（包没打全）也照样列出来 —— 由调用方报成 ``absent``，**不许静默少装一个**。
    """
    root = defaults_root()
    jobs = []
    for name, src_rel, target_rel, what in HOME_FILES:
        jobs.append({"name": name, "what": what, "src": os.path.join(root, src_rel),
                     "rel": target_rel, "targets": target_homes()})
    ws = _workspace_dirs()
    for name, src_rel, target_rel, what in WORKSPACE_FILES:
        jobs.append({"name": name, "what": what, "src": os.path.join(root, src_rel),
                     "rel": target_rel, "targets": ws})
    return jobs


def install_files() -> dict:
    """把家目录 / 工作区的说明文件补上（**只补缺、只升我们自己那份的旧版本**）。

    返回 ``{ok, installed, updated, unchanged, kept, absent, errors, source, entries}``；
    ``entries`` 是逐条明细（``name`` / ``target`` / ``what`` / ``action`` / ``why``），
    给人看"到底动没动、为什么"。
    """
    installed, updated, unchanged, kept, absent, errors, entries = [], [], [], [], [], [], []
    for job in _file_jobs():
        name, src = job["name"], job["src"]
        try:
            with open(src, encoding="utf-8") as fh:
                text = fh.read()
        except Exception as e:
            absent.append(name)                      # 源不在 = 包没打全，要吵
            entries.append({"name": name, "target": "", "what": job["what"],
                            "action": "absent", "why": "源文件读不到：%s（%s）" % (e, src)})
            continue
        for folder in job["targets"]:
            target = os.path.join(folder, job["rel"])
            try:
                action, why = _install_one(target, text, name)
            except Exception as e:
                action, why = "error", str(e)
            entries.append({"name": name, "target": target, "what": job["what"],
                            "action": action, "why": why})
            if action == "error":
                # "读不了目标"（权限/它是个目录）不该把整件事带崩，也不许静默 —— 报出来继续装别的。
                errors.append("%s@%s: %s" % (name, folder, why))
                continue
            tag = "%s@%s" % (name, folder)
            {"installed": installed, "updated": updated,
             "unchanged": unchanged, "kept": kept}[action].append(tag)
    return {
        "ok": not errors and not absent,
        "installed": installed, "updated": updated, "unchanged": unchanged,
        "kept": kept, "absent": absent, "errors": errors,
        "source": defaults_root(),
        "entries": entries,
        # 一句话总结（安装器/面板直接印，别各写一份）
        "summary": ("说明文件：新装 %d、升级 %d、未变 %d、保留你的 %d%s"
                    % (len(installed), len(updated), len(unchanged), len(kept),
                       ("、缺源 %d" % len(absent)) if absent else "")),
    }


def status_files() -> dict:
    """**只读**：每个目标上的说明文件是"就位 / 缺 / 是你自己那份 / 包没打全"。"""
    rows = []
    for job in _file_jobs():
        try:
            with open(job["src"], encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            text = None                   # 源读不到 = 包没打全；这时**不猜**目标是谁的
        for folder in job["targets"]:
            target = os.path.join(folder, job["rel"])
            try:
                with open(target, encoding="utf-8-sig") as fh:
                    cur = fh.read()
            except Exception:
                cur = None
            if text is None:
                state = "unpacked"        # 随包模板不在（install 会把这种报成 absent 并吵）
            elif cur is None:
                state = "missing"
            elif _norm(cur) == _norm(text):
                state = "ok"
            elif _is_builtin(cur):
                state = "edited"          # 我们写的模板，被用户改过 —— 正常且不许动
            else:
                state = "user"            # 用户自己的同名文件 —— 更不许动
            rows.append({"name": job["name"], "what": job["what"], "home": folder,
                         "target": target, "state": state})
    return {
        "source": defaults_root(),
        "packed": [n for n, _s, _t, _w in HOME_FILES + WORKSPACE_FILES
                   if os.path.isfile(os.path.join(defaults_root(), _s))],
        "entries": rows,
        "missing": sorted({r["name"] for r in rows if r["state"] == "missing"}),
    }


def install() -> dict:
    """把随包技能补进各 DSH 家目录的 `skills/`，并补上说明文件。**只补缺、绝不覆盖**。

    返回 ``{ok, installed, skipped, absent, errors, homes, files}``：
      * ``installed`` —— 这次真的拷进去的（``"<名字>@<家目录>"``）；
      * ``skipped`` —— 目标已存在，**保留用户那一份**；
      * ``absent`` —— 随包清单里有、但代码树里没有（= 包没打全，要吵）；
      * ``errors`` —— 拷贝失败（尽量继续装别的，别一错全停）；
      * ``files`` —— **家目录/工作区说明文件（AGENTS.md）那一块**（2026-10-16 新增）：
        刻意**不与技能混在一张单子上** —— 面板「能力」页那一栏说的是技能，而安装器两边都要印。
        `ok` 把两边一起算（任一边有错/缺源就不算 ok）。
    """
    src = _sources()
    absent = [n for n in SHIPPED if n not in src]
    homes = target_homes()
    installed, skipped, errors = [], [], []
    for home in homes:
        dst_root = os.path.join(home, "skills")
        try:
            os.makedirs(dst_root, exist_ok=True)
        except Exception as e:
            errors.append("%s: 建 skills 目录失败：%s" % (home, e))
            continue
        for name, s in src.items():
            d = os.path.join(dst_root, name)
            if os.path.exists(d):
                skipped.append("%s@%s" % (name, home))     # 绝不覆盖
                continue
            try:
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                installed.append("%s@%s" % (name, home))
            except Exception as e:
                errors.append("%s@%s: %s" % (name, home, e))
    # 家目录 / 工作区的说明文件（AGENTS.md）—— 与技能同一趟装，但**分开报**（见 docstring）。
    # 技能那一步失败（建目录失败）也照装说明文件：两件事互不依赖，别一个坏就全停。
    files = install_files()
    return {
        "ok": not errors and not absent and files["ok"],
        "installed": installed,
        "skipped": skipped,
        "others": sorted({n for h in homes for n in all_skills(h) if n not in SHIPPED}
                         - {n for n in absent}),
        "absent": absent,
        "errors": errors,
        "homes": homes,
        "source": code_skills_root(),
        "files": files,
    }
