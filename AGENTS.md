# AGENTS.md — 给 AI/协作者的长期约定与踩坑记录

## 必读：已知坑（务必牢记）

### **ECHO 不直接连 LLM：所有推理只发配置的智能体**（2026-10-22 用户定调，硬约束）

- **用户原话**："echo 不直接连 llm 完成动作，所有推理动作都要发配置的 agent 进行"；
  随后两次澄清：`echo-auto` **是注册到配置好的 agent 的**、**是给对应 agent 提供内外网模型服务路由的功能，不是 echo 自己用的**。
- **现行架构**：
  * 纪要、总结、推理、归档、语音指令 → 一律走 `app/agents` 里**当前选中的智能体**
    （`meeting._agent_ready_for_reasoning()` → `manager.agent_ready()`）。
  * 没有智能体就**明确失败**：`meeting.request_summary()` / `request_topic_segments()`
    返回 `False` 并写 error 日志；**不静默降级、不直连**。
  * `echo-auto` 仍然存在，但只活在 **智能体这一侧**：`app/llm_router.py` 把
    `llm-pi-ai.providers.echo-auto` 写进 DSH 家目录，`dsh-failover/` 提供路由；
    **不是 ECHO 的 provider，不在 `/api/providers` 里**。
- **已删除（别再复活）**：
  * `app/providers/openai.py` 的 `OpenAICompatLlmProvider`（`openai-llm`）；
  * `app/providers/router.py`（把 `echo-auto` 包装成 ECHO 的 LLM provider）；
  * `providers.KINDS` 里的 `"llm"`、`base.LlmProvider`；
  * `config.DEFAULTS` 的 `providerLlm` / `providerLlmBaseUrl` / `providerLlmApiKey` / `providerLlmModel`；
  * `meeting.direct_llm_decision()` / `_llm_provider_for_summary()` / `_provider_summary_text()` /
    `_spawn_provider_summary()`；
  * 向导 S6 的"直连兜底"折叠区与 UI 的「语言模型 provider」块（`rtLlmHost` / `loadRouterLlm`）。
- **判据**：
  * `tests/test_summary_agent_only.py`：没有 agent 时直接 False、有 agent 时只调 `_spawn_summary_waiter`；
  * `tests/test_failover_boot.py::ProviderBoundaryTests`：`P.specs("llm") == []`、catalog 无 `llm`；
  * `tests/test_providers.py`：只剩 ASR/TTS 两类；
  * `scripts/audit-settings.py --check` 不再出现 `providerLlm*`。
- **文档**：`docs/3.0-设计总览与组件关系.md` §"必须写清的降级边界"、
  `docs/向导-分步设计.md` §0.4/S6、`docs/设置项归属表.md` 已同步；
  `2.0-PROGRESS` / `3.0-PROGRESS` / `REFACTOR-PLAN` 顶部有"历史、已作废"提示。

### 跑全量门禁**之前先停本机后端**（2026-10-03 实测，三条红全是它引起的）

- **现场**：为了给用户试会议转写，我把本机能力后端起起来了（`POST /api/capability/backend/start`，
  pid 占着 8900 数据口 + 8901 admin 口）。接着跑全量门禁 → **`FAILED (failures=2)`**，而且日志里还有
  `ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8901)`：
  * `tests/server/*`（admin API 那批）要**真的绑 8901** → 被我那个后端占着，直接起不来；
  * `tests/test_backend_proc.py::IsolationTests::test_the_real_database_is_not_written`
    —— 它有"真库不许被写"的护栏，而后端跑着时会往真库/真目录写状态 → 红；
  * `tests/test_backend_fetch.py::EnsureRuntimeTests::test_it_never_raises_without_an_interpreter_or_an_unpacked_pack`
    —— 它假设"没有解释器/没有解开的包"。**这条的根因我一开始写错了**：不是"后端进程在跑"，
    而是①开发机上装好的 `data/backend/`（那份 runtime）**存在**就算，②**更要命的是设置
    `capabilityBackendPackage` 指着那个 3.1 GB 离线包**（2026-10-04 查实：单跑这条用例要
    **225 秒**——它真的去解包了）。**已经按纪律修掉**：用例在 `setUp` 里只屏蔽这一个键
    （`backend_fetch._setting` + `ECHO_BACKEND_PACKAGE`），修完 **1.2 秒**、连跑两轮门禁都绿。
    这条留给后人的教训：**用例必须屏蔽"用户显式指的路径"这类设置**，否则开发机的状态会漏进用例，
    表现还是"红的地方不是坏的地方"。
- **铁律**：**门禁要独占端口与数据目录** → 跑之前先
  `POST /api/capability/backend/stop`（或确认 8900/8901 都空），跑完再按需要起回来。
  这与"停/重启 ECHO 前先看 `meeting.active`"是同一条纪律的两个面：**别让真服务与用例抢同一份资源**。
  （至于上面第三条：停进程**不够**，那份装好的目录还在 —— 所以真正的解法是它自己的隔离，已做。）
- **判据**：`Get-NetTCPConnection -LocalPort 8900,8901 -State Listen` 全空；日志里不该再有 10048；
  以及 `python -m unittest tests.test_backend_fetch` 应当是**秒级**（要 200 多秒 = 又漏进真包了）。

### 后端归属：**每棵树只用自己的那一份，同机同一时间只跑一个**（2026-10-04 用户拍板）

- **用户的决定（原话）**："稳定版用稳定版的后端，dev 用 dev 的后端，这样不影响开发"，
  以及补充的 **"不用改端口，他俩不会同时启动的"**。
  ⚠️ 这条**推翻了**我 2026-10-03 写在这里的"共用一个后端、而且推荐"—— 那个说法**是错的**，
  别再照它做。**两棵树各有各的后端安装**（dev: `C:\echo-dev\data\backend`；
  稳定版: `D:\ECHO\backend`），**谁也不连谁的那份**。
- **为什么做不到"同时跑"（这是设计，不是 bug）**：`backend_setup.configure()` 每次启动都用
  **写死的 8900/8901** 重新生成 `server.yaml`（手工把 yaml 改成 8910 会被它**覆盖回去**，
  实测过）；`backend_proc` 的"端口被占"判据也比对这对端口，于是第二棵树会得到原话：
  *"端口被占：8900 被 …占着…**同机只跑一个后端，请先停掉它或换端口**（ECHO 不会替你停别人的进程）"*。
  **用户明确说不加端口设置**，所以这条现状要**照实描述**，不要再去"想办法共存"。
- **切换 = 停旧那份 → 起这棵树自己那份 → 重新配对**（**换后端 = 换 jwt_secret → 旧凭据立刻作废**，
  所以"重新配对"不是可选项）。
  * `capabilityEchoServerUrl` 是**隐藏设置**：dev 上 `PUT` 能改（200），**稳定版那台曾被 422 拒**
    —— 别再依赖它，**后端地址的真正来源是配对文件**（`<数据根>/data/backend.json` 的 `base_url`，
    由后端启动时写的 `state/local-pair.json` 换来）。"起自己的后端 + 本机配对"就够了。
  * 判据：`GET /api/capability` 的 `backends[]` 里 `echo-server` 的 `ready=true`（`false` 且
    `code=unauthorized` = 配对还指着**别人那份**后端，去点 `13/14-后端-切到*.cmd`）。
- **判"后端归谁"不许问实例**：切换时**旧实例已经先被停掉**，这时去问它的面板必然问不到，
  于是 8900 明明被占、脚本却以为"没人拥有" → 后端被孤儿化（2026-10-04 实测）。
  **唯一可靠判据 = 各棵树的 `<数据根>\data\logs\backend.pid` + 那个进程的命令行里
  `server.main … --config <该树的后端目录>`**（桌面助手的 `Get-BackendPid` 就是照这个写的）。
- 桌面工具包（`C:\Users\zhkq\Desktop\ECHO\`）已按这条实现：`5/6/7-切换*` 与
  `13/14-后端-切到*` 会**停旧那份、起新这棵自己的、并重新配对**；`1/2-启动*` **不抢**
  （没人拥有时才接管，否则只提示）；`15-后端-清掉孤儿` 按 pid 收掉实例已停却还占着端口的那份。


### 起 ECHO 的脚本一律**后台**跑，`-Supervise` 是常驻的（2026-10-03 用户提醒）

- **用户原话**："dev启动脚本要后台运行，不然会堵塞指令"。现场：我在前台调
  `scripts\start.ps1 -Background -Supervise` → 它拉起的是**常驻看门狗**，前台一直不返回，
  工具调用被打断两次；子进程还继承了 stdout，后台作业也迟迟不结束。
- **铁律**：
  * 只要"起来就行"：`powershell -File scripts\start.ps1 -Background`，**并且**把它放进后台作业
    （工具的 background 模式 / `Start-Process`），不要在前台等；
  * 要常驻看门狗（开机自启那一套）走 `.vbs` / Startup 快捷键那条路，**不要**在会话里
    前台跑 `-Supervise`；
  * 验证用**另一次调用**做（`Get-NetTCPConnection -LocalPort <port> -State Listen` +
    `GET /api/status`），别在同一条命令里睡等。
- **别用 `job_kill` 去停那个后台作业**：作业里挂着刚起来的 ECHO（stub+child 一对 = 一个实例，
  PID 会变：本次 stub 64220 → 实际在听的是 46776 `pythonw.exe`）。要停就走
  `POST /api/control/echo/stop` 或 `scripts\stop.ps1`。

### 「有解释器」不等于「运行时能用」+ Blackwell 要 cu128 + 交付形态 + 网络/编码/残包/安装器四条坑（2026-10-01 真机定，十条）

这几条都是 2026-10-01 在"本机跑后端"那条路上踩出来的，症状都很像"服务坏了/网不通/显卡不行"，根因都不在那些地方
（①–⑤是凌晨那一轮，⑥⑦⑧⑨是同一天接着挖的，⑩是深夜用户第二次真机实测撞出来的）：

- **① 判"运行时就绪"必须探依赖，不能只看 `runtime/python.exe` 在不在。**
  薄包（`ECHO-backend-portable-*.zip`）**只带解释器**，fastapi/uvicorn/torch 全靠"取运行时"装。
  旧判据 `backend_proc.python_exe()` 一看解释器在就记「运行时已在」→ **跳过装依赖** → 后端一起来就
  `ModuleNotFoundError: No module named 'fastapi'` 退出（面板只留一句"后端起来后立刻退出了"）。
  **判据只有一处**：`app/backend_env.py::check_server_deps()`（`SERVER_IMPORTS = ("fastapi","uvicorn")`
  + import 过；约 1 秒）；`runtime()` 的 `depsOk`/`usable`、`backend_fetch.ensure_runtime()` 第 ⓪ 步、
  `backend_env.plan()` 的 `implemented` 都走它。**依赖没装全时别去跑 `check_torch_abi`**
  —— 那会报"ABI 不符"，而真相是"依赖还没装"（说错原因会把人引去清 `runtime/` 重装）。
- **② Blackwell（RTX 50 系，`compute_cap ≥ 12.0`）必须 cu128。** cu126 的轮子里**没有 sm_120 的
  kernel：症状是**装得上、起得来、一跑模型就 CUDA 报错**（不是装不上，所以最容易被当成"服务问题"）。
  判据在 `backend_env.MIN_COMPUTE_CAP_FOR_CU128 = 12.0`（Hopper 9.0 仍走 cu126）。
  **加了新变体就必须登记 `VARIANT_MODELS`** —— 没登记时 `weights()` 的 `wanted` 是空表，
  面板显示"模型 0/0 就绪"（看着像齐了，其实那一档根本没在查权重）。
- **③ `web/app.js` 第 2 行是 `"use strict"`：未声明就赋值 = 当场抛 `ReferenceError`。**
  2026-10-01 的现场是 `_capBackendCache` 从没声明过，两个 `try` 把它吞成
  「读不到本机后端的状态」/「读不到这台机器的后端计划」——**功能没坏、显示坏了**，而且只有真点开
  那一页才看得见。新加面板状态变量**先 `let` 再赋值**；门禁
  `tests/test_capability_panel.py::PanelStateDeclarationTests` 会扫。
- **④ 交付形态 = 交付目录 + 一层日期，里面是一个脚本 + 1~3 个 zip**（用户 2026-10-01 定，两句话叠起来的）：
  *"客户端的包，后端薄包和安装脚本放到 delivery 目录"* + *"再 delivery 目录下加一层交付日期编码，区分不同版本"*。
  落点：`D:\ECHO-delivery\ECHO-delivery-<日期>-<时分>\{装我.cmd, ECHO-kit-*.zip,
  ECHO-backend-portable-*.zip[, ECHO-backend-offline-*.zip], 先读我.md, 清单.txt}` ——
  组包命令 `python scripts\build_delivery.py --out D:\ECHO-delivery`（`--out` 就是"建在交付目录下、自己带日期那一层"）。
  脚本**自己解 kit zip**（`tar -xf` 到 `.\echo-kit\`）—— 同事不用右键解压；后端两个 zip 靠
  `-BackendDir "%HERE%"` 传给 `install-all.ps1` 那个「后端选择」步骤（参数 `-Backend ask|pair|local|skip`）。
  * **出包顺序（改了 `scripts/` 下任何东西都走一遍）**：`build_kit.py` →（`--check` 绿）→ `build_delivery.py`。
    `scripts/build_delivery.py` **自己也在主包的白名单里**，所以改它同样会让 `dist/` 变 STALE（这一轮又踩了一次）。
  * **判据**：`tests/test_build_delivery.py`（那一层带日期 / 几版能并存 / 不动用户目录里别的东西 / 失败不留半个包 /
    **脚本里必须有 `ECHO-kit-*.zip` + `tar -xf`** —— 少了这两样就不是"双击就能装"）。
  * **坑（不报错的那种）**：`%~dp0` 结尾是反斜杠，直接塞进带引号的 PowerShell 参数会把引号
    **转义掉** —— 必须 `set "HERE=%HERE:~0,-1%"` 再去传（`KIT` 仍要留尾反斜杠，因为
    `if exist "%KIT%bundle\wheels"` 靠它拼）。**判据**：跑出来的 `-BackendDir` 长度必须等于目录长度。
  * **坑**：`edit` 工具**会抹掉 UTF-8 BOM**。`scripts/install-all.ps1` 含中文，抹掉之后
    WinPS 5.1 按 GBK 解析 → **66 个解析错**（`表达式或语句中包含意外的标记"}"`）。
    改完立刻验：`[System.Management.Automation.Language.Parser]::ParseFile()`，缺 BOM 就按字节
    补回 `EF BB BF`（别重新编码）。
- **⑤ 后端离线包 = 薄包 + 装好依赖的 `runtime/`（同一个 zip，文件名 `ECHO-backend-offline-*.zip`）。**
  **判据全在内容**：`app/backend_fetch.py::_has_installed_deps()` 认"`<顶层>/runtime/` 下的
  `site-packages` 里有 fastapi"，所以①薄包（只有解释器）不会被误认成离线包，②
  `build_backend_portable.py --runtime-from` 出的厚包（名字仍是 `*-portable-*`）**照样认** ——
  **名字只做排序提示**。有离线包时 `ensure_runtime()` 第 ① 步解开即用（**零 pip**）、
  `plan()['approxDownloadGB']` 是 `0`；没有才从国内源装（约 3 GB）。
  摆放位置：安装根、安装根同级的 `*交付*`/`*delivery*`，或脚本旁边（`-BackendDir`）。
- **⑥ 随包的那个 CPython **装不了依赖** —— pip 被 PEP 668 挡住 + pip 本身残缺（2026-10-01 实测两份包都有）。**
  薄包/`dist/delivery-clean` 里的 `runtime/` 是**搬过来的 uv 托管 CPython**：
  `Lib/EXTERNALLY-MANAGED` 原样带着（→ `This environment is externally managed`，pip **一律拒绝安装**），
  而 `pip/` 还少一整个子包（→ `No module named 'pip._internal.models'`）。两条报错**都不提"包没装"**，
  所以看着像权限/网络问题。
  * **修法（两侧都要）**：应用侧 `app/backend_fetch.py::ensure_pip()` 在**任何 pip 之前**跑 ——
    摘标记 + 必要时用 `ensurepip._bundled` 那两个 wheel `--force-reinstall` **离线**装回 pip；
    出包侧 `build_backend_portable.py` 在拷完 runtime 之后做同样两件事（**不过就响亮失败、不留半个包**），
    `verify()` 另加一条"包里不许再有 EXTERNALLY-MANAGED"。
  * **坑**：`python -m ensurepip --upgrade` **自己会失败**（它不删 site-packages 里那份残缺的 pip，
    导入仍可能落到残缺那份上）—— 必须自己去 `ensurepip/_bundled` 拿 wheel 并且 `--force-reinstall`。
  * **判据**：`tests/test_backend_fetch.py::PipSelfHealTests`、`tests/test_backend_pack.py::PipPrepTests`。
- **⑦ 大文件走这条网络会被"掐"而不是"慢" —— pip 不会续传，所以必须留一条 Range 续传的后路（2026-10-01 实测）。**
  * **症状**：`pip install torch` 在那台机器上**永远装不完**，报
    `[SSL] record layer failure` / schannel `SEC_E_DECRYPT_FAILURE`；
    而同一个文件用浏览器/curl 能下 —— 很容易被误判成"镜像没源"或"网速慢"
    （**我第一晚就是这么误判的**：只量了阿里云 190 KB/s 就下了结论）。
  * **真因**：链路上有 TLS 中间盒子（这台机器装着奇安信 agent），**大流量传到几十 MB 就被切断**；
    SJTU 实测 **5~8 MB/s** 但每几十 MB 断一次。**pip 不做 Range 续传** → 一断整份重来。
  * **铁律**：判"某个源能不能用"要**量吞吐**（`curl --max-time 30 -w %{size_download}`），
    不要只看索引页 200、也不要只量一个站。
  * **绕法（实测有效）**：`curl -C - --retry-all-errors --retry-delay 1` 循环续传 —— 21 轮把
    **2729.5 MB** 的 `torch-2.9.1+cu128` 下完（约 7 分钟）。
  * **国内源的真实格局**（别记成"国内没源"）：国内 PyPI 镜像又多又快（清华 **69.8 MB/s**），
    **但 Windows 上 PyPI 的 torch 是 CPU 版**（118 MB，CUDA 依赖全标 `platform_system=="Linux"`；
    `torchaudio` 只有 328 KB）→ 对 GPU 后端**没用**；带 CUDA 的 Windows 轮子国内只有 **SJTU**
    （标准 PEP503 索引，两个域名）与阿里云的**下载页**（不是索引）。
  * **产品现状（别把计划当已实现）**：`app/backend_fetch.py` 现在只有 `TORCH_INDEX_TMPLS` 按序试这条
    pip 路 —— **pip 不会续传**，所以在会掐大流量的链路上，"点一下自己装"仍然可能失败。
    **已实现的行之有效的答案是离线包**（`ECHO-backend-offline-*.zip`，见⑤）。
    * **待做**：把"自己下轮子（Range 续传）+ `pip --no-index`"接进 `ensure_runtime()` 的 torch 那一步
      —— 设计已经验过：`<exe> -c "import sys,sysconfig;…"` 拿目标解释器标签 → 按标签从索引挑最新轮子
      → `Range` 一块块续（单次上限 32 MB，断了就接）→ `pip install --no-index --no-deps` 本地装。
      出包脚本 `dist/_fetch_wheels.py` 就是这条路的**可用原型**（21 轮下完 2729.5 MB）。
- **⑧ 出包"按目录名递归剪"会剪出**残包**，而残包的症状会伪装成"显卡不能用"（2026-10-01 真机事故）。**
  * **现场**：用户装了离线包，后端起来了，但每次转写 `503 model_failed`；服务端说的是
    "模型 qwen3asr 要求 GPU，但**它依赖的运行时（torch）看不到可用的 CUDA**" —— **这是误诊**。
    拿那个运行时直接探，真相是
    `ImportError: cannot import name 'data' from partially initialized module 'torch.utils'`。
  * **根因**：`build_backend_portable.EXCLUDE_DIRS` 里有裸的 `"data"`，而 `_copy_tree()` **按目录名在
    任意深度剪** → 剪掉了 `torch/utils/data/`、`transformers/models/`、`funasr/models/`。
    `data`/`models`/`tests`/`docs`/`logs`/`dist` 这些名字在真实 Python 包里**遍地都是**，按名字剪必坏。
  * **铁律（两张表 + 真 import）**：
    ① 排除表分两张 —— `EXCLUDE_DIRS_SOURCE`（仓库树，**只在被拷那一层的根上**按名字剪）、
    `EXCLUDE_DIRS_TREE`（运行时，**只剪 `__pycache__`/`.git`**）；
    ② 出包自检必须**从打好的包里真 import**（`import_check()`：带了哪个查哪个，不过就响亮失败、不留半个包）
    —— "包里带了什么"看清单就行，"带的东西能不能用"只能真跑；
    ③ **"运行时就绪"也要含这一条**（`backend_env.check_packed_torch()` + `ensure_runtime` 第 ⓪ 步）：
    `site-packages/torch` 在 → `import torch, torch.utils.data, torchaudio` 必须过，
    否则残包会被判就绪、**永远不会重新解包**，用户无路可走（本轮是手工删 `runtime/` 才恢复的）。
    判据：`tests/test_backend_pack.py::CopyFilterTests`/`ImportCheckTests`、
    `tests/test_backend_env.py::PackedTorchTests`。
  * **判据（见到那句话该往哪查）**：服务端说"torch 看不到 CUDA"时，**先探 `import torch, torch.utils.data`**，
    别去查驱动；再看包里那几个目录在不在（`zipfile` 数条目，比"文件总数"更能看出来）。
  * **顺带一条交付形态的坑**：`search_dirs()` 必须把**交付目录里那层带日期的版本目录**也算上
    （`<交付目录>\ECHO-delivery-<日期>\<zip>`）—— 安装期靠 `-BackendDir` 找到，事后自己解包就找不到了。
- **⑨ `pip install -r <清单>` 会按 locale 解码，中文 Windows 上是 cp936 → 含中文的清单直接崩（2026-10-01 实测）。**
  * **症状**：`UnicodeDecodeError: 'gbk' codec can't decode byte 0xab in position 17` ——
    **不写文件名、不提编码**，看着像"包坏了/网不通"。
  * **真因**：`server/requirements.txt` 是「UTF-8 中文注释 + 没有 BOM、没有 PEP263 声明」，
    而 pip 的 `auto_decode()` 在两者都没有时按 `locale.getpreferredencoding()` 解码。
    `requirements-core.txt` 早就有声明，**只有 `server/` 这份漏了**（它在"后端运行时"那条路上，平时跑不到）。
  * **两侧都要修**：① 文件首行补 `# -*- coding: utf-8 -*-`（门禁
    `tests/test_install_entry.py::RequirementsFilesAreLocaleSafe` 扫全仓库）；
    ② **更重要的**：`app/backend_fetch.py::_pip_env()` 给所有 pip 子进程设 `PYTHONUTF8=1`
    + `PYTHONIOENCODING=utf-8` —— 它管的是"**别人手上那份已经发出去的旧包**"（实测带它读没有声明的旧清单也 exit 0）。

- **⑩ 安装器里"解包了"不等于"起来了"；而且会议转写**没有**"客户端本地引擎"这一档（2026-10-01 真机第二次）。**
  * **现场**：用户从新交付层重装、组件选 `-Engines sherpa`（面板上的"本地转写"）、后端选"本机自己跑"，
    装完立刻转写 → 会议 `error`，原话 **"没有可用的后端"**。取证：`D:\ECHO\backend` 里**只有解出来的包**，
    **没有 `server.yaml`、没有 `state/`、连 `backend.log` 都没有** → **从没启动过**。
  * **根因**：`scripts/install-all.ps1::Invoke-BackendStep` 的**离线包分支只解包 + 检查运行时可用**，
    **没有** `POST /api/capability/backend/start`；而**薄包分支有**。于是"有离线包"反而成了
    "装完不能用"的组合（上一轮没露馅，是因为用户自己点了一下"起后端"补上了）。
  * **铁律**：离线包 / 薄包**两条分支都要走同一个 `Start-LocalBackendNow`**（触发 + 轮询
    `GET /api/capability/backend` 到 `job.running` 为假；**必须有超时上限**，超时如实说"还在起"）。
    判据：`tests/test_install_entry.py::BackendChoiceTests`。
  * **概念澄清（别再被"本地转写"带偏）**：`capabilityMeetingAsrBackend` 只有 **`echo-server` / `asr-provider`**
    —— 2026-09-29 的决定是"**本机这一档退役**：会议转写不再由客户端进程内的引擎承担，要全本机跑就在本机
    起一个能力后端（同一个 `echo-server` 取值）"。组件那步的 `-Engines sherpa` 只服务**语音助手**那条路。
  * **通用判据**：界面上凡是"已就位/已启用"的措辞，背后必须有一个**真的可用**的判据
    （这次是"起过 + `ready` 三层全过"），否则用户拿到的是"看着装好了、一用就报没有后端"。

### 回环后端算「本机」，「不出机」许可放行它 —— 但地址分不出"本机服务"与"本机隧道"（2026-09-29 用户拍板）

- **判据**：`app/capabilities/echo_server.py` 的 `EchoServerClient.source` **按 `base_url` 算** ——
  `127.0.0.1` / `localhost` / `[::1]` / `127.0.0.0/8` → `SOURCE_LOCAL`（不算出网），其余 → `SOURCE_LAN`。
  它原来是与地址无关的**类属性** `SOURCE_LAN`，于是只绑回环的本机后端会被 `privacy=none`
  （「不出机」）判成 `blocked` —— 而「帮我起本机后端」起的正是那一档（`app/backend_setup.py`），
  等于让最在意隐私的人用不了自己刚装的后端，还逼他把许可放宽到内网（一放宽，**真正的内网后端
  也跟着被放行**）。**这条判据是用户拍板的，别改回类属性**；要模拟别的档请换 `base_url`
  （属性**故意没有 setter**，用例钉着）。
- ⚠️ **已知边界（有意的取舍）**：地址分不出"本机服务"与"本机隧道" ——
  把远端后端用 `ssh -L 18900:远端:8900` 映射到本机回环会被判成"没出机"。
  所以面板与向导里都写着"**用隧道请选内网许可**"（`web/app.js` 的一致性提示、`app/wizard.py` 末页）。
- **配套口径（别退回旧说法）**：`privacy=none` 时后端**连得上、只是不被允许**，
  会议侧记 **`blocked-by-privacy`**（不是 `waiting-backend`），面板说的是
  "被「允许音频去哪」挡住了 + 去改成内网"，**不是"连不上"**（说错原因会把人引去查网络）。
  判据在 `app/meeting.py::_policy_blocked_only()`。程序**不自动改**用户的 `capabilityPrivacy`。
- 由来与实证见 `docs/3.0-PROGRESS.md` §8.9；决定表在 `docs/3.0-设计总览与组件关系.md` §11.1 第 22 条。

### 开发/稳定版怎么走：dev 里开发 → 稳定后推 git → **按用户指令**才同步稳定版（2026-09-23 用户定的规矩）

- **两棵树是各自独立的代码副本**：`C:\echo-dev`（dev 环境 / 仓库）与 `D:\ECHO`（稳定版安装）。
  稳定版跑它自己的 `app/`、读自己的 `data/`；**改仓库不会自动影响稳定版**。
- 用户的规矩（原话："以后开发在 dev 环境，稳定后再推送 git，同时根据我的指令同步推稳定版"）：
  1. 开发只在 `C:\echo-dev` 里做；
  2. **稳定了才 `git push`**（不要为了"先存一下"就推半成品）；
  3. 同步到稳定版**只在用户明确说"同步 / 部署到稳定版"时**做 —— 不许当成"顺手修一下"的副作用。
- **同步稳定版只有一个入口**：`scripts/deploy-stable.ps1`。它会：
  ① 发现有会议正在录就**拒绝**（exit 2）；② 工作树脏就拒绝；③ 用 `build_kit.py` 出包
  （与用户拿到的是**同一个产物**）；④ 把 `<kit>\ECHO\*` 覆盖到 `D:\ECHO`
  （`data\` `models\` `runtime-core\` 不在包里，原样保留）＋ 把安装技能覆盖到
  `<DestDir>\.dsh\skills\echo-install\`；⑤ 覆盖后 `compileall` + import 冒烟；
  ⑥ **只有加 `-Restart` 才重启 ECHO**。`-DryRun` 只报计划。
  **流程：先 `-DryRun` 看计划 → 报给用户 → 用户点头 → 再真跑。**
- **第一次事故（2026-09-23）**：为了让修复"立刻生效"，我手工把 19 个文件拷进 `D:\ECHO`
  并重启，结果把用户**正在录的会议**打断了（ECHO 把它记成 `interrupted`，音频留着但录音断了）。
  两条铁律由此而来：
  * **不许手工往 `D:\ECHO` 拷文件** —— 要同步就走 `deploy-stable.ps1`；
  * **任何停/重启 ECHO 的动作之前，先看 `GET /api/status` 的 `meeting.active`**。
- **切换器不再住在仓库里**：`scripts/install-switcher.ps1` 把 `echo-supervisor.ps1` +
  `echo-instance-lib.ps1` + 启动 vbs 装到 `%USERPROFILE%\.echo-switch\`，并把开机自启动指过去。
  这样删掉/搬走 dev 树，稳定版照样开机自启。**别再用 `switch-instance.ps1 -InstallAutostart`**
  —— 它会把自启动指回仓库（这正是被替换掉的那个耦合）。
- 现状以 `scripts\switch-instance.ps1 -Status` 与 `GET /api/status` 为准，不要凭记忆。

### 设置缓存**不许"先挂空 dict、再逐键填"**（2026-09-29 全量门禁逮住的并发 bug，会表现成"改了设置不生效"）

- **症状**：`tests/test_settings_wiring.py::RoundTripTests::test_every_setting_round_trips` 在全量跑时红、
  **单跑却绿**；报的是"某**一个**键写进去没读回来"（实测 `routerConnectTimeout`：`1.5 != 1.75`），
  而 `PUT /api/settings` **返回 200**、**库里也确实是新值**。
- **根因**（`app/config.py` 的 `Settings._load()`）：它**先把 `self._cache = {}` 挂上去，再逐键去库里填** ——
  任何**别的线程**在这个窗口（一百多次开库、几十毫秒）里调 `settings.get(k)`，都会看到"填到一半"的缓存，
  `k` 还没轮到就落到兜底分支 → **返回静态默认值**。而**每次 `update()` 都会把缓存置空**，
  于是"改完设置、紧接着任何后台读"就会读到旧值 —— **生产表现就是「用户改了设置、面板说成功、别的组件仍按旧值跑」**。
- **为什么只在全量跑时出现**：单跑时那条用例独占进程，没有别的线程在同一窗口里读；
  全量里**前面某个用例留下的后台线程**（本轮是 `tests/test_runtime_hotkey.py` 漏掉的真实热键监听线程）
  正好在那几十毫秒里读了一次设置。→ 这是"跨用例污染"最典型的形状：**红的地方不是坏的地方**。
- **修法**（最小改动）：给 `Settings` 一把 `threading.RLock()`；`_load()` 的填充与 `_drop_cache()` 的失效**同锁**；
  `update()` 的"写库 + 置空缓存"放进**同一临界区**。
- **判据**（别用"重跑到绿"糊过去）：造一个后台读线程（照真实用法 `settings.get("wakePaused")` 同形）后跑那条用例 ——
  **修前 3 轮共 11 个键**出现"200 但读回旧值"，**修后 2 次 × 3 轮 = 0**。
- **铁律**：
  * 任何"整体重建的缓存"**必须原子地整体替换**（先在**局部**变量里建好完整 dict，最后**一次性**赋给 `self._cache`），
    或者整段放进锁里 —— **绝不要**先挂一个空/半成品再慢慢填；
  * 拼缓存时**不要**写 `if k not in cache: cache[k] = ...` 这种"就地补"，除非它在锁内且是唯一写者；
  * 这类 bug 的后果是**静默的**（接口 200、库里对、读的人拿到默认值），只有并发 + 全量跑才现形 ——
    所以**别把门禁红当噪声**：先问"是不是有别的线程在同一个窗口里读"。
- **同一轮还挖出一个测试卫生问题**：`tests/test_runtime_hotkey.py` 的两个用例调 `runtime.start_all()`
  起了**真的** `HotkeyListener`（win32 消息循环线程），而 cleanup 只把 `runtime._hotkey = None`
  —— **丢句柄、不停线程**✗ → 每个用例漏一个线程、一直活到整轮结束，还占着全局热键
  （日志里的"注册失败（可能被占用）"就是它们）。
  **铁律**：测试里"停一个监听器"必须是**真的停**（或一开始就把它打桩），
  **不许**用"把引用置空"冒充清理 —— 那既漏线程，又会污染后面的用例。

### 能力后端的接口契约与依赖口径（2026-09-29 真机实测，写脚本调它踩了三次才对齐）

自己写脚本 / 工具去调能力后端（`server/` 那套）时，**这三条契约必须照做**，否则会得到看似"服务坏了"的报错：

- **① `POST /v1/pair` 的字段名是 `clientName`**（不是 `name`，也没有 `clientVersion`）：
  `{"code": "<一次性码>", "clientName": "<名字>"}`。传错字段**不报错**，只会静默拿到空名字。
- **② `POST /v1/token` 走 HTTP Basic**，凭据是 `client_id:secret`：
  `Authorization: Basic base64("<clientId>:<secret>")`。**不要把 clientId/secret 放 JSON body** —— 那样会
  401「未授权」，很容易被误判成"配对失败"。响应里的令牌字段是 **`accessToken`**（不是 `token`），
  同响应还有 `expiresIn` / `scopes` / `clientId`。
- **③ `/v1/asr` 与 `/v1/diarize` 收的是"裸音频 body"**：`Content-Type: audio/wav` + 直接把 wav 字节当请求体。
  用 `multipart/form-data` 上传会得到 **HTTP 415 `unsupported_media`**
  （`detail: 不认识的 Content-Type: 'multipart/form-data'`）。令牌走 `Authorization: Bearer <accessToken>`。

**依赖口径（`torch` 与 `torchaudio`）**：判据是**两者的 CUDA 源标签一致**（都带 `+cu126`），**不是版本号相同** ——
PyTorch 2.9 之后 **torchaudio 停更**，版本号天然对不上（cu126 索引上实测可用组合：`torch 2.14.0+cu126` +
`torchaudio 2.11.0+cu126`）。真正的坑是**从普通 PyPI 拉来的无标签 torchaudio**（cu13x 那套，要 `libcudart.so.13`）
→ `_torchaudio.abi3.so` 加载失败 → **`import torchaudio` 崩 → funasr(SenseVoice) 的模型加载也失败 →
每个 `/v1/asr` 都 503 `model_failed`**（转写与分离一起被打死）。所以 `server/Dockerfile` 把两者写在
**同一条 pip、同一个 index-url** 里，并在**构建期**校验"CUDA 标签一致 + `import torchaudio` 通过" ——
这类坑必须在构建时炸，不要留到运行时表现为 503。

**顺带一个能力结论（同日真机验证）**：**RTX 2060 SUPER（Turing，cap 7.5）能跑说话人分离** ——
pyannote 4.0.7 在 cu126 上装得上也跑得动（600 秒音频约 26.7 秒，走 `/v1/diarize` 返回 200）。
"Turing 没有 bf16 所以只能转写"这个推断**对分离不成立**（分离不吃 bf16）。

### 远程部署的坑（SSH 喂脚本 / `docker build` / 管理员口令与 scopes）（2026-09-28 两台真机实测）

- **① PowerShell 往 ssh 的 stdin 喂多行脚本时，CR 只会落在最后一行。**
  * **症状**：最后一行恰好是一条关键命令时报**文件名里带 `\r`** 的诡异错误 ——
    `python3 x.py\r: No such file or directory`（看着像文件不存在，其实文件在）。
  * **根因**：PowerShell 的换行是 CRLF，而 `bash -s` 只把**最后一行**的那个 CR 留在了行内，
    于是 `\r` 成了文件名/参数的一部分。
  * **铁律**：脚本最后一行放一条**无害命令**（如 `echo ok`），或整体 `sed -i 's/\r$//'` 清一遍。
- **② 后台进程会吃掉脚本剩余的 stdin。**
  * **症状**：在 `bash -s` 里用 `nohup … &` 起了后台任务之后，**后续行被那个进程读走** ——
    表现为 `sed` / `tail` 报出莫名其妙的"文件不存在"，而脚本本身看不出错。
  * **根因**：后台进程继承了同一个 stdin（那个管道/heredoc），把剩下的脚本内容当自己的输入读了。
  * **铁律**：后台任务一律 `< /dev/null`，并 `setsid` 脱离会话 —— 否则远端 SSH 一断，
    `docker build` 会被取消（报 `context canceled`）。
- **③ `docker build` 必须在服务端脱离会话跑。**
  * **症状**：SSH 一断，构建就是 `context canceled`。
  * **铁律**：`setsid nohup docker build … < /dev/null > build.log 2>&1 &`，
    另开会话 `tail -f build.log`（与②是同一条纪律的两个面：stdin 要断、会话要脱）。
- **④ `cfg.state_root` 是属性，不是方法。**
  * **症状**：写成 `cfg.state_root()` 报 `'str' object is not callable`。
  * **根因**：它是**已经算好的字符串属性**（属性名不带括号）。
  * **铁律**：写 `cfg.state_root`；不确定就先看定义或 `type()`，别凭手感加括号。
- **⑤ 容器根只读时 `docker cp` 必失败。**
  * **症状**：`docker cp` 报 `rootfs is marked read-only`。
  * **根因**：`read_only: true` 下容器根不可写，cp 的落点写不进去（与"根只读"是同一个约束）。
  * **铁律**：要把文件送进容器，走**可写的 tmp 卷**（`/var/echo/tmp`）或打一个薄镜像层。
- **⑥ `unzip` 不保证存在。**
  * **症状**：单位那台 Ubuntu 上 `unzip` 直接 `command not found`。
  * **铁律**：改用 `python3 -c 'import zipfile;zipfile.ZipFile("x.zip").extractall(".")'`。
- **⑦ CLI 改管理员口令后，运行中的服务不一定立刻认（只记录观察到的事实，根因**还没**定位）。**
  * **观察**：重置口令后**立刻登录仍 401**；**重启容器也仍 401**；
    但**容器内直接打登录接口是 200**。
  * **不要下结论**（说"缓存"或别的都还没有证据）—— 谁定位到根因谁来补这一条。
  * **2026-10-05 补了一半（本机实测，非容器）**：那条 401 有一条**很容易被当成"口令不对"的**
    机制 —— 管理面登录带**失败退避**：`server/admin.py::_Throttle(max_failures=5, window_s=300)`
    （**5 分钟内错 5 次锁 5 分钟**，锁着时一律回同一句"用户名或口令不对"，
    分不出"口令错"与"被限流"）。实测把这条踩实了：先写错字段名（是 `username`，不是 `name`）
    连试多次 → 之后**带着正确口令也全是 401**，**重启后端也没用**。
    * ❌ **但别把那些 401 全归给限流**：我等到窗口自然过期之后用 curl 再试**仍然 401**，
      而同一个人**在浏览器里能正常登录** —— 差别在管理面对**写请求**的三样要求
      （`server/admin.py` 开头写明：CSRF 双提交 + 非简单请求标志头 + 同站 Origin）；
      我的 curl 只带了 `Origin`/`Referer`，**没带标志头、也没走取 CSRF 的第一步**。
      **教训**：拿脚本调管理面的**写**接口，要先把 CSRF 那一步走完；
      **别拿自己的 401 去推断服务端行为**（这正是本条当初"根因没定位"的成因）。
    * **判据（分清"口令错"与"被限流"）**：绕开 HTTP 直接验哈希 ——
      `server.admin.verify_password("口令", 库里的 password_hash)`；
      表是 **`admin_users(username, password_hash, disabled, created_at, last_login)`**
      （库：`<后端目录>/state/echo-server-auth.db`）。这条返回 True 就说明**库没问题**，
      那只剩限流/字段名两种可能。
    * **顺带**：`--set-password` 从 **stdin** 读口令（刻意没有 `--password` 参数，防泄漏），
      所以非交互设置要用管道：`"口令" | <后端 runtime>\python.exe -m server.main --config server.yaml --set-password <名字>`。
  * **跨进程验证登录时要注意的坑**：curl 不带**同站 Origin** 等前置时，请求会在"登录"之前
    就被同站校验挡掉，那个 401 与"口令不对"不是一回事，别把两者当同一个结论。
- **⑧ 管理员口令 / Client scopes 的实操要点。**
  * **症状**：给同事发配对码时 scopes 少写一项（例如漏 `diarize`）→ 客户端那个槽
    `forbidden` → **回落本机**跑 —— 用户看到"有分离结果"，其实是**本机算的**
    （后端根本没参与，最容易误判成"后端能用"）。
  * **根因**：服务端按 scopes 授权，缺一项就拒那一档；客户端把它当"这档不可用"并回落本机。
  * **铁律**：发码时按用途**写全** scopes；改 scopes 的写法是
    `--set-scopes <CLIENT> --scopes a,b`（scopes 走**独立**的 `--scopes`，别和别的项混写）。

### PowerShell 5.1 的 Get-Content/Set-Content 往返会**毁掉 UTF-8 中文文件**（2026-09-26 事故）

- **症状**：`(Get-Content x -Raw) -replace ... | Set-Content x -Encoding UTF8` 之后，文件里每个汉字都变成
  `鎺у埗闈㈡澘` 这种**双编码乱码**（还平白多出 BOM）。当时一口气毁掉了 `web/app.js`
  （5308 行 / 28 万字节）——它**没有干净副本**（工作树里还压着上一个 agent 未提交的改动，
  git 里只有更旧的版本）。
- **真因**：`pwsh` 工具在这台机器上跑的是 **Windows PowerShell 5.1**，`Get-Content`/`Set-Content`
  默认按 **ANSI（cp936）**读写；UTF-8 文件先被按 GBK 解码、再按 UTF-8 编码写回 = 双编码。
  ASCII 一个字节没变，所以"看着还能跑"，但中文全废、`git diff` 也读不出人话。
- **铁律**：**改仓库里的文本文件只用 `edit`/`write` 工具或 Python（显式 `encoding="utf-8"`）**；
  绝不用 PowerShell 的 `Get-Content | Set-Content` 做"读—改—写"。要批量替换就写一小段 Python。
  含中文的 `.ps1` 必须保 BOM 或改成 ASCII 注释（5.1 按 ANSI 解析会直接语法报错，
  本轮也踩了：`dist/_shot.ps1` 里一句中文注释就让整个脚本解析失败）。
- **万一还是毁了**：DSH 会话记录 `~/.dsh/sessions/<cwd 名>/<session-id>/session.v4.jsonl.zstd`
  里有**每一次 edit 的 old_string/new_string**（用 `zstandard` 解压、按行 `json.loads`，
  参数可能是 JSON 字符串、要再解一层）。救援配方：`git show HEAD:<file>` 打底 → 按时间顺序重放
  各会话的 edit → 判据用**纯 ASCII 行逐行比对**（坏文件只坏了非 ASCII，纯 ASCII 行必须一字不差；
  本轮 3561 行全中，据此确认救回的就是原文件）。

### 重启/杀掉 ECHO 之前必须先看有没有正在录音的会议（2026-09-23 事故）

- **事故**：为了改代码重启 ECHO，把用户**正在录的会议**打断了。ECHO 自己处理得很诚实 ——
  起来时记 `[meeting] 检测到中断的会议（正在录音但进程已退出）：已标为 interrupted: <名字>`，
  音频文件留在 `data/meetings/<名字>/` 里（可以重新转写），但这场录音**当场就断了**，
  用户白录了一段。
- **铁律**：任何会停/重启 ECHO 的操作（`stop.ps1`、杀 `app.main`、换包、换实例）之前，
  先看 `GET /api/status` 的 `meeting.active`（或 `data/echo.pid` + 会议目录里正在增长的 `*.wav`）。
  **`meeting.active == true` 就不要动** —— 先问用户，或等他录完。
  顺带：刷新前端、改设置、跑单测都不需要重启；`PUT /api/settings` 是热生效的。
- 附带结论：会议录音用的是**前台**麦克风租约（`recorder.py` 里 `input_stream(device_id)`
  没有 `background=True`），所以**录音期间下语音指令会被拒**（"麦克风正在录音或测试"）。
  想让"会议用全向麦 + 指令用耳机"真正并行，得让不同设备各自持有流，这是待办。

### 麦克风打不开 / 会议无法录音 / 进程 CPU 飙高 —— sd.default.device 下标取反 + CoreAudio HAL 死锁

- **症状**：面板显示 `无法开始录音：打开麦克风超时（设备被占用或权限不足）`；`POST /api/control/mic/test` 挂住无响应；运行中的 `python mac/run_mac.py` 进程 CPU 长时间 >400%（线程空转）。
- **根因 1（代码 bug）**：`sd.default.device` 是 `(输入, 输出)` 二元组。早期 `app/audio/recorder.py` 误把 `[1]`（输出设备）当默认输入、把 `[0]`（真正的默认输入）当输出排除掉，于是跳过内置麦克风、去打开 `OrayVirtualAudioDevice` / `"iPhone"的麦克风` 这类虚拟/接力设备。
- **根因 2（系统层）**：在 macOS 上打开这些虚拟/接力设备可能把 `CoreAudio` HAL 锁死。`sample <pid>` 可见 `HALB_Mutex::Lock()` 卡在 `HAL_CreateIOProcID` / `AudioDeviceStop`，此时**任何**进程内麦克风操作都会永久超时，无法自愈。
- **修复**：默认输入用 `device=None` 交给 PortAudio 选（见 `app/audio/wake.py:232` 同样结论）；`recorder.default_input_device()` 返回 `sd.default.device[0]`。已改于 `app/audio/recorder.py`（2026-09-18）。
- **恢复**：只能重启 ECHO（`bash mac/restart_mac.sh`）清掉死锁；重启会中断进行中的转写。
- **排查命令**：
  - `curl -s -X POST http://127.0.0.1:8970/api/control/mic/test`（无响应=卡死）
  - `sample <pid> 3 -file /tmp/sample.txt` 后搜 `HALB_Mutex` / `CreateIOProcID`
- **铁律**：任何地方都**不要**用 `sd.default.device[下标]` 硬取输入设备；需要默认输入就传 `device=None`。

### 启动后多出一个空的终端窗口 —— venv 的 pythonw 把控制台"漏"给了 Windows Terminal（2026-09-23）

- **症状**：ECHO 启动后屏幕上多一个空的终端窗口（标题就是 `…\Scripts\pythonw.exe`），一直不走。用户报的原话是"启动后有个 powershell 空窗"。
- **根因**：运行时是 **uv 建的 venv**，它的 `Scripts\pythonw.exe` 是 **trampoline** —— 会再 re-exec 一个**控制台子系统**的 `python.exe`。`Start-Process` **没有** `CreateNoWindow` 开关，而 `-WindowStyle Hidden` 只管住第一个进程（SW_HIDE）；可见的那个控制台是**孙子进程**自己开的，于是被系统默认终端应用（Windows Terminal）接管成可见标签页。实测：`pythonw.exe -m app.main` 与 `WindowsTerminal.exe -Embedding` 在**同一秒**出现。
- **判据（别再用"新窗口句柄"或 conhost 判断，都会漏）**：WT 把新控制台开成**标签页**，窗口句柄不变。可靠判据是枚举窗口类 `PseudoConsoleWindow` 的**拥有者进程**：被 WT 托管（=可见）的控制台，其进程必有一个；隐藏的 watchdog / 路由进程都没有。对照实验也证明 `Start-Process`、加 `-WindowStyle Hidden`、裸 `Start-Process` 三种写法都不影响这个判据。
- **修复**：`scripts/echo-launch-lib.ps1` 的 `Start-EchoProcess` —— 走 .NET `ProcessStartInfo.CreateNoWindow = $true`（= `CREATE_NO_WINDOW`），trampoline 拿到的是"没有窗口的控制台"，后代全部继承。**为什么用 `cmd /c` 而不是 `RedirectStandardOutput`**：.NET 只能重定向到**管道**，而管道需要活着的读端；`start.ps1 -Background` 启动完就退出，读端一死 ECHO 迟早会写满管道卡住。`cmd /c` 直接重定向到文件，字节不变。
- **铁律**：`scripts/start.ps1` / `startup.ps1` / `launch-desktop.ps1` **一律用 `Start-EchoProcess` 起 ECHO**，不要再写 `Start-Process -FilePath $pyw`。
- **顺带一个编码坑**：这三个脚本是 **ASCII-only（无 BOM）**，改它们时注释也只能用 ASCII；`launch-desktop.ps1` 是例外（**带 BOM**、里面有中文）。**任何用 Python/编辑器整体改写这些文件的操作都必须保住 BOM** —— 本次就踩了：抹掉 `launch-desktop.ps1` 的 BOM 之后，PowerShell 5.1 按 ANSI 读中文注释，直接**解析失败**（用 5.1 的 `Parser.ParseFile` 复核能同样报出来）。

### 发 Release 绕 GitHub DNS 污染：uploads 的 IP 只能靠"带 token 的探针"选（2026-09-23）

- 这台网络把 `*.github.com` 全解析成 `127.0.0.1`。`gh-push.ps1` 管的是 **git push**；
  **`gh`（建 Release、传附件）要自己起代理**：`scripts/gh-proxy.py <port> host=ip ...`，
  再让 `gh` 走它（`$env:HTTPS_PROXY='http://127.0.0.1:<port>'`）。
- **api 与 uploads 不是同一台前置**，所以必须按 host 分别钉 IP（这正是 `gh-proxy.py` 支持
  `host=ip` 写法的原因）。`gh release upload` 报 `HTTP 400 / 404 Bad request` 基本都是
  **uploads 指错了 IP**，不是权限或 release id 的问题。
- **别用"未授权探针"挑 IP —— 会把你骗反**（这次连骗两轮）：
  - 不带 token 去 POST 上传端点时，**正确的主机**可能回 `400`（请求体不合规），
    而**错误的主机**反而回 `403`。看着"403 更像个真端点"，结论正好是反的。
  - 可靠判据：**带 token 逐个 IP 真上传一次**，谁回 **201** 谁对（探针资产随后删掉）。
    2026-09-23 实测：uploads 只有 `20.205.243.161` 给 201，`.165` 给 403、`140.82.112.6` 给 404。
  - api 用 `GET /rate_limit` 判：`200`/`401` = 对；`301` = 那台不是 api 主机（`/` 的状态码没这分辨力）。
- **IP 会漂，别记死**：同一天里 api 的 DoH 结果 `20.205.243.168` 先能用、一小时后连不上，
  换 `140.82.112.6` 才通。**发版前现探现用**。
- **`gh-proxy.py` 开了 `SO_REUSEADDR`**：旧代理没杀干净也能再 bind 同一端口，于是"两个代理
  抢端口"很难察觉（表现是同一个 host 一会儿通一会儿不通）。换 IP 前先按命令行把
  `gh-proxy.py` 进程**全部**杀干净。
- 中文标题/正文仍然走 `gh api --input <utf8-json>`（PowerShell 直接传参会乱码）。

### 跑单测会把开发机上正在跑的「标准版 harness」杀掉（2026-09-22 事故）

- **症状**：面板上 `harness`（标准版）显示 `offline / 已停止`，会议纪要生成报
  `harness 登录失败：<urlopen error [WinError 10061] 目标计算机积极拒绝>`（43199 没在监听）。
- **根因**：`tests/test_harness_agent.py::HarnessBrowserOpenTests::test_online_without_token_still_opens`
  调的是**真实**的 `harness_proc.ensure_token()`。而 `harness_proc._pid_path()` 走
  `paths.data_root()`，**不受**测试里 patch 的 `db.DATA_DIR` 约束 —— 于是它读到真实的
  `data/logs/harness.pid`，认定"这是 ECHO 起的"，`stop()` 就把正在跑的那个**杀掉**；
  紧接着的 `ensure_running()` 又因为测试把 `online`/`token` 换成了替身而永远起不来。
  表现就是"服务自己停了"，而当时四个停止出口都只写一句"已停止"，查不出是谁。
- **修复**：`tests/test_harness_agent.py` 在 `setUpModule()` 里把 `_pid_path` 指到临时目录
  （顺带把 `db.DATA_DIR/DB_FILE` 也隔离，免得把"已停止"写进真实库），并加了护栏用例
  `test_harness_pid_file_is_isolated_from_the_real_one`；`harness_proc.stop(reason=…)` 现在把
  **谁/为什么**写进组件状态与日志（`已停止标准版 harness（原因）pid=…`），下次一眼可查。
- **铁律**：测试**不许**直接碰真实 harness 的 pid 文件/端口状态；凡是会走到
  `harness_proc.stop()` / `ensure_running()` 的测试，要么打桩，要么先做上面那种隔离。
- **同类第二处（2026-09-22 复查发现，已修）**：`tests/test_settings_wiring.py` 的
  `_quiet_side_effects()` 只哑掉了 wake 与 router，**漏了智能体联动** —— 它的
  "全部设置项往返"用例会把 `agentBackend` 探成别的值，于是真实走到
  `harness_proc.stop()`，照样能把正在跑的 harness 杀掉。现在那里把
  `settings_effects._agent` 整体换成替身；`tests/test_providers.py` 的整批 PUT 也用
  `settings_effects.apply → []` 哑掉。**凡是走 `PUT /api/settings` 的测试**都要检查这一点。
- **同类第三处：真 token 文件被单测删掉（2026-09-22 晚，已修）**。症状是"ECHO 重启后
  面板显示 `token 未获取`"，点「用浏览器打开标准版」还会为了换 token 把 harness 重启
  一遍（这台机器冷启动两分钟）。根因：`HarnessProcTests` 的两个 stop() 用例只把
  `_clear_pid` 打了桩，`stop()` 末尾的 `forget_token()` 照样删**真实**的
  `data/logs/harness-token.txt`（实测：跑完那个测试类文件就没了）。修法两条：
  ① `harness_proc` 的 token 路径改成可打桩的 `_token_path()`，`test_harness_agent` 在
  `setUpModule` 里连同 pid 文件一起指到临时目录，并加护栏
  `test_harness_token_file_is_isolated_from_the_real_one`；② token 落点从 `data/logs/`
  挪到 **`{DATA}/harness-token.txt`**（与 `echo.pid` 同级；老位置仍会被读一次并自动迁移）。
  顺带加固：读取用 `utf-8-sig`，免得 PowerShell `Set-Content -Encoding UTF8` 写进去的
  BOM 被当成 token 的一部分（报错是一句莫名其妙的 `ascii codec can't encode '\ufeff'`）。
- **铁律（补）**：`harness_proc` 里凡是走 `paths.data_root()` 的落盘状态（pid / token），
  测试都必须先隔离路径；"只 patch 了 `_clear_pid`"不等于拦住了 `forget_token()`。

### 标准版 harness 的「本地永久安装」在 registry 上装不下来（2026-09-22 实测）

- **症状**：照 `echo-install` 技能的命令 `npm install @deepseek-ai/dsh@0.1.5-rc.2` 报
  `ETARGET No matching version found for @deepseek-ai/dsh-client-ui-sidebar-documentpreview@^0.1.5-rc.3`。
  技能会**静默回退**到 `npx -y @deepseek-ai/dsh web`，于是每次冷启动 ~2 分钟（本地入口实测 9 秒）。
- **根因**：rc.2 的依赖图里有个子包被写成 `^0.1.5-rc.3`（预发布号按 semver 不向下兼容 rc.2），
  而那个子包的 rc.3 **从没发布过**（registry 上最高 rc.2）→ 这条安装路径在公共 registry 上是坏的，
  与本机环境无关。新机器照技能装都会失败。
- **现在的正解**（2026-09-22 晚已并进技能）：两个平台的组件安装器都改走同一个助手
  `.dsh/skills/echo-install/scripts/harness-install-local.{ps1,sh}`，它按三条路依次试：
  ① 已装好直接用；② `npm install @deepseek-ai/dsh@<版本>`；③ **从 npx 缓存复制**同版本那份整树
  （`npm config get cache` 的 `_npx/*/node_modules`；找不到同版本就用最新的并明确告警版本不同）。
  装完跑同一套完整性自检（`lib/bin.js` 非空 + 每个 `node-pty` 有 `package.json` 与 `lib/index.js`）。
  可一行参数换版本 / 离线只走缓存：`-DshVersion` / `--dsh-version`、`-FromCache` / `--from-cache`；
  修装坏的标准版可以直接单跑这个助手。契约由 `tests/test_install_entry.py::HarnessLocalInstallTests` 钉住。
  本机实测：从缓存复制 222.9 MB 用时约 55 秒（Windows）/ 78 秒（Git Bash 冒烟），冷启动 9.2 秒。
- **落点与设置**：`harnessCommand` 是**隐藏设置**（不经 `/api/settings` 下发，改在智能体展开区）。
  写成 `"<node 全路径>" "<...>\lib\bin.js" web` 会把 node 路径**钉死**（node 换版本后要手改）；
  留空则 ECHO 自己解析 —— 出厂值 + 本地入口存在时自动优先本地（`harness_proc.command()`），
  node 走托管目录探测，换版本也不用改配置。

### 只装了一个 DSH（桌面版或标准版）时 ECHO AUTO 注册不到 / 令牌读成空串（2026-09-22 同事反馈）

- **症状**：标准版（或桌面版）里选不到「ECHO AUTO」；或者选了却一直 `401 invalid
  router token`；面板「模型路由」里的成员提示"缺凭据"（内网网关令牌明明填过）。
- **根因**：ECHO 把三件事都写死成**桌面版那一份家目录**（`DSH_HOME` 或用户家目录下的
  .dsh）：① `boot._agent_dsh_available()` 只看桌面版适配器，只装标准版时直接跳过注册；
  ② `llm_router` 的家目录只有一个；③ 路由进程 `dsh-failover/proxy.py` 的 `CRED_YAML`
  也写死桌面版。而标准版的家目录是**独立的**（`harnessHome`，缺省 `{DATA}/harness`），
  向导默认推荐的偏偏就是标准版。
- **修复**：`llm_router.dsh_homes()` 列出**实际存在**的家目录（判据：里面有
  `settings.yaml`），注册与令牌都按它走，令牌在各家目录保持同一个值；
  boot 的闸改成"桌面版或标准版任一可用"；`homes.json` + `ECHO_DSH_HOMES` 让路由进程
  按同一份清单找成员密钥（热读，家目录后出现也不用重启）。四种装法（只有桌面版 /
  只有标准版 / 两个都有 / 两个都没有）都有用例：`tests/test_dsh_multi_home.py`。
- **铁律**：**不许**再把 DSH 家目录写死成 `~/.dsh`。新代码要家目录就调
  `llm_router.dsh_homes()`（app/ 里还有"写死 .dsh 的文件白名单 + 总数上限"的测试
  `tests/test_dsh_home_coupling.py` 盯着，加一处就红）。替没装的 DSH 造目录同样是错的
  （D25）：家目录不存在就跳过，并如实回报"没找到 DSH 家目录"。

### 交付包（dist/）只能由 `scripts/build_kit.py` 出，不要手工组 kit（2026-09-22）

- **事故**：同事要测安装，`dist/` 里的 kit 比源码旧了整整 9 小时 —— 当天 13:30–15:00 的修复
  （含「标准版本地永久安装」）**一个都没进包**，拿旧包测等于测不到新东西。根因是组 kit 一直是
  **手工活**：`scripts/` 里没有对应脚本，靠人记步骤，还要从 `dist/` 里翻上一代 kit 去捡
  `先读我.md`（而 `dist/` 不进 git，随时会被清空）。手工活必然漂移。
- **现在的正解**：`python scripts/build_kit.py` 一条命令出全部 —— 两个平台的主包 + 两个 kit + 自检。
  `--check` 只报告 `dist/` 与当前源码是否一致（退出码 2=过期 / 3=还没出过包）；`--kits-only` 复用
  已有主包只重组 kit。`先读我.md` 模板**在 git 里**（`delivery/`，刻意不在打包白名单内，
  所以不会被塞进主包）。
- **推送前的闸**：`scripts/gh-push.ps1` 在 Windows gate 之后跑一次 `--check`，过期就**黄字警告**。
  刻意**不拦推送** —— `dist/` 不在 git 里，不该让一个本地产物挡住代码。
- **两个平台必须同一个 profile**：都用 `-Profile main`。mac 曾经用 `public`：包里**没有**
  `components/`，而 `manifest.json` 照样声明了 `components/offline-pack.json` —— 交付清单里的
  假话，而 manifest 正是安装流程用来判断「这是已解开的包」的那个文件。`build-package.ps1` 现在
  只声明**真的进了包**的那些，`build_kit.py` 也把它列成自检项。
- **踩坑一：`edit` 工具会抹掉 UTF-8 BOM。** `scripts/build-package.ps1` 含中文注释，靠 BOM 让
  PowerShell 5.1 正确按 UTF-8 读；一次 `edit` 之后 BOM 没了 → 5.1 按 GBK 解析 → 直接**解析失败**
  （报 `表达式或语句中包含意外的标记"}"`）。改过含中文的 `.ps1` 之后务必确认 BOM 还在；
  `tests/test_script_encoding.py` 会挡住，别绕过它。
- **踩坑二：kit 的 zip 条目必须带 `<kit 名>/` 顶层前缀。** 少了它，解包出来是一堆散文件而不是
  「一个文件夹」，而 `先读我.md` 恰恰让同事「把这个文件夹整个交给助手」。**这个坑不报错**。
- **铁律**：要出包就走 `scripts/build_kit.py`；**不要把手工步骤写回文档当正解**。
  契约由 `tests/test_build_kit.py` 钉住（模板在 git、两边都走 main、kit 前缀、推送挂检查）。

## 会议"手机当麦克风" + 录音静音 + 事件下行（2026-10-15 夜，用户口径与实现）

用户这一天前后给了三条口径，都写进代码注释了，这里留一份索引：

1. **"手机发起的指令，手机收，电脑是配合执行和存储"** → 会议录音的采集源 = 发起设备：
   `meeting.start_meeting_phone()` 不建 `MeetingRecorder`（不开电脑的声卡），音频由手机按段
   `POST /api/meeting/segment` 传上来（`append_meeting_segment` 落成 `NN.wav`）。
   下游（转写/压缩/纪要/议题）一条都不用改。
   ⚠️ **本机麦那场拒绝外来段**（两路音频混进同一场 = 转写里两个人同时说话）。
   判据：`tests/test_meeting_phone_mic.py`。
2. **全屏录音界面**（用户要"漂亮的电平 + 结束 + 静音/恢复"）：`ui/MeetingRecordingScreen.kt`
   + 电脑侧 `POST /api/meeting/pause|resume`。暂停区间记进那场会议 `meta["pauses"]`
   （`from/to/by`），**暂停时立刻落盘**（崩了也留得下"这段时间被暂停过"）。判据：
   `tests/test_meeting_pause.py`、安卓 `RecordingUiTest`。
   ⚠️ **这一条里的"暂停语义"已在 2026-10-16 深夜被用户推翻** ✗ —— 现行口径见本文
   下面「暂停语义（2026-10-16 深夜，用户推翻 2026-10-15 的"不写盘"）」那一节，
   **别照上面这段的旧描述去做**（旧描述是"不写盘、不产生音频"）。
3. **"app 是 echo 的前端触点，不必所有功能都实现；设置只管 app 自己的选项与权限"** →
   安卓设置页重排为 连接/设备/按键与快捷方式/语音/通知/权限与省电/关于，**ECHO 侧的配置一律
   不进去**（同一个键只能一处能改）。设备（输入麦）用 `AppPrefs` + `InputDevices` 真生效
   （`AudioRecord.setPreferredDevice`；选的那支不在了**如实回落**）。
   服务状态回到首页那张小卡片（状态灯 + 点开看 9 个组件）。

### 暂停语义（2026-10-16 深夜，用户推翻 2026-10-15 的"不写盘"）

- **旧口径已作废** ✗：2026-10-15 写的是"**暂停 = 不写盘、不产生音频**"（当时的理由是"写静音帧
  会让转写里出现'这段没人说话'的假象"）。用户 2026-10-16 深夜**推翻了它**。
- **用户原话**：*"**暂停区间就是空白录音，不影响会议总时长，不用剃掉**"*；追问"手机与电脑本机麦
  要不要统一"时选了 **B：统一**。
- **新口径（现行，别再按旧的做）** ✓：暂停 = **照常录音，但写进去的是全零（空白）音频**。
  * 采样率 / 声道数 / 位深 / **每缓冲帧数**与暂停前**完全一致**，只是内容全零
    （`app/audio/recorder.py::MeetingRecorder._loop`：把 `stream.read()` 拿到的真数据换成
    `np.zeros(len(data), dtype=data.dtype)`，走的还是**同一行写盘**）；
  * **时间轴逐字连续**：一场 40 分钟、其中暂停 5 分钟 → 音频总长**仍是 40 分钟**，
    第 N 段覆盖的仍是挂钟的 `10N ~ 10N+10` 分钟（不再漂）。
  * **为什么必须逐字连续**（这条最容易改错）：段是按"写进去的采样数"切分的；暂停要是不计数，
    段边界会**整体前移**，"第 N 段 = 挂钟第 N 个 10 分钟"这个对齐关系当场失效。
  * **为什么是零、不是"接着录真实声音"**：暂停就是"这段时间不想留下内容"——写真实声音等于
    暂停白按了。
  * **为什么不做任何剪切/补偿**：用户明确说**"不用剃掉"**。转写里那一段之所以没有文字，是因为
    **音频本身就是空白**，不是因为我们事后去剪/补了它。
- **配套（都照旧，别改）**：
  * `meta["pauses"]`（`from`/`to`/`by`）仍然**如实记录**、**暂停时立刻落盘**（`_save_meta`）；
    但它从此是**纯标注**（"这几段是暂停的"），**不参与任何拼接/裁剪/补偿**。
  * 接口签名与回包（`POST /api/meeting/pause|resume` 的 `ok`/`message`）**一个字不改**
    —— 面板与手机都按它走（手机那句提示文案是另一条线的事）。
  * **手机（离线补传）那侧同一口径**（另一条线在做 ✓）：那边**不听电脑的麦**、也就**没法自己
    合成静音**，所以它是在本机把"静音期间的帧"补成零（`android/**/MeetingMicService.kt`，
    与电脑侧原来"本地丢帧"是同一个改动面）。**服务端不需要为它兜底补零**——收到的段本该是连续的。
  * 判据：`tests/test_meeting_pause.py`（时长 / 逐段边界 / 内容是零 / 转写无字 / `meta` 对得上）、
    安卓 `RecordingUiTest`。

## 事件下行：手机怎么知道"电脑上发生了什么"

`GET /api/events?since=&wait=`（`app/api.py::api_events`）：`since<0` 只给游标（**不推历史**，
新装的手机不该被旧事件淹一遍），`since>=0` 给新事件（正序、不重复），`wait` 长轮询（≤25s）。
手机侧两条订阅路：App 在前台（搭状态卡那 5 秒节拍）+ 常驻听写开着时（`ListenService.eventLoop`，
`wait=10`），**共用同一份游标**所以不会互相重复推送。
**边界如实写在设置页**：App 关着、听写也没开时收不到（没有推送通道）。
判据：`tests/test_events_feed.py`、安卓 `EventNotifyTest`。

## 面板的"网络可达"（用户 2026-10-15 提醒）

面板上有三类东西**只在本机成立**，现在都在 `title` 里写明了（`web/app.js`）：
* Agent 那格的 ↗ 是**服务端**开浏览器（token 不下发页面）→ 开在**那台电脑**上，从别的机器/手机
  看面板时看不到那个窗口；
* 后端管理页（`adminUrl`，只绑回环）→ 只有在这台电脑上打开面板时能点开，远程用 `ssh -L`；
* 面板本身：`apiAuthEnabled=true` 时**只有回环**不必带令牌（中继又硬要求鉴权必须开）→
  从局域网打开面板会读不到状态，代码里本来就有一句提示。
**没做**（要用户拍板才能做）：让面板**经中继**打开（白名单加 `/`、`/web/*`、`/sw.js`、
`/manifest.json` + 一条"中继专用只读会话"）—— 那是暴露面的变化，不能擅自做。

### 交付层的标准（2026-10-05 用户拍板固化）：**客户端包必须自带载荷**

- **用户原话**："把如果生成交付包的方案固化下来，未来新版本都按照这个标准来"。
  之后的每一版交付都按这一节走，别再靠"记得传 `--bundle-from`"。
- **事故（就是它促成的这条）**：2026-10-05 下午那层交付里 kit 从 **285 MB 掉成 7 MB** ✗
  —— 载荷（`bundle/`）没带，同事装的时候变成**全量联网下载**（运行环境 + 依赖 + 模型）。
  根因是"手工活"：载荷要 `build_offline_pack.py --bundle` 出 ✓ 再 `build_kit.py --bundle-from`
  塞进 kit ✓，而组层时**没人拦**（`build_delivery.py` 用一张 0 MB 的假 kit 都能出层 ✓）。
- **标准（三条）**：
  1. **客户端包必须自带载荷**：kit 里有 `<顶层>/bundle/wheels/` 与
     `<顶层>/bundle/models/sherpa-onnx-streaming/`（后者是**语音指令**那条路的流式模型，
     189 MB）—— 判据在 `build_delivery.bundle_problems()`，**缺就直接拒绝出层** ✓；
  2. **正确顺序**：`build_offline_pack.py --bundle --out dist\_bundle` →
     `build_kit.py --platforms win --bundle-from dist\_bundle\ECHO-bundle-<stamp>\bundle` →
     `build_delivery.py --scenario client`（帮助文本里也印这一份，报错时直接照抄）✓；
  3. **`--scenario full-local`**（对方那台能本地解码）再加：后端**离线包**（3.1 GB）+
     它**必须自带权重**（`--models-from` 出的那份；不带就拒绝，因为"零下载"是它的语义）✓。
     权重的真实体积（本机实测）：Qwen3-ASR 1793 MB + ForcedAligner 1755 MB +
     SenseVoice 897 MB + VAD 4 MB + pyannote 221 MB ≈ **4.7 GB**。
  4. **交付层要带手机触点 APK**（用户 2026-10-15："apk 也放到交付包里"）：`build_delivery.py`
     自动带 `dist\ECHO-android-*.apk`，没有就退回 `android\app\build\outputs\…\app-debug.apk`
     （**debug 包就是现在能直接装的形态**，`android/README.md` 的构建命令就是 `assembleDebug`），
     落进资料夹时命名 `ECHO-android-app-debug.apk`，并写进 `清单.txt`；找不到**只警告不拦**
     （`--no-apk` 可显式不带，`--apk PATH` 可指定）。判据：`tests/test_delivery_standard.py::ApkInDeliveryTests`。
- **尺寸对照**（用来一眼看出包对不对）：
  `client` ≈ 307 MB（kit 286 + 薄包 21）**——但 `dist` 里存在后端离线包时，`assemble` 会按默认
  把它一起带上，于是这一层是 ≈3.4 GB**（2026-10-06 实测；要"纯客户端"得先移走离线包，
  `--scenario` 目前没有"不带离线包"的开关 —— 这是已知的小缺口）；
  `full-local` ≈ 8 GB（再加 3.1 GB 离线包 + 4.7 GB 权重）。
  **kit 只有个位数 MB = 一定是裸包** ✓。
- **判据**：`tests/test_delivery_standard.py`（裸包/只带 wheel/合格/坏了都覆盖；
  `full-local` 的权重判据；CLI 面 ✓）。**别把这些判据放松** —— 它挡的是一次真实回退 ✓。

### 两条"写补丁 / 验活"的教训（2026-10-05，都是我当天现犯的）

- **插入点不能只看"第一个 `def`" —— 要看缩进。** 我用"第一个 `def `"当锚点把 helper 插进了
  `Admission` **类体里** ✗ → 类的 `__init__` 被挤成模块级函数 → `class Admission` 只剩一句 docstring
  → `Admission() takes no arguments` → **后端起不来**（现象是 `/v1/health` 连不上，而面板照样显示
  `running=True`，见下一条）。规矩：
  * 锚点用**行级定位**：找**行**、从那一行**取缩进**，别拿整段文本精确匹配（差一个字符就整段不写，
    而且**不报错**）；
  * 改完不只 `ast.parse` ✓，**再核一条结构** ✓（例如"这个类还有没有 `__init__`"、"这个函数还在不在
    模块级"）—— 语法正确 ≠ 语义没被搬走；
  * ⚠️ 用 `Get-Content | ForEach-Object { "  " + $_ }` **看**文件时，每行会多 2 个空格；
    照着抄缩进必错（本轮栽过一次）。要么用 `read` 工具，要么正则里写 `^[ \t]*`。
- **验活必须打 HTTP，不能信 pid 记录。** 那次后端其实已经崩了（模块加载失败），而面板与接口照样报
  `running=True` —— 因为那只是 `{DATA}\logs\backend.pid` 里的一行字 ✓。判据应当是
  `curl http://127.0.0.1:8900/v1/health` 回 **200** ✓（外加端口在听 ✓）。
  同理：`python -m server.main --list-calls/--list-clients` 这类 CLI 能跑通，本身就说明后端代码是好的 ✓
  —— 排障时先跑它们，比读日志快。

### 「谁该被看护 / 谁该被拉起」必须由**配置**回答，不能由记录回答（2026-10-13 同一天逮到三次）

同一类错误的三个变体：**拿"曾经有过"的痕迹当"现在还需要"的证据**。

- **看门狗（`app/backend_watch.py`）**：旧判据是 `backend_pid.read_pid() > 0`（"ECHO 起过它"）
  → 一台**改用网络后端**的机器上，那个本机后端被**永远看护**：杀了 30 秒又被拉回来，
  一直占着 8900/8901，谁也不知道它为什么还在（用户的原话是"有没有起不必要的应用"）。
  **现在的判据按配置走**（`wanted()`）：
  ① 三个槽（`capabilityMeetingAsrBackend` / `capabilityDiarizeBackend` /
  `capabilityEmbedBackend`）**都不要**能力后端 → 不守；
  ② 这棵树**连的是谁**（配对文件 `data/backend.json` 的 `base_url` 优先，兜底隐藏设置
  `capabilityEchoServerUrl`）：回环 → 守；**远端 → 不守**（pid 记录只**如实报出来**，
  不当成"要守"的理由 —— 它只是"曾起过"的痕迹）；
  ③ 没有配对线索时：**pid 记录 + 本机后端配置齐**（runtime ready + `server.yaml` 在）才算。
  `state()["wanted"]` 把结论给面板读；⚠️ **`state()` 不许现算 `wanted()`** ——
  `wanted()` 要调 `backend_admin.view()`，而 `view()` 反过来读 `state()`，现算就是**无限递归**。
- **`dsh_homes()` 的判据放宽了，写入侧必须跟着放宽。** 判据 2026-09-25 就认了"只有
  `.credentials.yaml` 的家目录"（**标准版 harness 从不写 `settings.yaml`**），但两个写入器
  （`llm_router._sync_settings_ruamel` / `_sync_settings_text`）都假设那个文件已存在 →
  `FileNotFoundError` → 每次启动 **9 条** `[boot] ECHO AUTO 注册失败`、ECHO AUTO 永远注册不上
  （用户 2026-10-13 报的就是这个，目标是"ECHO 自己的 DSH 标准版"）。
  **修法**：文件不存在 = **空文档，从零建出来**（两条路都要改）。**只改一半的修复比不改更难查**
  —— 判据说"这是家目录"，写入说"这文件不存在"。
  判据：`tests/test_dsh_multi_home.py::RegistrationTests`（ruamel 路 + 文本兜底路各一条）。
- **工具包 `Get-PairingInfo`（`C:\Users\zhkq\Desktop\ECHO\_echo-tools.ps1`）**：原来**先问面板端口**
  —— 两棵树共用 8970，树没起来时**答话的是另一棵树**（于是 dev 被标成"配对到网络后端"，
  而它自己的配对文件写着 `127.0.0.1`）→ `switch-dev` 会**跳过 dev 真需要的本机后端**。
  **配对文件优先**，且只在 `Get-EchoUp` 确认"就是这棵树"时才问面板。
- **判"多余进程"不许只看"它自己没在监听"。** `pythonw.exe … proxy.py` 是 **trampoline**：
  真正在服务的是它 re-exec 出来的 `python.exe` 子进程（同一个脚本、同一秒、**只有子进程听 8899**，
  日志还落在启动器的 stdout 里）—— 和 ECHO 自己那对（`pythonw -m app.main` → `python -m app.main`）
  是**同一形态**。把它当"空闲残留"杀掉会连**模型路由**一起搞坏。
  判据：**这个进程树里有没有人监听**，不是"这个进程自己有没有监听"。
- **配套的后端归属判据**（同一天早先）：看门狗守不守、工具包搬不搬本机后端，都只认**配置 + 配对文件**；
  "停掉本机后端"要走产品接口（`POST /api/capability/backend/stop`），它会写"你明确停过"的退避
  （`PAUSE_AFTER_STOP=30 分钟`）并删掉 `backend.pid`；**按 pid 杀**则会留下 pid 记录 →
  看门狗判据①又把它拉回来（工具包 `Stop-BackendByPid` 已补上删记录这一刀）。




### 清理 `dist/` 与旧包时：**每种包的最新一份必须留住**（2026-10-14 用户定的规矩）

- **用户原话**："以后清理的时候各种包最新版本都要留一份，避免下次出包时需要不必要的重新下载"。
- **必须留（每类至少一份，而且要留最新那一份）**：
  * `ECHO-kit-<最新戳>.zip` —— 客户端 kit（**带载荷**那份；它的解压目录可删，zip 要留）
  * `ECHO-backend-portable-<最新戳>.zip` —— 后端薄包
  * `ECHO-backend-offline-<最新戳>.zip` —— 后端离线包（做过就留；3.1 GB，`full-local`/"零下载"的唯一底料）
  * `ECHO-main-<平台>-<版本>-<最新戳>.zip`(+`.sha256`) —— 主包
  * `dist\_bundle\ECHO-bundle-<最新戳>\bundle\` —— **bundle 缓存**（wheels + models；`build_kit.py --bundle-from` 直接吃它）
- **可以删**：更早的 kit / 主包 zip 与它们的**解压目录**、`ECHO-backend-portable-<更早>*` 目录
  （实测每个 **5.4 GB**）、`dist\_superseded`、`dist\offline`、`_offline-cache`、`shots`/`compress-shots`、
  `_edgepro*`、`_bundle` 里更早的 bundle（目录与 zip 都要清）、`__pycache__`、零散临时脚本/截图。
- **为什么**：bundle 缓存里是**模型 + 轮子**（2026-10-14 实测：models 229 MB + wheels 68 MB + runtime 12.9 MB），
  删了下次出 kit 就得重新下载/重打；离线包 3.1 GB 删掉则 `full-local` 交付要整个重做。
  那次清理把 `dist` 从 **41.36 GB 降到 3.95 GB**（删 37.4 GB）**正是靠"每类只留最新一份"**。
- **判据（清理完自检）**：上列五类**各至少一份且是最新的**；`dist` 里不该再出现上一版的 kit/主包/解压目录；
  ⚠️ `ECHO-kit-*.zip` 只有个位数 MB = **裸包**（缺 `bundle/` 载荷）—— 那不是"要留的最新一份"，是**该删的坏包**
  （2026-10-14 实测踩过：另一个会话重出的 kit 只有 7.3 MB，载荷全丢）。
- **顺带**：清理**不可逆** —— 删之前先把"要留的清单"打印出来核对；GitHub 上的旧 release 可以删
  （**tag 保留**，`git checkout v2.1.5` 照旧可用），但**别把本地那份唯一的离线包也一起删掉**。

### 每日回顾「前置环节」每轮都「已停止」：小窗口轮询撞上 `wait_for_reply` 的超时取消（2026-10-14 真机事故）

- **症状**：点「开始回顾」→ DSH 会话里**每一轮都显示「已停止」**（工具结果报
  `glob was aborted before completion`），前置环节追问 3 次后**干等到 900 秒超时**；
  车里听不到任何东西。用户原话："每日回顾功能无法工作了"。
- **根因**（取证在 DSH 会话事件里，不靠猜）：`daily_review.preflight` 用
  `wait_for_reply(sid, timeout=PRE_POLL_S=3)` **每 3 秒轮询一次**，而 `dsh_agent.wait_for_reply`
  在"这个窗口里没等到**新的**助手文本"时会 `self.cancel(session_id)` 收尾。前置环节是一条
  **几分钟的长活**（扫企微/微信/桌面/WPS 缓存、读当天纪要归档），于是**每几秒被打断一次**。
  * 判据①：会话里 `turn/end` 的 `reason={"kind":"aborted","reason":{"kind":"user"}}` ——
    DSH 里只有 `session/cancel` 会产生它（`dsh-api-session-controller` 的
    `cancel()` → `agent.cancel({kind:'user'}, …)`）；
  * 判据②：`tool/result` 是 `aborted before completion (tool timeout or caller cancellation)`；
  * 判据③：ECHO 日志里连续 3 条「本轮结束但没带标记，追问第 N 次」= `wait_for_reply`
    每次都在取消之后才返回 `done=True`。
- **修法**（`app/agents/{base,dsh_agent,codebuddy}.py` + `app/daily_review.py`）：
  * `wait_for_reply(..., cancel_on_timeout=False)`：**"分小段轮询等长活"的调用方必须传它**；
    默认 `True` 保留给"发一条、等一轮"的调用方（语音命令、回顾提交）。
  * 前置环节判"本轮结束"要**连续 `PRE_IDLE_POLLS=2` 次空闲**（工具调用间隙 `running` 会闪断），
    助手文本**跨窗口保留**，追问用尽**如实退出**（不再空等 900 秒），`should_stop` 一置位就收尾。
  * 教训一句话：`wait_for_reply` 的"超时取消"是**为单轮问答设计的副作用** ——
    谁拿它做长活轮询，谁就会被自己的等待打断。
- **用户同日另外两条口径（一并做了）**：
  * **续说轮次用短模板**：第 2 段起走 `dailyReviewPromptNext`（内置 `daily_review.DEFAULT_NEXT_PROMPT`），
    只带"转写（顶在最前）+ 登记 + 播报契约"，不再重复首轮那套长篇要求（用户："用户的转写要求不突出"）；
  * **轮次等待按进展判完成**（`daily_review.wait_for_turn`）：会话空闲连续 2 次且拿到文本才算完成；
    还在跑就继续等（"没动静"只提醒一句、**不掐断**），硬上限 `ROUND_HARD_MAX_S=900`（15 分钟）。
    设置项 `dailyReviewReplyTimeoutSec` 的语义随之从"每轮最大等待"改成"多久没动静提醒你一句"。
- **判据（护栏，别再放松）**：`tests/test_agent_reply_wait.py`（默认仍取消 / 传 False 绝不取消）、
  `tests/test_daily_review.py` 的 `PreflightTests`（不许取消、空闲确认、停止即时生效、追问用尽如实退出）、
  `RoundTemplatesTests`（续说短模板、转写顶到最前）、`RoundWaitTests`（长活不掐断、硬上限、空闲无文本立刻返回）。
- **附带挖到的坑**：**没在 `DEFAULTS` 里声明的"隐藏设置"其实改不动** —— `Settings.update()` 有一句
  `if k not in DEFAULTS: continue`，PUT 会被**静默丢掉**。`dailyReviewPrePrompt` 的注释原来写着
  "想改文案可以设隐藏设置 dailyReviewPrePrompt"，**那句是不成立的**；现已把 `dailyReviewPrePrompt` /
  `dailyReviewPromptNext` 都声明成 `hidden=True`（值留空 = 用代码里的内置模板）。新加隐藏项照这个来。

### 中继（relay）：手机与电脑互相连不通时经能力后端中转（2026-10-15 用户拍板）

- **现场**：公司内网里手机与办公电脑**互通受限**（AP 隔离 / 跨网段 / 入站被拦），
  但两边都能连到那台能力后端（`aeros.zetone.cn:8900`，**2026-10-15 起域名改为
  `echoserver.zhkq.online:8900`**，同一台机器 221.216.62.2）。
  改后端地址**别手工改配对文件**：`POST /api/capability/address {"base_url": …}`
  就是为"同一台后端换地址（IP→域名/换网段/加反代）、**保留凭据不重新配对**"做的（2026-10-13 加）。
  隐藏设置 `capabilityEchoServerUrl` 也能改（`PUT /api/settings` 的 body **必须是 `{"values":{…}}`**，
  发裸字典会得到 422 `Field required: values` —— AGENTS 里那条"稳定版被 422 拒"就是这个形状问题），
  但它**会让地址有两个来源**：改完设置记得清回空，只留配对文件那一份。
  用户原话："我们可以都连接后端来中转吗"。
- **做法**：电脑**主动出站**去后端取活（长轮询，**不引新依赖**、可 curl 调），手机的请求经后端转进来。
  三处：`server/relay.py`（主机取活/交结果 + 手机入口 + 白名单 + 限流）、
  `app/relay.py`（常驻线程执行 + 退避重连）、安卓**一行不改**（配对页把地址填成中继地址即可）。
  设置 `relayEnabled`（**默认关**，面板与服务组）+ `GET /api/relay`（状态 + 手机该填的地址）。
- ⚠️ **一条必须成对不变的安全命门**：电脑侧转发时带 `X-Echo-Relay`，
  而 `app/api.py::_is_loopback_call()` 见到它**不许再把回环当免检**。
  少了任何一半，中继就是**绕过令牌的后门**（请求从 127.0.0.1 进来，本机豁免把鉴权整个跳过）。
  判据：`tests/test_relay_host.py::LoopbackExemptionTests`。
- 另外三条口径：**hostId 取自已认证的 client_id**（不接受自报，否则能冒充别人的主机）；
  **只放行 `/health` 与 `/api/*`**（两处都判，且挡 `..`）；**开中继的前提是
  `apiAuthEnabled=true`**（回环免检 + 鉴权关着 = 把电脑敞开），不满足时 `ready()` 如实说原因。
- **这条改掉了 2026-10-09 的那个决定**（`docs/手机触点-App设计.md`："不把 ECHO 暴露到公网、
  不做内网穿透"）—— 用户 2026-10-15 明确接受该取舍，于是做成**默认关、手动开**；
  老文档那一处已加日期批注，以 `docs/中继-手机经后端中转.md` 为准。
- **部署**：后端要带 `server/relay.py`（薄包已重出：`dist\ECHO-backend-portable-20261015-1051.zip`，
  或只换 `relay.py` + `main.py` 两个文件重启容器）；中枢是**单进程内存态**，
  **后端必须单 worker**（`Dockerfile` 的 CMD 就是一个进程），多 worker 会随机 503。

## 后端 TLS 落地（2026-10-16 凌晨）：入口变 https，两个产品缺口待修

**做了什么**：能力后端的公网入口从明文 `http://echoserver.zhkq.online:8900` 换成
`https://echoserver.zhkq.online:8900` —— **复用已放通的 8900**，不需要网管加 NAT 规则
（原计划的 8443 方案卡在"上游 NAT 只放通 443 与 8900"）。证书 Let's Encrypt（DNS-01，
阿里云 RAM 凭据），宿主无 cron → **用户级 systemd timer** 自动续期；Caddy 容器
（**host 网络**，`request_body` 必须放**站点级**、`admin` 用 unix socket、
`caddy reload` **必须 `--force`** 才重读静态证书 —— 这三条都是实测踩出来的）。
明文口收口：compose 里 `"8900:8900"` → `"127.0.0.1:8902:8900"`，Caddy 监听 `*:8900`
反代 `127.0.0.1:8902`。判据：Windows 侧 `https://…:8900/v1/health` → **200 且
`ssl_verify_result=0`**（公网 CA，未加 `-k`）；明文 8900 → 000；稳定版 `paired.baseUrl`
变 https、clientId 不变、`ready=true`、中继 40 秒内 `connected=true`。
逐一命令/备份/回滚见 `C:\Users\zhkq\.echo-tls\ECHO-TLS-REMOTE-RECORD.md`。

### ⚠️ 缺口 1：`POST /api/capability/address` 不补证书固定（http→https 必撞）

改完立刻是 `后端 配的是 https，但本机没有它的证书（缺 certPem）——拒绝连接`，
`usable.ready=False`。产品**没有"只补证书不重新配对"的入口**（面板「改地址」只调
`/capability/address`；要证书只有 `/capability/pair`，那要一次性配对码、**会换新 clientId**、
scopes 还得重发）。当晚的绕法：用 `pairing.pair()` 里**逐字同一段**
（`fetch_cert_pem`→`fingerprint_of`→`credentials.save`）补 pin，**保住 clientId/secret/scopes/hostId**。
**该修**：`/capability/address` 在 scheme 变 https（或带 `fp=`）时自动抓取并固定证书 ——
否则每次换地址都得重新配对，而重新配对会换 clientId（中继 hostId 跟着变，手机侧地址也废）。

### ⚠️ 缺口 2：配对串**永远带不出 https**

`server_advertised.advertised_host` 只认 `host[:port]`（`ops.normalize_advertised_host`），
客户端 `normalize_base_url` 见没有 scheme 就补 `http://` → **别人粘配对串会配到明文地址**
（现在明文被 Caddy 回 400）。**该修**：公布地址带上 scheme（或"远程场景默认 https"）。
过渡期办法：**手填** `https://echoserver.zhkq.online:8900`。

### 另一条：明文后端告警部署了但**没生效**

`boot.backend_security_note()`（2026-10-16 加的）取的是 `_base_url_from_settings()`，
而这台机器上它先拿到**本机配对文件**的 `127.0.0.1:8900` → 被判回环而静默 ✗。**该修**：
告警要看**实际用于网络调用**的那个地址（remote 配对），不是本机那份。
## 更正 + 三个新发现（2026-10-16 早，A1/A3 那件事的实证复盘）

**更正上一节「明文后端告警部署了但没生效」的根因**：我原先写"它先拿到本机配对文件的
`127.0.0.1:8900` 所以被判回环"——**这句是错的**。子代理按代码+真机数据实证：
`echo_server._base_url_from_settings(use_credentials=False)` **不读配对文件**，而这台机器
`D:\ECHO\data\echo.db` 里 `capabilityEchoServerUrl` 是**空串** → 旧代码直接 `return ""` 静默 ✗。
修法不变（改用**实际调用那条路**：`_base_url_from_settings()` 默认参数 =
`client_from_settings()` / `EchoServerClient.base_url` / `backend_endpoint()` 同一份解析），
但"为什么静默"必须记对，否则下一个人会去查错地方。

**新发现 1（优先级不一致，未修）**：`docs/设置项归属表.md` 与 `backend_watch._backend_target()`
都写"**配对优先**"，而真实调用是 `_base_url_from_settings()` 的"**隐藏设置优先**、其次配对文件"。
告警已按**真实**调用走（否则告警与实际连的地址会不一致）并有用例钉住；
真要统一口径得改 `_base_url_from_settings()` 的优先级（影响面大，另开一轮）。

**新发现 2（一次网络抖动会把可用状态改成不可用，未修）**：`POST /api/capability/address`
现在是"**先** `set_base_url()` 落盘、**再**抓证书补 pin"→ 抓证书失败时地址已改、证书没有、
`ready=False` ✗。更稳的是"**先抓证书、成功才落地址**"（需要动 `app/capability_admin.py`）。

**新发现 3（功能没入口）**：补 pin 的接口已支持配对串里的 `fp=`（指纹，用来防中间人），
但面板「改地址」那格**没有**填指纹的地方 → 这个防中间人的能力目前用不上（`web/app.js` 未动）。

**A1 的行为变化（有意，已钉用例）**：https 且本地缺证书时**重提同一地址**会顺手补 pin
（旧实现回 200"地址没变"继续坏着）；证书已在时仍是纯 no-op。https→https **换主机**会重取证书
（TOFU：证书里没有"这是哪台机器"的可靠记录，不重取就会撞"服务端换证书了？重新配对一次"的误导句）。

## A2 已关掉（2026-10-16 上午）：配对串能带出 https + 远程不再静默降级

* **公布串的 scheme 按部署事实表达**：证书配了→https；显式写了 scheme→按写的（含远程显式 `http://`）；
  否则局域网/本机→http、**远程→https** —— 所以真机 `echoserver.zhkq.online:8900` 一个字不改也能发出
  https 串（这条把上面"过渡期办法：手填 https"作废了）。要退回旧口径：`server/ops._published_scheme`
  最后一行改回 `return "http"`，其余不用动。
* **客户端不再静默降级**：`normalize_base_url` 对"手输的无 scheme 远程地址"**报错**（教人写
  `https://主机:端口`）；唯一例外是改地址时沿用**凭据里已记着的** scheme。选"报错"而不是"猜 https"
  的理由：猜 https 会把真跑明文的后端连错、且现象与"它挂了"分不开；猜 http 就是缺口本身。
* **改地址原子化**：现在**先抓证书 + 校 `fp=`（不写盘）、成功才一次性写地址与证书**；失败时旧地址/旧凭据
  逐字未变、仍能连旧地址（`AddressChangeIsAtomicTests` 里有一条"失败时一次 save 都不许发生"）。
  原来"先落盘、再补 pin"→ 一次网络抖动就把可用状态改成不可用 ✗。
* 面板「改地址」多了可选 **`fp=`（证书指纹）** 输入，文案如实写"防中途被换成另一张证书 / 留空照旧可用但
  少一层保护"。**`docs/**` 与 `delivery/*/先读我.md` 里"手填 https"的旧说明还没改**（不在当轮允许清单内）。
## 后端 diarize 优化实测：**batch 这条路只有 9–16%，已按红线回滚**（2026-10-16）

用户换卡（**RTX 2060 SUPER 8 GB → RTX 2080 Ti 22 GB**）后问"diarize 还有优化余地吗"。实测归因与结论：

* **单次 600 s 分离 ≈ 18.5–20 s**，阶段占比：**分割 ~6.8 s + 嵌入 ~11.8 s + 聚类 ~0.1 s**；
  模型加载只占 **1.17 s**（`loadMs`，`unloads: 0`）→ **`resident: true` 白改** ✗（Lead 试过又回滚：
  常驻让两档同时在卡上跑，与 ASR 抢带宽，diarize 峰值反而抬到 42 s ✗ —— 那个 42 s 还叠加了测量探针抢锁的噪声）。
* **pyannote 4.0.7 的 `embedding_batch_size` / `segmentation_batch_size` 默认都是 1**，
  而 `app/audio/diarize.py` 构造 `SpeakerDiarization` 时**没传** → 600 s 音频退化成 **~1773 次单条 forward** ✓。
  **唯一正确的传参位置是构造函数**（`instantiate(HYPERPARAMS)` 的 `default_parameters` 里没有这两个键 ✗）。
* **显式传 `embedding_batch_size=16` 的实测收益**：微基准 **+16.1%**（18.67 → 15.67 s）、
  线上 **+8.6~9.7%**（19.1–20.7 → 17.2–18.1 s），**质量零变化**（说话人集合/turns 完全一致 ✓），
  显存峰值 124 MB → **874 MB**。**低于我们定的 20% 红线 → 回滚** ✗（判据与原始数据见当轮报告；
  镜像 `echo-backend:0.1.0-cu126-bs16-20261016-2045` 仍在宿主上，要启用只需再 scp 一个文件 + 重建 ✓）。
* **为什么没到 1.7–2×（关键）**：嵌入那一档**不是 GPU forward 受限** ✗，而是卡在每个
  (chunk, speaker) 对的 **CPU 侧 `Audio.crop` + `nan_to_num`/`astype` + `vstack`**（1773 项，批不掉 ✗）
  —— hook 实测嵌入只从 11.78 s → 8.70 s。**再要更快得换方向**（改那条 per-item 预处理路径，
  或用更大的 `segmentation_step`＝**精度红线**，须先算真实会议音频的 DER ✓）。
  GPU 侧本身已打满（采样均值 81%、持续段 85–94%、功耗 246–254 W **顶到 250 W 墙** ✓）。
* **`segmentation_batch_size` 故意没一起改**：它在构造 `Inference` 时就固化，且分割那档（~1/3）
  在干净条件下没实测过收益 ✗ → 保守只动嵌入（结果也没达标 ✓）。

### 宿主"改后端代码"的正确姿势（本轮查实，别再猜）

* **构建上下文是 `/home/zysk/echo/src/`**（含 `app/` + `server/` + `compose.yaml`），
  **`/home/zysk/echo/backend/` 只是运行时目录**（只有 compose.yaml / .env / server.yaml ✗ 没有源码）；
  它 compose 里的 `context: .` 现场构建跑不通，靠 `.env` 的 `ECHO_IMAGE` 指镜像 ✓。
* **增量重建入口 = `src/Dockerfile.update`**（`FROM echo-backend:0.1.0-cu126` + 两句 COPY app/server ✓）：
  `cd /home/zysk/echo/src && setsid nohup docker build -f Dockerfile.update -t echo-backend:<新 tag> . < /dev/null > build.log 2>&1 &`
  —— 实测 **0.2 s、零下载**（`server/Dockerfile` 里源码 COPY 在**所有 pip 层之后** ✓，所以不会重下 torch ✓）。
* **改完宿主源码要 `scp` 同步那一个文件**（两侧 sha256 对账 ✓）；**被否掉的改动要把宿主那份也还原** ✓
  （否则将来一次 `Dockerfile.update` 会静默把被否的东西又发上去 ✗）。
* 回滚 = `.env` 的 `ECHO_IMAGE` 指回旧 tag + `docker compose up -d backend` ✓（**秒级**，实测有效 ✓）。
* 顺带两条**陈旧信息**（本轮发现，尚未改）：`.env` 的 `ECHO_SERVER_ID` 仍写 `gpu-office-2060s` ✗；
  `server.yaml` 的注释仍按 **8 GB 卡**校准（`vram_budget_mb` 的说明、`est_vram_mb: 1800` 与它自己那句
  "实测峰值 4.6 GB 要给 4700" 自相矛盾 ✗）。现在实际是 **22 GB 卡 + `vram_budget_mb: 16000`** ✓。

### 验收数字（当时那两场真实会议）

* **78（5392.6 s）重转后 = 8 个说话人**（改前库里那次是 **1 个的退化结果、仅 185/1868 行有标签** ✗
  → 重跑本身修掉了一个真实质量问题 ✓）；**79（4333.2 s）= 4 个说话人**，标签直方图与库里逐项相同 ✓。
* ⚠️ **质量对比只能严格说"说话人集合与人数一致"**：78 上旧代码自己就有波动（turns 194 vs 193、
  lines 1877 vs 1866），所以"边界无实质变化"**没有**被严格证明 ✗。
## 2026-10-17 深夜 / 18 凌晨：真机驱动的一轮修复（决策与踩坑索引）

用户连续三轮真机测试逼出十几个真 bug。这里只记**容易再犯**的判断与判据，细节见对应提交与用例。

### 「谁在采集」必须是事实，不能被缺省值/幂等键吞掉

* 现象：手机点「开始录音」→ **电脑开麦录了** ✗、手机传的段全被拒 ✓、用户以为"手机发的会"。
* 根因两层：① 安卓那颗按钮走的是**遥控电脑**（请求**零参数** ✗，`meetingStart()` 已**删除** ✓）；
  ② 服务端 `src = (source or "pc")` 把"没说"读成"明说要 pc" ✗。
* 现行判据（只此一份）：`app/api.py::_meeting_capture_source()` = **`clientId` → 显式 `source` → 发起方**（`X-Echo-Relay` = mobile ⇒ 手机麦 ✓）。
  **手机显式 `source=pc` 仍算电脑麦** ✓（不许一刀切）。
* 配套硬约束：**建会必须延后到"第一段音频真的落盘"之后** ✓（否则会出现"没有音频的手机麦会议" ✗ —— 空会占住服务端唯一录音槽 ✗）；
  **结束时把不足一段的尾段封上** ✓（`MeetingTailFlush` ✓）—— **0 个样本才是唯一允许不建会的情况** ✓，且要如实说"没有录到音频" ✓。
* 判据：`tests/test_phone_start_is_phone_source.py`（含"中继 + 零参数 → 手机麦"的真机回归条 ✓）、安卓 `MeetingPhoneStartTest` / `MeetingTailFlushTest`。

### `meeting.active` 不等于"这台电脑在录音"

* 手机麦会议在上传期间**服务端就是 active** ✓（在等段传完），但**电脑面板不许显示成"电脑在录"** ✗。
* 判据：`meeting_status().source` ✓（面板 `isPhoneMicMeeting` 一处判据 ✓）；电脑那颗「开始录音」置灰 + `title` 说明 ✓，
  **不许变成「停止录音」** ✗（否则点了会误停手机那场 ✗）。`/api/meetings` 行也要带 `source` ✓（列表与仪表盘不能一个说手机、一个说电脑 ✗）。
* ⚠️ 状态泄漏：`start_meeting()`（电脑麦）若不显式写 `_state["source"]="pc"`，**上一场手机麦之后会继续报 phone** ✗
  → 面板显示"手机在录"并把按钮置灰，**用户连停都停不了** ✓。这条有用例守着 ✓。

### MediaCodec 产的 FLAC 是"合法但没写长度"，libsndfile 会崩

* 症状：`CompressionError: 音频段01.flac解码失败：Internal psf_fseek() failed.`；旁证 meta 里
  `durationSeconds = 1152921504606847.0`（= 2^63/8000 ✗）。
* 根因：MediaCodec 写 FLAC 头时还不知道长度 → `STREAMINFO.total_samples=0`、`md5=0`（**规范合法** ✓）；
  libsndfile 1.2.2 把它读成 `SF_COUNT_MAX(2^63-1)` ✗ → 逐块 seek 必失败 ✗。**文件本身是好的** ✓（ffmpeg 完整解出 ✓）。
* 双保险（两侧都要）：
  * **手机**：封段收尾**原地回填 36-bit `total_samples`** ✓（跨 5 字节：偏移 21 的低半字节 + 22..25 ✓、**长度不变** ✓、
    写完重新解析自检 ✓），失败就**整段退回 WAV** ✓（`audio/FlacStreamInfo.kt` ✓）。
  * **服务端**：解码 `libsndfile 优先 → ffmpeg 兜底` ✓；**不许放松校验** —— `-loglevel error` 下 **stderr 必须为空**
    （实测**截断的 FLAC 会解出半截且退出码 0** ✗，只听退出码就会谎报成功 ✓）+ 用标准库 `wave` 复核位深/采样率/帧数 ✓。
  * `audio_seconds()` 遇到哨兵值要**真解一遍量** ✓，**绝不把 1.15e15 秒写进 meta** ✗。
* 判据：`tests/test_audiofile_mediacodec_flac.py`（**合成**样本：6 KB 正弦 + 同样的坏头 ✓，
  真实录音不进 git ✓）、安卓 `FlacStreamInfoTest` / `MeetingSegmentFlacHeaderTest`。

### 「一次探测超时 ≠ 后端不可用」（会让人白等 15 秒然后手动重转）

* 现象：停会报"后端暂不可用…等它恢复后可重新转写" ✗，**48 秒后手动重转就成功** ✓，后端整晚在线 ✓。
* 根因：每轮转写新建 router → 现场 `GET /v1/capabilities`（**timeout=15** ✗）→ 一次失败就 `provides=frozenset()` ✗ →
  判"它什么都不提供" ✗ → `waiting-backend` ✓。**时间差正好 15 秒** ✓。
* 现行策略：探测 **5s × 2 次（+0.4s 间隔）** ✓（比原来单次还短 ✓）；失败**不清空** `provides` ✓，
  用**进程级上次已知能力**兜底（**上限 30 分钟** ✓ + **必须先过免凭据 `/v1/health`** ✓），
  `supports()` 与 `provides` **分离** ✓（"上次说能干什么" vs "这轮敢不敢派活" ✓）；健康也不通 → **不派活** ✓。
  每次失败**必须落日志** ✓（旧代码只在内存里，事后零痕迹 ✗ —— 这次就是因此只有推断 ✗）。
* 判据：`tests/test_caps_probe_resilience.py`（含"一直失败仍如实报 waiting-backend"✓ —— **不许把它变成永远不报** ✗）。

### 暂停 = 空白音频（补充：**计时必须每帧推进**，且**只有尾段允许不足一段**）

* 现象：暂停时计时器冻住 ✗ → 用户误判"恢复后不到一分钟就封了第一段" ✗。
* 真相：界面大计时在**组合体里**读挂钟、只在重组时算 ✗；而暂停时唯一每 100ms 变化的 `level` 被跳过 ✗ → 整块界面不重组 ✗。
  **时长其实一直在累计** ✓（真机音频逐秒 RMS 已证：30s 满段 + 19.9s 尾段、暂停区全零、总长 ≈ 挂钟 ✓）。
* 铁律：**计时/本段进度每帧（含暂停帧）推进** ✓；**正常封段必须刚好一个段长，只有结束时的尾段允许不足** ✓
  （生产判据 `segmentLengthProblem` + `Log.e` ✓）——"暂停期间不要封段"是**口径变更**，别擅自做 ✗。

### 朗读：谁发起谁念 + 只念摘要（单一出处）

* 用户口径：**手机的任务手机念、电脑只执行不念** ✓；**两端都只念摘要**（详情是给人看的，不是给耳朵听的 ✓）。
* 实现：电脑 `app/assistant.py::speak_text_for_reply()` 是**唯一出处** ✓（复用既有 `_DETAIL_SPLIT_RE → conclusion_only → brief_text` ✓），
  电脑喇叭与回传手机的 `speakText` **同调它** ✓；安卓 `SpeakPlan` **有 `speakText` 就念它、绝不自己切** ✓（老电脑没有时才取第一段摘要 ✓）。
* 两处兜底**故意相反**：通知拿不到 origin → 按 pc **少打扰** ✓；朗读拿不到 origin → **照念** ✓（该念却静默 = 功能坏掉 ✗）。注释两处都写明 ✓。

### `meta["pauses"]` 会被 stop 静默抹掉

* 现象：手机 `POST /api/meeting/pauses` **三次 200** ✓、日志也记"新增 1 条" ✓，但两场 `meta.json` 的 `pauses` 是空 ✗。
* 根因：`add_meeting_pauses` 只写盘、**不同步 `_state`** ✗ → 10 秒后 `stop_meeting` 用内存快照**无条件覆盖** ✗。
* 现行：补报后同步内存镜像 ✓ + stop 改成**合并盘上与内存**再写回 ✓（**只许往上加、不许覆盖** ✓）。
* 判据：`tests/test_meeting_pauses_land_in_meta.py`（**一律从磁盘重读 meta.json** ✓，且"临时还原修复 → 用例红"验证过 ✓）。

### 测试卫生：`tearDownClass` 先收线程、再还原库路径、再删目录

* 见 `tests/test_meeting_retranscribe_guard.py`：后台转写线程没收就走 → `database is locked` / `no such table: settings` ✗，
  还会把下一个类的 `setUpClass` 卡住 → **整类 7 条用例被"既不算跑过也不报错"地吞掉** ✗（门禁 `Ran 3279` vs 3286 的来源 ✓）。

### 仓库 `.gitignore` 裸 `data/` 会吃掉同名源码目录（本次逮到）

* 现象：`android/app/src/main/java/com/echo/contact/data/` 下 **19 个 .kt 从来没进过 git** ✗
  （`EchoApi.kt` / DTO / `PairStore` …）→ **远端仓库根本编译不出这个 App** ✗。
* 根因：`.gitignore` 里是裸的 **`data/`**（= 任意深度名为 data 的目录 ✗）；Git **不允许**在被排除的目录里用 `!` 反选 ✗。
* 修法：锚定成 **`/data/`** ✓（只忽略仓库根的运行时数据 ✓），并补进那些源文件 ✓。**加目录忽略规则一律加前导 `/`** ✓。
### 录音格式：**PC 维持 WAV，手机才是 FLAC**（2026-10-18 凌晨用户拍板）

- 用户看过"手机录音没有压缩率"后问"PC 也直接压缩吧"，我给的方案是"**在场段 WAV + 封段转 FLAC**"（崩溃可抢救 ✓），
  但用户最终决定：**"那就维持wav录音的模式"** ✓ —— **PC 端不要改**，仍然"WAV 在场 → 事后压缩归档" ✓。
  * 那条"封段转 FLAC"的改动**已还原**（`app/audio/recorder.py` / `app/meeting.py` 的未提交修改被 `git checkout` 掉 ✓）。
  * 别再把这条当待办 ✗；要改得用户再点头 ✓。
- **手机**那侧仍是 FLAC ✓（直录 + 封段回填 `STREAMINFO.total_samples` ✓ 见上文「MediaCodec 产的 FLAC」）。
  理由：体积减半 + 已修好头的坑 ✓；用户对手机这条没有异议 ✓。
- 顺带一条**判据教训**：`meta.json` 里"压缩率"那栏只对"事后压缩的 WAV"有意义 ✓；
  源本来就是压缩格式时显示 `-`/1.0 是**如实**，不要为了"好看"去造一个数字 ✗。
## 2026-10-19 凌晨：真机第三/四轮的决策与坑（回顾交互 / 819 / 电平 / 推送）

### 「819 度」的真根因：**上游清洗**，不是 TTS

- 现象：手机和电脑都把 `8~19℃` 念成「819度」✗。
- 真因（实证，逐字节复算命中 id 178/181/182）：`app/assistant.py::_clean_line` 里 `re.sub(r"[_~^]", "", t)` 把 `8~19` 的 `~` 当 **markdown 强调符**删掉 ✗ → `819`；`:221` 再把 `℃`→`度`。
  **`commands.brief`（= 手机念的 `speakText`）存进库时就已经是 819** ✗ —— 所以**下游 TTS 归一化救不了** ✗，手机侧也无解 ✗（819 与真的"八百一十九"不可区分）。
- 修：在**任何 markdown 清洗之前**先用 `_RANGE_RE` 把"数字 ~ 数字（可带 ℃|°C|度|%|％、小数、空格）"救成 `X 到 Y` ✓；`~` 的 markdown 语义**只剥成对的 `~~…~~`** ✓。
- 双保险（仍保留 ✓）：电脑 `app/audio/speech_text.py`（挂在 `tts.speak()/synthesize()` 选引擎前）+ 安卓 `SpeechText.kt`（系统 TTS / ECHO 合成请求 / 常驻听写三处）——它们能救**还带分隔符**的形态（`60%–78%`、`1013–1017hPa` ✓），但救不了已经被清洗掉的 `~` ✗。
- **铁律**：文本清洗类函数里，**数字之间的符号先判语义再删** ✓（`~` 在数字间是区间 ✗ 不是强调）；"看着像 markdown" 不是删除的理由 ✓。

### 手机「回顾」= PC 版交互（开始 → 多轮 → 结束）

- 用户口径："跟 pc 版的交互一样，开始回顾开始然后继续交互" ✓。
- **开始只调 `POST /api/daily-review/start`** ✓（幂等建/取当天 session ✓、**不碰麦** ✓）。
  ⛔ **不许调 `POST /api/daily-review/go`** ✗ —— 它内部是 `daily_review.start()` + `assistant.start_review()`，后者会**真的开电脑那支麦** ✗（PC 面板那个按钮才用它 ✓）。
- 进行中：**多轮对话** ✓ —— 底部输入条（打字 → `sendCommand` / 按住说话 → 既有 `startRecording(asCommand=true)` → `voice-command` ✓），一轮**追加两条**（我说的 + ECHO 回的 ✓）→ 多轮天然是追加 ✓（判据："两轮都追加不是覆盖" ✓）。
- 结束：`POST /api/daily-review/stop` ✓ + 用 `reviewStopNote` **三态**如实说 ✓。
- ⚠️ **已知限制（如实说，不绕）**：回顾历史只读 `commands.source='review'`，而 `voice-command` 落 `source='mobile'` →
  手机这条**不会进「回顾历史」** ✗ → UI 用"**刚才那一场**"只读卡兜 ✓，并写明"每一轮都在「指令」历史里" ✓。
- 一次性口述（`submitReviewDictation` → `/daily-review/submit`，阻塞 2–3 分钟）**保留为退路** ✓，别删 ✗。

### 回顾「按了发不起来」：**断在哪都没人说**

- 现象：手机两个入口都点不动 ✓、电脑日志里 **零 `POST /api/stt/transcribe`** ✗（但 GET 都 200 ✓）。
- 真因：`MainViewModel.startRecording()` 里 `if (_recording.value) return true` —— **已经在录时再按 = 静默 no-op** ✗；
  另三档也全静默 ✗：**没给麦克风权限**（回顾页旧码 `if (granted) {…}` 外无分支）、**麦被会议录音/常驻听写占着**、**麦开了但 `read()` 一直 0 字节**；
  失败原因还写进 `_submit`（"发指令失败" ✗）+ 全 App 无人读的 `_speakNote` ✗。
- 修：`ui/DictationEntry.kt::dictationGate()`（**四档原因互不相同** ✓）+ `listenHoldsMic()`（只有 STARTING/LISTENING 真握麦 ✓）+ **3 秒"收不到声音"看门狗** ✓；
  回顾口述的原因只写 `_dictateNote`（**一个 `_submit` 都不碰** ✓）；口述卡挪到功能卡下面（小屏不再被折叠线挡 ✗）。
- **铁律**：给用户按的入口，**每一条 return 都要有可见原因** ✓；"已经在做"也是要说的状态 ✗ 不许静默 ✓。

### 录音页电平（"不好看" → 重做）

- 用户："电平效果感觉还是不好看" —— 一半是样式（17 格离散柱 ✗）、一半是**抖动**（原始 RMS 每 100ms 跳 ✗）。
- 设计：**中心对称等厚胶囊** + 全宽渐变（中心蓝→紫→端头红，取主题色 ✓ 无硬编码 ✗）+ **峰值保持 800ms / 0.30 每秒回落** + **包络 0.35 攻 / 0.12 放**（**帧间时间归一** ✓ 换机手感不变 ✓）+ 暂停/极低时 **2.6s 缓慢呼吸只动亮度** ✓（长度不动 ✓ 否则就成"抖"）。
- **`level^0.65` 视觉压缩** ✓：麦克风 RMS 本来就低（说话常 0.1–0.3），线性映射"看着没反应" —— 这是"不好看"的另一半 ✓。
- 性能：`withFrameNanos` 只写 4 格 `mutableStateOf` + **只在 `onDrawBehind` 里读** → **整页零重组** ✓，每帧 2 个圆角矩形 + 2 根线 ✓。
- 预算：电平带 112dp（含 64dp 条）✓，录音页总高度**只减不增** ✓；⚠️ 布局余量仅 **6dp** ✓（再加大就翻 ✗）。

### 首页经中继打开 DSH 标准版网页（只读）

- 服务端：`/api/status` 顶层新增 **`dshWebUrl` / `dshWebReason` / `dshWebReadOnly`**（同一份也在 `/api/pair/phone` 与 `/api/relay` ✓）；
  值 = relay `publicUrl` + `api/relaydsh/<只读会话令牌>/` ✓；不可用时 url 空 + 人话 reason ✓ **绝不自己拼** ✗。
- 白名单：静态 `""`/`index.html`/`assets/`/`plugins/`/`manifest`/`favicon` ✓ + **DSH RPC 方法白名单**（`session/list`、`session/page`、`settings/describe`…）✓，
  `session/prompt`、`settings/update`、`terminal/*`、`credentials/*` **一律 403** ✓；只 GET/HEAD ✓；**没放行面板路径** ✓（面板写死绝对 `/api`，经中继打开等于把读写会话交给管理面 ✗）。
- 安卓：**App 内 WebView** ✓ + `shouldInterceptRequest` 对**同源**补 `Authorization: Bearer <手机令牌>` ✓ ——
  **系统浏览器必 401** ✗（中继对每个 `api/*` 都要 Authorization，浏览器不带 ✗）；令牌**只进请求头** ✓（不拼 URL、不注入 JS、日志不打 path ✓）、跨源不补 ✓、外链交系统浏览器且不带令牌 ✓。
- 边界：DSH 实时流是 **WebSocket** ✗，中继只搬 HTTP → 这页是**快照视图** ✓（会话/历史能看 ✓，不实时推 ✗）；非 GET/HEAD 同源请求补不了头 ✓（会亮提示 ✓）。

### `gh-push.ps1`：退出码可能是**假的**

- 老版本确有"`Confirm-Push` 返回值被丢弃 → push 退 0 就报成功" ✗ → 已改成 **`ls-remote` 逐行解析 + ref 精确等于 `refs/heads/<branch>` + 与本地 HEAD 小写化精确相等** 才算成功 ✓（空/格式怪/多行/别的 ref/非零退出全判失败 ✓）。
- ⚠️ **另有一个假象** ✗：从命令行以 `-File` 启动该脚本时，会出现"**1 秒、退出码 0、零输出、脚本体一行没执行**"✗（插 `Write-Host` 标记不打印、`Start-Transcript` 不生成 ✓）；同一份文件用 `python.exe` 直拉则一切正常 ✓。
- **铁律**：验收推送**别只看退出码** ✗ → 看输出里的 **`verified: origin/<branch> == <sha>`** 或 `push OK` ✓，并 `git ls-remote` 核一次 ✓。

### 测试/协作两条经验

- **并发改同一棵 `android` 树会互相踩** ✗：`Conflicting overloads`、`classes.jar 被另一个构建占用` ✓ → 收尾跑门禁前先等"树安静"（mtime 静 2–3 分钟 ✓），或让 agent 在 `git worktree` 隔离副本里验证 ✓。
- **用例按"下一个函数签名"切源码** ✓（`TestSources` 那套）：往两个函数之间插新函数会让既有用例**红** ✓ → 新代码要挑好插入点 ✓（本次已按"函数段"标出禁止插入区 ✓）。
### 待改（用户 2026-10-19 上午攒批，等"测试完一起"做）

**攒批清单（一次改完、一次收口，别一条条发版 ✗）**：

1. **桌面图标名 → 「超级助理」** ✓（用户："app 在桌面展示的名称就叫超级助理吧，echo 已经做成图标了"）。
   - 只改**启动器显示名** ✗（App 内标题仍是 `ECHO 超级助理` ✓）；用户当天后来说 **"不着急改 / 等我测试完一起"** ✓ → 先攒着 ✓。
2. **录音页电平：更敏感 + 波形感** —— 用户原话："录音点评现在是呼吸状，感觉用**更加敏感的波形效果**更明显，你先记下"。
   - 现状：`RecordingLevelMeter` 是**中心对称胶囊** + 包络 0.35 攻 / 0.12 放 + 峰值保持 + 静音时 2.6s 呼吸 ✓ → 用户觉得**太"温"** ✗。
   - 方向（**待真机对照再定** ✓）：
     * **更敏感** ✓：攻击更快（0.45–0.6）/ 映射更陡（`level^0.65` → 更大指数或加门限压缩）/ 释放在低段更快回落 ✓；
     * **波形感** ✓：**注意我们只有 RMS 电平、没有原始 PCM** ✗ → 能做的是**"最近 N 帧电平历史"的滚动波形** ✓（时间窗 ~0.5–1s、对称镜像、峰值顶到外沿 ✓），不是真波形 ✗；
       ⚠️ 若要**真波形**，得把采集侧 PCM 摘要（每 ~50ms 的 min/max 或 peak）暴露给 UI ✗ → 要动 `MeetingMicService` ✓（改动面大 ✓ 需用户点头 ✓）。
   - 判据想法：给定电平序列 → 波形/包络在同一时间窗内**可见位移** ✓；静音时**不呼吸**（或极轻 ✓ 用户嫌它"呼吸状" ✗）。
3. **电平高度再高一点**（用户 2026-10-19 上午追加原话："高度可以再高点，反正空间够大"）
   - 现状：电平带 112dp（含 64dp 条）✓，录音页总高度当时刻意"**只减不增**"✗；
   - 用户现在**明确允许加高** ✓ → 可以把电平带加高（如 112 → 140–160dp）✓，
     ⚠️ 但要同步更新**布局预算用例**（`HomeLayoutTest` / `RecordingUiTest` 里那条"总高度不增"✗ 要按新口径改 ✓，
     不是删 ✗）；余量只有 6dp ✗，加高后小屏（568dp 高）仍必须按钮可见 ✓。
4. （等用户测试结论补充）
## 2026-10-19～20：补传马拉松的六条工程教训（都是我自己当天踩的）

### ① python **文本模式**写回会把 LF 变成 CRLF，撞 `test_line_endings`（2026-10-20 门禁红）

- **现场**：改 `server/relay.py` 的一个常量，用 `io.open(p, encoding="utf-8")` 读 + `io.open(p, "w")` 写 ✓ →
  Windows 下默认 `newline=None` 会把 `\n` **翻译成 `\r\n`** ✗ → 门禁 `tests.test_line_endings.ServerTreeHasNoCrlf`
  红 ✓（`['server\\relay.py'] != []` ✗），而**逻辑完全正确** ✓。
- **铁律**：改仓库里已有文件的行尾 → **只读改写用 `newline=""` 或二进制** ✓：
  `io.open(p, "w", encoding="utf-8", newline="\n")` ✓ / `open(p,"rb").read().replace(b"\r\n", b"\n")` ✓。
  改完**立刻验**：`b.count(b"\r\n")` 应为 0 ✓（`server/`、`scripts/`、`tests/` 都被这条守门 ✓）。

### ② `git commit -m "<含中文/引号的长消息>"` 会被 PowerShell 拆坏 → 用 `-F`

- **现场**：`git commit -m "fix: …（用户："…"）"` 在这台机子上被拆成一堆 `pathspec` 错误 ✗
  （`error: pathspec '还是叫' did not match…`）→ **提交没发生** ✗（但 `git add` 已生效 ✓，容易误以为提了 ✗）。
- **铁律**：多行/含中文/含引号的提交消息**一律**：
  `[System.IO.File]::WriteAllText("$env:TEMP\msg.txt", $msg, (New-Object System.Text.UTF8Encoding($false)))` +
  `git commit -q -F "$env:TEMP\msg.txt"` ✓（这套本会话用了十几次 ✓ 一次没坏 ✓）。

### ③ 门禁期间 **HEAD 变了 → 不许推**（否则推出没门禁过的代码 ✗）

- **现场**：门禁跑 ~40 分钟 ✓，中途我又提交了几笔 ✗ → 推送脚本用的是**当前 HEAD** ✗（`git rev-parse HEAD`）→
  差点把**未经门禁**的新提交推上去 ✗。
- **铁律**：收口脚本必须在**开始时记 `$h = git rev-parse HEAD`** ✓，门禁后先比 `HEAD -ne $h` → **不推** ✓；
  且**停掉**那条在旧树上跑的门禁作业（`job_kill` ✓），另起一条 ✓。

### ④ `job_output(wait: true)` 会**提前返回** `[status: running]`（不是阻塞到结束 ✗）

- **现场**：想用它等 10 分钟，结果 ~10 秒就回 `(no new output) [status: running]` ✗ → 白等/误判。
- **铁律**：等长任务用 **`Start-Sleep` 轮询**（或 `powershell` 里 while + `Start-Sleep` ✓），
  或直接 **`Start-Process` + 日志文件** ✓（本会话的 `_gate_finalNN.log` 这套最稳 ✓）。

### ⑤ `[Math]::Min($a, $b)` 在 PowerShell 里会因**参数类型**报"找不到重载"

- **现场**：`[Math]::Min($idx+40, $lines.Count)` → `找不到"Min"的重载，参数计数为:"2"` ✗（`$idx` 是数组/类型不匹配 ✗）。
- **铁律**：拿不准就用 `if` 或 `[Math]::Min([int]$a, [int]$b)` ✓；更稳的是**别在 PowerShell 里算** ✓ ——
  这种活交给一小段 python ✓（本会话后来都这么做 ✓）。

### ⑥ 代理端口**不要用 8901**（那是能力后端的 admin 口 ✗）

- **现场**：给 GitHub 推送代理随手挑了 `-LocalPort 8901` ✗ + 清理逻辑 `Stop-Process` 该端口 ✓ →
  等于**可能误杀本机能力后端的 admin** ✗（本机当次没跑后端 ✓ 侥幸 ✓）。
- **铁律**：代理/临时端口用 **98xx**（本会话后期用 9821–9825 ✓）；并且**清理只杀"自己起的那个"** ✓ ——
  记下 `Start-Process` 返回的 pid ✓，按 pid 杀 ✓，不要按端口杀 ✗。

### 补：两件与"补传"有关的**语义**（写进代码注释了，这里留索引）

- **手机补传的段是 FLAC（≈10 MB/10 分钟）** ✓，盘上同名 `.wav` 是服务端解码副本 ✓ —— 判断"传多大"要看上传侧 ✓。
- **"仅上传"不等于"在录音"** ✓（`meta["uploading"]=true` / 无 `end` ✓）：它会占住 `active` ✗ 挡住电脑建会 ✗；
  对外要能区分 ✓，但 `active` 的既有契约**别改** ✗（改则连带动 pause/stop/客户端 ✓）。

### 2026-10-21 同事装机四问（根因与永久修法，别再逐台救火）

用户原话四条：① 装在非默认路径时输入 `C:\echo` 报"路径有问题"；② 两台电脑都报要装 .NET；
③ 安装器里选网络后端、贴配对码，http/https 都配不上；④ 服务端生成的配对串还是 http，
改成 https 后才在 ECHO 设置里配对成功。

- **① 路径 = 引号/尾反斜杠转义，不是路径本身非法。**
  Explorer「复制为路径」给的是 `"C:\ECHO\"`；`-Root "C:\ECHO\"` 里的反斜杠会把收尾引号
  转义掉，后面的 `-BackendDir …` 被吞进值里。**入口先归一化**：去空白/引号/尾反斜杠
  （`delivery/kit-install.cmd` + `install-all.ps1` + `install.ps1`），再校验。
  另外 `C:\` 根目录在受管电脑上常没写权限 —— `install-all` 现在**当场**报"创建不了安装目录"，
  不再等到写 data 时才报一句像"包坏了"的拒绝访问。
- **② .NET 是**边条**（`echo-sidebar.exe`，framework-dependent net7.0 WinForms）要的，ECHO 本体不要。**
  缺运行时时 apphost 自己弹「You must install or update .NET」，而 `Popen` 当场返回成功 ——
  以前调用方以为边条起来了。现在 `app/runtime.py::_sidebar_runtime_ok()` 先判
  `app/platform/*/env.py::sidebar_runtime()`：没有就**不起边条**、自动用浏览器面板
  （`autostart_sidebar` 返回 `fallback: browser …`；热键那条走 `_spawn_sidebar → open_panel_window`）。
  构建侧加 `RollForward=LatestMajor`（.NET 7 已 EOL，8/9/10 也能跑）。**不要**再用"让用户装 .NET"
  当答案 —— 那是把可选外壳的成本转嫁给用户。
- **③ 配对失败先分型，再谈重试。** 安装器现在把服务端的 `detail` 原样取出来（以前只显示
  `HTTP 400`），并按 401/403（码一次性，重发）、429（限流 5 分钟）、证书/`fp`、Docker 网桥
  （`172.17-31.x` 同事连不上）、TCP 连不上分别给下一步。管理员串写 `http` 而实际是 HTTPS
  反代时，**同一地址自动再试一次 https（只升不降；401/403/429 不重试）**。
  顺带修了两处一直没生效的代码：`GET /api/settings` 是 `{settings:[{key,value}]}`（不是扁平字典）、
  `PUT` 要包 `values` —— 所以"配对后把「不出机」改成「内网」"以前从来没成功过。
- **④ 配对串的 scheme 不能只靠 `server.tls.certfile`。** TLS 在反代/网关上终结时后端是明文，
  旧逻辑永远发 `http://…`。现在 `server/ops.py::_published_scheme` 多了一层**这一次管理面请求的
  协议提示**（`X-Forwarded-Proto` → `Origin`/`Referer` → `request.url.scheme`）：
  **只升级到 https，不降级**；显式 `https://` / 显式 `http://` 仍然优先。管理面页面显示的地址
  与"此刻发码会用的地址"用同一个函数（否则页面说 http、发出来 https，更容易误判）。
  **交付里的 `ECHO-backend-portable-20261015` 是旧 `server/ops.py`（没有这层）**——发给同事前
  要重出后端包，否则服务端那一侧还是 http。