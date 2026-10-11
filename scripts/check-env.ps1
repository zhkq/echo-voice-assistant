# check-env.ps1 - ECHO pre-install environment check (READ-ONLY, changes nothing)
#
# ASCII-ONLY ON PURPOSE: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI/GBK,
# so any Chinese in here would either print as mojibake or break the parser.
#
# Why this exists (2026-10-11): a fresh install on a test machine picked an OLD
# standalone-harness version because node was a *packaged* build whose npm/npx sit
# NEXT TO node.exe but are not on PATH - the installer concluded "no npm". This
# script checks exactly that, plus the things that decide whether the first-run
# wizard will appear (empty target dir => fresh install) and whether "latest" can
# be resolved at all.
#
# Usage:   powershell -NoProfile -ExecutionPolicy Bypass -File check-env.ps1
#          powershell -NoProfile -ExecutionPolicy Bypass -File check-env.ps1 -Target C:\ECHO

param(
    [string]$Target = 'C:\ECHO',
    [int]$MinFreeGB = 12
)

$ErrorActionPreference = 'Continue'
$script:Bad = 0
$script:Warn = 0

function Head([string]$t) { Write-Host ''; Write-Host "== $t" -ForegroundColor Cyan }
function Ok([string]$t)   { Write-Host ("  [ok]   " + $t) -ForegroundColor Green }
function Warn2([string]$t){ $script:Warn++; Write-Host ("  [warn] " + $t) -ForegroundColor Yellow }
function Bad([string]$t)  { $script:Bad++; Write-Host ("  [fail] " + $t) -ForegroundColor Red }
function Info([string]$t) { Write-Host ("         " + $t) -ForegroundColor DarkGray }

# ---------------------------------------------------------------- OS
Head 'Windows / PowerShell'
try {
    $os = [System.Environment]::OSVersion.Version
    Info ("Windows build " + $os.Build + "  (" + [System.Environment]::OSVersion.VersionString + ")")
    if ($os.Build -ge 17134) { Ok 'Windows 10 1803 or newer (tar.exe available)' }
    else { Bad 'Windows is too old: need 10 1803+ (the kit is unpacked with tar.exe)' }
} catch { Warn2 'Could not read the OS version' }
Info ("PowerShell " + $PSVersionTable.PSVersion.ToString())
if (-not [Environment]::Is64BitOperatingSystem) { Bad '32-bit Windows is not supported' }
else { Ok '64-bit Windows' }

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if ($tar) { Ok ("tar.exe: " + $tar.Source) } else { Bad 'tar.exe not found (needed to unpack the kit)' }

# ---------------------------------------------------------------- Node / npm / npx
Head 'Node.js (the standalone harness needs it)'
$nodeRoots = New-Object System.Collections.Generic.List[string]
$nodeExe = ''
$c = Get-Command node -ErrorAction SilentlyContinue
if ($c) { $nodeExe = $c.Source; Ok ("node on PATH: " + $nodeExe) } else { Warn2 'node is NOT on PATH (the installer can still use a local copy)' }
foreach ($pat in @(
        (Join-Path $env:USERPROFILE '.workbuddy\binaries\node\versions\*\node.exe'),
        (Join-Path $env:LOCALAPPDATA 'hermes\node\node.exe'),
        (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'nodejs\node.exe'))) {
    foreach ($p in @(Get-ChildItem -Path $pat -ErrorAction SilentlyContinue)) {
        $d = Split-Path -Parent $p.FullName
        if (-not $nodeRoots.Contains($d)) { $nodeRoots.Add($d) }
        if (-not $nodeExe) { $nodeExe = $p.FullName }
    }
}
if (-not $nodeExe) { Bad 'No node.exe found - the installer would fall back to a slow npx run' }
else {
    Info ("node = " + $nodeExe)
    try { Info ("node version = " + (& $nodeExe --version)) } catch { }
}
foreach ($d in $nodeRoots) {
    $npm = Join-Path $d 'npm.cmd'; $npx = Join-Path $d 'npx.cmd'
    $hasNpm = Test-Path -LiteralPath $npm; $hasNpx = Test-Path -LiteralPath $npx
    if ($hasNpm -and $hasNpx) { Ok ("npm AND npx are next to node: " + $d) }
    elseif ($hasNpm) { Warn2 ("npm but NO npx next to node: " + $d) }
    elseif ($hasNpx) { Warn2 ("npx but NO npm next to node: " + $d) }
    else { Warn2 ("neither npm nor npx next to node: " + $d) }
}
if (Get-Command npm -ErrorAction SilentlyContinue) { Ok 'npm is on PATH' } else { Warn2 'npm is NOT on PATH (fine only if it sits next to node - see above)' }

# ---------------------------------------------------------------- disk
Head 'Disk space'
try {
    $drive = (Split-Path -Qualifier (Resolve-Path -LiteralPath (Split-Path -Parent $Target) -ErrorAction SilentlyContinue).Path)
    if (-not $drive) { $drive = Split-Path -Qualifier $Target }
    $vol = Get-Volume -DriveLetter ($drive.TrimEnd(':')) -ErrorAction SilentlyContinue
    if ($vol) {
        $freeGB = [math]::Round($vol.SizeRemaining / 1GB, 1)
        Info ("$drive free = $freeGB GB   (need about " + $MinFreeGB + " GB: kit 0.4 + backend offline 3.1 + installed ~2)")
        if ($freeGB -ge $MinFreeGB) { Ok 'enough free space' } else { Bad ("only $freeGB GB free on $drive") }
    } else { Warn2 "could not read free space for $drive" }
} catch { Warn2 'could not read free space' }

# ---------------------------------------------------------------- target dir (decides the wizard!)
Head ("Target directory: " + $Target)
if (-not (Test-Path -LiteralPath $Target)) {
    Ok 'does not exist yet -> a FRESH install (the first-run wizard WILL appear)'
} else {
    $items = @(Get-ChildItem -LiteralPath $Target -Force -ErrorAction SilentlyContinue)
    if ($items.Count -eq 0) {
        Ok 'exists but is EMPTY -> a FRESH install (the first-run wizard WILL appear)'
    } else {
        Warn2 ("NOT empty (" + $items.Count + " entries) -> the installer records UPGRADE, and an upgrade does NOT show the wizard")
        Info 'what is there:'
        foreach ($i in ($items | Select-Object -First 12)) { Info ("   " + $i.Name) }
        Info 'if you want the wizard on this machine: install into an empty folder (or wipe this one first)'
    }
    $mode = Join-Path $Target 'data\install-mode.json'
    if (Test-Path -LiteralPath $mode) {
        Info ('data\install-mode.json already there: ' + (Get-Content -LiteralPath $mode -Raw -ErrorAction SilentlyContinue))
    }
}
try {
    $parent = Split-Path -Parent $Target
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $probe = Join-Path $parent ('.echo-write-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    Set-Content -LiteralPath $probe -Value 'x' -ErrorAction Stop
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    Ok ('can write next to ' + $Target)
} catch { Bad ('cannot write to ' + (Split-Path -Parent $Target) + ' - run as an account that can, or pick another target') }

# ---------------------------------------------------------------- ports / old processes
Head 'Ports and leftovers'
foreach ($port in 8970, 8900, 8901) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) { Warn2 ("port $port is already listening (pid " + $conn[0].OwningProcess + ") - a previous ECHO/backend?") }
    else { Ok "port $port is free" }
}
$procs = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*app.main*' -or $_.CommandLine -like '*server.main*' })
if ($procs.Count) {
    Warn2 ("$($procs.Count) ECHO-related python process(es) already running")
    foreach ($p in ($procs | Select-Object -First 6)) { Info ("   pid " + $p.ProcessId) }
    Info 'stop them first: the installer starts its own instance'
} else { Ok 'no ECHO / backend process running' }

# ---------------------------------------------------------------- network (can we get the LATEST dsh?)
Head 'Network (decides whether the installer can use the LATEST harness version)'
$npmCmd = $null
$c = Get-Command npm -ErrorAction SilentlyContinue
if ($c) { $npmCmd = $c.Source }
if (-not $npmCmd) {
    foreach ($d in $nodeRoots) {
        $p = Join-Path $d 'npm.cmd'
        if (Test-Path -LiteralPath $p) { $npmCmd = $p; break }
    }
}
if ($npmCmd) {
    try {
        $latest = & $npmCmd 'view' '@deepseek-ai/dsh' 'dist-tags.latest' 2>$null
        $v = ([string]($latest | Select-Object -First 1)).Trim()
        if ($v) { Ok ("npm registry reachable; latest @deepseek-ai/dsh = " + $v) }
        else { Warn2 'npm reachable but no dist-tag came back' }
    } catch { Warn2 'npm could not query the registry (offline / proxy?) - the installer would use its built-in fallback version' }
} else {
    Warn2 'no npm at all -> cannot query the registry for the latest version'
}
foreach ($h in @('registry.npmjs.org', 'github.com')) {
    try {
        $r = Test-NetConnection -ComputerName $h -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue
        if ($r) { Ok ("TCP $h:443 reachable") } else { Warn2 ("TCP $h:443 NOT reachable") }
    } catch { Warn2 ("could not test $h:443") }
}

# ---------------------------------------------------------------- optional extras
Head 'Optional'
$obs = @(
    (Join-Path $env:LOCALAPPDATA 'Obsidian\Obsidian.exe'),
    (Join-Path $env:ProgramFiles 'Obsidian\Obsidian.exe')) | Where-Object { Test-Path -LiteralPath $_ }
if ($obs) { Ok ('Obsidian found: ' + $obs[0]) } else { Info 'Obsidian not found (optional - only needed for note-taking; you can also point ECHO at any folder)' }

# ---------------------------------------------------------------- verdict
Head 'Verdict'
if ($script:Bad -eq 0 -and $script:Warn -eq 0) { Write-Host '  ALL GOOD - ready for a fresh install.' -ForegroundColor Green }
elseif ($script:Bad -eq 0) { Write-Host ("  USABLE, but read the " + $script:Warn + " warning(s) above.") -ForegroundColor Yellow }
else { Write-Host ("  " + $script:Bad + " hard problem(s) and " + $script:Warn + " warning(s) - fix the [fail] lines first.") -ForegroundColor Red }
Write-Host ''
Write-Host '  Next: double-click 装我.cmd in the delivery folder.' -ForegroundColor Cyan
if (Test-Path -LiteralPath $Target) {
    $items = @(Get-ChildItem -LiteralPath $Target -Force -ErrorAction SilentlyContinue)
    if ($items.Count -gt 0) {
        Write-Host '  NOTE: the target folder is not empty, so the panel will treat this as an UPGRADE' -ForegroundColor Yellow
        Write-Host '        (no first-run wizard). Use an empty folder if you want to walk the wizard.' -ForegroundColor Yellow
    }
}
exit ($(if ($script:Bad -gt 0) { 1 } else { 0 }))
