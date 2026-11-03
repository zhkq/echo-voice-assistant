# -*- coding: utf-8 -*-
"""install_state.py — 「这台机器装成什么样了」的唯一真值。

为什么需要它（2026-09-21 实测反馈）
----------------------------------
安装现在由**技能**在用户自己的 agent 里完成（见 `docs/安装-技能优先.md`）。而面板原来只问
一个问题：`data/installed-components.json` 在不在 —— 在就进仪表盘，不在就**强制进向导**
（`web/app.js` 的 `bootView()`）。那个文件只有**向导末页**才写，技能安装从不写，于是
"技能装完，打开 ECHO 还是进向导"。

这里把安装情况变成一等概念：

* :func:`save_report` / :func:`load_report` —— 技能装完登记的真值（`data/install-report.json`）。
* :func:`declared` —— 登记过没有（首装判据：新报告 **或** 老的 installed-components.json）。
* :func:`missing` —— **从真值推**"还缺什么"（模型 ready / pip 模块 import / node / harness online）。
  技能拿它做自检与最终汇报，面板拿它做横幅与「能力」页 —— 一套判断，两处复用。

刻意不碰的东西：**不写设置值**（报告是明文，可能被拷来拷去）、不启动/停止任何进程。
"""
import json
import os
import time

#: 技能登记的文件（数据根下）
REPORT_FILE = "install-report.json"
#: 报告格式版本；将来改结构时靠它迁移
REPORT_SCHEMA = "echo-install-report/1"

#: 引擎表：**客户端已不再安装本地 STT 引擎**（2026-10-31 用户拍板）。
#:
#: 指令 / 回顾 / 会议转写统一走能力后端（echo-server + Qwen3-ASR）。
#: 保留 `ENGINE_SPECS` / `STT_TO_ENGINE` 两个名字，只是为了让历史调用方拿到空表，
#: 不再有"本机引擎缺依赖/缺模型"这回事。
ENGINE_SPECS = {}
STT_TO_ENGINE = {}


def _data_root() -> str:
    try:
        from app import paths
        return paths.data_root()
    except Exception:
        return ""


def report_path(path: str = "") -> str:
    return path or os.path.join(_data_root(), REPORT_FILE)


def load_report(path: str = "") -> dict:
    """读安装报告；没有/坏了都返回空 dict（**不抛** —— 面板不能因为一个文件读不了就白屏）。"""
    try:
        with open(report_path(path), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_report(payload: dict, path: str = "") -> dict:
    """写安装报告（原子替换）。返回落盘的完整内容。"""
    target = report_path(path)
    data = dict(payload or {})
    data["schema"] = REPORT_SCHEMA
    data["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    folder = os.path.dirname(target)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return data


# ---------------------------------------------------------------- 安装模式（首次 vs 升级）
#
# 为什么要单独记一笔，而不是去"猜数据形状"（2026-10-10 用户口径）：
#   面板要分清两种人 ——
#     * **首次安装** → 进「首次启用向导」（配 DSH/模型 key、笔记库、三个技能）；
#     * **升级安装** → 直接进工作状态，**向导入口只留在「设置」里**，顶部不提示。
#   而"数据目录里有东西"分不出这两者：全新装完、服务一起来就会建库写设置，
#   于是刚装好的机器看起来也像老机器。**唯一知道真相的是安装器**（它自己判断了
#   是全新目录还是覆盖已有目录），所以由它写下来。
#
#   `first-run.json` 与老的 `installed-components.json` **分开**：后者是旧向导末页写的，
#   语义是"旧向导走完了"，而新的首次启用向导管的是另一批事（笔记库/技能），
#   两者不能互相顶替（2026-09-21 那次回归就是判据用错了更窄的一个函数）。
#: 安装模式登记（安装器写）
MODE_FILE = "install-mode.json"
#: 「首次启用向导」走完的凭据（新向导写）
FIRST_RUN_FILE = "first-run.json"

MODE_FRESH = "fresh"
MODE_UPGRADE = "upgrade"


def mode_path(path: str = "") -> str:
    return path or os.path.join(_data_root(), MODE_FILE)


def first_run_path(path: str = "") -> str:
    return path or os.path.join(_data_root(), FIRST_RUN_FILE)


def _read_json(target: str) -> dict:
    # `utf-8-sig`：**必须容忍 BOM** —— 安装器是 PowerShell 5.1，`Set-Content -Encoding UTF8`
    # 会写 BOM；带 BOM 的 JSON 用严格 utf-8 读会直接抛，于是"安装模式"静默变回空串
    # （2026-10-11 特意两个方向都堵上：PS 那边写不带 BOM，这边读也容忍）。
    try:
        with open(target, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(target: str, payload: dict) -> dict:
    folder = os.path.dirname(target)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return payload


def install_mode(path: str = "") -> str:
    """`fresh` / `upgrade`；**没登记过返回空串**（不猜 —— 空串由 :func:`onboarding` 兜底）。"""
    m = str(_read_json(mode_path(path)).get("mode") or "").strip().lower()
    return m if m in (MODE_FRESH, MODE_UPGRADE) else ""


def save_install_mode(mode: str, *, note: str = "", path: str = "") -> dict:
    """记下"这次是全新装还是覆盖已有目录"。安装器在**装之前**探测、装完写。"""
    m = str(mode or "").strip().lower()
    if m not in (MODE_FRESH, MODE_UPGRADE):
        raise ValueError("mode 只能是 fresh / upgrade：%r" % (mode,))
    return _write_json(mode_path(path), {
        "mode": m,
        "note": str(note or ""),
        "savedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def first_run_done(path: str = "") -> bool:
    """「首次启用向导」走完了没有。

    **只认 JSON 布尔 `true`**（`"yes"` / `1` / `"true"` 一律不算）：写失败时宁可再弹一次
    （用户点一下就好），也不要"因为读到一个真值就以为走完了" —— 那会让新用户
    永远配不上笔记库与技能。
    """
    return _read_json(first_run_path(path)).get("done") is True


def mark_first_run_done(*, mode: str = "", path: str = "") -> dict:
    return _write_json(first_run_path(path), {
        "done": True,
        "mode": str(mode or install_mode(path) or ""),
        "savedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def existing_content() -> dict:
    """安装根里**已经有什么** —— "这是升级"的旁证（**只读、绝不抛**）。

    两个用途：① 安装模式没登记时（老版本升上来的机器）兜底判断；
    ② 安装器/面板要把"继承了哪些东西"说清楚（会议数据是事故级，必须能核对）。
    """
    out = {"dsh": False, "db": False, "rows": 0, "meetings": 0, "vault": ""}
    try:
        from app import paths
        home = paths.dsh_home_root()
        out["dsh"] = bool(home) and os.path.isdir(home) and any(
            os.path.exists(os.path.join(home, n))
            for n in ("settings.yaml", "skills", "sessions", "storages"))
    except Exception:
        pass
    try:
        import sqlite3
        from app import paths
        db = os.path.join(paths.data_root(), "echo.db")
        if os.path.isfile(db):
            out["db"] = True
            # 只读打开：**绝不**顺手建库/迁移（探测不该改机器状态）
            uri = "file:%s?mode=ro" % db.replace("\\", "/")
            con = sqlite3.connect(uri, uri=True)
            try:
                for table in ("commands", "meetings"):
                    try:
                        out["rows"] += int(
                            con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
                    except Exception:
                        pass
            finally:
                con.close()
    except Exception:
        pass
    try:
        from app import paths
        root = paths.meetings_root()
        if root and os.path.isdir(root):
            out["meetings"] = len([n for n in os.listdir(root)
                                   if os.path.isdir(os.path.join(root, n))])
    except Exception:
        pass
    try:
        from app.config import settings
        out["vault"] = str(settings.get("worklogVaultRoot", "") or "").strip()
    except Exception:
        pass
    return out


def onboarding(path: str = "") -> dict:
    """**首次启用向导该不该自动弹** —— 面板只问这一个判据（别在 JS 里再算一遍）。

    判据（2026-10-10 用户口径）：
      1. 走过一次 → 不再弹；
      2. **升级安装 → 不弹**（入口只留「设置」里）：明确登记 `upgrade` 优先；
         没登记时用「真内容」兜底 —— 注意「有库」不算老机器（刚装完也有库，2026-10-11
         用户实测的坑），要看命令/会议的行数与笔记库设置；
      3. 其余 → 弹。**注意包含"技能刚装完的全新机器"** —— 技能不负责配笔记库与
         三个技能，所以 `declared()` 不能拿来当首装判据（2026-09-21 那次正是如此）。
    """
    mode = install_mode(path)
    done = first_run_done(path)
    ex = existing_content()
    if mode == MODE_UPGRADE:
        should, reason = False, "登记为升级安装 —— 直接进工作状态，向导入口在设置里"
    elif mode == MODE_FRESH:
        should = not done
        reason = "登记为全新安装" + ("（向导已走过）" if done else "")
    else:
        # 2026-10-11 用户实测：「用交付包装完，但没看到向导」——根因就在这一行。
        # 原来只看 `db`/`dsh` **在不在**：而**刚装完的机器一样有库**（服务一起来就建
        # `echo.db`）、也照样有 DSH 家目录，于是全新安装被误判成升级 -> 不进向导。
        # 判据必须看**里面有没有真东西**：
        #   * `rows`（commands + meetings 行数）> 0 -> 这台机器用过；
        #   * `meetings`（会议目录数）> 0 -> 有真数据（事故级，必须保留）；
        #   * `vault`（笔记库设置）非空 -> 用户配过。
        # 空库 + 有家目录 = 刚装完 -> **算全新**（该进向导）。
        looks_old = bool(int(ex.get("rows") or 0) > 0 or int(ex.get("meetings") or 0) > 0
                         or ex.get("vault"))
        should = (not done) and not looks_old
        reason = ("没登记安装模式，按已有内容判为升级" if looks_old
                  else "没登记安装模式，看着是全新安装")
    return {"shouldOnboard": bool(should), "mode": mode, "firstRunDone": done,
            "reason": reason, "existing": ex}



def _wizard_file_exists() -> bool:
    """老安装（向导装的）靠 installed-components.json 登记 —— 它也算"登记过"。"""
    try:
        from app import wizard
        return os.path.isfile(wizard.installed_path())
    except Exception:
        return False


def declared(path: str = "") -> bool:
    """安装登记过没有。**认两处**：新的 install-report.json 或老的 installed-components.json。

    新老都认是有意的：老机器不去补文件，装过一次就不该再被当成首装。
    """
    try:
        if os.path.isfile(report_path(path)):
            return True
    except Exception:
        pass
    return _wizard_file_exists()


def _module_ok(name: str) -> bool:
    """pip 依赖装没装：**以 import 为准**（不是"pip 说成功"，2026-09-21 踩过）。"""
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _model_ready(model_id: str):
    try:
        from app import modelinfo
        return modelinfo.ready(model_id)
    except Exception:
        return None


def engine_spec(model_id: str) -> dict:
    """按模型 id 反查引擎规格：客户端已无本地引擎 → 恒为 {}（保留签名）。"""
    return {}


def engine_problem(choice: str) -> str:
    """"这个 sttModel 取值现在能不能真用" —— **客户端已不再使用本地转写引擎**。

    2026-10-31 起指令/回顾/会议统一走能力后端（Qwen3-ASR），设置里残留的
    `sttModel` 老值不再参与任何决策，所以这里**恒返回空串**（不拦设置保存）。
    """
    return ""


def _node_ok() -> bool:
    try:
        import shutil
        return bool(shutil.which("npx") or shutil.which("node"))
    except Exception:
        return False


def _harness_online() -> bool:
    try:
        from app import harness_proc
        if not harness_proc.requested():
            return False
        return bool(harness_proc.online(timeout=0.6))
    except Exception:
        return False


def _dsh_online() -> bool:
    """一套 DSH 是否在线：桌面版（43120）**或** ECHO 起的无头 dsh（43199）任一在跑。

    2026-11-03 真机：这里原来只问 `manager.dsh_ready()`（桌面版），于是"官方桌面端没开、
    ECHO 自己起了无头 dsh 并在线"的机器被 `/api/install/state` 判成"还有 1 项没就绪"，
    面板一直挂横幅「DSH Desktop 没在运行（你选的是桌面版）」、向导也不收尾。
    """
    try:
        from app import harness_proc
        from app import manager
        if manager.dsh_ready():
            return True
        return bool(harness_proc.online(timeout=1.0))
    except Exception:
        return False


def _wanted_from_settings() -> dict:
    """没有报告时，从**设置**推"用户想要什么"。

    2026-10-31 起客户端不再装本地 STT 模型/引擎、也不再做唤醒/本地分离 ——
    engines/wake/diarize 恒为空；只有 agent 仍需从设置读。
    """
    out = {"engines": [], "wake": False, "diarize": False, "agent": ""}
    try:
        from app.config import settings
        out["agent"] = str(settings.get("agentBackend", "") or "").strip()
    except Exception:
        pass
    return out


def wanted(report: dict = None) -> dict:
    """这次要的是什么：优先用技能登记的报告，没有就退回设置。"""
    report = report if report is not None else load_report()
    if report:
        # 老报告里的 engines/wake/diarize 是历史字段：客户端已不收这些组件，一律忽略。
        return {
            "engines": [],
            "wake": False,
            "diarize": False,
            "agent": str(report.get("agent") or ""),
        }
    return _wanted_from_settings()


def _is_windows() -> bool:
    """当前是不是 Windows —— **走平台接缝**，不在 app/ 里直接写 os.name 分支
    （audit-paths 会拦：平台差异只能住在 app/platform/ 下，2026-09-21 实测踩到）。"""
    try:
        from app import platform as echo_platform
        return echo_platform.current() == "win32"
    except Exception:
        return False


def _install_command_for(spec: dict) -> str:
    """引擎的 pip 安装命令（走组件清单，不在这里再抄一份"包名 → 命令"）。

    2026-09-25 的 bug 就是"抄了两份"：面板「复制安装命令」用了 `sys.executable`，
    而「复制命令」还是裸 `python` —— 干净装机上后者直接跑不了。凡是**要贴给用户跑**的
    命令，都从 `app/interpreter.py` 那条路算。
    """
    try:
        from app import components
        return components.install_command_for_model(str(spec.get("model") or ""))
    except Exception:
        return ""


def missing(report: dict = None) -> list:
    """**还缺什么** —— 逐项从真值推，返回给人看的三元组列表。

    每一项：``{"feature", "reason", "fix"}``（与向导 `build_plan()` 的 missing 同形，
    面板/技能可以直接复用同一套渲染）。

    缺依赖那一项额外带 **安装命令**（`fix` 里已经写着），模型未下那一项带
    ``nextStep`` / ``action``：面板要能直接说出"下一步点哪个按钮"，而不是让用户对着
    "模型文件还没下载"猜（同事 2026-09-25：他以为卡死了，其实只差再点一次下载）。
    """
    want = wanted(report)
    out = []

    # 客户端不再装本地 STT/唤醒/分离模型（2026-10-31）：
    # 没有"缺引擎依赖 / 缺模型 / 缺 KWS / 缺 pyannote"这几类缺失项了。
    # 剩下只有执行智能体是否在线（它仍在本机/局域网）。
    # 2026-10-04（用户实测指出）：这一项**必须看当前设置**，不能只看装机报告。
    # 报告是装机那一刻写下的（例如 agent=harness）；用户在面板里把执行智能体改成
    # DSH Desktop 之后报告不会变 —— 于是这里照旧去查 harness，永远报
    # "harness 没在运行、还没装完"。用户的原话："我改成 dsh desktop 你停掉标准版，
    # 这个是合理的，报错是因为你还在查标准版？…把这段检查目标改成跟设置一样，
    # 或者跟仪表盘最下方的探测方式一致" —— 仪表盘底部那个 Agent 卡片看的就是
    # `agentBackend`，所以这里也以它为准；报告只在**设置里没有**时兜底（老安装）。
    agent_now = want["agent"]
    try:
        from app.config import settings as _s
        agent_now = str(_s.get("agentBackend", "") or "").strip() or agent_now
    except Exception:
        pass
    if agent_now in ("harness", "dsh"):
        # 一套 DSH（2026-11-03）：`harness` 只是旧值，判断与 `dsh` 完全一样 ——
        # 桌面版在跑（43120）或 ECHO 起的无头 dsh 在跑（43199）都算在线。
        # 2026-11-03 真机：只问桌面版会把"无头 dsh 在线"误判成"没在运行"。
        if not _dsh_online():
            out.append({"feature": "智能体（会议纪要 / 归档 / 语音指令）",
                        "reason": "DSH 引擎没在跑（桌面版和无头 dsh 都不在线）",
                        "fix": "在面板「启动页」点 DSH 的「启动」——ECHO 会起无头 dsh；"
                               "或打开官方 DeepSeek Harness 桌面端（ECHO 会自动改用它）。"
                               "官方桌面端没装的话，重跑安装器并让它联网装（约 289 MB），"
                               "或把 dsh-latest-windows-x64.exe 放到 装我.cmd 旁边后重跑。"})
    return out


def state(path: str = "") -> dict:
    """给面板与技能的一份完整状态。"""
    report = load_report(path)
    miss = missing(report)
    engines = wanted(report)["engines"]
    return {
        "declared": declared(path),
        "report": report,
        "wanted": wanted(report),
        # 安装模式与"首次启用向导走过没"（2026-10-11）：面板横幅据此决定要不要给
        # 「手动向导」按钮 —— **升级安装的入口只留在「设置」里**（用户口径）。
        "installMode": install_mode(),
        "firstRunDone": first_run_done(),
        "engines": [],   # 客户端已不再安装本地 STT 引擎（2026-10-31）
        "missing": miss,
        "ready": not miss,
        "summary": ("这台机器已经装好" if not miss
                    else "还有 %d 项没就绪" % len(miss)),
    }
