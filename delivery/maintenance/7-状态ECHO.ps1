<#
.SYNOPSIS
  查看 ECHO 状态（只读），运维目录第 7 步。

.DESCRIPTION
  打印：安装/版本、服务与端口、面板地址、运行时长、各组件状态、当前会议/转写状态、
  会议总数与最近一场、关键目录、磁盘剩余、在跑的 ECHO 进程；可选打印日志尾部。
  不启动、不停止、不改任何东西。

.PARAMETER Json
  输出机器可读的 JSON（排障/上报用）

.PARAMETER LogTail
  额外打印服务日志最后 N 行（默认 0 = 不打）

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "7-状态ECHO.ps1"

.EXAMPLE
  ... -LogTail 30
#>
param(
    [string]$Root = '',
    [switch]$Json,
    [int]$LogTail = 0
)

$scriptDir = $PSScriptRoot
. (Join-Path $scriptDir '_echo-common.ps1')

$base = Find-EchoBase $Root
if (-not $base) {
    if ($Json) {
        Write-Output (@{ ok = $false; error = 'echo-not-found' } | ConvertTo-Json -Compress)
    } else {
        Write-Head 'ECHO 状态'
        Write-Fail '没有找到 ECHO 安装目录。请用 -Root 指定，例如：-Root "D:\ECHO"'
    }
    exit 2
}
$L = Get-EchoLayout $base
$st = Get-EchoStatus $L.Base
$paths = Get-EchoJson ('http://127.0.0.1:{0}/api/paths/env' -f $L.Port) 8
$meetingItems = Get-EchoMeetingListFromApi $L.Base
$procs = @(Get-EchoRelatedProcesses $L.Base)
$meetingCount = -1
$latest = $null
if ($null -ne $meetingItems) {
    $meetingCount = @($meetingItems).Count
    if ($meetingCount -gt 0) { $latest = @($meetingItems)[0] }
}

if ($Json) {
    $payload = [ordered]@{
        ok         = [bool]$st
        generated  = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        echo       = [ordered]@{ base = $L.Base; code = $L.Code; data = $L.Data; meetingsDir = $L.Meetings; layout = $L.Layout; version = $L.Version; port = $L.Port }
        service    = [ordered]@{ up = [bool]$st; uptime = $(if ($st) { $st.uptime } else { 0 }); pid = $(if ($st -and $st.components) { ($st.components | Where-Object { $_.name -eq 'server' } | Select-Object -First 1).pid } else { 0 }) }
        components = $(if ($st) { @($st.components) } else { @() })
        meeting    = [ordered]@{ count = $meetingCount; active = [bool]($st -and $st.meeting -and $st.meeting.active); busy = [bool]($st -and $st.busy); busyOwner = $(if ($st) { $st.busyOwner } else { '' }) }
        latest     = $latest
        paths      = $(if ($paths) { $paths } else { $null })
        processes  = @($procs | ForEach-Object { [ordered]@{ name = $_.Name; pid = $_.PID } })
    }
    $payload | ConvertTo-Json -Depth 8
    if ($st) { exit 0 } else { exit 1 }
}

Write-Head 'ECHO 状态'
Write-Note ('安装目录   ' + $L.Base)
Write-Note ('程序目录   ' + $L.Code + '    (' + $L.Layout + ')')
Write-Note ('版本       ' + $(if ($L.Version) { $L.Version } else { '未知' }))

Write-Section '服务'
if ($st) {
    $serverPid = ''
    if ($st.components) {
        $srv = @($st.components | Where-Object { $_.name -eq 'server' } | Select-Object -First 1)
        if ($srv.Count -gt 0) { $serverPid = [string]$srv[0].pid }
    }
    Write-Ok ('运行中 · 端口 ' + $L.Port + $(if ($serverPid) { ' · pid ' + $serverPid } else { '' }) + $(if ($st.uptime) { ' · 已运行 ' + (Format-Duration ([double]$st.uptime)) } else { '' }))
    Write-Note ('面板地址   http://127.0.0.1:' + $L.Port + '/')
} else {
    Write-Fail '未运行（端口 ' + $L.Port + ' 没有响应）'
    Write-Step '启动：双击 4-启动ECHO.cmd；排障：本脚本加 -LogTail 30'
}

if ($st -and $st.components) {
    Write-Section '组件'
    foreach ($c in @($st.components)) {
        $detail = [string]$c.detail
        if ($detail.Length -gt 96) { $detail = $detail.Substring(0, 96) + '…' }
        $color = 'Gray'
        if ([string]$c.status -eq 'online') { $color = 'Green' }
        elseif ([string]$c.status -in @('error', 'failed')) { $color = 'Red' }
        elseif ([string]$c.status -eq 'idle' -or [string]$c.status -eq 'skipped') { $color = 'DarkGray' }
        Write-Host ('   ' + ([string]$c.name).PadRight(10) + ' ' + ([string]$c.status).PadRight(8) + ' ' + $detail) -ForegroundColor $color
    }
}

Write-Section '会议'
if ($null -eq $meetingItems) {
    Write-Note '会议列表取不到（服务未运行？）'
} else {
    $active = ($st -and $st.meeting -and $st.meeting.active)
    Write-Note ('总数       ' + $meetingCount + ' 场')
    if ($active) {
        Write-Warn ('正在录音/转写：' + [string]$st.meeting.source + ' · 开始于 ' + [string]$st.meeting.startedAt + ' · 段数 ' + [string]$st.meeting.segments)
    } else {
        Write-Note '当前       空闲（没有在录音）'
    }
    if ($st -and $st.busy) {
        Write-Warn ('有后台任务在跑：' + [string]$st.busyOwner + ' ' + [string]$st.busyPhase)
    }
    if ($latest) {
        $title = [string]$latest.title
        if (-not $title) { $title = [string]$latest.name }
        Write-Note ('最近一场   ' + [string]$latest.started_at + '  ' + $title + '  [' + [string]$latest.status + ']')
    }
}

if ($paths -and $paths.roots) {
    Write-Section '目录（来自 ECHO 运行配置）'
    foreach ($r in @($paths.roots)) {
        if ([string]$r.name -in @('DATA', 'MEETINGS', 'MODELS', 'DSH', 'DSH_HOME', 'AIDE')) {
            Write-Note (([string]$r.name).PadRight(10) + ' ' + [string]$r.path)
        }
    }
}
$drive = Split-Path -Qualifier $L.Data
if ($drive) {
    $drv = Get-PSDrive -Name ($drive.TrimEnd(':')) -ErrorAction SilentlyContinue
    if ($drv) { Write-Note ('磁盘       ' + $drive + ' 剩余 ' + (Format-Size $drv.Free)) }
}

Write-Section '进程'
if ($procs.Count -eq 0) {
    Write-Note '没有在跑的 ECHO 进程'
} else {
    $byName = @{}
    foreach ($p in $procs) {
        if (-not $byName.ContainsKey($p.Name)) { $byName[$p.Name] = 0 }
        $byName[$p.Name] = $byName[$p.Name] + 1
    }
    $summary = @()
    foreach ($k in ($byName.Keys | Sort-Object)) { $summary += ('{0}×{1}' -f $k, $byName[$k]) }
    Write-Note ('共 ' + $procs.Count + ' 个：' + ($summary -join '，'))
}

if ($LogTail -gt 0) {
    Write-Section ('日志尾部（最后 ' + $LogTail + ' 行）')
    $tail = Get-EchoLogTail $L.Base $LogTail
    if ($tail.Count -eq 0) {
        Write-Note '没有日志可显示（data\logs\echo-server.log*)'
    } else {
        foreach ($line in $tail) { Write-Host ('   ' + $line) -ForegroundColor DarkGray }
    }
}

if ($st) { exit 0 } else { exit 1 }
