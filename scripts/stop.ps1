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
#   3. our own echo-sidebar.exe - ASK it to exit (it is a separate detached process, so
#      killing ECHO never takes it down; see "SIDEBAR" below)
# We do NOT blanket-kill every pythonw.exe: unrelated Python services may be running.
param(
    [switch]$Quiet,
    # Restart path only: a restart starts ECHO again and the rail is meant to survive it
    # (the rail page retries while ECHO boots), so restart-echo.ps1 passes this switch.
    [switch]$KeepSidebar
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

# ---- 4) our own local capability backend (added 2026-10-06) ----
# Ports 8900/8901 are shared by BOTH installs (dev and stable run one at a time), so a
# backend left running after ECHO stops keeps the other tree from starting its own - the
# next switch then comes up against a backend that answers `unauthorized`.
# Ownership is decided the SAME way as for ECHO above: read the process command line and
# require `-m server.main --config <this tree's backend dir>`. The other tree's backend
# never matches this path, so it is left alone (see the AGENTS.md rule: only stop what is
# provably ours, never guess a victim).
$beRoot = Join-Path $base 'backend'
try {
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction Stop
    foreach ($p in $procs) {
        $cmd = [string]$p.CommandLine
        if ($cmd -notmatch 'server\.main') { continue }
        if ($cmd -notmatch [regex]::Escape($beRoot)) { continue }
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Step "stopped local capability backend (pid $($p.ProcessId), config under $beRoot)"
    }
    # clear the ownership record so a later start is not fooled by a stale pid
    $bePid = Join-Path $base 'data\logs\backend.pid'
    if (Test-Path $bePid) {
        Remove-Item $bePid -Force -ErrorAction SilentlyContinue
        Write-Step 'cleared the backend pid record'
    }
} catch {
    Write-Step "WARNING: could not scan for the local backend ($($_.Exception.Message))"
}

# ---- 3) our own echo-sidebar.exe: ask it to exit (added 2026-10-21) ----
# WHY this is needed: the sidebar is a SEPARATE process (app/runtime.py launches it with
# DETACHED_PROCESS precisely so it survives an ECHO restart). So stopping ECHO never takes
# it down - user report 2026-10-21: "8970 no longer listens, but the sidebar is still on
# screen". The normal stop path (POST /api/control/echo/stop) sends the same command from
# Python (app/runtime.py:quit_sidebar); this script has to do it itself because it kills
# ECHO hard, so there is no Python left to notify anyone.
#
# HOW: reuse the sidebar's OWN IPC - the named pipe "echo-sidebar-toggle" that already
# carries toggle / rail-hide / rail-show / rail-expand - and send one more command, "quit".
# Nothing new is invented: no extra port, no new protocol.
#
# OWNERSHIP: same rule as for ECHO above - only a sidebar whose command line points into
# THIS tree is signalled, so another install's rail is never touched. And if it does not
# answer (older build, pipe busy), that is only a note: the sidebar's own heartbeat
# (sidebar/Heartbeat.cs) exits it a few seconds later when the service is gone.
function Send-SidebarQuit([string]$root) {
    $ourSidebars = @()
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name like 'echo-sidebar%'" -ErrorAction Stop
        foreach ($p in $procs) {
            if ([string]$p.CommandLine -like "*$root\sidebar\*") { $ourSidebars += $p.ProcessId }
        }
    } catch {
        Write-Step "WARNING: could not look for the sidebar ($($_.Exception.Message))"
        return
    }
    foreach ($sidebarPid in $ourSidebars) {
        $pipe = $null
        try {
            $pipe = New-Object System.IO.Pipes.NamedPipeClientStream -ArgumentList '.', 'echo-sidebar-toggle', ([System.IO.Pipes.PipeDirection]::Out)
            $pipe.Connect(600)
            $writer = New-Object System.IO.StreamWriter -ArgumentList $pipe
            $writer.Write('quit')
            $writer.Flush()
            $writer.Dispose()
            $pipe.Dispose()
            Write-Step "asked the sidebar to exit (pid $sidebarPid)"
        } catch {
            Write-Step "sidebar did not take the quit command (pid $sidebarPid): $($_.Exception.Message)"
        } finally {
            if ($pipe) { try { $pipe.Dispose() } catch { } }
        }
    }
}

if (-not $KeepSidebar) { Send-SidebarQuit $root }

if ($stopped.Count -eq 0) {
    Write-Step 'ECHO was not running (or could not be identified)'
    exit 1
}
exit 0
