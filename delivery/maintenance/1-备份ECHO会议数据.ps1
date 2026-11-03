<#
.SYNOPSIS
  备份 ECHO 的「会议数据」（录音 / 转写 / 纪要 / 数据库），给清理与重装打底。

.DESCRIPTION
  - 数据库用 SQLite 在线备份接口取一致性快照（ECHO 不用停，WAL 也不会丢数据）
  - 会议目录整目录复制（默认含录音；加 -NoAudio 则只备数据库与文本，体积小很多）
  - 另导出各表 JSONL、settings.json，并写 manifest.json 与《恢复说明.md》
  - 默认备份到安装目录同级：<盘>\ECHO-backup\ECHO-<时间戳>\

.PARAMETER Root
  ECHO 安装目录（一般自动探测，跨盘/自定义目录时再传）

.PARAMETER Dest
  备份目录（默认 <盘>\ECHO-backup\ECHO-<时间戳>）

.PARAMETER NoAudio
  不备份录音（.flac/.wav 等），只备份数据库、转写文本、纪要

.PARAMETER NoTableDump
  不导出各表 JSONL（只留数据库快照）

.PARAMETER Force
  正在录音/转写时也强行备份（可能拿到半场会议的音频，不推荐）

.PARAMETER NoRestart
  仅在「找不到 Python、需要停机复制数据库」时生效：复制完不自动重启 ECHO

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "1-备份ECHO会议数据.ps1"

.EXAMPLE
  ... -Dest "E:\ECHO-会议数据备份" -NoAudio
#>
param(
    [string]$Root = '',
    [string]$Dest = '',
    [switch]$NoAudio,
    [switch]$NoTableDump,
    [switch]$Force,
    [switch]$NoRestart
)

$scriptDir = $PSScriptRoot
. (Join-Path $scriptDir '_echo-common.ps1')

Write-Head 'ECHO 会议数据备份'

# ---------------------------------------------------------------- 1. 找安装
$base = Find-EchoBase $Root
if (-not $base) {
    Write-Fail '没有找到 ECHO 安装目录。'
    Write-Step '请用 -Root 指定，例如：-Root "D:\ECHO"'
    exit 2
}
$L = Get-EchoLayout $base
if (-not $L.Code) {
    Write-Fail ('该目录不像 ECHO 安装目录（找不到 echo-core\app\main.py）：' + $L.Base)
    exit 2
}

Write-Note ('安装目录   ' + $L.Base)
Write-Note ('程序目录   ' + $L.Code + '    (' + $L.Layout + ')')
Write-Note ('数据目录   ' + $L.Data)
Write-Note ('会议目录   ' + $L.Meetings)
if ($L.ServiceUp) {
    Write-Note ('服务状态   运行中 · 端口 ' + $L.Port + ' · 版本 ' + $L.Version)
} else {
    Write-Note '服务状态   未运行（备份不受影响）'
}

$db = Join-Path $L.Data 'echo.db'
if (-not (Test-Path -LiteralPath $db)) {
    Write-Fail ('找不到数据库文件：' + $db)
    exit 2
}

# ---------------------------------------------------------------- 2. 录音保护
$meetingActive = $false
if ($L.ServiceUp) {
    $st = Get-EchoStatus $L.Base
    if ($st) {
        if ($st.meeting -and $st.meeting.active) { $meetingActive = $true }
        if ($st.busy) { $meetingActive = $true }
    }
}
if ($meetingActive) {
    if (-not $Force) {
        Write-Fail 'ECHO 正在录音或转写中，现在备份可能只拿到半场会议的音频。'
        Write-Step '请先结束录音、等转写完成后重跑；确有需要可加 -Force。'
        exit 3
    }
    Write-Warn '正在录音/转写：按 -Force 继续（音频可能不完整）。'
}

# ---------------------------------------------------------------- 3. 备份目录
if (-not $Dest) {
    $parent = Split-Path -Parent $L.Base
    if (-not $parent) { $parent = $L.Base }
    $Dest = Join-Path $parent ('ECHO-backup\ECHO-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$Dest = ([string]$Dest).Trim().Trim('"').TrimEnd('\')
if ($Dest -eq $L.Base -or $Dest.StartsWith($L.Base + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Fail '备份目录不能放在 ECHO 安装目录里面（清理时会被一起删掉）。'
    Write-Step ('请换一个目录，例如：' + (Join-Path (Split-Path -Parent $L.Base) ('ECHO-backup\ECHO-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))))
    exit 2
}

# 空间检查
$srcStats = Get-PathStats $L.Meetings -NoAudio:$NoAudio
$need = [double]$srcStats.Bytes + (Get-Item -LiteralPath $db).Length + 300MB
$qual = Split-Path -Qualifier $Dest
if ($qual) {
    $driveName = $qual.TrimEnd(':')
    $drv = Get-PSDrive -Name $driveName -ErrorAction SilentlyContinue
    if ($drv) {
        if ($drv.Free -lt ($need * 1.05)) {
            Write-Fail ('目标盘空间不足：预计需要 ' + (Format-Size ($need * 1.05)) + '，可用 ' + (Format-Size $drv.Free))
            exit 2
        }
        Write-Step ('目标盘可用 ' + (Format-Size $drv.Free) + '，预计需要 ' + (Format-Size ($need * 1.05)))
    }
}

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Write-Section '开始备份'
Write-Note ('备份目录   ' + $Dest)

# ---------------------------------------------------------------- 4. 数据库快照
$py = Get-EchoPython $L.Base
$tool = Join-Path $scriptDir 'py\echo_db_tools.py'
$outDb = Join-Path $Dest 'echo.db'
$dbSummaryPath = Join-Path $Dest 'db-summary.json'
$tablesDir = ''
if (-not $NoTableDump) { $tablesDir = Join-Path $Dest 'db-tables' }

if (-not $py) {
    Write-Warn '本机没找到 Python：改为「停服务 → 复制数据库 → 再启动」的保守方式。'
    $null = Stop-EchoAll $L.Base
    if (-not (Wait-EchoDown $L.Base 30)) { Write-Fail 'ECHO 停不下来，为避免坏库已中止。'; exit 4 }
    Copy-Item -LiteralPath $db -Destination $outDb -Force
    $wal = $db + '-wal'; $shm = $db + '-shm'
    if (Test-Path -LiteralPath $wal) { Copy-Item -LiteralPath $wal -Destination ($outDb + '-wal') -Force }
    if (Test-Path -LiteralPath $shm) { Copy-Item -LiteralPath $shm -Destination ($outDb + '-shm') -Force }
    if (-not $NoRestart) {
        try { Start-EchoDetached $L.Code; [void](Wait-EchoUp $L.Base 90) } catch { }
    }
    Write-Ok ('数据库已复制（' + (Format-Size (Get-Item -LiteralPath $outDb).Length) + '）')
} else {
    if (-not (Test-Path -LiteralPath $tool)) { Write-Fail ('缺少工具脚本：' + $tool); exit 2 }
    Write-Step '数据库在线快照（不用停 ECHO）…'
    try {
        $pyArgs = @($tool, 'backup', '--db', $db, '--out-db', $outDb, '--summary', $dbSummaryPath)
        if ($tablesDir) { $pyArgs += @('--tables-dir', $tablesDir) }
        $null = Invoke-EchoPython $py $pyArgs
    } catch {
        Write-Fail ('数据库快照失败：' + $_.Exception.Message)
        exit 4
    }
    $dbInfo = Read-JsonFile $dbSummaryPath
    $integText = '未知'
    $meetText = '?'
    if ($dbInfo) {
        $integText = [string]$dbInfo.integrity
        $meetText = [string]$dbInfo.tables.meetings
    }
    $dbSizeText = Format-Size (Get-Item -LiteralPath $outDb).Length
    Write-Ok ('数据库快照完成：' + $dbSizeText + '，完整性 ' + $integText + '，会议 ' + $meetText + ' 场')
}

# ---------------------------------------------------------------- 5. 会议目录
$dstMeetings = Join-Path $Dest 'meeting'
if (Test-Path -LiteralPath $L.Meetings) {
    Write-Step ('复制会议目录（' + (Format-Size $srcStats.Bytes) + '，' + $srcStats.Dirs + ' 个目录）…')
    New-Item -ItemType Directory -Force -Path $dstMeetings | Out-Null
    $rcArgs = @($L.Meetings, $dstMeetings, '/E', '/R:2', '/W:2', '/NFL', '/NDL', '/NJH', '/NJS', '/NP')
    if ($NoAudio) { $rcArgs += @('/XF', '*.flac', '*.wav', '*.m4a', '*.mp3', '*.ogg', '*.aac') }
    $rc = 0
    try { $null = & robocopy @rcArgs 2>&1; $rc = $LASTEXITCODE } catch { $rc = 16 }
    if ($rc -ge 8) {
        Write-Fail ('复制会议目录失败（robocopy 退出码 ' + $rc + '）')
        exit 4
    }
    Write-Ok '会议目录复制完成'
} else {
    Write-Warn ('没有找到会议目录（' + $L.Meetings + '），跳过。')
}

# 已删除会议的回收站（如果有）
$deletedSrc = Join-Path $L.Data 'deleted-meetings'
if (Test-Path -LiteralPath $deletedSrc) {
    $deletedDst = Join-Path $Dest 'deleted-meetings'
    $null = & robocopy $deletedSrc $deletedDst /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP 2>&1
    Write-Step '已备份 data\deleted-meetings（历史删除的会议）'
}

# 小配置（配对/身份；重装后可选恢复）
$cfgDir = Join-Path $Dest 'config'
foreach ($f in @('backend.json', 'echo-e2e-identity.json', 'echo-e2e-devices.json')) {
    $src = Join-Path $L.Data $f
    if (Test-Path -LiteralPath $src) {
        if (-not (Test-Path -LiteralPath $cfgDir)) { New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null }
        Copy-Item -LiteralPath $src -Destination (Join-Path $cfgDir $f) -Force
    }
}

# ---------------------------------------------------------------- 6. 校验
Write-Section '校验'
$dstStats = Get-PathStats $dstMeetings -NoAudio:$NoAudio
Write-Note ('源：' + $srcStats.Dirs + ' 目录 / ' + $srcStats.Files + ' 文件 / ' + (Format-Size $srcStats.Bytes))
Write-Note ('备份：' + $dstStats.Dirs + ' 目录 / ' + $dstStats.Files + ' 文件 / ' + (Format-Size $dstStats.Bytes))
$dirFail = ($srcStats.Dirs -ne $dstStats.Dirs)
$fileFail = ($srcStats.Files -ne $dstStats.Files)
if ($dirFail -or $fileFail) {
    Write-Warn '目录/文件数与源不一致——请检查上面的 robocopy 输出或磁盘空间。'
} else {
    Write-Ok '文件数一致'
}
$dbMeetings = -1
$dbLines = -1
$schemaVer = ''
$integrity = ''
$dbHash = ''
if (Test-Path -LiteralPath $dbSummaryPath) {
    $dbInfo = Read-JsonFile $dbSummaryPath
    if ($dbInfo) {
        $dbMeetings = [int]$dbInfo.tables.meetings
        $dbLines = [int]$dbInfo.tables.lines
        $schemaVer = [string]$dbInfo.schema_version
        $integrity = [string]$dbInfo.integrity
    }
} elseif ($py) {
    $cntSummary = Join-Path $Dest 'db-counts.json'
    try {
        $null = Invoke-EchoPython $py @($tool, 'counts', '--db', $outDb, '--summary', $cntSummary)
        $cnt = Read-JsonFile $cntSummary
        if ($cnt) { $dbMeetings = [int]$cnt.tables.meetings; $dbLines = [int]$cnt.tables.lines; $schemaVer = [string]$cnt.schema_version; $integrity = [string]$cnt.integrity }
    } catch { }
}
if (Test-Path -LiteralPath $outDb) { $dbHash = (Get-FileHash -LiteralPath $outDb -Algorithm SHA256).Hash }
Write-Note ('数据库：会议 ' + $dbMeetings + ' 场 / 转写行 ' + $dbLines + ' / schema v' + $schemaVer + ' / 完整性 ' + $integrity)
if ($dbMeetings -ge 0 -and $dstStats.Dirs -gt 0 -and $dbMeetings -ne $dstStats.Dirs) {
    Write-Warn ('数据库里的会议数（' + $dbMeetings + '）与会议目录数（' + $dstStats.Dirs + '）不一致：多半是备份期间正好开始/结束了会议。')
    Write-Step '建议等这场会议结束后**再跑一次备份**（会生成新的时间戳目录，清理脚本自动用最新的那份）。'
}

# ---------------------------------------------------------------- 7. 清单与说明
$manifest = [ordered]@{
    schema     = 'echo-backup/1'
    createdAt  = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    echo       = [ordered]@{ base = $L.Base; code = $L.Code; data = $L.Data; layout = $L.Layout; version = $L.Version; port = $L.Port }
    database   = [ordered]@{ file = 'echo.db'; bytes = $(if (Test-Path -LiteralPath $outDb) { (Get-Item -LiteralPath $outDb).Length } else { 0 }); sha256 = $dbHash; integrity = $integrity; schemaVersion = $schemaVer; meetings = $dbMeetings; lines = $dbLines }
    meetings   = [ordered]@{ source = $L.Meetings; folder = 'meeting'; dirs = $dstStats.Dirs; files = $dstStats.Files; bytes = $dstStats.Bytes; audioIncluded = (-not $NoAudio) }
    tableDumps = $(if ($tablesDir) { 'db-tables' } else { '' })
    deletedMeetings = $(if (Test-Path -LiteralPath (Join-Path $Dest 'deleted-meetings')) { 'deleted-meetings' } else { '' })
}
Write-Utf8NoBom (Join-Path $Dest 'manifest.json') ($manifest | ConvertTo-Json -Depth 6)

$readme = @"
# ECHO 会议数据备份

- 备份时间：$($manifest.createdAt)
- 来源安装：$($L.Base)（$($L.Layout)，版本 $($L.Version)）
- 来源会议目录：$($L.Meetings)
- 数据库：echo.db（schema v$schemaVer，完整性 $integrity，会议 $dbMeetings 场，转写行 $dbLines）
- 会议目录：meeting\（$($dstStats.Dirs) 个会议目录，$($dstStats.Files) 个文件，$(Format-Size $dstStats.Bytes)）
- 录音：$(if ($NoAudio) { '未备份（-NoAudio）' } else { '已备份（.flac/.wav 等）' })

## 里面有什么

| 路径 | 说明 |
|---|---|
| echo.db | 会议数据库快照（会议 / 说话人 / 转写行 / 纪要记录 / 声纹） |
| meeting\ | 每场会议一个目录：录音分段、meta.json、transcript.md、summary.md、topics.md、meeting_note.md |
| db-tables\ | 各表 JSONL 与 settings.json，纯文本，便于人工核对 |
| deleted-meetings\ | 历史删除过的会议（有才存在） |
| config\ | backend.json 等小配置（可选恢复，一般不用） |
| manifest.json | 本备份的清单（数量、大小、哈希） |

## 怎么还原

1. 先重装 ECHO（双击交付包里的 装我.cmd，装到原目录或新目录都行）。
2. 双击本目录同级的 3-还原ECHO会议数据.cmd。
   - 默认「只还原会议数据」：会议、说话人、转写行、纪要、声纹，保留新装的设置；
   - 加 -Mode Full 则整库替换（连旧设置、旧会话一起回来）。
3. 还原脚本会自动停/起 ECHO，并核对会议场数。

## 注意

- 这份备份在清理 ECHO 时**不会**被删（它在本安装目录之外）；但建议复制到另一块盘或网盘。
- 录音体积大（这里 $(Format-Size $dstStats.Bytes)），如只需要纪要/转写文本，下次备份可加 -NoAudio。
- 笔记库（Obsidian）里归档的纪要属于笔记库，不在这个备份里，清理 ECHO 不会动它们。
"@
Write-Utf8NoBom (Join-Path $Dest '恢复说明.md') $readme

# ---------------------------------------------------------------- 8. 汇总
Write-Section '备份完成'
Write-Note ('备份目录   ' + $Dest)
Write-Note ('数据库     ' + (Format-Size $manifest.database.bytes) + '（会议 ' + $dbMeetings + ' 场）')
Write-Note ('会议目录   ' + $dstStats.Dirs + ' 个 / ' + $dstStats.Files + ' 个文件 / ' + (Format-Size $dstStats.Bytes))
Write-Note '建议把整个备份目录复制到另一块盘（清理只删 ECHO 安装目录，不会删备份）。'
Write-Host ''
Write-Note '下一步：双击 2-清理ECHO.cmd 清理，再装 ECHO，最后双击 3-还原ECHO会议数据.cmd。'
exit 0
