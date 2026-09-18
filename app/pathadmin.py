# -*- coding: utf-8 -*-
"""pathadmin.py — 存储路径的管理动作（P1；D20 / D21）

用户把 `meetingsDir` 改到新位置后，**旧会议还留在原目录**。老办法是让用户自己去资源
管理器拖，但会议目录名是 ECHO 生成的、库里还有记录，拖错就会出现"面板里看得到会议、
点开却没有音频"。所以把它做成一个动作：

  * 只搬**看起来像会议目录**的目录（`YYYY-MM-DD_HH-MM-SS`），其它条目一概不动并单独报告，
    避免误伤用户手工放进来的东西；
  * 目标已存在同名目录时**跳过**（不覆盖、不合并）；
  * 同盘用 rename（快），跨盘 `shutil.move` 自动退化为复制 + 删源；
  * 返回结构化结果，面板直接渲染；`dry_run=True` 只报告不动手。

为什么**不需要**改数据库：库里只存会议 `name`，音频/纪要路径都是"根 + name"现算的
（见 `meeting.meetings_dir()`），所以把目录搬过去就等于迁移完成。
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Optional

#: 录制器生成的会议目录名格式（`app/meeting.py` 里 now.strftime("%Y-%m-%d_%H-%M-%S")）
MEETING_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")


def meeting_dir_names(root: str) -> list:
    """列出 ``root`` 下符合会议目录命名规则的目录名（排序）。"""
    try:
        return sorted(n for n in os.listdir(root)
                      if MEETING_DIR_RE.match(n) and os.path.isdir(os.path.join(root, n)))
    except OSError:
        return []


def _existing_ancestor(path: str) -> str:
    """找到最近的已存在祖先目录（dry-run 时用来校验"能不能写到那里"）。"""
    p = os.path.abspath(path)
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p or os.path.abspath(path)


def migrate_meetings(target: str, *, source: Optional[str] = None,
                     dry_run: bool = False) -> dict:
    """把会议目录从当前根搬到 ``target``。返回结构化结果（永不抛异常）。

    结果字段：``ok`` / ``error`` / ``source`` / ``target`` / ``moved`` / ``skipped`` /
    ``failed`` / ``others`` / ``dryRun``。
    """
    from app import paths

    src = os.path.normpath(source or paths.meetings_root())
    result = {"ok": False, "error": "", "source": src, "target": "",
              "moved": [], "skipped": [], "failed": [], "others": [], "dryRun": bool(dry_run)}

    if dry_run and not os.path.isdir(target):
        # dry-run 不该创建目录，但也要能回答"这个目标行不行" —— 退而校验最近的已存在祖先。
        ok, why = paths.validate_dir(_existing_ancestor(target), create=False)
    else:
        ok, why = paths.validate_dir(target, create=not dry_run)
    if not ok:
        result["error"] = why
        return result
    dst = paths.resolve(target)
    result["target"] = dst

    if os.path.normcase(dst) == os.path.normcase(src):
        result["error"] = "目标就是当前会议目录，无需迁移"
        return result
    if not os.path.isdir(src):
        result["error"] = "当前会议目录不存在：%s" % src
        return result
    if os.path.normcase(dst).startswith(os.path.normcase(src) + os.sep):
        result["error"] = "目标不能是当前会议目录的子目录"
        return result

    try:
        entries = sorted(os.listdir(src))
    except OSError as exc:
        result["error"] = "无法读取当前会议目录：%s" % exc
        return result

    for name in entries:
        src_path = os.path.join(src, name)
        if not os.path.isdir(src_path) or not MEETING_DIR_RE.match(name):
            result["others"].append(name)
            continue
        dst_path = os.path.join(dst, name)
        if os.path.exists(dst_path):
            result["skipped"].append(name)
            continue
        if dry_run:
            result["moved"].append(name)
            continue
        try:
            shutil.move(src_path, dst_path)
            result["moved"].append(name)
        except Exception as exc:                      # 单个失败不拖垮整体
            result["failed"].append({"name": name, "error": "%s: %s" % (type(exc).__name__, exc)})

    result["ok"] = not result["failed"]
    if result["failed"] and not result["error"]:
        result["error"] = "%d 个会议目录迁移失败（其余已迁移）" % len(result["failed"])
    return result


def env_report() -> dict:
    """面板"环境体检"页要的一份数据：四类根 + 配置状态 + 端口 + 会议目录计数。"""
    from app import paths, ports

    roots = paths.active_roots()
    report = paths.preflight()
    report["configured"] = {"meetings": roots["meetingsConfigured"],
                            "models": roots["modelsConfigured"]}
    report["port"] = ports.read_port_file(paths.data_root(), 0)
    report["meetingDirs"] = len(meeting_dir_names(roots["meetings"]))
    return report
