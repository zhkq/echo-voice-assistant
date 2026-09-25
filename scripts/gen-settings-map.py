# -*- coding: utf-8 -*-
"""从真实的 `app.config.DEFAULTS` 生成 `docs/设置项归属表.md`（可复现，别手改表）。

规矩（用户 2026-09-25 定）：**不能莫名取消** —— 每个真实设置项都必须落在三种处置之一：
  * 保留
  * 合并到 X（说明并进哪一项）
  * 已过时（说明被什么取代）
没有归属判断的写「待定」，等拍板，绝不静默消失。

用法：`python scripts/gen-settings-map.py`
生成表后**不要手工编辑**（会与 DEFAULTS 漂移）—— 要改归属就改本文件里的 SPECIAL / GRP_RULES。

2026-09-25 修正（设置区一稿的实现方指出）：
  * 代码里 `deprecated=True` 的项**任何接口都不下发**，原来表里把它们当"保留"了
    （`dshStartCommand` / `dshNodePath` / `dshPackageDir`）。现在直接从 `meta["deprecated"]` 判，
    不再靠人工清单 —— 少写一个就漏一个，这次就是这么漏的。
  * 另有几个键**没有任何接口下发**（`allowVirtualInputDevice`、`capabilityEchoServerToken`、
    `capabilityEchoServerStaticToken`）：表里单独点名，处置写"保留（当前无下发通道）"，
    免得看成"界面忘了放"。
"""
import collections
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as cfg  # noqa: E402

#: 键 -> (归属页签/卡片, 常用/高级, 处置)。特判优先于 grp 兜底规则。
SPECIAL = {
    # 会议能力：三项合成两个控件（界面层已实现；配置层合并是下一步）
    "capabilityMeetingAsrBackend": ("能力后端 / 会议转写服务", "常用", "合并 → 会议转写服务（ECHO 后端/本机/网络服务商）"),
    "capabilityDiarizeBackend": ("能力后端 / 会议转写服务", "常用", "合并 → 会议产出（全部/说话人/只要文字）"),
    "capabilityEmbedBackend": ("能力后端 / 会议转写服务", "常用", "合并 → 会议产出（全部/说话人/只要文字）"),
    "capabilityPrivacy": ("能力后端 / 会议转写服务", "高级", "保留（出网许可，约束三个后端；界面层实现方建议与选择一致性一起显示）"),
    # 在线服务：从「智能体」挪到「语音与设备 / 命令采集」
    "providerAsr": ("语音与设备 / 命令采集", "高级", "保留（转写引擎下拉里的「在线服务」）"),
    "providerAsrBaseUrl": ("语音与设备 / 命令采集", "高级", "保留（同上）"),
    "providerAsrApiKey": ("语音与设备 / 命令采集", "高级", "保留（密钥，接口永不回显）"),
    "providerAsrModel": ("语音与设备 / 命令采集", "高级", "保留（同上）"),
    # 工作区：提到「智能体 / 工作区」常显
    "commandWorkspace": ("智能体 / 工作区", "常用", "保留（指令空间）"),
    "meetingWorkspace": ("智能体 / 工作区", "常用", "保留（会议工作区）"),
    "meetingsDir": ("语音与设备 / 会议录音", "常用", "保留（音频存放；与会议工作区同目录，**界面应显示解析后的真实路径**）"),
    # 引擎与设备
    "sttModel": ("语音与设备 / 命令采集", "常用", "保留（转写引擎）"),
    "meetingSttModel": ("能力后端 / 本机", "常用", "保留（本机跑会议时的引擎）"),
    "sttDevice": ("语音与设备 / 命令采集", "高级", "保留（推理设备）"),
    "ttsEngine": ("语音与设备 / 任务反馈", "常用", "保留（朗读）"),
    "wakeEnabled": ("语音与设备 / 命令采集", "常用", "保留"),
    "inputDeviceId": ("语音与设备 / 命令采集", "常用", "保留（收音设备）"),
    "meetingInputDeviceId": ("语音与设备 / 会议录音", "常用", "保留（会议麦克风）"),
    "meetingSegmentMinutes": ("语音与设备 / 会议录音", "常用", "保留（分段时长）"),
    "outputDeviceIds": ("语音与设备 / 任务反馈", "高级", "保留（**仍是单选，语义待改** —— 它其实是优先级列表）"),
    "commandOutputDeviceId": ("语音与设备 / 任务反馈", "高级", "保留"),
    "meetingOutputDeviceId": ("语音与设备 / 任务反馈", "高级", "保留（实现方建议：留在任务反馈）"),
    "triggerKeys": ("语音与设备 / 命令采集", "高级", "保留（**仍是逗号文本框，语义待改**）"),
    # 同一功能被劈两处：实现方指出，采纳 → 三项都归「常规 / 界面」
    "minimalReplyHint": ("常规 / 界面", "常用", "保留（与 minimalReply/minimalReplyChars 同页，别再劈开）"),
    "minimalReply": ("常规 / 界面", "高级", "保留（同上；原来归「语音与设备/任务反馈」，实现方指出应合到一起）"),
    "minimalReplyChars": ("常规 / 界面", "高级", "保留（同上）"),
    "meetingDiarize": ("语音与设备 / 会议录音", "常用", "保留（实现方指出放「命令采集」不合理，改到会议录音）"),
    # 服务/运维
    "serverPort": ("常规 / 服务", "高级", "保留（面板端口）"),
    "agentBackend": ("智能体 / 智能体", "常用", "保留（选哪个智能体）"),
    "harnessHome": ("智能体 / 智能体", "常用", "保留（家目录）"),
    "harnessCommand": ("智能体 / 智能体", "高级", "保留（启动命令覆盖）"),
    # 有键、无下发通道（实现方点名）——明确写出来，别看成"界面忘了放"
    "allowVirtualInputDevice": ("语音与设备 / 命令采集", "高级", "保留（**当前无下发通道**：voice 组 hidden 且不在 /api/capability 的 settings 里；要可编辑得后端加）"),
    "capabilityEchoServerToken": ("能力后端 / 后端面板", "高级", "保留（**当前无下发通道**：ROUTING_KEYS 刻意排除，怕与配对令牌混淆）"),
    "capabilityEchoServerStaticToken": ("能力后端 / 后端面板", "高级", "保留（同上）"),
    # 已过时（代码里已有弃用迁移，老值会折进新项）
    "worklogMode": ("—", "—", "**已过时**：与 worklogEnabled 重复，代码里有弃用迁移"),
    "providerTts": ("—", "—", "**已过时**：与 ttsEngine 重复，代码里有弃用迁移"),
}

#: grp -> (归属, 常用/高级) 兜底规则（分组名取自真实导出的 12 个）
GRP_RULES = {
    "panel": ("常规 / 界面", "常用"),
    "paths": ("智能体 / 工作区", "常用"),
    "dsh": ("智能体 / 智能体", "高级"),
    "agent": ("智能体 / 智能体", "高级"),
    "router": ("智能体 / 通道设置", "高级"),
    "wake": ("语音与设备 / 命令采集", "高级"),
    "model": ("语音与设备 / 命令采集", "高级"),
    "voice": ("语音与设备", "高级"),
    "meeting": ("语音与设备 / 会议录音", "高级"),
    "provider": ("语音与设备 / 命令采集", "高级"),
    "capability": ("能力后端 / 会议转写服务", "高级"),
    "worklog": ("智能体 / 智能体", "高级"),
}


def build_rows():
    rows = []
    for key, meta in cfg.DEFAULTS.items():
        grp = str(meta.get("grp") or "")
        opts = meta.get("options")
        dest, level, disp = SPECIAL.get(key, (None, None, None))
        if meta.get("deprecated"):
            dest, level = "—", "—"
            disp = "**已过时**：`deprecated=True`，任何接口都不下发（点名的替代项见说明）"
        elif dest is None:
            dest, level = GRP_RULES.get(grp, ("待定", "待定"))
            disp = "保留（待确认归属）" if dest != "待定" else "**待定**"
        kind = meta.get("value_type") or ("bool" if isinstance(meta.get("value"), bool) else "str")
        rows.append(dict(key=key, label=str(meta.get("label") or ""), grp=grp,
                         hidden=bool(meta.get("hidden")), kind=kind,
                         opts=("" if not opts else "/".join(map(str, opts))[:60]),
                         dest=dest, level=level, disp=disp))
    return rows


def main() -> int:
    rows = build_rows()
    by_grp = collections.OrderedDict()
    for r in rows:
        by_grp.setdefault(r["grp"] or "(无分组)", []).append(r)

    out = io.StringIO()
    out.write("# ECHO 设置项归属表（从 `app.config.DEFAULTS` 机械导出，%d 项）\n\n" % len(rows))
    out.write("> 本表由 `scripts/gen-settings-map.py` 生成，**不要手工编辑**（会与代码漂移）。\n")
    out.write("> 改归属请改生成器里的 `SPECIAL` / `GRP_RULES`，然后重跑。\n\n")
    out.write("**规矩：不能莫名取消。** 每一项都必须落在三种处置之一 —— **保留** / **合并到 X** / **已过时（写明被谁取代）**；\n")
    out.write("归属没定的写「**待定**」，等拍板，绝不静默消失。\n\n")
    out.write("| 处置 | 含义 |\n|---|---|\n")
    out.write("| 保留 | 原样存在，只挪位置 / 改展示（常用 or 高级） |\n")
    out.write("| 合并 → X | 并入另一项（老库按迁移表折算，行为不变） |\n")
    out.write("| 已过时 | 有据可查的重复项 / 不再需要的项，写明取代者 |\n\n")

    cnt = collections.Counter()
    for r in rows:
        d = r["disp"]
        cnt["已过时" if d.startswith("**已过时**") else
            ("待定" if "待定" in d else ("合并" if d.startswith("合并") else "保留"))] += 1
    out.write("**统计**：" + " · ".join("%s = %d" % (k, cnt[k]) for k in ("保留", "合并", "已过时", "待定") if cnt[k]) + "\n\n")

    for grp, items in by_grp.items():
        out.write("## 现分组 `%s`（%d 项）\n\n" % (grp, len(items)))
        out.write("| 键 | 标签 | 类型/选项 | 隐藏 | 归属（页签 / 卡片） | 层级 | 处置 |\n")
        out.write("|---|---|---|---|---|---|---|\n")
        for r in items:
            out.write("| `%s` | %s | %s%s | %s | %s | %s | %s |\n" % (
                r["key"], r["label"] or "—", r["kind"],
                ("：" + r["opts"]) if r["opts"] else "", "是" if r["hidden"] else "",
                r["dest"], r["level"], r["disp"]))
        out.write("\n")

    pending = [r for r in rows if "待定" in r["disp"]]
    out.write("## 待定项清单（需要拍板）\n\n")
    out.write("（无）\n" if not pending else "".join(
        "- `%s`（%s）现分组 `%s`\n" % (r["key"], r["label"] or "无标签", r["grp"]) for r in pending))

    dst = os.path.join(ROOT, "docs", "设置项归属表.md")
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out.getvalue())
    print("写入 %s" % dst)
    print("总项数 %d；" % len(rows) + " · ".join("%s=%d" % (k, cnt[k]) for k in ("保留", "合并", "已过时", "待定") if cnt[k]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
