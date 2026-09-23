# AGENTS.md — 给 AI/协作者的长期约定与踩坑记录

## 必读：已知坑（务必牢记）

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
