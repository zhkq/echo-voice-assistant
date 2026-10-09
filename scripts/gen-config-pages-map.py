# -*- coding: utf-8 -*-
"""把「通用 / 业务配置 / 能力与智能体」三个配置页的**当前结构**导成两份文件：

  * `docs/配置页结构.md`     —— 给人看/改的清单（页 → 卡 → 常用/高级 → 每一项的键/类型/默认/选项）
  * `docs/配置页结构.xmind`  —— 同一份数据的思维导图（XMind Zen/2020+ 直接打开）

**为什么要机械生成**（而不是手写一份）：用户 2026-10-02 要"整体调整"这三页的 IA，
而"屏幕上的结构"由 **三处**共同决定 ——

  1. `app/config.py` 的 `DEFAULTS`：每一项的标签/类型/默认/选项/说明（**字段真相**）；
  2. `web/app.js` 的 `SET_CARDS` / `SET_TABS` / `SET_ADV_SEC` / `SET_SHORT_LABELS` …：
     **哪张卡、常用还是高级、高级里归哪个小节**（**落点真相**，注释里写着"唯一的落点声明"）；
  3. `web/index.html` 的静态卡 + `SET_CARDS` 里那些 `() => …` 的**函数渲染块**
     （服务卡、转写走哪条路、智能体选择、模型路由合并卡…）——这部分**不在设置元数据里**，
     所以在本文件底部的 `UI_EXTRAS` 里手写维护（改界面就改它，并在 docstring 这里留话）。

用法：`python scripts/gen-config-pages-map.py`
前置：`node` 在 PATH 上（抽 `app.js` 的字面量用它求值，避免手抄漂移）。
改了 IA 之后**重跑本脚本**，别手改生成出来的两份文件。
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as cfg  # noqa: E402

MD_PATH = os.path.join(ROOT, "docs", "配置页结构.md")
XMIND_PATH = os.path.join(ROOT, "docs", "配置页结构.xmind")

PAGE_ORDER = ("settings", "business")   # 2026-10-02 IA：通用/能力与智能体并进「设置」
PAGE_TITLE = {"settings": "① 设置", "business": "② 业务配置"}

# ---------------------------------------------------------------- ① 从 app.js 抽落点表
EXTRACTOR = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function grab(name, kind) {
  const re = new RegExp("(?:const|let|var)\\s+" + name + "\\s*=\\s*", "m");
  const m = re.exec(src);
  if (!m) return null;
  let i = m.index + m[0].length;
  let depth = 0, j = i;
  for (; j < src.length; j++) {
    const c = src[j];
    if (c === "{" || c === "[" || c === "(") depth++;
    else if (c === "}" || c === "]" || c === ")") { depth--; if (depth === 0) { j++; break; } }
  }
  let text = src.slice(i, j);
  if (kind === "set") {
    const inner = text.replace(/^new\s+Set\(\s*\[/, "").replace(/\]\s*\)$/, "").replace(/\/\/[^\n]*/g, "");
    return inner.split(",").map((s) => s.trim().replace(/^["']|["']$/g, "")).filter(Boolean);
  }
  const clean = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "").replace(/\/\/[^\n"'`]*$/gm, "");
  return JSON.parse(JSON.stringify(eval("(" + clean + ")"), (k, v) => (typeof v === "function" ? "[函数渲染]" : v)));
}
const out = {};
for (const n of ["SET_TABS", "SET_CARDS", "SET_ADV_SEC", "SET_SHORT_LABELS", "SET_UNITS",
                 "SET_LOUD_NOTE", "SET_GROUP_NAMES", "SET_SUB_NAMES", "SET_MEETING_BACKENDS",
                 "SET_OPT_LABELS"]) out[n] = grab(n);
for (const n of ["SET_LOUD_DESC", "SET_PLACED_ELSEWHERE"]) out[n] = grab(n, "set");
process.stdout.write(JSON.stringify(out));
"""


def extract_ui_map() -> dict:
    js = os.path.join(ROOT, "web", "app.js")
    tmp = os.path.join(tempfile.gettempdir(), "echo-extract-ui-map.js")
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(EXTRACTOR)
    try:
        out = subprocess.run(["node", tmp, js], capture_output=True, timeout=120)
    except FileNotFoundError:
        raise SystemExit("找不到 node —— 抽 app.js 的落点表要用它（装了 node 再跑）")
    if out.returncode != 0:
        raise SystemExit("抽落点表失败：%s" % out.stderr.decode("utf-8", "ignore")[:600])
    return json.loads(out.stdout.decode("utf-8"))


# ---------------------------------------------------------------- ② 不在设置元数据里的界面块
#:
#: 手写维护（**改 index.html / 那些渲染函数就改这里**）。`SET_CARDS` 里 `common`/`adv` 写成
#: `"[函数渲染]"` 或带 `dynAfter` 的卡，它到底画了什么，只有这份表说得清。
UI_EXTRAS = {
    # 2026-10-02 IA：三个设置页签并成一个「设置」，静态卡也全搬进它 —— 所以非设置块都归这里。
    "settings": [
        {"kind": "dynamic", "card": "运行状态",
         "detail": "「运行状态」一行（服务/端口/版本原文）+ 两条条件警告（**正在录音**时提醒别重启、"
                   "配置端口与当前页面端口不一致时警告）",
         "buttons": ["重启 ECHO 服务", "启动日志 ↓"]},
        {"kind": "static", "card": "启动状态",
         "detail": "摘要 `#bootSummary` + 就绪说明 `#bootReadyNote` + 组件进程启停 `#bootOps`"
                   "（组件/模型的就绪清单**只在「能力与智能体」页渲染一处**）",
         "buttons": []},
        {"kind": "static", "card": "启动日志",
         "detail": "`#bootLogs`（自动刷新）+ 右上角实时状态 `#bootLive`；**默认收起**",
         "buttons": []},
        {"kind": "static", "card": "安装向导",
         "detail": "`#wizHost`：11 步首次安装/补能力流程；**默认收起**（2026-09-25 从顶层页签并进本页最后一张卡）",
         "buttons": []},
{"kind": "dynamic", "card": "AI 组件（Agent 那一段）",
         "detail": "常用：智能体下拉（`agentBackend`）+ 状态 chip + 原因说明；选了标准版 harness 时多一行"
                   "「家目录」（`harnessHome`）。\n高级：小节「产品开关」（`agentCodebuddyEnabled` / "
                   "`agentHarnessEnabled`）与「当前智能体的参数」（`harnessCommand` / `harnessPort` / "
                   "`harnessToken` / `dshBaseUrl` / `agentCustomPath`）",
         "buttons": ["检测", "浏览器打开"]},
        {"kind": "static", "card": "模型路由（合并卡，静态卡）",
         "detail": "`#rtMergeCard`：概要行 `#rtReg`；`## 语言模型` → `#rtLlmHost`；"
                   "`## 通道成员（顺序 = 优先级）` → `#rtMembers` + 候选下拉 `#rtCandSelect`；"
                   "`## 派发情况` → `#rtStats`；右上状态徽标 `#rtBadge`、`仪表盘 ↗`。\n"
                   "**高级**：`## 路由参数` → `#rtSetHost`（7 项 `router*` 设置，由 renderSettingsPanes 填）",
         "buttons": ["＋ 加入", "保存（本卡：成员+优先级+7 项参数）", "立即探测", "重载配置", "注册到 DSH"]},
        {"kind": "static", "card": "已配对后端连接情况（默认收起）",
         "detail": "说明段（音频会离开本机）；`#capPairBox`：配对串/地址 + 配对码 → 配对 / 解除配对；"
                   "「检测本机后端」（读本机配对文件）；「起本机后端 / 停掉它 / 就绪自测 / 生成 compose」；"
                   "计划与状态文本 `#capBackendPlan` `#capBackendReady` `#capBackendNotes` `#capPairState` "
                   "`#capBackendJob`；后端清单 `#capBackendList`；路由设置 `#capRouteSettings`"
                   "（`capabilityEchoServerUrl` + 指路一句）。真的配上时自动展开一次",
         "buttons": ["刷新", "配对", "解除配对", "检测本机后端", "起本机后端", "停掉它", "就绪自测", "生成 compose"]},
        {"kind": "static", "card": "本地能力部署运行情况（默认展开）",
         "detail": "`#capOvSummary` / `#capOverview` 总览；`#capKindCards`（按种类的模型卡）与 "
                   "`#capFuncCards`（按用途：ASR/分离/声纹…）都带「占用 / 上次使用 / 使用次数」；"
                   "`#capEnvHost`、`#envCheckHost` 环境自检；`#capModelsDirHost` = `modelsDir` 一行。"
                   "默认只展开「配置为要用的」，其余（whisper 三档等）折叠",
         "buttons": ["下载缺失", "刷新"]},
        {"kind": "static", "card": "清理（默认收起）",
         "detail": "`#cleanupBody`：近期没用过的模型（先预览后确认）+ `#cleanupDaysHost` = `modelCleanupDays` 一行；"
                   "标题徽标写「N 项建议 · 可释放 X」",
         "buttons": ["预览", "确认清理（卡内）"]},
    ],
    "business": [],
}

#: 已知重叠 / 遗留（机械部分看不出来，得人写；用户调整 IA 时最该先看这里）
KNOWN_ISSUES = [
    "**model / provider 两处都有入口**：`sttModel` / `ttsEngine` / `wakeEngine` / `device` / "
    "`voiceprint*` 在「业务配置」的卡里有控件，而它们的 `grp` 又让「能力」页签的老「用哪个实现」"
    "卡承载一次（`MODEL_KEYS` 的注释里写明了这是「四个非设置视图重做」要收口的遗留）。",
    "**会议那三个能力键是 hidden 的**：`capabilityMeetingAsrBackend` / `capabilityDiarizeBackend` / "
    "`capabilityEmbedBackend` 不在设置列表里下发，只由「业务配置 → 会议」的 `renderMeetingServiceCard()` "
    "合成一个单选 + 一个「分离与声纹由谁做」小节（`SET_MEETING_BACKENDS` 只有「能力后端 / 网络服务商」两档，"
    "**没有「本机」** —— 全本机跑 = 在本机起一个后端，取值仍是 `echo-server`）。",
    "**`meetingSttModel` 已废弃**（`deprecated=True`，接口不下发），`SET_PLACED_ELSEWHERE` 里也因此删过它。",
    "**没有下发通道的键**：`allowVirtualInputDevice`（虚拟/接力麦，默认关；说明写在「语音指令 → 高级 → 录音与转写」"
    "的小节注里）、`capabilityEchoServerToken`、`capabilityEchoServerStaticToken`。",
    "**`modelCleanupDays` / `modelsDir` / `capabilityEchoServerUrl`** 三个键不在 `SET_CARDS` 里，"
    "由「能力与智能体」页的三张静态卡自己画（见 `SET_PLACED_ELSEWHERE`）。",
    "**「队列」卡没有设置项**：整张卡是 `renderQueueCard()` 一个函数（执行队列 + 最近几条 + 两个跳历史按钮）。",
]


# ---------------------------------------------------------------- ③ 组装数据
def settings_meta() -> dict:
    out = {}
    for key, spec in cfg.DEFAULTS.items():
        out[key] = {
            "label": spec.get("label", ""), "grp": spec.get("grp", ""), "sub": spec.get("sub", ""),
            "type": spec.get("value_type", ""), "value": spec.get("value"),
            "options": list(spec.get("options") or []),
            "hidden": bool(spec.get("hidden")), "deprecated": bool(spec.get("deprecated")),
            "desc": re.sub(r"\s+", " ", (spec.get("description") or "")).strip(),
        }
    return out


def adv_section_of(key: str, meta: dict, ui: dict, card: dict) -> str:
    """**逐字照搬** `web/app.js::sAdvSection()` 的规则（别自己发明）：

        SET_ADV_SEC[键] || SET_SUB_NAMES[键的 sub] || 卡片的 advDefault || "其他"

    第三档很容易漏：`SET_CARDS.general` 的「运行状态」卡就是靠 `advDefault: "端口"`
    把 `serverPort` 挂进「端口」小节，而 `SET_ADV_SEC` 里**没有**它。
    """
    if key in (ui.get("SET_ADV_SEC") or {}):
        return ui["SET_ADV_SEC"][key]
    sub = (meta.get(key) or {}).get("sub") or ""
    if sub and (ui.get("SET_SUB_NAMES") or {}).get(sub):
        return ui["SET_SUB_NAMES"][sub]
    return card.get("advDefault") or "其他"


def short_label(key: str, meta: dict, ui: dict) -> str:
    return (ui.get("SET_SHORT_LABELS") or {}).get(key) or (meta.get(key) or {}).get("label") or key


def option_text(key: str, meta: dict, ui: dict) -> str:
    m = meta.get(key) or {}
    if m["type"] == "bool":
        return "开 / 关"
    opts = m["options"]
    if not opts:
        return ""
    short = (ui.get("SET_OPT_LABELS") or {}).get(key) or {}
    out = []
    for o in opts:
        # options 有**两种写法**：裸字符串，或 `{"value","label"}`。
        # 2026-10-09 修：后者一直让本脚本崩在 `short.get(o)`（dict 当键 →
        # `TypeError: unhashable type: 'dict'`）—— 于是 `docs/配置页结构.md` 从
        # `dailyReviewSttBackend` 换成 dict 写法那天起就再也 regenerate 不了（**不报错给用户看**，
        # 只是脚本 exit 1）。两种写法都要认。
        value = o.get("value") if isinstance(o, dict) else o
        label = (o.get("label") if isinstance(o, dict) else None) or short.get(value) or str(value)
        label = str(label)
        key_text = str(value)
        out.append(("%s（%s）" % (label, key_text)) if label != key_text else key_text)
    return " / ".join(out)


def value_text(key: str, meta: dict) -> str:
    m = meta.get(key) or {}
    v = m["value"]
    if m["type"] == "bool":
        return "开" if v else "关"
    if v in (None, ""):
        return "（空）"
    return str(v)


def build(ui: dict) -> dict:
    meta = settings_meta()
    placed = set(ui.get("SET_PLACED_ELSEWHERE") or [])
    loud = set(ui.get("SET_LOUD_DESC") or [])
    tabs = {t["id"]: t for t in (ui.get("SET_TABS") or [])}
    pages = []
    for page in PAGE_ORDER:
        cards = []
        for card in (ui.get("SET_CARDS") or {}).get(page, []):
            common = card.get("common")
            adv = card.get("adv")
            rows_common = [] if not isinstance(common, list) else common
            rows_adv = [] if not isinstance(adv, list) else adv
            dyn = "[函数渲染]" if (isinstance(common, str) and common.startswith("[函数")) else ""
            adv_dyn = "[函数渲染]" if (isinstance(adv, str) and adv.startswith("[函数")) else ""
            secs, seen = [], {}
            for key in rows_adv:
                sec = adv_section_of(key, meta, ui, card)
                seen.setdefault(sec, []).append(key)
            for sec in list(card.get("advOrder") or []) + [s for s in seen if s not in (card.get("advOrder") or [])]:
                if sec in seen and sec not in [s["name"] for s in secs]:
                    secs.append({"name": sec, "keys": seen[sec]})
            cards.append({
                "id": card.get("id", ""), "title": card.get("title", ""),
                "hint": card.get("hint", ""), "help": card.get("help", ""),
                "note": card.get("note", ""), "advDefault": card.get("advDefault", ""),
                "covers": card.get("covers") or [],
                "common": rows_common, "commonDyn": dyn,
                "advDyn": adv_dyn, "advSections": secs,
                "advNote": {k: ("[函数渲染]" if isinstance(v, str) and v.startswith("[函数") else "[HTML 说明]")
                            for k, v in (card.get("advNote") or {}).items()},
                "dynAfter": "[函数渲染]" if card.get("dynAfter") else "",
            })
        pages.append({
            "id": page, "title": PAGE_TITLE[page], "who": (tabs.get(page) or {}).get("who", ""),
            "cards": cards,
            "extras": UI_EXTRAS.get(page, []),
            "n_common": sum(len(c["common"]) for c in cards),
            "n_adv": sum(len(s["keys"]) for c in cards for s in c["advSections"]),
        })
    # **没落点的键**：不是这三页任何卡的 common/adv/covers，也不在 SET_PLACED_ELSEWHERE。
    # 它们运行时掉进 `_fallbackCards()` 的「未归类」卡 —— 用户调 IA 时最该先看这张清单。
    accounted = set(placed)
    for p in pages:
        for c in p["cards"]:
            accounted.update(c["common"])
            accounted.update(k for s in c["advSections"] for k in s["keys"])
            accounted.update(c["covers"])
    unaccounted = sorted(k for k, m in meta.items() if k not in accounted)
    return {"pages": pages, "meta": meta, "ui": ui, "placed": sorted(placed), "loud": sorted(loud),
            "unaccounted": unaccounted, "generated": time.strftime("%Y-%m-%d %H:%M")}


# ---------------------------------------------------------------- ④ 出 Markdown
def md_row(key, data, *, level_note=False):
    meta = data["meta"]
    ui = data["ui"]
    m = meta.get(key) or {}
    flags = []
    if m.get("hidden"):
        flags.append("hidden")
    if m.get("deprecated"):
        flags.append("废弃")
    if key in data["loud"]:
        flags.append("说明露在明面")
    if key in data["placed"]:
        flags.append("落点在别处")
    label = short_label(key, meta, ui)
    full = m.get("label") or ""
    unit = (ui.get("SET_UNITS") or {}).get(key, "")
    cells = ["`%s`" % key, label, (full if full != label else ""), m.get("type", ""),
             value_text(key, meta), option_text(key, meta, ui), unit,
             "、".join(flags)]
    return "| " + " | ".join(c if c else "—" for c in cells) + " |"


def render_md(data: dict) -> str:
    out = []
    w = out.append
    w("# ECHO 配置页结构（通用 · 业务配置 · 能力与智能体）\n")
    w("> 生成时间：%s ｜ **别手改这份文件**，改结构后重跑 `python scripts/gen-config-pages-map.py`。" % data["generated"])
    w(">")
    w("> 数据来源：① `app/config.py` 的 `DEFAULTS`（字段真相：标签/类型/默认/选项/说明）；")
    w("> ② `web/app.js` 的 `SET_CARDS` / `SET_TABS` / `SET_ADV_SEC` / `SET_SHORT_LABELS` / `SET_UNITS`（落点真相：哪张卡、常用还是高级、归哪个小节）；")
    w("> ③ `web/index.html` 的静态卡与 `SET_CARDS` 里的函数渲染块（**不在设置元数据里**，手写在生成器底部的 `UI_EXTRAS`）。\n")
    w("字段含义：**短标签**＝行里实际显示的词（`SET_SHORT_LABELS`，完整名在 `?` 浮窗）；**类型** `str/int/float/bool/enum`；")
    w("**选项**列里 `()` 内是写回后端的真实取值。\n")

    w("## 0. 总览\n")
    w("| 页 | 这一页管什么（`SET_TABS.who`） | 设置卡 | 常用项 | 高级项 | 非设置块 |")
    w("|---|---|---|---|---|---|")
    for p in data["pages"]:
        w("| **%s** | %s | %s | %d | %d | %d |" % (
            p["title"], p["who"], " · ".join(c["title"] for c in p["cards"]),
            p["n_common"], p["n_adv"], len(p["extras"])))
    total = sum(p["n_common"] + p["n_adv"] for p in data["pages"])
    w("")
    w("设置项合计（这三页的卡里）：**%d 项**；`DEFAULTS` 全集 %d 项（差额是别处渲染的 hidden / 废弃 / "
      "落点在别处的键，见附录）。\n" % (total, len(data["meta"])))

    for p in data["pages"]:
        w("---\n")
        w("## %s\n" % p["title"])
        w("> **这一页管什么**：%s\n" % p["who"])
        for i, c in enumerate(p["cards"], 1):
            w("### %s.%d 卡：%s\n" % (PAGE_TITLE[p["id"]][0], i, c["title"]))
            if c["hint"]:
                w("*（提示）* %s\n" % c["hint"])
            if c["help"]:
                w("*（`?` 浮窗）* %s\n" % c["help"])
            if c["common"] or c["commonDyn"]:
                w("**常用**\n")
                if c["commonDyn"]:
                    w("- %s（这张卡的常用区整块是函数画的，见下面的「非设置块」）" % c["commonDyn"])
                if c["common"]:
                    w("")
                    w("| 键 | 短标签 | 完整标签 | 类型 | 默认 | 选项 | 单位 | 备注 |")
                    w("|---|---|---|---|---|---|---|---|")
                    for k in c["common"]:
                        w(md_row(k, data))
                w("")
            if c["advSections"] or c["advDyn"]:
                w("**高级**（默认收起%s）\n" % ("，`advDefault` = %s" % c["advDefault"] if c["advDefault"] else ""))
                if c["advDyn"]:
                    w("- %s（整块函数画的）" % c["advDyn"])
                for sec in c["advSections"]:
                    w("")
                    w("*小节：%s*%s" % (sec["name"],
                                        "（有额外说明 " + "、".join(c["advNote"].keys()) + "）"
                                        if sec["name"] in c["advNote"] else ""))
                    w("")
                    w("| 键 | 短标签 | 完整标签 | 类型 | 默认 | 选项 | 单位 | 备注 |")
                    w("|---|---|---|---|---|---|---|---|")
                    for k in sec["keys"]:
                        w(md_row(k, data))
                w("")
            if c["covers"]:
                w("**这张卡还「代管」（`covers`）这些键**（它们 hidden / 由函数画，不在上面的表里）：%s\n"
                  % "、".join("`%s`" % k for k in c["covers"]))
            if c["dynAfter"]:
                w("**卡内附加块（`dynAfter`）**：%s（见「非设置块」）\n" % c["dynAfter"])
        if p["extras"]:
            w("### %s 的非设置块（静态卡 / 函数渲染）\n" % PAGE_TITLE[p["id"]][0])
            w("| 种类 | 卡片 | 内容 | 按钮 |")
            w("|---|---|---|---|")
            for e in p["extras"]:
                w("| %s | %s | %s | %s |" % ("静态卡" if e["kind"] == "static" else "函数渲染",
                                             e["card"], e["detail"].replace("\n", "<br>"),
                                             "、".join(e["buttons"]) or "—"))
            w("")

    w("---\n")
    w("## 附录 A：落点在别处的键（`SET_PLACED_ELSEWHERE`）\n")
    for k in data["placed"]:
        m = data["meta"].get(k) or {}
        w("- `%s`（%s）—— %s" % (k, m.get("label", ""), m.get("desc", "")[:90]))
    w("")
    w("## 附录 B：界面对这几项的「特殊处理」\n")
    w("- **说明露在明面上**（不平铺进 `?` 浮窗，只有不可逆/出网/生物特征这类）：%s"
      % "、".join("`%s`" % k for k in data["loud"]))
    w("- **行里用短标签**的项数：%d（完整内容见各表「短标签」列）" % len(data["ui"].get("SET_SHORT_LABELS") or {}))
    w("- **带单位**的项数：%d" % len(data["ui"].get("SET_UNITS") or {}))
    w("- **下拉用短文案**的项：%s" % "、".join("`%s`" % k for k in (data["ui"].get("SET_OPT_LABELS") or {})))
    w("- **`hidden=True`（界面不列，但接口下发）的键**：%s"
      % "、".join("`%s`" % k for k, m in sorted(data["meta"].items()) if m.get("hidden")) or "（无）")
    w("- **`deprecated=True`（接口不下发）的键**：%s"
      % "、".join("`%s`" % k for k, m in sorted(data["meta"].items()) if m.get("deprecated")) or "（无）")
    w("")
    w("## 附录 C：已知重叠 / 遗留（调 IA 时先看这几条）\n")
    for s in KNOWN_ISSUES:
        w("- %s" % s)
    w("")
    w("## 附录 D：**没出现在这三页任何卡片里**的键\n")
    w("这些键不会被 `SET_CARDS` 画出来，运行时会掉进 `_fallbackCards()` 的「未归类」卡"
      "（`web/app.js` 的注释：这样「新加的设置项不会静默消失」）。**调 IA 时从这里往下清**："
      "要么给它一个落点，要么标 `hidden` / `deprecated`。\n")
    n_hidden = sum(1 for k in data["unaccounted"] if data["meta"][k].get("hidden"))
    n_dep = sum(1 for k in data["unaccounted"] if data["meta"][k].get("deprecated"))
    n_vis = sum(1 for k in data["unaccounted"]
                if not data["meta"][k].get("hidden") and not data["meta"][k].get("deprecated"))
    w("**结论先说**：共 %d 项 —— hidden %d、废弃 %d、**可见却没落点 %d**"
      "（可见的那几个才是「界面会多出一张未归类卡」的原因）。\n"
      % (len(data["unaccounted"]), n_hidden, n_dep, n_vis))
    if not data["unaccounted"]:
        w("（没有 —— 每个可见键都有落点）\n")
    else:
        w("| 键 | 标签 | 组 | 值类型 | 默认 | hidden | 废弃 | 说明 |")
        w("|---|---|---|---|---|---|---|---|")
        for k in data["unaccounted"]:
            m = data["meta"][k]
            w("| `%s` | %s | %s | %s | %s | %s | %s | %s |" % (
                k, m["label"], m["grp"] + ("/" + m["sub"] if m["sub"] else ""), m["type"],
                value_text(k, data["meta"]), "是" if m["hidden"] else "—",
                "是" if m["deprecated"] else "—", m["desc"][:70]))
        w("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- ⑤ 出 XMind
def _id_gen():
    n = 0
    while True:
        n += 1
        yield "topic-%d" % n


_IDS = _id_gen()


def render_xmind(data: dict) -> bytes:
    def topic(title, children=None):
        t = {"id": next(_IDS), "class": "topic", "title": title}
        kids = [c for c in (children or []) if c]
        if kids:
            t["children"] = {"attached": kids}
        return t

    def row_topic(key):
        m = data["meta"].get(key) or {}
        label = short_label(key, data["meta"], data["ui"])
        bits = ["%s（%s）" % (label, key)]
        if m.get("type") == "bool":
            bits.append("开关，默认%s" % ("开" if m.get("value") else "关"))
        else:
            bits.append("默认 %s" % value_text(key, data["meta"]))
            if option_text(key, data["meta"], data["ui"]):
                bits.append("选项：" + option_text(key, data["meta"], data["ui"]))
        if key in data["placed"]:
            bits.append("落点在别处")
        if key in data["loud"]:
            bits.append("说明露在明面")
        return topic(" ｜ ".join(bits))

    page_topics = []
    for p in data["pages"]:
        card_topics = []
        for c in p["cards"]:
            kids = []
            if c["hint"]:
                kids.append(topic("提示：" + c["hint"]))
            if c["help"]:
                kids.append(topic("? 说明：" + c["help"]))
            common_kids = [row_topic(k) for k in c["common"]]
            if c["commonDyn"]:
                common_kids.append(topic("（整块函数渲染）"))
            if common_kids:
                kids.append(topic("常用（%d）" % len(c["common"]), common_kids))
            adv_kids = []
            for sec in c["advSections"]:
                adv_kids.append(topic("小节：%s" % sec["name"], [row_topic(k) for k in sec["keys"]]))
            if c["advDyn"]:
                adv_kids.append(topic("（整块函数渲染）"))
            if adv_kids:
                kids.append(topic("高级（%d 项，默认收起%s）"
                                  % (sum(len(s["keys"]) for s in c["advSections"]),
                                     "；默认展开小节：" + c["advDefault"] if c["advDefault"] else ""),
                                  adv_kids))
            if c["covers"]:
                kids.append(topic("代管的 hidden 键", [topic("`%s`" % k) for k in c["covers"]]))
            if c["dynAfter"]:
                kids.append(topic("卡内附加块（函数渲染）"))
            card_topics.append(topic("卡：%s" % c["title"], kids))
        extra_kids = []
        for e in p["extras"]:
            ek = [topic("内容：" + e["detail"].replace("\n", " "))]
            if e["buttons"]:
                ek.append(topic("按钮：" + "、".join(e["buttons"])))
            extra_kids.append(topic("%s：%s" % ("静态卡" if e["kind"] == "static" else "函数渲染块", e["card"]), ek))
        page_topics.append(topic("%s —— %s" % (p["title"], p["who"]), card_topics + extra_kids))

    appendix = [
        topic("附录 A：落点在别处的键", [topic("`%s`" % k) for k in data["placed"]]),
        topic("附录 B：特殊处理", [
            topic("说明露在明面", [topic("`%s`" % k) for k in data["loud"]]),
            topic("行里用短标签的项数：%d" % len(data["ui"].get("SET_SHORT_LABELS") or {})),
            topic("hidden 键", [topic("`%s`" % k) for k, m in sorted(data["meta"].items()) if m.get("hidden")]),
            topic("废弃键", [topic("`%s`" % k) for k, m in sorted(data["meta"].items()) if m.get("deprecated")]),
        ]),
        topic("附录 C：已知重叠 / 遗留", [topic(s.replace("**", "")) for s in KNOWN_ISSUES]),
        topic("附录 D：没落点的键（%d）" % len(data["unaccounted"]),
              [topic("`%s`（%s%s）" % (k, data["meta"][k].get("label", ""),
                                      "，hidden" if data["meta"][k].get("hidden") else
                                      ("，已废弃" if data["meta"][k].get("deprecated") else "，**可见却没落点**")))
               for k in data["unaccounted"]]),
    ]
    root = topic("ECHO 配置页结构（%d 项设置 / 3 页 / %d 张设置卡）"
                 % (len(data["meta"]), sum(len(p["cards"]) for p in data["pages"])),
                 page_topics + appendix)
    content = [{"id": "sheet-1", "class": "sheet", "title": "配置页结构", "rootTopic": root}]
    manifest = {"file-entries": {"content.json": {}, "metadata.json": {}}}
    metadata = {"creator": {"name": "ECHO gen-config-pages-map.py", "version": "1.0"}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("content.json", json.dumps(content, ensure_ascii=False, indent=1))
        z.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=1))
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return buf.getvalue()


def main() -> int:
    ui = extract_ui_map()
    data = build(ui)
    md = render_md(data)
    with io.open(MD_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(md)
    with open(XMIND_PATH, "wb") as fh:
        fh.write(render_xmind(data))
    print("[ok] %s（%d 行）" % (MD_PATH, md.count("\n")))
    print("[ok] %s（%.1f KB）" % (XMIND_PATH, os.path.getsize(XMIND_PATH) / 1024))
    for p in data["pages"]:
        print("     %-16s 设置卡 %d 张（常用 %d / 高级 %d）+ 非设置块 %d"
              % (p["title"], len(p["cards"]), p["n_common"], p["n_adv"], len(p["extras"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
