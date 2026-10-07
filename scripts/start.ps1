# start.ps1 - start the ECHO service.
#   foreground : powershell -File scripts\start.ps1
#   background : powershell -File scripts\start.ps1 -Background
#   supervised : powershell -File scripts\start.ps1 -Background -Supervise
#
# -Supervise is what the Startup shortcut uses: a resident watchdog that restarts
# ECHO whenever it is not listening, so ECHO's survival does NOT depend on the DSH
# Desktop plugin. A DSH upgrade then never requires restarting ECHO; if the plugin
# registration is lost, only the optional sidebar is affected.
#
# ASCII-ONLY ON PURPOSE. Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI/GBK:
# non-ASCII text gets mangled and the script fails to parse (that silently broke
# this very file on 2026-09-12 - "%s" style mojibake in a string literal -> "The
# string is missing the terminator"). Keep all scripts in this folder ASCII-only.
param(
    [switch]$Background,
    [switch]$Supervise,
    [int]$RestartDelaySeconds = 10
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

# Start the service without ever creating a console window: the venv pythonw would
# otherwise leak a console that Windows Terminal shows as an empty tab.
. (Join-Path $PSScriptRoot 'echo-launch-lib.ps1')

# 3.0 install-base layout: when this tree's code folder is named echo-core, the data root
# and the runtime live in the PARENT folder (siblings of echo-core). Everything below that
# used to be "just $root" is now "$base" - but $root stays the code root, because that is
# what -WorkDir and `-m app.main` need. On a legacy flat tree base == root, so nothing changes.
$roots = Get-EchoRoots -Start $root
$base = $roots.Base

# (retired 2026-09-17) The DSH Desktop host plugin (echo-host) is gone - see plugin/README.md.

# D22: the main package ships no runtime - the runtime-core component supplies it.
# New layout looks NEXT TO echo-core first (runtime survives a code-only upgrade), then
# the legacy in-tree locations. Prefer runtime-core\python.exe, fall back to the bundled venv.
$py = Join-Path $base 'runtime-core\python.exe'
if (-not (Test-Path $py)) { $py = Join-Path $base 'runtime-core\Scripts\python.exe' }
if (-not (Test-Path $py)) { $py = Join-Path $base 'venv\Scripts\python.exe' }
if (-not (Test-Path $py)) { $py = Join-Path $root 'runtime-core\python.exe' }
if (-not (Test-Path $py)) { $py = Join-Path $root 'runtime-core\Scripts\python.exe' }
if (-not (Test-Path $py)) { $py = Join-Path $root 'venv\Scripts\python.exe' }
# ECHO_PYTHON exists for ONE reason: a tree whose own path is non-ASCII cannot let
# funasr/nagisa read model files through it, so it points at an ASCII junction of
# the venv. A tree whose path is ALREADY ASCII must not borrow it - otherwise a
# frozen stable install (e.g. C:\echo1.0) silently runs on the dev tree's venv,
# and installing dependencies for 2.0 would contaminate the stable install.
$pyAlt = $env:ECHO_PYTHON
if ($pyAlt -and (Test-Path $pyAlt) -and ($root -match '[^\x20-\x7E]')) { $py = $pyAlt }
if (-not (Test-Path $py)) { Write-Host 'runtime missing (runtime-core\ or venv\) - run install.ps1 / setup.ps1 first' -ForegroundColor Red; exit 1 }

$logDir = Join-Path $base 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$outLog = Join-Path $logDir 'echo-server.log'
$errLog = "$outLog.err"
$supLog = Join-Path $logDir 'echo-supervisor.log'

function SupLog([string]$message) {
    Add-Content -Path $supLog -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message) -Encoding UTF8
}

# pythonw.exe (GUI subsystem): no console window, so closing any window never sends
# a window-CLOSE event to ECHO (python.exe -NoNewWindow used to attach the service
# to the launcher console and a forrtl abort killed it on window close).
$pyw = $py -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path $pyw)) { Write-Host "pythonw missing: $pyw" -ForegroundColor Red; exit 1 }

# ECHO refuses to start when the port is already taken (anti-duplicate guard), so
# "probe then start" can never race with an existing instance.
#
# Port resolution (2026-10-06): ONE resolver for every script - `scripts\echo-port.py`.
# Order: config serverPort -> echo-port.txt -> ECHO_PORT (last resort) -> 8970.
# Why not "ECHO_PORT first" (what this used to do): that variable is inherited from ANY
# parent (a terminal, a scheduled task, a persistent user variable) and may point at the
# OTHER tree or at a long-dead port - the log then says "resolved port=18060" while ECHO
# is really on 8970, and the restart confirms a port nobody is listening on.
# The config IS the user's choice, so it wins; echo-port.txt is what ECHO actually bound.
function Resolve-EchoPort([string]$root) {
    $resolver = Join-Path $PSScriptRoot 'echo-port.py'
    if ((Test-Path $resolver) -and (Test-Path $py)) {
        try {
            $out = (& $py $resolver --data-root $root 2>$null | Select-Object -First 1)
            if ($out -match '^\d+$') { return [int]$out }
        } catch { }
    }
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
        if (-not $iar.AsyncWaitHandle.WaitOne(1200)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch { return $false }
    finally { $c.Dispose() }
}

function Start-EchoOnce([switch]$Quiet) {
    # Must go through Start-EchoProcess: Start-Process has no CreateNoWindow switch and
    # the venv pythonw leaks a console that Windows Terminal shows as an empty tab
    # (see the header of echo-launch-lib.ps1).
    $p = Start-EchoProcess -Pythonw $pyw -WorkDir $root -Arguments '-m app.main' `
        -OutLog $outLog -ErrLog $errLog
    if (-not $Quiet) { Write-Host "ECHO started in background (PID $($p.Id))  panel port: see data\echo-port.txt" }
    return $p
}

# Make the capability backend follow this tree's CONFIGURATION (added 2026-10-07).
#
# WHY: after a MACHINE REBOOT the user clicks the dev launcher (this script, -Background)
# and then saw the panel's backend tile stay RED ("pending start") forever --- because the
# capability backend is a SEPARATE process and **nothing here started it**. The
# 2026-10-06 fix added a follow-up to restart-echo.ps1 only (the panel's restart button),
# so "restart" brought the backend back but "start" did not.
#
# WHAT it does now (user's rule, 2026-10-07): it reads `mode` from
# `GET /api/capability/backend` --- which is `local` when this machine is paired to a
# loopback backend (or not paired yet) and `network` when it is paired to a backend on
# another machine:
#
#   mode=local   -> POST /api/capability/backend/take-over
#                   (stop a FOREIGN backend on 8900, start this tree's own, reuse pairing)
#   mode=network -> **probe only**. Do NOT touch any local process: when the user has
#                   configured a backend on another machine, the local one is not what
#                   they want, and starting it behind their back would be wrong.
#
# It is sent only AFTER ECHO answers on its port. `take-over` returns immediately (the
# model load takes tens of seconds and ECHO reports that progress in its own job, which
# the panel shows) --- so the launcher is not blocked.
function Start-EchoBackendFollowUp([int]$port) {
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-EchoPort $port) { break }
    }
    if (-not (Test-EchoPort $port)) { return }
    $logFile = Join-Path $logDir 'restart.log'
    function Write-FollowUp([string]$text) {
        Add-Content -Path $logFile -Value ("[{0}] backend follow-up: {1}" -f `
            (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text) -Encoding UTF8
    }
    $mode = 'local'
    $pairedUrl = ''
    try {
        $st = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/capability/backend" -TimeoutSec 30
        if ($st) {
            if ($st.mode) { $mode = [string]$st.mode }
            if ($st.paired) { $pairedUrl = [string]$st.paired.baseUrl }
        }
    } catch {
        Write-FollowUp ("could not read backend state ({0}) - assuming local" -f $_)
    }
    if ($mode -eq 'network') {
        # Probe the configured backend instead of starting anything here.
        $code = ''
        try {
            $req = [System.Net.HttpWebRequest]::Create($pairedUrl + '/v1/health')
            $req.Timeout = 5000
            $req.Method = 'GET'
            $resp = $req.GetResponse()
            $code = [int]$resp.StatusCode
            $resp.Close()
        } catch [System.Net.WebException] {
            if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            else { $code = 'unreachable' }
        } catch { $code = 'unreachable' }
        Write-FollowUp ("mode=network - configured backend is {0} (probe: {1}). Local backend left alone." -f `
            $pairedUrl, $code)
        return
    }
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/capability/backend/take-over" `
            -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 120
        if ($r -and $r.message) { Write-FollowUp ([string]$r.message) }
    } catch {
        Write-FollowUp ("take-over failed: {0}" -f $_)
    }
}

if ($Background -and -not $Supervise) {
    [void](Start-EchoOnce)
    Start-EchoBackendFollowUp (Resolve-EchoPort $base)
    exit 0
}

if ($Supervise) {
    SupLog "===== supervisor start (restartDelay=${RestartDelaySeconds}s) ====="
    $echoPort = Resolve-EchoPort $base
    SupLog "resolved ECHO port=$echoPort"
    while ($true) {
        if (Test-EchoPort $echoPort) { Start-Sleep -Seconds 15; continue }
        SupLog "ECHO not listening - starting"
        try {
            $p = Start-EchoOnce -Quiet
            SupLog "started pid=$($p.Id)"
        } catch {
            SupLog "start failed: $_"
        }
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            if (Test-EchoPort $echoPort) { break }
        }
        if (-not (Test-EchoPort $echoPort)) {
            SupLog "not listening after 30s - retry in ${RestartDelaySeconds}s"
            Start-Sleep -Seconds $RestartDelaySeconds
        }
    }
}

# Foreground (debug)
& $py -m app.main
