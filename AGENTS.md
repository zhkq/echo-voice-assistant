# AGENTS.md — 给 AI/协作者的长期约定与踩坑记录

## 必读：已知坑（务必牢记）

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

