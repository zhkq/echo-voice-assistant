# -*- coding: utf-8 -*-
"""skills_setup.py — 把**随包分发**的技能装进各 DSH 家目录的 `skills/`。

为什么需要它（2026-10-11 用户指出的坑）
--------------------------------------
随包的技能本来是跟着**代码树**走的（主包白名单里有 `.dsh`，所以安装后它们在
`<代码目录>/.dsh/skills/<名字>/`），而 ECHO 的 agent 是从 **`DSH_HOME/skills/`** 读技能的：
标准版 harness 的 `DSH_HOME` 是 `{echoBase}/dsh/home`（老布局 `{DATA}/harness`），
桌面版是 `~/.dsh`。**这是两个不同的目录** —— 于是新装的机器上，连随包的
`meeting-record` 其实都**没生效**（"新用户安装后没有 skill"的根因）。

两条铁律
--------
1. **只补缺、绝不覆盖**：用户可能自己精心调过 `daily-review`（本仓库作者就是这样）。
   目标已存在就跳过，并把"保留了你那份"如实报出来。
2. **只装随包清单里的**（:data:`SHIPPED`）：不要把用户自己放进 `skills/` 的任何东西当素材。
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


def status() -> dict:
    """**只读**：每个家目录里，这几个技能各自是"已就位"还是"缺"。"""
    src = _sources()
    homes = target_homes()
    rows = []
    for home in homes:
        per = {}
        for name in SHIPPED:
            d = os.path.join(home, "skills", name)
            per[name] = "ok" if os.path.isdir(d) else "missing"
        rows.append({"home": home, "skills": per,
                     "missing": [n for n, v in per.items() if v == "missing"]})
    return {
        "source": code_skills_root(),
        "shipped": list(SHIPPED),
        "packed": sorted(src),          # 这里少一个就说明**包没打全**，面板会说出来
        "homes": rows,
        "missing": sorted({n for r in rows for n in r["missing"]}),
    }


def install() -> dict:
    """把随包技能补进各 DSH 家目录的 `skills/`。**只补缺、绝不覆盖**。

    返回 ``{ok, installed, skipped, absent, errors, homes}``：
      * ``installed`` —— 这次真的拷进去的（``"<名字>@<家目录>"``）；
      * ``skipped`` —— 目标已存在，**保留用户那一份**；
      * ``absent`` —— 随包清单里有、但代码树里没有（= 包没打全，要吵）；
      * ``errors`` —— 拷贝失败（尽量继续装别的，别一错全停）。
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
    return {
        "ok": not errors and not absent,
        "installed": installed,
        "skipped": skipped,
        "absent": absent,
        "errors": errors,
        "homes": homes,
        "source": code_skills_root(),
    }
