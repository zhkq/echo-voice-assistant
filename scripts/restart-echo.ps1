# restart-echo.ps1 - restart the ECHO service (stop, wait for the port to free, start).
#
# WHO CALLS THIS: ECHO itself, from POST /api/system/restart -> app/runtime.py:restart_echo().
# The API process cannot stop and restart itself, so it launches this script DETACHED
# (DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP) and answers the request immediately; the
# script then survives the death of the ECHO process it is about to kill.
#
# ASCII-ONLY ON PURPOSE (same reason as start.ps1 / stop.ps1): Windows PowerShell 5.1 reads a
# BOM-less .ps1 as ANSI/GBK, and non-ASCII text then breaks parsing.
#
# Reuses stop.ps1 / start.ps1 so there is exactly one definition of "how ECHO is stopped"
# and "how ECHO is started".
#
# CAUTION - do not invoke this from inside a PowerShell pipeline (`... | Out-Null`,
# `$x = & ...`). start.ps1 spawns ECHO as a grandchild, and that grandchild inherits the
# pipeline's write handle, so the pipe never reaches EOF while ECHO runs and the CALLER
# blocks forever (observed 2026-09-12; the caller then killed the tree and took the freshly
# started ECHO down with it). Call it directly, or launch it with Start-Process:
#   Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',<this>
# The ECHO API path is unaffected: app/runtime.py starts this script with file-redirected
# std handles (CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW), so no pipe is involved.
param(
    [int]$WaitMs = 900,       # let the HTTP response reach the panel before we pull the plug
    [int]$PortWaitSeconds = 20
)

$ErrorActionPreference = 'Stop'
$scripts = $PSScriptRoot
$root = Split-Path $scripts -Parent
# 3.0 安装根布局：代码在 <base>\echo-core 时，data（restart.log / echo-port.txt）在 <base> 下。
# 这个脚本不 source echo-launch-lib.ps1（它要在"运行时可能已经坏了"的时候也能跑），
# 所以按同一条规则内联算一次 —— 判据与 app\paths.py:echo_base() 一致。
# 同事实测报告 §4.2 E：漏改这里会让 restart.log 落进 echo-core\data\、端口文件读不到。
$installBase = $root
if ((Split-Path $root -Leaf) -ieq 'echo-core') { $installBase = Split-Path $root -Parent }
$logDir = Join-Path $installBase 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'restart.log'

function Write-Log([string]$message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Add-Content -Path $log -Value $line -Encoding UTF8
}

# ECHO port: ECHO_PORT env -> data\echo-port.txt -> 8970 (was hardcoded to 8970, which
# stopped matching the live service once the port moved to 18060 - the restart then
# "confirmed" a port nobody was listening on).
function Resolve-EchoPort([string]$root) {
    if ($env:ECHO_PORT) { try { if ([int]$env:ECHO_PORT -gt 0) { return [int]$env:ECHO_PORT } } catch { } }
    $f = Join-Path $root 'data\echo-port.txt'
    if (Test-Path $f) {
        try {
            $v = (Get-Content $f -Raw -ErrorAction Stop).Trim()
            if ([int]$v -gt 0) { return [int]$v }
        } catch { }
    }
    return 8970
}

function Test-EchoPort([int]$port = 8970) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(800)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch { return $false }
    finally { $c.Dispose() }
}

Write-Log "===== restart requested (wait ${WaitMs}ms) ====="
Start-Sleep -Milliseconds $WaitMs

$port = Resolve-EchoPort $installBase
Write-Log "resolved ECHO port=$port"

$stop = Join-Path $scripts 'stop.ps1'
$start = Join-Path $scripts 'start.ps1'
$startOut = Join-Path $logDir 'restart-start.out'
$startErr = Join-Path $logDir 'restart-start.err'

# Run a helper script and WAIT, without ever putting it on a PowerShell pipeline.
# Why not "& powershell ... -File $start | Out-Null": start.ps1 spawns ECHO as a grandchild,
# which inherits the pipeline's write handle; the pipe therefore never reaches EOF while ECHO
# lives, and Out-Null (and this whole script) blocks forever. Redirecting to FILES avoids any
# inherited pipe. Bounded by $StepTimeoutSeconds so a stuck step can never hang the caller.
function Invoke-Step([string]$file, [string[]]$extra, [int]$timeoutSeconds) {
    $errFile = if ($file -like '*start.ps1') { $startErr } else { Join-Path $logDir 'restart-step.err' }
    $outFile = if ($file -like '*start.ps1') { $startOut } else { Join-Path $logDir 'restart-step.out' }
    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $file) + $extra
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $args -WorkingDirectory $root `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru -WindowStyle Hidden
    if (-not $p.WaitForExit($timeoutSeconds * 1000)) {
        try { $p.Kill() } catch { }
        return "TIMEOUT after ${timeoutSeconds}s (killed)"
    }
    $p.Refresh()
    # Start-Process -PassThru 在 5.1 里取 ExitCode 常为 $null，取不到就报 done（每步之后都有
    # 端口校验兜底，所以"成功与否"不依赖这个数字）。
    $code = $null
    try { $code = $p.ExitCode } catch { }
    if ($null -eq $code) { return 'done' }
    return "exit=$code"
}

Write-Log ("stop step: " + (Invoke-Step $stop @('-Quiet') 60))

# Wait until nothing listens on the port, otherwise the new instance refuses to start
# (ECHO has an anti-duplicate guard on the port).
$deadline = (Get-Date).AddSeconds($PortWaitSeconds)
while ((Get-Date) -lt $deadline -and (Test-EchoPort $port)) { Start-Sleep -Milliseconds 400 }
if (Test-EchoPort $port) {
    Write-Log "PORT STILL BUSY after ${PortWaitSeconds}s - aborting restart"
    exit 1
}
Write-Log "port $port free"

$result = Invoke-Step $start @('-Background') 120
Write-Log ("start step: " + $result)
if ($result -like 'TIMEOUT*') { exit 1 }

# Confirm it came back (informational only; the panel polls /api/status itself).
$deadline = (Get-Date).AddSeconds($PortWaitSeconds)
while ((Get-Date) -lt $deadline -and -not (Test-EchoPort $port)) { Start-Sleep -Milliseconds 400 }
if (Test-EchoPort $port) { Write-Log "ECHO is listening again" }
else { Write-Log "WARNING: ECHO not listening yet after ${PortWaitSeconds}s" }
Write-Log "===== restart done ====="
