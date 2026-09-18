# macOS 测试清单（交给朋友照着做）

> 目的：在 macOS 14+ 上把 ECHO 跑起来，并回答计划里**只能在 mac 上验证**的四项
> （S7 能否验证 / S9 公证链 / S10 Carbon 热键 / S12 外挂运行时的 TCC 归属）。
> 遇到失败不用自己排查 —— 按第 4 节把材料发回即可。

## 0. 环境要求

- macOS **14.0+**（`sw_vers` 看版本）
- Python **3.11**（建议 `brew install python@3.11`）
- 网络（首次要装依赖、下模型）

## 1. 拿到代码

解压 `ECHO-public-1.0.0-*.zip`（约 2.6 MB，**只有代码**：app/ web/ mac/ scripts/ …；
模型与依赖按需装，不带 venv、不带模型权重）。

## 2. 装依赖并启动

```bash
cd <解压出来的目录>
bash mac/setup_mac.sh     # 建 venv + 装依赖（会打印每一步）
bash mac/start_mac.sh     # 启动 ECHO
```

面板默认 http://127.0.0.1:8970 —— **实际端口以 `data/echo-port.txt` 为准**。

## 3. 逐条记录（通过 / 失败 + 现象）

| # | 检查项 | 期望 |
|---|---|---|
| 1 | `sw_vers`、`python -V`、`uname -m` | 14+；3.11.x；记录 Intel / Apple Silicon |
| 2 | `bash mac/setup_mac.sh` 的输出 | 无报错（有报错请原文发回） |
| 3 | 面板能打开 | 页面可见，`/api/status` 能返回版本号 |
| 4 | **全局热键**（S10） | 按热键能触发 ECHO；**重点记录**：是否需要你去开「辅助功能」权限？ |
| 5 | **麦克风授权**（S12） | 首次录音是否弹授权；授权主体显示的是 **ECHO** 还是某个匿名可执行文件 |
| 6 | 录音 → 转写 → 纪要 | 录 10 秒，能得到文字；能生成纪要 |
| 7 | 新功能：设置页「**环境体检**」 | 四类根都显示出来；**DATA 应为 `~/Library/Application Support/ECHO`** |
| 8 | 新功能：「**组件**」页签 | 列表里**不该出现** CUDA 组件；`agent-dsh` 应出现（macOS 14+） |

## 4. 要回传的东西

- `sw_vers` / `python -V` / `uname -m`
- `bash mac/setup_mac.sh` 的**完整输出**
- `data/logs/` 下的日志（尤其 `echo-server.log`）
- 「环境体检」与「组件」页签的截图
- 失败项的现象（报错原文、截图）

## 5. 已知的、不是你问题的项（如实记录即可）

- **代码签名与公证还没做**（S9 未完成）：首次打开会有 Gatekeeper 提示，属预期现象。
- **常驻 helper / 边条 `.app` 的双击体验**（S7）：目前 mac 侧仍是 1.x 的"注入式"实现
  （`mac/run_mac.py` 往 `sys.modules` 里塞 mac 实现），把热键/边条做成签名原生宿主是
  **P3** 的活。所以第 4、5 项请**如实记录当前行为**，不要按"应该怎样"判断对错。
- ECHO 的正式 mac 交付形态（`.dmg` + 公证 `.app`）在 **P4**，这次测的是"源码能否跑通"。