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
