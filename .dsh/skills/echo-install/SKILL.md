---
name: echo-install
description: 在**一台新机器**上安装/准备 ECHO（本地语音助理）：先跟用户确认要哪些组件，再装好主程序、运行时、按需下载模型（走国内镜像）、写好设置并自检。Windows 与 macOS 各有一套现成命令。当用户说"装 ECHO / 安装 ECHO / 帮我准备 ECHO 的环境 / 给同事装 ECHO"时使用。
whenToUse: 新机器首装 ECHO（Windows 或 macOS）；或把 ECHO 交给同事时，让**他那边的 agent** 照着做
---

# ECHO 安装准备

> 给**任何 agent** 用 —— 你不需要是 DSH。每一步都给现成命令；你负责**问清楚**、**照做**、**如实汇报**。

## 0. 这个技能怎么用（也是给同事看的三句话）

1. 资料夹里是**一个技能 + 一份已经解开的主程序**：`echo-install/`（本技能）与 `ECHO/`（主程序，
   不用再解压；Windows 上资源管理器里显示成 `ECHO\`）。把整个资料夹给你的 agent。
2. 对你的 agent 说：**「按 echo-install 这个技能给我装 ECHO」**。
3. agent 会问你几个问题（装到哪、要哪些能力），然后自己下载安装。

**它不需要 git、不需要 GitHub**：代码来自资料夹里那个 `ECHO/`；依赖来自 **PyPI**；
模型来自 **ModelScope / hf-mirror**（ECHO 内置 `HF_ENDPOINT=https://hf-mirror.com`）。
运行时按平台不同：**Windows** 从 python.org 取（嵌入包兜底），**macOS** 用 Homebrew 的 `python@3.11`。

### 0.1 快路：一条命令（固化脚本，不绕 agent）

上面那条"交给 agent 逐步做"的路**继续有效**；但如果只求**装得快**（同事抱怨的就是这个：
每步联网 + 每步确认），资料夹里有把第 1–4 节固化下来的入口：

```powershell
# ① 离线最小包（ECHO-kit-min-*.zip）：**双击 装我.cmd 就行**；等价的命令行是
powershell -NoProfile -ExecutionPolicy Bypass -File "<kit>\install-offline.ps1" -Root D:\ECHO
# ② 在线工具包（ECHO-kit-*.zip）：入口在主程序里（同一个脚本），加 -Offline 就是离线那条路
powershell -NoProfile -ExecutionPolicy Bypass -File "<kit>\ECHO\scripts\install-all.ps1" -Yes
```

它一次走完：**就位主程序 → 建运行时 → 装核心依赖 → 装组件 → 打印面板地址**。
其中"装组件"这一步**仍然调本技能里的
`echo-install-components.ps1`**（引擎/模型/设置/自检只有这一份实现，两边共用）。
参数：`-Profile minimal|main`、`-Root <安装根>`、`-Agent none|harness|dsh`、`-Yes`、`-Offline`。

* **离线包不需要联网、也不需要 agent**：依赖来自包里的 `bundle\wheels`（pip 带
  `--no-index --find-links`），流式模型来自 `bundle\models`，运行时兜底是
  `bundle\runtime\python-3.11.9-embed-amd64.zip`（目标机没有 Python / uv 时用它）。
* 离线包里 **`-Agent` 默认 `none`** —— 包**不含 DSH/harness**（`@deepseek-ai/dsh` 是私有
  npm 包，许可上不能随包分发）。要会议纪要 / 归档 / 语音指令的智能体时，仍走第 5 节那条
  （联网 + npm / npx 缓存）的路，装组件时用 `-Agent harness`。
* 想加唤醒词 / whisper 档 / SenseVoice / 说话人分离：那些**模型不随包**，装完在面板 →
  「能力」里按需下载即可（联网）。

> 两条路装出来的**东西是一样的**（同一个组件脚本、同一套设置键、同一份自检）。
> 快路只是把"读 SKILL.md → 逐步确认 → 逐步联网"换成"一条命令跑到底"。

## 1. 先跟用户确认（必须问，不要替他决定）

| 要问什么 | 推荐 | 为什么问 |
|---|---|---|
| **装到哪个目录** | Windows `D:\ECHO`（没 D 盘就用空间最大的盘）；macOS `~/ECHO` | ⚠ **必须纯英文路径**，别放桌面/中文/OneDrive/iCloud —— 转写引擎读中文路径会出问题 |
| **转写方式**（可多选，见下表） | **`sherpa`**（边听边出字，189 MB，**不需要独显**） | 这是唯一"装完立刻能用又不吃显卡"的一档 |
| **要不要唤醒词**（喊一声就开始） | 不要 | 要常开麦克风，有隐私成本；模型 40 MB |
| **要不要说话人分离**（会议里区分谁在说） | 不要（除非会议真的要区分谁在说） | 现在可一键装（ModelScope 匿名可下）；**使用条款请用户自行确认** |
| **要不要显卡加速 / 方言口音** | 不要 | Windows 要 N 卡（mac 没有 CUDA，见下）；方言档 3.6 GB，还得先有 torch |
| **模型 / 会议文件 / 笔记库放哪** | 留空=默认（都在安装目录下） | 填了「笔记库」就会把纪要归档到那儿（通常是你已有的 Obsidian 库） |

**转写方式怎么选**（体积是模型的，pip 依赖另算）：

| 选项 | 体积 | pip 依赖 | 说明 |
|---|---|---|---|
| `sherpa` | 189 MB | `sherpa-onnx` | **边听边出字**，CPU 实时，最省内存 —— 推荐 |
| `whisper-base` | 141 MB | `faster-whisper` | 更准的档位（D1 推荐组合：sherpa + base） |
| `whisper-tiny` / `small` / `medium` / `large-v3` | 75 / 464 / 1500 / 2950 MB | 同上 | 档位越高越准也越慢 |
| `sensevoice` | 896 MB | `funasr` + `torch`（**+约 2 GB**） | 中文短句标点最准，但拖 torch |
| `qwen3asr` | 3.6 GB | `qwen-asr` + `transformers==4.57.6` + `torch` | 方言/口音更强；Windows 需要 N 卡，mac 上很慢 |

**会议转写还额外要一把"时间戳骨架"**（`whisper-small` + `faster-whisper`，约 464 MB）：
`sensevoice` 自己不出时间戳，非得靠它对齐；`qwen3asr` 有 ForcedAligner，只在回退时才用。
**这套依赖漏了不会报错**，只会把一整场会转成"（未检测到有效语音）"的空文件 ——
所以选 `sensevoice` 时安装器会自动把 `whisper-small` 一起装上，别手工砍掉（2026-09-23 实测）。

**CUDA 版 torch 必须从 PyTorch 官方索引装**（`-AccelCuda` / `--accel-cuda`）：
PyPI 上的 Windows torch 是 **CPU 版**，装完 `torch.cuda.is_available()` 是 False，
表面"装上了"其实一直在跑 CPU。Windows 侧安装器现在会自动换索引（默认 cu128）。

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

**智能体（会议纪要 / 归档 / 语音指令靠它）**：**默认就用标准版**（`agentBackend=harness`），
**不用问用户**；只要本机有 Node.js，ECHO 会自己把它拉起来。没有 Node 就照第 5 节处理。

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

# 智能体要用 Node；边条要用 .NET 7 Desktop Runtime（没有也能用浏览器面板）
foreach ($c in 'node','npx') { $p = Get-Command $c -ErrorAction SilentlyContinue; "$c -> $(if($p){$p.Source}else{'没有'})" }
dotnet --list-runtimes 2>$null | Select-String 'WindowsDesktop.App 7\.'

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

把结论告诉用户（尤其："哪条路能用""要不要装 Node"），再往下走。

## 3. 装（都可重跑）

资料夹里 `ECHO/` 是**已经解开的主程序** —— 所以这一步不再解压，只是把它放到安装目录。

### Windows ① 装主程序 + 准备运行时

> **`install.ps1` 就在 `ECHO\scripts\` 里**（资料夹解开后它就在那儿），不是散在资料夹根上。
>
> **务必用下面这种 `powershell -File …` 的形式**（`-ExecutionPolicy Bypass` 是关键）。
> 有同事在**已经打开的 PowerShell 会话里**直接 `& '…\install.ps1'`，而那台机器默认
> `ExecutionPolicy = Restricted` —— 脚本**一行都不执行**，退出码还是 **0**、日志文件也不生成，
> 表现得像"跑了 5 秒，什么都没发生"，很难查（2026-09-22 同事实测 2.1）。
> 非要在当前会话里跑，就先 `Set-ExecutionPolicy -Scope Process Bypass -Force`。

```powershell
$kit = 'C:\资料目录'          # 解开资料夹后的目录（里面有 ECHO\ 和 echo-install\）
powershell -NoProfile -ExecutionPolicy Bypass -File "$kit\ECHO\scripts\install.ps1" `
    -DestDir 'D:\ECHO' -Silent
# 加 -PipIndex https://pypi.tuna.tsinghua.edu.cn/simple  可换国内 pip 镜像（慢就用它）
```

脚本看到 `ECHO\` 同级有 `manifest.json`，就知道这是"已解开的包"，直接复制（约 10 MB，秒级）。
若 `ECHO\` **已经在**你要装的位置（例如把资料夹直接解压到了 `D:\`、得到 `D:\ECHO`），
把 `-DestDir` 指成它自己即可，脚本会跳过多余的复制。

**退化路径**：如果手上只有 `ECHO-main-*.zip`（没解开），先解一层再跑同样的命令：

```powershell
Expand-Archive -Path "$kit\ECHO-main-win-x64-*.zip" -DestinationPath $kit -Force
# 之后同上：-File "$kit\ECHO\scripts\install.ps1" ...
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
    -Engines sherpa,whisper-base `      # ← 换成第 1 步确认的
    -Wake `                             # ← 用户要唤醒词才加
    -Agent harness `                    # 默认标准版
    -NotesDir 'D:\我的笔记库'            # ← 用户给了笔记库才加
```

> 用**显式路径**（如上），别用 `$PSScriptRoot`：那条命令是在**你自己的终端**里执行的，
> 内联运行时 `$PSScriptRoot` 是空的，会拼出 `\scripts\...` 这种不存在的路径。

### macOS ① 建环境 + 启动

```bash
kit="$HOME/资料目录"            # 解开资料夹后的目录（里面有 ECHO/ 和 echo-install/）
dest="$HOME/ECHO"

# 环境：Homebrew 装 python@3.11 + portaudio → 建 venv → 装 mac/requirements-mac.txt
#       （它顺带会在有 swiftc 时编译原生浮动条；没有就跳过，用浏览器面板）
bash "$kit/ECHO/mac/setup_mac.sh"
```

⚠️ `setup_mac.sh` 是在**资料夹原地**建 venv 的（它按脚本位置定位仓库根）。所以 mac 上的顺序是
**先把 `ECHO/` 放到最终位置，再跑 setup**：

```bash
mkdir -p "$(dirname "$dest")"
[ -d "$dest" ] || mv "$kit/ECHO" "$dest"    # 放到最终位置（同盘 mv 是秒级）
bash "$dest/mac/setup_mac.sh"               # 建 venv + 装依赖（首次几分钟）
bash "$dest/mac/start_mac.sh"               # 后台启动，并打印面板地址
```

### macOS ② 按确认结果装组件

```bash
bash "$kit/echo-install/scripts/echo-install-components.sh" \
    --dest "$dest" \
    --engines sherpa,whisper-base \      # ← 换成第 1 步确认的
    --agent harness \                    # 默认标准版
    --notes-dir "$HOME/我的笔记库"        # ← 用户给了笔记库才加（要唤醒词再加 --wake）
```

> 参数名与 Windows 版一一对应（`--engines` / `--wake` / `--diarize` / `--agent` / `--models-dir`
> / `--meetings-dir` / `--notes-dir` / `--pip-index`），只是写法从 `-PascalCase` 变成 `--kebab-case`。
> 脚本自带 `--help`。

两边这个脚本都做五件事：装所选引擎的 **pip 依赖** → **准备智能体**（把标准版 harness
**永久装到 `<安装目录>/harness/dsh`**，冷启动约 10 秒；装不上才回退 `npx -y @deepseek-ai/dsh`）
→ 起服务 → **下载所选模型**（ModelScope 优先，回落 hf-mirror，带进度与超时）→ 写设置 →
**自检并登记**。**幂等**：已经装好的会跳过，重跑安全。

> 每一步的输出也会落到 **`<安装目录>/data/logs/install-<时间戳>.log`**（独立于终端）——
> 终端被关掉、或被 agent 收走输出时，排障看这份日志就够（A6）。
>
> **给 agent：如果你的执行环境会中途回收后台进程** —— 有同事的沙箱约 2 分钟杀一次，
> 而且**连脚本派生出来的 ECHO 服务也一起带走**，组件脚本因此断过两次（断在 pip 中途、
> 断在模型下载刚发起时）。**断在中途就直接重跑同一条组件命令**，脚本是幂等的：
> 已装好的会跳过（依赖秒过、标准版走"已装好"快路径、模型跳过），重跑即续上。
> 断点看 **`<安装目录>/data/logs/install-<最新时间戳>.log`** 的最后几行。
> 真人双击安装不受影响（不会有人两分钟杀一次进程）。
>
> **另一条沙箱专属的坑**：这类环境里没有控制台，`install.ps1` 收尾那步"拉起 ECHO"
> 会失败并报 `ERROR_NO_DATA (0x800700E8)`「管道正被关闭」（`-Silent` 也会走到那一步）。
> **那是启动方式的问题，不是安装的问题** —— 看 `/api/install/state` 是 `ready: true`
> 就说明装好了，别重装。（新版 `install.ps1` 已不会因此把安装判成失败；旧包里会打印
> 「安装中断」+ 退出码 1，见到时按这条判断。）

> **为什么改成永久安装（2026-09-22）**：同一台机器实测 `npx -y @deepseek-ai/dsh web` 冷启动
> **2 分 10 秒**，直连本地 `lib/bin.js` 只要 **9 秒** —— npx 每次都要重新解析安装。
> 装完还会做一次**完整性自检**：`lib/bin.js` 存在且非空、任何 `node-pty` 都带 `package.json`
> 与 `lib/index.js` —— 同事踩过"目录在、文件被截断"导致 dsh 直接加载失败。
>
> **设置里写不写 `harnessCommand`：默认不写**（2026-09-22 晚改）。设置停在出厂值时，ECHO 自己会
> 优先用本地入口（`harness_proc.command()`：出厂值 + 本地入口存在 → 直连），node 也由它探测
> （`node_dirs()` 与技能里的 `Resolve-NodeDir` 是同一批目录）。好处是 **node 换版本不用改配置** ——
> 写死绝对路径时那条路径一失效 harness 就起不来，而 `harnessCommand` 是**隐藏设置**（不在设置页，
> 只在「智能体」那行的展开区可见），用户很难自己找到。要显式钉死（例如 node 位置很怪、想让面板
> 一眼看到实际命令）：加 `-PinHarnessCommand` / `--pin-harness-command`。
> 手动改它注意：`PUT /api/settings` 的 body 要包一层 `{"values": {"harnessCommand": "…"}}`。

> **装本地入口的三条路（脚本 `harness-install-local.ps1` / `.sh`）**：
> ① 已经装好 → 直接用；② `npm install @deepseek-ai/dsh@<版本>`；③ **从 npx 缓存复制**同版本那份整树。
> ③ 存在的原因：`0.1.5-rc.2` 的依赖图**出过** registry 事故 —— 有子包被写成
> `^0.1.5-rc.3`，而那个子包的 rc.3 当时还没发布，`npm` 报
> `ETARGET No matching version found for …documentpreview@^0.1.5-rc.3`。
> **2026-09-22 晚同事复测：rc.3 已发布，npm 这条路现在是通的**（584 包，约 2 分钟）。
> 但这类事故会复发，而 npx 慢是必然的，所以 ③ 保留。
> 缓存里没有时脚本会**先让 npx 把缓存填上再复制**（全新机器的 `_npx` 缓存是空的 ——
> 同事这次就是这种情况）；填不上才回退 npx。版本不一致会**明确告警**（仍可用）。
> 想指定别的版本：`-DshVersion <版本>` / `--dsh-version <版本>`。
> 单独修装坏的标准版：直接跑 `harness-install-local.ps1 -DestDir <安装目录>`（Windows）或
> `bash harness-install-local.sh --dest <安装目录>`（macOS），加 `-FromCache` / `--from-cache`
> 可跳过 npm 走缓存复制（离线也能装）。

三个要点（都是 2026-09-21 实测踩出来的）：

- **装完会 `POST /api/install/report` 登记**。这是"装完了"的凭据：面板据此**不再提示"还没装完"、
  也不再自动进向导**。没登记的话，用户打开面板会看到一条"这台机器还没登记安装完成"的横幅。
- **自检以 `import` / 模型 ready / 智能体 online 为准**，任何一项没成就**非 0 退出**并列出是哪几项 ——
  别把"pip 返回 0"当成"装好了"（uv 建的 venv 没 pip，依赖一个没装，安装器却打印了"安装完成"）。
- **失败会显形**：模型下载失败会打印**服务给的原因**（例如 `ImportError: No module named
  'modelscope'`），不会再出现"一直排队中"这种含糊状态（那是状态词表 + 取错结构两个 bug）。
  依赖没装上的引擎会**跳过它的模型下载**（装了也跑不起来），直接告诉你缺哪个包。

## 4. 验收（必须做，并如实汇报）

Windows：

```powershell
$port = [int](Get-Content 'D:\ECHO\data\echo-port.txt' -Raw).Trim()
(Invoke-RestMethod "http://127.0.0.1:$port/api/status").components | Select-Object name,status
(Invoke-RestMethod "http://127.0.0.1:$port/api/models").items     | Select-Object id,ready,local_mb
```

macOS（**注意数据根不一定是安装目录**：全新安装时是 `~/Library/Application Support/ECHO`）：

```bash
port="$(cd "$dest" && ./venv/bin/python -c 'from app import paths; print(paths.data_root())' \
        | xargs -I{} cat {}/echo-port.txt)"
curl -s "http://127.0.0.1:$port/api/status" | ./venv/bin/python -m json.tool | head -30
curl -s "http://127.0.0.1:$port/api/models" | ./venv/bin/python -m json.tool | head -40
```

然后**用三句话说清**（别只说"装好了"）：

- **能干什么**：录音转文字（本机，`sherpa`）、会议录音 + 文字稿、面板地址（Windows 还有 `Ctrl+Shift+E`）
- **还不能干什么**：哪些没装（唤醒词 / 说话人分离 / 显卡加速 / 方言档），以及**会议纪要**要不要智能体
- **以后怎么补**：面板 → 能力 / 向导，随时补，不用重装

## 5. 常见失败

| 现象 | 怎么处理 |
|---|---|
| **Windows**：启动时弹 **「You must install or update .NET」** | 那是**右缘浮动条**要 .NET 7 Desktop **Runtime**（与 ECHO 本体无关）。要么装它，要么：浏览器打开 `http://127.0.0.1:<端口>/` → 设置 → 面板 → **仪表盘打开方式 = browser**，并把「启动时自动显示折叠条」关掉 |
| **Windows**：`import torch` / `sherpa_onnx` / `ctranslate2` 报 **`DLL load failed … 找不到指定的模块`** | 缺 **Microsoft Visual C++ 2015-2022 运行库（x64）** —— 这些原生扩展都要它，干净镜像上常常没有（2026-09-21 同事实测）。技能的第 3 步会**自动下载安装**（弹 UAC 点「是」）；装不了就手动装 `https://aka.ms/vs/17/release/vc_redist.x64.exe` 后**重跑技能**（可能要重启一次） |
| **Windows**：弹 **「WebView2 初始化失败」** | 缺 Edge WebView2 Runtime（Win10/11 一般自带）；同样可改用浏览器面板 |
| **macOS**：提示「来自身份不明的开发者」/ 打不开 | **正常现象**：还没做签名与公证。右键 →「打开」，或去「系统设置 → 隐私与安全性」里允许一次 |
| **macOS**：热键按了没反应 | 要在「系统设置 → 隐私与安全性 → **辅助功能 / 输入监控**」里给终端（或 ECHO）授权 —— **首次必须人工点**，脚本代替不了 |
| **macOS**：麦克风没声音 | 同样在「隐私与安全性 → 麦克风」里授权；授权弹窗里显示的应用名可能是 `python`/`终端`，这是过渡实现（原生宿主还没做完） |
| **macOS**：浮动条没出现 | 需要 Apple Command Line Tools（`xcode-select --install`）后跑 `bash mac/build_sidebar.sh`；**没有也能用**：浏览器打开面板即可 |
| **macOS**：`brew` 找不到 | 先装 Homebrew（https://brew.sh），再重跑 `mac/setup_mac.sh` |
| **macOS**：装 `funasr`/`torch` 很慢 | Apple 芯片装的是普通版 torch（走 MPS，**不要**装 CUDA 版）；嫌大就先只装 `sherpa` |
| 智能体没起来（纪要/归档/指令不能用） | 需要 **Node.js**：装 Node 后重跑第 3 步的组件脚本（`-Agent harness` / `--agent harness`）—— 脚本会把标准版**永久装到 `<安装目录>/harness/dsh`** 并写好 `harnessCommand`，ECHO 直接拉起（冷启动约 10 秒）；或改用已装的 DSH 桌面版。**注意**：node 装在托管目录（如 WorkBuddy）里、不在系统 PATH 时，ECHO 自己会探测（2026-09-22 起）；若 `harnessCommand` 里是 `npx …`（回退路径），第一次启动仍要等 1-2 分钟 |
| npm 报 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` | 宿主（WorkBuddy 等）的安全删除 shim 拦了批量删除。装 dsh 时带 `CODEBUDDY_SAFE_DELETE_ENABLED=0`；**别中途 kill npx/npm**，否则包会装残（`node-pty` 缺 `index.js`） |
| npm 装 dsh 报 `ETARGET / No matching version found for …documentpreview@^0.1.5-rc.3` | 这是 **registry 侧的依赖图事故**（不是你的机器问题）：`0.1.5-rc.2` 有个子包依赖 `^0.1.5-rc.3`，而那个 rc.3 当时没发布。**2026-09-22 晚复测已恢复**（rc.3 已发布），先直接重跑一次多半就好了。仍不行时脚本会自动改走 **npx 缓存复制**（缓存空会先让 npx 填上，再复制）；也可直接 `harness-install-local.ps1 -DestDir <安装目录> -FromCache`（macOS：`bash harness-install-local.sh --dest <安装目录> --from-cache`）。日志里会写明用了哪份缓存树、版本是否一致 |
| 改设置报 `422 … ["body","values"] Field required` | `PUT /api/settings` 的 body 必须包一层：`{"values": {"harnessCommand": "…"}}`；`agentBackend` 写 `harness`（不是组件名 `agent-harness`）并同时开 `agentHarnessEnabled` |
| `pip` 慢 / 超时 | Windows 加 `-PipIndex https://pypi.tuna.tsinghua.edu.cn/simple`；mac 加 `--pip-index https://pypi.tuna.tsinghua.edu.cn/simple`，重跑（已装好的会跳过） |
| 模型下载慢或卡住 | 挑更小的档位（如 `whisper-tiny`）；下载在 ECHO 服务里继续跑，可不盯；进度看面板 → 能力 |
| **说话人分离**装不上 | 2026-09-21 起三件套从 **ModelScope 匿名可下**（`scripts/install_pyannote.py`，ModelScope 优先、HF 兜底）。先查两件事：① 是不是改过「模型放哪」——落点跟随 `modelsDir`；② 使用条款是否已由用户确认 |
| 会议转出来是空的（`（未检测到有效语音）`）或整场没有说话人 | 两把常见的刀：① **时间戳骨架缺依赖** —— `sensevoice` 少了 `whisper-small`/`faster-whisper` 会安静地转出空文件（见第 1 节表下的说明）；② **说话人分离失败**（缺 `speechbrain`、或用条款/模型没下）。2026-09-23 起分离失败**不再连累**整场转写（会补空说话人列照常落库），失败原因写进 `logs` 表与 `data\logs\echo-server.log.err`，先看这两处 |
| whisper 报 `Library cublas64_12.dll is not found or cannot be loaded` | torch 被换成了 CUDA 13 的构建（cu130），而 faster-whisper 的 ctranslate2 只认 CUDA 12 的 cublas。装回 cu128：`pip install --force-reinstall torch torchaudio --index-url https://download.pytorch.org/whl/cu128` |
| 选 `qwen3asr` 却报 `qwen-asr package is required for Qwen3-ASR` | 只装了 `transformers` 没装 `qwen-asr`（这正是 2.0.0 的漏洞，已修）。跑 `scripts\install-qwen3asr.ps1`（它会装 `qwen-asr==0.0.6` + `transformers==4.57.6`）。**别只升 transformers**：5.x 上 qwen-asr 0.0.6 直接 import 不了 |
| 老机器磁盘不够 | 只装 `sherpa`（189 MB）+ 不装唤醒词，安装目录约 0.4 GB |
| **macOS**：脚本报 `$'\r': command not found` | 说明 `.sh` 被传成了 CRLF（Windows 上解压/复制过的痕迹）。用 `bash` 跑之前先 `perl -pi -e 's/\r$//' 文件` 或重新从 zip 解一次 |

## 6. 别做的事

- **不要把两边的命令混用**：macOS 上**没有** `install.ps1`/`-DestDir` 那套（那是 Windows 专属：
  .NET/WebView2 边条、Windows 嵌入包 CPython、注册表快捷方式）；Windows 上也没有 `mac/setup_mac.sh`。
  先认平台，再挑一套。
- **不要**让用户去 `git clone`（公司网多半连不上 GitHub）—— 代码只用资料夹里的 `ECHO/`。
- **不要**替用户装 CUDA/torch：体积大、和驱动绑定，让他们按需自己决定（mac 上更是没有 CUDA）。
- **不要**把 `agentBackend` 写成组件 id（`agent-harness`）—— 那是**组件名**，设置里要用的名字是
  **`harness`**（并同时打开 `agentHarnessEnabled`）。写错了选择会**静默失效**（2026-09-20 踩过）。
- **不要**在没跑 `import` 复核前说"装好了"：`pip` 返回 0 或 `python.exe` 存在都不等于引擎能用
  （2026-09-21 踩过：uv 建的 venv 没有 pip，依赖一个都没装，安装器却打印了"安装完成"）。
