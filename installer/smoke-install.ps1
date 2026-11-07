# =====================================================================
# smoke-install.ps1 - silent install / start / stop / uninstall / upgrade smoke test
#
# Runs on a clean Windows runner (CI) or a disposable test machine:
#   1. install ECHO-Setup-<ver>.exe silently into a temp directory
#   2. verify the program tree, runtime, sidebar and the FIXED data root
#   3. start ECHO.exe, wait for /api/status, stop it through ECHO.exe --stop
#   4. write a marker in the data root, uninstall, verify the marker survived
#   5. install again, verify install-mode.json says "upgrade", start/stop again
#
# The data root is %LOCALAPPDATA%\ECHO (fixed by the product).  To avoid destroying a
# developer's real data this script refuses to run unless -ForceCleanData is given
# when %LOCALAPPDATA%\ECHO\data\echo.db already exists.
#
# ASCII-only on purpose (WinPS 5.1 parses BOM-less scripts as ANSI).
# =====================================================================
param(
    [Parameter(Mandatory = $true)][string]$Setup,
    [string]$WorkDir = '',
    [int]$ReadyTimeoutSec = 150,
    [switch]$ForceCleanData,
    [switch]$KeepArtifacts
)

$ErrorActionPreference = 'Stop'

function Say([string]$m) { Write-Host ('  ' + $m) }
function Ok([string]$m)  { Write-Host ('  [ok] ' + $m) -ForegroundColor Green }
function Fail([string]$m) { throw $m }

function Test-Port([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch { return $false } finally { $client.Dispose() }
}

function Read-Port([string]$dataRoot) {
    $f = Join-Path $dataRoot 'echo-port.txt'
    if (-not (Test-Path -LiteralPath $f)) { return 8970 }
    try {
        $v = (Get-Content -LiteralPath $f -Raw -ErrorAction Stop).Trim()
        if ([int]$v -gt 0) { return [int]$v }
    } catch { }
    return 8970
}

function Wait-Echo([string]$dataRoot, [int]$timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $port = Read-Port $dataRoot
        if (Test-Port $port) {
            try {
                $r = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/status" -f $port) `
                                       -TimeoutSec 5
                if ($r) { return $port }
            } catch { }
        }
        Start-Sleep -Milliseconds 700
    }
    return 0
}

function Wait-PortClosed([int]$port, [int]$timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-Port $port)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Install-Echo([string]$setupPath, [string]$appDir, [string]$logPath) {
    $args = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
              '/LANG=chinesesimplified', '/MERGETASKS=!desktopicon',
              ('/DIR="' + $appDir + '"'), ('/LOG="' + $logPath + '"'))
    $p = Start-Process -FilePath $setupPath -ArgumentList $args -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Fail ("installer failed with exit code {0}; see {1}" -f $p.ExitCode, $logPath)
    }
}

function Start-Echo([string]$appDir, [string]$dataRoot) {
    $launcher = Join-Path $appDir 'ECHO.exe'
    $p = Start-Process -FilePath $launcher -ArgumentList '--quiet' -PassThru
    $port = Wait-Echo $dataRoot $ReadyTimeoutSec
    if (-not $port) {
        Fail ("ECHO did not become ready within {0}s; launcher pid={1}" -f $ReadyTimeoutSec, $p.Id)
    }
    return @{ Port = $port; Launcher = $p }
}

function Stop-Echo([string]$appDir, [int]$port) {
    $launcher = Join-Path $appDir 'ECHO.exe'
    $p = Start-Process -FilePath $launcher -ArgumentList '--stop --quiet' -Wait -PassThru
    $code = $null
    try { $code = $p.ExitCode } catch { }
    if ($null -ne $code -and $code -ne 0) {
        Fail ("ECHO.exe --stop returned {0}" -f $code)
    }
    if (-not (Wait-PortClosed $port 90)) {
        Fail ("port {0} is still listening after --stop" -f $port)
    }
}

function Uninstall-Echo([string]$appDir) {
    $unins = Join-Path $appDir 'unins000.exe'
    if (-not (Test-Path -LiteralPath $unins)) { Fail ("missing uninstaller: " + $unins) }
    $p = Start-Process -FilePath $unins -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Fail ("uninstaller failed with exit code {0}" -f $p.ExitCode)
    }
}

$setupPath = (Resolve-Path -LiteralPath $Setup).Path
if (-not $WorkDir) {
    $WorkDir = Join-Path $env:TEMP ('echo-installer-smoke-' + [guid]::NewGuid().ToString('N'))
}
$WorkDir = [System.IO.Path]::GetFullPath($WorkDir)
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

$appDir = Join-Path $WorkDir 'ECHO-app'
$dataRoot = Join-Path $env:LOCALAPPDATA 'ECHO\data'
$stateRoot = Join-Path $env:LOCALAPPDATA 'ECHO'
$createdState = -not (Test-Path -LiteralPath $stateRoot)

if ((Test-Path -LiteralPath (Join-Path $dataRoot 'echo.db')) -and -not $ForceCleanData) {
    Fail ("real ECHO data already exists at {0}. Refusing to touch it; pass -ForceCleanData on a disposable machine." -f $dataRoot)
}

Say ("setup : " + $setupPath)
Say ("app   : " + $appDir)
Say ("data  : " + $dataRoot)
Say ("work  : " + $WorkDir)

try {
    # ---------------------------------------------------------------- fresh install
    $installLog = Join-Path $WorkDir 'install-fresh.log'
    Install-Echo $setupPath $appDir $installLog
    Ok 'fresh install exited 0'

    foreach ($rel in @('ECHO.exe', 'echo-core\app\main.py', 'runtime-core\python.exe',
                       'echo-core\web\app.js', 'echo-core\.dsh\skills\echo-install\scripts\harness-install-desktop.ps1')) {
        $p = Join-Path $appDir $rel
        if (-not (Test-Path -LiteralPath $p)) { Fail ("missing after install: " + $rel) }
    }
    if (-not (Test-Path -LiteralPath $dataRoot)) { Fail ("data root was not created: " + $dataRoot) }
    $modeFile = Join-Path $dataRoot 'install-mode.json'
    if (-not (Test-Path -LiteralPath $modeFile)) { Fail 'install-mode.json missing' }
    $mode = (Get-Content -LiteralPath $modeFile -Raw | ConvertFrom-Json).mode
    if ($mode -ne 'fresh') { Fail ("first install should be fresh, got " + $mode) }
    Ok 'program tree + fresh install-mode.json verified'

    # ---------------------------------------------------------------- start / stop
    $started = Start-Echo $appDir $dataRoot
    Ok ("ECHO is ready on port " + $started.Port)
    Stop-Echo $appDir $started.Port
    Ok 'ECHO stopped; port closed'

    # ---------------------------------------------------------------- uninstall keeps data
    $marker = Join-Path $dataRoot 'smoke-marker.txt'
    'keep-me' | Set-Content -LiteralPath $marker -Encoding ASCII
    Uninstall-Echo $appDir
    if (Test-Path -LiteralPath (Join-Path $appDir 'ECHO.exe')) { Fail 'uninstaller left ECHO.exe behind' }
    if (-not (Test-Path -LiteralPath $marker)) { Fail 'uninstaller deleted the data marker' }
    Ok 'uninstall removed program files and kept the data root'

    # ---------------------------------------------------------------- upgrade preserves data
    $upgradeLog = Join-Path $WorkDir 'install-upgrade.log'
    Install-Echo $setupPath $appDir $upgradeLog
    if (-not (Test-Path -LiteralPath $marker)) { Fail 'reinstall lost the data marker' }
    $mode = (Get-Content -LiteralPath $modeFile -Raw | ConvertFrom-Json).mode
    if ($mode -ne 'upgrade') { Fail ("reinstall should be upgrade, got " + $mode) }
    Ok 'upgrade install kept data and wrote install-mode.json=upgrade'

    $started = Start-Echo $appDir $dataRoot
    Stop-Echo $appDir $started.Port
    Ok 'upgraded install starts and stops'

    Uninstall-Echo $appDir
    if (-not (Test-Path -LiteralPath $marker)) { Fail 'final uninstall deleted the data marker' }
    Ok 'final uninstall kept the data marker'

    # ---------------------------------------------------------------- logs
    $logs = Join-Path $WorkDir 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    foreach ($name in @('launcher.log', 'echo-server.log', 'echo-server.log.err')) {
        $src = Join-Path $dataRoot ('logs\' + $name)
        if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $logs -Force }
    }
    Copy-Item -LiteralPath $installLog -Destination $logs -Force -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath $upgradeLog -Destination $logs -Force -ErrorAction SilentlyContinue

    Say ''
    Ok ('SMOKE PASSED. logs: ' + $logs)
    exit 0
} catch {
    Write-Host ''
    Write-Host ('  [fail] ' + $_.Exception.Message) -ForegroundColor Red
    $logDir = Join-Path $dataRoot 'logs'
    foreach ($name in @('launcher.log', 'echo-server.log', 'echo-server.log.err')) {
        $p = Join-Path $logDir $name
        if (Test-Path -LiteralPath $p) {
            Write-Host ('  --- ' + $name + ' (tail) ---')
            Get-Content -LiteralPath $p -Tail 30 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ('  ' + $_) }
        }
    }
    exit 1
} finally {
    # Always salvage ECHO's own logs before cleaning up: on a failed/cancelled start or
    # stop, launcher.log / echo-server.log are the only evidence of why.
    $salvage = Join-Path $WorkDir 'data-logs'
    New-Item -ItemType Directory -Force -Path $salvage | Out-Null
    $liveLogs = Join-Path $dataRoot 'logs'
    if (Test-Path -LiteralPath $liveLogs) {
        Copy-Item -Path (Join-Path $liveLogs '*') -Destination $salvage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($KeepArtifacts) {
        Write-Host ('  artifacts kept: ' + $WorkDir)
    } else {
        Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($createdState -and -not $KeepArtifacts) {
        # Only remove what this smoke created; a pre-existing install is never touched.
        Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'ECHO') -Recurse -Force -ErrorAction SilentlyContinue
    }
}
