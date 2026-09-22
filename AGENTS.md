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
