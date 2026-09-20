# =====================================================================
# echo-install-components.ps1 - 按需求把 ECHO 的引擎依赖 / 模型 / 设置装好
#
# 谁在用它：`.dsh/skills/echo-install`（安装专用技能）。技能先跟用户确认"要哪些组件"，
# 再把选择翻译成本脚本的参数。**不用 git、不用 GitHub**：依赖走 PyPI，模型走
# ModelScope / hf-mirror（ECHO 内置 `HF_ENDPOINT=https://hf-mirror.com`）。
#
# 前提：`install.ps1` 已经把主包解开、`runtime-core` 就绪（本脚本自己会检查，缺了就报错）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File echo-install-components.ps1 `
#       -DestDir D:\ECHO -Engines sherpa,whisper-base -Wake
#
# 参数：
#   -DestDir <目录>        ECHO 安装目录（必填）
#   -Engines <列表>        转写引擎，逗号分隔。可选：
#                          sherpa(189MB,免 torch) / whisper-tiny(75) / whisper-base(141)
#                          / whisper-small(464) / whisper-medium(1500) / whisper-large-v3(2950)
#                          / sensevoice(896,需 torch) / qwen3asr(3.6GB,需独显)
#   -Wake                  装唤醒词 KWS（40 MB，默认不装：要常开麦克风）
#   -Diarize               装说话人分离（pyannote，需 HF 授权，重依赖）
#   -AccelCuda             装 CUDA 版 torch（需 N 卡）
#   -Agent <名>            智能体后端：harness(默认,= DSH 标准版) / dsh / none
#   -ModelsDir / -MeetingsDir / -NotesDir   三处位置（留空=默认；-NotesDir 填了会开归档）
#   -PipIndex <url>        国内 pip 镜像，例如 https://pypi.tuna.tsinghua.edu.cn/simple
#   -SkipPip               只下模型、不装 pip 依赖
#   -WaitSeconds <秒>      等模型下载的上限（默认 1800）
#
# 编码：含中文，所以**必须 UTF-8 带 BOM**（WinPS 5.1 对无 BOM 的 .ps1 按 ANSI 读，
#       中文会乱码甚至静默解析失败）—— 见 tests/test_script_encoding.py。
# =====================================================================
param(
    [Parameter(Mandatory = $true)][string]$DestDir,
    [string[]]$Engines = @('sherpa'),
    [switch]$Wake,
    [switch]$Diarize,
    [switch]$AccelCuda,
    [string]$Agent = 'harness',
    [string]$ModelsDir = '',
    [string]$MeetingsDir = '',
    [string]$NotesDir = '',
    [string]$PipIndex = '',
    [switch]$SkipPip,
    [int]$WaitSeconds = 1800
)

$ErrorActionPreference = 'Stop'
$script:Root = (Resolve-Path -LiteralPath $DestDir).Path

function Say([string]$m)  { Write-Host "  $m" }
function Ok([string]$m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Err([string]$m)  { Write-Host "  [fail] $m" -ForegroundColor Red }
function Step([string]$m) { Write-Host ''; Write-Host "== $m" -ForegroundColor Cyan }

# 引擎 → (pip 依赖 / 要下的 model_id / 写进 sttModel 的值)
# 依据：app/audio/stt.py 的 _parse_choice()（sttModel 取值 = sherpa|sensevoice|qwen3asr|whisper 档名）
#       与 app/components.py 的清单（model_id / 体积 / 来源）。
$ENGINE_MAP = @{
    'sherpa'           = @{ pip = @('sherpa-onnx');                       model = 'sherpa';           stt = 'sherpa' }
    'whisper-tiny'     = @{ pip = @('faster-whisper', 'huggingface-hub'); model = 'whisper-tiny';     stt = 'tiny' }
    'whisper-base'     = @{ pip = @('faster-whisper', 'huggingface-hub'); model = 'whisper-base';     stt = 'base' }
    'whisper-small'    = @{ pip = @('faster-whisper', 'huggingface-hub'); model = 'whisper-small';    stt = 'small' }
    'whisper-medium'   = @{ pip = @('faster-whisper', 'huggingface-hub'); model = 'whisper-medium';   stt = 'medium' }
    'whisper-large-v3' = @{ pip = @('faster-whisper', 'huggingface-hub'); model = 'whisper-large-v3'; stt = 'large-v3' }
    'sensevoice'       = @{ pip = @('funasr', 'modelscope', 'torch');     model = 'sensevoice';       stt = 'sensevoice' }
    'qwen3asr'         = @{ pip = @('transformers', 'modelscope', 'torch'); model = 'qwen3asr';       stt = 'qwen3asr' }
}

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
    $port = Get-EchoPort
    $args = @{ Uri = "http://127.0.0.1:$port$Path"; Method = $Method; TimeoutSec = $TimeoutSec }
    if ($null -ne $Body) {
        $args.ContentType = 'application/json'
        $args.Body = ($Body | ConvertTo-Json -Depth 6 -Compress)
    }
    return Invoke-RestMethod @args
}

function Ensure-Service {
    Step '启动 ECHO 服务'
    try { $null = Invoke-Api -Path '/api/status' -TimeoutSec 5; Ok '服务已在运行'; return }
    catch { }
    $start = Join-Path $script:Root 'scripts\start.ps1'
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

# ---------------------------------------------------------------- 模型

function Get-ModelState([string]$Id) {
    try {
        $models = Invoke-Api -Path '/api/models'
        foreach ($it in $models.items) { if ($it.id -eq $Id) { return $it } }
    } catch { }
    return $null
}

function Wait-Model([string]$Id) {
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $jobs = (Invoke-Api -Path '/api/models').jobs
            $job = $null
            if ($jobs -and $jobs.PSObject.Properties.Name -contains $Id) { $job = $jobs.$Id }
            if ($job) {
                if ($job.status -eq 'done') { return $true }
                if ($job.status -eq 'error') { Warn ("{0} 下载失败：{1}" -f $Id, $job.error); return $false }
            }
        } catch { }
        Start-Sleep -Seconds 5
    }
    Warn ("等 {0} 超时（{1}s）—— 下载在服务里继续跑，稍后在面板看进度" -f $Id, $WaitSeconds)
    return $false
}

function Install-Model([string]$Id, [string]$Label) {
    $st = Get-ModelState $Id
    if ($st -and $st.ready -eq $true) { Ok ("{0} 已经装好，跳过" -f $Label); return $true }
    Step ("下载模型：{0}" -f $Label)
    try { $r = Invoke-Api -Path '/api/models/download' -Method Post -Body @{ id = $Id } -TimeoutSec 30 }
    catch { Err ("触发下载失败：{0}" -f $_.Exception.Message); return $false }
    if (-not $r.ok) { Warn ("接口说：{0}" -f $r.message); return $false }
    Say '下载中（走 ModelScope / hf-mirror 镜像）…'
    return (Wait-Model $Id)
}

# ---------------------------------------------------------------- 主流程

Write-Host ''
Write-Host '  === ECHO 组件安装（按需下载）===' -ForegroundColor White
Write-Host ("  安装目录：{0}" -f $script:Root)
if (-not (Test-Path (Join-Path $script:Root 'app'))) {
    Err '这个目录里没有 app\ —— 看起来不是 ECHO 安装目录（先跑 install.ps1）'
    exit 1
}
$rcPy = Get-RuntimePython
if (-not $rcPy) { Err '找不到 runtime-core\python.exe —— 先跑 install.ps1'; exit 1 }
Ok ("运行时：{0}" -f $rcPy)

# 1) 依赖 + 模型
$pips = @()
$models = @()
foreach ($e in $Engines) {
    if (-not $ENGINE_MAP.ContainsKey($e)) { Warn ("不认识这个引擎，跳过：{0}" -f $e); continue }
    $m = $ENGINE_MAP[$e]
    $pips += $m.pip
    $models += , @($m.model, $e)
}
if ($Wake) { $pips += 'sherpa-onnx'; $models += , @('kws', '唤醒词 KWS') }
if ($Diarize) {
    $pips += @('pyannote.audio', 'torch')
    Warn '说话人分离（pyannote）是 HF 上的 gated 模型：ECHO 不能替你下载，要在 HF 同意条款后自己拉（见面板 → 组件）'
}
if ($AccelCuda) { $pips += 'torch'; Warn 'CUDA 版 torch 体积大（约 2.5 GB），且要求 N 卡与匹配的驱动' }

Install-PipDeps $pips

Ensure-Service

$modelOk = @{}
foreach ($pair in $models) { $modelOk[$pair[0]] = Install-Model $pair[0] $pair[1] }

# 2) 写设置（与面板同一套键）
Step '写入设置'
$values = @{}
if ($Engines.Count -gt 0) {
    $first = $ENGINE_MAP[$Engines[0]]
    $values['sttModel'] = $first.stt
    # 会议通常要更准：选里有 whisper 档就用它
    $whisper = $Engines | Where-Object { $_ -like 'whisper-*' } | Select-Object -First 1
    $values['meetingSttModel'] = if ($whisper) { $ENGINE_MAP[$whisper].stt } else { $first.stt }
}
if ($Wake) { $values['wakeEnabled'] = $true }
if ($ModelsDir) { $values['modelsDir'] = $ModelsDir }
if ($MeetingsDir) { $values['meetingsDir'] = $MeetingsDir }
if ($NotesDir) { $values['worklogVaultRoot'] = $NotesDir; $values['worklogEnabled'] = $true }
switch ($Agent) {
    'harness' { $values['agentBackend'] = 'harness'; $values['agentHarnessEnabled'] = $true }
    'dsh'     { $values['agentBackend'] = 'dsh' }
    default   { }
}
if ($values.Count -gt 0) {
    foreach ($k in $values.Keys) { Say ("{0} = {1}" -f $k, $values[$k]) }
    try { $null = Invoke-Api -Path '/api/settings' -Method Put -Body @{ values = $values } -TimeoutSec 60; Ok '设置已写入' }
    catch { Err ("写设置失败：{0}" -f $_.Exception.Message) }
} else { Say '没有要写的设置' }

# 3) 自检 + 报告
Step '自检'
try {
    $st = Invoke-Api -Path '/api/status' -TimeoutSec 20
    foreach ($c in $st.components) { Say ("{0,-9} {1}" -f $c.name, $c.status) }
} catch { Warn ("取 /api/status 失败：{0}" -f $_.Exception.Message) }
foreach ($pair in $models) {
    $st2 = Get-ModelState $pair[0]
    $flag = if ($st2 -and $st2.ready -eq $true) { '✓ 就绪' } else { '✗ 还没好' }
    Say ("{0,-22} {1}" -f $pair[1], $flag)
}
Write-Host ''
Write-Host '  完成。剩下的：' -ForegroundColor White
Say '1) 面板：按 Ctrl+Shift+E（或浏览器打开 http://127.0.0.1:<端口>/）'
Say '2) 会议纪要/归档/指令需要"智能体"：面板 → 设置 → 智能体（本脚本已按 -Agent 选好；'
Say '   harness 需要本机有 Node.js，没有就装 Node 或改用已装的 DSH 桌面版）'
Say '3) 说话人分离要 HF 授权；CUDA 加速要 N 卡 —— 都不影响转写与纪要'
