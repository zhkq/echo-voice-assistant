---
name: echo-install
description: 在**一台新机器**上安装/准备 ECHO（本地语音助理）：先跟用户确认要哪些组件，再解开主包、按需下载运行时与模型（走国内镜像）、写好设置并自检。当用户说"装 ECHO / 安装 ECHO / 帮我准备 ECHO 的环境 / 给同事装 ECHO"时使用。
whenToUse: 新机器首装 ECHO；或把 ECHO 交给同事时，让**他那边的 agent** 照着做
---

# ECHO 安装准备

> 给**任何 agent** 用 —— 你不需要是 DSH。每一步都给现成命令；你负责**问清楚**、**照做**、**如实汇报**。

## 0. 这个技能怎么用（也是给同事看的三句话）

1. 把这个技能文件夹连同 **`ECHO-main-<平台>-<版本>-<时间>.zip`**（必备，约 3 MB）放在一起。
2. 对你的 agent 说：**「按 echo-install 这个技能给我装 ECHO」**。
3. agent 会问你几个问题（装到哪、要哪些能力），然后自己下载安装。

**它不需要 git、不需要 GitHub**：代码来自那个 zip；运行时来自 **python.org**；依赖来自 **PyPI**；
模型来自 **ModelScope / hf-mirror**（ECHO 内置 `HF_ENDPOINT=https://hf-mirror.com`）。

## 1. 先跟用户确认（必须问，不要替他决定）

| 要问什么 | 推荐 | 为什么问 |
|---|---|---|
| **装到哪个目录** | `D:\ECHO`（没有 D 盘就用空间最大的盘） | ⚠ **必须纯英文路径**，别放桌面/中文/OneDrive —— 转写引擎读中文路径会出问题 |
| **转写方式**（可多选，见下表） | **`sherpa`**（边听边出字，189 MB，**不需要独显**） | 这是唯一"装完立刻能用又不吃显卡"的一档 |
| **要不要唤醒词**（喊一声就开始） | 不要 | 要常开麦克风，有隐私成本；模型 40 MB |
| **要不要说话人分离**（会议里区分谁在说） | 不要 | 需 HF 授权（gated），ECHO 不能替你下 |
| **要不要显卡加速 / 方言口音** | 不要 | 要 N 卡；方言档 3.6 GB，还得先有 torch |
| **模型 / 会议文件 / 笔记库放哪** | 留空=默认（都在安装目录下） | 填了「笔记库」就会把纪要归档到那儿（通常是你已有的 Obsidian 库） |

**转写方式怎么选**（体积是模型的，pip 依赖另算）：

| 选项 | 体积 | pip 依赖 | 说明 |
|---|---|---|---|
| `sherpa` | 189 MB | `sherpa-onnx` | **边听边出字**，CPU 实时，最省内存 —— 推荐 |
| `whisper-base` | 141 MB | `faster-whisper` | 更准的档位（D1 推荐组合：sherpa + base） |
| `whisper-tiny` / `small` / `medium` / `large-v3` | 75 / 464 / 1500 / 2950 MB | 同上 | 档位越高越准也越慢 |
| `sensevoice` | 896 MB | `funasr` + `torch`（**+约 2 GB**） | 中文短句标点最准，但拖 torch |
| `qwen3asr` | 3.6 GB | `transformers` + `torch` | 方言/口音更强，**需要 N 卡** |

**智能体（会议纪要 / 归档 / 语音指令靠它）**：**默认就用标准版**（`agentBackend=harness`），
**不用问用户**；只要本机有 Node.js，ECHO 会自己把它拉起来。没有 Node 就照第 5 节处理。

## 2. 先探测，再动手（30 秒）

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
```

把结论告诉用户（尤其："哪条路能用""要不要装 Node"），再往下走。

## 3. 装（两步，都可重跑）

### ① 解开主包 + 准备运行时

> ⚠️ **`install.ps1` 在 `ECHO-main-*.zip` 里面**（路径是 `ECHO\scripts\install.ps1`）。
> 资料目录里**没有**散着的 `install.ps1` —— 别在那儿找它（2026-09-21 同事就卡在这里）。
> 也不能只把 `install.ps1` 单独拷出来跑：它要跟同一个包里的 `manifest.json`、其它脚本待在一起。

```powershell
$pkg  = 'C:\资料目录'          # 放着 ECHO-main-win-x64-*.zip 的那个目录
$zip  = (Get-ChildItem "$pkg\ECHO-main-win-x64-*.zip" -File |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
$stage = Join-Path $env:TEMP 'echo-pack'
Expand-Archive -Path $zip -DestinationPath $stage -Force      # 先解出来

powershell -NoProfile -ExecutionPolicy Bypass -File "$stage\ECHO\scripts\install.ps1" `
    -Zip $zip -DestDir 'D:\ECHO' -Silent
#                                  ↑ 显式传 -Zip：不传的话脚本要在自己的目录/上级/~/Downloads 里
#                                    猜哪个 ECHO-*.zip 是交付包，资料目录里往往不止一个 zip
# 加 -PipIndex https://pypi.tuna.tsinghua.edu.cn/simple  可换国内 pip 镜像（慢就用它）
```

`$stage` 只是个中转站，装完可以删（`Remove-Item $stage -Recurse -Force`）—— 安装目录里已经有它要的东西了。

主包**不含运行时**。`install.ps1` 会按三级降级找 CPython：
`uv venv --python 3.11` → `py -3.11 -m venv` → **python.org 嵌入包 + get-pip**（约 11 MB，只依赖 python.org）。
最后一级是内网的正解 —— **全程不需要 GitHub**。基础依赖约 100 MB，几分钟。

### ② 按确认结果装组件

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

它做四件事：装所选引擎的 **pip 依赖** → 起服务 → **下载所选模型**（ModelScope / hf-mirror，带进度与超时）
→ 写设置（`sttModel`/`meetingSttModel`/`wakeEnabled`/`agentBackend`+`agentHarnessEnabled`/三处目录）。
**幂等**：已经装好的会跳过，重跑安全。

## 4. 验收（必须做，并如实汇报）

```powershell
$port = [int](Get-Content 'D:\ECHO\data\echo-port.txt' -Raw).Trim()
(Invoke-RestMethod "http://127.0.0.1:$port/api/status").components | Select-Object name,status
(Invoke-RestMethod "http://127.0.0.1:$port/api/models").items     | Select-Object id,ready,local_mb
```

然后**用三句话说清**（别只说"装好了"）：

- **能干什么**：录音转文字（本机，`sherpa`）、会议录音 + 文字稿、`Ctrl+Shift+E` 呼出面板
- **还不能干什么**：哪些没装（唤醒词 / 说话人分离 / 显卡加速 / 方言档），以及**会议纪要**要不要智能体
- **以后怎么补**：面板 → 能力 / 向导，随时补，不用重装

## 5. 常见失败

| 现象 | 怎么处理 |
|---|---|
| 启动时弹 **「You must install or update .NET」** | 那是**右缘浮动条**要 .NET 7 Desktop **Runtime**（与 ECHO 本体无关）。要么装它，要么：浏览器打开 `http://127.0.0.1:<端口>/` → 设置 → 面板 → **仪表盘打开方式 = browser**，并把「启动时自动显示折叠条」关掉 |
| 弹 **「WebView2 初始化失败」** | 缺 Edge WebView2 Runtime（Win10/11 一般自带）；同样可改用浏览器面板 |
| 智能体没起来（纪要/归档/指令不能用） | 需要 **Node.js**：装 Node 后重跑第 3 步②（`-Agent harness`）—— ECHO 会自动拉起标准版；或改用已装的 DSH 桌面版 |
| `pip` 慢 / 超时 | 加 `-PipIndex https://pypi.tuna.tsinghua.edu.cn/simple` 重跑（已装好的会跳过） |
| 模型下载慢或卡住 | 挑更小的档位（如 `whisper-tiny`）；下载在 ECHO 服务里继续跑，可不盯；进度看面板 → 能力 |
| **说话人分离**装不上 | pyannote 是 HF gated 模型：让用户自己去 HF **同意条款**再拉，ECHO 不代下（许可证不允许再分发） |
| 老机器磁盘不够 | 只装 `sherpa`（189 MB）+ 不装唤醒词，安装目录约 0.4 GB |

## 6. 别做的事

- **不要**让用户去 `git clone`（公司网多半连不上 GitHub）—— 代码只用资料目录里的 zip。
- **不要**替用户装 CUDA/torch：体积大、和驱动绑定，让他们按需自己决定。
- **不要**把 `agentBackend` 写成组件 id（`agent-harness`）—— 那是**组件名**，设置里要用的名字是
  **`harness`**（并同时打开 `agentHarnessEnabled`）。写错了选择会**静默失效**（2026-09-20 踩过）。
