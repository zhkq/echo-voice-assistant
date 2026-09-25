# =====================================================================
# install-offline.ps1 - one-command OFFLINE install for the ECHO min kit.
#
# This file lives in the kit root (next to ECHO\ and bundle\).
# It is a thin wrapper: it forwards to the real fast-path installer that
# ships inside the main package:
#
#     ECHO\scripts\install-all.ps1 -Offline -Yes -Agent none ...
#
# Why a wrapper instead of putting the logic here:
#   * install-all.ps1 is part of the packed tree (scripts\ is whitelisted by
#     build-package.ps1), so the ONLINE kit and the repo use the very same
#     script. Two copies would drift.
#   * `-File install.ps1` propagates the script's `exit N` as the process
#     exit code, which is what this wrapper needs to report success/failure.
#
# Offline means: pip gets `--no-index --find-links bundle\wheels`, the STT
# model is copied out of bundle\models, and no agent (DSH/harness) is
# installed - @deepseek-ai/dsh is a private npm package and must not be
# redistributed with the kit.
#
# Usage (also reachable by double clicking the .cmd launcher next to this file):
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\install-offline.ps1
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\install-offline.ps1 `
#         -Root D:\ECHO -Profile main -Wake
#
# Encoding: ASCII only on purpose (no BOM needed, safe in every code page).
# =====================================================================
param(
    [string]$Root = '',
    [ValidateSet('minimal', 'main')][string]$Profile = 'minimal',
    [switch]$Wake,
    [switch]$Diarize,
    [string]$NotesDir = '',
    [switch]$NoShortcuts,
    [switch]$SkipStart,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
if (-not $here) { try { $here = Split-Path -Parent $PSCommandPath } catch { $here = (Get-Location).Path } }

$all = Join-Path $here 'ECHO\scripts\install-all.ps1'
if (-not (Test-Path $all)) {
    Write-Host ("  [x] missing {0} - the kit looks incomplete (ECHO\scripts\install-all.ps1)" -f $all) -ForegroundColor Red
    Write-Host '      Re-unpack the kit zip, or run ECHO\scripts\install.ps1 manually.' -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path (Join-Path $here 'bundle\wheels'))) {
    Write-Host ("  [x] missing {0} - this is not the offline kit (or it was repacked wrong)" -f (Join-Path $here 'bundle\wheels')) -ForegroundColor Red
    exit 1
}

# -Offline -Yes -Agent none are hard-wired here: this entry point only makes
# sense for the offline kit, and the kit never ships an agent.
$argList = @('-Offline', '-Yes', '-Agent', 'none', '-Profile', $Profile)
if ($Root) { $argList += @('-Root', $Root) }
if ($Wake) { $argList += '-Wake' }
if ($Diarize) { $argList += '-Diarize' }
if ($NotesDir) { $argList += @('-NotesDir', $NotesDir) }
if ($NoShortcuts) { $argList += '-NoShortcuts' }
if ($SkipStart) { $argList += '-SkipStart' }

Write-Host ("  -> powershell -NoProfile -ExecutionPolicy Bypass -File `"{0}`" {1}" -f $all, ($argList -join ' ')) -ForegroundColor DarkGray
if ($DryRun) { exit 0 }

& powershell -NoProfile -ExecutionPolicy Bypass -File $all @argList
exit $LASTEXITCODE
