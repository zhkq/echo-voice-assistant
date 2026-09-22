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
#   powershell -File scripts\build-package.ps1 -Profile main       # D22 main package
#   powershell -File scripts\build-package.ps1 -Profile internal   # legacy monolith
#   powershell -File scripts\build-package.ps1 -DryRun             # checks + inventory only
#   powershell -File scripts\build-package.ps1 -OutDir D:\build
#
# PROFILES
#   public    core code only: app web mac scripts plugin docs assets
#             dsh-failover (minus config.json) .dsh sidebar sources + root docs
#   main      the D22 delivery package: same content as public, PLUS components\*.json,
#             wrapped in ECHO\, with manifest.json at its root. Carries NO runtime and NO
#             model - the installer adds the required component (runtime-core, D23) and
#             the panel wizard adds the optional ones (D24). Hard gate: unpacked <= 20 MB.
#   internal  legacy monolith (pre-D22): public + models\ (minus pyannote) + venv\
#   component one component package: -Profile component -Component runtime-core
#             -> ECHO-component-<id>-<platform>-<version>-<stamp>.zip
#   offline   the offline component bundle (D23): -Profile offline [-Components a,b,c]
#             -> ECHO-offline-<platform>-<version>-<stamp>.zip   (gate: <= 700 MB)
#             A pack carries DESTINATION-RELATIVE paths at the archive root, so unpacking it
#             at the ECHO install root is all that is needed. Pack declarations live in
#             components\*.json ("pack" key): kind=files (copy from -> to) or kind=runtime
#             (build runtime-core from a relocatable CPython + requirements-core.txt).
#
# NEVER PACKED (all profiles)
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
    [ValidateSet('public', 'main', 'internal', 'component', 'offline')][string]$Profile = 'public',
    [string]$OutDir = '',
    [string]$Version = '',
    [string]$Component = '',
    [string[]]$Components = @(),
    # 目标平台的标签，用来命名包与写 manifest.json 的 platform（默认按**构建机**推断）。
    # 为什么需要它：macOS 的交付包只能在 mac 上打（sidebar 是 Windows .NET 产物、打包脚本是
    # PowerShell），而我们要在 Windows 上给同事准备 mac 资料夹 —— 没有这个参数，包名与清单
    # 会永远写着 win-x64（2026-09-21 加）。取值形如 macos-arm64 / macos-universal。
    [string]$Platform = '',
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

# ---------------------------------------------------------------- platform
# Delivery packages are per platform (D22): ECHO-<profile>-<platform>-<version>-<stamp>.
$arch = if ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }
$plat = "win-$arch"
if ($PSVersionTable.PSVersion.Major -ge 6) {
    if ($IsMacOS) { $plat = "macos-$arch" }
    elseif ($IsLinux) { $plat = "linux-$arch" }
}
# -Platform 显式覆盖：让 Windows 上也能产出"给 mac 用"的资料夹（包名与 manifest 的 platform
# 都要标对，否则同事/agent 会以为拿到的是 Windows 包）。
if ($Platform) {
    if ($Platform -notmatch '^[a-z0-9]+-[a-z0-9]+$') {
        Die ("-Platform 形如 macos-arm64 / macos-universal，收到: {0}" -f $Platform)
    }
    $plat = $Platform
}
Say "platform: $plat"

# ---------------------------------------------------------------- component / offline packs
# D22/D23: models and engines ship as components, never inside the main package. A pack carries
# DESTINATION-RELATIVE paths at its archive root (runtime-core\python.exe,
# models\sherpa-onnx-streaming\...), so "unpack at the install root" is the whole install story.
# The pack manifest is deliberately NOT called manifest.json: that name belongs to the main
# package, and a user may well unpack a component pack into the install root by hand.
function Get-PackDecls {
    $decls = @{}
    $dir = Join-Path $root 'components'
    if (-not (Test-Path $dir)) { return $decls }
    foreach ($f in (Get-ChildItem $dir -Filter '*.json' -File | Sort-Object Name)) {
        $data = $null
        try { $data = Get-Content $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
        foreach ($raw in @($data)) {
            if ($raw -and $raw.id -and $raw.pack) { $decls[[string]$raw.id] = $raw.pack }
        }
    }
    return $decls
}

function Get-RelocatablePython {
    # The D22 payload is a standalone CPython (no dependency on the target machine's Python).
    # uv installs exactly that build, so we reuse it instead of inventing our own.
    $uvRoot = Join-Path $env:APPDATA 'uv\python'
    $cands = @()
    if (Test-Path $uvRoot) {
        $cands = @(Get-ChildItem $uvRoot -Directory -Filter 'cpython-3.11*' -ErrorAction SilentlyContinue |
                   Sort-Object Name -Descending)
    }
    if ($cands.Count -eq 0) {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            Say 'no local standalone CPython 3.11 - running "uv python install 3.11.15" (needs network)'
            & $uv.Source python install 3.11.15 2>&1 | ForEach-Object { Say "    $_" }
            if (Test-Path $uvRoot) {
                $cands = @(Get-ChildItem $uvRoot -Directory -Filter 'cpython-3.11*' -ErrorAction SilentlyContinue |
                           Sort-Object Name -Descending)
            }
        }
    }
    foreach ($c in $cands) { if (Test-Path (Join-Path $c.FullName 'python.exe')) { return $c.FullName } }
    return ''
}

function Build-RuntimeCorePayload([string]$destDir) {
    $base = Get-RelocatablePython
    if (-not $base) { Die 'no relocatable CPython available - install uv (https://docs.astral.sh/uv/) first' }
    Say ("runtime-core base: {0}" -f $base)
    if (Test-Path $destDir) { Remove-Item $destDir -Recurse -Force }
    New-Item -ItemType Directory -Path $destDir -Force | Out-Null
    & robocopy $base $destDir /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
    if (-not (Test-Path (Join-Path $destDir 'python.exe'))) { Die ("copy failed: {0}" -f $destDir) }
    # PEP 668's marker describes uv's SHARED install; this copy is our private artifact.
    $marker = Join-Path $destDir 'Lib\EXTERNALLY-MANAGED'
    if (Test-Path $marker) {
        Remove-Item $marker -Force
        Say 'removed Lib\EXTERNALLY-MANAGED (belongs to uv, not to this copy)'
    }
    $req = Join-Path $root 'requirements-core.txt'
    if (-not (Test-Path $req)) { Die 'requirements-core.txt missing - cannot build runtime-core' }
    Say 'installing core dependencies into runtime-core (needs network)...'
    $py = Join-Path $destDir 'python.exe'
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '-q',
                 '--no-warn-script-location', '-r', $req)
    $out = & $py @pipArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | Select-Object -Last 12 | ForEach-Object { Say "    $_" }
        Die 'runtime-core dependency install failed'
    }
    $chk = & $py -c "import fastapi,uvicorn,pydantic,httpx,numpy,sounddevice,soundfile,soxr,yaml;print('ok')" 2>&1
    if ($LASTEXITCODE -ne 0) { Die ("runtime-core import check failed: {0}" -f (($chk | Select-Object -Last 3) -join ' ')) }
    Ok 'runtime-core payload built and import-checked'
}

function New-ComponentPayload([string]$id, [hashtable]$decls, [string]$stage) {
    if (-not $decls.ContainsKey($id)) { Die ("no pack declaration for '{0}' (add it to components\*.json)" -f $id) }
    $pack = $decls[$id]
    if ($pack.kind -eq 'runtime') {
        $dest = Join-Path $stage ([string]$pack.dest)
        Build-RuntimeCorePayload $dest
        return (Get-ChildItem $dest -Recurse -File -Force | Measure-Object).Count
    }
    if ($pack.kind -eq 'files') {
        $wrote = 0
        foreach ($it in @($pack.items)) {
            $from = Join-Path $root ([string]$it.from).Replace('/', '\')
            if (-not (Test-Path $from)) { Die ("payload missing for '{0}': {1}" -f $id, $it.from) }
            $dst = Join-Path $stage ([string]$it.to).Replace('/', '\')
            New-Item -ItemType Directory -Path $dst -Force | Out-Null
            & robocopy $from $dst /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
            $wrote += (Get-ChildItem $dst -Recurse -File -Force | Measure-Object).Count
        }
        return $wrote
    }
    Die ("unsupported pack kind '{0}' for '{1}'" -f $pack.kind, $id)
}

if ($Profile -eq 'component' -or $Profile -eq 'offline') {
    $decls = Get-PackDecls
    if ($decls.Count -eq 0) { Die 'no pack declarations found in components\*.json' }
    if ($Profile -eq 'component') {
        if (-not $Component) { Die '-Profile component needs -Component <id>' }
        $ids = @($Component)
    } else {
        $ids = @($Components)
        if ($ids.Count -eq 0) { $ids = @('runtime-core', 'stt-sherpa', 'stt-whisper-base', 'wake-kws') }
    }
    foreach ($id in $ids) {
        if (-not $decls.ContainsKey($id)) { Die ("unknown component id: {0}" -f $id) }
    }
    Say ("components: {0}" -f ($ids -join ', '))
    foreach ($id in $ids) {
        $p = $decls[$id]
        Say ("    {0,-22} kind={1,-8} declared ~{2} MB" -f $id, $p.kind, $p.approx_mb)
    }
    $packName = if ($Profile -eq 'offline') { "ECHO-offline-$plat-$Version" } else { "ECHO-component-$($ids[0])-$plat-$Version" }
    if ($DryRun) {
        Write-Host ''
        Ok ("dry run: nothing written. Would produce $packName-<stamp>.zip in $OutDir")
        exit 0
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmm'
    $stage = Join-Path $env:TEMP ("echo-pack-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Say "staging to $stage"
    $compInfo = New-Object System.Collections.Generic.List[object]
    foreach ($id in $ids) {
        Say ("packing {0} ..." -f $id)
        $n = New-ComponentPayload $id $decls $stage
        Say ("    {0} files" -f $n)
        $compInfo.Add([pscustomobject]@{ id = $id; files = $n; approxMb = $decls[$id].approx_mb })
    }
    $stageFiles = @(Get-ChildItem $stage -Recurse -File -Force)
    $packBytes = ($stageFiles | Measure-Object Length -Sum).Sum
    Say ("unpacked: {0} MB" -f [Math]::Round($packBytes / 1MB, 2))
    if ($Profile -eq 'offline' -and $packBytes -gt 700MB) {
        Say 'largest contributors:'
        $stageFiles | Sort-Object Length -Descending | Select-Object -First 8 | ForEach-Object {
            Say ("    {0,10} MB  {1}" -f [Math]::Round($_.Length / 1MB, 1),
                 $_.FullName.Substring($stage.Length + 1))
        }
        Die ("offline bundle is {0} MB unpacked - over the 700 MB budget" -f [Math]::Round($packBytes / 1MB, 2))
    }
    if ($Profile -eq 'offline') { Ok ("offline bundle size gate: {0} MB <= 700 MB" -f [Math]::Round($packBytes / 1MB, 2)) }
    $sums = New-Object System.Collections.Generic.List[string]
    foreach ($f in ($stageFiles | Sort-Object FullName)) {
        $rel = $f.FullName.Substring($stage.Length + 1).Replace('\', '/')
        $h = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLower()
        $sums.Add("$h  $rel")
    }
    $packManifest = [ordered]@{
        format        = 'echo-package/1'
        kind          = $Profile
        appVersion    = $Version
        platform      = $plat
        builtAt       = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')
        components    = @($compInfo | ForEach-Object { [ordered]@{ id = $_.id; files = $_.files; approxMb = $_.approxMb } })
        files         = $stageFiles.Count
        unpackedBytes = $packBytes
        checksums     = 'SHA256SUMS.txt'
        note          = 'Payload paths are relative to the ECHO install root: unpack there. The installer takes runtime-core; the panel wizard takes the rest (D23/D24).'
    }
    [System.IO.File]::WriteAllText((Join-Path $stage 'pack-manifest.json'),
        (($packManifest | ConvertTo-Json -Depth 6) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
    [System.IO.File]::WriteAllLines((Join-Path $stage 'SHA256SUMS.txt'), $sums, (New-Object System.Text.UTF8Encoding($false)))
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
    $zipPath = Join-Path $OutDir ("$packName-$stamp.zip")
    Say "compressing to $zipPath"
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    Add-Type -AssemblyName System.IO.Compression   # ZipArchiveMode lives here, not in FileSystem
    $zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($f in (Get-ChildItem $stage -Recurse -File -Force | Sort-Object FullName)) {
            $rel = $f.FullName.Substring($stage.Length + 1).Replace('\', '/')
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $f.FullName, $rel, [System.IO.Compression.CompressionLevel]::Optimal)
        }
    } finally { $zip.Dispose() }
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
    [System.IO.File]::WriteAllText("$zipPath.sha256", "$zipHash  $(Split-Path $zipPath -Leaf)`n",
        (New-Object System.Text.UTF8Encoding($false)))
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host ''
    Ok ("built {0}" -f $zipPath)
    Say ("    size    : {0} MB" -f [Math]::Round((Get-Item $zipPath).Length / 1MB, 2))
    Say ("    files   : {0}" -f $stageFiles.Count)
    Say ("    sha256  : {0}" -f $zipHash)
    Write-Host ''
    exit 0
}

# ---------------------------------------------------------------- whitelist
$dirs = @('app', 'web', 'mac', 'scripts', 'plugin', 'docs', 'assets', 'dsh-failover', '.dsh', 'sidebar')
if ($Profile -eq 'main') {
    # components\*.json = the offline component declarations the panel also reads.
    if (Test-Path (Join-Path $root 'components')) { $dirs += @('components') }
    else { Say 'components\ not present yet - manifest.json will declare none' }
}
if ($Profile -eq 'internal') { $dirs += @('models', 'venv') }
$rootFiles = @('README.md', 'ARCHITECTURE.md', 'LICENSE', 'pyproject.toml',
               'requirements.txt', 'requirements-core.txt', '.gitignore', '.gitattributes')

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
    '(^|/)dsh-failover/homes\.json$',
    '(^|/)models/pyannote(/|$)',
    '(^|/)logs?(/|$).*\.log$',
    '(^|/)dist(/|$)'
)
# mac 包不带 Windows 边条的 .NET 构建产物：sidebar/bin/ 里是 net7.0-windows/win-x64 的
# 预编译 exe 与 WebView2 DLL（约 4 MB），对 mac 毫无用处（mac 的浮动框是 mac/sidebar 下的
# Swift，用 Xcode 现编）。源码（.csproj/Program.cs）保留，将来在 Windows 上重建还要用。
if ($plat -like 'macos*') {
    $forbiddenPath += '(^|/)sidebar/bin/'
    Say 'macos pack: excluding sidebar/bin (Windows .NET build output)'
}
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

# ---------------------------------------------------------------- D22 size gate
# The main package has exactly one job: be small enough to hand over by mail/IM and to
# upgrade in seconds. If it grows past the budget, fail here instead of quietly shipping
# a package that defeats the whole split (REFACTOR-PLAN D22: 10-20 MB, main = code only).
if ($Profile -eq 'main') {
    if ($totalBytes -gt 20MB) {
        Say 'largest contributors:'
        $files | Sort-Object Len -Descending | Select-Object -First 10 | ForEach-Object {
            Say ("    {0,10} KB  {1}" -f [Math]::Round($_.Len / 1KB, 1), $_.Rel)
        }
        Die ("main package is {0} MB unpacked - over the 20 MB D22 budget" -f [Math]::Round($totalBytes / 1MB, 2))
    }
    Ok ("main package size gate: {0} MB <= 20 MB" -f [Math]::Round($totalBytes / 1MB, 2))
}

if ($DryRun) {
    Write-Host ''
    Ok "dry run: nothing written. Would produce ECHO-$Profile-$plat-$Version-<stamp>.zip in $OutDir"
    exit 0
}

# ---------------------------------------------------------------- stage
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$stage = Join-Path $env:TEMP ("echo-pack-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Say "staging to $stage"
# The main and internal profiles wrap everything in ECHO\. Reason: scripts\install.ps1
# (the delivery route) detects the archive's single top-level directory and moves it into
# place. Built flat, the zip has ~20 top-level entries, so Step04 used to move just one of
# them and then delete the rest of the extracted tree. The public profile stays flat:
# docs\macOS-*.md tell the reader to unzip it directly.
$wrap = if ($Profile -eq 'public') { '' } else { 'ECHO' }
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

# ---------------------------------------------------------------- manifest.json
# The installer and the panel wizard both need to know "what is this package, and what
# does it still need". requiredComponents is PARSED from app\components.py REQUIRED_IDS
# rather than duplicated here, so the manifest can never drift from the panel's view.
$requiredIds = @()
$compPy = Join-Path $root 'app\components.py'
if (Test-Path $compPy) {
    $mm = Select-String -LiteralPath $compPy -Pattern 'REQUIRED_IDS\s*=\s*\(([^)]*)\)' | Select-Object -First 1
    if ($mm) {
        $requiredIds = @([regex]::Matches($mm.Matches[0].Groups[1].Value, '"([^"]+)"') |
                         ForEach-Object { $_.Groups[1].Value })
    }
}
if ($requiredIds.Count -eq 0) {
    Warn 'could not parse REQUIRED_IDS from app\components.py - assuming runtime-core'
    $requiredIds = @('runtime-core')
}
$compDecl = @()
$compDir = Join-Path $root 'components'
if (Test-Path $compDir) {
    # 只声明**真的进了包**的那些：components/ 目前只随 main 档打包，public 档不打。
    # 不筛的话 public 的 manifest 会声明一个包里根本没有的文件 —— 交付清单里的假话，
    # 而 manifest.json 正是安装流程用来判断"这是已解开的包"的那个文件（2026-09-22 发现：
    # mac 走 public 时它声明了 components/offline-pack.json，包里却没有）。
    $packedRels = @($files | ForEach-Object { $_.Rel })
    $compDecl = @(Get-ChildItem $compDir -Filter '*.json' -File | Sort-Object Name |
                  ForEach-Object { "components/$($_.Name)" } |
                  Where-Object { $packedRels -contains $_ })
}
$manifest = [ordered]@{
    format             = 'echo-package/1'
    kind               = $Profile
    appVersion         = $Version
    platform           = $plat
    builtAt            = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')
    git                = "$branch @ $head$dirty"
    layout             = $layout
    files              = $files.Count
    unpackedBytes      = $totalBytes
    checksums          = 'SHA256SUMS.txt'
    runtimeBundled     = ($Profile -eq 'internal')
    modelsBundled      = ($Profile -eq 'internal')
    requiredComponents = $requiredIds
    componentManifests = $compDecl
    entrypoints        = @('install.bat', 'install.ps1', 'scripts/start.ps1')
    note               = 'No runtime and no models are bundled (D22). The installer adds the required components; the panel wizard adds the optional ones.'
}
$manifestRel = if ($wrap) { "$wrap/manifest.json" } else { 'manifest.json' }
[System.IO.File]::WriteAllText((Join-Path $stage $manifestRel.Replace('/', '\')),
    (($manifest | ConvertTo-Json -Depth 6) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
Say ("manifest.json: kind=$Profile platform=$plat required=$($requiredIds -join ',') components=$($compDecl.Count)")

# ---------------------------------------------------------------- zip
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$zipName = "ECHO-$Profile-$plat-$Version-$stamp.zip"
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
