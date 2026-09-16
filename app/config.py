# -*- coding: utf-8 -*-
"""config.py — ECHO 配置管理（存储于 SQLite settings 表）

所有配置以"键值 + 面板元数据"的形式入库，面板据此动态渲染表单：
  grp        分组（general/voice/meeting/dsh/panel）
  value_type str|int|float|bool|json|list  → 决定输入控件
  options    JSON 候选值列表 → 下拉/多选
  deprecated True = 已弃用，面板不再展示（见下）

首次启动调用 seed_defaults() 写入默认值（INSERT OR IGNORE，不覆盖已有配置）。
读取用 get()/all()，写入用 update()（批量）。

已弃用项（deprecated=True）：
  仍保留在 DEFAULTS 与库中，保证老配置可读、迁移可比对、get() 兼容；
  但 all() 会把它们从面板数据里剔除，因此设置窗口不再出现。
  确认某个已弃用项彻底无人引用后，才可从 DEFAULTS 删除。
"""
import os

import app.db as db

# ---- 路径占位符 -----------------------------------------------------------
# 配置里存占位符而不是绝对路径，这样同一个默认值在任何机器/任何安装目录都能用，
# 也不会把个人路径写进仓库。读取时（Settings._load）统一展开。
#   {ECHO} = ECHO 根目录（仓库根）
#   {DATA} = ECHO 的 data 目录
ECHO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def expand_path(value):
    """展开配置值里的 {ECHO} / {DATA} 占位符；非字符串原样返回。

    展开后统一走 os.path.normpath：占位符写的是正斜杠（`{ECHO}/data/meetings`），
    直接拼接会得到 `C:\\...\\ECHO-public/data/meetings` 这种混合分隔符——Windows
    能用，但日志里难看，且与用户手填的反斜杠路径比较时还需要额外容错。
    """
    if not isinstance(value, str) or "{" not in value:
        return value
    out = (value.replace("{ECHO}", ECHO_ROOT)
                .replace("{DATA}", os.path.join(ECHO_ROOT, "data")))
    return os.path.normpath(out)

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


# key -> dict(value, grp, label, description, value_type, options, deprecated?)
# deprecated=True 的项不进入设置窗口（all() 会过滤），仅保留兼容读取。
DEFAULTS = {
    # ---------- 通用 ----------
    "dshBaseUrl":      dict(value="http://127.0.0.1:43120", grp="general", label="DSH 服务地址",
                            description="DSH Desktop 2.x 的 Web 服务地址（GUI 与 API 同端口，默认 43120）", value_type="str"),
    "serverPort":      dict(value=8970, grp="general", label="ECHO 面板端口",
                            description="控制面板与 API 的监听端口（8890 曾被系统保留段占用，改用 8970）", value_type="int"),
    "commandIdleRotateHours": dict(value=4, grp="general", label="命令会话空闲轮换小时",
                                   description="默认命令会话空闲超过 N 小时且新指令未要求延续上一话题时，"
                                               "自动轮换新会话（0=关闭；会话不在默认工作区时会强制轮换一次）",
                                   value_type="float"),
    "commandWorkspace": dict(value="", grp="general", label="命令会话工作区",
                             description="默认命令会话建在这个目录对应的 DSH 工作区里，"
                                         "从而归入侧栏对应分组（例如你自己的「日常交互」）。"
                                         "留空 = 建在 ECHO 根目录（侧栏显示为未分组）",
                             value_type="str"),
    "userLocation":    dict(value="北京", grp="general", label="用户所在地",
                            description="发给 DSH 命令时附带的地理位置（天气/时间等问答需要）",
                            value_type="str"),
    "sendEnvContext":  dict(value=True, grp="general", label="发送环境上下文",
                            description="命令前附加当前时间与所在地，让 DSH 对'今天/明天/本地'有概念",
                            value_type="bool"),
    "device":          dict(value="auto", grp="general", label="计算设备",
                            description="auto=cuda 优先，失败回退 CPU",
                            value_type="str", options=["auto", "cpu", "cuda"]),
    "sttModel":        dict(value="sensevoice", grp="voice", label="命令转写引擎",
                            description="sensevoice 最快（中文短命令），qwen3asr 更准（需下载模型），sherpa 流式，whisper 模型按名",
                            value_type="str",
                            options=["sensevoice", "qwen3asr", "sherpa", "tiny", "base", "small", "medium", "large"]),
    "sttLanguage":     dict(value="zh", grp="voice", label="转写语言",
                            description="命令与会议共用的转写语言：zh/en/ja/ko/yue，或 auto 自动识别。"
                                        "填全名（如 Chinese）会自动纠正；非法值回退 zh（Whisper 只认 ISO 码，"
                                        "填错会让转写结果变空）",
                            value_type="str", options=["zh", "en", "ja", "ko", "yue", "auto"]),
    "triggerKeys":     dict(value=["vol_up"], grp="voice", label="媒体键触发",
                            description="耳机/键盘媒体键作为说话快捷键（vol_up/play_pause/next/prev）",
                            value_type="list", options=["vol_up", "vol_down", "play_pause", "next", "prev"]),
    "wakeHotkey":      dict(value="Ctrl+Alt+C", grp="voice", label="唤醒热键",
                            description="全局热键开始录音说话", value_type="str"),
    "fallbackHotkey":  dict(value="Ctrl+Alt+V", grp="voice", label="回退热键",
                            description="媒体键失效时使用的备用热键", value_type="str"),
    # 面板热键由 ECHO 服务自己 RegisterHotKey 注册（纯 ctypes），**不依赖 DSH 插件**：
    # 2026-09-12 实测 DSH Desktop 2.0.9 里插件（ESM 动态 import）取不到 electron 的
    # app/BrowserWindow/screen（只有 net/systemPreferences），插件侧的 globalShortcut
    # 不可用，因此把"打开仪表盘"的热键落在 ECHO 进程里。
    "panelHotkey":     dict(value="Ctrl+Shift+E", grp="voice", label="仪表盘热键",
                            description="全局热键切换 ECHO 仪表盘（由 ECHO 服务进程注册）", value_type="str"),
    "panelOpenMode":   dict(value="sidebar", grp="voice", label="仪表盘打开方式",
                            description="sidebar=右缘边条（无边框/置顶/铺满高度，与升级前一致）；app=Chromium 应用窗口；browser=默认浏览器",
                            value_type="str", options=["sidebar", "app", "browser"]),
    "panelAutoStart":  dict(value=True, grp="voice", label="启动时自动显示折叠条",
                            description="ECHO 启动后自动在屏幕右缘显示折叠条（仅当「仪表盘打开方式」= sidebar 时生效）；"
                                        "已在运行则不打扰（不会把已展开的面板收起来）",
                            value_type="bool"),
    "panelStartCollapsed": dict(value=True, grp="voice", label="自动显示时收起为折叠条",
                                description="True=启动后显示 64px 折叠条（点箭头/热键展开）；False=直接展开面板",
                                value_type="bool"),
    "silenceThreshold": dict(value=0.012, grp="voice", label="静音阈值",
                             description="音量低于此值视为静音（0~1）", value_type="float"),
    "silenceHangoverMs": dict(value=1100, grp="voice", label="静音收尾毫秒",
                              description="静音持续多久自动停录", value_type="int"),
    "noSpeechAbortMs": dict(value=4000, grp="voice", label="无语音放弃毫秒",
                            description="开口后多长时间没声音就放弃", value_type="int"),
    "maxRecordMs":     dict(value=30000, grp="voice", label="最长录音毫秒",
                            description="单次命令录音上限", value_type="int"),
    "inputDeviceId":   dict(value=-1, grp="voice", label="输入设备 ID",
                            description="-1=系统默认麦克风", value_type="int"),
    "consumeMediaKey": dict(value=True, grp="voice", label="拦截媒体键",
                            description="触发后不向系统透传媒体键", value_type="bool"),
    "beepOnStart":     dict(value=True, grp="voice", label="开始提示音", description="开始录音时播放提示音",
                            value_type="bool"),
    "beepOnDone":      dict(value=True, grp="voice", label="停录提示音", description="停止录音时播放提示音",
                            value_type="bool"),
    "beepOnSend":      dict(value=True, grp="voice", label="发送提示音", description="命令发送成功提示音",
                            value_type="bool"),
    "voiceConfirm":    dict(value=True, grp="voice", label="语音复述确认",
                            description="发送前朗读一遍识别到的命令", value_type="bool"),
    "voiceBrief":      dict(value=True, grp="voice", label="语音简报",
                            description="任务完成后朗读精简结果", value_type="bool"),
    "ttsEngine":       dict(value="auto", grp="voice", label="语音合成引擎",
                            description="auto=优先 edge-tts（★微软在线：播报文本会发往微软，需访问 speech.platform.bing.com），失败才降级 Windows SAPI（全离线，音色略差）",
                            value_type="str", options=["auto", "edge-tts", "sapi", "off"]),
    "notifyOnSend":    dict(value=True, grp="voice", label="桌面通知",
                            description="发送成功后弹系统通知", value_type="bool"),
    "maxBriefChars":   dict(value=200, grp="voice", label="简报最大字数",
                            description="语音简报文本长度上限", value_type="int"),
    # ---------- 极简回复（2026-09-12 用户要求）----------
    # 命令末尾附一段"先给极简结论、再换行给详情"的要求：
    # 语音只朗读结论那一段（assistant.conclusion_only），详情留在回复/会话里给人看。
    "minimalReply":    dict(value=True, grp="voice", label="要求极简回复",
                            description="在命令末尾附一句要求：先给极简结论，再换行写详情（语音只读结论）",
                            value_type="bool"),
    "minimalReplyChars": dict(value=60, grp="voice", label="极简回复字数上限",
                              description="写进要求的长度约束（口语一句话约 30~60 字）", value_type="int"),
    "minimalReplyHint": dict(value=_MINIMAL_REPLY_HINT_V2,
                             grp="voice", label="极简回复要求文案",
                             description="拼在命令末尾；{chars} 会替换成上面的字数上限",
                             value_type="str"),
    # ---------- 唤醒词 ----------
    "wakeEnabled":     dict(value=False, grp="wake", label="启用语音唤醒",
                            description="说唤醒词免按键唤起（唤醒词见下方配置）", value_type="bool"),
    "wakePaused":      dict(value=True, grp="wake", label="唤醒暂停（勿扰）",
                            description="来电/会议期间临时关闭唤醒", value_type="bool"),
    "wakeEngine":      dict(value="sherpa", grp="wake", label="唤醒引擎",
                            description="sherpa-onnx KWS（离线，关键词直接指定）",
                            value_type="str", options=["sherpa", "openwakeword"]),
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
    "meetingSttModel":  dict(value="sensevoice", grp="meeting", label="会议转写模型",
                             description="sensevoice 最快（中文会议推荐）；small/medium/large 是 whisper；qwen3asr 最准但慢约 20 倍（GPU rtf≈0.45）",
                             value_type="str",
                             options=["sensevoice", "qwen3asr", "small", "medium", "large"]),
    "meetingSegmentMinutes": dict(value=10, grp="meeting", label="分段分钟",
                                  description="录音每 N 分钟存一个文件", value_type="int"),
    "meetingAutoSummarize": dict(value=True, grp="meeting", label="自动生成纪要",
                                 description="转写完成后自动请 DSH 生成纪要（★转写全文会发给 DSH 配置的模型服务：内网网关即贵单位内网，公网 API 即模型厂商）", value_type="bool"),
    "meetingKeepRawAudio": dict(value=True, grp="meeting", label="保留原始音频",
                                description="删除会议时是否同时删除音频", value_type="bool"),
    "meetingDiarize":   dict(value=False, grp="meeting", label="区分说话人",
                             description="本地 pyannote 分离（CPU 下较慢）", value_type="bool"),
    # ---------- 常用联系人声纹（issue #6）：改名入库 → 新会议自动认人 ----------
    # 样本是说话人嵌入（256 维），只存本机 data/echo.db；不做云端、不出网。
    # 【默认关闭】声纹属于生物特征数据：收集与自动认人都必须由用户显式开启（opt-in）。
    # 注意：不要用 DEFAULT_MIGRATIONS 做 True→False 的翻转 —— 那套机制每次启动都会比对
    # 「旧默认值」，用户一旦主动开启就会被下一次启动翻回去；改默认值 + 让用户自己开即可。
    "voiceprintEnabled": dict(value=False, grp="meeting", label="声纹识别常用联系人",
                              description="默认关闭。开启后会议转写会用声纹库自动识别已入库的联系人，"
                                          "把「说话人N」直接标成联系人名（需先开启「区分说话人」，"
                                          "且联系人有已入库的声纹样本）；关闭时不留存任何声纹样本",
                              value_type="bool"),
    "voiceprintAutoEnroll": dict(value=False, grp="meeting", label="改名时自动入库声纹",
                                 description="默认关闭。开启后在会议里把说话人改名为联系人时，"
                                             "自动把该说话人本场的声音存成声纹样本（声纹库属生物特征数据，"
                                             "样本可在会议页「说话人管理」里查看/删除）",
                                 value_type="bool"),
    "voiceprintThreshold": dict(value=0.65, grp="meeting", label="声纹匹配阈值",
                                description="余弦相似度下限（0~1）：越高越不容易认错人、也越容易漏认。"
                                            "默认 0.65 偏保守；先看日志（source=voiceprint）里的实际相似度再调",
                                value_type="float"),
    "voiceprintMargin": dict(value=0.05, grp="meeting", label="声纹歧义间隔",
                             description="候选联系人与次优的最小差距：差距过小视为认不准，不自动命名",
                             value_type="float"),
    "meetingWorkspace": dict(value="{ECHO}/data/meetings", grp="meeting",
                             label="会议纪要工作区",
                             description="一场会议一个 DSH 会话（纪要/分段/归档共用），下一场新建；"
                                         "这些会话都会登记进这个目录对应的 DSH 工作区，"
                                         "从而归入侧栏的「会议工作区」分组。"
                                         "{ECHO} = ECHO 根目录；留空 = 用固定的纪要会话（不分组）",
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
                        description="skill=委派 DSH 调用你自己的归档技能（推荐，可完全自定义归档规则）；off=不归档",
                        value_type="str", options=["skill", "off"]),
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
    #   * agentCustomPath = CLI 类产品的可执行文件路径，在表格展开区里渲染。
    # 三者都 hidden=True：不进 /api/settings 的 settings 列表，只走 agents 字段与本行内联。
    "agentBackend": dict(value="dsh", grp="agent", label="执行智能体",
                         description="ECHO 把命令与会议纪要交给哪个智能体执行；"
                                     "在面板的智能体表格里切换。默认 DSH",
                         value_type="str", options=["dsh", "codebuddy"], hidden=True),
    "agentCodebuddyEnabled": dict(value=True, grp="agent", label="启用 CodeBuddy Code",
                                  description="腾讯 CodeBuddy Code（WorkBuddy 内置同一引擎）",
                                  value_type="bool", agent_key="codebuddy", hidden=True),
    "agentCustomPath": dict(value="", grp="agent", label="CLI 路径",
                            description="可执行文件路径；留空 = 自动探测"
                                        "（PATH → WorkBuddy 内置目录）。仅在自动探测失败时需要填",
                            value_type="str", hidden=True),
    # ---------- 面板 ----------
    "panelAutoRefresh": dict(value=3, grp="panel", label="面板自动刷新秒",
                             description="仪表盘轮询间隔（0=关闭）", value_type="int"),
    "apiAuthEnabled":   dict(value=False, grp="panel", label="API 鉴权",
                             description="外部触点（手机 App）启用 Bearer Token 校验",
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
}


class Settings:
    """配置门面：读改走内存缓存，写时落库。"""

    def __init__(self):
        self._cache = None

    def _load(self):
        if self._cache is None:
            self._cache = {}
            for k, meta in DEFAULTS.items():
                self._cache[k] = expand_path(db.get_setting(k, meta["value"]))
        return self._cache

    def seed_defaults(self):
        """首次启动写入默认值 + 同步元数据（不覆盖已有 value）。"""
        for key, meta in DEFAULTS.items():
            if db.get_setting(key) is None:
                db.set_setting(key, meta["value"], grp=meta["grp"], label=meta["label"],
                               description=meta["description"], value_type=meta["value_type"],
                               options=meta.get("options", []))
            else:
                # 已有值：仅同步面板元数据（分组/说明/选项），保留用户 value
                db.sync_setting_meta(key, meta["grp"], meta["label"],
                                     meta["description"], meta["value_type"],
                                     meta.get("options", []))
                # 旧默认值迁移（只在值仍等于旧默认时改写）
                migration = DEFAULT_MIGRATIONS.get(key)
                if migration:
                    old_value, new_value = migration
                    if db.get_setting(key) == old_value:
                        db.set_setting(key, new_value, grp=meta["grp"], label=meta["label"],
                                       description=meta["description"], value_type=meta["value_type"],
                                       options=meta.get("options", []))
        self._cache = None

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
        """
        rows = db.all_settings()
        out = []
        for r in rows:
            meta = DEFAULTS.get(r["key"], {})
            if meta.get("deprecated") or meta.get("hidden"):
                continue
            out.append(r)
        return out

    def deprecated_keys(self):
        """已弃用的配置键（供诊断/清理脚本使用）。"""
        return [k for k, meta in DEFAULTS.items() if meta.get("deprecated")]

    def update(self, mapping):
        """批量更新配置（校验 key 存在；类型按 value_type 强转）。"""
        cleaned = {}
        for k, v in mapping.items():
            if k not in DEFAULTS:
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
            cleaned[k] = v
        if cleaned:
            db.upsert_settings(cleaned)
            self._cache = None
        return cleaned

    def reset(self, key=None):
        """恢复默认值（单个或全部）。"""
        if key:
            if key in DEFAULTS:
                db.set_setting(key, DEFAULTS[key]["value"], grp=DEFAULTS[key]["grp"],
                               label=DEFAULTS[key]["label"], description=DEFAULTS[key]["description"],
                               value_type=DEFAULTS[key]["value_type"],
                               options=DEFAULTS[key].get("options", []))
        else:
            self.seed_defaults()
            for key, meta in DEFAULTS.items():
                db.set_setting(key, meta["value"], grp=meta["grp"], label=meta["label"],
                               description=meta["description"], value_type=meta["value_type"],
                               options=meta.get("options", []))
        self._cache = None


settings = Settings()
