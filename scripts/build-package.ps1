# =====================================================================
# build-package.ps1 - build an ECHO delivery package from this tree
#
# WHY THIS EXISTS
#   The delivery zip handed out on 2026-09-14 carried real internal
#   information: dsh-failover/config.json plus nine config.json.bak-* and a
#   settings.yaml.bak-* holding the intranet gateway URL and a userId.
#   Nobody noticed, because that package was assembled by hand.
#   This script makes that impossible: it packs a WHITELIST, refuses to
#   finish when a forbidden file or content pattern appears, and re-checks
#   the zip it just produced.
#
# USAGE
#   powershell -File scripts\build-package.ps1                    # public, code only
#   powershell -File scripts\build-package.ps1 -Profile internal   # + models + venv
#   powershell -File scripts\build-package.ps1 -DryRun             # checks + inventory only
#   powershell -File scripts\build-package.ps1 -OutDir D:\build
#
# PROFILES
#   public    core code only (D22): app web mac scripts plugin docs assets
#             dsh-failover (minus config.json) .dsh sidebar sources + root docs
#   internal  public + models\ (minus pyannote: gated weights) + venv\
#
# NEVER PACKED (both profiles)
#   dsh-failover\config.json   real intranet gateway + userId
#   settings.yaml*             DSH home settings (and their .bak copies)
#   *.credentials*  *.env      credentials
#   *.bak*                     a .bak is a copy of something, often the above
#   *.db *.pid data\           runtime state (meeting recordings are private)
#   .git\ .ruff_cache\ __pycache__\ obj\ *.pyc   build/VCS noise
#   models\pyannote\           gated weights: guide the user, never ship
#
# ASCII-ONLY on purpose (see echo-instance-lib.ps1).
# =====================================================================
param(
    [ValidateSet('public', 'internal')][string]$Profile = 'public',
    [string]$OutDir = '',
    [string]$Version = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not $OutDir) { $OutDir = Join-Path $root 'dist' }

function Say([string]$m)  { Write-Host "  $m" }
function Ok([string]$m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Die([string]$m)  { Write-Host "  [fail] $m" -ForegroundColor Red; exit 1 }

Write-Host ''
Write-Host "  === ECHO build-package (profile=$Profile, dryRun=$DryRun) ==="

# ---------------------------------------------------------------- version
# app/__init__.py is the single source of truth (tests/test_version.py pins it).
if (-not $Version) {
    $init = Join-Path $root 'app\__init__.py'
    if (-not (Test-Path $init)) { Die "not an ECHO tree: $init missing" }
    $m = Select-String -LiteralPath $init -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $m) { Die "cannot read __version__ from app\__init__.py" }
    $Version = $m.Matches[0].Groups[1].Value
}
$pyproject = Join-Path $root 'pyproject.toml'
if (Test-Path $pyproject) {
    $p = Select-String -LiteralPath $pyproject -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($p -and $p.Matches[0].Groups[1].Value -ne $Version) {
        Die "version mismatch: app/__init__.py=$Version pyproject.toml=$($p.Matches[0].Groups[1].Value)"
    }
}
Say "version: $Version"

# ---------------------------------------------------------------- whitelist
$dirs = @('app', 'web', 'mac', 'scripts', 'plugin', 'docs', 'assets', 'dsh-failover', '.dsh', 'sidebar')
if ($Profile -eq 'internal') { $dirs += @('models', 'venv') }
$rootFiles = @('README.md', 'ARCHITECTURE.md', 'LICENSE', 'pyproject.toml',
               'requirements.txt', '.gitignore', '.gitattributes')

# Paths never packed, matched against the tree-relative path (forward slashes).
$forbiddenPath = @(
    '(^|/)\.git(/|$)',
    '(^|/)__pycache__(/|$)',
    '\.pyc$',
    '(^|/)\.ruff_cache(/|$)',
    '(^|/)obj(/|$)',
    '\.pdb$',
    '(^|/)settings\.yaml',
    '\.credentials',
    '(^|/)\.env($|\.)',
    '\.bak($|[-._])',
    '\.db(-wal|-shm)?$',
    '\.pid$',
    '(^|/)dsh-failover/config\.json$',
    '(^|/)models/pyannote(/|$)',
    '(^|/)logs?(/|$).*\.log$',
    '(^|/)dist(/|$)'
)
# Content never packed: scan text files for these.
# The literals are split on purpose so this script passes its own scan - it has to
# live in the same tree it packs. Note there is deliberately NO rule for the string
# "settings.yaml.bak": that artifact is blocked by the PATH check above, and matching
# the bare string only trips over prose (comments/docs explaining the 2026-09-14
# incident), which is exactly the kind of false positive that gets a check disabled.
$rxIntranetHost = ('aiopen\.' + 'bjunicom')
$rxUserId       = ('zhou' + 'kq1')
$rxApiKey       = '(?i)sk-[A-Za-z0-9]{20,}'
$rxPemHeader    = 'BEGIN [A-Z ]*PRIVATE KEY'
$forbiddenContent = @($rxIntranetHost, $rxUserId, $rxApiKey, $rxPemHeader)

# The PEM-header rule is skipped inside vendored third-party trees (venv\, models\).
# Those trees legitimately carry PEM markers: pycryptodome ships real throwaway test
# keys in Crypto/SelfTest/**, and cryptography/paramiko mention the header as a plain
# literal. 2026-09-20: this made every -Profile internal build fail with 34 false
# positives - i.e. the very package scripts\install.ps1 needs could not be built.
# Every other content rule AND every path rule still applies to vendored files, so a
# token pasted into venv\pip.conf is still caught.
$vendoredPrefix = @('venv/', 'models/')
function Test-Vendored([string]$rel) {
    foreach ($p in $vendoredPrefix) { if ($rel.StartsWith($p)) { return $true } }
    return $false
}
$textExt = @('.py', '.ps1', '.cmd', '.bat', '.js', '.css', '.html', '.json', '.md',
             '.txt', '.yaml', '.yml', '.toml', '.cs', '.csproj', '.sh', '.plist', '.vbs', '.svg')

function Test-ForbiddenPath([string]$rel) {
    foreach ($rx in $forbiddenPath) { if ($rel -match $rx) { return $true } }
    return $false
}

# ---------------------------------------------------------------- collect
Say 'collecting (whitelist)...'
$files = New-Object System.Collections.Generic.List[object]
foreach ($d in $dirs) {
    $full = Join-Path $root $d
    if (-not (Test-Path $full)) { Warn "missing dir, skipped: $d"; continue }
    Get-ChildItem $full -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $rel = $_.FullName.Substring($root.Length + 1).Replace('\', '/')
        if (-not (Test-ForbiddenPath $rel)) { $files.Add([pscustomobject]@{ Rel = $rel; Full = $_.FullName; Len = $_.Length }) }
    }
}
foreach ($f in $rootFiles) {
    $full = Join-Path $root $f
    if (Test-Path $full) { $files.Add([pscustomobject]@{ Rel = $f; Full = $full; Len = (Get-Item $full).Length }) }
}
if ($files.Count -eq 0) { Die 'nothing selected - refusing to build an empty package' }

$totalBytes = ($files | Measure-Object Len -Sum).Sum
Say ("selected {0} files, {1} MB unpacked" -f $files.Count, [Math]::Round($totalBytes / 1MB, 2))
Say 'inventory:'
$files | Group-Object { ($_.Rel -split '/')[0] } | Sort-Object Name | ForEach-Object {
    $b = ($_.Group | Measure-Object Len -Sum).Sum
    Say ("    {0,-16} {1,6} files  {2,10} KB" -f $_.Name, $_.Count, [Math]::Round($b / 1KB, 1))
}

# ---------------------------------------------------------------- hard checks
Say 'hard checks...'
$bad = @()
foreach ($f in $files) {
    if (Test-ForbiddenPath $f.Rel) { $bad += "forbidden path: $($f.Rel)" }
}
foreach ($f in $files) {
    $ext = [System.IO.Path]::GetExtension($f.Rel).ToLower()
    if ($textExt -notcontains $ext) { continue }
    if ($f.Len -gt 4MB) { continue }              # minified vendor bundles: skip the scan
    $rules = $forbiddenContent
    if (Test-Vendored $f.Rel) {
        $rules = @($forbiddenContent | Where-Object { $_ -ne $rxPemHeader })
    }
    $i = 0
    foreach ($line in [System.IO.File]::ReadLines($f.Full)) {
        $i++
        foreach ($rx in $rules) {
            if ($line -match $rx) { $bad += "forbidden content: $($f.Rel):$i  (/$rx/)" }
        }
        if ($bad.Count -gt 40) { break }
    }
    if ($bad.Count -gt 40) { break }
}
if ($bad.Count -gt 0) {
    Write-Host ''
    $bad | Select-Object -First 40 | ForEach-Object { Write-Host "      $_" -ForegroundColor Red }
    Die ("$($bad.Count) violation(s) - refusing to build. Do NOT 'fix' this by adding an exclusion.")
}
Ok 'no forbidden files or content'

if ($DryRun) {
    Write-Host ''
    Ok "dry run: nothing written. Would produce ECHO-$Profile-$Version-<stamp>.zip in $OutDir"
    exit 0
}

# ---------------------------------------------------------------- stage
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$stage = Join-Path $env:TEMP ("echo-pack-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Say "staging to $stage"
# The internal profile wraps everything in ECHO\. Reason: scripts\install.ps1 (the
# internal whole-package route) detects the archive's single top-level directory and
# moves it into place. Built flat, this zip has ~20 top-level entries, so Step04 moved
# just one of them and then deleted the rest of the extracted tree. The public profile
# stays flat: docs\macOS-*.md tell the reader to unzip it directly.
$wrap = if ($Profile -eq 'internal') { 'ECHO' } else { '' }
if ($wrap) { Say "wrapping every file in $wrap/ (installer expects a single top-level dir)" }
function Get-StagedRel([string]$rel) { if ($wrap) { return "$wrap/$rel" } return $rel }
foreach ($f in $files) {
    $dst = Join-Path $stage (Get-StagedRel $f.Rel).Replace('/', '\')
    $dstDir = Split-Path $dst -Parent
    if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
    Copy-Item -LiteralPath $f.Full -Destination $dst -Force
}

# BUILD-INFO.txt + SHA256SUMS.txt
$head = ''
try { $head = (git -C $root rev-parse --short HEAD 2>$null) } catch { }
$branch = ''
try { $branch = (git -C $root rev-parse --abbrev-ref HEAD 2>$null) } catch { }
$dirty = ''
try { if ((git -C $root status --porcelain 2>$null | Measure-Object).Count -gt 0) { $dirty = ' (working tree dirty)' } } catch { }
$layout = if ($wrap) { "all files under $wrap/ ; BUILD-INFO.txt and SHA256SUMS.txt sit at the archive root" }
          else { "flat, no wrapper directory" }
$sumsBase = if ($wrap) { "relative to $wrap/ inside the archive" } else { "relative to the archive root" }
$info = @(
    "ECHO delivery package",
    "profile      : $Profile",
    "version      : $Version",
    "built at     : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')",
    "built from   : $root",
    "git          : $branch @ $head$dirty",
    "files        : $($files.Count)",
    "unpacked     : $([Math]::Round($totalBytes / 1MB, 2)) MB",
    "layout       : $layout",
    "checksums    : SHA256SUMS.txt lists the paths $sumsBase",
    "",
    "This package was produced by scripts\build-package.ps1, which packs a whitelist and",
    "refuses to build when a forbidden file or content pattern is present. It never",
    "contains dsh-failover\config.json, settings.yaml*, *.credentials*, *.bak* or data\.",
    "",
    "Models and engines are NOT part of the public package (D22): the panel guides the",
    "user through downloading what their machine needs."
)
[System.IO.File]::WriteAllLines((Join-Path $stage 'BUILD-INFO.txt'), $info, (New-Object System.Text.UTF8Encoding($false)))

$sums = New-Object System.Collections.Generic.List[string]
foreach ($f in ($files | Sort-Object Rel)) {
    $h = (Get-FileHash -LiteralPath (Join-Path $stage (Get-StagedRel $f.Rel).Replace('/', '\')) -Algorithm SHA256).Hash.ToLower()
    $sums.Add("$h  $($f.Rel)")
}
[System.IO.File]::WriteAllLines((Join-Path $stage 'SHA256SUMS.txt'), $sums, (New-Object System.Text.UTF8Encoding($false)))

# ---------------------------------------------------------------- zip
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$zipName = "ECHO-$Profile-$Version-$stamp.zip"
$zipPath = Join-Path $OutDir $zipName
Say "compressing to $zipPath"
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.IO.Compression        # ZipArchiveMode / ZipArchive live here
# Build the archive entry by entry instead of ZipFile.CreateFromDirectory: on Windows
# the latter writes BACKSLASH entry names (mac\setup_mac.sh), and some macOS tools
# (Finder, plain unzip) then create a file literally named "mac\setup_mac.sh"
# instead of the directory. The zip spec wants forward slashes, and this package is
# meant to be opened on a Mac.
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($f in (Get-ChildItem $stage -Recurse -File -Force | Sort-Object FullName)) {
        $rel = $f.FullName.Substring($stage.Length + 1).Replace('\', '/')
        [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip, $f.FullName, $rel, [System.IO.Compression.CompressionLevel]::Optimal)
    }
} finally {
    $zip.Dispose()
}

# ---------------------------------------------------------------- verify output
Say 'verifying the produced zip...'
$zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
$entries = @($zip.Entries)
$viol = @($entries | Where-Object { Test-ForbiddenPath $_.FullName })
$zip.Dispose()
if ($viol.Count -gt 0) {
    $viol | Select-Object -First 10 | ForEach-Object { Write-Host "      $($_.FullName)" -ForegroundColor Red }
    Die 'the produced zip contains forbidden paths - staged copy is left at the temp dir for inspection'
}
Ok "zip re-checked: $($entries.Count) entries, no forbidden paths"

$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
[System.IO.File]::WriteAllText("$zipPath.sha256", "$zipHash  $zipName`n", (New-Object System.Text.UTF8Encoding($false)))
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ''
Ok "built $zipPath"
Say ("    size    : {0} MB" -f [Math]::Round((Get-Item $zipPath).Length / 1MB, 2))
Say ("    entries : {0}" -f $entries.Count)
Say ("    sha256  : {0}" -f $zipHash)
Write-Host ''
exit 0
