# -*- coding: utf-8 -*-
"""preview-settings-menu.py - 不用浏览器，打印"设置页实际会渲染成什么样"。

为什么需要它
------------
设置页的分组/顺序/可见性是三处约定叠出来的：
  1. `app/config.py:DEFAULTS` 的 grp 与声明顺序（后端元数据，`order` 字段）；
  2. `web/app.js` 的 `SET_GROUP_ORDER` / `SET_GROUP_NAMES`（分组标题与先后）；
  3. `web/app.js` 的 `MODEL_KEYS`（被「模型」页签接管的 9 项，要从表单里过滤掉）。
改完分组想确认"用户到底看到什么"，开浏览器点一遍是最慢的办法；这个脚本用**同一份
元数据 + 同一套排序/过滤规则**把它打印出来（临时库，不碰真实 data/）。

用法
----
    python scripts/preview-settings-menu.py                # 正常情况（模型页签可用）
    python scripts/preview-settings-menu.py --models-fail   # 模型页签挂了 → 模型组回退显示
    python scripts/preview-settings-menu.py --out docs/settings-menu.txt
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _panel_group_tables():
    """从 web/app.js 抓面板的分组表与 MODEL_KEYS（正则抓取，不执行 JS）。"""
    with open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    order = re.search(r"const SET_GROUP_ORDER = \[(.*?)\];", js, re.S)
    names = re.search(r"const SET_GROUP_NAMES = \{(.*?)\};", js, re.S)
    model_keys = re.search(r"const MODEL_KEYS = new Set\(\[(.*?)\]\);", js, re.S)
    if not (order and names and model_keys):
        raise SystemExit("web/app.js 里的分组表被改得认不出来了")
    return (re.findall(r'"([a-z]+)"', order.group(1)),
            dict(re.findall(r'([a-z]+):\s*"([^"]+)"', names.group(1))),
            set(re.findall(r'"(\w+)"', model_keys.group(1))))


def rows(models_tab_ok=True):
    """面板 loadSettings() 拿到的行（含它做的过滤与排序）。"""
    import app.db as db
    from app.config import settings
    tmp = tempfile.mkdtemp(prefix="echo-menu-")
    old = (db.DATA_DIR, db.DB_FILE)
    db.DATA_DIR, db.DB_FILE = tmp, os.path.join(tmp, "preview.db")
    try:
        db.init()
        settings.seed_defaults()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        data = TestClient(app).get("/api/settings").json()["settings"]
    finally:
        db.DATA_DIR, db.DB_FILE = old
        shutil.rmtree(tmp, ignore_errors=True)

    _order, _names, model_keys = _panel_group_tables()
    out = [s for s in data if not (models_tab_ok and s["key"] in model_keys)]
    # 与 web/app.js 的 loadSettings() 完全一致：先按 order，再按 key
    out.sort(key=lambda s: (s.get("order", 10 ** 6), s["key"]))
    return out


def render(models_tab_ok=True):
    order, names, _model_keys = _panel_group_tables()
    data = rows(models_tab_ok)
    groups = {}
    for s in data:
        groups.setdefault(s["grp"], []).append(s)
    if models_tab_ok and "model" not in groups:
        pass
    lines = []
    known = [g for g in order if g in groups]
    extra = [g for g in groups if g not in order]
    for grp in known + extra:
        items = groups[grp]
        title = names.get(grp)
        head = "%s（%s）" % (title, grp) if title else "%s（未命名分组！）" % grp
        lines.append("%s  —— %d 项" % (head, len(items)))
        for s in items:
            flags = []
            if s.get("secret"):
                flags.append("密钥")
            if s.get("hasValue"):
                flags.append("已配置")
            vt = s.get("value_type", "?")
            value = s.get("value")
            if s.get("secret"):
                value = "******" if s.get("hasValue") else "(空)"
            elif isinstance(value, list):
                value = ",".join(str(v) for v in value) or "(空)"
            elif isinstance(value, str) and len(value) > 28:
                value = value[:28] + "…"
            lines.append("    %-22s %-26s %-6s = %s%s"
                         % (s.get("label", ""), s["key"], vt, value,
                            ("  [" + ",".join(flags) + "]") if flags else ""))
        lines.append("")
    lines.append("可见项合计：%d（含智能体表格 1 块）" % len(data))
    return "\n".join(lines)


def main(argv):
    out_path = ""
    if "--out" in argv:
        out_path = argv[argv.index("--out") + 1]
    text = render(models_tab_ok="--models-fail" not in argv)
    if out_path:
        with open(os.path.join(ROOT, out_path) if not os.path.isabs(out_path) else out_path,
                  "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("written: %s" % out_path)
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
