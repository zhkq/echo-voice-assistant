# -*- coding: utf-8 -*-
"""这台 ECHO 是**哪个实例**（dev / stable / 别的）——只读，供面板挂牌子用。

为什么要有：两个实例（开发版 `C:\echo-dev`、稳定版 `D:\ECHO`）的面板长得一模一样，
用户分不清自己开的是哪一个（2026-10-04 反馈）。**真相在切换器配置里**
（`~/.echo-instances.json`：名字 → 代码根目录），所以这里读它、拿本树的路径去比对。

**对交付物的影响 = 零**（用户要求）：客户机上没有这份配置 → 返回空名字 →
面板那个牌子保持隐藏；而且**只有** `name == "dev"` 时面板才亮牌子。
"""
import json
import os
from typing import Any, Dict

from app import paths

#: 切换器配置（`scripts/switch-instance.ps1` 与 echo-supervisor 读的同一份）
CONFIG_NAME = ".echo-instances.json"

#: 名字 → 面板上的人话（只有 dev 会被面板显示，其余留着排障用）
LABELS = {"dev": "开发版", "stable": "稳定版"}


def config_path() -> str:
    return os.path.join(os.path.expanduser("~"), CONFIG_NAME)


def _norm(p: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(p or "").strip().rstrip("\\/")))
    except Exception:                                            # noqa: BLE001
        return str(p or "").strip().lower()


def load_config(path: str = "") -> Dict[str, Any]:
    """读切换器配置；读不到/坏了都返回 {}（永不抛）。带 BOM 也认（PS 写出来的常带）。"""
    p = path or config_path()
    try:
        with open(p, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def info(path: str = "") -> Dict[str, Any]:
    """本机这一棵树的实例信息。查不到名字就只报路径（面板据此不挂牌子）。"""
    cfg = load_config(path)
    here = {_norm(paths.echo_root()), _norm(paths.echo_base())}
    name = ""
    for key, item in (cfg.get("instances") or {}).items():
        root = item.get("root") if isinstance(item, dict) else item
        if _norm(root) in here:
            name = str(key)
            break
    return {
        "name": name,
        "label": LABELS.get(name, name),
        "root": paths.echo_root(),
        "base": paths.echo_base(),
        #: 面板只认这个（用户要求：只有开发版挂牌子，交付物不受影响）
        "badge": "开发版" if name == "dev" else "",
    }
