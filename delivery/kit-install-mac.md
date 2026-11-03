# ECHO for macOS 安装文档（给同事）

> 这是**详细版**。只想最快装起来：把解压出来的整个文件夹交给你的 AI 助手，
> 说一句「**按 echo-install 这个技能给我装 ECHO**」，它会把下面第 3 节的事全做完。
> 想自己掌控每一步，就照第 4 节敲命令 —— 两条路装出来的东西一模一样。

---

## 1. 五分钟了解

| 项 | 说明 |
|---|---|
| 它是什么 | 本地语音助理：录音转文字、会议录音 + 文字稿、语音指令、纪要归档 |
| 代码在哪 | 装到 `~/ECHO/echo-core`（**纯英文路径**，别放桌面/中文/iCloud 同步目录） |
| 装完多大 | 约 0.2 GB（主程序 + 运行环境；**不含任何转写模型**） |
| 数据在哪 | `~/ECHO/data`（录音、数据库、日志、端口文件都在这）。不传云端 |
| 谁在转写 | **能力后端**（一台装了 GPU 的机器）。客户端只负责录音/播放/界面 |
| 要联网吗 | 要（依赖从 PyPI 装）。**不需要 git，也不需要访问 GitHub** |

**最重要的一句话**：转写不再在你自己电脑上算。没配对「能力后端」之前，
指令、回顾、会议转写都会明确报「能力后端不可用」——这不是装坏了（见第 6 节）。

---

## 2. 安装前确认（1 分钟）

| 要求 | 怎么确认 | 不满足怎么办 |
|---|---|---|
| macOS **14.0 或更新** | `sw_vers -productVersion` | 更老的系统没测过，别装 |
| 已装 **Homebrew** | `brew --version` | 先装：https://brew.sh（官方脚本，一次就好） |
| 磁盘 ≥ 5 GB 空闲 | `df -h ~` | 清点空间 |
| 能上网 | `curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/` | 换网络/代理 |
| （可选）Node.js | `node -v` | 只有要用**智能体**（纪要/归档/语音指令）才需要，见第 7 节 |

---

## 3. 装法 A：交给 AI 助手（推荐）

1. 双击解压 `ECHO-kit-macos-*.zip`，得到一个文件夹（里面有 `echo-core/`、`echo-install/`、`先读我.md`）。
2. 把这个文件夹**整个**交给你的 AI 助手（Claude Code / Cursor / DSH 都行）。
3. 说：「**按 echo-install 这个技能给我装 ECHO**」。
4. 它会问你几件事，照着选：

   | 它会问 | 怎么答 |
   |---|---|
   | 装到哪个目录 | `~/ECHO`（推荐；必须纯英文） |
   | 能力后端怎么来 | 有管理员给你的配对串就选「配对」，没有就选「先跳过」（见第 6 节） |
   | 模型 / 会议文件 / 笔记库放哪 | 不确定就留空（都用默认）；有 Obsidian 库可以填笔记库 |
   | 要哪些能力 | 会直接告诉你：客户端不需要选转写引擎/唤醒词/显卡加速（都已取消） |

5. 装完它会把面板地址打给你，并做一次自检。**它说「装好了」之前别自己去点面板**。

---

## 4. 装法 B：自己敲命令

把下面的 `$KIT` 换成你解压出来的那个文件夹路径（例如 `~/Downloads/ECHO-kit-macos-<时间戳>`）。

```bash
KIT="$HOME/Downloads/ECHO-kit-macos-20261102-1513"   # ← 换成你的实际目录
DEST="$HOME/ECHO"                                    # ← 安装根（纯英文）

# ① 把主程序放到最终位置。setup 是在「原地」建环境的，所以先搬再装。
mkdir -p "$(dirname "$DEST")"
mv "$KIT/echo-core" "$DEST"

# ② 建环境：Homebrew 装 python@3.11 与 portaudio → 建 venv → 装依赖（首次较慢）
bash "$DEST/echo-core/mac/setup_mac.sh"

# ③ 启动（后台跑，打印面板地址）
bash "$DEST/echo-core/mac/start_mac.sh"

# ④ 装核心依赖 / 写设置 / 起智能体 / 自检登记（这一步才是「装完」的凭据）
bash "$KIT/echo-install/scripts/echo-install-components.sh" \
     --dest "$DEST" --agent harness
```

说明：

* `--agent harness` = 装标准版智能体（纪要/归档/语音指令用）。**没装 Node.js 会装不上**：
  先 `brew install node` 再重跑第 ④ 步；完全不用智能体就换成 `--agent none`。
* pip 慢就加：`--pip-index https://pypi.tuna.tsinghua.edu.cn/simple`（重跑安全，已装好的会跳过）。
* 每条命令都能**重跑**（幂等）；中途断了就原样再跑一次。
* 装完别关终端之前，先确认第 5 节的四样都对。

---

## 5. 装完先看这四样（验收）

```bash
# ① 服务活着吗（能看到 JSON 就活着）
cat ~/ECHO/data/echo-port.txt      # 真实端口（默认 8970）
curl -s "http://127.0.0.1:$(cat ~/ECHO/data/echo-port.txt)/api/status" | head -c 400

# ② 数据根到底在哪（3.0 布局是 ~/ECHO/data；老式扁平安装才是 ~/Library/Application Support/ECHO）
cd ~/ECHO/echo-core && ../venv/bin/python -c 'from app import paths; print(paths.data_root())'

# ③ 能力后端配对成功了吗（看 echo-server 的 ready 是不是 true）
curl -s "http://127.0.0.1:$(cat ~/ECHO/data/echo-port.txt)/api/capability" | head -c 800

# ④ 日志（出问题先看这里）
ls -lt ~/ECHO/data/logs/ | head
```

浏览器打开 `http://127.0.0.1:<端口>` 就是面板。装完面板**直接进仪表盘**（不会再把你塞进向导）。

三句话总结现状：

* **能干什么**：录会议 + 文字稿（走能力后端）、语音指令、每日回顾、（装了智能体后）自动写纪要；
* **还不能干什么**：没配对能力后端 = 没有任何转写；没装 Node/智能体 = 没有纪要；
* **以后怎么补**：面板 →「能力」页配对后端 / 选智能体，随时补，**不用重装**。

---

## 6. 必须做：配对能力后端

2026-10-31 起客户端**不再装任何本地转写模型/引擎**（本地 STT 引擎、唤醒词、说话人分离模型全部退役），
转写统一在那台 GPU 后端的 Qwen3-ASR 上做。所以：

| 情况 | 做法 |
|---|---|
| 管理员给了你配对串（`echo://pair?host=…&code=…`） | 面板 →「能力」→ 配对，粘贴整串；或安装时选「配对」 |
| 没有配对串 | 面板 →「能力」→ 复制本机的「配对申请」，发给管理员换一张码 |
| 本机就想自己跑后端 | 需要 NVIDIA GPU 的机器（本机另一台或这台 Linux 工作站），按后端部署文档起，再配对 |

注意事项：

* **配对码是一次性的**：用掉/过期就找管理员重发；换后端 = 换密钥 = **必须重新配对**。
* 配对成功后 `GET /api/capability` 里 `echo-server` 的 `ready` 是 `true`；
  `false` 且带 `unauthorized` = 配对还指着别的/旧的后端。
* 用 **ssh 隧道**把远端后端映射到本机时，请把面板「允许音频去哪」改成**内网** ——
  程序只按地址判「是不是本机」，分不出「真本机」与「本机上的隧道」。
* 配对后若会议转写仍失败：先看内容根下 `logs/` 与后端日志里的 `busy` / `model_failed`，
  两者含义不同（`busy` = 同一人已有请求在跑，稍后重试即可）。

---

## 7. 智能体（纪要 / 归档 / 语音指令）与 Node

* 智能体 = 本机跑一个 DSH 标准版，**需要 Node.js**：`brew install node`。
* 安装脚本会把它装到 `~/ECHO/dsh/app`（3.0 布局），并让 ECHO 直接拉起（冷启动约 10 秒）。
* 装不上时脚本会如实报错（常见原因：没 Node / npm 源不通 / 上次装残）。
  修法：`brew install node` 后重跑第 ④ 步；还不行就加 `--from-cache` 走 npx 缓存复制。
* 不装智能体也能录音、转写、看文字稿、用面板 —— 只是不会自动出纪要。

---

## 8. 系统授权与首次启动（这几步必须人工点）

| 现象 | 怎么处理 |
|---|---|
| 「来自身份不明的开发者」/ 打不开 | **正常**（还没做签名与公证）。右键 →「打开」，或去「系统设置 → 隐私与安全性」允许一次 |
| 第一次录音弹麦克风授权 | 点允许；弹窗里显示的应用名可能是 `python` / `终端`，这是过渡实现，不是装错 |
| 全局热键没反应 | 「系统设置 → 隐私与安全性 → **辅助功能** / **输入监控**」里给终端（或 ECHO）打勾；首次必须人工点 |
| 右侧浮动条没出现 | 需要 Apple Command Line Tools：`xcode-select --install` 后跑 `bash ~/ECHO/echo-core/mac/build_sidebar.sh`；**不编也能用**，浏览器开面板即可 |
| 面板显示「模型 0/0」或空的 | **正常**：客户端不装模型，模型都在能力后端那台机器上 |

---

## 9. 日常使用

```bash
bash ~/ECHO/echo-core/mac/start_mac.sh     # 启动（已在跑会提示 pid）
bash ~/ECHO/echo-core/mac/stop_mac.sh      # 停止
bash ~/ECHO/echo-core/mac/restart_mac.sh   # 重启
```

* 面板：`http://127.0.0.1:<端口>`，端口看 `~/ECHO/data/echo-port.txt`。
* 运行日志：`~/ECHO/data/logs/echo-mac.out`；安装日志：`~/ECHO/data/logs/install-<时间戳>.log`；
  智能体日志：`~/ECHO/data/logs/harness.log`。
* **重启/停止前先看面板上有没有正在录的会议**（`GET /api/status` 的 `meeting.active`）——
  录音期间重启会把那场会议记成「已中断」（音频留着，但当场断了）。
* 语音朗读默认走在线合成（edge-tts，会把**被朗读的那段文字**发到微软）；
  想全离线就在设置里把「语音合成引擎」换成离线引擎（macOS `say`）。

---

## 10. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `brew: command not found` | 先装 Homebrew（https://brew.sh），再重跑 `setup_mac.sh` |
| 脚本报 `$'\r': command not found` | 脚本被 Windows 传成了 CRLF。`perl -pi -e 's/\r$//' <文件>` 或重新解压 zip |
| pip 卡住/超时 | 加国内镜像重跑：`--pip-index https://pypi.tuna.tsinghua.edu.cn/simple` |
| `setup_mac.sh` 说找不到 Python 3.11 | `brew install python@3.11`，或 `brew upgrade` 后重跑 |
| 面板打不开 | 端口被别的东西占了：`cat ~/ECHO/data/echo-port.txt` 拿真实端口；仍不行看 `logs/echo-mac.out` |
| 提示端口被占 / 起不来 | 同机只跑一个后端；先 `bash ~/ECHO/echo-core/mac/stop_mac.sh` 清残留再启动 |
| 录音没声音 / 打不开麦 | 检查「隐私与安全性 → 麦克风」授权；关掉其它占麦的应用；实在不行重启 ECHO（别连点开麦） |
| 说话没反应（热键） | 先给它辅助功能授权；面板上的 🎤 按钮不依赖系统热键，可以直接试 |
| 装智能体时报 `'node' 不是内部或外部命令`（乱码也是它） | npm 的 install 脚本找不到 node：确认 `node -v` 能用、`brew install node` 后重跑第 ④ 步 |
| 装智能体报 `ETARGET …` | registry 侧依赖图事故：直接重跑一次；仍失败加 `--from-cache` 走 npx 缓存复制 |
| 转写一直不出字 | ① 麦克风授权？② 面板「能力」里后端 `ready` 是不是 true？③ 后端 scopes 里有没有 `asr`？ |
| 会议转出来是空的 | 同上；另外同一人的请求是串行的，上一条还在跑时要等（提示 `busy` 时不要狂点重试） |

---

## 11. 升级 / 重装 / 卸载

* **升级**：拿新的 `ECHO-kit-macos-*.zip`，解压后把新的 `echo-core/` 覆盖到 `~/ECHO/echo-core`
  （`data/`、`venv/`、`dsh/` 都不动）→ 重跑 `setup_mac.sh` → 重跑组件脚本第 ④ 步。
  升级前后配置与数据都留着。
* **只想重装运行环境**：`rm -rf ~/ECHO/venv` 后重跑 `setup_mac.sh`。
* **完全重来**：`bash ~/ECHO/echo-core/mac/stop_mac.sh`，然后删掉 `~/ECHO` 整目录即可，不留后台进程。
* 卸载**不会**自动删你的录音与数据库 —— 就在 `~/ECHO/data` 里，要留就拷走。

---

## 12. 出事时把这几样发回来（别自己改配置）

```bash
sw_vers; uname -m
bash ~/ECHO/echo-core/mac/setup_mac.sh 2>&1 | tail -40   # 或那次的完整输出
ls -lt ~/ECHO/data/logs/ | head
tail -200 ~/ECHO/data/logs/echo-mac.out
tail -100 ~/ECHO/data/logs/harness.log 2>/dev/null
curl -s "http://127.0.0.1:$(cat ~/ECHO/data/echo-port.txt)/api/status"
```

再说明一句「哪一步、期望什么、实际什么」，基本就能定位。**不要**自己改设置里的隐藏项
（`harnessCommand` 之类），改错了会静默失效，很难查。

---

### 附：这一层资料夹里有什么

| 文件 | 说明 |
|---|---|
| `ECHO-kit-macos-*.zip` | 客户端主包：解开是 `echo-core/`（主程序）+ `echo-install/`（安装技能）+ `先读我.md` |
| `先读我.md` | 三句话版说明（AI 助手那条路的主角） |
| `安装文档.md` | 就是本文 |
| `清单.txt` | 文件清单 + SHA256 校验值 |

这一层**不含**：Windows 的 `装我.cmd`、后端 GPU 包、手机 APK、Windows 运维脚本 —— 那些是
Windows 侧的交付物，Mac 用不上；后端在那台 GPU 机器上单独部署。
