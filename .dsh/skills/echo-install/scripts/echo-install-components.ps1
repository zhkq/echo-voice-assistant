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
# 参数（2026-10-31 起客户端不再装本地 STT 引擎/唤醒/模型）：
#   -DestDir <目录>        ECHO 安装目录（必填）
#   -Engines <列表>        **已废弃**：传了只警告、不生效
#   -Wake / -Diarize / -AccelCuda   **已废弃**：唤醒取消、分离与 torch 都在能力后端
#   -Agent <名>            智能体后端：harness(默认,= DSH 标准版) / dsh / none
#   -HarnessSource <来源>  harness 从哪来：auto(默认,先用已装的官方桌面端/已有 DSH，没有再下载) /
#                          desktop(没有就下载官方桌面端) / npm(保持旧的本地 npm 安装)
#   -DesktopInstallDir     官方桌面端安装目录（留空=官方默认的 %LOCALAPPDATA%\Programs\DeepSeek Harness）
#   -Offline               离线安装：不联网下载官方桌面端（已装的仍可用）
#   -DshVersion <版本>     标准版本地安装的版本。**默认空 = 装 npm 上的 latest**
#   -PinHarnessCommand     把本地入口的绝对路径写进设置 harnessCommand（默认**不写**）
#   -DshDir <目录>         已有 DSH 的安装目录（给了就不装 npm 包，只探测+写 harnessCommand）
#   -AskDsh                允许问一句"已有 DSH 吗"（只在交互控制台生效）
#   -ModelsDir / -MeetingsDir / -NotesDir   三处位置（留空=默认；-NotesDir 填了会开归档）
#   -PipIndex <url>        国内 pip 镜像（核心依赖由 runtime-core 提供，这里一般用不到）
#   -SkipPip               只装设置/智能体、不装 pip 依赖
#   -KeepSettings          升级模式：**不写任何设置**（智能体 / 路径都保持原样）
#   -SkipModels            升级模式：历史兼容（客户端已没有模型可跳过）
#   -UpgradeDsh            升级模式：用户同意了才升级 DSH 标准版（走 helper 的 -Upgrade）
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
    [string]$Agent = 'harness',
    # 2026-10-31：官方 DeepSeek Harness 桌面端自带 Node + dsh CLI，可以完全跳过 npm/Node。
    # auto   = 先用已装的官方桌面端（或已有 DSH），都没有再联网下载官方桌面端；
    # desktop= 没有就下载官方桌面端（仍失败才回退 npm）；
    # npm    = 保持 2026-10-31 之前的行为（本地 npm 永久安装）。
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
    # 升级模式里用户明确同意时：让 harness 本地安装脚本走 -Upgrade（否则它会按
    # "已装好即用"的快路径跳过，等于没升级）。
    [switch]$UpgradeDsh,
    [int]$WaitSeconds = 1800
)

$ErrorActionPreference = 'Stop'
# -DestDir 是**安装根**。3.0 布局里代码在 <根>\echo-core，数据/模型/DSH/会议/指令都是
# 它的兄弟目录；老式扁平安装里代码就在根下（判据与 app\paths.py:echo_base() 同一条）。
#   $script:Root = 安装根（数据、日志、运行时、DSH 落点都按它算）
#   $script:Code = 代码目录（scripts\ 在这儿）
$script:Root = (Resolve-Path -LiteralPath $DestDir).Path
$script:Code = $script:Root
if ((Split-Path $script:Root -Leaf) -ieq 'echo-core') {
    # 调用方把代码目录当成了安装根（例如从 <根>\echo-core 里重跑）→ 上提一层
    $script:Root = Split-Path $script:Root -Parent
} elseif (-not (Test-Path (Join-Path $script:Root 'app\main.py'))) {
    # 全新安装：代码还没到位，但 3.0 布局已经定了 —— 代码将落在 <根>\echo-core
    $script:Code = Join-Path $script:Root 'echo-core'
}
$script:HarnessCommand = ''    # 本地/已有 DSH 的启动命令（见 Prepare-Agent）
$script:ExistingDsh = $false   # 用户指定了已有 DSH：必须把 harnessCommand 写进设置

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

# ---------------------------------------------------------------- 智能体（标准版 harness）

function Resolve-NodeDir {
    # npx 可能在"托管式" node 里（同事机器上只有 WorkBuddy 的 node，且不在系统 PATH 里）。
    # 候选顺序与 ECHO 运行时的探测保持一致（见 app/platform/win32/env.py 的 node_dirs()）。
    # 2026-10-27：先看 PATH 上的 node —— 已有 DSH 的机器可能只把 node 放 PATH，
    # 没有 npx（也不在下面这批固定目录里）；旧逻辑会误报"没找到 node/npx"。
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return (Split-Path -Parent $cmd.Source) }
    $cands = @()
    $wb = Join-Path $env:USERPROFILE '.workbuddy\binaries\node\versions'
    if (Test-Path $wb) {
        $cands += (Get-ChildItem $wb -Directory -ErrorAction SilentlyContinue |
                   Sort-Object Name -Descending | ForEach-Object { $_.FullName })
    }
    if ($env:APPDATA) { $cands += (Join-Path $env:APPDATA 'nvm\current') }
    foreach ($p in @("$env:ProgramFiles\nodejs", "${env:ProgramFiles(x86)}\nodejs",
                     "$env:LOCALAPPDATA\Programs\nodejs")) {
        if ($p -and $p -notlike '\nodejs') { $cands += $p }
    }
    foreach ($d in $cands) {
        foreach ($n in @('npx.cmd', 'npx.exe', 'npx')) {
            if (Test-Path (Join-Path $d $n)) { return $d }
        }
    }
    return ''
}

function Get-DshProbeHelper {
    # 探测逻辑只有一份（find-existing-dsh.ps1）：install 这条路与以后别的入口共用它。
    $here = $PSScriptRoot
    if (-not $here) { try { $here = Split-Path -Parent $PSCommandPath } catch { $here = '' } }
    if (-not $here) { return '' }
    return (Join-Path $here 'find-existing-dsh.ps1')
}

function Resolve-ExistingDsh {
    param([string]$Raw, [string]$NodeExe = '')
    $helper = Get-DshProbeHelper
    if (-not $helper -or -not (Test-Path -LiteralPath $helper)) {
        return [pscustomobject]@{ ok = $false; entry = ''; node = ''; command = ''; version = '';
                                  error = ('缺 find-existing-dsh.ps1（脚本目录 {0}）' -f $helper) }
    }
    $out = @()
    try {
        # 路径当普通参数传，**不拼命令行**：引号/空格/反斜杠都交给 .ps1 的参数绑定。
        $out = & $helper -Path $Raw -NodeExe $NodeExe 2>&1
    } catch {
        return [pscustomobject]@{ ok = $false; entry = ''; node = ''; command = ''; version = '';
                                  error = $_.Exception.Message }
    }
    $line = @($out | ForEach-Object { "$_" } | Where-Object { $_.Trim().StartsWith('{') } |
              Select-Object -Last 1)
    if (-not $line) {
        return [pscustomobject]@{ ok = $false; entry = ''; node = ''; command = ''; version = '';
                                  error = '探测脚本没有返回结果' }
    }
    try { return ($line | ConvertFrom-Json) } catch {
        return [pscustomobject]@{ ok = $false; entry = ''; node = ''; command = ''; version = '';
                                  error = ('探测结果解析失败：{0}' -f $_.Exception.Message) }
    }
}

function Resolve-AutoDsh {
    param([string[]]$ExtraDir = @(), [string]$NodeExe = '')
    $here = $PSScriptRoot
    if (-not $here) { try { $here = Split-Path -Parent $PSCommandPath } catch { $here = '' } }
    $helper = ''
    if ($here) { $helper = Join-Path $here 'find-existing-dsh-auto.ps1' }
    if (-not $helper -or -not (Test-Path -LiteralPath $helper)) {
        return [pscustomobject]@{ ok = $false; entry = ''; node = ''; command = ''; version = '';
                                  error = ('缺 find-existing-dsh-auto.ps1（脚本目录 {0}）' -f $here) }
    }
    $probeArgs = @{ ExtraDir = @($ExtraDir) }
    if ($NodeExe) { $probeArgs['NodeExe'] = $NodeExe }
    $out = @()
    try { $out = & $helper @probeArgs 2>&1 } catch {
        return [pscustomobject]@{ ok = $false; entry = ''; node = ''; command = ''; version = '';
                                  error = $_.Exception.Message }
    }
    $line = @($out | ForEach-Object { "$_" } | Where-Object { $_.Trim().StartsWith('{') } |
              Select-Object -Last 1)
    if (-not $line) {
        return [pscustomobject]@{ ok = $false; entry = ''; node = ''; command = ''; version = '';
                                  error = '自动探测脚本没有返回结果' }
    }
    try { return ($line | ConvertFrom-Json) } catch {
        return [pscustomobject]@{ ok = $false; entry = ''; node = ''; command = ''; version = '';
                                  error = ('自动探测结果解析失败：{0}' -f $_.Exception.Message) }
    }
}

function Resolve-OfficialDesktop {
    # 2026-10-31：官方 DeepSeek Harness 桌面端自带无头 CLI（不需要系统 Node）。
    # 直接探这两个确定位置，**不要**走 find-existing-dsh-auto 的"任意已有 DSH"列表 ——
    # 那会把另一棵 ECHO 的本地 harness 认成"官方桌面端"。
    $cands = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd'),
        (Join-Path $env:ProgramFiles 'DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd')
    )
    if (${env:ProgramFiles(x86)}) {
        $cands += (Join-Path ${env:ProgramFiles(x86)} 'DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd')
    }
    foreach ($c in $cands) {
        if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) {
            return Resolve-ExistingDsh $c
        }
    }
    return $null
}

function Ask-ExistingDsh {
    # 只在 -AskDsh 且真有交互控制台时进来；无人值守/agent 默认不走这里。
    $interactive = $true
    try { if ([Console]::IsInputRedirected) { $interactive = $false } } catch { $interactive = $false }
    if (-not $interactive) {
        Warn '没有交互控制台：按"新装 DSH"继续（要指定已有目录请用 -DshDir）'
        return $null
    }
    Write-Host ''
    Write-Host '    这台机器上已经有 DSH 标准版吗？' -ForegroundColor White
    Say '  有就选 y：我会先自动探测常见安装位置；探测不到再让你填目录。'
    Say '  直接回车 = 按新装处理（和现在一样，从 npm 装到 ECHO 的 dsh\app）。'
    $ans = ''
    try { $ans = Read-Host '    已有 DSH？(y/N)' } catch { }
    if (('{0}' -f $ans).Trim() -notmatch '^(y|yes|是)$') { return $null }

    # 先自动探测常见位置（2026-10-27 用户要求）：找到就给用户确认，
    # 不再一上来就让人手填路径。探测不到才进入手动输入。
    $auto = Resolve-AutoDsh -ExtraDir @($script:Root)
    if ($auto.ok) {
        Ok ('自动探测到已有 DSH：{0}' -f $auto.entry)
        if ($auto.version) { Say ('  DSH 版本：{0}' -f $auto.version) }
        Say ('  启动命令：{0}' -f $auto.command)
        $use = ''
        try { $use = Read-Host '    用这个已有 DSH 吗？(Y/n)' } catch { }
        if (('{0}' -f $use).Trim() -notmatch '^(n|no|否)$') { return $auto }
        Say '  好，手动指定另一个目录。'
    } else {
        Warn ('没有在常见位置自动找到已有 DSH：{0}' -f $auto.error)
        Say '  也可以直接回车，改为按"新装 DSH"继续。'
    }

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $raw = ''
        try {
            $raw = Read-Host ('    已有 DSH 的安装目录（第 {0}/3 次；可直接粘贴"复制为路径"的结果）' -f $attempt)
        } catch { }
        $probe = Resolve-ExistingDsh $raw
        if ($probe.ok) { return $probe }
        Warn ('没探测到可用 DSH：{0}' -f $probe.error)
        if ($attempt -lt 3) {
            $more = ''
            try { $more = Read-Host '    再输一次？(Y/n)' } catch { }
            if (('{0}' -f $more).Trim() -match '^(n|no|否)$') { break }
        }
    }
    $fallback = ''
    try { $fallback = Read-Host '    3 次都没探测到。改为按"新装 DSH"继续？(Y/n)' } catch { }
    if (('{0}' -f $fallback).Trim() -notmatch '^(n|no|否)$') { return $null }
    return [pscustomobject]@{ ok = $false; entry = ''; node = ''; command = ''; version = '';
                              error = '没有探测到已有的 DSH 安装目录（3 次）' }
}

function Prepare-Agent {
    # 标准版 harness **本地永久安装 + 绝对路径直连**（2026-09-22 同事实测后改）：
    #   同一台机器同一个 dsh：
    #     npx -y @deepseek-ai/dsh web   →  就绪 **2 分 10 秒**（1403 行 npm warn cleanup）
    #     node <本地 bin.js> web        →  就绪 **9 秒**
    #   慢的锅不在 dsh 也不在用户机器，就在 npx 这一层（每次冷启动都要重新解析安装 + 回滚清理）。
    #   绝对路径还顺带绕开两个坑：① 进程 PATH 里没有 node（桌面快捷方式启动时）；
    #   ② 宿主（WorkBuddy 等）的安全删除 shim 拦 npm 批量删除。
    # 失败时**回退**到 npx（ECHO 的默认命令），不让安装流程卡死。
    if ($Agent -ne 'harness') { return $true }
    Step '准备智能体（标准版 harness）'

    # 2026-10-27 用户口径：安装时先问"是否已经有 DSH"。
    #   * 已有：让用户指定目录 → 探测 → 只写 harnessCommand，**绝不**再下 npm 包；
    #   * 新装：走下面原来的本地永久安装（行为不变）。
    # -DshDir 是显式参数（无人值守/agent 可直接给）；-AskDsh 才允许交互提问。
    $probe = $null
    if ($DshDir) {
        $probe = Resolve-ExistingDsh $DshDir
        if (-not $probe.ok) {
            Err ('指定的已有 DSH 目录不可用：{0}' -f $probe.error)
            Err ('  目录：{0}' -f $DshDir)
            Err '  期望能找到 node_modules\@deepseek-ai\dsh\lib\bin.js（或直接指到该 bin.js）。'
            return $false
        }
    } elseif ($AskDsh) {
        $probe = Ask-ExistingDsh
        if ($probe -and -not $probe.ok) {
            Err ('已有 DSH 没探测到：{0}' -f $probe.error)
            Err '  重跑安装并选"新装"，或换一个含 @deepseek-ai\dsh\lib\bin.js 的目录。'
            return $false
        }
    }
    if ($probe -and $probe.ok) {
        $script:ExistingDsh = $true
        $script:HarnessCommand = [string]$probe.command
        if ($probe.entry -match '(?i)dsh\.cmd$') {
            Ok ('使用官方 DeepSeek Harness 桌面端自带的 dsh：{0}' -f $probe.entry)
        } else {
            Ok ('使用已有 DSH：{0}' -f $probe.entry)
        }
        if ($probe.version) { Say ('  DSH 版本：{0}' -f $probe.version) }
        Say ('  启动命令：{0}' -f $script:HarnessCommand)
        return $true
    }

    # 2026-10-31：没有显式给目录时，先找"已经装好的官方桌面端"。
    # 官方安装包自带 node + dsh，`resources\runtime\cli\bin\dsh.cmd` 就是无头 CLI；
    # 用它等于跳过 Node/npm（用户要求：不把安装包塞进交付包，装的时候从官方地址下最新版）。
    if (-not $probe -and $HarnessSource -ne 'npm') {
        $official = Resolve-OfficialDesktop
        if ($official -and $official.ok) { $probe = $official }
    }
    if ($probe -and $probe.ok) {
        $script:ExistingDsh = $true
        $script:HarnessCommand = [string]$probe.command
        if ($probe.entry -match '(?i)dsh\.cmd$') {
            Ok ('使用官方 DeepSeek Harness 桌面端自带的 dsh：{0}' -f $probe.entry)
        } else {
            Ok ('自动找到已有 DSH：{0}' -f $probe.entry)
        }
        if ($probe.version) { Say ('  DSH 版本：{0}' -f $probe.version) }
        Say ('  启动命令：{0}' -f $script:HarnessCommand)
        return $true
    }

    # 没有已装的官方桌面端：联网下载官方安装包并静默安装（从官方地址拿 latest，
    # 所以 ECHO 的交付包里不需要放这个 277MB 的 exe，升级也只需重跑这一步）。
    if ($HarnessSource -ne 'npm' -and -not $Offline -and -not $KeepSettings) {
        $here = $PSScriptRoot
        if (-not $here) { try { $here = Split-Path -Parent $PSCommandPath } catch { $here = '' } }
        $desktopHelper = ''
        if ($here) { $desktopHelper = Join-Path $here 'harness-install-desktop.ps1' }
        if ($desktopHelper -and (Test-Path -LiteralPath $desktopHelper)) {
            Step '下载并安装官方 DeepSeek Harness（自带 Node/dsh，跳过 npm）'
            Say '  从 download.deepseek.com 下载最新版（约 277 MB），然后静默安装。'
            $dargs = @('-LogFile', $script:InstallLog)
            if ($DesktopInstallDir) { $dargs += @('-InstallDir', $DesktopInstallDir) }
            if ($UpgradeDsh) { $dargs += '-Upgrade' }
            $dout = @()
            try { $dout = & $desktopHelper @dargs 2>&1 } catch {
                Warn ('下载/安装官方桌面端失败：{0}' -f $_.Exception.Message)
            }
            $dline = @($dout | ForEach-Object { "$_" } | Where-Object { $_ -like 'HARNESS_COMMAND=*' } |
                       Select-Object -Last 1)
            if ($dline.Count -gt 0) {
                $script:ExistingDsh = $true
                $script:HarnessCommand = ([string]$dline[-1]).Substring('HARNESS_COMMAND='.Length)
                $vline = @($dout | ForEach-Object { "$_" } | Where-Object { $_ -like 'DSH_VERSION=*' } |
                           Select-Object -Last 1)
                Ok '官方 DeepSeek Harness 已就绪（自带 dsh CLI）'
                if ($vline.Count -gt 0) { Say ('  DSH 版本：{0}' -f ([string]$vline[-1]).Substring('DSH_VERSION='.Length)) }
                Say ('  启动命令：{0}' -f $script:HarnessCommand)
                return $true
            }
            Warn '官方桌面端没装成 —— 回退到本地 npm 安装'
            if ($HarnessSource -eq 'desktop') { return $false }
        } else {
            Warn ('缺 harness-install-desktop.ps1（脚本目录 {0}）—— 回退到本地 npm 安装' -f $here)
        }
    }

    Step '准备智能体（标准版 harness：本地永久安装）'
    $nodeDir = Resolve-NodeDir
    if (-not $nodeDir) {
        Warn '没找到 node/npx —— 标准版 harness 需要本机有 Node.js'
        Say '  装了 Node 再重跑本脚本；装了但不在 PATH 里时，下面会把全路径自动写进设置。'
        return $false
    }
    $node = Join-Path $nodeDir 'node.exe'
    if (-not (Test-Path $node)) { $node = Join-Path $nodeDir 'node' }
    Ok ("node: {0}" -f $node)

    # 装/修本地永久入口交给专门的脚本：它按"① 已装好 ② npm install ③ 从 npx 缓存复制"
    # 三条路依次试。**为什么要留着第 ③ 条**：`@deepseek-ai/dsh@0.1.5-rc.2` 的依赖图出过
    # registry 事故（子包依赖 ^0.1.5-rc.3，而那个 rc.3 当时没发布），npm 会 ETARGET ——
    # 老逻辑这时只会回退 npx，用户那边每次冷启动都要 2 分钟。该事故 2026-09-22 晚复测已恢复
    # （rc.3 发布了、npm 584 包装成），但这类事故会复发，而 npx 慢是必然的，故三条路都保留。
    # $PSScriptRoot 在 -File 执行时一定有；内联/点源时可能为空，退一步用 $PSCommandPath
    $here = $PSScriptRoot
    if (-not $here) { try { $here = Split-Path -Parent $PSCommandPath } catch { $here = '' } }
    $helper = ''
    if ($here) { $helper = Join-Path $here 'harness-install-local.ps1' }
    if (-not $helper -or -not (Test-Path -LiteralPath $helper)) {
        Warn ("缺 harness-install-local.ps1（脚本目录 {0}）—— 回退到 npx（首次启动要多等 1-2 分钟）" -f $here)
        return $false
    }
    $cmdFile = Join-Path $env:TEMP ("echo-harness-command-{0}.txt" -f $PID)
    $out = @()
    try {
        $helperArgs = @('-DestDir', $script:Root, '-Version', $script:DshVersion,
                        '-NodeExe', $node, '-CommandFile', $cmdFile, '-LogFile', $script:InstallLog)
        if ($UpgradeDsh) { $helperArgs += '-Upgrade' }
        $out = & $helper @helperArgs
    } catch {
        Warn ("装本地标准版时出错：{0}" -f $_.Exception.Message)
    }
    $cmdLine = @($out | Where-Object { $_ -like 'HARNESS_COMMAND=*' })
    if (Test-Path -LiteralPath $cmdFile) {
        $script:HarnessCommand = (Get-Content -LiteralPath $cmdFile -Raw).Trim()
        Remove-Item -LiteralPath $cmdFile -Force -ErrorAction SilentlyContinue
    } elseif ($cmdLine.Count -gt 0) {
        $script:HarnessCommand = ([string]$cmdLine[-1]).Substring('HARNESS_COMMAND='.Length)
    }
    if (-not $script:HarnessCommand) {
        Warn '标准版本地永久安装没成 —— 回退到 npx，首次启动要多等 1-2 分钟（日志里有原因）'
        return $false
    }
    Ok '标准版已就绪（本地永久安装，冷启动约 10 秒）'
    Say ("  启动命令：{0}" -f $script:HarnessCommand)
    return $true
}

function Get-HarnessState {
    # 标准版 harness 的状态在 /api/status 的 **components** 里（name = 'harness'）。
    # 顶层 `dsh` 是 DSH **桌面版**那条适配器（app/api.py → manager.dsh_ready()），
    # `-Agent harness` 时它本来就该是 offline —— 拿它判 harness 会**恒为假**。
    # 2026-09-22 同事实测：装完白等 150 秒、报"还差 1 项"并 EXITCODE=1，
    # 而那一刻 /api/status 里 harness 明明是 online。
    param($Status)
    $h = @($Status.components | Where-Object { $_.name -eq 'harness' })
    if ($h.Count -eq 0) { return $null }
    return $h[0]
}

function Wait-Harness {
    if ($Agent -ne 'harness') { return $true }
    # 等待时长分档：本地永久安装（绝对路径直连）实测 ~11 秒就起；走 npx 才要一两分钟。
    # 从前一律 150 秒 —— 那是照 npx 时代定的，失败时白等两分半，还把提示指向"从 npm 拉包慢"。
    $local = [bool]$script:HarnessCommand
    $Seconds = 45
    $how = '本地永久安装，通常 10 秒内'
    if (-not $local) { $Seconds = 150; $how = 'npx 路径，首次要 1-2 分钟' }
    Step ("等智能体就绪（{0}）" -f $how)
    $deadline = (Get-Date).AddSeconds($Seconds)
    $last = ''
    while ((Get-Date) -lt $deadline) {
        try {
            $st = Invoke-Api -Path '/api/status' -TimeoutSec 10
            $h = Get-HarnessState $st
            if ($h -and $h.status -eq 'online') {
                Ok ("智能体已就绪（harness online：{0}）" -f $h.detail)
                return $true
            }
            if ($h -and $h.status -eq 'failed') {
                # 它已经明确失败了就不要再等满 —— 把 detail 原样说出来（多半是 node 不在 PATH / 装残）
                Warn ("智能体启动失败：{0}" -f $h.detail)
                return $false
            }
            if ($h) { $last = [string]$h.status }
        } catch { }
        Start-Sleep -Seconds 3
    }
    if (-not $last) { $last = '取不到状态' }
    Warn ("等 {0}s harness 仍未 online（最后状态：{1}）—— 看 data\logs\harness.log" -f $Seconds, $last)
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

$null = Prepare-Agent          # 先把 npm 包预热好，免得写入设置后 ECHO 拉起时干等
Ensure-Service

# 2026-10-31：客户端不装本地模型 —— 没有"跳过缺依赖引擎的模型下载"这一段。

# 2) 写设置（与面板同一套键）
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
switch ($Agent) {
    'harness' { $values['agentBackend'] = 'harness'; $values['agentHarnessEnabled'] = $true }
    'dsh'     { $values['agentBackend'] = 'dsh' }
    default   { }
}
# **默认不写 harnessCommand**（2026-09-22 晚改）：设置停在出厂值时，ECHO 自己会优先用本地
# 入口（`harness_proc.command()`：出厂值 + 本地入口存在 -> 直连，冷启动约 10 秒），node 也由
# 它探测（`node_dirs()` 与上面 Resolve-NodeDir 是同一批目录）。好处：node 换版本
# （WorkBuddy / nvm 升级）不用改配置 —— 写死绝对路径时那条路径一失效 harness 就起不来，
# 而这个设置是隐藏项，用户很难自己找到。想钉死：加 -PinHarnessCommand。
# 没装成本地入口时也不用写：ECHO 用默认 npx 命令兜底（首次慢 1-2 分钟，但能用）。
# 已有 DSH 的用户给了外部目录：**必须**写 harnessCommand，否则 ECHO 不知道去哪找。
# 新装本地入口仍按原口径默认不写（除非显式 -PinHarnessCommand）。
$writeCommand = ($script:HarnessCommand -and $PinHarnessCommand)
if ($script:ExistingDsh) { $writeCommand = $true }
if ($writeCommand) { $values['harnessCommand'] = $script:HarnessCommand }
if ($values.Count -gt 0) {
    foreach ($k in $values.Keys) { Say ("{0} = {1}" -f $k, $values[$k]) }
    try { $null = Invoke-Api -Path '/api/settings' -Method Put -Body @{ values = $values } -TimeoutSec 60; Ok '设置已写入' }
    catch { Err ("写设置失败：{0}" -f $_.Exception.Message) }
} else { Say '没有要写的设置' }
}

# 3) 自检：**以 import / ready / online 为准**，有任何一项没成就非 0 退出（别假装成功）
Step '自检'
$failed = @()
try {
    $st = Invoke-Api -Path '/api/status' -TimeoutSec 20
    foreach ($c in $st.components) {
        # 未选中的那个智能体印成 offline 会被读成"坏了"——同事已被这条误导过两次
        # （上一轮"dsh offline 是不是坏了"，这一轮又是启动页）。直接标 skipped 并写清你没选它。
        if ($c.name -eq 'dsh' -and $Agent -eq 'harness') {
            Say ("{0,-9} {1}  （未使用：你选的是标准版 harness）" -f $c.name, 'skipped'); continue
        }
        if ($c.name -eq 'harness' -and $Agent -ne 'harness') {
            Say ("{0,-9} {1}  （未使用：你选的是 {2}）" -f $c.name, 'skipped', $Agent); continue
        }
        Say ("{0,-9} {1}" -f $c.name, $c.status)
    }
} catch { Warn ("取 /api/status 失败：{0}" -f $_.Exception.Message) }
# 2026-10-31：客户端没有本地 STT 引擎/骨架/模型可自检 —— 转写链路由能力后端负责。
$agentOk = Wait-Harness
if (-not $agentOk) { $failed += '智能体（harness）' }

# 两个默认分组（「会议空间」/「指令空间」）：装好就该在 DSH 侧栏看到，而不是等开完
# 第一场会、说第一句指令才冒出来。走 ECHO 自己的接口，幂等；用户改过名的工作区不动
# （见 app/workspaces.py）。放在 Wait-Harness 之后：workspace/create 要 DSH 正在监听。
Step '建立 DSH 分组（会议空间 / 指令空间）'
if ($KeepSettings -or $Agent -eq 'none') {
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
    agent     = $Agent
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
