# check-windows.ps1 - Windows smoke gate for ECHO (compile + import + contract + tests).
#
# WHY THIS EXISTS (2026-09-15): ECHO's primary platform is Windows (hotkey / sidebar /
# PowerShell launchers), while the repo also carries macOS
# support that lives in mac/ and is injected through its own entry point. Nothing used to
# check that a change keeps the Windows path alive: a PR touching only "shared" files
# (removed imports, a new path helper, SQLite write locking) can silently break Windows
# even with zero Mac involvement, and CI was green because there was no CI at all.
#
# This is the local gate: run it after merging an external PR, and before pushing
# (scripts\gh-push.ps1 calls it automatically unless -SkipCheck is passed).
#
# USAGE
#   powershell -ExecutionPolicy Bypass -File scripts\check-windows.ps1
#   powershell ... -Quick     # compile + import + platform contract only (skip full tests)
#   powershell ... -Quiet     # print only failures and the final summary
#
# Interpreter: $env:ECHO_PYTHON (ASCII junction override, see docs/DEPLOY.md)
#              -> %USERPROFILE%\.echo-venv (ASCII junction, optional)
#              -> venv\Scripts\python.exe
#
# ASCII-ONLY ON PURPOSE (same rule as start.ps1 / startup.ps1 / gh-push.ps1): Windows
# PowerShell 5.1 parses a BOM-less .ps1 as ANSI/GBK, and non-ASCII text then breaks parsing.
param(
    [switch]$Quick,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$py = $env:ECHO_PYTHON
if (-not ($py -and (Test-Path $py))) {
    $junction = Join-Path $env:USERPROFILE '.echo-venv\Scripts\python.exe'
    if (Test-Path $junction) { $py = $junction }
}
if (-not ($py -and (Test-Path $py))) { $py = Join-Path $root 'runtime-core\python.exe' }
if (-not ($py -and (Test-Path $py))) { $py = Join-Path $root 'runtime-core\Scripts\python.exe' }
if (-not ($py -and (Test-Path $py))) { $py = Join-Path $root 'venv\Scripts\python.exe' }
if (-not (Test-Path $py)) {
    Write-Host "[FAIL] no interpreter found (set ECHO_PYTHON or create venv\): $py" -ForegroundColor Red
    exit 1
}

$results = New-Object System.Collections.Generic.List[string]

function Invoke-Step([string]$name, [string]$exe, [string[]]$argv) {
    if (-not $Quiet) { Write-Host ("--- " + $name) -ForegroundColor Cyan }
    $out = & $exe @argv 2>&1
    $code = $LASTEXITCODE
    if (-not $Quiet) { $out | Select-Object -Last 12 | ForEach-Object { Write-Host ("    " + $_) } }
    if ($code -eq 0) { $results.Add("PASS  $name"); return }
    if ($Quiet) { $out | Select-Object -Last 25 | ForEach-Object { Write-Host ("    " + $_) -ForegroundColor Red } }
    $results.Add("FAIL  $name (exit=$code)")
}

Write-Host "ECHO Windows gate - interpreter: $py" -ForegroundColor Green
if (-not $Quiet) { Write-Host "repo: $root" }

Invoke-Step 'compileall app mac scripts' $py @('-m', 'compileall', '-q', 'app', 'mac', 'scripts')
Invoke-Step 'import smoke (entry modules)' $py @('-c', "import app.main, app.api, app.db, app.pathutil, app.modelinfo, app.llm_router, app.audio.tts, app.audio.wake, app.netguard; print('import smoke OK')")
Invoke-Step 'platform contract tests' $py @('-m', 'unittest', '-q', 'tests.test_platform_contract')
if (-not $Quick) {
    Invoke-Step 'unit tests (tests/)' $py @('-m', 'unittest', 'discover', '-s', 'tests', '-t', '.', '-q')
}

# ruff lives in the "dev" extra (pip install -e .[dev]). Fall back to `uv tool run ruff`
# when the venv has no ruff but uv is available, so the F821 check still runs locally.
# F821 is what catches "import was removed but a function body still uses it" - the exact
# bug class a module-level import smoke test cannot see.
$ruff = $null
& $py -m ruff --version *> $null
if ($LASTEXITCODE -eq 0) {
    $ruff = @($py, '-m', 'ruff')
} else {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        & $uv.Source tool run ruff --version *> $null
        if ($LASTEXITCODE -eq 0) { $ruff = @($uv.Source, 'tool', 'run', 'ruff') }
    }
}
if ($ruff) {
    $ruffExe = $ruff[0]
    $ruffArgs = @()
    if ($ruff.Count -gt 1) { $ruffArgs = $ruff[1..($ruff.Count - 1)] }
    Invoke-Step 'ruff (undefined names F821 / syntax E9)' $ruffExe ($ruffArgs + @('check', '--select', 'F821,E9', 'app', 'mac', 'scripts'))
} else {
    $results.Add('SKIP  ruff (install with: pip install -e .[dev]  or  uv tool install ruff)')
}

Write-Host ''
Write-Host '==== ECHO Windows gate summary ====' -ForegroundColor Green
$results | ForEach-Object { Write-Host ("  " + $_) }
$failed = @($results | Where-Object { $_ -like 'FAIL*' })
if ($failed.Count -gt 0) {
    Write-Host ''
    Write-Host ("[FAIL] " + $failed.Count + " check(s) failed - do not push until fixed.") -ForegroundColor Red
    exit 1
}
Write-Host ''
Write-Host '[OK] Windows gate passed.' -ForegroundColor Green
exit 0
