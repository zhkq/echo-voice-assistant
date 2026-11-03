# =====================================================================
# echo-install-components.ps1 - 按需求把 ECHO 的引擎依赖 / 模型 / 设置装好
#
# 谁在用它：`.dsh/skills/echo-install`（安装专用技能）。技能先跟用户确认"要哪些组件"，
# 再把选择翻译成本脚本的参数。**不用 git、不用 GitHub**：依赖走 PyPI，模型走
# 客户端不再下载本地 STT 模型（2026-10-31：转写全部走能力后端）。
#
# 前提：`install.ps1` 已经把主包解开、`runtime-core` 就绪（本脚本自己会检查，缺了就报错）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File echo-install-components.ps1 `
#       -DestDir D:\ECHO            # 2026-10-31 起客户端只装 runtime-core，无本地 STT
#
# 参数（2026-11-03 起只有一套 DSH = 官方桌面端自带的 dsh）：
#   -DestDir <目录>        ECHO 安装目录（必填）
#   -Engines <列表>        **已废弃**：传了只警告、不生效
#   -Wake / -Diarize / -AccelCuda   **已废弃**：唤醒取消、分离与 torch 都在能力后端
#   -Agent <名>            智能体：dsh(默认) / none。**harness 已退役**：传了明确警告并按 dsh 处理
#   -DesktopInstallDir     官方桌面端安装目录（留空=官方默认的 %LOCALAPPDATA%\Programs\DeepSeek Harness）
#   -Offline               离线安装：不联网下载官方桌面端（已装的仍可用）
#   -UpgradeDsh            用户同意了才重新下载/静默安装官方桌面端（只换程序，不改配置）
#   -HarnessSource / -DshVersion / -PinHarnessCommand / -DshDir / -AskDsh
#                          **已退役**：只保留参数，免得旧命令行/旧 install-all 报"未知参数"；
#                          传了只警告、不生效（不再有 npm 安装、独立家目录或 harnessCommand）
#   -ModelsDir / -MeetingsDir / -NotesDir   三处位置（留空=默认；-NotesDir 填了会开归档）
#   -PipIndex <url>        国内 pip 镜像（核心依赖由 runtime-core 提供，这里一般用不到）
#   -SkipPip               只装设置/智能体、不装 pip 依赖
#   -KeepSettings          升级模式：**不写任何设置**（智能体 / 路径都保持原样）
#   -SkipModels            升级模式：历史兼容（客户端已没有模型可跳过）
#   -WaitSeconds <秒>      历史兼容参数（客户端不再下载模型）
#
#
# 编码：含中文，所以**必须 UTF-8 带 BOM**（WinPS 5.1 对无 BOM 的 .ps1 按 ANSI 读，
#       中文会乱码甚至静默解析失败）—— 见 tests/test_script_encoding.py。
# =====================================================================
param(
    [Parameter(Mandatory = $true)][string]$DestDir,
    [string[]]$Engines = @(),
    [switch]$Wake,
    [switch]$Diarize,
    [switch]$AccelCuda,
    # 2026-11-03：只有一套 DSH —— 官方 DeepSeek Harness 桌面端自带的 dsh。
    # `harness` 是退役名字，传了会明确警告并按 `dsh` 处理。
    [string]$Agent = 'dsh',
    # 以下五个参数**已退役**：只保留，免旧命令行/旧 install-all 因"未知参数"直接失败。
    # 它们曾经的语义（npm 本地永久安装 / 独立家目录 / harnessCommand）都已删除。
    [string]$HarnessSource = 'auto',
    [string]$DesktopInstallDir = '',
    [switch]$Offline,
    [string]$DshVersion = '',
    [switch]$PinHarnessCommand,
    [string]$DshDir = '',
    [switch]$AskDsh,
    [string]$ModelsDir = '',
    [string]$MeetingsDir = '',
    [string]$NotesDir = '',
    [string]$PipIndex = '',
    [string]$TorchIndex = '',
    [switch]$SkipPip,
    # 升级模式（2026-10-23 用户口径）：只更新核心代码。
    #   -KeepSettings：一个设置项都不写（sttModel / 会议模型 / 智能体 / 路径全保持原样）
    #   -SkipModels  ：不下载、不替换已有模型权重
    [switch]$KeepSettings,
    [switch]$SkipModels,
    # 升级模式：用户明确同意时重新下载/静默安装官方桌面端（只换程序，不改配置）。
    [switch]$UpgradeDsh,
    [int]$WaitSeconds = 1800
)

$ErrorActionPreference = 'Stop'
# -DestDir 是**安装根**。3.0 布局里代码在 <根>\echo-core，数据/模型/会议/指令都是
# 它的兄弟目录；老式扁平安装里代码就在根下（判据与 app\paths.py:echo_base() 同一条）。
#   $script:Root = 安装根（数据、日志、运行时都按它算）
#   $script:Code = 代码目录（scripts\ 在这儿）
$script:Root = (Resolve-Path -LiteralPath $DestDir).Path
$script:Code = $script:Root
if ((Split-Path $script:Root -Leaf) -ieq 'echo-core') {
    # 调用方把代码目录当成了安装根（例如从 <根>\echo-core 里重跑）→ 上提一层
    $script:Root = Split-Path -Parent $script:Root
} elseif (-not (Test-Path (Join-Path $script:Root 'app\main.py'))) {
    # 全新安装：代码还没到位，但 3.0 布局已经定了 —— 代码将落在 <根>\echo-core
    $script:Code = Join-Path $script:Root 'echo-core'
}
# 2026-11-03：不再有「本地/已有 DSH 的启动命令」，也不写 harnessCommand。
# 只记有效选择（dsh/none）与等待失败的原因，供自检如实报出。
$script:AgentChoice = 'dsh'
$script:DshWaitReason = ''

# A6：每一步同时留一份**独立于终端**的日志（终端关掉、输出被 agent 收走时还能回看）。
# 落点 <安装根>\data\logs\install-<时间戳>.log；写不进去（权限/只读）就静默放弃，绝不挡安装。
$script:InstallLog = ''
try {
    $logDir = Join-Path $script:Root 'data\logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $script:InstallLog = Join-Path $logDir ("install-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Add-Content -LiteralPath $script:InstallLog -Encoding UTF8 -Value ("===== echo-install-components {0} 安装目录={1} =====" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $script:Root)
} catch { $script:InstallLog = '' }

function LogLine([string]$m) {
    if (-not $script:InstallLog) { return }
    try { Add-Content -LiteralPath $script:InstallLog -Encoding UTF8 -Value ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) } catch { }
}
function Say([string]$m)  { Write-Host "  $m"; LogLine "  $m" }
function Ok([string]$m)   { Write-Host "  [ok]   $m" -ForegroundColor Green; LogLine "[ok]   $m" }
function Warn([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow; LogLine "[warn] $m" }
function Err([string]$m)  { Write-Host "  [fail] $m" -ForegroundColor Red; LogLine "[fail] $m" }
function Step([string]$m) { Write-Host ''; Write-Host "== $m" -ForegroundColor Cyan; LogLine "== $m" }

# 2026-10-31 用户拍板：客户端默认档不装任何本地 STT 模型/引擎，语音唤醒取消。
# 指令 / 回顾 / 会议转写统一走能力后端（echo-server + Qwen3-ASR）。
# 保留 `$ENGINE_MAP` 这个名字（空表）只为让历史调用方/测试能读到"没有本地引擎"。
$ENGINE_MAP = @{}

# ---------------------------------------------------------------- 运行时 / 端口 / API

function Get-RuntimePython {
    foreach ($rel in @('runtime-core\python.exe', 'runtime-core\Scripts\python.exe',
                       'venv\Scripts\python.exe')) {
        $p = Join-Path $script:Root $rel
        if (Test-Path $p) { return $p }
    }
    return ''
}

function Get-EchoPort {
    if ($env:ECHO_PORT) { try { return [int]$env:ECHO_PORT } catch { } }
    $f = Join-Path $script:Root 'data\echo-port.txt'
    if (Test-Path $f) {
        try { $v = [int]((Get-Content $f -Raw).Trim()); if ($v -gt 0) { return $v } } catch { }
    }
    return 8970
}

function Invoke-Api {
    param([string]$Path, [string]$Method = 'GET', $Body = $null, [int]$TimeoutSec = 30)
    # 走 Invoke-WebRequest 再**自己按 UTF-8 解字节**，而不是 Invoke-RestMethod 直接吃 JSON：
    # Windows PowerShell 5.1 在响应头没写 charset 时按 **ISO-8859-1** 解 JSON —— FastAPI 回的
    # `application/json` 正好不带 charset，于是所有中文 detail 变成 `ç¬ç«` 这种乱码
    # （同事实测报告 §4.4 G：`{DATA}\logs\install-*.log` 里满是乱码，排障时误导人）。
    # 5.1 没有能救它的 -Encoding 开关，只能拿原始字节自己解。
    $req = @{ Uri = "http://127.0.0.1:$(Get-EchoPort)$Path"; Method = $Method
              TimeoutSec = $TimeoutSec; UseBasicParsing = $true }
    if ($null -ne $Body) {
        $req.ContentType = 'application/json; charset=utf-8'
        $req.Body = [System.Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 6 -Compress))
    }
    $resp = Invoke-WebRequest @req
    $bytes = if ($resp.RawContentStream) { $resp.RawContentStream.ToArray() } else { @() }
    if (-not $bytes -or $bytes.Length -eq 0) { return $null }
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if (-not $text.Trim()) { return $null }
    return ($text | ConvertFrom-Json)
}

function Ensure-Service {
    Step '启动 ECHO 服务'
    try { $null = Invoke-Api -Path '/api/status' -TimeoutSec 5; Ok '服务已在运行'; return }
    catch { }
    $start = Join-Path $script:Code 'scripts\start.ps1'
    if (-not (Test-Path $start)) { Err "找不到启动脚本：$start"; exit 1 }
    Say '服务没在跑，后台启动一次…'
    Start-Process powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$start`"", '-Background') -WindowStyle Hidden
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 2
        try { $null = Invoke-Api -Path '/api/status' -TimeoutSec 5; Ok ("服务已就绪（端口 $(Get-EchoPort)）"); return }
        catch { }
    }
    Err '启动后 120 秒仍未就绪 —— 看 data\logs\echo-server.log'
    exit 1
}

# ---------------------------------------------------------------- pip 依赖

function Install-PipDeps([string[]]$Packages) {
    if ($SkipPip) { Warn "已跳过 pip 依赖（-SkipPip）：$($Packages -join ', ')"; return }
    $py = Get-RuntimePython
    if (-not $py) { Err '找不到运行时（runtime-core）—— 先跑 install.ps1'; exit 1 }
    $pkgs = @($Packages | Where-Object { $_ } | Select-Object -Unique)
    if ($pkgs.Count -eq 0) { return }
    Step ("安装依赖：" + ($pkgs -join ', '))
    $extra = @()
    if ($PipIndex) { $extra = @('-i', $PipIndex); Say ("pip 源：{0}" -f $PipIndex) }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $py -m pip install --no-warn-script-location @extra @pkgs 2>&1 |
            ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
    if ($code -eq 0) { Ok ("依赖就绪：" + ($pkgs -join ', ')) }
    else { Warn ("pip 返回 $code —— 依赖可能没装全；模型仍会尝试下载") }
}

# ---------------------------------------------------------------- 模型（已取消）
# 客户端不再下载/安装任何本地 STT 模型；这里不再有 Get-ModelState / Wait-Model /
# Install-Model 这些函数（2026-10-31）。

# ---------------------------------------------------------------- VC++ 运行库（原生扩展的前置）

function Test-VCRuntime {
    # Windows 上 numpy / soundfile 等**原生扩展**
    # 都要 Microsoft Visual C++ 2015-2022 运行库（x64）。干净镜像上常常没有，而报错只说
    # "DLL load failed while importing …: 找不到指定的模块" —— 2026-09-21 同事就卡在这一句上。
    # 官方检测点：注册表 14.0\VC\Runtimes\x64 的 Installed=1（14.0 = 2015 起合并的那一版）。
    foreach ($k in @('HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
                     'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64')) {
        try { if ((Get-ItemProperty -Path $k -ErrorAction Stop).Installed -eq 1) { return $true } } catch { }
    }
    # 注册表读不到时退一步看 System32 的关键 DLL（三件齐了才认）
    foreach ($d in @('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll')) {
        if (-not (Test-Path (Join-Path $env:SystemRoot "System32\$d"))) { return $false }
    }
    return $true
}

function Install-VCRuntime {
    # 下载官方安装器并静默安装。**需要管理员** —— 会弹 UAC，请用户点「是」；
    # 装不了（无管理员/被策略拦）就返回 $false，由调用方给人工指引。
    $url = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
    $exe = Join-Path $env:TEMP 'vcredist_x64.exe'
    Say ("下载 VC++ 运行库：{0}" -f $url)
    try { Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing -TimeoutSec 300 }
    catch { Warn ("下载失败：{0}" -f $_.Exception.Message); return $false }
    Say '开始安装（会弹 UAC 授权框，请点「是」）…'
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $p = Start-Process -FilePath $exe -ArgumentList @('/install', '/quiet', '/norestart') -Wait -PassThru
        # 0 = 成功；3010 = 成功但需要重启 —— 都算装上了
        if ($p.ExitCode -eq 0 -or $p.ExitCode -eq 3010) { return $true }
        Warn ("VC++ 安装程序返回 {0}（没有管理员权限时常见）" -f $p.ExitCode)
        return $false
    } catch { Warn ("VC++ 安装失败：{0}" -f $_.Exception.Message); return $false }
    finally { $ErrorActionPreference = $prev }
}

function Get-ImportFailure([string]$Name) {
    # 真 import 一次并把错误文本带回来 —— 用来区分"包没装"和"缺 DLL（要装 VC++）"。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = (& $rcPy -c "import $Name" 2>&1 | Out-String)
        return @{ ok = ($LASTEXITCODE -eq 0); text = ($out).Trim() }
    } catch { return @{ ok = $false; text = "$($_.Exception.Message)" } }
    finally { $ErrorActionPreference = $prev }
}

function Test-DllLoadFailure([string]$Text) {
    if (-not $Text) { return $false }
    return ($Text -match 'DLL load failed' -or $Text -match 'WinError 126' -or
            $Text -match '找不到指定的模块' -or
            $Text -match 'The specified module could not be found')
}

# ---------------------------------------------------------------- 智能体（唯一一套 DSH）
#
# 2026-11-03 架构收敛：**只保留一套 DSH** —— 官方 DeepSeek Harness 桌面端自带的 dsh
# （`%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd`）。
# 标准版 harness（npm 安装 + 独立家目录 `<安装根>\harness\dsh`）已**彻底退役**：
#   * 不再安装任何 npm 包，也不再要求系统 Node.js / npx；
#   * 不再探测已有 DSH 目录，不再写 harnessCommand / harnessHome / harnessPort /
#     harnessToken / agentHarnessEnabled；
#   * DSH 的家目录固定是 `%USERPROFILE%\.dsh`（官方桌面端自己的那份），ECHO 不另建。
# 安装器在这里只做一件事：**保证官方桌面端已安装**。下载 / 静默安装 / Authenticode
# 签名校验 / "桌面端正在运行就不覆盖" 全部沿用 `harness-install-desktop.ps1`。

function Resolve-AgentChoice {
    # `-Agent harness` 是退役名字：明确警告，并按 `dsh` 处理（不静默乱装）。
    if ($Agent -eq 'none') { return 'none' }
    if ($Agent -eq 'harness') {
        Warn '-Agent harness 已退役：现在只有一套 DSH（官方桌面端自带的 dsh），按 -Agent dsh 处理'
        return 'dsh'
    }
    return 'dsh'
}

function Warn-RetiredArgs {
    if ($HarnessSource -and $HarnessSource -ne 'auto') {
        Warn ("-HarnessSource {0} 已退役（只有官方桌面端自带的 dsh）—— 已忽略" -f $HarnessSource)
    }
    if ($DshVersion) { Warn ("-DshVersion {0} 已退役（DSH 随官方桌面端升级）—— 已忽略" -f $DshVersion) }
    if ($PinHarnessCommand) { Warn '-PinHarnessCommand 已退役（不再写 harnessCommand）—— 已忽略' }
    if ($DshDir) { Warn ("-DshDir {0} 已退役（只认官方桌面端）—— 已忽略" -f $DshDir) }
    if ($AskDsh) { Warn '-AskDsh 已退役（不再安装标准版 harness）—— 已忽略' }
}

function Prepare-Agent {
    # 返回 $true = 已能确定 dsh 程序来源（或用户明确不要智能体）。
    if ($script:AgentChoice -eq 'none') {
        Step '准备智能体（已选择不装）'
        Say '按 -Agent none：不检查、不安装 DSH；以后仍可在面板里配'
        return $true
    }
    Step '准备智能体（官方 DeepSeek Harness 桌面端自带的 dsh）'

    $here = $PSScriptRoot
    if (-not $here) { try { $here = Split-Path -Parent $PSCommandPath } catch { $here = '' } }
    $desktopHelper = ''
    if ($here) { $desktopHelper = Join-Path $here 'harness-install-desktop.ps1' }
    if (-not $desktopHelper -or -not (Test-Path -LiteralPath $desktopHelper)) {
        Err ('找不到 harness-install-desktop.ps1（脚本目录 {0}）—— 无法确认官方桌面端是否已安装' -f $here)
        return $false
    }

    # 离线 / 升级模式默认**只认已装**，绝不联网下载；只有用户显式同意升级 DSH
    # （-UpgradeDsh）时才重新下载。桌面端正在运行时 helper 不覆盖，继续用现有 CLI。
    #
    # ⚠️ 2026-11-03 真机 P0：这里原来写的是**数组 splat**（`$dargs = @('-LogFile', $x)`，
    # `& $helper @dargs`）。数组 splat 是**按位置**绑定参数，`-LogFile` 会落进
    # `-InstallDir` 这个 string 参数、日志路径再撞到 `-Upgrade` 开关 → 整个 helper
    # 根本没执行，安装器只报"官方桌面端没就绪"。**必须用哈希表 splat**（按名字绑定）。
    $dargs = @{ LogFile = $script:InstallLog }
    if ($DesktopInstallDir) { $dargs['InstallDir'] = $DesktopInstallDir }
    if ($UpgradeDsh -and -not $Offline) {
        $dargs['Upgrade'] = $true
    } elseif ($Offline -or $KeepSettings) {
        $dargs['NoDownload'] = $true
    }

    $dout = @()
    try {
        $dout = & $desktopHelper @dargs 2>&1
    } catch {
        Warn ('下载/安装官方桌面端失败：{0}' -f $_.Exception.Message)
    }
    foreach ($line in @($dout)) { LogLine ("  desktop: {0}" -f "$line") }

    $cmdLine = @($dout | ForEach-Object { "$_" } | Where-Object { $_ -like 'HARNESS_COMMAND=*' } |
                 Select-Object -Last 1)
    if ($cmdLine.Count -eq 0) {
        Err '官方 DeepSeek Harness 桌面端没就绪（拿到它的程序入口失败）'
        if ($Offline -or ($KeepSettings -and -not $UpgradeDsh)) {
            Say '  离线/升级模式不下载：请先在这台机器上装好官方桌面端，再重跑本脚本。'
        } else {
            Say '  联网安装失败时：手动装官方桌面端 https://download.deepseek.com/desktop/dsh-latest-windows-x64.exe'
            Say '  装好后重跑本脚本（已装会直接复用）。'
        }
        return $false
    }
    $cmd = ([string]$cmdLine[-1]).Substring('HARNESS_COMMAND='.Length)
    $vline = @($dout | ForEach-Object { "$_" } | Where-Object { $_ -like 'DSH_VERSION=*' } |
               Select-Object -Last 1)
    Ok '官方 DeepSeek Harness 已就绪（自带无头 dsh，无需 Node.js / npm）'
    if ($vline.Count -gt 0) { Say ('  DSH 版本：{0}' -f ([string]$vline[-1]).Substring('DSH_VERSION='.Length)) }
    Say ('  程序入口：{0}' -f $cmd)
    Say '  DSH 家目录：%USERPROFILE%\.dsh（官方桌面端自己的那份，ECHO 不另建）'
    return $true
}

function Get-DshState {
    # 只认 /api/status 的 components 里 name='dsh' 那一行。
    param($Status)
    $h = @($Status.components | Where-Object { $_.name -eq 'dsh' })
    if ($h.Count -eq 0) { return $null }
    return $h[0]
}

function Wait-Dsh {
    # 先 POST /api/control/dsh/start（桌面版已在跑就用它，否则起无头 dsh），
    # 再轮询 /api/status 直到 components 里 dsh == online。
    # 上限 180 秒；超时**如实报"还在起"**，绝不把没起来说成起来了。
    if ($script:AgentChoice -eq 'none') {
        $script:DshWaitReason = '未选择智能体'
        return $true
    }
    Step '拉起 DSH 并等它 online'
    try {
        $r = Invoke-Api -Path '/api/control/dsh/start' -Method Post -Body @{} -TimeoutSec 60
        if ($r -and $r.message) { Say ("ECHO 说：{0}" -f $r.message) }
        # ok=false 不立刻判死：ECHO 可能正在后台起，状态仍会从 starting 走到 online。
        if ($r -and ($null -ne $r.ok) -and (-not $r.ok)) {
            Warn 'start 接口回了"没起来"——继续看状态（可能仍在后台准备）'
        }
    } catch {
        Warn ("调 /api/control/dsh/start 失败：{0}" -f $_.Exception.Message)
    }

    $Seconds = 180
    $deadline = (Get-Date).AddSeconds($Seconds)
    $last = ''
    while ((Get-Date) -lt $deadline) {
        try {
            $st = Invoke-Api -Path '/api/status' -TimeoutSec 10
            $h = Get-DshState $st
            if ($h -and $h.status -eq 'online') {
                Ok ("DSH 已就绪（dsh online：{0}）" -f $h.detail)
                return $true
            }
            if ($h -and $h.status -eq 'failed') {
                $script:DshWaitReason = ('启动失败：{0}' -f $h.detail)
                Warn ("DSH 启动失败：{0}" -f $h.detail)
                return $false
            }
            if ($h) { $last = [string]$h.status }
        } catch { }
        Start-Sleep -Seconds 3
    }
    if (-not $last) { $last = '取不到状态' }
    $script:DshWaitReason = ('等了 {0}s 仍未 online，还在起（最后状态：{1}）' -f $Seconds, $last)
    Warn ("等 {0}s dsh 仍未 online（最后状态：{1}）—— 还在起，别当成装好了；看 data\logs\echo-server.log" -f $Seconds, $last)
    return $false
}

# ---------------------------------------------------------------- 主流程

Write-Host ''
Write-Host '  === ECHO 组件安装（按需下载）===' -ForegroundColor White
Write-Host ("  安装根：{0}（代码在 {1}）" -f $script:Root, $script:Code)
if (-not (Test-Path (Join-Path $script:Code 'app'))) {
    Err ("这个目录里没有 app\ —— 看起来不是 ECHO 安装目录：{0}（先跑 install.ps1）" -f $script:Code)
    exit 1
}
$rcPy = Get-RuntimePython
if (-not $rcPy) { Err '找不到 runtime-core\python.exe —— 先跑 install.ps1'; exit 1 }
Ok ("运行时：{0}" -f $rcPy)

# 1) 依赖 + 模型：**客户端已不再装本地 STT 引擎/模型**（2026-10-31）。
#    核心依赖由 runtime-core（requirements-core.txt / bundle\wheels）提供，这里不追加任何引擎包。
$pips = @()
$models = @()
if (@($Engines).Count -gt 0 -or $Wake -or $Diarize -or $AccelCuda) {
    Warn '客户端已不再安装本地 STT 引擎/唤醒/分离/CUDA 组件（转写全部走能力后端）——这些参数已忽略'
}

# VC++ 运行库先解决：numpy/soundfile 等原生扩展仍需要它。
Step '检查 VC++ 运行库（原生扩展的前置）'
if (Test-VCRuntime) {
    Ok 'VC++ 2015-2022 运行库已就绪'
} else {
    Warn '缺 Microsoft Visual C++ 2015-2022 运行库（x64）—— 音频 IO 的原生扩展需要它'
    if (Install-VCRuntime) {
        Ok 'VC++ 运行库已安装'
    } else {
        Say '  没装成。请手动装（或让管理员装）后重跑本脚本：'
        Say '    https://aka.ms/vs/17/release/vc_redist.x64.exe'
        Say '  装的时候会弹 UAC，点「是」；装完可能需要重启一次。'
    }
}

$script:AgentChoice = Resolve-AgentChoice
Warn-RetiredArgs
$agentPrepared = Prepare-Agent   # 先确认官方桌面端（唯一 dsh 程序来源）就绪
Ensure-Service

# 2026-10-31：客户端不装本地模型 —— 没有"跳过缺依赖引擎的模型下载"这一段。

# 2) 写设置（与面板同一套键）
$settingsOk = $true
if ($KeepSettings) {
    Step '写入设置（升级模式：一个设置项都不改）'
    Say '智能体 / 路径：全部保持原样（客户端已无本地 STT 引擎/模型）'
} else {
Step '写入设置'
$values = @{}
# 只认**查得到**的引擎名，认不出就一个都不写。
# 为什么必须筛（2026-09-23 实测踩到）：`powershell -File 脚本.ps1 -Engines a,b` 会把
# `a,b` 当成**一个字符串**（-File 不做数组解析）→ `$ENGINE_MAP['a,b']` 是 $null →
# `$first.stt` 也是 $null → **把用户原有的 sttModel / meetingSttModel 覆盖成 null**，
# 而且引擎依赖（funasr/transformers）也被静默跳过。宁可不动设置，也不能写空值。
# 2026-10-31：客户端不再使用本地转写引擎 —— 不写 sttModel / meetingSttModel / wakeEnabled。
# 老库里残留的值不影响任何决策（Lead 已把设置项与调用方改成全后端口径）。
if ($ModelsDir) { $values['modelsDir'] = $ModelsDir }
if ($MeetingsDir) { $values['meetingsDir'] = $MeetingsDir }
if ($NotesDir) { $values['worklogVaultRoot'] = $NotesDir; $values['worklogEnabled'] = $true }
switch ($script:AgentChoice) {
    'dsh'   { $values['agentBackend'] = 'dsh'; $values['dshAutoStart'] = $true }
    default { }
}
# 2026-11-03：**不再写任何 harness* 设置**。
# 只有一套 DSH（官方桌面端自带的 dsh），家目录固定 `%USERPROFILE%\.dsh`；
# `agentHarnessEnabled` / `harnessCommand` / `harnessHome` / `harnessPort` /
# `harnessToken` 这些键一律不碰（旧库里残留的值也不影响 ECHO 的 dsh 路径）。
if ($values.Count -gt 0) {
    foreach ($k in $values.Keys) { Say ("{0} = {1}" -f $k, $values[$k]) }
    try { $null = Invoke-Api -Path '/api/settings' -Method Put -Body @{ values = $values } -TimeoutSec 60; Ok '设置已写入' }
    catch { Err ("写设置失败：{0}" -f $_.Exception.Message); $settingsOk = $false }
} else { Say '没有要写的设置' }
}

# 3) 自检：**以 online / ready 为准**，有任何一项没成就非 0 退出（别假装成功）
Step '自检'
$failed = @()
try {
    $st = Invoke-Api -Path '/api/status' -TimeoutSec 20
    foreach ($c in $st.components) {
        # 没选智能体时 dsh 印成 offline 会被读成"坏了"。直接标 skipped 并写清是没选它。
        if ($c.name -eq 'dsh' -and $script:AgentChoice -eq 'none') {
            Say ("{0,-9} {1}  （未使用：安装时选了 -Agent none）" -f $c.name, 'skipped'); continue
        }
        Say ("{0,-9} {1}" -f $c.name, $c.status)
    }
} catch { Warn ("取 /api/status 失败：{0}" -f $_.Exception.Message) }
# 2026-10-31：客户端没有本地 STT 引擎/骨架/模型可自检 —— 转写链路由能力后端负责。
# 2026-11-03：智能体只有 dsh 这一套；拉起之后等 components 里 dsh == online（上限 180s）。
if (-not $settingsOk) {
    $failed += '写入设置（agentBackend=dsh / dshAutoStart=true / 路径）'
}
if (-not $agentPrepared) {
    $failed += '智能体 DSH（官方 DeepSeek Harness 桌面端没就绪：下载/安装失败，或离线/升级时本机没预装）'
} elseif ($script:AgentChoice -ne 'none') {
    if (-not (Wait-Dsh)) { $failed += ("智能体 DSH（{0}）" -f $script:DshWaitReason) }
}

# 两个默认分组（「会议空间」/「指令空间」）：装好就该在 DSH 侧栏看到，而不是等开完
# 第一场会、说第一句指令才冒出来。走 ECHO 自己的接口，幂等；用户改过名的工作区不动
# （见 app/workspaces.py）。放在 Wait-Dsh 之后：workspace/create 要 DSH 正在监听。
Step '建立 DSH 分组（会议空间 / 指令空间）'
if ($KeepSettings -or $script:AgentChoice -eq 'none') {
    # 升级/不动智能体时：分组属于智能体侧状态，保持原样。
    Say '智能体未改动：DSH 分组保持原样（不建、不改）'
} else {
try {
    $sp = Invoke-Api -Path '/api/dsh/workspaces/ensure' -Method Post -Body @{} -TimeoutSec 90
    foreach ($it in $sp.spaces) {
        switch ("$($it.action)") {
            'failed'    { Err ("{0,-10} 建立失败：{1}" -f $it.label, $it.detail); $failed += ("分组「{0}」" -f $it.label) }
            'no-agent'  { Warn ("{0,-10} 智能体没就绪，稍后 ECHO 会自动补：{1}" -f $it.label, $it.detail) }
            'skipped'   { Warn ("{0,-10} 没配目录，跳过（这个空间的会话会落在「未分组」）" -f $it.label) }
            default     { Ok ("{0,-10} 「{1}」→ {2}" -f $it.label, $it.title, $it.path) }
        }
    }
} catch {
    # 不是硬失败：ECHO 建会话时还会再建一次（那时智能体必然已就绪）
    Warn ("调 /api/dsh/workspaces/ensure 失败（不影响使用，ECHO 建会话时会自动补）：{0}" -f $_.Exception.Message)
}
}

# 4) 登记安装 —— 这是"装完了"的凭据：面板据此不再提示未安装、也不再自动进向导
if ($KeepSettings) {
    # 升级模式：**保留原有的安装登记**。用空引擎/agent=none 重写会抹掉"装过什么"的记录。
    Step '登记安装（升级模式：保留原记录）'
    Say '原有安装登记保持原样（不重写）'
} else {
Step '登记安装'
$report = @{
    installer = 'echo-install skill'
    platform  = 'win32'
    destDir   = $script:Root
    engines   = @()
    wake      = $false
    diarize   = $false
    accelCuda = $false
    agent     = $script:AgentChoice
    models    = @()
    dirs      = @{ modelsDir = $ModelsDir; meetingsDir = $MeetingsDir; notesDir = $NotesDir }
    pip       = ($pips -join ' ')
}
try {
    $null = Invoke-Api -Path '/api/install/report' -Method Post -Body @{ report = $report } -TimeoutSec 30
    Ok '已登记（面板不会再提示"还没装完"）'
} catch { Warn ("登记失败（不影响使用）：{0}" -f $_.Exception.Message) }
}

# 5) 结论
Write-Host ''
if ($failed.Count -eq 0) {
    if ($KeepSettings) {
        Write-Host '  升级完成：核心代码已更新。' -ForegroundColor Green
        Say '数据 / 设置 / 模型权重 / 后端与手表配置 / 智能体配置：一律未改动'
        Say '面板与快捷键照旧；不需要再做任何配置。'
        exit 0
    }
    Write-Host '  全部就绪。怎么开始用：' -ForegroundColor White
    Say '1) 双击桌面「ECHO 个人助理」，面板会自动打开（也可以按 Ctrl+Shift+E）'
    Say '2) 对着麦克风说一句试试；开完会在「会议」里看到文字稿'
    Say '3) 以后想加能力：面板 → 能力 → 随时补（不用重装）'
    exit 0
}
if ($KeepSettings) {
    Write-Host ("  核心代码已更新，但**有 {0} 项自检没就绪** —— 别当成升级完整了：" -f $failed.Count) -ForegroundColor Red
} else {
    Write-Host ("  装完了，但**有 {0} 项没就绪** —— 别当成装好了：" -f $failed.Count) -ForegroundColor Red
}
foreach ($f in $failed) { Say ("  - {0}" -f $f) }
Say '补救：照上面的提示装依赖 / 重跑本脚本（已装好的会跳过）；也可以到 面板 → 能力 里重试。'
exit 1
