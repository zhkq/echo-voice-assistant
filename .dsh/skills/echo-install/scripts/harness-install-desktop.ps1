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
    [string]$LogFile = ''
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
        # 2026-11-03 真机：只看"文件在"太粗暴 —— 残留的 dsh.cmd / 别的产品的
        # dsh 也会被当成"已安装"。官方桌面端的 CLI 必须位于一个**装好的桌面端根**
        # （同目录树里有 DeepSeek Harness.exe）。找不到根 = 不是官方安装，跳过。
        if (-not (Get-InstallRoot $full)) { continue }
        return $full
    }
    return ''
}

function Get-DshVersion([string]$cmd) {
    try {
        $old = $env:DSH_HOME
        $env:DSH_HOME = Join-Path $env:TEMP ('echo-dsh-version-' + $PID)
        try {
            $out = @(& $cmd --version 2>&1)
        } finally {
            if ($null -eq $old) { Remove-Item Env:DSH_HOME -ErrorAction SilentlyContinue }
            else { $env:DSH_HOME = $old }
        }
        $line = @($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^\d+\.\d+' } |
                  Select-Object -Last 1)
        if ($line.Count -gt 0) { return [string]$line[-1] }
    } catch { }
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

function Is-Running {
    try {
        $p = @(Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue)
        return ($p.Count -gt 0)
    } catch { return $false }
}

function Download-Installer {
    $tmp = Join-Path $env:TEMP ('echo-dsh-desktop-' + $PID + '.exe')
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue)
    if ($curl -and $curl.Source) {
        for ($i = 1; $i -le 12; $i++) {
            Say ('downloading official DeepSeek Harness (attempt ' + $i + '/12)...')
            & $curl.Source -L --fail --retry 3 --retry-delay 2 --retry-all-errors `
                -C - -o $tmp $URL 2>&1 | Out-Null
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
        # 文件在、但跑不起来（残包/被删了一半/权限）→ 按"没装"处理，往下走重装，
        # 而不是后面才抛一句让人以为"已装好"的错。
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

if (Is-Running) {
    if ($existing) {
        Warn 'DeepSeek Harness is running; not reinstalling. Close it first if you want to upgrade.'
        $v = Get-DshVersion $existing
        Emit $existing $v (Get-InstallRoot $existing)
        exit 0
    }
    throw 'DeepSeek Harness is running. Close it, then run this installer again.'
}

$tmp = Download-Installer
Say 'verifying code signature...'
$sig = Get-AuthenticodeSignature -LiteralPath $tmp
if ($sig.Status -ne 'Valid') {
    throw ('installer signature is not valid: ' + $sig.Status + ' ' + $sig.StatusMessage)
}
Ok 'signature valid'

$targets = New-Object System.Collections.Generic.List[string]
if ($InstallDir -and -not $DefaultLocation) { $targets.Add($InstallDir) }
$targets.Add((Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness'))

$installed = ''
foreach ($target in $targets) {
    # ⚠️ 别用 `$args` 这个变量名：它是 PowerShell 的自动变量（当前脚本/函数的参数表），
    # 赋值/当数组用会踩坑（2026-11-03 真机：安装命令没执行）。用独立名字。
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
    $cli = ''
    for ($i = 0; $i -lt 30; $i++) {
        $cli = Find-OfficialCli $target
        if ($cli) { break }
        Start-Sleep -Seconds 1
    }
    if ($cli) { $installed = $cli; break }
    if (-not $InstallDir -or $DefaultLocation) { break }
}

if (-not $installed) {
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
Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
exit 0
