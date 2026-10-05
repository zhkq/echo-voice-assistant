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
#   powershell ... -Quick      # compile + import + platform contract only (skip full tests)
#   powershell ... -Quiet      # silence library logs + print only failures and the summary
#   powershell ... -Parallel   # tests/ in parallel shards -- OPT-IN, see the note below
#   powershell ... -Sequential # explicit single-process run (the default)
#
# Interpreter: $env:ECHO_PYTHON (ASCII junction override, see docs/DEPLOY.md)
#              -> %USERPROFILE%\.echo-venv (ASCII junction, optional)
#              -> venv\Scripts\python.exe
#
# ASCII-ONLY ON PURPOSE (same rule as start.ps1 / startup.ps1 / gh-push.ps1): Windows
# PowerShell 5.1 parses a BOM-less .ps1 as ANSI/GBK, and non-ASCII text then breaks parsing.
param(
    [switch]$Quick,
    [switch]$Quiet,
    # WHY -Parallel EXISTS: the suite is ~24 min single-process on a 24-core box and nearly
    # all of it is *waiting* (test HTTP clients, real subprocess spawns, scrypt), not CPU,
    # so sharding by module is close to free. Measured 2026-10-01: 24 min -> ~4.5 min wall
    # (6 shards, 1331s sum over 104 modules).
    #
    # WHY IT IS NOT THE DEFAULT: on this tree it is not yet green. A 6-shard run left
    # exactly 5 failures that all pass in isolation, i.e. cross-process coupling in the
    # tests, not in this script:
    #   test_config_compat.test_paths_follow_the_user_value_after_reseeding  (paths read the
    #     real data\meetings while another shard was rewriting settings/real db)
    #   test_backend_proc.PortOwnerTests (x3) + SpawnTests.test_spawn_refuses_... (x4)
    #     -- port_owner() under concurrent load reports pid=0 / the wrong owner
    # Full evidence: C:\echo-dev\dist\_parallel_run1.log
    # Promote it to the default only after those two modules are hardened for concurrency;
    # until then -Parallel is for a fast local iteration loop you are willing to re-confirm
    # with -Sequential before pushing. -Sequential exists to say that out loud.
    [switch]$Parallel,
    [switch]$Sequential,
    [int]$Jobs = 0
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
    $t0 = Get-Date
    # STREAM while capturing. The old `$out = & $exe @argv 2>&1` captured EVERYTHING, so the
    # 927-test step (~9 min) produced zero output until it finished: no progress, and no way to
    # tell "slow" from "hung" when reading this as a background job log (user report 2026-09-23).
    $lines = New-Object System.Collections.Generic.List[string]
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe @argv 2>&1 | ForEach-Object {
            $lines.Add([string]$_)
            if (-not $Quiet) { Write-Host ("    " + $_) }
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    $secs = [int]((Get-Date) - $t0).TotalSeconds
    if (-not $Quiet) { Write-Host ("    [{0}s]" -f $secs) -ForegroundColor DarkGray }
    if ($code -eq 0) { $results.Add("PASS  $name (${secs}s)"); return }
    # Already streamed above unless -Quiet; only then re-print the tail for the failure.
    # 60 lines, not 25: the parallel runner puts a failed shard's whole output at the END
    # of its own output, so a short tail can cut off the actual traceback.
    if ($Quiet) { $lines | Select-Object -Last 60 | ForEach-Object { Write-Host ("    " + $_) -ForegroundColor Red } }
    $results.Add("FAIL  $name (exit=$code, ${secs}s)")
}

Write-Host "ECHO Windows gate - interpreter: $py" -ForegroundColor Green
if (-not $Quiet) { Write-Host "repo: $root" }

# -Quiet used to only hide this script's own progress lines, so the log stayed just as big:
# the bulk of it is libraries logging every test request (httpx INFO) plus ResourceWarnings
# from a *passing* suite. Tell the children to drop that, or "quiet" is a lie.
# The repo root goes on PYTHONPATH because the switch lives in <root>\sitecustomize.py,
# which CPython imports at startup -- that is the only hook that reaches a bare
# `python -m unittest` child. Both are set ONLY under -Quiet.
if ($Quiet) {
    $env:ECHO_GATE_QUIET_LIBS = '1'
    $env:PYTHONPATH = if ($env:PYTHONPATH) { "$root;$env:PYTHONPATH" } else { $root }
}

Invoke-Step 'compileall app server mac scripts' $py @('-m', 'compileall', '-q', 'app', 'server', 'mac', 'scripts')
Invoke-Step 'import smoke (entry modules)' $py @('-c', "import app.main, app.api, app.db, app.pathutil, app.modelinfo, app.llm_router, app.audio.tts, app.audio.wake, app.netguard; print('import smoke OK')")
Invoke-Step 'platform contract tests' $py @('-m', 'unittest', '-q', 'tests.test_platform_contract')
if (-not $Quick) {
    $runner = Join-Path $root 'scripts\check-parallel.py'
    if ($Parallel -and -not $Sequential -and (Test-Path $runner)) {
        $runnerArgs = @($runner)
        if ($Jobs -gt 0) { $runnerArgs += @('-j', "$Jobs") }
        Invoke-Step 'unit tests (tests/, parallel shards)' $py $runnerArgs
    } else {
        if ($Parallel -and -not (Test-Path $runner) -and -not $Quiet) {
            Write-Host "    note: scripts\check-parallel.py not found - running sequentially" -ForegroundColor Yellow
        }
        Invoke-Step 'unit tests (tests/)' $py @('-m', 'unittest', 'discover', '-s', 'tests', '-t', '.', '-q')
    }
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
    Invoke-Step 'ruff (undefined names F821 / syntax E9)' $ruffExe ($ruffArgs + @('check', '--select', 'F821,E9', 'app', 'server', 'mac', 'scripts'))
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
