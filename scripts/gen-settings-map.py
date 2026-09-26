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
#:
#: 2026-09-26（IA 重构）：用户把配置重新分成**三类 + 一个历史位** ——
#:   ① ECHO 通用（快捷键 · 打开/启动形式 · 运行状态）
#:   ② 业务配置（会议 · 语音指令 · 朗读与反馈 · 队列 · 工作区与归档）
#:   ③ 能力与智能体（智能体选择 · 模型路由 · 设备选择；下面是已配对后端 / 本地能力 / 清理）
#:   ④ 历史（指令历史 · 会议历史；**这一轮只预留位置**）
#: 所以下面的归属名全部换成新页签名；`grp`（库里那份分组元数据）保持原样不动 ——
#: 它是"键来自哪一族"的历史事实，翻新它只会让老库的元数据发生无意义的改写。
SPECIAL = {
    # 会议能力：转写 / 分离 / 声纹"由谁做" 三行现在都在「业务配置 → 会议」卡里
    # （2026-09-26 从「模型路由 → 会议能力通道」挪过去：那是会议的配置，不是语言模型路由的）。
    "capabilityMeetingAsrBackend": ("业务配置 / 会议 · 转写走哪条路", "常用",
                                    "合并 → 会议「转写走哪条路」单选（ECHO 后端/本机/网络服务商）"),
    "capabilityDiarizeBackend": ("业务配置 / 会议 · 转写走哪条路", "常用",
                                 "保留（谁做说话人分离；**会议一定会分离**，这里只选由谁做）"),
    "capabilityEmbedBackend": ("业务配置 / 会议 · 转写走哪条路", "常用",
                               "保留（谁做声纹嵌入；识别是标配，入库由用户决定）"),
    "capabilityPrivacy": ("业务配置 / 会议", "高级", "保留（出网许可，约束三个会议后端）"),
    # 在线服务：留在「业务配置 / 语音指令」（转写引擎下拉里的「在线服务」）
    "providerAsr": ("业务配置 / 语音指令", "高级", "保留（转写引擎下拉里的「在线服务」）"),
    "providerAsrBaseUrl": ("业务配置 / 语音指令", "高级", "保留（同上）"),
    "providerAsrApiKey": ("业务配置 / 语音指令", "高级", "保留（密钥，接口永不回显）"),
    "providerAsrModel": ("业务配置 / 语音指令", "高级", "保留（同上）"),
    # 工作区：提到「业务配置 / 工作区与归档」常显
    "commandWorkspace": ("业务配置 / 工作区与归档", "常用", "保留（指令空间）"),
    "meetingWorkspace": ("业务配置 / 工作区与归档", "常用", "保留（会议工作区）"),
    "meetingsDir": ("业务配置 / 会议", "常用", "保留（音频存放；与会议工作区同目录，**界面应显示解析后的真实路径**）"),
    # 引擎与设备
    "sttModel": ("业务配置 / 语音指令", "常用", "保留（转写引擎）"),
    "meetingSttModel": ("能力与智能体 / 本地能力", "常用",
                        "保留（本机跑会议时的引擎；选择入口在「业务配置 → 会议 → 转写走哪条路」）"),
    "sttDevice": ("能力与智能体 / 设备选择", "高级", "保留（推理设备）"),
    "device": ("能力与智能体 / 设备选择", "常用", "保留（推理设备：auto/cpu/cuda）"),
    "ttsEngine": ("业务配置 / 朗读与反馈", "常用", "保留（朗读）"),
    "wakeEnabled": ("业务配置 / 语音指令", "常用", "保留"),
    "inputDeviceId": ("业务配置 / 语音指令", "常用", "保留（收音设备）"),
    "meetingInputDeviceId": ("业务配置 / 会议", "常用", "保留（会议麦克风）"),
    "meetingSegmentMinutes": ("业务配置 / 会议", "常用", "保留（分段时长）"),
    "outputDeviceIds": ("能力与智能体 / 设备选择", "常用", "保留（播放设备池与优先级）"),
    "commandOutputDeviceId": ("能力与智能体 / 设备选择", "常用", "保留（同上）"),
    "meetingOutputDeviceId": ("能力与智能体 / 设备选择", "常用", "保留（同上）"),
    "triggerKeys": ("业务配置 / 语音指令", "高级", "保留（**仍是逗号文本框，语义待改**）"),
    # 同一功能被劈两处：实现方指出，采纳 → 三项都归「业务配置 / 朗读与反馈」
    "minimalReplyHint": ("业务配置 / 朗读与反馈", "高级", "保留（与 minimalReply/minimalReplyChars 同卡，别再劈开）"),
    "minimalReply": ("业务配置 / 朗读与反馈", "高级", "保留（同上）"),
    "minimalReplyChars": ("业务配置 / 朗读与反馈", "高级", "保留（同上）"),
    # 2026-09-26（概念纠正）：会议转写 = 转写 + 分离 + 声纹，三件标配 ——
    # "要不要分离"这个二选一被拆掉了（键连同 meetingDiarize 一起废弃，见 DEPRECATED_NOTES）。
    "voiceprintAutoEnroll": ("业务配置 / 语音指令", "高级",
                             "保留（**改名即入库**：唯一与声纹有关的开关，默认关）"),
    "voiceprintThreshold": ("业务配置 / 语音指令", "高级", "保留（认错人时唯一能调的东西）"),
    "voiceprintMargin": ("业务配置 / 语音指令", "高级", "保留（同上）"),
    # 服务/运维
    "serverPort": ("ECHO 通用 / 运行状态", "高级", "保留（面板端口）"),
    "agentBackend": ("能力与智能体 / 智能体选择", "常用", "保留（选哪个智能体）"),
    "harnessHome": ("能力与智能体 / 智能体选择", "常用", "保留（家目录）"),
    "harnessCommand": ("能力与智能体 / 智能体选择", "高级", "保留（启动命令覆盖）"),
    # 2026-09-26 新增：清理的阈值（天）。只是**建议**的阈值，不影响任何加载路径。
    "modelCleanupDays": ("能力与智能体 / 清理", "常用",
                         "**新增**（「近期」是多少天：默认 90；0 = 不按时间过滤）"),
    "modelsDir": ("能力与智能体 / 本地能力", "常用", "保留（模型权重放哪）"),
    # 有键、无下发通道（实现方点名）——明确写出来，别看成"界面忘了放"
    "allowVirtualInputDevice": ("业务配置 / 语音指令", "高级", "保留（**当前无下发通道**：voice 组 hidden 且不在 /api/capability 的 settings 里；要可编辑得后端加）"),
    "capabilityEchoServerToken": ("能力与智能体 / 已配对后端", "高级", "保留（**当前无下发通道**：ROUTING_KEYS 刻意排除，怕与配对令牌混淆）"),
    "capabilityEchoServerStaticToken": ("能力与智能体 / 已配对后端", "高级", "保留（同上）"),
    "capabilityEchoServerUrl": ("能力与智能体 / 已配对后端", "高级", "保留（直连后端的地址；配对优先）"),
    # 已过时（代码里已有弃用迁移，老值会折进新项）
    "worklogMode": ("—", "—", "**已过时**：与 worklogEnabled 重复，代码里有弃用迁移"),
    "providerTts": ("—", "—", "**已过时**：与 ttsEngine 重复，代码里有弃用迁移"),
}

#: 已过时项的**替代者**：`deprecated=True` 的项一律按"已过时"计数，但"被谁取代"
#: 必须写清楚（规矩：不能莫名取消）。这里写的是那句话本身，SPECIAL 里写的处置会被覆盖。
DEPRECATED_NOTES = {
    "meetingDiarize": ("**已过时**：**会议转写一律包含说话人分离**（转写 + 分离 + 声纹三件标配，"
                       "本地或走 ECHO 后端都一样）——「要不要分离」不再是用户设置。"
                       "**分离由谁做**见 `capabilityDiarizeBackend`（「业务配置 → 会议 · 转写走哪条路」）；"
                       "老库里的值只作兼容读取，改了没有任何效果"),
    "voiceprintEnabled": ("**已过时**：**声纹识别（认人）是标配**，没有开关 —— 只要库里已经有这个人，"
                          "会议就显示姓名（库空时静默无结果、零副作用）。"
                          "「要不要把声音**存进**库」仍由用户决定，见 `voiceprintAutoEnroll`"
                          "与会议详情里的「声纹入库」按钮"),
}

#: grp -> (归属, 常用/高级) 兜底规则（分组名取自真实导出的 12 个）。
#: 2026-09-26：归属名换成新 IA 的页签；**特判（SPECIAL）优先**，所以像 `model` 这种
#: 一族里跨页签的（引擎在业务配置、设备与清理在能力与智能体）由 SPECIAL 单独点名。
GRP_RULES = {
    "panel": ("ECHO 通用 / 快捷键与打开方式", "常用"),
    "paths": ("业务配置 / 工作区与归档", "常用"),
    "dsh": ("能力与智能体 / 智能体选择", "高级"),
    "agent": ("能力与智能体 / 智能体选择", "高级"),
    "router": ("能力与智能体 / 模型路由 · 路由参数", "高级"),
    "wake": ("业务配置 / 语音指令", "高级"),
    "model": ("业务配置 / 语音指令", "高级"),
    "voice": ("业务配置 / 语音指令", "高级"),
    "meeting": ("业务配置 / 会议", "高级"),
    "provider": ("业务配置 / 语音指令", "高级"),
    "capability": ("业务配置 / 会议 · 转写走哪条路", "高级"),
    "worklog": ("业务配置 / 工作区与归档", "高级"),
}


def build_rows():
    rows = []
    for key, meta in cfg.DEFAULTS.items():
        grp = str(meta.get("grp") or "")
        opts = meta.get("options")
        dest, level, disp = SPECIAL.get(key, (None, None, None))
        if meta.get("deprecated"):
            dest, level = "—", "—"
            disp = DEPRECATED_NOTES.get(
                key, "**已过时**：`deprecated=True`，任何接口都不下发（点名的替代项见说明）")
        elif dest is None:
            dest, level = GRP_RULES.get(grp, ("待定", "待定"))
            disp = "保留（待确认归属）" if dest != "待定" else "**待定**"
        kind = meta.get("value_type") or ("bool" if isinstance(meta.get("value"), bool) else "str")
        rows.append(dict(key=key, label=str(meta.get("label") or ""), grp=grp,
                         hidden=bool(meta.get("hidden")), kind=kind,
                         opts=("" if not opts else "/".join(map(str, opts))[:60]),
                         dest=dest, level=level, disp=disp))
    return rows


def render(rows) -> str:
    """把行渲染成表（`--check` 与真写盘共用它 —— 两处各拼一份必然漂移）。"""
    by_grp = collections.OrderedDict()
    for r in rows:
        by_grp.setdefault(r["grp"] or "(无分组)", []).append(r)

    out = io.StringIO()
    out.write("# ECHO 设置项归属表（从 `app.config.DEFAULTS` 机械导出，%d 项）\n\n" % len(rows))
    out.write("> 本表由 `scripts/gen-settings-map.py` 生成，**不要手工编辑**（会与代码漂移）。\n")
    out.write("> 改归属请改生成器里的 `SPECIAL` / `GRP_RULES` / `DEPRECATED_NOTES`，然后重跑。\n\n")
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
    return out.getvalue(), cnt


def main(argv=None) -> int:
    argv = list(argv or [])
    rows = build_rows()
    text, cnt = render(rows)
    dst = os.path.join(ROOT, "docs", "设置项归属表.md")
    if "--check" in argv:
        # `--check`：**盘上那份与 DEFAULTS 是否一致**（退出码 2 = 过期）。
        # 为什么需要它：这张表是"设置项不能莫名消失"的唯一账本，而它靠人记得重跑；
        # 忘了重跑就没人发现新键没归属、或废弃项没标注 —— 那正是这条规矩要防的事。
        try:
            with open(dst, encoding="utf-8") as fh:
                old = fh.read()
        except OSError:
            print("还没生成过：%s" % os.path.relpath(dst, ROOT))
            return 3
        if old.replace("\r\n", "\n") != text:
            print("过期：%s 与 app.config.DEFAULTS 不一致，请重跑 "
                  "`python scripts/gen-settings-map.py`" % os.path.relpath(dst, ROOT))
            return 2
        print("一致：%s（%d 项）" % (os.path.relpath(dst, ROOT), len(rows)))
        return 0
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("写入 %s" % dst)
    print("总项数 %d；" % len(rows) + " · ".join("%s=%d" % (k, cnt[k]) for k in ("保留", "合并", "已过时", "待定") if cnt[k]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
