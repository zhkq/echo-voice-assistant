# clean-removed-settings.ps1 - remove settings rows that are no longer defined.
#
# WHY THIS EXISTS (2026-10-23 field bug): an install upgraded from an older version
# kept four orphan rows in data\echo.db (`providerLlm*` - the old "language model
# provider / online LLM" inputs). The panel lists settings from DB rows, so those
# four showed up under "Uncategorized" as garbage. v2.1.9 deletes them on startup; this
# script lets an already-upgraded v2.1.8 install clean them WITHOUT re-downloading
# a whole kit.
#
# WHAT IT DOES
#   1. finds <Root>\data\echo.db and <Root>\runtime-core\...\python.exe
#   2. copies the DB to echo.db.bak-before-clean-<stamp> (unless -DryRun)
#   3. deletes rows whose key is in $Removed (the same four keys as app/config.py
#      REMOVED_SETTINGS in v2.1.9)
#   4. prints what it found/deleted
#
# It touches NO other setting, NO meeting/model/backend data.
# ECHO can keep running (SQLite allows the concurrent write); refresh the panel
# afterwards. Restart ECHO if the panel still shows them from a cached page.
#
# ASCII-ONLY ON PURPOSE (Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI).
param(
    [string]$Root = 'D:\ECHO',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$db = Join-Path $Root 'data\echo.db'
if (-not (Test-Path -LiteralPath $db)) {
    throw "echo.db not found: $db  (pass -Root <your ECHO folder>)"
}

$py = ''
foreach ($rel in @('runtime-core\Scripts\python.exe', 'runtime-core\python.exe',
                   'venv\Scripts\python.exe')) {
    $p = Join-Path $Root $rel
    if (Test-Path -LiteralPath $p) { $py = $p; break }
}
if (-not $py) { throw "no Python found under $Root\runtime-core" }

$Removed = @('providerLlm', 'providerLlmBaseUrl', 'providerLlmApiKey', 'providerLlmModel')

if (-not $DryRun) {
    $bak = "$db.bak-before-clean-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Copy-Item -LiteralPath $db -Destination $bak -Force
    Write-Host ("backup : {0}" -f $bak)
} else {
    Write-Host 'dry run: no backup, no writes'
}

$tmp = Join-Path $env:TEMP ("echo-clean-settings-{0}.py" -f $PID)
@'
import sqlite3
import sys

db = sys.argv[1]
mode = sys.argv[2]
keys = sys.argv[3:]
con = sqlite3.connect(db, timeout=10)
q = 'select key from settings where key in (%s)' % ','.join('?' * len(keys))
found = [r[0] for r in con.execute(q, keys)]
print('found  : ' + (', '.join(found) if found else '(none)'))
if mode != 'dry':
    con.executemany('delete from settings where key=?', [(k,) for k in found])
    con.commit()
    print('deleted: %d' % len(found))
con.close()
'@ | Set-Content -LiteralPath $tmp -Encoding ASCII
try {
    $mode = if ($DryRun) { 'dry' } else { 'apply' }
    & $py $tmp $db $mode @Removed
    if ($LASTEXITCODE -ne 0) { throw "python exited with $LASTEXITCODE" }
} finally {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}

if ($DryRun) {
    Write-Host ''
    Write-Host 're-run without -DryRun to delete them.'
} else {
    Write-Host ''
    Write-Host 'done. Refresh the panel (or restart ECHO) to see the four items disappear.'
}
