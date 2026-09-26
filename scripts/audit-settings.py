# -*- coding: utf-8 -*-
"""audit-settings.py - ECHO settings inventory: who defines / writes / READS each key.

Why this exists
---------------
The panel's settings menu grew organically: keys were added by feature, groups were
named ad hoc, and nothing ever checked whether a key is still read by the code.
The user asked for exactly that review: "which settings are dead or duplicated, and
are they actually read?"  Guessing from the panel is impossible - a key that is
written by the panel and read by nobody looks exactly like a working one.

What it does
------------
For every key in app/config.py:DEFAULTS it reports, per file/line:
  * DEF  - the definition site (config.py:DEFAULTS / DEFAULT_MIGRATIONS)
  * READ - a read of the current value (settings.get("k"), .get("k"), etc.)
  * WRITE- a write (settings.update({...}), db.set_setting("k"), panel PUT)
  * PANEL- a reference in web/*.js (data-key, settingFrom(list,"k"), _settingsCache)
and then a verdict:
  OK        - read somewhere in app/ (the product code actually consumes it)
  PANEL-ONLY- only defined + referenced by the panel: suspicious (write-only)
  DEAD      - defined but neither read nor referenced anywhere
  (DEPRECATED / HIDDEN / SECRET / PLATFORM are printed as flags, not verdicts)

Usage
-----
    python scripts/audit-settings.py            # report to stdout + docs/settings-audit.md
    python scripts/audit-settings.py --check    # exit 1 if any key is DEAD/PANEL-ONLY

Read-only: it never imports app/ (so it works even when the tree cannot be imported)
and never writes anything except the markdown report.
"""
from __future__ import annotations

import io
import os
import re
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("app", "web", "scripts", "mac", "dsh-failover")
SCAN_EXT = (".py", ".js", ".html", ".ps1", ".sh")
SKIP_PARTS = ("__pycache__", "node_modules", ".git", "build", "dist")

#: 只读这些名字后面的键：settings.get("k") / _s.get("k") / .all().get("k")
READ_PATTERNS = (
    r'settings\s*\.\s*get\s*\(\s*%s',
    r'_s\s*\.\s*get\s*\(\s*%s',
    r'cfg\s*\.\s*get\s*\(\s*%s',
    r'config\s*\.\s*get\s*\(\s*%s',
    r'get_setting\s*\(\s*%s',
)
#: 写入形态
WRITE_PATTERNS = (
    r'settings\s*\.\s*update\s*\(\s*\{[^}]*%s',
    r'set_setting\s*\(\s*%s',
    r'upsert_settings\s*\(\s*\{[^}]*%s',
)

#: 面板**真的读了这个值**（不只是渲染成输入框）：
#     settingByKey("k") / settingsValue("k") / settingFrom(list,"k") / key === "k"
# 为什么单独算一类：`panelAutoRefresh` 这类项的唯一消费方就是面板自己（控制轮询间隔），
# 它既不出现在 app/ 里，也不是"写进去没人读"。把它们混进 PANEL-ONLY 会把真问题淹掉。
PANEL_READ_PATTERNS = (
    r'settingByKey\s*\(\s*["\']%s["\']',
    r'settingsValue\s*\(\s*["\']%s["\']',
    r'settingFrom\s*\([^)]*["\']%s["\']',
    r'key\s*===\s*["\']%s["\']',
)

#: `--check` 的人工确认名单 —— 名字必须在这里出现，否则算未确认（新增项会被拦下）。
#: 每条都写"谁在读它"，因为正是"看不出谁在读"才需要这份名单。
ACK_INDIRECT = {
    # 智能体注册表：类属性 config_key 决定启用开关，`is_enabled` 用变量读（不是字面量）
    "agentCodebuddyEnabled": "app/agents/__init__.py:111 is_enabled() 按类的 config_key 读",
    # provider 选择：providers 层用 `"provider%s" % kind.capitalize()` 拼键名读
    # （providerLlm / providerAsr 共用同一个循环名，所以扫描看不见字面量）。
    # 2026-09-20 起纪要不再自己读 providerLlm（改 agent-first，见 meeting.direct_llm_decision）。
    "providerLlm": "app/providers/__init__.py:181 active_id() 拼键名读（provider<Kind>）",
    # 路由进程参数：router_admin 把它们映射成 dsh-failover/config.json 的字段名
    "routerProbeInterval": "app/router_admin.py:435 参数映射（写进 dsh-failover/config.json）",
    "routerFirstByteTimeout": "app/router_admin.py:436 参数映射",
    "routerConnectTimeout": "app/router_admin.py:437 参数映射",
    "routerBreakerThreshold": "app/router_admin.py:438 参数映射",
    "routerBreakerCooldown": "app/router_admin.py:439 参数映射",
    # 热键：平台层按 (键名, 默认值) 元组注册，键名是循环变量
    "wakeHotkey": "app/platform/win32/hotkey.py:148 热键注册表（键名来自元组）",
    "fallbackHotkey": "app/platform/win32/hotkey.py:148 同上（媒体键失效时的备用键）",
    "panelHotkey": "app/runtime.py:206 热键注册表（键名来自元组）",
    # 分组名：app/workspaces.py 的 DEFAULT_SPACES 里用 title_key 当**循环变量**读
    # （`_setting(spec["title_key"])`），所以扫描看不见字面量。消费方见那一行。
    "meetingWorkspaceTitle": "app/workspaces.py:36 _setting(title_key)：DSH「会议空间」分组名",
    "commandWorkspaceTitle": "app/workspaces.py:36 _setting(title_key)：DSH「指令空间」分组名",
    # 按用途分的输入设备：app/audio/recorder.py 的 resolve_input_device() 查
    # INPUT_DEVICE_KEYS 表读（键名来自表里的值，调用点上不是字面量）
    "commandInputDeviceId": "app/audio/recorder.py:resolve_input_device() 查表读（指令/唤醒用哪个麦）",
    "meetingInputDeviceId": "app/audio/recorder.py:resolve_input_device() 查表读（会议录音用哪个麦）",
    # 通用兜底那一项也是循环读（与用途键一起遍历），不再是 settings.get("inputDeviceId")
    "inputDeviceId": "app/audio/recorder.py:resolve_input_device() 循环读（两个用途的通用兜底）",
    # 能力路由（3.0）：槽 → 设置键的映射在 router._setting_key_for() 里，
    # 调用点拿到的是映射出来的**键名变量**（`self._get(_setting_key_for(slot))`），
    # 所以扫描看不见字面量。映射本身在 router.py 的 _setting_key_for()。
    #
    # ⚠ capabilityMeetingAsrBackend / capabilityDiarizeBackend / capabilityEmbedBackend
    # **不在这里**（2026-09-26）：面板新增了「会议能力通道」那一块，`web/app.js` 的
    # `friendlyOption()` 按**键名**给这三个后端取值出中文名（`key === "capabilityDiarizeBackend"`）。
    # 面板那一处是**直接**读，`verdict()` 的优先级里 direct 面板读压过 app 间接读，
    # 于是这三项判成 PANEL-READ —— 名单也要跟着搬（见 `ACK_PANEL`）。
    # 这正是这份名单的防腐机制：判据变了就搬位置，而不是两边都留着。
    # capabilityPrivacy **已经不在这里**（2026-09-24）：闸报"已不再是间接读，请从名单里删掉"。
    # 原因是它现在有一处**直接字面量**读法（app/capability_admin.py 的 `_setting("capabilityPrivacy")`），
    # 扫描看得见了。这正是这份名单的防腐机制在起作用 —— 名单里多留一项 = 掩护一处真的漏读。
    # 播放设备（扬声器）与采集侧同形：用途键在 OUTPUT_DEVICE_KEYS 表里，
    # 调用点拿到的是**映射出来的键名变量**（`_read_setting(key)`），所以扫描看不见字面量。
    # （同一份名单里 commandInputDeviceId / meetingInputDeviceId 就是同一个理由。）
    "commandOutputDeviceId": "app/audio/output.py:resolve_output_device() 查 OUTPUT_DEVICE_KEYS 读（指令播报用哪台扬声器）",
    "meetingOutputDeviceId": "app/audio/output.py:resolve_output_device() 查 OUTPUT_DEVICE_KEYS 读（会议播报用哪台扬声器）",
}

#: 唯一消费方是面板 UI 的项（面板读它来改变自己的行为，app/ 不需要读）。
ACK_PANEL = {
    "panelAutoRefresh": "web/app.js 的 _panelRefreshSeconds()：仪表盘/启动页/路由页的轮询间隔",
    # 会议能力通道那 3 项（2026-09-26）：面板按**键名**给后端取值出中文名
    # （`friendlyOption()` 的 `key === "capability…Backend"` 那两支）。
    # 它们**同时**被 app/ 间接读 —— `app/capabilities/router.py:_setting_key_for()`
    # 把槽映射成键名变量再 `settings.get()`，扫描看不见那个字面量。
    # 但面板这一处是**直接**读，`verdict()` 里 direct 面板读优先，所以判 PANEL-READ、
    # 登记在这里。**下一个人注意**：这三项不是"只有面板在读"，
    # 真消费方仍是路由（`ACK_INDIRECT` 顶上那段注释记着这件事）。
    "capabilityMeetingAsrBackend": "web/app.js:friendlyOption() 出「ECHO 后端/本机/网络服务商」中文名；"
                                   "app 侧见 router._setting_key_for()（asr.text / asr.timestamps 用哪个后端）",
    "capabilityDiarizeBackend": "web/app.js:friendlyOption() 出「自动/ECHO 后端/本机」中文名；"
                                "app 侧见 router._setting_key_for()（说话人分离用哪个后端）",
    "capabilityEmbedBackend": "web/app.js:friendlyOption() 出「自动/ECHO 后端/本机」中文名；"
                              "app 侧见 router._setting_key_for()（说话人嵌入用哪个后端）",
}


def _iter_files():
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in SKIP_PARTS]
            for fn in sorted(filenames):
                if fn.endswith(SCAN_EXT):
                    yield os.path.join(dirpath, fn)


def _code_lines(path):
    """返回 [(lineno, 代码)]：**保留字符串字面量**、丢弃注释与独立文档串。

    为什么与 `audit-paths.py` 相反（那边把字符串抹掉）：设置项的键名**就是字符串字面量**
    （`cfg.get("beepOnStart")`），抹掉字符串等于把唯一的信号抹掉 —— 这是本工具第一版
    把 68 个真实在用的键判成 DEAD 的根因。独立字符串（docstring）仍要丢，
    否则我自己写在注释性的模块文档里提一句键名就会被当成"读过了"。
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    if path.endswith(".py"):
        try:
            out = {}
            prev = None
            for tok in tokenize.tokenize(io.BytesIO(raw).readline):
                if tok.type in (tokenize.COMMENT, tokenize.ENCODING):
                    continue
                ln = tok.start[0]
                if tok.type == tokenize.STRING:
                    # 只丢**三引号文档串**（且是独立语句）。第一版按"独立字符串"判定，
                    # 把多行字典里的键（`"routerProbeInterval": (...)`）也丢掉了 ——
                    # 那行的前一个有意义 token 正是换行，与 docstring 长得一模一样，
                    # 于是 4 个真实在用的 router 设置被误判成 DEAD。
                    is_docstring = tok.string[:3] in ('"""', "'''") and prev in (
                        None, tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT)
                    if not is_docstring:
                        out[ln] = out.get(ln, "") + " " + tok.string
                    prev = tok.type
                    continue
                out[ln] = out.get(ln, "") + " " + tok.string
                if tok.type != tokenize.ENCODING:
                    prev = tok.type
            return sorted(out.items())
        except Exception:
            pass
    text = raw.decode("utf-8", "replace")
    return [(i, line.split("#", 1)[0]) for i, line in enumerate(text.splitlines(), 1)]


def _keys_from_config():
    """从 config.py 抓 DEFAULTS / DEFAULT_MIGRATIONS 的键名（不 import app/）。"""
    path = os.path.join(ROOT, "app", "config.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    keys = []
    for m in re.finditer(r'^\s{4}"([A-Za-z][A-Za-z0-9_]*)"\s*:\s*dict\(', src, re.M):
        keys.append(m.group(1))
    mig = re.search(r"DEFAULT_MIGRATIONS\s*=\s*\{(.*?)\n\}", src, re.S)
    migrated = re.findall(r'"([A-Za-z][A-Za-z0-9_]*)"\s*:', mig.group(1)) if mig else []
    flags = {}
    for key in keys:
        blk = re.search(r'^\s{4}"%s"\s*:\s*dict\((.*?)\n?\s*\),?\s*$' % re.escape(key),
                        src, re.M | re.S)
        body = blk.group(1) if blk else ""
        grp = re.search(r'grp="([a-z]+)"', body)
        sub = re.search(r'sub="([a-z]+)"', body)
        flags[key] = {
            "grp": grp.group(1) if grp else "?",
            # 二级小节：包含在 grp 里的再分节（见 config.SUBS + web/app.js 的 SET_SUB_NAMES）
            "sub": sub.group(1) if sub else "",
            "deprecated": "deprecated=True" in body,
            "hidden": "hidden=True" in body,
            "secret": "secret=True" in body,
            "label": (re.search(r'label="([^"]*)"', body) or [None, ""])[1],
        }
    return keys, migrated, flags


def scan():
    keys, migrated, flags = _keys_from_config()
    hits = {k: {"def": [], "read": [], "write": [], "panel": [], "panel_read": [],
                "indirect": []} for k in keys}
    for path in _iter_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        for ln, code in _code_lines(path):
            for key in keys:
                # 该行是否出现了这个键的字符串字面量（单引号或双引号）
                quoted = re.search('["\']%s["\']' % re.escape(key), code)
                if not quoted:
                    continue
                where = "%s:%d" % (rel, ln)
                is_panel = rel.startswith("web/")
                lit = '["\']%s["\']' % re.escape(key)
                # 定义行：tokenize 拼出来的行首只有一个空格，所以用 \s* 而不是 \s{4}
                is_def = rel == "app/config.py" and bool(
                    re.search(r'^\s*"%s"\s*:' % re.escape(key), code))
                if is_def:
                    hits[key]["def"].append(where)
                    continue
                if is_panel:
                    if any(re.search(p % re.escape(key), code) for p in PANEL_READ_PATTERNS):
                        hits[key]["panel_read"].append(where)
                    else:
                        hits[key]["panel"].append(where)
                    continue
                if any(re.search(p % lit, code) for p in WRITE_PATTERNS):
                    hits[key]["write"].append(where)
                elif re.search(r'\.\s*get\s*\(\s*%s' % lit, code) or \
                     re.search(r'\[\s*%s\s*\]' % lit, code) or \
                     re.search(r'get_setting\s*\(\s*%s' % lit, code) or \
                     re.search(r'settings_get\s*\(\s*%s' % lit, code) or \
                     re.search(r'_setting\s*\(\s*%s' % lit, code) or \
                     re.search(r'_s\s*\.\s*get\s*\(\s*%s' % lit, code):
                    # 直接读 / 字典式读（`cfg = settings` 后 cfg.get("k")）/ 包装函数读
                    # （`self.settings_get("k")`、`_setting("k")` 这类薄封装）
                    hits[key]["read"].append(where)
                else:
                    # 在 app/ 里被提到、但不是上面任何一种形态：多半是
                    # `for key in ("wakeHotkey", "fallbackHotkey", ...)` 这种**间接读**，
                    # 或者是配置写入器里的键清单。不算死，但要说清是"待人工确认"。
                    hits[key]["indirect"].append(where)
    return keys, flags, hits


def verdict(flags_one, hit):
    """判定优先级：app 里真读 > 面板自己读 > app 间接读 > 面板表单 > 谁都不碰。"""
    if flags_one["deprecated"]:
        return "DEPRECATED"
    app_read = [h for h in hit["read"] if h.startswith("app/") and not h.startswith("app/config.py")]
    if app_read:
        return "OK"
    if hit.get("panel_read"):
        return "PANEL-READ"           # 面板消费（需 ACK_PANEL 说明）
    app_ref = [h for h in hit["indirect"] if h.startswith("app/")]
    if app_ref:
        return "OK-INDIRECT"          # 间接读（循环变量/键清单）：需 ACK_INDIRECT 说明
    if hit["panel"]:
        return "PANEL-ONLY"
    return "DEAD"


def _unacknowledged(rows):
    """--check 要拦下的项：死项 / 只写没人读 / 没写进确认名单的间接读与面板消费。"""
    bad = []
    for key, f, h, v in rows:
        if v in ("DEAD", "PANEL-ONLY"):
            bad.append((key, f, h, v))
        elif v == "OK-INDIRECT" and key not in ACK_INDIRECT:
            bad.append((key, f, h, "OK-INDIRECT(未确认)"))
        elif v == "PANEL-READ" and key not in ACK_PANEL:
            bad.append((key, f, h, "PANEL-READ(未确认)"))
    return bad


def main(argv):
    keys, flags, hits = scan()
    rows = []
    for key in keys:
        rows.append((key, flags[key], hits[key], verdict(flags[key], hits[key])))
    order = {"DEAD": 0, "PANEL-ONLY": 1, "OK-INDIRECT": 2, "PANEL-READ": 3,
             "OK": 4, "DEPRECATED": 5}
    rows.sort(key=lambda r: (order[r[3]], r[1]["grp"], r[0]))

    bad = _unacknowledged(rows)
    counts = {}
    for _k, f, _h, v in rows:
        counts[v] = counts.get(v, 0) + 1
    print("ECHO settings audit")
    print("keys: %d   %s" % (len(keys), "  ".join("%s=%d" % kv for kv in sorted(counts.items()))))
    print("")
    for key, f, h, v in bad:
        print("  [%s] %s (grp=%s)" % (v, key, f["grp"]))
        for kind in ("def", "read", "write", "panel", "panel_read", "indirect"):
            if h.get(kind):
                print("        %-10s %s" % (kind, ", ".join(h[kind][:4])))

    md = ["# 设置清单审计（`scripts/audit-settings.py` 生成）", "",
          "对 `app/config.py:DEFAULTS` 的每一键，扫 `app/ web/ scripts/ mac/ dsh-failover/`：",
          "谁定义 / 谁写 / **谁读**。判定：", "",
          "* `OK` = app/ 的产品代码里真的读了；",
          "* `OK-INDIRECT` = app/ 里通过循环变量/键清单间接读（需在 `ACK_INDIRECT` 里写明谁读）；",
          "* `PANEL-READ` = 唯一消费方是面板自己（需在 `ACK_PANEL` 里写明用途）；",
          "* `PANEL-ONLY` = 只有面板把它渲染成表单/写进去，没人读（可疑）；",
          "* `DEAD` = 谁都不碰；`DEPRECATED` = 已弃用（不再展示、写入被拒收）。", "",
          "`--check` 在出现 DEAD / PANEL-ONLY / **未确认**的间接读与面板消费时返回 1；",
          "`tests/test_settings_wiring.py` 会跑这个检查，并额外验「每项都能读回」。", "",
          "| 键 | 分组 | 二级小节 | 标记 | 判定 | 读它的地方（app/ 内） | 面板读 | 写它的地方 |",
          "|---|---|---|---|---|---|---|---|"]
    for key, f, h, v in rows:
        tag = ",".join([t for t, on in (("deprecated", f["deprecated"]), ("hidden", f["hidden"]),
                                        ("secret", f["secret"])) if on]) or "-"
        reads = "<br>".join(h["read"][:4]) or "-"
        panel_reads = "<br>".join(h.get("panel_read", [])[:3]) or "-"
        writes = "<br>".join(h["write"][:3]) or "-"
        md.append("| `%s` | %s | %s | %s | **%s** | %s | %s | %s |"
                  % (key, f["grp"], f.get("sub") or "-", tag, v, reads, panel_reads, writes))
    out = os.path.join(ROOT, "docs", "settings-audit.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    print("")
    print("report: %s" % os.path.relpath(out, ROOT))
    if "--check" in argv and bad:
        print("CHECK FAILED: %d key(s) 需要处理或写进确认名单: %s"
              % (len(bad), ", ".join("%s(%s)" % (b[0], b[3]) for b in bad)))
        return 1
    if "--check" in argv:
        print("CHECK OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
