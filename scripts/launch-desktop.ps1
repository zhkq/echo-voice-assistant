# launch-desktop.ps1 - desktop one-click start: ECHO (background) + open the panel.
#   Triggered by the desktop shortcut (named "ECHO personal assistant" in Chinese);
#   it can also be run by hand.
#   Every step is appended to data\logs\launch.log so problems can be traced.
#
# ASCII-ONLY ON PURPOSE. Windows PowerShell 5.1 parses a BOM-less .ps1 as ANSI/GBK:
# non-ASCII text gets mangled and the script then fails to PARSE, which is silent
# from the outside ("double click does nothing, the log stays empty"). That is
# exactly how this file behaved for a long time - keep it ASCII.
#
# Notes fixed on 2026-09-12:
#   * DSH was probed on port 3080 (a relic of the pre-2.x standalone web service).
#     DSH Desktop 2.x serves the GUI and the API on the SAME port, 43120.
#   * The old "ask ECHO to start DSH" step could never work: ECHO's
#     /api/control/dsh/start only probes DSH (app/manager.py: "desktop is managed
#     by the user/autostart, ECHO does not start or stop it"), yet the log claimed
#     a start was requested. Now it states the truth instead.
#   * $err was referenced in the failure dialog but only assigned on the
#     "ECHO not running" branch - it is initialized up front now.

$ErrorActionPreference = 'Continue'
# Disable any default proxy so 127.0.0.1 probes cannot be hijacked by a system proxy.
[System.Net.WebRequest]::DefaultWebProxy = $null

# Hide this console window from inside the script.
# The shortcut must NOT use -WindowStyle Hidden: this machine's policy blocks
# hidden powershell launches from shortcuts (the script then never runs at all).
try {
    if (-not ('EchoConHider' -as [type])) {
        Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class EchoConHider { [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow); }'
    }
    [EchoConHider]::ShowWindow([EchoConHider]::GetConsoleWindow(), 0) | Out-Null
} catch { }

$root = Split-Path $PSScriptRoot -Parent
$py = Join-Path $root 'venv\Scripts\python.exe'
# Prefer the ASCII junction (works around tools that cannot read non-ASCII paths).
$pyAlt = $env:ECHO_PYTHON   # 可选：非 ASCII 路径下的解释器覆盖，见 docs/DEPLOY.md
if ($pyAlt -and (Test-Path $pyAlt)) { $py = $pyAlt }

# ECHO 面板端口：优先级 ECHO_PORT 环境变量 → data\echo-port.txt → 默认 8970。
# 为什么需要：Windows 动态端口段（默认 1024-15000）会被 Hyper-V/WSL 划为保留段且
# 每次重启漂移，落在其中的端口 bind 会失败（Errno 13），届时必须换端口——
# 脚本若还盯着旧端口，双击快捷方式就会打开一个空页面。
# echo-port.txt 由 ECHO 启动时写出（app/main.py），是端口的权威来源。
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
$echoPort = Resolve-EchoPort $root
$base = "http://127.0.0.1:$echoPort"
$dshPort = 43120          # DSH Desktop 2.x: GUI + API on one port
$logDir = Join-Path $root 'data\logs'
$logFile = Join-Path $logDir 'launch.log'
$out = Join-Path $logDir 'echo-server.log'
$err = Join-Path $logDir 'echo-server.log.err'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}
function Show-Error($text) {
    try { [System.Windows.Forms.MessageBox]::Show($text, 'ECHO', 'OK', 'Error') | Out-Null } catch { }
}
Log "===== launch-desktop: start ECHO ====="

# ---------- 0. (retired) DSH Desktop host plugin ----------
# The echo-host DSH plugin was retired on 2026-09-17: DSH Desktop 2.0.9+ no longer
# exposes the Electron main-process API to plugins (no sidebar window), DSH 2.0.11
# ships its own profile plugin system, and the registration inside
# <install>\resources\app.asar.unpacked was wiped by every upgrade. ECHO is started
# by this script / scripts\startup.ps1 and its own autostart instead.
# Details: plugin/README.md

Add-Type -AssemblyName System.Windows.Forms | Out-Null

# TCP probe: uvicorn listening is enough to call the service alive (model preload
# happens in the background and does not block the port).
function Test-TcpPort([int]$port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(1500)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch { return $false }
    finally { $c.Dispose() }
}

# ---------- 1. start ECHO in the background when it is not running ----------
$alive = Test-TcpPort $echoPort
Log "ECHO listening before start: $alive"
if (-not $alive) {
    if (-not (Test-Path $py)) {
        Log "venv missing: $py"
        Show-Error "venv missing - run scripts\setup.ps1 first`n$py"
        exit 1
    }
    # pythonw.exe (GUI subsystem) creates no console and attaches to none, so closing
    # any window never sends ECHO a window-CLOSE event. (python.exe -NoNewWindow used
    # to attach the service to the launcher console; closing that window triggered a
    # forrtl abort - see the window-CLOSE event in echo-server.log.err.)
    $pyw = $py -replace 'python\.exe$', 'pythonw.exe'
    if (-not (Test-Path $pyw)) {
        Log "pythonw missing: $pyw"
        Show-Error "pythonw missing - run scripts\setup.ps1 first`n$pyw"
        exit 1
    }
    $p = Start-Process -FilePath $pyw -ArgumentList @('-m', 'app.main') -WorkingDirectory $root `
        -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    Log "ECHO started in background PID=$($p.Id); waiting for the port (pythonw has no window)"
    for ($i = 0; $i -lt 75; $i++) {
        Start-Sleep -Seconds 1
        if (Test-TcpPort $echoPort) { $alive = $true; break }
    }
}
if (-not $alive) {
    Log "ECHO failed to listen within 75s"
    Show-Error "ECHO did not listen within 75 seconds. See:`n$err"
    exit 1
}
Log "ECHO port ready: $base"

# ---------- 2. DSH Desktop status (ECHO must not and cannot start it) ----------
$dshOk = Test-TcpPort $dshPort
Log "DSH Desktop listening on ${dshPort}: $dshOk"
if (-not $dshOk) {
    Log "DSH Desktop is not running. ECHO does NOT start it (app/manager.py: the desktop"
    Log "  app is managed by the user / its own autostart). Start DSH Desktop yourself;"
    Log "  the panel is still usable in the browser in the meantime."
}

# ---------- 3. open the panel (a Chromium app window when available) ----------
function Open-Panel {
    $url = "http://127.0.0.1:$echoPort"
    $pf86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    $cands = @()
    if ($pf86) { $cands += (Join-Path $pf86 'Microsoft\Edge\Application\msedge.exe') }
    if ($env:ProgramFiles) { $cands += (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe') }
    $cands += (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe')
    if ($env:ProgramFiles) { $cands += (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe') }
    if ($pf86) { $cands += (Join-Path $pf86 'Google\Chrome\Application\chrome.exe') }
    $cands += (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    $cands += (Join-Path $env:LOCALAPPDATA 'Qaxbrowser\Application\qaxbrowser.exe')
    foreach ($exe in $cands) {
        if ($exe -and (Test-Path $exe)) {
            Start-Process $exe -ArgumentList "--app=$url" | Out-Null
            return $true
        }
    }
    Start-Process $url            # fall back to the default browser
    return $true
}
try {
    # Absorb the return value so it is not printed into the (hidden) console.
    $null = Open-Panel
    Log "panel opened: http://127.0.0.1:$echoPort"
} catch { Log "opening the panel failed: $_" }
