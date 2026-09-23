# startup.ps1 - logon entry point for ECHO (Startup shortcut runs this).
#
# Goal: ECHO must come up on its own at every logon and must NOT depend on DSH
# Desktop. A DSH Desktop upgrade then never affects ECHO.
#
# What it does, in order:
#   1. (retired 2026-09-17) DSH plugin self-heal - removed with the echo-host plugin
#   2. start ECHO if its port (ECHO_PORT / data\echo-port.txt) is not listening
#   3. supervise: restart ECHO whenever it stops listening (resident loop)
#
# ASCII-ONLY ON PURPOSE. Windows PowerShell 5.1 parses a BOM-less .ps1 as ANSI/GBK,
# so any non-ASCII literal here would be read back mangled and the file would fail
# to parse - that silently broke start.ps1/launch-desktop.ps1 on 2026-09-12 while
# every log said nothing at all. Keep every script we create ASCII-only.
#
# Manual run: powershell -ExecutionPolicy Bypass -File scripts\startup.ps1
# Uninstall : remove the shortcut "ECHO startup.lnk" from the Startup folder
#             (or run scripts\install-autostart.ps1 -Remove, then re-create it
#             pointing at this script).
param(
    [int]$RestartDelaySeconds = 10
)

$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent

# Start the service without ever creating a console window: the venv pythonw would
# otherwise leak a console that Windows Terminal shows as an empty tab.
. (Join-Path $PSScriptRoot 'echo-launch-lib.ps1')
$logDir = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$supLog = Join-Path $logDir 'echo-supervisor.log'

# Hide this console window from inside the script.
# The Startup shortcut must NOT use -WindowStyle Hidden: this machine's policy
# blocks hidden powershell launches from shortcuts (the script would then never run
# at all - documented in launch-desktop.ps1). So the script hides its OWN console
# after starting, exactly like launch-desktop.ps1 does. Without this, the logon
# launch leaves a black "ECHO startup" console on screen forever, because the
# supervisor loop below never exits (reported by the user on 2026-09-12).
try {
    if (-not ('EchoConHider' -as [type])) {
        Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class EchoConHider { [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow); }'
    }
    [EchoConHider]::ShowWindow([EchoConHider]::GetConsoleWindow(), 0) | Out-Null
    $script:hiddenConsole = $true
} catch {
    $script:hiddenConsole = $false
}

function SupLog([string]$message) {
    Add-Content -Path $supLog -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message) -Encoding UTF8
}

SupLog "===== startup.ps1 begin (restartDelay=${RestartDelaySeconds}s, consoleHidden=$($script:hiddenConsole)) ====="

# ---- 0. single-instance guard for the WATCHDOG itself ----
# Both the root supervisor (echo-supervisor.ps1) and scripts\switch-instance.ps1
# call Start-EchoInstance. In the window where both see this install as "down"
# they can each spawn a watchdog, leaving two loops racing for the same port
# (observed 2026-09-18). A check-then-launch guard cannot close that window; a
# kernel mutex can - the second watchdog fails to create it and exits at once.
# Released automatically when this process ends.
$sha = [System.Security.Cryptography.SHA1]::Create()
$hex = [System.BitConverter]::ToString(
    $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLower()))).Replace('-', '')
$mtxName = 'Local\ECHO-watchdog-' + $hex.Substring(0, 16)
$createdNew = $false
$script:WatchdogMutex = New-Object System.Threading.Mutex($true, $mtxName, [ref]$createdNew)
if (-not $createdNew) {
    SupLog "another watchdog for this install is already running - exiting"
    exit 0
}
SupLog "watchdog mutex acquired ($mtxName)"

# ---- 1. (retired 2026-09-17) DSH plugin registration self-heal ----
# The echo-host DSH plugin is retired; nothing to heal. ECHO's own autostart is
# this script (see the Startup shortcut -> scripts\echo-startup.vbs).

# ---- 2/3. resolve pythonw and keep ECHO alive ----
# D22: main package has no bundled runtime; runtime-core component provides it.
$py = Join-Path $root 'runtime-core\python.exe'
if (-not (Test-Path $py)) { $py = Join-Path $root 'runtime-core\Scripts\python.exe' }
if (-not (Test-Path $py)) { $py = Join-Path $root 'venv\Scripts\python.exe' }
# ECHO_PYTHON exists for ONE reason: a tree whose own path is non-ASCII cannot let
# funasr/nagisa read model files through it, so it points at an ASCII junction of
# the venv. A tree whose path is ALREADY ASCII must not borrow it - otherwise a
# frozen stable install (e.g. C:\echo1.0) silently runs on the dev tree's venv,
# and installing dependencies for 2.0 would contaminate the stable install.
$pyAlt = $env:ECHO_PYTHON
if ($pyAlt -and (Test-Path $pyAlt) -and ($root -match '[^\x20-\x7E]')) { $py = $pyAlt }
if (-not (Test-Path $py)) { SupLog "runtime missing - cannot start ECHO (runtime-core\ or venv\): $py"; exit 1 }
$pyw = $py -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path $pyw)) { SupLog "pythonw missing: $pyw"; exit 1 }

$outLog = Join-Path $logDir 'echo-server.log'
$errLog = "$outLog.err"

# ECHO port: ECHO_PORT env -> data\echo-port.txt -> 8970.
# Hardcoding 8970 was a real bug (2026-09-15): once the port moved to 18060 the probe
# below could never see the live ECHO, so the supervisor spawned a duplicate instance
# every ~80s for hours (each duplicate exits on ECHO's own anti-duplicate guard).
# Resolution order matches scripts\launch-desktop.ps1, which was fixed when the
# hardcoded port was removed; this file was missed.
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
        if (-not $iar.AsyncWaitHandle.WaitOne(1200)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch { return $false }
    finally { $c.Dispose() }
}

# ECHO writes data\echo.pid as soon as it takes its single-instance lock, i.e.
# BEFORE it loads models. During that window the port is not listening yet, so a
# port-only probe would spawn a second instance. Checking the pid closes it.
# 2026-09-18: the live host had exactly two such leftovers (one serving, one
# alive-but-not-listening and still loading SenseVoice in the background).
function Test-EchoAlive {
    $pidFile = Join-Path $root 'data\echo.pid'
    if (-not (Test-Path $pidFile)) { return $false }
    $p = Get-Content $pidFile -Raw -ErrorAction SilentlyContinue
    if ($null -eq $p) { return $false }
    $p = $p.Trim()
    if ($p -notmatch '^[0-9]+$') { return $false }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    # Guard against pid reuse: only accept a process that is really ECHO.
    if ($proc.CommandLine -notmatch 'app\.main') { return $false }
    return $true
}

# ECHO has its own single-instance lock (see app\single_instance.py) plus a port
# check, so this probe-then-start dance can no longer race with a live instance.
function Start-EchoOnce {
    # Must go through Start-EchoProcess: Start-Process has no CreateNoWindow switch and
    # the venv pythonw leaks a console that Windows Terminal shows as an empty tab
    # (see the header of echo-launch-lib.ps1).
    $p = Start-EchoProcess -Pythonw $pyw -WorkDir $root -Arguments '-m app.main' `
        -OutLog $outLog -ErrLog $errLog
    return $p
}

$echoPort = Resolve-EchoPort $root
SupLog "resolved ECHO port=$echoPort"

if (Test-EchoPort $echoPort) { SupLog "ECHO already listening on $echoPort" }
elseif (Test-EchoAlive) { SupLog "ECHO process is alive but not listening yet (booting)" }
else { SupLog "ECHO not listening at logon - starting" }

while ($true) {
    if (Test-EchoPort $echoPort) { Start-Sleep -Seconds 15; continue }
    if (Test-EchoAlive) {
        # Booting (loading models) - do NOT start a second instance.
        SupLog "ECHO alive but port not ready - waiting"
        Start-Sleep -Seconds 3
        continue
    }
    SupLog "ECHO not running - starting"
    try {
        $p = Start-EchoOnce
        SupLog "started pid=$($p.Id)"
    } catch {
        SupLog "start failed: $_"
    }
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-EchoPort $echoPort) { break }
    }
    if (-not (Test-EchoPort $echoPort)) {
        if (Test-EchoAlive) {
            SupLog "still booting after 30s - keep waiting"
        } else {
            SupLog "still not listening after 30s - retry in ${RestartDelaySeconds}s"
            Start-Sleep -Seconds $RestartDelaySeconds
        }
    }
}
