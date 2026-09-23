# =====================================================================
# deploy-stable.ps1 - push the repo's current build into the STABLE install.
#
# WHY THIS EXISTS (2026-09-23, user's workflow rule)
#   The user's rule, in their words: develop in the dev tree; push to git only
#   once it is stable; and push into the stable install only on their explicit
#   instruction (never as a side effect of "fixing something").
#   The dev tree (C:\echo-dev) and the stable install (D:\ECHO) are TWO SEPARATE
#   copies of the code. Editing the repo does NOT change the running stable ECHO.
#   Earlier that gap was bridged by ad-hoc file copies, which bypassed every
#   check, could not be reproduced, and once KILLED a meeting recording because
#   the copy ended with a silent restart. This script is the ONLY sanctioned way
#   to push code into the stable install, and it is run ONLY on the user's
#   explicit instruction - never as a side effect of "fixing something".
#
# WHAT IT DOES
#   1. refuses while a meeting is recording   (GET <dest>/api/status -> meeting.active)
#   2. refuses on a dirty working tree        (unless -AllowDirty)
#   3. builds the kit                         (scripts\build_kit.py - the same artifact users get)
#   3b. picks the kit for THIS machine's platform via
#       `build_kit.py --deploy-kit win|macos` and refuses a kit whose own
#       manifest.json says another platform  (see the 2026-09-23 note below)
#   4. overlays <kit>\ECHO\*        onto <DestDir>            (data\ models\ runtime-core\ untouched)
#      copies  <kit>\echo-install\* onto <DestDir>\.dsh\skills\echo-install\
#   5. verifies the target                    (compileall app + import smoke)
#   6. restarts ECHO ONLY with -Restart       (default: takes effect at the next start)
#
# USAGE
#   powershell -File scripts\deploy-stable.ps1 -DryRun     # plan + step-by-step diff, write nothing
#   powershell -File scripts\deploy-stable.ps1             # deploy, do NOT touch the running ECHO
#   powershell -File scripts\deploy-stable.ps1 -Restart    # deploy + restart ECHO
#   powershell -File scripts\deploy-stable.ps1 -NoBuild    # reuse the newest kit from dist\
#   powershell -File scripts\deploy-stable.ps1 -Kit X.zip  # pin one archive (name in dist\ or a path)
#
# ASCII-ONLY on purpose: Windows PowerShell 5.1 parses a BOM-less .ps1 as ANSI,
# so a non-ASCII literal can silently break the script (tests/test_script_encoding.py).
# =====================================================================
param(
    [string]$DestDir = 'D:\ECHO',
    [switch]$DryRun,
    [switch]$Restart,
    [switch]$NoBuild,
    [switch]$AllowDirty,
    [string]$Kit = '',
    [int]$WaitSeconds = 120
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent

function Say([string]$m)  { Write-Host "  $m" }
function Ok([string]$m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn2([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "  [fail] $m" -ForegroundColor Red }

# ------------------------------------------------------------------ platform
# 2026-09-23 BUG (fixed here): the kit was picked as "the newest ECHO-kit-*.zip in
# dist", but do_build() assembles win FIRST and macos SECOND, so the macOS kit is
# always the newest. A Windows deploy therefore overlaid the macOS kit - and the two
# kits differ in exactly one file's CONTENT: manifest.json. D:\ECHO\manifest.json
# ended up saying platform=macos-universal, and manifest.json is what install/update
# flows read to decide which platform this unpacked tree is.
# Now the selector lives in ONE place: scripts\build_kit.py --deploy-kit <key>
# (build_kit.find_kits / newest_kit, pinned by tests\test_build_kit.py::KitDiscovery).
function Get-HostPlatformKey {
    # This script deploys onto the machine it runs on; the kits are win-x64 and
    # macos-universal (see build_kit.PLATFORMS).
    if ($env:OS -eq 'Windows_NT') { return 'win' }
    return 'macos'
}

function Get-KitPlatform([string]$ZipPath) {
    # Read ECHO/manifest.json out of the zip WITHOUT expanding it.
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        $z = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
        try {
            $e = $z.Entries | Where-Object { $_.FullName -match '(^|/)ECHO/manifest\.json$' } |
                 Select-Object -First 1
            if (-not $e) { return '' }
            $sr = New-Object System.IO.StreamReader($e.Open())
            $txt = $sr.ReadToEnd()
            $sr.Close()
            $m = [regex]::Match($txt, '"platform"\s*:\s*"([^"]+)"')
            if ($m.Success) { return $m.Groups[1].Value }
        } finally { $z.Dispose() }
    } catch { return '' }
    return ''
}

Write-Host ''
Write-Host '  === deploy to the STABLE install ==='
Say "repo : $repoRoot"
Say "dest : $DestDir"
if ($DryRun) { Warn2 'DRY RUN - nothing will be written, ECHO will not be touched' }
Write-Host ''

if (-not (Test-Path -LiteralPath $DestDir)) { Fail "dest not found: $DestDir"; exit 1 }

# ---------------------------------------------------------------- python / port
function Get-RepoPython {
    foreach ($rel in @('runtime-core\python.exe', 'runtime-core\Scripts\python.exe',
                       'venv\Scripts\python.exe')) {
        $p = Join-Path $repoRoot $rel
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return ''
}
$py = Get-RepoPython
if (-not $py) { Fail 'no python found in the repo (runtime-core\ or venv\)'; exit 1 }

function Get-DestPort {
    $f = Join-Path $DestDir 'data\echo-port.txt'
    if (Test-Path -LiteralPath $f) {
        try { $v = [int]((Get-Content $f -Raw).Trim()); if ($v -gt 0) { return $v } } catch { }
    }
    return 8970
}

# ---- 1) a recording in progress is a hard stop ------------------------------
$port = Get-DestPort
$meetingActive = $false
try {
    $st = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/status" -TimeoutSec 8
    if ($st.meeting -and $st.meeting.active) { $meetingActive = $true }
} catch {
    Warn2 "could not read /api/status (ECHO may be stopped): $($_.Exception.Message)"
}
if ($meetingActive) {
    Fail "a MEETING is being recorded right now - refusing to deploy."
    Say  '       Restarting ECHO to load new code would kill that recording.'
    Say  '       Stop the recording first, then run this again.'
    exit 2
}
Ok 'no meeting is being recorded'

# ---- 2) working tree must be clean -----------------------------------------
$dirty = @(& git -C $repoRoot status --porcelain 2>$null)
if ($dirty.Count -gt 0 -and -not $AllowDirty) {
    Fail 'the working tree has uncommitted changes - commit them first (or -AllowDirty):'
    $dirty | Select-Object -First 12 | ForEach-Object { Say "      $_" }
    exit 2
}
Ok 'working tree is clean'

# ---- 3) build the kit ------------------------------------------------------
if ($NoBuild) {
    Say 'skipping the build (-NoBuild): reusing the newest dist\ECHO-kit-*.zip'
} else {
    Say 'building the kit (scripts\build_kit.py) ...'
    if ($DryRun) {
        Say '  [dry] would run: python scripts\build_kit.py'
    } else {
        & $py (Join-Path $repoRoot 'scripts\build_kit.py') 2>&1 |
            ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
        if ($LASTEXITCODE -ne 0) { Fail "build_kit.py failed ($LASTEXITCODE)"; exit 1 }
    }
}
$zip = $null
if ($Kit) {
    # Explicit override: a name inside dist\, or a full path anywhere.
    $cand = $Kit
    if (-not (Test-Path -LiteralPath $cand)) {
        $cand = Join-Path (Join-Path $repoRoot 'dist') $Kit
    }
    if (-not (Test-Path -LiteralPath $cand)) { Fail "kit not found: $Kit"; exit 1 }
    $zip = Get-Item -LiteralPath $cand
    Say "kit  : $($zip.Name)  (pinned with -Kit)"
} else {
    $platKey = Get-HostPlatformKey
    $kitLine = @(& $py (Join-Path $repoRoot 'scripts\build_kit.py') --deploy-kit $platKey 2>&1 |
                ForEach-Object { "$_" }) | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0 -or -not $kitLine) {
        Fail "could not resolve the $platKey kit (scripts\build_kit.py --deploy-kit $platKey): $kitLine"
        exit 1
    }
    $zip = Get-Item -LiteralPath $kitLine.Trim() -ErrorAction SilentlyContinue
    if (-not $zip) { Fail "kit path from build_kit.py does not exist: $kitLine"; exit 1 }
    Say "kit  : $($zip.Name)  (platform=$platKey)"
    # The guard that would have caught the 2026-09-23 mix-up: refuse a kit whose own
    # manifest says another platform.
    $kitPlat = Get-KitPlatform $zip.FullName
    if ($kitPlat) {
        $matchesPlat = $false
        if ($platKey -eq 'win') {
            if ($kitPlat -like 'win*') { $matchesPlat = $true }
        } else {
            if ($kitPlat -like 'macos*') { $matchesPlat = $true }
        }
        if (-not $matchesPlat) {
            Fail "this kit is for '$kitPlat' but this machine is '$platKey' - refusing to overlay it."
            Say  '       (use -Kit to pin a different archive on purpose)'
            exit 1
        }
    } else {
        Warn2 'could not read ECHO/manifest.json from the kit - platform not verified'
    }
}

# ---- 4) overlay ------------------------------------------------------------
$tmp = Join-Path $env:TEMP ("echo-deploy-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if ($DryRun) {
    Say "  [dry] would expand $($zip.Name) to $tmp"
} else {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Expand-Archive -LiteralPath $zip.FullName -DestinationPath $tmp -Force
}
$kitDir = if ($DryRun) { $null } else { (Get-ChildItem -Path $tmp -Directory | Select-Object -First 1) }
$srcEcho = if ($kitDir) { Join-Path $kitDir.FullName 'ECHO' } else { $null }
$srcSkill = if ($kitDir) { Join-Path $kitDir.FullName 'echo-install' } else { $null }

$added = 0; $updated = 0; $same = 0
function Copy-Tree([string]$From, [string]$To, [string]$Label) {
    if ($DryRun) { Say "  [dry] would overlay $Label : $From -> $To"; return }
    if (-not (Test-Path -LiteralPath $From)) { Warn2 "missing in kit: $From"; return }
    Get-ChildItem -LiteralPath $From -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($From.Length).TrimStart('\')
        $dst = Join-Path $To $rel
        $dir = Split-Path $dst -Parent
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        if (-not (Test-Path -LiteralPath $dst)) {
            $script:added++
        } elseif ((Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash -ne
                  (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash) {
            $script:updated++
            Copy-Item -LiteralPath $dst -Destination "$dst.bak-before-deploy" -Force
        } else {
            $script:same++
            return
        }
        Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
    }
}

if ($DryRun) {
    Say "  [dry] would overlay the kit's ECHO\ over $DestDir (data\ models\ runtime-core\ are not in the kit, so they stay)"
} else {
    Copy-Tree $srcEcho $DestDir 'ECHO\'
    Copy-Tree $srcSkill (Join-Path $DestDir '.dsh\skills\echo-install') 'echo-install\'
    Ok "overlay done: $added added, $updated updated, $same unchanged"
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# ---- 5) verify the target --------------------------------------------------
$destPy = Join-Path $DestDir 'runtime-core\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $destPy)) { $destPy = Join-Path $DestDir 'runtime-core\python.exe' }
if (Test-Path -LiteralPath $destPy) {
    if ($DryRun) {
        Say '  [dry] would verify: compileall app + import smoke'
    } else {
        Push-Location $DestDir
        try {
            & $destPy -m compileall -q app | Out-Null
            if ($LASTEXITCODE -ne 0) { Fail 'compileall failed on the target'; exit 1 }
            & $destPy -c "import app, app.api, app.meeting, app.workspaces, app.audio.recorder" 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { Fail 'import smoke failed on the target'; exit 1 }
            Ok 'target verified (compileall + import smoke)'
        } finally { Pop-Location }
    }
} else {
    Warn2 "cannot verify: no interpreter under $DestDir\runtime-core"
}

# ---- 6) restart (only when asked) ------------------------------------------
if ($Restart) {
    if ($DryRun) {
        Say '  [dry] would restart ECHO'
    } else {
        Say 'restarting ECHO ...'
        Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
            Where-Object { $_.CommandLine -like "*$DestDir*" -and $_.CommandLine -match 'app\.main' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 3
        $vbs = Join-Path $DestDir 'scripts\echo-startup.vbs'
        if (Test-Path -LiteralPath $vbs) {
            & wscript.exe $vbs
            $waited = 0
            while ($waited -lt $WaitSeconds) {
                Start-Sleep -Seconds 3; $waited += 3
                try {
                    $null = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/status" -TimeoutSec 5
                    Ok "ECHO is back on port $port (${waited}s)"; break
                } catch { }
            }
            if ($waited -ge $WaitSeconds) { Warn2 "ECHO did not answer within ${WaitSeconds}s - check data\logs" }
        } else { Warn2 "no echo-startup.vbs under $DestDir\scripts" }
    }
} else {
    Say 'ECHO was NOT restarted (no -Restart): the new code loads at the next start.'
}

Write-Host ''
if ($DryRun) { Warn2 'dry run finished - nothing changed' } else { Ok 'deploy finished' }
exit 0
