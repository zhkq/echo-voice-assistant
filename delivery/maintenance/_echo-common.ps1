# =====================================================================
#  ECHO 运维脚本 · 公共函数
#  被 1-备份ECHO会议数据.ps1 / 2-清理ECHO.ps1 / 3-还原ECHO会议数据.ps1 dot-source
#  兼容 Windows PowerShell 5.1；本文件为 UTF-8 with BOM。
# =====================================================================
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$script:EchoScriptDir = $PSScriptRoot

# ------------------------------ 输出 ------------------------------
function Write-Head([string]$Text) {
    Write-Host ''
    Write-Host ('=' * 68) -ForegroundColor DarkCyan
    Write-Host ('  ' + $Text) -ForegroundColor Cyan
    Write-Host ('=' * 68) -ForegroundColor DarkCyan
}
function Write-Section([string]$Text) { Write-Host ''; Write-Host ('-- ' + $Text) -ForegroundColor Cyan }
function Write-Step([string]$Text) { Write-Host ('   . ' + $Text) -ForegroundColor Gray }
function Write-Ok([string]$Text) { Write-Host ('   [OK] ' + $Text) -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host ('   ' + $Text) -ForegroundColor DarkGray }
function Write-Warn([string]$Text) { Write-Host ('   [!] ' + $Text) -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host ('   [x] ' + $Text) -ForegroundColor Red }

function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} B' -f $Bytes)
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $txt = [System.IO.File]::ReadAllText($Path, (New-Object System.Text.UTF8Encoding($false)))
        return ($txt | ConvertFrom-Json)
    } catch { return $null }
}

# ------------------------------ ECHO 探测 ------------------------------
function Get-EchoJson([string]$Url, [int]$TimeoutSec = 8) {
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        $bytes = $resp.RawContentStream.ToArray()
        if (-not $bytes -or $bytes.Length -eq 0) { return $null }
        return ([System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
    } catch { return $null }
}

function Get-EchoPort([string]$Base) {
    $f = Join-Path $Base 'data\echo-port.txt'
    if (Test-Path -LiteralPath $f) {
        try {
            $raw = [System.IO.File]::ReadAllText($f).Trim()
            if ($raw -match '^\d{2,5}$') { return [int]$raw }
        } catch { }
    }
    return 8970
}

function Get-EchoStatus([string]$Base) {
    $port = Get-EchoPort $Base
    return (Get-EchoJson ("http://127.0.0.1:{0}/api/status" -f $port) 5)
}

function Test-EchoCandidatePath([string]$Path) {
    if (-not $Path) { return '' }
    $p = ([string]$Path).Trim().Trim('"').TrimEnd('\')
    if (-not $p) { return '' }
    if ((Split-Path $p -Leaf) -ieq 'echo-core') { $p = Split-Path $p -Parent }
    if ((Test-Path -LiteralPath (Join-Path $p 'echo-core\app\main.py')) -or
        (Test-Path -LiteralPath (Join-Path $p 'app\main.py'))) { return $p }
    return ''
}

function Find-EchoBase([string]$Hint) {
    foreach ($c in @($Hint, $env:ECHO_ROOT)) {
        $r = Test-EchoCandidatePath $c
        if ($r) { return $r }
    }
    # 1) 正在运行的实例（命令行里带 <base>\echo-core\）
    try {
        foreach ($p in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
            $cl = [string]$p.CommandLine
            if (-not $cl) { continue }
            if ($cl -match '([A-Za-z]:\\[^"]*?)\\echo-core\\') {
                $r = Test-EchoCandidatePath $Matches[1]
                if ($r) { return $r }
            }
        }
    } catch { }
    # 2) 切换器登记（.echo-instances.json）里的当前实例，其次是登记的全部实例
    $inst = Read-JsonFile (Join-Path $env:USERPROFILE '.echo-instances.json')
    if ($inst -and $inst.instances) {
        $order = @()
        if ($inst.current) { $order += [string]$inst.current }
        foreach ($prop in $inst.instances.PSObject.Properties) { $order += [string]$prop.Name }
        foreach ($name in $order) {
            $entry = $inst.instances.PSObject.Properties[$name]
            if ($entry -and $entry.Value -and $entry.Value.root) {
                $r = Test-EchoCandidatePath ([string]$entry.Value.root)
                if ($r) { return $r }
            }
        }
    }
    # 3) 常见安装目录
    foreach ($c in @('D:\ECHO', 'C:\ECHO', 'D:\echo', 'C:\echo')) {
        $r = Test-EchoCandidatePath $c
        if ($r) { return $r }
    }
    # 4) 各盘一级目录扫描（限制深度，避免很慢）
    try {
        foreach ($d in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^[C-Z]$' })) {
            if (-not $d.Root) { continue }
            foreach ($x in (Get-ChildItem -LiteralPath $d.Root -Directory -ErrorAction SilentlyContinue | Select-Object -First 100)) {
                $r = Test-EchoCandidatePath $x.FullName
                if ($r) { return $r }
            }
        }
    } catch { }
    return ''
}

function Get-EchoLayout([string]$Base) {
    $b = ([string]$Base).Trim().Trim('"').TrimEnd('\')
    $layout = [pscustomobject]@{
        Base = $b; Code = ''; Data = (Join-Path $b 'data'); Meetings = ''
        Layout = ''; Port = 0; ServiceUp = $false; Version = ''
    }
    if (Test-Path -LiteralPath (Join-Path $b 'echo-core\app\main.py')) {
        $layout.Code = Join-Path $b 'echo-core'; $layout.Layout = '3.0'
    } elseif (Test-Path -LiteralPath (Join-Path $b 'app\main.py')) {
        $layout.Code = $b; $layout.Layout = '1.x'
    }
    $layout.Port = Get-EchoPort $b
    $st = Get-EchoStatus $b
    if ($st) { $layout.ServiceUp = $true; $layout.Version = [string]$st.version }
    if (-not $layout.Version) {
        $doc = Read-JsonFile (Join-Path $layout.Code 'manifest.json')
        if ($doc -and $doc.appVersion) { $layout.Version = [string]$doc.appVersion }
    }
    $envJson = Get-EchoJson ("http://127.0.0.1:{0}/api/paths/env" -f $layout.Port)
    if ($envJson -and $envJson.roots) {
        foreach ($r in $envJson.roots) { if ($r.name -eq 'MEETINGS' -and $r.path) { $layout.Meetings = [string]$r.path } }
    }
    if (-not $layout.Meetings) {
        foreach ($c in @((Join-Path $b 'meeting'), (Join-Path $layout.Data 'meetings'), (Join-Path $b 'meetings'))) {
            if (Test-Path -LiteralPath $c) { $layout.Meetings = $c; break }
        }
    }
    if (-not $layout.Meetings) { $layout.Meetings = Join-Path $b 'meeting' }
    return $layout
}

function Get-EchoPython([string]$Base) {
    $cands = @(
        (Join-Path $Base 'runtime-core\python.exe'),
        (Join-Path $Base 'runtime-core\Scripts\python.exe'),
        (Join-Path $Base 'venv\Scripts\python.exe'),
        (Join-Path $Base 'runtime\Scripts\python.exe'),
        (Join-Path $Base '.venv\Scripts\python.exe'),
        (Join-Path $Base 'echo-core\.venv\Scripts\python.exe')
    )
    foreach ($p in $cands) { if (Test-Path -LiteralPath $p) { return $p } }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return ''
}

function Invoke-EchoPython([string]$Python, [string[]]$Arguments) {
    $env:PYTHONIOENCODING = 'utf-8'
    $out = & $Python @Arguments 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        $text = ([string]::Join([Environment]::NewLine, @($out | ForEach-Object { [string]$_ }))).Trim()
        throw ('数据库操作失败（退出码 ' + $code + '）' + [Environment]::NewLine + $text)
    }
    return $out
}

# ------------------------------ 目录统计 ------------------------------
$script:EchoAudioExt = @('.flac', '.wav', '.m4a', '.mp3', '.ogg', '.aac')

function Get-PathStats([string]$Path, [switch]$NoAudio) {
    $result = [pscustomobject]@{ Dirs = 0; Files = 0; Bytes = [long]0 }
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $result }
    $result.Dirs = @(Get-ChildItem -LiteralPath $Path -Recurse -Directory -ErrorAction SilentlyContinue).Count
    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue)
    if ($NoAudio) { $files = @($files | Where-Object { $script:EchoAudioExt -notcontains $_.Extension.ToLowerInvariant() }) }
    $result.Files = $files.Count
    if ($files.Count -gt 0) {
        $result.Bytes = [long](($files | Measure-Object -Property Length -Sum).Sum)
    }
    return $result
}

# ------------------------------ 启停 ------------------------------
# 切换器登记里的"当前实例"（.echo-instances.json）。没有登记时返回空串。
function Get-EchoCurrentBase {
    $inst = Read-JsonFile (Join-Path $env:USERPROFILE '.echo-instances.json')
    if (-not $inst -or -not $inst.current -or -not $inst.instances) { return '' }
    $entry = $inst.instances.PSObject.Properties[[string]$inst.current]
    if (-not $entry -or -not $entry.Value -or -not $entry.Value.root) { return '' }
    $root = ([string]$entry.Value.root).TrimEnd('\')
    if ((Split-Path $root -Leaf) -ieq 'echo-core') { $root = Split-Path $root -Parent }
    return $root.TrimEnd('\')
}

# 只有"当前实例"才去动切换器 / 边条缓存这类**全局**进程；
# 清理另一个（非当前）安装时，不能把正在用的那套的看门狗和边条一起杀掉。
function Test-EchoBaseIsCurrent([string]$Base) {
    $cur = Get-EchoCurrentBase
    if (-not $cur) { return $true }
    return ($cur.TrimEnd('\') -ieq $Base.TrimEnd('\'))
}

# 保护当前进程及其祖先进程：调用本脚本的终端/包装进程，命令行里很可能也带着
# 安装目录（例如 powershell -File D:\ECHO\... ），绝不能把它们一起杀掉。
function Get-EchoProtectedPids {
    $set = New-Object System.Collections.ArrayList
    [void]$set.Add([int]$PID)
    try {
        $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
        $byId = @{}
        foreach ($p in $procs) { $byId[[int]$p.ProcessId] = $p }
        $cur = [int]$PID
        for ($i = 0; $i -lt 12; $i++) {
            $p = $byId[$cur]
            if (-not $p) { break }
            $ppid = [int]$p.ParentProcessId
            if ($ppid -le 0 -or $ppid -eq $cur) { break }
            if (-not $set.Contains($ppid)) { [void]$set.Add($ppid) }
            $cur = $ppid
        }
    } catch { }
    return $set
}

# 只认 ECHO 自己会用的进程名（DSH 桌面版 "DeepSeek Harness.exe" 不在此列，绝不动它）
function Test-EchoOwnedProcess([object]$Process, [string]$LowerBase, [bool]$IsCurrent) {
    if ([int]$Process.ProcessId -eq $PID) { return $false }
    if ([string]$Process.Name -notmatch '^(echo-sidebar|python|pythonw|powershell|pwsh|node|wscript|cscript|cmd|msedgewebview2)\.exe$') { return $false }
    $cl = [string]$Process.CommandLine
    if (-not $cl) { return $false }
    $cl2 = $cl.ToLowerInvariant()
    if ($cl2.Contains($LowerBase + '\') -or $cl2.Contains($LowerBase + '"')) { return $true }
    if ($IsCurrent -and ($cl2.Contains('\.echo-switch\') -or $cl2.Contains('\echo-sidebar\'))) { return $true }
    return $false
}

function Stop-EchoAll([string]$Base) {
    $lower = $Base.ToLowerInvariant()
    $isCurrent = Test-EchoBaseIsCurrent $Base
    $protect = Get-EchoProtectedPids
    $killed = New-Object System.Collections.ArrayList
    $stop = Join-Path $Base 'echo-core\scripts\stop.ps1'
    if (-not (Test-Path -LiteralPath $stop)) { $stop = Join-Path $Base 'scripts\stop.ps1' }
    if (Test-Path -LiteralPath $stop) {
        try { $null = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stop -Quiet 2>&1 } catch { }
        Start-Sleep -Milliseconds 900
    }
    for ($round = 1; $round -le 4; $round++) {
        $procs = @()
        try { $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) } catch { }
        $hit = @()
        foreach ($p in $procs) {
            if ($protect.Contains([int]$p.ProcessId)) { continue }
            if (-not (Test-EchoOwnedProcess $p $lower $isCurrent)) { continue }
            $hit += $p
        }
        if ($hit.Count -eq 0) { break }
        foreach ($p in $hit) {
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                [void]$killed.Add(('{0} (pid {1})' -f $p.Name, $p.ProcessId))
            } catch { }
        }
        Start-Sleep -Milliseconds 800
    }
    return $killed
}

function Wait-EchoDown([string]$Base, [int]$TimeoutSec = 25) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-EchoStatus $Base)) { return $true }
        Start-Sleep -Milliseconds 700
    }
    return (-not (Get-EchoStatus $Base))
}

function Start-EchoDetached([string]$Code) {
    $start = Join-Path $Code 'scripts\start.ps1'
    if (-not (Test-Path -LiteralPath $start)) { throw ('找不到启动脚本: ' + $start) }
    # 注意：不要把 -WindowStyle Hidden 放进 powershell 的参数里 —— 这台机器的组策略会
    # 直接拒绝“隐藏窗口的 powershell 启动”（报 Access denied by policy rule）。
    # 隐藏窗口由 Start-Process 的 -WindowStyle 控制，足够。
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $start, '-Background'
    ) | Out-Null
}

function Wait-EchoUp([string]$Base, [int]$TimeoutSec = 120) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $st = Get-EchoStatus $Base
        if ($st) { return $st }
        Start-Sleep -Seconds 2
    }
    return $null
}

# /api/meetings 返回的是 {"items":[...]}（不是裸数组），这里统一拆一层
function Get-EchoMeetingListFromApi([string]$Base) {
    $port = Get-EchoPort $Base
    $resp = Get-EchoJson ("http://127.0.0.1:{0}/api/meetings" -f $port) 20
    if ($null -eq $resp) { return $null }
    if ($resp -is [System.Array]) { return @($resp) }
    if ($resp.PSObject.Properties['items']) { return @($resp.items) }
    return @($resp)
}

function Get-EchoMeetingCountFromApi([string]$Base) {
    $list = Get-EchoMeetingListFromApi $Base
    if ($null -eq $list) { return -1 }
    return @($list).Count
}

function Get-EchoRelatedProcesses([string]$Base) {
    $lower = $Base.ToLowerInvariant()
    $isCurrent = Test-EchoBaseIsCurrent $Base
    $protect = Get-EchoProtectedPids
    $list = @()
    try {
        foreach ($p in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
            if ($protect.Contains([int]$p.ProcessId)) { continue }
            if (-not (Test-EchoOwnedProcess $p $lower $isCurrent)) { continue }
            $cl = [string]$p.CommandLine
            $list += [pscustomobject]@{ Name = [string]$p.Name; PID = [int]$p.ProcessId; CommandLine = $cl }
        }
    } catch { }
    return $list
}

# ------------------------------ 备份目录 ------------------------------
function Get-NewestBackup([string]$Base) {
    $roots = New-Object System.Collections.ArrayList
    $parent = Split-Path -Parent $Base
    if ($parent) { [void]$roots.Add((Join-Path $parent 'ECHO-backup')) }
    [void]$roots.Add((Join-Path $env:USERPROFILE 'ECHO-backup'))
    [void]$roots.Add('D:\ECHO-backup')
    $all = @()
    foreach ($r in ($roots | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $r) {
            $all += @(Get-ChildItem -LiteralPath $r -Directory -ErrorAction SilentlyContinue |
                Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'manifest.json') })
        }
    }
    if ($all.Count -eq 0) { return '' }
    return [string](($all | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName)
}

function Confirm-Continue([string]$Prompt) {
    $ans = Read-Host ('  ' + $Prompt)
    return ($ans -eq 'CLEAN')
}

# ------------------------------ 运维动作 ------------------------------
# 按 ECHO 自己的设置打开面板：有 launch-desktop.ps1 就用它（边条/窗口/浏览器按设置来），
# 没有就退回默认浏览器打开 http://127.0.0.1:<port>/ 。
function Open-EchoPanel([string]$Code, [string]$Base) {
    $launch = Join-Path $Code 'scripts\launch-desktop.ps1'
    if (Test-Path -LiteralPath $launch) {
        # 同上：不要在 powershell 参数里塞 -WindowStyle Hidden（组策略会拒绝）
        Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $launch
        ) | Out-Null
        return 'launch-desktop.ps1（按设置打开边条/窗口）'
    }
    $url = 'http://127.0.0.1:' + (Get-EchoPort $Base) + '/'
    try { Start-Process $url | Out-Null; return $url } catch { return '' }
}

# 命令行里包含某段文字（小写比较）的进程列表 —— 用来判断"看门狗/切换器是不是已经在跑"
function Get-EchoRunningCommand([string]$Fragment) {
    $frag = $Fragment.ToLowerInvariant()
    $hits = @()
    try {
        foreach ($p in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
            $cl = [string]$p.CommandLine
            if (-not $cl) { continue }
            if ($cl.ToLowerInvariant().Contains($frag)) { $hits += $p }
        }
    } catch { }
    return $hits
}

# 拉起本安装的看门狗（scripts\startup.ps1，常驻监督 ECHO 端口）
# 返回：started / already / missing
function Start-EchoWatchdog([string]$Code) {
    $sup = Join-Path $Code 'scripts\startup.ps1'
    if (-not (Test-Path -LiteralPath $sup)) { return 'missing' }
    if (@(Get-EchoRunningCommand ($Code.TrimEnd('\') + '\scripts\startup.ps1')).Count -gt 0) { return 'already' }
    # 同上：-WindowStyle Hidden 只放在 Start-Process 上，别放进 powershell 参数里
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $sup
    ) | Out-Null
    return 'started'
}

# 本安装就是切换器里的"当前实例"时，恢复切换器监督进程（.echo-switch\echo-supervisor.ps1）
# 返回：started / already / missing / skip
function Start-EchoSwitchSupervisor([string]$Base) {
    if (-not (Test-EchoBaseIsCurrent $Base)) { return 'skip' }
    $vbs = Join-Path $env:USERPROFILE '.echo-switch\echo-switch-startup.vbs'
    if (-not (Test-Path -LiteralPath $vbs)) { return 'missing' }
    if (@(Get-EchoRunningCommand '.echo-switch\echo-supervisor.ps1').Count -gt 0) { return 'already' }
    try {
        Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $vbs + '"') -WindowStyle Hidden | Out-Null
        return 'started'
    } catch { return 'missing' }
}

function Test-EchoMeetingBusy([string]$Base) {
    $st = Get-EchoStatus $Base
    if (-not $st) { return $false }
    if ($st.meeting -and $st.meeting.active) { return $true }
    if ($st.busy) { return $true }
    return $false
}

function Get-EchoLogTail([string]$Base, [int]$Lines = 15) {
    $err = Join-Path $Base 'data\logs\echo-server.log.err'
    $log = Join-Path $Base 'data\logs\echo-server.log'
    $file = ''
    if ((Test-Path -LiteralPath $err) -and ((Get-Item -LiteralPath $err).Length -gt 0)) { $file = $err }
    elseif (Test-Path -LiteralPath $log) { $file = $log }
    if (-not $file) { return @() }
    return @(Get-Content -LiteralPath $file -Tail $Lines -ErrorAction SilentlyContinue)
}

function Format-Duration([double]$Seconds) {
    if ($Seconds -lt 60) { return ('{0:N0} 秒' -f $Seconds) }
    if ($Seconds -lt 3600) { return ('{0:N1} 分钟' -f ($Seconds / 60)) }
    $h = [math]::Floor($Seconds / 3600)
    $m = [math]::Floor(($Seconds % 3600) / 60)
    return ('{0} 小时 {1} 分' -f $h, $m)
}

