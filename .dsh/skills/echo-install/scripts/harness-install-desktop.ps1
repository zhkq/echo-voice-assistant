# =====================================================================
# harness-install-desktop.ps1 - install/use the official DeepSeek Harness
# Windows desktop package as ECHO's "standard harness" (headless dsh web).
#
# Why this exists (2026-10-31):
#   * The official desktop installer bundles Node.js + the dsh package and
#     ships resources\runtime\cli\bin\dsh.cmd. That cmd is a headless
#     `ELECTRON_RUN_AS_NODE` CLI; `dsh.cmd web --port N --no-open` prints a
#     token exactly like `npx @deepseek-ai/dsh web` (verified 0.2.1-rc.5).
#   * Using it removes the Node.js/npm install step entirely.
#   * It is downloaded from the official URL at install time; ECHO never
#     redistributes the binary in its kit (so upgrades stay easy).
#
# Output (stdout, last lines):
#   HARNESS_COMMAND="<dsh.cmd>" web
#   DSH_VERSION=<version>
#   DSH_INSTALL_DIR=<dir>
# Exit code: 0 = usable; 1 = failed (reason printed).
#
# This file is ASCII-only on purpose (no BOM needed).
# =====================================================================
param(
    # Optional install directory. Empty = official per-user default.
    [string]$InstallDir = '',
    # Re-download/reinstall even if an installed copy is found.
    [switch]$Upgrade,
    # Never download; only use an already installed copy.
    [switch]$NoDownload,
    # Do not try to install into a custom dir first (use official default).
    [switch]$DefaultLocation,
    [int]$TimeoutSec = 900,
    [string]$LogFile = '',
    # Optional already-downloaded official installer .exe (side-load for offline
    # / intranet machines). Empty = search next to this script and up to the
    # delivery folder, then fall back to downloading.
    [string]$InstallerPath = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$URL = 'https://download.deepseek.com/desktop/dsh-latest-windows-x64.exe'
$MIN_BYTES = 200MB

function Say([string]$m) { Write-Host ('  ' + $m) }
function Warn([string]$m) { Write-Host ('  [warn] ' + $m) -ForegroundColor Yellow }
function Ok([string]$m) { Write-Host ('  [ok] ' + $m) -ForegroundColor Green }
function LogLine([string]$m) {
    if (-not $LogFile) { return }
    try {
        $dir = Split-Path -Parent $LogFile
        if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -LiteralPath $LogFile -Value ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m) -Encoding UTF8
    } catch { }
}

function Get-OfficialRoots {
    $roots = New-Object System.Collections.Generic.List[string]
    $roots.Add((Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness'))
    $roots.Add((Join-Path $env:ProgramFiles 'DeepSeek Harness'))
    if (${env:ProgramFiles(x86)}) {
        $roots.Add((Join-Path ${env:ProgramFiles(x86)} 'DeepSeek Harness'))
    }
    return $roots
}

function Find-OfficialCli([string]$preferDir = '') {
    $cands = New-Object System.Collections.Generic.List[string]
    if ($preferDir) {
        $cands.Add((Join-Path $preferDir 'resources\runtime\cli\bin\dsh.cmd'))
        $cands.Add((Join-Path $preferDir 'dsh.cmd'))
    }
    foreach ($r in (Get-OfficialRoots)) {
        $cands.Add((Join-Path $r 'resources\runtime\cli\bin\dsh.cmd'))
    }
    $cmd = Get-Command dsh -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { $cands.Add([string]$cmd.Source) }
    foreach ($c in $cands) {
        if (-not $c) { continue }
        if (-not (Test-Path -LiteralPath $c -PathType Leaf)) { continue }
        $full = (Resolve-Path -LiteralPath $c).Path
        # 2026-11-03 real-machine: "file exists" is too loose -- a leftover dsh.cmd
        # (or an unrelated product's dsh) was treated as "installed". The official
        # CLI must sit inside an installed desktop root (tree contains
        # DeepSeek Harness.exe). No root => not an official install, skip it.
        if (-not (Get-InstallRoot $full)) { continue }
        return $full
    }
    return ''
}

function Get-DshVersion([string]$cmd) {
    # 2026-11-03: run --version with a HARD timeout. A broken CLI can hang, and a
    # hung `& $cmd` would hang the whole installer. Start-Process + WaitForExit is
    # the only reliable timeout for a .cmd shim in PowerShell 5.1.
    $verDir = Join-Path $env:TEMP ('echo-dsh-version-' + $PID)
    $outFile = Join-Path $verDir 'out.txt'
    $old = $env:DSH_HOME
    $env:DSH_HOME = $verDir
    try {
        New-Item -ItemType Directory -Force -Path $verDir | Out-Null
        $p = Start-Process -FilePath $cmd -ArgumentList '--version' -NoNewWindow -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError ($outFile + '.err') -ErrorAction Stop
        if (-not $p.WaitForExit(20000)) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch { }
            return ''
        }
        $out = @()
        if (Test-Path -LiteralPath $outFile) { $out = @(Get-Content -LiteralPath $outFile -ErrorAction SilentlyContinue) }
        $line = @($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^\d+\.\d+' } |
                  Select-Object -Last 1)
        if ($line.Count -gt 0) { return [string]$line[-1] }
    } catch {
        # Fallback: no timeout possible here, but still better than failing outright.
        try { $out = @(& $cmd --version 2>&1) } catch { $out = @() }
        $line = @($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^\d+\.\d+' } |
                  Select-Object -Last 1)
        if ($line.Count -gt 0) { return [string]$line[-1] }
    } finally {
        if ($null -eq $old) { Remove-Item Env:DSH_HOME -ErrorAction SilentlyContinue }
        else { $env:DSH_HOME = $old }
        Remove-Item -LiteralPath $verDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    return ''
}

function Get-Offset($cmd) {
    return ('"' + $cmd + '" web')
}

function Get-InstallRoot([string]$cmd) {
    $d = Split-Path -Parent $cmd
    for ($i = 0; $i -lt 6 -and $d; $i++) {
        if (Test-Path -LiteralPath (Join-Path $d 'DeepSeek Harness.exe')) { return $d }
        $d = Split-Path -Parent $d
    }
    return ''
}

function Emit([string]$cmd, [string]$version, [string]$dir) {
    Write-Output ('HARNESS_COMMAND=' + (Get-Offset $cmd))
    if ($version) { Write-Output ('DSH_VERSION=' + $version) }
    if ($dir) { Write-Output ('DSH_INSTALL_DIR=' + $dir) }
}

function Find-CliUnderRoot([string]$root) {
    # Only accept a CLI that is installed under $root: a custom-dir install must
    # not "succeed" by silently reusing an older install elsewhere.
    if (-not $root) { return '' }
    if (-not (Test-Path -LiteralPath $root)) { return '' }
    $want = (Resolve-Path -LiteralPath $root).Path.TrimEnd('\')
    foreach ($c in @((Join-Path $root 'resources\runtime\cli\bin\dsh.cmd'),
                     (Join-Path $root 'dsh.cmd'))) {
        if (-not (Test-Path -LiteralPath $c -PathType Leaf)) { continue }
        $full = (Resolve-Path -LiteralPath $c).Path
        $found = Get-InstallRoot $full
        if (-not $found) { continue }
        if ($found.TrimEnd('\') -ieq $want) { return $full }
    }
    return ''
}

function Get-RunningKinds {
    # The GUI and ECHO's headless dsh share the same exe name
    # (DeepSeek Harness.exe). Matching by process name alone would mistake the
    # ECHO engine for the GUI. Split them by command line: the headless CLI has
    # dsh-desktop-host\lib\cli.js (and web --port).
    $kinds = @{ Gui = $false; Headless = $false }
    try {
        $procs = @(Get-CimInstance Win32_Process -Filter "Name = 'DeepSeek Harness.exe'" -ErrorAction SilentlyContinue)
        foreach ($p in $procs) {
            $cmd = [string]$p.CommandLine
            if ($cmd -match 'dsh-desktop-host\\lib\\cli\.js' -or $cmd -match 'web --port') {
                $kinds.Headless = $true
            } else {
                $kinds.Gui = $true
            }
        }
    } catch { }
    return $kinds
}

function Find-LocalInstaller {
    # Side-load: an already downloaded official installer placed next to this
    # script or anywhere up to the delivery folder (offline / intranet machines).
    # Match the official naming so we do not pick up some random .exe.
    $cands = New-Object System.Collections.Generic.List[string]
    if ($InstallerPath) { $cands.Add($InstallerPath) }
    if ($env:ECHO_DSH_INSTALLER) { $cands.Add([string]$env:ECHO_DSH_INSTALLER) }
    $dirs = New-Object System.Collections.Generic.List[string]
    $d = $PSScriptRoot
    for ($i = 0; $i -lt 6 -and $d; $i++) {
        $dirs.Add($d)
        $d = Split-Path -Parent $d
    }
    foreach ($dir in $dirs) {
        if (-not $dir -or -not (Test-Path -LiteralPath $dir)) { continue }
        foreach ($pat in @('dsh-latest-windows-x64.exe', 'dsh-*windows*.exe', 'deepseek*harness*.exe')) {
            foreach ($f in @(Get-ChildItem -LiteralPath $dir -Filter $pat -File -ErrorAction SilentlyContinue)) {
                $cands.Add($f.FullName)
            }
        }
    }
    foreach ($c in $cands) {
        if (-not $c) { continue }
        if (-not (Test-Path -LiteralPath $c -PathType Leaf)) { continue }
        $len = (Get-Item -LiteralPath $c).Length
        if ($len -ge $MIN_BYTES) { return (Resolve-Path -LiteralPath $c).Path }
    }
    return ''
}

function Invoke-Curl {
    # Run curl via Start-Process (NOT "& curl ... 2>&1"): in PowerShell 5.1 the
    # first stderr line of a native command becomes a TERMINATING error while
    # $ErrorActionPreference='Stop', and curl writes its progress meter to
    # stderr -> download aborted at attempt 1 (2026-11-03 real-machine).
    param([string]$CurlExe, [string[]]$CurlArgs)
    $out = Join-Path $env:TEMP ('echo-curl-out-' + $PID + '.txt')
    $err = Join-Path $env:TEMP ('echo-curl-err-' + $PID + '.txt')
    try {
        $p = Start-Process -FilePath $CurlExe -ArgumentList $CurlArgs -NoNewWindow -Wait `
            -PassThru -RedirectStandardOutput $out -RedirectStandardError $err -ErrorAction Stop
        return $p.ExitCode
    } catch {
        return -1
    } finally {
        Remove-Item -LiteralPath $out, $err -Force -ErrorAction SilentlyContinue
    }
}

function Test-DownloadReachable {
    # 6-second reachability probe. Offline machines must fail FAST instead of
    # letting curl retry for minutes. Any HTTP response counts as reachable.
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue)
    if ($curl -and $curl.Source) {
        $code = Invoke-Curl -CurlExe $curl.Source -CurlArgs @('-s', '-o', 'NUL', '-I', '-L', '--max-time', '6', $URL)
        if ($code -eq 0) { return $true }
        # Some CDNs dislike HEAD; try a 1-byte range GET.
        $code = Invoke-Curl -CurlExe $curl.Source -CurlArgs @('-s', '-o', 'NUL', '-L', '--max-time', '6', '-r', '0-0', $URL)
        if ($code -eq 0) { return $true }
        return $false
    }
    try {
        $r = Invoke-WebRequest -Uri $URL -Method Head -UseBasicParsing -TimeoutSec 8
        return ($r.StatusCode -ge 200)
    } catch { return $false }
}

function Download-Installer {
    if (-not (Test-DownloadReachable)) {
        throw 'cannot reach download.deepseek.com (no network / proxy blocks it)'
    }
    $tmp = Join-Path $env:TEMP ('echo-dsh-desktop-' + $PID + '.exe')
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue)
    if ($curl -and $curl.Source) {
        for ($i = 1; $i -le 12; $i++) {
            Say ('downloading official DeepSeek Harness (attempt ' + $i + '/12)...')
            $null = Invoke-Curl -CurlExe $curl.Source -CurlArgs @(
                '-L', '--fail', '--retry', '3', '--retry-delay', '2',
                '--retry-all-errors', '-C', '-', '-o', $tmp, $URL)
            $len = 0
            if (Test-Path -LiteralPath $tmp) { $len = (Get-Item -LiteralPath $tmp).Length }
            if ($len -ge $MIN_BYTES) { return $tmp }
            Start-Sleep -Seconds 2
        }
    }
    # Fallback: no resume, but still better than nothing.
    Say 'curl not available or resume failed; downloading with Invoke-WebRequest...'
    Invoke-WebRequest -Uri $URL -OutFile $tmp -UseBasicParsing -TimeoutSec $TimeoutSec
    if ((Get-Item -LiteralPath $tmp).Length -lt $MIN_BYTES) {
        throw 'downloaded installer is too small (incomplete download)'
    }
    return $tmp
}

$existing = Find-OfficialCli $InstallDir
if ($existing -and -not $Upgrade) {
    $v = Get-DshVersion $existing
    if (-not $v) {
        # File exists but cannot run (broken copy / half-deleted / permissions):
        # treat it as NOT installed and reinstall, instead of failing later with
        # a misleading "already installed" error.
        Warn ('found a dsh CLI that cannot run; treating as NOT installed: ' + $existing)
        $existing = ''
    } else {
        Ok ('using installed DeepSeek Harness CLI: ' + $existing)
        Say ('  version: ' + $v)
        Emit $existing $v (Get-InstallRoot $existing)
        LogLine ('reuse ' + $existing + ' version=' + $v)
        exit 0
    }
}
if ($NoDownload) {
    if ($existing) { $v = Get-DshVersion $existing; Emit $existing $v ''; exit 0 }
    throw 'DeepSeek Harness desktop is not installed, and -NoDownload was given'
}

$running = Get-RunningKinds
if ($running.Gui -or $running.Headless) {
    if ($Upgrade) {
        if ($running.Headless) {
            throw 'ECHO headless dsh (same exe) is running. Stop ECHO first (panel shutdown or scripts\stop.ps1), then re-run this installer to upgrade.'
        }
        throw 'DeepSeek Harness is running. Close it (tray -> exit), then run this installer again to upgrade.'
    }
    if ($running.Gui) {
        throw 'DeepSeek Harness is running. Close it, then run this installer again.'
    }
    throw 'ECHO headless dsh (same exe) is running. Stop ECHO first (panel shutdown or scripts\stop.ps1), then re-run this installer.'
}

$tmp = Find-LocalInstaller
$downloaded = $false
if ($tmp) {
    Say ('using side-loaded official installer: ' + $tmp)
    LogLine ('side-load ' + $tmp)
} else {
    Say 'no side-loaded installer found (put dsh-latest-windows-x64.exe next to the install launcher); trying network...'
    LogLine 'side-load: not found'
    $tmp = Download-Installer
    $downloaded = $true
}
Say 'verifying code signature...'
$sig = Get-AuthenticodeSignature -LiteralPath $tmp
if ($sig.Status -ne 'Valid') {
    # Offline machines may not fetch the CRL/OCSP (revocation unreachable) while
    # the signature itself is intact -> do not block a side-loaded install for
    # that; anything else (NotSigned / HashMismatch / UnknownError) is fatal.
    $msg = [string]$sig.StatusMessage
    if ($sig.Status -eq 'UnknownError' -and $msg -match '(?i)revoc|crl|ocsp|timeout|offline') {
        Warn ('signature revocation could not be checked (offline?): ' + $msg)
        Warn 'signature itself looks intact; continuing'
    } else {
        throw ('installer signature is not valid: ' + $sig.Status + ' ' + $msg)
    }
} else {
    Ok 'signature valid'
}

$targets = New-Object System.Collections.Generic.List[string]
if ($InstallDir -and -not $DefaultLocation) { $targets.Add($InstallDir) }
$targets.Add((Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness'))

$installed = ''
foreach ($target in $targets) {
    # NEVER name this $args: that is a PowerShell automatic variable (parameter
    # list of the current script/function). Using it as a plain array caused the
    # install command to not run at all (2026-11-03 real-machine).
    $installerArgs = @('/S')
    $defaultDir = Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness'
    $isDefaultDir = (-not $InstallDir) -or $DefaultLocation -or ($target -ieq $defaultDir)
    if (-not $isDefaultDir) {
        # NSIS /D= must be last and unquoted; a path with spaces cannot be
        # passed safely through Start-Process. Use the official default then.
        if ($target -match '\s') {
            Warn ('install dir has spaces, using official default instead: ' + $target)
            $target = $defaultDir
        } else {
            $installerArgs += ('/D=' + $target)
        }
    }
    Say ('running official installer (silent) -> ' + $target)
    $p = $null
    try {
        $p = Start-Process -FilePath $tmp -ArgumentList $installerArgs -Wait -PassThru
        $code = $p.ExitCode
    } catch {
        $code = -1
        Warn ('installer failed to start: ' + $_.Exception.Message)
    }
    LogLine ('installer target=' + $target + ' exit=' + $code)
    if ($code -ne 0) { Warn ('installer exit code ' + $code) }
    # NSIS can return before the last files are flushed; poll briefly.
    # Use Find-CliUnderRoot (NOT Find-OfficialCli): a custom -InstallDir must be
    # verified to actually contain the new copy, otherwise a leftover install in
    # %LOCALAPPDATA% would make a failed custom install look successful.
    $cli = ''
    for ($i = 0; $i -lt 30; $i++) {
        $cli = Find-CliUnderRoot $target
        if ($cli) { break }
        Start-Sleep -Seconds 1
    }
    if ($cli) { $installed = $cli; break }
    if (-not $InstallDir -or $DefaultLocation) { break }
}
if (-not $installed) {
    if ($InstallDir -and -not $DefaultLocation) {
        Warn ('custom install dir did not produce a usable CLI: ' + $InstallDir)
        Warn 'falling back to any already-installed copy (if one exists)'
    }
    $installed = Find-OfficialCli ''
}
if (-not $installed) {
    throw 'installer finished but resources\runtime\cli\bin\dsh.cmd was not found'
}

$version = Get-DshVersion $installed
if (-not $version) {
    throw ('official dsh CLI found but could not run it: ' + $installed)
}
$dir = Get-InstallRoot $installed
Ok ('DeepSeek Harness ready: ' + $installed)
Say ('  version: ' + $version)
LogLine ('installed ' + $installed + ' version=' + $version)
Emit $installed $version $dir
if ($downloaded) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
exit 0
