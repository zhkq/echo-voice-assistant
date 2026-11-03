---
name: echo-install
description: 在**一台新机器**上安装/准备 ECHO（本地语音助理）：先跟用户确认要哪些组件，再装好主程序、运行时、写好设置并自检；客户端不再下载本地转写模型（指令/回顾/会议转写都走能力后端 Qwen3-ASR）。Windows 与 macOS 各有一套现成命令。当用户说"装 ECHO / 安装 ECHO / 帮我准备 ECHO 的环境 / 给同事装 ECHO"时使用。
whenToUse: 新机器首装 ECHO（Windows 或 macOS）；或把 ECHO 交给同事时，让**他那边的 agent** 照着做
---

# ECHO 安装准备

> 给**任何 agent** 用 —— 你不需要是 DSH。每一步都给现成命令；你负责**问清楚**、**照做**、**如实汇报**。

## 0. 这个技能怎么用（也是给同事看的三句话）

1. 资料夹里是**一个技能 + 一份已经解开的主程序**：`echo-install/`（本技能）与 `echo-core/`
   （主程序，不用再解压；Windows 上资源管理器里显示成 `echo-core\`）。
   更早出的包里那个目录叫 `ECHO\` —— **两个名字都认**（装机后代码落在 `<安装根>\echo-core`）。
   把整个资料夹给你的 agent。
2. 对你的 agent 说：**「按 echo-install 这个技能给我装 ECHO」**。
3. agent 会问你几个问题（装到哪、要哪些能力），然后自己下载安装。

**它不需要 git、不需要 GitHub**：代码来自资料夹里那个 `echo-core/`；依赖来自 **PyPI**；
客户端**不再下载任何转写/分离/唤醒模型**（2026-10-31 起转写全在能力后端）。
运行时按平台不同：**Windows** 从 python.org 取（嵌入包兜底），**macOS** 用 Homebrew 的 `python@3.11`。

### 0.1 快路：一条命令（固化脚本，不绕 agent）

上面那条"交给 agent 逐步做"的路**继续有效**；但如果只求**装得快**（同事抱怨的就是这个：
每步联网 + 每步确认），资料夹里有把第 1–4 节固化下来的入口：

```powershell
# ① 离线最小包（ECHO-kit-min-*.zip）：**双击 装我.cmd 就行**；等价的命令行是
powershell -NoProfile -ExecutionPolicy Bypass -File "<kit>\install-offline.ps1" -Root D:\ECHO
# ② 在线工具包（ECHO-kit-*.zip）：**双击 kit 根的 装我.cmd**
#    （它只问一句"装到哪个目录"，然后调下面这条；资料夹里有 bundle\ 就自动加 -Offline）
powershell -NoProfile -ExecutionPolicy Bypass -File "<kit>\echo-core\scripts\install-all.ps1" -Yes
```

它一次走完：**就位主程序 → 建运行时 → 装核心依赖 → 装组件 → 打印面板地址**。
其中"装组件"这一步**仍然调本技能里的
`echo-install-components.ps1`**（引擎/模型/设置/自检只有这一份实现，两边共用）。
参数：`-Profile minimal|main`、`-Root <安装根>`、`-Agent none|dsh`、`-Yes`、`-Offline`。
Windows 多一个（2026-11-03）：`-DesktopInstallDir <目录>`（官方桌面端装到哪；留空=官方默认位置）。
旧的 `-HarnessSource` / `-DshDir` / `-AskDsh` / `-DshVersion` / `-PinHarnessCommand` 已退役，
只保留参数以免旧命令行报错（传了只警告、不生效）。

* **离线包不需要联网、也不需要 agent**：依赖来自包里的 `bundle\wheels`（pip 带
  `--no-index --find-links`），运行时兜底是 `bundle\runtime\python-3.11.9-embed-amd64.zip`
  （目标机没有 Python / uv 时用它）。**包里不再带任何转写模型** —— 2026-10-31 起客户端
  已不做本地转写（指令/回顾/会议都交给能力后端 Qwen3-ASR）。
* 离线包里 **`-Agent` 默认 `none`** —— 包**不含 DSH**（官方桌面端安装包约 277 MB，不随
  ECHO 交付包分发）。要会议纪要 / 归档 / 语音指令的智能体时：**先装官方 DeepSeek Harness
  桌面端**（或让管理员预装），安装器会自动找到它自带的
  `resources\runtime\cli\bin\dsh.cmd`；离线安装不联网，没有官方桌面端就明确报"需要先装
  官方桌面端"，不会假装装好了。
* **装完客户端只差一件事：配对能力后端**（第 3 步）。客户端默认档不再装
  sherpa/SenseVoice/whisper/唤醒词/分离模型；这些活全在后端那台 GPU 机器上做。

> 两条路装出来的**东西是一样的**（同一个组件脚本、同一套设置键、同一份自检）。
> 快路只是把"读 SKILL.md → 逐步确认 → 逐步联网"换成"一条命令跑到底"。

## 1. 先跟用户确认（必须问，不要替他决定）

| 要问什么 | 推荐 | 为什么问 |
|---|---|---|
| **装到哪个目录** | Windows `D:\ECHO`（没 D 盘就用空间最大的盘）；macOS `~/ECHO` | ⚠ **必须纯英文路径**，别放桌面/中文/OneDrive/iCloud —— 旧版转写引擎读中文路径会出问题；现在虽不转写，录音/数据目录仍建议英文 |
| **能力后端怎么来**（三选一：配对别人的 / 本机自己跑 / 先跳过） | 有 GPU 后端就**配对**（管理员给一次性配对串）；没有就「先跳过」以后在面板配 | 2026-10-31 起**指令、回顾、会议转写全走能力后端（Qwen3-ASR）**；没有后端就没有任何语音转写 |
| **模型 / 会议文件 / 笔记库放哪** | 留空=默认（都在安装目录下） | 填了「笔记库」就会把纪要归档到那儿（通常是你已有的 Obsidian 库） |

**客户端不需要选转写方式/唤醒词/说话人分离/显卡加速**（2026-10-31 取消）：

| 项 | 现在的口径 |
|---|---|
| 本地转写引擎/模型（sherpa / whisper / SenseVoice / qwen3asr） | **不装**。转写全在后端 GPU 上做（Qwen3-ASR），客户端安装包从 ~430 MB 降到 ~55 MB 量级 |
| 语音唤醒（喊一声就开始） | **已取消**。入口改成热键 / 媒体键 / 面板 🎤 / 手机「按住说话」 |
| 说话人分离 / 声纹嵌入 | **后端做**（pyannote + wespeaker）；声纹库和比对留在本机 |
| 显卡加速（CUDA torch） | **客户端不需要**（不再跑本地模型） |

**「能力后端」是硬前提**：没有配对后端时，指令/回顾/会议转写都会明确报
「能力后端不可用」，而不是静默降级到本地（本地引擎已经退役）。

**装完还会建两个 DSH 分组**（这一步不用你操心，脚本自己调
`POST /api/dsh/workspaces/ensure`）：

| 分组 | 目录 | 说明 |
|---|---|---|
| 「会议空间」 | `{ECHO}/data/meetings`（设置 `meetingWorkspace`） | 每场会议一个会话 |
| 「指令空间」 | `{ECHO}/data/command`（设置 `commandWorkspace`） | 默认命令 / 语音指令会话 |

DSH 的 `workspace/create` **只收目录**、分组名由目录名派生，所以中文名是建好之后再
`workspace/rename` 的；**用户自己改过名字的工作区一律不动**。名可在设置里改
（`meetingWorkspaceTitle` / `commandWorkspaceTitle`）。不建也不影响功能 ——
ECHO 建会话时还会再建一次，只是那时才出现在侧栏。

**智能体（会议纪要 / 归档 / 语音指令靠它）—— 只有一套 DSH**：官方 **DeepSeek Harness 桌面端**
自带的那个无头 `dsh`（`%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd`）。
**不需要 Node.js，也不需要 npm**；标准版 harness（npm 安装 + 独立家目录）已彻底退役。

1. 安装器先看官方桌面端装没装；**已装就直接用**（它正在运行也不覆盖，继续用现有 CLI）。
2. 没装就**从官方地址下载最新 Windows 安装包**（`https://download.deepseek.com/desktop/
   dsh-latest-windows-x64.exe`，约 277 MB），校验 Authenticode 签名后 `/S` 静默安装；
   安装包**不进 ECHO 交付包**，所以每次安装/升级拿到的都是官方最新版。
3. 桌面端正在运行时，NSIS 安装器会拒绝覆盖（exit 2）——脚本会提示先退出，并**继续用现有 CLI**。
4. `-Offline` / 升级模式不下载：只认已装官方桌面端，没有就**明确报错**（不假装装好了）。

> DSH 的家目录固定是 `%USERPROFILE%\.dsh`（官方桌面端自己的那份），ECHO **不另建**，也不写
> `harnessCommand` / `harnessHome` / `harnessPort` / `harnessToken` / `agentHarnessEnabled`。
> 设置里只写 `agentBackend=dsh` 和 `dshAutoStart=true` —— 后者控制"ECHO 启动时自动拉起 DSH"，
> 默认开。ECHO 用 `POST /api/control/dsh/start` 拉起它（桌面端已在跑就用它，否则起无头 dsh），
> 然后轮询 `/api/status` 的 `components` 里 `dsh` 到 `online`（上限约 180 秒，超时如实说"还在起"）。
> 面板里的「打开 DSH 界面」可以跳到它。
> 安装位置默认官方位置；要装到别处用 `-DesktopInstallDir <目录>`。

## 2. 先探测，再动手（30 秒）

**第一步永远是认平台** —— 两边的安装命令完全不同：

```bash
uname -s          # Darwin = macOS；MINGW*/MSYS* = Windows 上的 Git Bash（按 Windows 走）
```

### Windows

```powershell
# 磁盘（挑一个剩余空间 ≥ 5 GB 的盘）
Get-PSDrive -PSProvider FileSystem | Select-Object Name,@{n='FreeGB';e={[int]($_.Free/1GB)}}

# 有没有可用的 Python / uv（决定运行时怎么建；都没有也没关系，会走 python.org）
foreach ($c in 'uv','py','python') { $p = Get-Command $c -ErrorAction SilentlyContinue; "$c -> $(if($p){$p.Source}else{'没有'})" }

# 三条下载通道（本技能只依赖这三个，**不依赖 GitHub**）
foreach ($u in 'https://pypi.org/simple/','https://www.python.org/','https://www.modelscope.cn') {
  "$u -> " + (curl.exe -s -o NUL -w "%{http_code}" -L --connect-timeout 6 -m 15 $u)
}

# 智能体只有一套：官方 DeepSeek Harness 桌面端自带的 dsh —— **不需要 Node.js / npm**。
# 这里只探一下官方桌面端在不在（没有的话第 3 步会下载安装）。
$dshCli = Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd'
"DSH 桌面端 -> " + $(if (Test-Path $dshCli) { $dshCli } else { '没有（第 3 步会下载官方安装包装上）' })
# 边条（右缘浮动条）才要 .NET Desktop Runtime；没装时 ECHO 会**自动改用浏览器面板**
# （不会弹「You must install .NET」）。
dotnet --list-runtimes 2>$null | Select-String 'WindowsDesktop.App'

# VC++ 2015-2022 运行库：torch / ctranslate2 / sherpa-onnx / onnxruntime 这些**原生扩展**都要它。
# 干净镜像上常常没有，而报错只说 "DLL load failed … 找不到指定的模块"（同事 2026-09-21 卡在这）。
$vc = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
"VC++ 2015-2022 x64 -> " + $(if ($vc -and $vc.Installed -eq 1) { $vc.Version }
  else { '没有（第 3 步会自动下载安装，会弹 UAC；装不了就手动装 https://aka.ms/vs/17/release/vc_redist.x64.exe）' })
```

### macOS

```bash
# 系统版本（要求 14.0+）与芯片
sw_vers -productVersion; uname -m

# 磁盘空间（安装目录所在盘 ≥ 5 GB）
df -h "$HOME" | tail -1

# Homebrew 是 mac 侧运行时的唯一来源；没有就先装（脚本会提示）
command -v brew && brew --version | head -1 || echo "没有 Homebrew —— 见 https://brew.sh"

# 可选：Xcode Command Line Tools —— 只在你想用**原生浮动条**时才需要（没有就用浏览器面板）
xcrun --find swiftc 2>/dev/null || echo "没有 swiftc（浮动条会跳过编译）"

# 智能体要用 Node
command -v node && node -v || echo "没有 Node"

# 下载通道（同样不依赖 GitHub）
for u in https://pypi.org/simple/ https://www.modelscope.cn https://hf-mirror.com; do
  printf '%s -> ' "$u"; curl -s -o /dev/null -w '%{http_code}\n' -L --connect-timeout 6 -m 15 "$u"
done
```

把结论告诉用户（尤其："哪条路能用""智能体要不要 Node"—— Windows 不需要，macOS 仍需要），再往下走。

## 3. 装（都可重跑）

资料夹里 `echo-core/`（更早的包里叫 `ECHO/`）是**已经解开的主程序** ——
所以这一步不再解压，只是把它放到安装目录。

### Windows ① 装主程序 + 准备运行时

> **`install.ps1` 就在 `echo-core\scripts\` 里**（资料夹解开后它就在那儿；更早的包里是
> `ECHO\scripts\`），不是散在资料夹根上。
>
> **务必用下面这种 `powershell -File …` 的形式**（`-ExecutionPolicy Bypass` 是关键）。
> 有同事在**已经打开的 PowerShell 会话里**直接 `& '…\install.ps1'`，而那台机器默认
> `ExecutionPolicy = Restricted` —— 脚本**一行都不执行**，退出码还是 **0**、日志文件也不生成，
> 表现得像"跑了 5 秒，什么都没发生"，很难查（2026-09-22 同事实测 2.1）。
> 非要在当前会话里跑，就先 `Set-ExecutionPolicy -Scope Process Bypass -Force`。

```powershell
$kit = 'C:\资料目录'          # 解开资料夹后的目录（里面有 echo-core\（老包 ECHO\）和 echo-install\）
powershell -NoProfile -ExecutionPolicy Bypass -File "$kit\echo-core\scripts\install.ps1" `
    -DestDir 'D:\ECHO' -Silent
# 加 -PipIndex https://pypi.tuna.tsinghua.edu.cn/simple  可换国内 pip 镜像（慢就用它）
```

脚本看到代码目录同级有 `manifest.json`，就知道这是"已解开的包"，直接复制（约 10 MB，秒级）。
若代码目录**已经在**你要装的位置（例如把资料夹直接解压到了 `D:\`、得到 `D:\echo-core`），
把 `-DestDir` 指成安装根即可，脚本会跳过多余的复制。

**退化路径**：如果手上只有 `ECHO-main-*.zip`（没解开），先解一层再跑同样的命令：

```powershell
Expand-Archive -Path "$kit\ECHO-main-win-x64-*.zip" -DestinationPath $kit -Force
# 之后同上：-File "$kit\ECHO\scripts\install.ps1" ...（主包 zip 里那层就叫 ECHO\）
```

主包**不含运行时**。`install.ps1` 会按三级降级找 CPython：
`uv venv --python 3.11` → `py -3.11 -m venv` → **python.org 嵌入包 + get-pip**（约 11 MB，只依赖 python.org）。
前两级都要从 GitHub 资产拉 CPython（`objects.githubusercontent.com`）—— **公司网必失败**，
所以第三级才是内网的正解：**全程不需要 GitHub**。基础依赖约 100 MB，几分钟。

### Windows ② 按确认结果装组件

```powershell
$skill = 'C:\资料目录\echo-install'      # 你手上的技能目录（里面是 scripts\）
powershell -NoProfile -ExecutionPolicy Bypass -File "$skill\scripts\echo-install-components.ps1" `
    -DestDir 'D:\ECHO' `
    -Agent dsh `                        # 唯一一套 DSH（默认；不带也是它）
    -NotesDir 'D:\我的笔记库'            # ← 用户给了笔记库才加
```

> 用**显式路径**（如上），别用 `$PSScriptRoot`：那条命令是在**你自己的终端**里执行的，
> 内联运行时 `$PSScriptRoot` 是空的，会拼出 `\scripts\...` 这种不存在的路径。

### Windows ③ 后端（会议转写那台 GPU 机器）怎么来 —— **必须问用户，别替他决定**

装完客户端不等于会议能转写：转写默认走**能力后端**。装的时候（或装完）要问一句，
三个答案对应三种做法（2026-10-01 起 `install-all.ps1` 自己会问；**交互式控制台**里它会问，
脚本化跑要显式给参数）：

```powershell
# ① 用别人给的后端：贴配对串（管理员发的 echo://pair?host=…&code=…）
powershell -NoProfile -ExecutionPolicy Bypass -File "$kit\echo-core\scripts\install-all.ps1" `
    -Yes -Backend pair -BackendPair 'echo://pair?host=10.100.0.24:8900&code=XXXX'

# ② 本机自己跑（这台有 NVIDIA 卡）：把后端包放在 -BackendDir 指的目录里
#    —— 有 ECHO-backend-offline-*.zip（离线包）就**零下载**，只有薄包才会联网装依赖
powershell -NoProfile -ExecutionPolicy Bypass -File "$kit\echo-core\scripts\install-all.ps1" `
    -Yes -Backend local -BackendDir 'D:\交付目录'

# ③ 先不配（以后在面板「能力」页签里弄）
... -Yes -Backend skip
```

* **别替用户选**：后端决定"会议音频去哪"，是隐私档（`capabilityPrivacy`）的邻居问题 ——
  见「隐私」那一段。选①时脚本会顺带把 `none` 改成 `lan` 并说明，因为远端后端会被 `none` 挡下。
* **配对串是一次性的**：它换来的凭据落在 `{DATA}\backend.json`（配对**不写设置**）。
  过期/用过了就让管理员重发一张，别自己拼一个。
* **本机跑要判"运行时能不能用"，不是"`runtime\python.exe` 在不在"**：薄包只带解释器，
  依赖要靠"取运行时"那一步装。判据是 `import fastapi, uvicorn` 过（`app/backend_env.check_server_deps`）。
* **RTX 50 系（Blackwell，算力 12.0）要 cu128**（cu126 没有 sm_120 的 kernel：装得上、
  起得来、一跑模型就 CUDA 报错）。`app/backend_env.variant_for()` 已经按算力分档，别手改档位。

### macOS ① 建环境 + 启动

```bash
kit="$HOME/资料目录"            # 解开资料夹后的目录（里面有 echo-core/（老包 ECHO/）和 echo-install/）
dest="$HOME/ECHO"

# 环境：Homebrew 装 python@3.11 + portaudio → 建 venv → 装 mac/requirements-mac.txt
#       （它顺带会在有 swiftc 时编译原生浮动条；没有就跳过，用浏览器面板）
bash "$kit/echo-core/mac/setup_mac.sh"
```

⚠️ `setup_mac.sh` 是在**资料夹原地**建 venv 的（它按脚本位置定位仓库根）。所以 mac 上的顺序是
**先把那个代码目录放到最终位置，再跑 setup**：

```bash
mkdir -p "$(dirname "$dest")"
[ -d "$dest" ] || mv "$kit/echo-core" "$dest"   # 放到最终位置（同盘 mv 是秒级；老包是 $kit/ECHO）
bash "$dest/mac/setup_mac.sh"               # 建 venv + 装依赖（首次几分钟）
bash "$dest/mac/start_mac.sh"               # 后台启动，并打印面板地址
```

### macOS ② 按确认结果装组件

```bash
bash "$kit/echo-install/scripts/echo-install-components.sh" \
    --dest "$dest" \
    --agent harness \                    # 默认标准版
    --notes-dir "$HOME/我的笔记库"        # ← 用户给了笔记库才加
```

> 参数名与 Windows 版一一对应（`--agent` / `--models-dir` / `--meetings-dir` / `--notes-dir`
> / `--pip-index`），只是写法从 `-PascalCase` 变成 `--kebab-case`。`--engines` / `--wake`
> / `--diarize` 已降级为**警告并忽略**（2026-10-31 客户端不再装本地 STT/唤醒/分离模型）。
> 脚本自带 `--help`。

两边这个脚本都做四件事：装核心 **pip 依赖** → **准备智能体** → 起服务 → 写设置 → **自检并登记**。
2026-11-03 起 **Windows 只有一套 DSH = 官方 DeepSeek Harness 桌面端自带的 dsh**（已装直接用，
没装就从官方地址下载最新安装包静默安装；**无需 Node.js / npm**；不写任何 `harness*` 设置）。
**macOS 这轮不改**：仍走本地 npm（`<安装目录>/harness/dsh`，冷启动约 10 秒；装不上回退 npx）。
**不再下载任何转写/分离/唤醒模型**（客户端不做本地转写；那些都在能力后端）。**幂等**：已经装好的
会跳过，重跑安全。

> **Windows 安装不再问"这台机器上已经有 DSH 吗"**（2026-11-03）：只有官方桌面端这一套程序
> 来源 —— 装了就复用，没装就下载安装（约 277 MB，**不放进 ECHO 交付包**，因此升级时重跑即拿
> 官方最新版）。旧的 `-DshDir` / `-AskDsh` / `-HarnessSource` / `-DshVersion` /
> `-PinHarnessCommand` 参数已退役（传了只警告、不生效）。桌面端正在运行时安装器不会覆盖，
> 会继续用现有 CLI。

> 每一步的输出也会落到 **`<安装目录>/data/logs/install-<时间戳>.log`**（独立于终端）——
> 终端被关掉、或被 agent 收走输出时，排障看这份日志就够（A6）。
>
> **给 agent：如果你的执行环境会中途回收后台进程** —— 有同事的沙箱约 2 分钟杀一次，
> 而且**连脚本派生出来的 ECHO 服务也一起带走**，组件脚本因此断过两次（断在 pip 中途、
> 断在 pip 安装中途时）。**断在中途就直接重跑同一条组件命令**，脚本是幂等的：
> 已装好的会跳过（依赖秒过、官方面板/DSH 已就绪也直接复用），重跑即续上。
> 断点看 **`<安装目录>/data/logs/install-<最新时间戳>.log`** 的最后几行。
> 真人双击安装不受影响（不会有人两分钟杀一次进程）。
>
> **另一条沙箱专属的坑**：这类环境里没有控制台，`install.ps1` 收尾那步"拉起 ECHO"
> 会失败并报 `ERROR_NO_DATA (0x800700E8)`「管道正被关闭」（`-Silent` 也会走到那一步）。
> **那是启动方式的问题，不是安装的问题** —— 看 `/api/install/state` 是 `ready: true`
> 就说明装好了，别重装。（新版 `install.ps1` 已不会因此把安装判成失败；旧包里会打印
> 「安装中断」+ 退出码 1，见到时按这条判断。）

> **别再装"标准版 harness"**（2026-11-03 起退役）：不再有 `<安装目录>\harness\dsh`、不再有独立
> `DSH_HOME`、不再要求系统 Node.js，也不再写 `harnessCommand` / `harnessHome` / `harnessPort` /
> `harnessToken` / `agentHarnessEnabled`。DSH 的家目录固定是 `%USERPROFILE%\.dsh`（官方桌面端
> 自己的那份）。程序来源只有一个：官方桌面端自带的 `resources\runtime\cli\bin\dsh.cmd`。
>
> **升级 DSH**：官方桌面端自带自动更新。要让安装器重新下载 / 静默安装最新版，先退出桌面端，
> 再给组件脚本加 `-UpgradeDsh`（双击安装会在升级时问一句"要不要顺便升级 DSH"）。
> 只换程序文件，**智能体配置一个字不改**；桌面端在跑时安装器不会覆盖，会继续用现有 CLI。
> 面板里的「打开 DSH 界面」可以跳到它；`dshAutoStart`（默认开）控制"ECHO 启动时自动拉起 DSH"。
> **macOS 这轮不改**：仍用 `harness-install-local.sh --upgrade`（升级标准版，见脚本 `--help`）；
> Windows 不再走这条，只升级官方桌面端。


三个要点（都是 2026-09-21 实测踩出来的）：

- **装完会 `POST /api/install/report` 登记**。这是"装完了"的凭据：面板据此**不再提示"还没装完"、
  也不再自动进向导**。没登记的话，用户打开面板会看到一条"这台机器还没登记安装完成"的横幅。
- **自检以 `import` / 服务可用 / 智能体 online 为准**（客户端没有本地模型可查），任何一项没成就**非 0 退出**并列出是哪几项 ——
  别把"pip 返回 0"当成"装好了"（uv 建的 venv 没 pip，依赖一个没装，安装器却打印了"安装完成"）。
- **失败会显形**：pip 依赖装不上会打印**服务给的原因**，不会再出现"一直排队中"这种含糊状态。
  客户端已不再有"本地转写引擎/模型"这一类故障（2026-10-31 起全走后端）。

## 4. 验收（必须做，并如实汇报）

Windows：

```powershell
$port = [int](Get-Content 'D:\ECHO\data\echo-port.txt' -Raw).Trim()
(Invoke-RestMethod "http://127.0.0.1:$port/api/status").components | Select-Object name,status
(Invoke-RestMethod "http://127.0.0.1:$port/api/capability").backends | Select-Object name,ready,baseUrl
```

macOS（**注意数据根不一定是安装目录**：全新安装时是 `~/Library/Application Support/ECHO`）：

```bash
port="$(cd "$dest" && ./venv/bin/python -c 'from app import paths; print(paths.data_root())' \
        | xargs -I{} cat {}/echo-port.txt)"
curl -s "http://127.0.0.1:$port/api/status" | ./venv/bin/python -m json.tool | head -30
curl -s "http://127.0.0.1:$port/api/models" | ./venv/bin/python -m json.tool | head -40
```

然后**用三句话说清**（别只说"装好了"）：

- **能干什么**：录音转文字（能力后端 Qwen3-ASR）、会议录音 + 文字稿、面板地址（Windows 还有 `Ctrl+Shift+E`）
- **还不能干什么**：没配对能力后端就没有任何语音转写；**会议纪要**还需要官方 DeepSeek Harness
  桌面端（它自带的 dsh 由 ECHO 拉起；`dshAutoStart` 默认开）
- **以后怎么补**：面板 → 能力（配对后端 / 选智能体），随时补，不用重装

## 5. 常见失败

| 现象 | 怎么处理 |
|---|---|
| **Windows**：启动时弹 **「You must install or update .NET」** | 那是**右缘浮动条**要 .NET Desktop **Runtime**（与 ECHO 本体无关）。**2026-10-21 起新版会自动判**：没装运行时就不起边条、直接用浏览器面板，不再弹这个框。旧包还能见到时：浏览器打开 `http://127.0.0.1:<端口>/` → 设置 → 面板 → **仪表盘打开方式 = browser**，并把「启动时自动显示折叠条」关掉（不是必须装 .NET） |
| **安装路径**：输入 `C:\echo` 报「路径有问题」 | 2026-10-21 起入口会先去掉引号和结尾反斜杠（Explorer「复制为路径」给的是 `"C:\ECHO\"`，尾反斜杠会把后面那个参数的引号转义掉）。仍报错时确认：盘符开头的绝对路径、纯英文、**不要带引号**；`C:\` 根目录在受管电脑上可能没写权限，换 `D:\ECHO` |
| **安装器里配对失败**（http / https 都不行） | 看它现在给的分类：`401/403` = 码已被用掉/过期（配对码**一次性**，让管理员重发）；`Docker 网桥` = 公布的地址是容器内网（管理面「对外公布地址」填同事能访问的 IP/域名）；`证书/fp` = 串里的指纹与现在这张证书不一致；`限流` = 等 5 分钟。新版还会把 http 串自动按 https 再试一次（TLS 在反代上时） |
| **客户端**再见到 `import torch` / `sherpa_onnx` 报错 | 2026-10-31 起客户端不再装这些原生扩展（本地转写已退役）——这类报错只会在能力后端那台机器出现，按后端部署文档处理 |
| **Windows**：弹 **「WebView2 初始化失败」** | 缺 Edge WebView2 Runtime（Win10/11 一般自带）；同样可改用浏览器面板 |
| **macOS**：提示「来自身份不明的开发者」/ 打不开 | **正常现象**：还没做签名与公证。右键 →「打开」，或去「系统设置 → 隐私与安全性」里允许一次 |
| **macOS**：热键按了没反应 | 要在「系统设置 → 隐私与安全性 → **辅助功能 / 输入监控**」里给终端（或 ECHO）授权 —— **首次必须人工点**，脚本代替不了 |
| **macOS**：麦克风没声音 | 同样在「隐私与安全性 → 麦克风」里授权；授权弹窗里显示的应用名可能是 `python`/`终端`，这是过渡实现（原生宿主还没做完） |
| **macOS**：浮动条没出现 | 需要 Apple Command Line Tools（`xcode-select --install`）后跑 `bash mac/build_sidebar.sh`；**没有也能用**：浏览器打开面板即可 |
| **macOS**：`brew` 找不到 | 先装 Homebrew（https://brew.sh），再重跑 `mac/setup_mac.sh` |
| **macOS**：客户端不再装 `funasr`/`torch` | 本地转写已退役；mac 客户端只装服务/面板/音频 IO（安装包 ~58 MB 量级） |
| 智能体没起来（纪要/归档/指令不能用） | 只有一套 DSH：官方 **DeepSeek Harness 桌面端自带的 dsh**。先看 `GET /api/status` 的 `components` 里 `dsh` 的状态与 detail；`POST /api/control/dsh/start` 会拉起它（桌面端已在跑就用它，否则起无头 dsh），等 `online` 上限约 180 秒，超时就是"还在起"。没有官方桌面端时重跑第 3 步的组件脚本（`-Agent dsh`）—— 它会下载并静默安装官方桌面端。**不需要 Node.js / npm**；装好 ECHO 会随启动自动拉起 DSH（`dshAutoStart`），面板「打开 DSH 界面」可跳到它 |
| 装官方桌面端时报「已有程序在运行」/ NSIS exit 2 | 桌面端正在运行，安装器拒绝覆盖。先退出 DeepSeek Harness（托盘也退），再重跑；不想退出也行 —— 脚本会**继续用现有 CLI**，只是不升级程序文件 |
| 改设置报 `422 … ["body","values"] Field required` | `PUT /api/settings` 的 body 必须包一层：`{"values": {"agentBackend": "dsh"}}`；`agentBackend` 写 **`dsh`**（不是组件名 `agent-dsh`，也不是已退役的 `harness`）。要控制"随 ECHO 启动"另外写 `dshAutoStart`（默认 true）；`harnessCommand` / `agentHarnessEnabled` 已退役，别写 |
| `pip` 慢 / 超时 | Windows 加 `-PipIndex https://pypi.tuna.tsinghua.edu.cn/simple`；mac 加 `--pip-index https://pypi.tuna.tsinghua.edu.cn/simple`，重跑（已装好的会跳过） |
| 客户端模型下载 | **已退役**：客户端不再下载任何转写/分离/唤醒模型；模型都在能力后端。面板「模型」页为空是正常的 |
| **说话人分离**不可用 | 现在由**能力后端**做（pyannote + wespeaker）；客户端不装分离模型。先查后端 `/v1/capabilities` 是否有 `diarize.turns`，以及配对 scopes 是否含 `diarize` |
| 会议转出来是空的（`（未检测到有效语音）`）或整场没有说话人 | 客户端已不做本地转写：先查①能力后端是否配对/在线、`/v1/capabilities` 里 `asr.long` 是否为 ready；②配对 scopes 是否含 `asr` / `diarize`；③后端日志有没有 `model_failed` / `busy`。失败原因也写进 `logs` 表与 `data\logs\echo-server.log.err` |
| 客户端 whisper 相关报错 | 客户端已不装 whisper（本地转写退役）；在后端那台机器排查 |
| 后端 `/v1/asr` 报 `qwen-asr package is required for Qwen3-ASR` | 那是**后端**那台缺 `qwen-asr`（客户端不装）。在后端镜像/环境里装 `qwen-asr==0.0.6` + `transformers==4.57.6`；别只升 transformers：5.x 上 qwen-asr 0.0.6 import 不了 |
| 老机器磁盘不够 | 客户端不再装模型：安装包 ~55 MB、装完约 200 MB（运行时 + 代码）；模型空间在能力后端那台 |
| **macOS**：脚本报 `$'\r': command not found` | 说明 `.sh` 被传成了 CRLF（Windows 上解压/复制过的痕迹）。用 `bash` 跑之前先 `perl -pi -e 's/\r$//' 文件` 或重新从 zip 解一次 |

## 6. 别做的事

- **不要把两边的命令混用**：macOS 上**没有** `install.ps1`/`-DestDir` 那套（那是 Windows 专属：
  .NET/WebView2 边条、Windows 嵌入包 CPython、注册表快捷方式）；Windows 上也没有 `mac/setup_mac.sh`。
  先认平台，再挑一套。
- **不要**让用户去 `git clone`（公司网多半连不上 GitHub）—— 代码只用资料夹里的
  `echo-core/`（更早的包里叫 `ECHO/`）。
- **不要**替用户装 CUDA/torch：体积大、和驱动绑定，让他们按需自己决定（mac 上更是没有 CUDA）。
- **不要**把 `agentBackend` 写成组件 id（`agent-dsh`）—— 那是**组件名**，设置里要用的名字是
  **`dsh`**（`dshAutoStart` 控制是否随 ECHO 启动）。写错了选择会**静默失效**（2026-09-20 踩过）。
  也不要再写 `harness` / `agentHarnessEnabled` / `harnessCommand`：标准版 harness 已退役。
- **不要**在没跑 `import` 复核前说"装好了"：`pip` 返回 0 或 `python.exe` 存在都不等于引擎能用
  （2026-09-21 踩过：uv 建的 venv 没有 pip，依赖一个都没装，安装器却打印了"安装完成"）。
