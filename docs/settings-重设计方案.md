# 设置菜单重设计方案（2026-09-19）

> 起因（用户原话）：「整个设置菜单的分组情况和设置项是否为失效或者重复需要重新设计，
> 同时要**验证设置是否被代码正常读取**。」
> 本文件记录**清点结论 → 改了什么 → 怎么验证 → 以后怎么防止再退化**。
> 机器生成的清单在 [`docs/settings-audit.md`](settings-audit.md)（由 `scripts/audit-settings.py` 产出）。

---

## 1. 清点：86 个设置项，判定分五类

`scripts/audit-settings.py` 对 `app/config.py:DEFAULTS` 的每一键扫 `app/ web/ scripts/ mac/ dsh-failover/`，
回答三个问题：**谁定义 / 谁写 / 谁读**，然后给判定：

| 判定 | 含义 | 处置 |
|---|---|---|
| `OK` | `app/` 的产品代码里真的读了 | 保留 |
| `OK-INDIRECT` | `app/` 里通过循环变量/键清单间接读（如热键元组、路由参数映射表） | 保留，但必须在 `ACK_INDIRECT` 里写清"谁在读" |
| `PANEL-READ` | 唯一消费方是面板自己（面板读了它来改变自己的行为） | 保留，需在 `ACK_PANEL` 里写清用途 |
| `PANEL-ONLY` | 只有面板把它渲染成表单/写进库，没人读 | **必须修**（本次修掉 1 个） |
| `DEAD` | 谁都不碰 | **必须修**（本次修掉 1 个） |
| `DEPRECATED` | 已弃用的历史项，不再展示 | 值仍可读、写入被拒收，等确认无人引用再删 |

清点时的结论（改造前）：`DEAD=1`（`panelAutoRefresh`）、`PANEL-ONLY=1`（`wakeEngine`）、
`OK-INDIRECT=9`、`OK=71`、`DEPRECATED=3`。

---

## 2. 失效项：把"摆着没用"的两项接上

### 2.1 `panelAutoRefresh`（DEAD → 生效）

* 现象：面板上写着"面板自动刷新秒（0=关闭）"，但 `web/app.js` 里仪表盘/启动页/模型路由页的
  轮询**写死 2000ms**，这项从来没被读过。
* 修法：轮询改成 `_panelRefreshSeconds()`（读 `panelAutoRefresh`）+ `_panelRefreshDue()`，
  1 秒一跳只做"对表"，真正刷不刷由设置决定；`0` = 不自动刷新。
* 边界：**会议转写进度条不受此项影响**（那是进度指示，停掉就看不到进度了）；该项默认 3 秒。
* 启动就取一次设置元数据（`loadPanelPrefs()`）：仪表盘轮询在用户进设置页之前就开始了，
  只等 `loadSettings()` 会让"用户改了也不生效"。

### 2.2 `wakeEngine`（PANEL-ONLY → 生效，并删掉幽灵选项）

* 现象①：后端**从没读过**它 —— `app/audio/wake.py` 固定"先流式 ASR，失败回退 KWS"。
* 现象②：选项里的 `openwakeword` **从来没有实现过**（面板里能选，选了没有任何效果）。
* 修法：选项收敛成真实存在的两种实现 `sherpa`（流式识别文字再匹配）/ `kws`（关键词 spotting），
  后端 `_make_detector()` 按它选路；历史配置里的未知值（如 `openwakeword`）按默认 sherpa 处理，
  不会因为一个旧配置值让唤醒起不来；启动状态与面板显示也改为报真实的实现名
  （`app/audio/wake.py:engine_label()`）。

---

## 3. 重复项：一个功能只留一个开关

判定标准：**两个设置项能不能互相覆盖**。能，就是重复项 —— 用户会看到"改了一个没用/两个打架"。

| 重复对 | 问题 | 处置 |
|---|---|---|
| `ttsEngine` ↔ `providerTts` | 同一个"朗读用哪个"的两个入口；而且**互相打架**：配了 `providerTts` 时 `ttsEngine=off` 关不掉朗读 | 只留 `ttsEngine`（它带 `off`、`auto` 回退、分平台候选项）。`providerTts` 弃用，老值自动搬到 `ttsEngine`（`edge-tts` → `edge-tts`；`local-tts` → **本平台离线引擎**，不搬 `auto`：`auto` 会优先出网，等于把"我不想出网"反过来了）。卡片里那一格改为**只读状态**（当前实现 + 就绪 + 是否出网 + 指路） |
| `worklogEnabled` ↔ `worklogMode=off` | 同一个"不归档"的两个开关 | 只留 `worklogEnabled`；`worklogMode` 弃用，老值 `off` 自动把总开关置关（行为不变） |
| `agentCodebuddyEnabled` ↔ `agentBackend` | 面板是"互斥单选"，但产品自带的启用开关没人写 → 会出现"开关已选中、状态却是未启用" | 面板选中某产品时**一并**写它的 `configKey`（选中即启用），自相矛盾的状态消失 |
| `sttModel` ↔ `meetingSttModel` | 同一族引擎、命名不齐（"命令转写引擎" vs "会议转写模型"） | 统一成「命令转写引擎」/「会议转写引擎」，都归 `model` 组（顶部「能力」页签承载，见 [能力页签重设计](能力页签重设计.md)） |
| `meetingsDir` ↔ `meetingWorkspace` | 一个管**文件**放哪、一个管 **DSH 会话**登记到哪个工作区，标签相似极易混淆 | 改名为「会议文件目录」/「会议会话工作区」，并在描述里互相点名 |

> 顺带修掉的"双刃"项：`apiAuthEnabled` 打开后**连本地面板也会 401**（面板不带令牌，
> 一开就连不上、也没法从面板关回来）。本次给它加了更强的描述 + 面板上的二次确认；
> 彻底的做法（面板自动持有本地令牌）列入待办，不混进本次改动。

---

## 4. 分组重设计

### 4.1 改造前的问题

* `voice` 26 项一锅端：转写语言、4 个热键、录音门限、提示音、TTS、播报措辞、面板启动行为混在一起；
* `general` 是个杂物抽屉：DSH 地址、端口、命令目标、上下文注入、计算设备；
* `panel` 只有 2 项，且 `apiAuthEnabled` 其实不是"面板"的事；
* 组内顺序是**字母序**（`db.all_settings()` 按 `(grp, key)` 返回），所以"三个提示音开关"被
  `maxRecordMs` 之类的键隔开，看起来毫无编排。

### 4.2 改造后（一级 8 个分组 + 二级小节；顺序 = 使用频率）

一级分组（后端 `grp`，面板标题一律中文）:

| 分组 `grp` | 面板标题 | 项数 | 内容 |
|---|---|---|---|
| `agent` | 智能体 | — | 智能体表格（谁干活）；该组只放 `hidden` 的智能体键 |
| `voice` | 语音命令 | 27 | 见下方二级小节 |
| `wake` | 唤醒词 | 9 | 启用唤醒、勿扰、唤醒词、别名、灵敏度、冷却、确认帧/窗口、静音门控 |
| `meeting` | 会议 | 4 | 分段分钟、自动生成纪要、保留原始音频、会议会话工作区 |
| `worklog` | 纪要归档 | 4 | 启用、归档前校正权限、笔记库根目录、归档提示词模板 |
| `panel` | 面板与服务 | 8 | DSH 地址、ECHO 端口、仪表盘热键/打开方式/自动显示/收起、自动刷新秒、API 鉴权 |
| `paths` | 存储路径 | 2 | 会议文件目录、模型目录 |
| `router` | 模型路由 | 7 | 自动注册、显示名、探测间隔、首字节/连接超时、熔断阈值/冷却 |
| `model` | 模型与引擎 | 9 | 只在「能力」页签加载失败时出现（回退显示，见 §5.3） |

### 4.3 二级小节（`sub`）：包含关系，不是并列关系

第一版把「提示音与通知」「朗读与反馈」「命令与会话」做成了**与「语音命令」平级**的分组，
用户当场指出这不对：它们本来就是语音命令这一个功能的组成部分，**应该是包含而不是并列**。
所以改成两级：一级分组（`grp`）+ 组内小节（`sub`），小节也能各自折叠。

「语音命令」下的四个小节（顺序 = 使用流程：怎么录 → 发到哪 → 播报什么 → 提示音）:

| 小节 `sub` | 标题 | 项数 | 内容 |
|---|---|---|---|
| `record` | 录音与转写 | 10 | 转写语言、媒体键触发、唤醒/回退热键、静音阈值、静音收尾、无语音放弃、最长录音、输入设备、拦截媒体键 |
| `command` | 命令与会话 | 6 | 命令会话工作区、空闲轮换、面板同步的目标工作区/会话、所在地、环境上下文 |
| `speech` | 朗读与反馈 | 7 | 语音合成引擎（唯一）、复述确认、语音简报、简报字数、极简回复三项 |
| `beep` | 提示音与通知 | 4 | 开始/停录/发送提示音、桌面通知 |

约定（都有测试拦着）：

* 小节是**展示层**的从属关系：后端只声明归属（`config.py` 的 `sub=`），名字与顺序在面板
  （`web/app.js` 的 `SET_SUB_ORDER` / `SET_SUB_NAMES`）；
* 小节名不许与任何一级分组同名（否则又变成并列）；一个小节只能属于一个分组；
* 声明了的小节必须有设置项用它（不留空标题），用了的小节必须有中文标题（"别用英文"）；
* 折叠状态两级各存一份（`echo.settings.collapsedGroups` / `...collapsedSubs`），
  顶部「全部折叠」两级一起收。

### 4.4 组内顺序：`order` 字段

`db.all_settings()` 按 `(grp, key)` 字母序返回（库层保持简单稳定），无法表达编排。
因此 `config.SETTING_ORDER`（= `DEFAULTS` 的声明顺序）随每行一起下发为 `order`，
面板按它排序 —— 想调整组内顺序，就调 `DEFAULTS` 里的声明顺序。

### 4.5 一条硬约束（已写成测试）

面板的「智能体」组整块由智能体表格渲染（`renderAgentTable`）——**任何可见设置项若 `grp="agent"`，
会在界面上被整块吞掉、彻底看不见**。`tests/test_settings_wiring.py` 现在会拦住这种写法。

> 另：改完 `web/app.js` 后**必须整页刷新**（F5）才生效 —— 面板是单页应用，
> 文件只在页面加载时取一次；只切页签的话，浏览器里跑的还是旧 JS，新旧元数据混用就会出现
> "分组标题变成英文键名"的现象（2026-09-19 用户正是这样看到 `beep/speech/command` 的）。

---

## 5. 怎么验证（不用眼睛看面板）

```powershell
# 0) 不开浏览器看菜单长什么样（同一份元数据 + 同一套排序/过滤规则）
.\venv\Scripts\python.exe scripts\preview-settings-menu.py            # 正常
.\venv\Scripts\python.exe scripts\preview-settings-menu.py --models-fail   # 模拟能力页签挂掉（那 9 项回退到设置页）
.\venv\Scripts\python.exe scripts\preview-settings-menu.py --out docs\settings-menu.txt

# 1) 清点 + 门禁：任何"没人读 / 只写没人读 / 未确认"的项都会让这一步失败
.\venv\Scripts\python.exe scripts\audit-settings.py --check

# 2) 接线回归（23 个用例）：分组合法性、候选项有实现、全部 86 项往返读写、弃用与迁移
.\venv\Scripts\python.exe -m unittest tests.test_settings_wiring -v

# 3) 五项门禁（编译 / 导入 / 平台契约 / 全量单测 / ruff）
powershell -ExecutionPolicy Bypass -File scripts\check-windows.ps1
```

当前渲染结果的快照：[`docs/settings-menu.txt`](settings-menu.txt)（61 个可见项 + 智能体表格 +
能力页签；页签可用时那 9 项不在表单里）。

`tests/test_settings_wiring.py` 覆盖的保证：

1. **每项都有人读**：审计判定必须落在 `OK` / 已确认的 `OK-INDIRECT` / 已确认的 `PANEL-READ`；
   确认名单本身也要"正好对上"（名单里的项若已被直接读，测试会要求删掉，防名单腐烂）。
2. **每项都能读回**：对全部 86 项（含隐藏项与密钥）走一遍 `PUT /api/settings` → 读回；
   密钥必须遮罩 + `hasValue`，空串不得清空，`__clear__` 才清空。
3. **分组不出错**：可见项的 `grp` 必须在面板分组表里有标题与位置；可见项不许落在 `agent` 组；
   「能力」页签承载的 9 项必须在 `model` 组（页签挂了才能整块回退；`ttsEngine` 例外，
   它仍属「语音命令 → 朗读与反馈」，只是编辑入口在能力页签）。
   二级小节另有一组检查：名字不许与任何一级分组同名（包含关系）、一个小节只能属于一个分组、
   声明了的小节必须有项用它、所有分组与小节标题必须含中文（"别用英文"）。
4. **候选项不是幻觉**：`wakeEngine` 的每个选项都必须有实现（`wake.ENGINE_LABELS`），
   `ttsEngine` 的每个选项都必须被 `tts.speak` 处理。
5. **弃用可控**：弃用项不出现在 `/api/settings`、写入被拒收、迁移规则只能指向"仍生效的项"，
   且老值确实搬到新开关上（`worklogMode=off`、`providerTts` 两个取值都有用例）。

---

## 6. 已知遗留 / 待办（不在本次范围）

| 项 | 说明 |
|---|---|
| `apiAuthEnabled` 的令牌体验 | 建议面板自动持有本机令牌（或在开启时自动生成并写入 localStorage），彻底消除"开完连不上"的坑 |
| `router*` 生效时机 | 部分项要重启模型路由进程才生效（描述里已注明），面板暂未做"重启路由"按钮 |
| `paths` 改动后的迁移 | 会议文件目录改了要手动点「迁移已有会议」；模型目录要自行拷贝 |
| 已弃用项的清理 | `dshStartCommand` / `dshNodePath` / `dshPackageDir` / `worklogMode` / `providerTts` 确认彻底无人引用后可从 `DEFAULTS` 删除 |
| 设置页长度 | 分组变多后默认全部展开，页较长；顶部已有「全部折叠/展开」，暂不改默认折叠策略 |

---

## 7. 回滚

* 分组：`config.py` 的 `grp` 值与 `web/app.js` 的 `SET_GROUP_ORDER/NAMES` 一起回退即可
  （分组只是展示元数据，服务启动时由 `seed_defaults()` 同步进库，无需手工改库）。
* 弃用：把 `deprecated=True` 去掉即恢复展示（`providerTts` 也需恢复 `speak_text` 里的分支）。
* 迁移：`DEPRECATION_MIGRATIONS` 是一次性动作，回滚配置代码不会把值搬回去；
  被搬动过的两项是 `worklogEnabled`（可能被置为关）与 `ttsEngine`（可能从 `auto` 变成
  `edge-tts`/离线引擎），都在 `app_logs` 里留了 `source=config` 的记录，可据此还原。
