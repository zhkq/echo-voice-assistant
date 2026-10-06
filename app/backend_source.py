# -*- coding: utf-8 -*-
"""把**仓库里的后端源码**同步到**后端部署副本**（2026-10-06 加）。

## 为什么需要它（用户实测暴露的部署缺口）

ECHO 的能力后端跑的是**部署副本**里的代码，不是仓库里的：

    仓库           C:\\echo-dev\\server\\auth.py        ← 我改的
    部署（在跑）    C:\\echo-dev\\data\\backend\\server\\auth.py

那份副本来自**薄包**（`app/backend_fetch.PACKAGE_DIR_ITEMS` 把 `app`/`server` 铺到
`{backend}/`），而薄包只在**构建那一刻**快照一次源码（`build_backend_portable.SOURCE_ITEMS`
= 仓库的 `app/` + `server/`）。于是：

* 在仓库里改后端代码 → **运行中的后端一点都不知道**；
* 症状是"明明修好了却还是老样子"，而**证据全指向仓库那份**（我这次就据此误判了一轮
  `last_seen`：直接实例化仓库的 `Auth` 验通了，可真正在跑的后端根本没有那段代码）；
* 2026-10-06 实测差距：`auth.py`/`store.py`/`ops.py`/`localpair.py`/`admin.py` **5 个文件**
  全是我当天改的，一个都没生效。

## 判据与安全边界

* **只在源码树里动手**：`{repo}/server` 存在且非空才同步 —— 装好的机器上**没有**这棵树
  （源码已经在 `{backend}/server` 里了），所以这一步天然跳过，不会自己覆盖自己；
* **只同步 `server/`**：那是"服务端自己"的代码。`app/` 也随包走，但客户端本来就从自己的树跑，
  同步它没有意义、还可能踩到"客户端在用的那份"；
* **先备份**：不同于则不覆盖，而是把整份 `server/` 备到 `{backend}/server.bak-<时间戳>/`
  （只在**真的**有差异时才备份，避免每次启动都堆一份）；
* **不做删除**：只覆盖/新增，不删部署副本里多出来的文件 —— 删错一个就是"后端起不来"。
"""
from __future__ import annotations

import filecmp
import os
import shutil
import time
from typing import Any, Dict, List, Optional

#: 同步哪些顶层项（只 `server`，理由见文件头"安全边界"）。
SYNC_ITEMS = ("server",)

#: 不同步的目录名（与 `build_backend_portable.EXCLUDE_DIRS_TREE` 同口径）。
_EXCLUDE_DIRS = {"__pycache__", ".git", ".github", ".idea", ".vscode"}


def source_root() -> str:
    """源码树根 —— **走官方的口子** `paths.echo_root()`，不自己推导。

    原来这里写的是 `dirname(dirname(__file__))`，撞了仓库那条铁律
    （D29 / `tests/test_path_seam.py::test_only_paths_py_derives_the_install_root`：
    **安装根只许 `app/paths.py` 推导**）。走 `paths.echo_root()` 还顺带拿到
    `ECHO_ROOT` 覆盖的能力 —— 用例因此能把"源码树在哪"指到临时目录，
    不会去碰真实的部署副本。
    """
    from app import paths
    return paths.echo_root()


def _walk_files(root: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            out[rel] = p
    return out


def _differs(src: str, dst: str) -> bool:
    """内容不同吗？**先比大小再比内容**（`filecmp` 对大文件也要读，但这里是源码，够快）。"""
    try:
        if os.path.getsize(src) != os.path.getsize(dst):
            return True
    except OSError:
        return True
    try:
        return not filecmp.cmp(src, dst, shallow=False)
    except OSError:
        return True


def plan(backend_root: str, *, source_dir: str = "") -> Dict[str, Any]:
    """**只算不动**：要同步哪些文件。返回 ``{ok, changed[], reason, src, dst}``。

    没有任何差异时 `changed` 是空表 —— 调用方据此把这一步记成"已是最新"（不备份、不写盘）。
    """
    src_root = source_dir or source_root()
    item = SYNC_ITEMS[0]
    src = os.path.join(src_root, item)
    dst = os.path.join(backend_root, item) if backend_root else ""
    if not backend_root:
        return {"ok": True, "changed": [], "reason": "没给后端目录（跳过）",
                "src": src, "dst": ""}
    if not os.path.isdir(dst):
        # 部署副本还没有 `server/`（薄包还没解开）→ 不碰：
        # 那份该由 `backend_fetch.ensure_package()` 铺，这里插一脚只会打架。
        return {"ok": True, "changed": [], "reason": "后端还没有 server/（薄包未解）",
                "src": src, "dst": dst}
    if not os.path.isdir(src):
        # 装好的机器：没有源码树。**这正是"不该同步"的正常情形**。
        return {"ok": True, "changed": [], "reason": "没有源码树（装好的机器，跳过）",
                "src": src, "dst": dst}
    src_files = _walk_files(src)
    if not src_files:
        return {"ok": True, "changed": [], "reason": "源码树是空的（跳过）",
                "src": src, "dst": dst}
    changed: List[str] = []
    for rel, sp in sorted(src_files.items()):
        dp = os.path.join(dst, rel.replace("/", os.sep))
        if not os.path.isfile(dp) or _differs(sp, dp):
            changed.append(rel)
    return {"ok": True, "changed": changed,
            "reason": ("有 %d 个文件比部署副本新" % len(changed)) if changed else "已是最新",
            "src": src, "dst": dst}


def sync(backend_root: str, *, source_dir: str = "") -> Dict[str, Any]:
    """把差异文件覆盖过去（**先备份整份 `server/`**）。返回 ``{ok, copied[], detail}``。

    失败**不该拦住起后端**：调用方把它记成一步，失败了如实说，然后接着往下走
    —— 跑旧代码的后端也好过"因为同步失败所以起不来"。
    """
    info = plan(backend_root, source_dir=source_dir)
    changed = list(info.get("changed") or [])
    if not changed:
        return {"ok": True, "copied": [], "detail": info.get("reason") or "已是最新",
                **{k: info[k] for k in ("src", "dst")}}
    dst = info["dst"]
    bak = "%s.bak-%s" % (dst.rstrip("\\/"), time.strftime("%Y%m%d-%H%M%S"))
    try:
        shutil.copytree(dst, bak)
    except Exception as e:                                            # pragma: no cover - 兜底
        return {"ok": False, "copied": [],
                "detail": "同步前备份失败（%s）：没动任何文件" % e, "src": info["src"], "dst": dst}
    copied: List[str] = []
    try:
        for rel in changed:
            sp = os.path.join(info["src"], rel.replace("/", os.sep))
            dp = os.path.join(dst, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.copy2(sp, dp)
            copied.append(rel)
    except Exception as e:                                            # pragma: no cover - 兜底
        return {"ok": False, "copied": copied,
                "detail": "同步中断（%s）：已备份到 %s，可整份还原"
                          % (e, os.path.basename(bak)), "src": info["src"], "dst": dst}
    return {"ok": True, "copied": copied,
            "detail": "同步了 %d 个文件（备份 %s）" % (len(copied), os.path.basename(bak)),
            "src": info["src"], "dst": dst}


def backend_root_for_sync() -> Optional[str]:
    """要同步到哪儿 —— 与起后端用的是**同一个**后端根（`backend_setup.backend_root()`）。

    单独包一层是为了**可打桩**：用例把 `backend_setup.backend_root` 换掉时，
    这条也要跟着换，不能在别处再算一遍（两处判据必然漂开）。
    """
    try:
        from app import backend_setup
        return backend_setup.backend_root()
    except Exception:
        return None
