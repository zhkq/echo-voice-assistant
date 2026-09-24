# stop.ps1 - stop the ECHO service.
#
# ASCII-ONLY ON PURPOSE. Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI/GBK:
# non-ASCII text gets mangled and the script fails to parse (this is the same trap
# documented in start.ps1). Keep every script in this folder ASCII-only.
#
# BUG FIXED 2026-09-12: the original script assigned to $pid, which is a READ-ONLY
# PowerShell automatic variable (the current process id). The assignment threw
# "Cannot overwrite variable PID because it is read-only or constant", and because
# $ErrorActionPreference was 'Continue' the script then fell through to a
# Get-CimInstance fallback that can fail with access-denied - so stop.ps1 reported
# "ECHO is not running" while ECHO was in fact still running.
#
# STOP STRATEGY (ordered, all verified before killing):
#   1. data\echo.pid - stopped only if that pid looks like an ECHO python process
#   2. command-line scan for '-m app.main' whose path is under this repo
# We do NOT blanket-kill every pythonw.exe: unrelated Python services may be running.
param(
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

# 3.0 install-base layout (see app\paths.py:echo_base): with the code in <base>\echo-core,
# the pid/port files live in <base>\data. $root stays the CODE root - it is what the
# "is this pid ours" match below needs (the command line carries the code path).
. (Join-Path $PSScriptRoot 'echo-launch-lib.ps1')
$base = (Get-EchoRoots -Start $root).Base

function Write-Step([string]$message) {
    if (-not $Quiet) { Write-Host $message }
}

# Is this pid an ECHO python process? Guards against a stale/recycled pid.
# Returns: 'yes' | 'no' | 'unknown' (cannot inspect - caller decides).
function Test-EchoPid([int]$ProcessId) {
    try {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        if (-not $p) { return 'no' }
        if ($p.Name -notlike 'python*') { return 'no' }
        $cmd = [string]$p.CommandLine
        $exe = [string]$p.ExecutablePath
        if (($cmd -match 'app\.main') -and ($cmd -match [regex]::Escape($root) -or $exe -like '*ECHO*')) { return 'yes' }
        return 'no'
    } catch {
        # CIM unavailable (restricted token / WMI blocked). Fall back to what
        # Get-Process can tell us: name + path. Never crash the whole script here.
        $gp = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $gp) { return 'no' }
        if ($gp.ProcessName -notlike 'python*') { return 'no' }
        $path = [string]$gp.Path
        if ($path -like '*ECHO*' -or $path -like '*echo-venv*') { return 'yes' }
        return 'unknown'
    }
}

$stopped = @()

# ---- 1) pid file ----
$pidFile = Join-Path $base 'data\echo.pid'
if (Test-Path $pidFile) {
    $echoPid = (Get-Content $pidFile -Raw).Trim()
    if ($echoPid -match '^\d+$') {
        $verdict = Test-EchoPid ([int]$echoPid)
        if ($verdict -eq 'no') {
            Write-Step "pid file had $echoPid but that is not an ECHO process - ignored"
        } else {
            # 'yes' -> confirmed ECHO; 'unknown' -> cannot inspect, trust the pid file
            Stop-Process -Id ([int]$echoPid) -Force -ErrorAction SilentlyContinue
            $stopped += [int]$echoPid
            Write-Step "stopped ECHO (pid $echoPid, from pid file, check=$verdict)"
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# ---- 2) command-line scan ----
try {
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction Stop
    foreach ($p in $procs) {
        if ($stopped -contains $p.ProcessId) { continue }
        $cmd = [string]$p.CommandLine
        if ($cmd -notmatch 'app\.main') { continue }
        if ($cmd -match [regex]::Escape($root) -or [string]$p.ExecutablePath -like '*ECHO*') {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            $stopped += $p.ProcessId
            Write-Step "stopped ECHO (pid $($p.ProcessId), from command line)"
        }
    }
} catch {
    Write-Step "WARNING: could not scan processes ($($_.Exception.Message)); only the pid file was used"
}

if ($stopped.Count -eq 0) {
    Write-Step 'ECHO was not running (or could not be identified)'
    exit 1
}
exit 0
