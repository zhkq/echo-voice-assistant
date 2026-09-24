# -*- coding: utf-8 -*-
"""config.py — ECHO 配置管理（存储于 SQLite settings 表）

所有配置以"键值 + 面板元数据"的形式入库，面板据此动态渲染表单：
  grp        分组（见下方 DEFAULTS 的分节注释：panel/command/paths/voice/beep/speech/
             wake/meeting/worklog/router + 卡片承载的 agent/provider/model）
  value_type str|int|float|bool|json|list  → 决定输入控件
  options    JSON 候选值列表 → 下拉/多选
  hidden     True = 由面板的自定义 UI 承载（智能体表格 / 能力 provider 卡片），
             不进通用表单；值仍可读写（接口单独取）
  deprecated True = 已弃用（重复项或失效项），面板不再展示，写入被拒收（见下）

首次启动调用 seed_defaults() 写入默认值（INSERT OR IGNORE，不覆盖已有配置）。
读取用 get()/all()，写入用 update()（批量）。

已弃用项（deprecated=True）：
  仍保留在 DEFAULTS 与库中，保证老配置可读、迁移可比对、get() 兼容；
  但 all() 会把它们从面板数据里剔除（设置窗口不再出现），update() 直接拒收
  （写它不会有任何效果，静默接受只会造出"改了没反应"的假象）。
  老值表达的用户意图由 DEPRECATION_MIGRATIONS 在启动时搬到仍生效的那一项上。
  确认某个已弃用项彻底无人引用后，才可从 DEFAULTS 删除。

设置项与分组的完整审计（谁定义/谁读/谁写）见 `scripts/audit-settings.py` 与
`docs/settings-audit.md`；接线回归测试见 `tests/test_settings_wiring.py`。
"""
import os

from app import paths
import app.db as db

# ---- 路径占位符 -----------------------------------------------------------
# 配置里存占位符而不是绝对路径，这样同一个默认值在任何机器/任何安装目录都能用，
# 也不会把个人路径写进仓库。读取时（Settings._load）统一展开。
#   {ECHO} = ECHO 根目录（安装根，可被 ECHO_ROOT 覆盖）
#   {DATA} = ECHO 的数据根（**分平台**，可被 ECHO_DATA 覆盖）
#
# 两个占位符都必须**问路径层**，不许在这里各推一遍：
#   * {ECHO} 自己推 → 设了 ECHO_ROOT 时，配置展开与路径层会指向不同的树（split-brain）；
#   * {DATA} 写成 join(ECHO_ROOT, "data") → macOS 上会把数据根算进 .app 里，
#     而 D18 要求 macOS 写 ~/Library/Application Support/ECHO。
# ECHO_ROOT 这个模块属性保留给老调用方，取值与 paths.echo_root() 一致。
ECHO_ROOT = paths.echo_root()


def expand_path(value):
    """展开配置值里的 {ECHO} / {DATA} 占位符；非字符串原样返回。

    展开后统一走 os.path.normpath：占位符写的是正斜杠（`{ECHO}/data/meetings`），
    直接拼接会得到 `C:\\...\\ECHO-public/data/meetings` 这种混合分隔符——Windows
    能用，但日志里难看，且与用户手填的反斜杠路径比较时还需要额外容错。

    取值是**调用时**问路径层（不是模块常量），所以 ECHO_ROOT / ECHO_DATA 这类
    环境变量在进程内改了也立刻生效——测试和多实例隔离依赖这一点。
    """
    if not isinstance(value, str) or "{" not in value:
        return value
    out = (value.replace("{ECHO}", paths.echo_root())
                .replace("{DATA}", paths.data_root()))
    return os.path.normpath(out)


#: 清空密钥用的哨兵值（`Settings.update()` 认它；面板「清除」按钮送它）。
#: 为什么需要哨兵：`secret=True` 的项出口一律被遮成空串，空串只能解释成"不改"，
#: 否则面板整批回传就会把密钥静默清掉（2026-09-19 发现的数据丢失风险）。
CLEAR_SECRET = "__clear__"


# ---- 平台声明的配置默认值 / 候选项（D11）------------------------------------# 为什么要有这两个函数：平台差异不只体现在"环境默认值"（paths.py 用的 dataDir），
# 也体现在**配置项本身**上——macOS 没有 CUDA（device 该默认 cpu）、离线朗读是 say
# 而不是 SAPI（ttsEngine 候选项不同）、精简依赖不含 funasr（sttModel 默认要落在 whisper）。
# 这些原先散在 `mac/run_mac.py` 的"注入式覆盖 DEFAULTS"里；现在**声明式**放在各平台
# `env.py` 的 PLATFORM_DEFAULTS，由本模块消费。
#
# 关键约定：**平台没声明 = 用 DEFAULTS 里的 Windows 基准值**。所以 Windows 行为零变化，
# 而别的平台只需在自己的 env.py 里加一行，不必改共享代码、也不必在入口处 monkeypatch。
def platform_default(key, fallback=None):
    """该平台声明的默认值；没声明返回 ``fallback``。"""
    try:
        from app import platform as echo_platform
        value = echo_platform.setting_default(key)
    except Exception:
        value = None
    return fallback if value is None else value


def platform_options(key, fallback=None):
    """该平台声明的候选项（面板下拉用）；没声明返回 ``fallback``。"""
    try:
        from app import platform as echo_platform
        opts = echo_platform.setting_options(key)
    except Exception:
        opts = None
    return fallback if opts is None else opts


def _effective_default(key, meta):
    """某个配置项在当前平台上的实际默认值。"""
    return platform_default(key, meta["value"])


def _effective_options(key, meta):
    """某个配置项在当前平台上的实际候选项。"""
    return platform_options(key, meta.get("options", []))


#: 设备下拉项的 TTL 缓存（30 秒）。
#: 设备会插拔，所以选项必须**运行时**取；但也不该每刷一次面板就查一次 PortAudio
#: （mac 上的音频查询还可能把 CoreAudio HAL 卡住，见 AGENTS.md 的头号坑）。
_INPUT_OPTIONS = {"at": 0.0, "items": []}
#: 扬声器候选项的缓存（与输入侧各一份：设备增减会同时影响两者，但查询是分开的）
_OUTPUT_OPTIONS = {"at": 0.0, "items": []}

#: "系统默认"这一项的值。空串 = 不指定（跟随系统）；老配置里的 -1 也当同一意思。
SYSTEM_DEFAULT_DEVICE = ""


def _audio_device_options(max_age=30.0):
    """设备候选项（不含"系统默认"）：``[{value: 设备名, label: 人看的}]``。

    **value 用设备名，不用索引** —— 同一个名字对同一台物理设备是稳的，而 PortAudio 的
    索引会随"当前在位设备"的增减整体平移（用户 2026-09-23 问的"序号漂移"）。
    同一个名字出现在多套 host API 时按 `rank_hostapi()` 去重（WASAPI 优先），
    标签里保留 API 与原生采样率，用户才知道自己选的是哪一条。
    """
    import time as _time
    now = _time.time()
    if _INPUT_OPTIONS["items"] and (now - _INPUT_OPTIONS["at"]) < max_age:
        return _INPUT_OPTIONS["items"]
    try:
        from app.audio import recorder
        items = _dedupe_devices(recorder.list_input_devices(),
                                _input_extra_label, recorder.rank_hostapi)
    except Exception:
        return []
    _INPUT_OPTIONS["at"] = now
    _INPUT_OPTIONS["items"] = items
    return items


def _audio_input_options(current=None):
    """面板下拉的完整候选项：系统默认 + 各设备 +（必要时）**当前值本身**。

    `current` 是库里现在的值，它可能是**老配置的索引**（14/29）或一个已经拔掉的设备名 ——
    这两种都不在候选项里。**必须把它显式列出来**：否则面板会把它显示成"系统默认"，
    而用户一保存就把真值覆盖成空（静默丢配置）。
    """
    return _with_current(_audio_device_options(), current)


def _audio_output_options(current=None):
    """扬声器下拉：形状与输入侧完全一致（面板可以复用同一套渲染）。

    **`outputDeviceIds`（优先级池）不走这里** —— 它是个多值列表，
    面板该渲染成"可排序的多选"，不是下拉。
    """
    return _with_current(_audio_output_device_options(), current)


def _with_current(items, current):
    out = [{"value": SYSTEM_DEFAULT_DEVICE, "label": "系统默认（跟随系统）"}]
    out += list(items)
    cur = str(current if current is not None else "").strip()
    if cur and cur != "-1" and not any(str(o["value"]) == cur for o in out):
        out.insert(1, {"value": cur,
                       "label": "%s  ← 当前值（设备不在位，或是旧的索引号，建议重选）" % cur})
    return out


def _dedupe_devices(devices, extra_label, rank_of):
    """设备清单 → 下拉项：**同名去重**（按 host API 排名）并生成人看的标签。

    输入与输出**共用这一段**：去重规则、标签格式、"同一设备在多套 API 里出现"的处理
    完全一样，只有"推荐什么"不同（输入推荐原生 16 kHz；输出没有这条）——
    那部分由 `extra_label` 回调给。
    """
    best = {}
    for d in devices:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        rank = rank_of(d.get("hostapi"))
        if name in best and best[name][0] <= rank:
            continue
        api = str(d.get("hostapi") or "").replace("Windows ", "")
        sr = d.get("samplerate") or 0
        tag = " · ".join(x for x in (api, ("%g kHz" % (sr / 1000.0)) if sr else "") if x)
        label = name + (("  [%s]" % tag) if tag else "")
        suffix = extra_label(d)
        if suffix:
            label += "  " + suffix
        best[name] = (rank, {"value": name, "label": label})
    return [v[1] for _n, v in sorted(best.items(), key=lambda kv: (kv[1][0], kv[0]))]


def _input_extra_label(d):
    if d.get("virtual"):
        return "← 映射/虚拟设备，别选"
    if (d.get("samplerate") or 0) == 16000:
        return "← 原生 16 kHz，推荐"
    return ""


def _output_extra_label(d):
    # 输出侧**没有**"原生 16 kHz 推荐"这一条：播报走 44.1/48 kHz 才是正常的，
    # 拿输入的推荐语去标扬声器会把用户指错方向。
    if d.get("virtual"):
        return "← 映射/虚拟设备，别选"
    return ""


def _audio_output_device_options(max_age=30.0):
    """扬声器候选项（不含"系统默认"）：`[{value: 设备名, label: 人看的}]`。

    value 同样用**设备名**当稳定键（索引会随在位设备增减平移）。
    """
    import time as _time
    now = _time.time()
    if _OUTPUT_OPTIONS["items"] and (now - _OUTPUT_OPTIONS["at"]) < max_age:
        return _OUTPUT_OPTIONS["items"]
    try:
        from app.audio import output, recorder
        items = _dedupe_devices(output.list_output_devices(),
                                _output_extra_label, recorder.rank_hostapi)
    except Exception:
        return []
    _OUTPUT_OPTIONS["at"] = now
    _OUTPUT_OPTIONS["items"] = items
    return items


# ---- 极简回复要求文案（两个版本都保留：V1 是已落库的旧默认值，用于迁移比对）----
# V1：只回极简结论、详情留在会话里。
_MINIMAL_REPLY_HINT_V1 = (
    "【回复要求】最终回复只给极简结论：{chars} 字以内，一句话说清结果"
    "（做了什么/结论是什么）；不要罗列过程、步骤、命令或代码，"
    "也不要复述我的问题。详细内容留在本次会话里，我会自己回会话查看。")
# V2（2026-09-12 用户改版）：先给极简结论，再换行给详情。语音只读结论段。
_MINIMAL_REPLY_HINT_V2 = (
    "【回复要求】最终回复先给我一个极简结论：{chars} 字以内，一句话说清结果"
    "（做了什么/结论是什么）；不要罗列过程、步骤、命令或代码，也不要复述我的问题。"
    "最后换行写「详情如下：」，再把详细情况详细表达。")

# ---- 纪要归档提示词模板（送 DSH，由用户自己的归档技能执行）----
# 设计要点：
#   * 只给"材料在哪"，不塞纪要全文——技能自己读 {md_path}（避免长文截断丢信息）；
#   * {archive_hint} 保留为自由文本，个性化归档规则由技能解释；
#   * 把 {meeting_id} 当幂等键传下去，技能据此去重，避免重复登记；
#   * 要求技能回一句话人话结果，ECHO 不再解析结构化输出。
_WORKLOG_PROMPT_V1 = (
    "【纪要归档任务】\n"
    "笔记库根目录：{vault}\n"
    "会议标题：{title}\n"
    "会议时间：{started_at}（{date} 第 {hour} 时，约 {duration} 秒）\n"
    "参会人：{speakers}\n"
    "纪要文件：{md_path}\n"
    "幂等键：{meeting_id}\n"
    "归档要求：{archive_hint}\n"
    "\n"
    "请调用你的归档技能完成归档：判断归属位置 → 写入会议纪要 → 更新相关工作日志/项目日志。\n"
    "要求：\n"
    "1) 归档要求为空时，按你自己技能的默认规则判断，不要臆造目录；\n"
    "2) 幂等：若该幂等键已登记过，跳过重复写入；\n"
    "3) 纪要全文读 {md_path}，不要凭标题推测内容；\n"
    "4) 完成后只回复一句话说明归档结果（写到哪个文件）。")


#: 由「能力 provider」卡片**附带展示**的非 provider 组配置项。
# 为什么要有这个名单：`ttsEngine` 与已弃用的 `providerTts` 是同一个选择（朗读用哪个），
# 卡片需要把它取来显示"当前实现 / 是否出网 / 就绪"，但**不**在卡片里提供第二个编辑入口
# （两处设置互相打架正是 2026-09-19 那次收口要解决的问题）→ 接口给 `read_only=True`，
# 面板据此渲染成只读行 + 指路文案。
CARD_CONFIG_KEYS = ("ttsEngine",)


# key -> dict(value, grp, label, description, value_type, options, deprecated?)
# deprecated=True 的项不进入设置窗口（all() 会过滤），仅保留兼容读取。
DEFAULTS = {
    # ---------- 面板 / 服务（ECHO 自己的运行参数：端口、鉴权、界面行为）----------
    # 注：`dshBaseUrl` 不在这里 —— 它是"DSH 这个智能体"的参数，归 <agent> 组并标 hidden
    # （见文件末尾 agent 分组那几行），由智能体表格的展开区编辑，
    # 值经 GET /api/agents 的 settings 字段下发。
    # 2026-09-19 用户实测："下面的 dsh 没必要吧，或者把端口挪上去"。
    "serverPort":      dict(value=8970, grp="panel", label="ECHO 面板端口",
                            description="控制面板与 API 的监听端口（8890 曾被系统保留段占用，改用 8970）", value_type="int"),
    # ---------- 语音命令 → 命令与会话（二级子分组，见 grp/sub 的说明）----------
    "commandWorkspace": dict(value="{ECHO}/data/command", grp="voice", sub="command",
                             label="命令会话工作区",
                             description="默认命令会话建在这个目录对应的 DSH 工作区里，"
                                         "从而归入侧栏的「指令空间」分组。"
                                         "想并到自己的工作区（例如「日常交互」）就改成那个目录。"
                                         "留空 = 建在 ECHO 根目录（侧栏显示为未分组）",
                             value_type="str"),
    "commandWorkspaceTitle": dict(value="指令空间", grp="voice", sub="command",
                                  label="指令分组名",
                                  description="上面那个工作区在 DSH 侧栏里显示的分组名。"
                                              "只对 ECHO 自己创建的工作区生效，"
                                              "你手动改过名字的一律以你的为准",
                                  value_type="str"),
    "commandIdleRotateHours": dict(value=4, grp="voice", sub="command", label="命令会话空闲轮换小时",
                                   description="默认命令会话空闲超过 N 小时且新指令未要求延续上一话题时，"
                                               "自动轮换新会话（0=关闭；会话不在默认工作区时会强制轮换一次）",
                                   value_type="float"),
    "commandTargetWorkspace": dict(value="", grp="voice", sub="command", label="命令目标工作区（面板同步）",
                                   description="面板仪表盘「命令目标」下拉里选中的工作区。选中后，"
                                               "语音命令（媒体键/热键/唤醒/麦克风按钮）与打字命令都发到这里，"
                                               "不再使用上面那个会轮换的默认命令会话。"
                                               "由面板下拉自动写入，一般不用手改；留空 = 回到默认会话",
                                   value_type="str"),
    "commandTargetSession": dict(value="", grp="voice", sub="command", label="命令目标会话（面板同步）",
                                 description="面板选中的具体对话：填了就直接发给它（不轮换、也不自动挑最近会话）。"
                                             "会话被归档/删除后自动回退到「该工作区自动」。"
                                             "由面板下拉自动写入，一般不用手改",
                                 value_type="str"),
    # ---------- 存储路径（2.0 / D20、D21）----------
    # 留空 = 用默认值；默认值由 app/paths.py 解析（分平台，见 app/platform/<os>/env.py）。
    # 这里存的是"用户指定值"而不是解析后的绝对路径——占位符让同一份配置在任何机器都能用。
    "meetingsDir":     dict(value="", grp="paths", label="会议文件目录",
                            description="会议录音与纪要**文件**的存放目录。留空 = {DATA}/meetings。"
                                        "支持 {ECHO}/{DATA} 占位符与 ~。"
                                        "改后旧会议仍留在原目录，需要用面板里的「迁移已有会议」搬过来。"
                                        "（与「会议 → 会议会话工作区」不同：那一个是 DSH 会话登记到哪个工作区）",
                            value_type="str"),
    "modelsDir":       dict(value="", grp="paths", label="模型目录",
                            description="模型权重（whisper / SenseVoice / 唤醒词 / pyannote…）的存放目录。"
                                        "留空 = {ECHO}/models。支持 {ECHO}/{DATA} 占位符与 ~。"
                                        "改后需重新下载模型，或自行把原目录拷过去",
                            value_type="str"),
    "userLocation":    dict(value="北京", grp="voice", sub="command", label="用户所在地",
                            description="发给 DSH 命令时附带的地理位置（天气/时间等问答需要）",
                            value_type="str"),
    "sendEnvContext":  dict(value=True, grp="voice", sub="command", label="发送环境上下文",
                            description="命令前附加当前时间与所在地，让 DSH 对'今天/明天/本地'有概念",
                            value_type="bool"),
    # device / sttModel / wakeEngine / meetingSttModel / meetingDiarize / voiceprint*
    # 都归 grp="model"：它们正常由面板顶部「模型」页签承载（带就绪状态与获取入口），
    # 这里的元数据是给"页签加载失败时回退显示"用的（见 web/app.js 的 MODEL_KEYS）。
    "device":          dict(value="auto", grp="model", label="计算设备",
                            description="auto=cuda 优先，失败回退 CPU",
                            value_type="str", options=["auto", "cpu", "cuda"]),
    "sttModel":        dict(value="sensevoice", grp="model", label="命令转写引擎",
                            description="sensevoice 最快（中文短命令），qwen3asr 更准（需下载模型），sherpa 流式，whisper 模型按名",
                            value_type="str",
                            options=["sensevoice", "qwen3asr", "sherpa", "tiny", "base", "small", "medium", "large"]),
    # ---------- 语音命令 → 录音与转写（二级子分组）----------
    "sttLanguage":     dict(value="zh", grp="voice", sub="record", label="转写语言",
                            description="命令与会议共用的转写语言：zh/en/ja/ko/yue，或 auto 自动识别。"
                                        "填全名（如 Chinese）会自动纠正；非法值回退 zh（Whisper 只认 ISO 码，"
                                        "填错会让转写结果变空）",
                            value_type="str", options=["zh", "en", "ja", "ko", "yue", "auto"]),
    "triggerKeys":     dict(value=["vol_up"], grp="voice", sub="record", label="媒体键触发",
                            description="耳机/键盘媒体键作为说话快捷键（vol_up/play_pause/next/prev）",
                            value_type="list", options=["vol_up", "vol_down", "play_pause", "next", "prev"]),
    "wakeHotkey":      dict(value="Ctrl+Alt+C", grp="voice", sub="record", label="唤醒热键",
                            description="全局热键开始录音说话", value_type="str"),
    "fallbackHotkey":  dict(value="Ctrl+Alt+V", grp="voice", sub="record", label="回退热键",
                            description="媒体键失效时使用的备用热键", value_type="str"),
    # 面板热键由 ECHO 服务自己 RegisterHotKey 注册（纯 ctypes），**不依赖 DSH 插件**：
    # 2026-09-12 实测 DSH Desktop 2.0.9 里插件（ESM 动态 import）取不到 electron 的
    # app/BrowserWindow/screen（只有 net/systemPreferences），插件侧的 globalShortcut
    # 不可用，因此把"打开仪表盘"的热键落在 ECHO 进程里。
    "panelHotkey":     dict(value="Ctrl+Shift+E", grp="panel", label="仪表盘热键",
                            description="全局热键切换 ECHO 仪表盘（由 ECHO 服务进程注册）", value_type="str"),
    "panelOpenMode":   dict(value="sidebar", grp="panel", label="仪表盘打开方式",
                            description="sidebar=右缘边条（无边框/置顶/铺满高度，与升级前一致）；app=Chromium 应用窗口；browser=默认浏览器",
                            value_type="str", options=["sidebar", "app", "browser"]),
    "panelAutoStart":  dict(value=True, grp="panel", label="启动时自动显示折叠条",
                            description="ECHO 启动后自动在屏幕右缘显示折叠条（仅当「仪表盘打开方式」= sidebar 时生效）；"
                                        "已在运行则不打扰（不会把已展开的面板收起来）",
                            value_type="bool"),
    "panelStartCollapsed": dict(value=True, grp="panel", label="自动显示时收起为折叠条",
                                description="True=启动后显示 64px 折叠条（点箭头/热键展开）；False=直接展开面板",
                                value_type="bool"),
    "silenceThreshold": dict(value=0.012, grp="voice", sub="record", label="静音阈值",
                             description="音量低于此值视为静音（0~1）", value_type="float"),
    "silenceHangoverMs": dict(value=1100, grp="voice", sub="record", label="静音收尾毫秒",
                              description="静音持续多久自动停录", value_type="int"),
    "noSpeechAbortMs": dict(value=4000, grp="voice", sub="record", label="无语音放弃毫秒",
                            description="开口后多长时间没声音就放弃", value_type="int"),
    "maxRecordMs":     dict(value=30000, grp="voice", sub="record", label="最长录音毫秒",
                            description="单次命令录音上限", value_type="int"),
    "inputDeviceId":   dict(value="", grp="voice", sub="record", label="默认输入设备",
                            description="指令、唤醒、会议都用它；下面两项可以各自覆盖。"
                                        "「系统默认」= 跟随 Windows 的默认麦克风。"
                                        "选项是**设备名**（不是会漂的序号）；"
                                        "配置的设备不在位时会回退到系统默认，并记一条日志。",
                            value_type="str", options_from="audio_inputs"),
    # 2026-09-23 用户需求："指令用耳机收音、会议用全向麦（MAXHUB）" —— 之前只有一个
    # inputDeviceId，会议与指令只能共用一个麦。**通用项仍是兜底**（空 = 跟随它），
    # 所以老配置不需要任何数据迁移，行为一个字都不变。
    "commandInputDeviceId": dict(value="", grp="voice", sub="record",
                                 label="指令/唤醒输入设备",
                                 description="语音指令（媒体键/热键/唤醒/麦克风按钮）从哪个设备收音。"
                                             "想让耳机上的媒体键触发、并用耳机麦说话，就在这里选耳机。"
                                             "空 = 跟随上面的默认输入设备",
                                 value_type="str", options_from="audio_inputs"),
    "meetingInputDeviceId": dict(value="", grp="voice", sub="record",
                                 label="会议录音输入设备",
                                 description="会议从哪个设备录音。会议室里选全向麦（如 MAXHUB）"
                                             "比笔记本内置麦好得多。"
                                             "空 = 跟随上面的默认输入设备",
                                 value_type="str", options_from="audio_inputs"),
    # ---------- 播放设备（扬声器）也进设备池 ----------
    # 采集那侧早就有"按用途挑设备"，播放一直是"系统默认发声"，于是会出现
    # "麦选了耳机（不想把全场录进来），播报却从会议室音箱出去（把'已发送'念给全场听）"。
    # 两个方向的需求本来就是对称的，所以扬声器也照同一套形状管：
    # **有序候选（优先级池）+ 在位判定 + 回退不静默**。
    # 实现与理由见 `app/audio/output.py`。
    "outputDeviceIds": dict(value=[], grp="voice", sub="speech", label="扬声器优先级",
                            description="按**优先级**排列的播放设备，逗号分隔；"
                                        "每次播报取**第一个在位的**（拔了/没连上会自动跳过）。"
                                        "空 = 用系统默认扬声器。"
                                        "例：`Bose Speaker, 扬声器 (Realtek)`",
                            value_type="list", options_from="audio_outputs"),
    "commandOutputDeviceId": dict(value="", grp="voice", sub="speech",
                                  label="指令播报扬声器",
                                  description="语音复述确认、提示音从哪个设备出声。"
                                              "想只让自己听见（不打扰别人）就在这儿选耳机。"
                                              "空 = 按上面的优先级池挑",
                                  value_type="str", options_from="audio_outputs"),
    "meetingOutputDeviceId": dict(value="", grp="voice", sub="speech",
                                  label="会议播报扬声器",
                                  description="会议相关播报从哪个设备出声。"
                                              "想全场都听见就选会议室音箱（如 MAXHUB）。"
                                              "空 = 按上面的优先级池挑",
                                  value_type="str", options_from="audio_outputs"),
    # 2026-09-23 D2：虚拟/接力设备黑名单的**逃生口**。
    # 黑名单（见 recorder._VIRTUAL_HINTS）原来只挡"兜底遍历"，用户显式选的照开；
    # 但 AGENTS.md 里那次事故（macOS 打开 Oray/iPhone 麦克风把 CoreAudio HAL 锁死，
    # 之后**任何**麦克风操作永久超时、只能重启 ECHO）走的正是"显式指定"这条路。
    # 现在显式指定也拦，被拦时日志会说清怎么放行 —— 就是下面这一项。
    # 做成隐藏项：它是个"我确认要冒这个险"的开关，不该摆在常规设置里让人随手打开。
    "allowVirtualInputDevice": dict(value=False, grp="voice", sub="record",
                                    label="允许使用虚拟/接力输入设备",
                                    description="虚拟、映射、回环、汇总、接力类设备"
                                                "（Oray、iPhone 麦克风、立体声混音、BlackHole…）"
                                                "不是真麦克风。打开它们可能把系统音频服务卡死"
                                                "（macOS 上实测过一次，之后任何麦克风操作都失效、"
                                                "只能重启 ECHO），所以默认拒绝——"
                                                "包括你手动在上面两项里选中它们时。"
                                                "确实需要（如回环测试）才打开这一项",
                                    value_type="bool", hidden=True),
    "consumeMediaKey": dict(value=True, grp="voice", sub="record", label="拦截媒体键",
                            description="触发后不向系统透传媒体键", value_type="bool"),
    # ---------- 语音命令 → 提示音与通知（二级子分组）----------
    "beepOnStart":     dict(value=True, grp="voice", sub="beep", label="开始提示音", description="开始录音时播放提示音",
                            value_type="bool"),
    "beepOnDone":      dict(value=True, grp="voice", sub="beep", label="停录提示音", description="停止录音时播放提示音",
                            value_type="bool"),
    "beepOnSend":      dict(value=True, grp="voice", sub="beep", label="发送提示音", description="命令发送成功提示音",
                            value_type="bool"),
    "notifyOnSend":    dict(value=True, grp="voice", sub="beep", label="桌面通知",
                            description="发送成功后弹系统通知", value_type="bool"),
    # ---------- 语音命令 → 朗读与反馈（语音合成 + 播报哪些内容）----------
    # ttsEngine 是朗读"用哪个实现"的**唯一**开关：auto / edge-tts（微软在线）/ 本平台离线引擎 / off。
    # 历史上有第二个开关 providerTts（能力 provider 卡片上的 TTS 下拉），两者重复且会互相打架
    # （配了 providerTts 时 ttsEngine=off 关不掉朗读）→ 2026-09-19 弃用 providerTts。
    "ttsEngine":       dict(value="auto", grp="voice", sub="speech", label="语音合成引擎",
                            description="朗读与提示语用哪个实现，四选一："
                                        "auto=优先 edge-tts（★微软在线，文本会发往 speech.platform.bing.com），"
                                        "不可用时降级本机离线合成；edge-tts=只走在线；"
                                        "离线引擎（Windows SAPI / macOS say）=全离线、音色略差；off=关闭朗读",
                            value_type="str", options=["auto", "edge-tts", "sapi", "off"]),
    "voiceConfirm":    dict(value=True, grp="voice", sub="speech", label="语音复述确认",
                            description="发送前朗读一遍识别到的命令", value_type="bool"),
    "voiceBrief":      dict(value=True, grp="voice", sub="speech", label="语音简报",
                            description="任务完成后朗读精简结果", value_type="bool"),
    "maxBriefChars":   dict(value=200, grp="voice", sub="speech", label="简报最大字数",
                            description="语音简报文本长度上限", value_type="int"),
    # ---------- 极简回复（2026-09-12 用户要求）----------
    # 命令末尾附一段"先给极简结论、再换行给详情"的要求：
    # 语音只朗读结论那一段（assistant.conclusion_only），详情留在回复/会话里给人看。
    "minimalReply":    dict(value=True, grp="voice", sub="speech", label="要求极简回复",
                            description="在命令末尾附一句要求：先给极简结论，再换行写详情（语音只读结论）",
                            value_type="bool"),
    "minimalReplyChars": dict(value=60, grp="voice", sub="speech", label="极简回复字数上限",
                              description="写进要求的长度约束（口语一句话约 30~60 字）", value_type="int"),
    "minimalReplyHint": dict(value=_MINIMAL_REPLY_HINT_V2,
                             grp="voice", sub="speech", label="极简回复要求文案",
                             description="拼在命令末尾；{chars} 会替换成上面的字数上限",
                             value_type="str"),
    # ---------- 唤醒词 ----------
    "wakeEnabled":     dict(value=False, grp="wake", label="启用语音唤醒",
                            description="说唤醒词免按键唤起（唤醒词见下方配置）", value_type="bool"),
    "wakePaused":      dict(value=True, grp="wake", label="唤醒暂停（勿扰）",
                            description="来电/会议期间临时关闭唤醒", value_type="bool"),
    "wakeEngine":      dict(value="sherpa", grp="model", label="唤醒方式",
                            description="sherpa=流式识别出文字再匹配唤醒词（推荐，模型缺失时自动回退 KWS）；"
                                        "kws=关键词 spotting（只认唤醒词本身，更省 CPU）",
                            value_type="str", options=["sherpa", "kws"]),
    "wakeKeywords":    dict(value=["回声回声"], grp="wake", label="唤醒词",
                            description="支持多个，逗号分隔", value_type="list"),
    "wakeAliases":     dict(value=[], grp="wake", label="唤醒词别名（误听容错）",
                            description="流式识别常见误听写法，逗号分隔（如「对你有」是「嘿尼欧」的识别偏差，说唤醒词时会命中）", value_type="list"),
    "wakeThreshold":   dict(value=0.5, grp="wake", label="唤醒灵敏度阈值",
                            description="越高越不易误触发", value_type="float"),
    "wakeCooldownSec": dict(value=3, grp="wake", label="触发冷却秒",
                            description="两次唤醒的最小间隔", value_type="float"),
    "wakeConfirmX":    dict(value=2, grp="wake", label="确认帧数",
                            description="近 N 帧中至少 X 帧命中才触发（防噪声尖峰）", value_type="int"),
    "wakeConfirmN":    dict(value=3, grp="wake", label="确认窗口", description="见上", value_type="int"),
    "wakeSilenceFloor": dict(value=60, grp="wake", label="静音门控",
                             description="低于此音量不送入唤醒模型（省电防误触发）", value_type="int"),
    # ---------- 会议 ----------
    "meetingSttModel":  dict(value="sensevoice", grp="model", label="会议转写引擎",
                             description="sensevoice 最快（中文会议推荐）；small/medium/large 是 whisper；qwen3asr 最准但慢约 20 倍（GPU rtf≈0.45）",
                             value_type="str",
                             options=["sensevoice", "qwen3asr", "small", "medium", "large"]),
    "meetingSegmentMinutes": dict(value=10, grp="meeting", label="分段分钟",
                                  description="录音每 N 分钟存一个文件", value_type="int"),
    "meetingAutoSummarize": dict(value=True, grp="meeting", label="自动生成纪要",
                                 description="转写完成后自动请 DSH 生成纪要（★转写全文会发给 DSH 配置的模型服务：内网网关即贵单位内网，公网 API 即模型厂商）", value_type="bool"),
    "meetingKeepRawAudio": dict(value=True, grp="meeting", label="保留原始音频",
                                description="删除会议时是否同时删除音频", value_type="bool"),
    "meetingDiarize":   dict(value=False, grp="model", label="区分说话人",
                             description="本地 pyannote 分离（CPU 下较慢）", value_type="bool"),
    # ---------- 常用联系人声纹（issue #6）：改名入库 → 新会议自动认人 ----------
    # 样本是说话人嵌入（256 维），只存本机 data/echo.db；不做云端、不出网。
    # 【默认关闭】声纹属于生物特征数据：收集与自动认人都必须由用户显式开启（opt-in）。
    # 注意：不要用 DEFAULT_MIGRATIONS 做 True→False 的翻转 —— 那套机制每次启动都会比对
    # 「旧默认值」，用户一旦主动开启就会被下一次启动翻回去；改默认值 + 让用户自己开即可。
    "voiceprintEnabled": dict(value=False, grp="model", label="声纹识别常用联系人",
                              description="默认关闭。开启后会议转写会用声纹库自动识别已入库的联系人，"
                                          "把「说话人N」直接标成联系人名（需先开启「区分说话人」，"
                                          "且联系人有已入库的声纹样本）；关闭时不留存任何声纹样本",
                              value_type="bool"),
    "voiceprintAutoEnroll": dict(value=False, grp="model", label="改名时自动入库声纹",
                                 description="默认关闭。开启后在会议里把说话人改名为联系人时，"
                                             "自动把该说话人本场的声音存成声纹样本（声纹库属生物特征数据，"
                                             "样本可在会议页「说话人管理」里查看/删除）",
                                 value_type="bool"),
    "voiceprintThreshold": dict(value=0.65, grp="model", label="声纹匹配阈值",
                                description="余弦相似度下限（0~1）：越高越不容易认错人、也越容易漏认。"
                                            "默认 0.65 偏保守；先看日志（source=voiceprint）里的实际相似度再调",
                                value_type="float"),
    "voiceprintMargin": dict(value=0.05, grp="model", label="声纹歧义间隔",
                             description="候选联系人与次优的最小差距：差距过小视为认不准，不自动命名",
                             value_type="float"),
    "meetingWorkspace": dict(value="{ECHO}/data/meetings", grp="meeting",
                             label="会议会话工作区",
                             description="一场会议一个 DSH 会话（纪要/分段/归档共用），下一场新建；"
                                         "这些会话都会登记进这个目录对应的 DSH 工作区，"
                                         "从而归入侧栏的「会议工作区」分组。"
                                         "注意与「存储路径 → 会议目录」的区别：那一个是录音/纪要"
                                         "**文件**放哪，这一项是 DSH **会话**登记到哪个工作区。"
                                         "{ECHO} = ECHO 根目录；留空 = 用固定的纪要会话（不分组）",
                             value_type="str"),
    "meetingWorkspaceTitle": dict(value="会议空间", grp="meeting",
                                  label="会议分组名",
                                  description="上面那个工作区在 DSH 侧栏里显示的分组名。"
                                              "只对 ECHO 自己创建的工作区生效，"
                                              "你手动改过名字的一律以你的为准",
                                  value_type="str"),
    # ---------- 纪要归档（工作日志 / 笔记库）----------
    # 设计：ECHO 只负责"把材料备齐 + 定位笔记库"，至于写到哪个目录、日志长什么样、
    # 有哪些专项与例会，全部由用户自己的归档技能（skill）决定。因此这里只有 4 项，
    # 且默认值全为空——仓库里不携带任何个人笔记库路径、专项清单或单位会议体系。
    "worklogEnabled": dict(value=False, grp="worklog", label="启用纪要归档",
                           description="关闭时「写工作日志」按钮不可用，仅在本地保留纪要文件",
                           value_type="bool"),
    "worklogEnsureSessionAccess": dict(value=True, grp="worklog", label="归档前校正会话权限",
                                       description="归档前检查并校正 DSH 新会话默认权限为全盘访问"
                                                   "（笔记库在会话工作区之外，权限不足会被沙箱拦下、"
                                                   "只能靠自动提权重试，很慢）；关闭后只告警不修改",
                                       value_type="bool"),
    "worklogVaultRoot": dict(value="", grp="worklog", label="笔记库根目录",
                             description="纪要归档的目标根目录（如 Obsidian 库路径）。同时作为归档 DSH 会话的工作区",
                             value_type="str"),
    "worklogMode": dict(value="skill", grp="worklog", label="归档方式",
                        description="已弃用：与「启用纪要归档」是同一个开关的两个入口（重复项），"
                                    "2026-09-19 起只保留总开关 worklogEnabled",
                        value_type="str", options=["skill", "off"], deprecated=True),
    "worklogPrompt": dict(value=_WORKLOG_PROMPT_V1, grp="worklog", label="归档提示词模板",
                          description="送入 DSH 的归档命令模板；占位符：{vault} {title} {started_at} "
                                      "{duration} {speakers} {date} {hour} {md_path} {archive_hint} {meeting_id}",
                          value_type="str"),
    # ---------- DSH ----------
    # 以下三项是 DSH Desktop 1.x 时代"由 ECHO 拉起 DSH 进程"的产物。2.x 由桌面客户端
    # 自己托管服务，ECHO 只按 dshBaseUrl 连过去，这三项已无实际作用 → 标记弃用，
    # 不再占用设置窗口（值仍留在库里，避免误删历史配置）。
    "dshStartCommand": dict(value="web", grp="dsh", label="DSH 启动子命令",
                            description="已弃用：DSH Desktop 2.x 由桌面客户端托管，不再由 ECHO 拉起",
                            value_type="str", deprecated=True),
    "dshNodePath":     dict(value="", grp="dsh", label="Node 可执行文件路径",
                            description="已弃用：不再由 ECHO 拉起 DSH 进程",
                            value_type="str", deprecated=True),
    "dshPackageDir":   dict(value="", grp="dsh", label="DSH 包目录",
                            description="已弃用：不再由 ECHO 拉起 DSH 进程",
                            value_type="str", deprecated=True),
    # ---------- 智能体（ECHO 对接哪个产品来执行命令/出纪要）----------
    # 本分组的展示完全由面板的"智能体表格"接管（见 web/app.js renderAgentTable）：
    #   * agentBackend = 当前选中哪个产品（单选语义由表格的互斥开关维护），
    #                    仍由注册表 active_name() 读取，但不作为表单行展示；
    #   * agentCustomPath = CLI 类产品的可执行文件路径，在表格展开区里渲染；
    #   * dshBaseUrl = DSH 的服务地址，同样在 DSH 那一行的展开区里渲染
    #     （2026-09-19 从「面板与服务」挪上来：摊在那儿用户不知道它跟谁有关）。
    # 这些都 hidden=True：不进 /api/settings 的 settings 列表，只走 agents 字段与本行内联。
    "dshBaseUrl":      dict(value="http://127.0.0.1:43120", grp="agent", label="DSH 服务地址",
                            description="DSH Desktop 2.x 的 Web 服务地址（GUI 与 API 同端口，默认 43120）",
                            value_type="str", hidden=True),
    "agentBackend": dict(value="dsh", grp="agent", label="执行智能体",
                         description="ECHO 把命令与会议纪要交给哪个智能体执行；"
                                     "在面板的智能体表格里切换。默认 DSH",
                         value_type="str", options=["dsh", "codebuddy", "harness"], hidden=True),
    "agentCodebuddyEnabled": dict(value=True, grp="agent", label="启用 CodeBuddy Code",
                                  description="腾讯 CodeBuddy Code（WorkBuddy 内置同一引擎）",
                                  value_type="bool", agent_key="codebuddy", hidden=True),
    "agentCustomPath": dict(value="", grp="agent", label="CLI 路径",
                            description="可执行文件路径；留空 = 自动探测"
                                        "（PATH → WorkBuddy 内置目录）。仅在自动探测失败时需要填",
                            value_type="str", hidden=True),
    # ---- 独立 DeepSeek Harness（npm @deepseek-ai/dsh）：随 ECHO 一起启动的那个 agent ----
    # 与 DSH Desktop 只差两点：**谁提供 web 服务**（npm 包 vs 桌面客户端）、**鉴权**
    # （启动时打印的 token → Cookie vs 逆向签名 Cookie）；/api 接口面完全相同（2026-09-19 实测）。
    # 详见 app/harness_proc.py 与 docs/独立harness接入.md。
    "agentHarnessEnabled": dict(value=False, grp="agent", label="启用标准版 harness（DeepSeek Harness）",
                                description="本项与「执行智能体」都指向它时，ECHO 才把独立 harness "
                                            "作为子进程拉起（切走即停）；需要本机 Node / npx",
                                value_type="bool", hidden=True),
    "harnessHome": dict(value="", grp="agent", label="harness 数据目录（DSH_HOME）",
                        description="独立 harness 的家目录；留空 = {DATA}/harness。"
                                    "刻意与 Desktop 的家目录分开（各用各的），"
                                    "两边的会话与设置互不干扰",
                        value_type="str", hidden=True),
    "harnessPort": dict(value=43199, grp="agent", label="harness 端口",
                        description="独立 harness 的监听端口（默认 43199，刻意避开 Desktop 的 43120，"
                                    "两者可同时运行）",
                        value_type="int", hidden=True),
    "harnessCommand": dict(value="npx -y @deepseek-ai/dsh web", grp="agent", label="harness 启动命令",
                           description="拉起独立 harness 的命令（--port / --no-open 由 ECHO 追加）。"
                                       "安装技能会填成 <安装目录>/harness/dsh 里的本地入口（冷启动约 10 秒）；"
                                       "装不起来就把这里改回 npx -y @deepseek-ai/dsh web 兜底",
                           value_type="str", hidden=True),
    "harnessToken": dict(value="", grp="agent", label="harness 访问 token",
                         description="只有当你自己启动了 harness（终端里跑 npx @deepseek-ai/dsh web）"
                                     "才需要填它打印出来的 token；ECHO 自己拉起时会自动获取。"
                                     "留空 = 不改动已配置的值",
                         value_type="str", hidden=True, secret=True),
    # ---------- 能力 provider（P5 / D25：ASR / LLM / TTS 各选一个）----------
    # 留空 = 用该 kind 的默认实现（本地转写引擎 / ECHO AUTO 多上游路由 / 本平台离线朗读）。
    # 可选项是**运行时**注册出来的（见 GET /api/providers），所以这里不写死 options。
    "providerAsr": dict(value="", grp="provider", label="转写 provider", hidden=True,
                        description="留空 = 默认的本地转写引擎。在下方「能力 provider」卡片里选，"
                                    "或在这里填 provider id（见 GET /api/providers）",
                        value_type="str"),
    "providerLlm": dict(value="", grp="provider", label="语言模型 provider", hidden=True,
                        description="留空 = ECHO AUTO（多上游派发路由）。配了它，纪要不依赖 agent 也能生成；"
                                    "在下方「能力 provider」卡片里选",
                        value_type="str"),
    "providerTts": dict(value="", grp="provider", label="朗读 provider", hidden=True,
                        deprecated=True,
                        description="已弃用：朗读「用哪个」就是 ttsEngine（自动 / edge-tts / "
                                    "本平台离线引擎 / 关闭），两处开关会互相打架"
                                    "（配了 providerTts 时 ttsEngine=off 关不掉朗读）。"
                                    "2026-09-19 起只保留 ttsEngine；本项的值已自动搬到 ttsEngine",
                        value_type="str"),
    # ---- 在线服务预设（P5）：一个 OpenAI 兼容端点 + 一把密钥 ----
    # 这三项就是"配一个在线 LLM"的全部输入；配好把 providerLlm 指向 openai-llm 即生效。
    # 内网网关地址**不写进仓库**（属单位内部信息，见 REFACTOR-PLAN §13.4）：
    # 面板给"内网网关"预设时留空 base_url，让用户自己填。
    "providerLlmBaseUrl": dict(value="", grp="provider", label="在线 LLM 地址", hidden=True,
                               description="OpenAI 兼容端点的根地址，例如 https://api.deepseek.com/v1"
                                           "（内网网关填单位自己的地址；留空 = 不用在线 LLM）。"
                                           "可用下方「能力 provider」卡片的预设一键填入",
                               value_type="str"),
    "providerLlmApiKey": dict(value="", grp="provider", label="在线 LLM 密钥", hidden=True,
                              description="只保存在本机数据库；接口（含面板）永不回显。"
                                          "留空 = 不改；要清空请点「清除」。数据去向见「在线 LLM 地址」",
                              value_type="str", secret=True),
    "providerLlmModel": dict(value="", grp="provider", label="在线 LLM 模型名", hidden=True,
                             description="留空 = 用服务端默认（如 deepseek-chat / gpt-4o-mini）",
                             value_type="str"),
    "providerAsrBaseUrl": dict(value="", grp="provider", label="在线转写地址", hidden=True,
                               description="OpenAI 兼容的 /audio/transcriptions 根地址"
                                           "（例如 https://api.openai.com/v1）；留空 = 不用在线转写",
                               value_type="str"),
    "providerAsrApiKey": dict(value="", grp="provider", label="在线转写密钥", hidden=True,
                              description="只保存在本机数据库；接口永不回显。留空 = 不改；要清空请点「清除」。"
                                          "注意：会议音频会整段上传到该服务",
                              value_type="str", secret=True),
    "providerAsrModel": dict(value="", grp="provider", label="在线转写模型名", hidden=True,
                             description="留空 = whisper-1（OpenAI 兼容服务的默认转写模型）",
                             value_type="str"),
    # ---------- 能力路由（3.0）：每个槽用哪个后端 ----------
    # **全部 hidden**：这一批由将来的「能力路由」页签承载（与 provider 那批同一个做法）。
    # hidden 的好处是**不需要动 `web/app.js` 的分组表**，也不会在设置页里冒出半成品；
    # 值照样能读能写（`Settings.update` 只拒 deprecated，不拒 hidden）。
    "capabilityEchoServerUrl": dict(
        value="", grp="capability", label="ECHO 能力后端地址", hidden=True,
        description="ECHO 能力后端（无状态服务端）的根地址，例如 http://gpu-01:8900。"
                    "留空 = 不用这个后端。它只做 GPU 重活（会议转写/说话人分离/声纹），"
                    "**不存任何业务数据**",
        value_type="str"),
    "capabilityEchoServerToken": dict(
        value="", grp="capability", label="ECHO 能力后端令牌（配对获得）", hidden=True,
        description="配对换来的短期 JWT。过期后需要重新配对/换令牌 —— "
                    "本版**不做自动续期**，过期会如实报「凭据不被接受」而不是静默失败",
        value_type="str", secret=True),
    "capabilityEchoServerStaticToken": dict(
        value="", grp="capability", label="ECHO 能力后端静态令牌", hidden=True,
        description="服务端 `auth.mode=token` 时用的那把静态令牌（单人/本机场景）。"
                    "配了配对令牌就以那个为准",
        value_type="str", secret=True),
    "capabilityPrivacy": dict(
        value="lan", grp="capability", label="允许音频去哪", hidden=True,
        options=["none", "lan", "wan"],
        description="none = 不出机（只用本机引擎）；lan = 允许发到单位内网的 ECHO 后端；"
                    "wan = 还允许更远的公共服务。这是**约束**，不是优先级 —— "
                    "它决定哪些后端根本不被考虑，见能力路由的 privacy 判据",
        value_type="str"),
    "capabilityMeetingAsrBackend": dict(
        value="auto", grp="capability", label="会议转写用哪个后端", hidden=True,
        options=["auto", "echo-server", "local", "intranet"],
        description="会议链路的 asr.text。auto = 按「内网公共 → ECHO 后端 → 本机(装了才用)」"
                    "的顺序挑第一个可用的",
        value_type="str"),
    "capabilityDiarizeBackend": dict(
        value="auto", grp="capability", label="说话人分离用哪个后端", hidden=True,
        options=["auto", "echo-server", "local", "off"],
        description="说话人时间轴与逐说话人嵌入。off = 不要说话人（会议照常出无说话人的转写）。"
                    "**产出向量的能力只能落在能声明向量空间的后端上**（本机在内网公共服务上不行）",
        value_type="str"),
    "capabilityEmbedBackend": dict(
        value="auto", grp="capability", label="声纹提取用哪个后端", hidden=True,
        options=["auto", "echo-server", "local", "off"],
        description="现场注册联系人时的单条嵌入。**必须与会议里认出的说话人同源**"
                    "（同一个向量空间），否则余弦相似度没有意义 —— 而比错的表现是"
                    "**认错人且不报错**，所以这条由路由强制，不靠自觉",
        value_type="str"),
    # ---------- 面板 / 服务 ----------
    "panelAutoRefresh": dict(value=3, grp="panel", label="面板自动刷新秒",
                             description="仪表盘 / 启动页 / 模型路由页的自动刷新间隔（秒）；"
                                         "0 = 不自动刷新（切页或手动点刷新时才更新）。"
                                         "会议转写进度条不受此项影响（它需要一直更新）",
                             value_type="int"),
    "apiAuthEnabled":   dict(value=False, grp="panel", label="API 鉴权（手机 App）",
                             description="开启后**所有**接口都要求 Bearer 令牌 —— 包括本地面板，"
                                         "而面板不带令牌，因此开启后面板会连不上，只能带令牌"
                                         "或直接改库关掉。仅在外网访问/手机 App 场景下开启",
                             value_type="bool"),
    # ---------- 模型路由（ECHO AUTO）----------
    # 路由进程把 dsh-failover/config.json 里的 groups 转成 DSH 里的可选模型；
    # 组的成员/优先级在「模型路由」页签里勾选，这里只放全局行为开关。
    "routerAutoRegister": dict(value=True, grp="router", label="启动时注册到 DSH",
                               description="ECHO 启动后自动把模型组写成 DSH 的本地模型（ECHO AUTO）；"
                                           "关掉后需手动点「模型路由 → 注册到 DSH」",
                               value_type="bool"),
    "routerDisplayName":  dict(value="ECHO AUTO", grp="router", label="模型组显示名",
                               description="DSH 模型列表里看到的名称（改完立即同步注册）",
                               value_type="str"),
    "routerProbeInterval": dict(value=45, grp="router", label="健康探测间隔秒",
                                description="后台探测各成员可达性的间隔（0 表示不改动）；"
                                            "真实请求本身也会更新健康表",
                                value_type="int"),
    "routerFirstByteTimeout": dict(value=20.0, grp="router", label="成员首字节超时秒",
                                   description="成员接受连接后多久没吐第一个字就算不通、换下一个；"
                                               "改这项需要重启模型路由进程才生效",
                                   value_type="float"),
    "routerConnectTimeout": dict(value=1.5, grp="router", label="成员连接超时秒",
                                 description="TCP/TLS 握手耐心（故意很短，内网 DNS 失败要秒切）；"
                                             "改这项需要重启模型路由进程",
                                 value_type="float"),
    "routerBreakerThreshold": dict(value=2, grp="router", label="熔断连续失败次数",
                                   description="某成员连续失败几次后进入熔断、暂时跳过",
                                   value_type="int"),
    "routerBreakerCooldown": dict(value=30, grp="router", label="熔断冷却秒",
                                  description="熔断后多久放行一次试水请求",
                                  value_type="int"),
}

#: 面板渲染顺序 = 上面 DEFAULTS 的声明顺序（分组内也按它排）。
# 为什么必须显式给：`db.all_settings()` 是按 (grp, key) **字母序**返回的（库层的稳定排序），
# 直接拿它渲染会让"相关项挨在一起"的编排失效（例如三个提示音开关会被 maxRecordMs 之类的
# 键隔开）。这里把声明顺序作为 `order` 字段随行发给面板，由面板排序 —— 库层保持简单。
SETTING_ORDER = {key: index for index, key in enumerate(DEFAULTS)}

#: 二级子分组（`sub`）：**只用于展示**，与 grp 是"包含"关系而不是并列关系。
# 为什么需要：语音命令是个大功能，它的四件事（怎么录、发到哪、播报什么、提示音）
# 各成一组才看得清；但它们不该和「语音命令」平级（2026-09-19 用户反馈："语音命令和
# beep/command/speech 应该是包含不是并列"）。子分组名与顺序在面板侧
# （web/app.js 的 SET_SUB_ORDER / SET_SUB_NAMES），这里只声明归属。
SUBS = ("record", "command", "speech", "beep")

# 默认值迁移：早期版本把某个默认值当作"用户已设置"写进了库（seed_defaults 不覆盖已有
# value），此后改 DEFAULTS 就不生效了。这里登记"旧默认值 → 采用新默认值"：
# 只有当前值仍等于旧默认值时才改写，用户手动改过的一律不动。
# 2026-09-12：panelOpenMode 旧默认 app（打开整窗）→ 新默认 sidebar（右缘边条，与 DSH
# 插件升级前的边条行为一致）。
DEFAULT_MIGRATIONS = {
    "panelOpenMode": ("app", "sidebar"),
    # 2026-09-12：极简回复要求文案 V1（详情留会话）→ V2（先结论 + 换行详情）
    "minimalReplyHint": (_MINIMAL_REPLY_HINT_V1, _MINIMAL_REPLY_HINT_V2),
    # 2026-09-15：会议工作区默认值从「空（用固定纪要会话）」改为 ECHO 自己的
    # data/meetings 目录——因为 DSH 侧栏分组是显式登记制，只有指定了工作区目录
    # 才能把每场会议的会话登记进「会议工作区」。仅当用户从没改过（仍为空）才改写。
    "meetingWorkspace": ("", "{ECHO}/data/meetings"),
    # 2026-09-23：命令工作区旧默认是「空（建在 ECHO 根目录、侧栏未分组）」→ 新默认
    # `{ECHO}/data/command`。新机器装好就该在 DSH 侧栏看到「指令空间」分组，而不是
    # 攒一堆未分组的会话。仅当用户从没改过（仍为空）才改写；配过自己目录的一律不动。
    "commandWorkspace": ("", "{ECHO}/data/command"),
}

# 弃用项的值迁移：某个配置键被**弃用**（重复/失效）时，把"它当初表达的用户意图"
# 搬到仍然生效的那一项上，只在值等于触发值时才搬（用户自己在库里改过的一律不动）。
#   形状：被弃用键 -> ((触发值, 目标键, 目标值), ...)   —— 一个键可以有多条取值规则
# 放在 seed_defaults() 里跑：与 DEFAULT_MIGRATIONS 同一时机（每次启动比对一次，
# 搬完触发值就不成立，天然幂等）。
#: 目标值里的占位符：搬到"本平台的离线朗读引擎"（win=sapi / mac=say / linux=espeak）
OFFLINE_TTS_PLACEHOLDER = "{offline-tts}"

DEPRECATION_MIGRATIONS = {
    # 2026-09-19：worklogMode=off 与 worklogEnabled 是同一个"不归档"的两个开关（重复项），
    # 只保留总开关；老配置里选过「不归档」的，把总开关一起关掉 → 行为完全不变。
    "worklogMode": (("off", "worklogEnabled", False),),
    # 2026-09-19：providerTts 与 ttsEngine 重复（且会互相打架：配了 providerTts 时
    # ttsEngine=off 关不掉朗读）。把用户当初选的"本地/在线"意图搬到 ttsEngine ——
    # 选 edge-tts 的原样搬；选本地离线朗读的搬到本平台离线引擎（绝不搬成 auto：
    # auto 会优先走微软在线，等于把"我不想出网"的意图反过来）。
    "providerTts": (("edge-tts", "ttsEngine", "edge-tts"),
                    ("local-tts", "ttsEngine", OFFLINE_TTS_PLACEHOLDER)),
}


def _offline_tts_engine():
    """本平台 ttsEngine 候选项里的"离线引擎"取值（win=sapi / mac=say / linux=espeak）。"""
    opts = [str(o) for o in _effective_options("ttsEngine", DEFAULTS["ttsEngine"])]
    for o in opts:
        if o not in ("auto", "edge-tts", "off"):
            return o
    return "sapi"


class Settings:
    """配置门面：读改走内存缓存，写时落库。"""

    def __init__(self):
        self._cache = None

    def _load(self):
        if self._cache is None:
            self._cache = {}
            for k, meta in DEFAULTS.items():
                # 库里没有该键时，回落值是**本平台**的默认值（D11）
                self._cache[k] = expand_path(db.get_setting(k, _effective_default(k, meta)))
        return self._cache

    def seed_defaults(self):
        """首次启动写入默认值 + 同步元数据（不覆盖已有 value）+ 跑两类值迁移。"""
        for key, meta in DEFAULTS.items():
            value = _effective_default(key, meta)          # 平台声明优先（D11）
            options = _effective_options(key, meta)
            if db.get_setting(key) is None:
                db.set_setting(key, value, grp=meta["grp"], label=meta["label"],
                               description=meta["description"], value_type=meta["value_type"],
                               options=options)
            else:
                # 已有值：仅同步面板元数据（分组/说明/选项），保留用户 value
                db.sync_setting_meta(key, meta["grp"], meta["label"],
                                     meta["description"], meta["value_type"], options)
                # 旧默认值迁移（只在值仍等于旧默认时改写）
                migration = DEFAULT_MIGRATIONS.get(key)
                if migration:
                    old_value, new_value = migration
                    if db.get_setting(key) == old_value:
                        db.set_setting(key, new_value, grp=meta["grp"], label=meta["label"],
                                       description=meta["description"], value_type=meta["value_type"],
                                       options=options)
                # 弃用项的值迁移（把"已弃用开关"表达的意图搬到还在生效的那一项上）
                self._migrate_deprecated(key)
        self._cache = None

    def _migrate_deprecated(self, key):
        """把已弃用键的取值翻译成仍生效键的取值（见 DEPRECATION_MIGRATIONS）。

        只在"当前值 == 触发值"时搬一次；搬完触发条件不再成立，所以每次启动跑都安全。
        目标键用 `db.upsert_settings`（保留既有元数据），搬动用 add_log 留痕——
        用户看到"归档总开关被关掉"时能查到是这次配置收敛造成的，不是 bug。
        """
        rules = DEPRECATION_MIGRATIONS.get(key)
        if not rules:
            return
        current = db.get_setting(key)
        for trigger, target, target_value in rules:
            if str(current if current is not None else "").strip().lower() != trigger:
                continue
            if target_value == OFFLINE_TTS_PLACEHOLDER:
                target_value = _offline_tts_engine()
            db.upsert_settings({target: target_value})
            try:
                db.add_log("info", "config",
                           "%s 已弃用（当前值 %s）→ %s 已自动设为 %s"
                           % (key, trigger, target, target_value))
            except Exception:
                pass

    def get(self, key, default=None):
        cache = self._load()
        if key in cache:
            return cache[key]
        if key in DEFAULTS:
            return DEFAULTS[key]["value"]
        return default

    def all(self):
        """合并 DB 元数据与当前值，返回面板可直接渲染的列表。

        被剔除的项（面板与 /api/settings 都看不到，但值仍留在库里、get() 依然可读）：
          * deprecated=True —— 已弃用的历史配置；
          * hidden=True     —— 由面板自定义 UI 承载的配置（例如「智能体」分组
                               改由智能体表格渲染，就不再作为普通表单行出现）。

        每行带 `order`（= DEFAULTS 里的声明顺序）：库层返回的是 (grp, key) 字母序，
        光靠它无法表达"三个提示音开关要挨在一起"这类编排，面板据此字段排序。

        **密钥（``secret=True``）在这里被遮掉**（P5 凭据管理）：这一层是所有出口的必经之路
        （`/api/settings`、面板、将来的手机端），所以在源头遮一次，而不是指望每个消费方
        都记得处理。遮法：``value`` 一律置空 + ``hasValue`` 告诉界面"库里其实有值"，
        ``secret`` 让界面渲染成密码框。真实值只有 `Settings.get()`（进程内）能拿到 ——
        供 provider 组装请求头用。
        """
        rows = db.all_settings()
        out = []
        for r in rows:
            meta = DEFAULTS.get(r["key"], {})
            if meta.get("deprecated") or meta.get("hidden"):
                continue
            r = dict(r)
            r["order"] = SETTING_ORDER.get(r["key"], len(DEFAULTS))
            # 二级子分组（可选）：同一个 grp 内的再分节，面板渲染成可折叠的小节
            r["sub"] = meta.get("sub", "")
            if meta.get("options_from") == "audio_inputs":
                # 设备的下拉项在**运行时**注入：设备会插拔，静态写进库会过期。
                # 把当前值一起传进去 —— 旧索引/已拔掉的设备名也要在列表里出现，
                # 否则面板会显示成"系统默认"，一保存就把真值覆盖掉。
                r["options"] = _audio_input_options(r.get("value"))
            elif meta.get("options_from") == "audio_outputs":
                r["options"] = _audio_output_options(r.get("value"))
            if meta.get("secret"):
                r["hasValue"] = bool(str(r.get("value") or "").strip())
                r["value"] = ""
                r["secret"] = True
            out.append(r)
        return out

    def deprecated_keys(self):
        """已弃用的配置键（供诊断/清理脚本使用）。"""
        return [k for k, meta in DEFAULTS.items() if meta.get("deprecated")]

    def update(self, mapping):
        """批量更新配置（校验 key 存在；类型按 value_type 强转）。

        **密钥的防误清空**（2026-09-19，P5 凭据管理）：`secret=True` 的项在出口一律被遮成空串
        （见 `all()`），于是"面板整批回传"会把空串当成新值 → **用户的密钥被静默清掉**。
        所以这里定死一条契约：
          * 空串（或纯空白）= **不改**（跳过，计入 `skipped`）；
          * 要清空必须显式送 ``CLEAR_SECRET``（面板的「清除」按钮就是这么做的）。

        **已弃用项直接拒收**（2026-09-19 设置收敛）：deprecated=True 的键还留在 DEFAULTS 里只是
        为了兼容读取与迁移比对，写它不会有任何效果 —— 静默接受会造出"设置界面里没有、
        但改了却没反应"的假象，所以这里显式跳过。用户仍可用 `reset()` 把它恢复成默认值。
        """
        cleaned = {}
        skipped = []
        for k, v in mapping.items():
            if k not in DEFAULTS:
                continue
            if DEFAULTS[k].get("deprecated"):
                skipped.append(k)              # 已弃用：不再生效，拒收
                continue
            vt = DEFAULTS[k]["value_type"]
            try:
                if vt == "int":
                    v = int(v)
                elif vt == "float":
                    v = float(v)
                elif vt == "bool":
                    v = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
                elif vt == "list":
                    v = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
            except Exception:
                continue
            if DEFAULTS[k].get("secret"):
                if isinstance(v, str) and v.strip() == CLEAR_SECRET:
                    v = ""
                elif not str(v or "").strip():
                    skipped.append(k)          # 空 = 不改（防面板整批回传把密钥清掉）
                    continue
            cleaned[k] = v
        if cleaned:
            db.upsert_settings(cleaned)
            self._cache = None
        return cleaned

    def reset(self, key=None):
        """恢复默认值（单个或全部）。默认值取**本平台**声明的那个（D11）。"""
        if key:
            if key in DEFAULTS:
                meta = DEFAULTS[key]
                db.set_setting(key, _effective_default(key, meta), grp=meta["grp"],
                               label=meta["label"], description=meta["description"],
                               value_type=meta["value_type"],
                               options=_effective_options(key, meta))
        else:
            self.seed_defaults()
            for key, meta in DEFAULTS.items():
                db.set_setting(key, _effective_default(key, meta), grp=meta["grp"],
                               label=meta["label"], description=meta["description"],
                               value_type=meta["value_type"],
                               options=_effective_options(key, meta))
        self._cache = None


settings = Settings()
