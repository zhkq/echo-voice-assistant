<#
.SYNOPSIS
  把「1-备份ECHO会议数据」备份出来的会议数据还原进（新装的）ECHO。

.DESCRIPTION
  - 默认「只还原会议数据」：会议、说话人、转写行、纪要记录、会议会话、声纹
    写进新装的数据库，**保留新装的设置**（设备、热键、后端配对等都是新的）
  - -Mode Full：整库替换（旧设置 / 旧会话 / 旧指令历史一起回来）
  - 同时把 meeting\ 会议目录（录音 + transcript/summary/note 文件）合并回去
  - 自动停 ECHO、还原、再启动，并核对会议场数

.PARAMETER Root
  新装 ECHO 的安装目录（一般自动探测）

.PARAMETER BackupPath
  备份目录（默认自动找最新的 ECHO-backup\ECHO-*）

.PARAMETER Mode
  Meetings（默认）或 Full

.PARAMETER Replace
  新库里已经有会议数据时，允许覆盖（先删后灌）

.PARAMETER DryRun
  只打印计划，不动任何数据

.PARAMETER NoStart
  还原完不自动启动 ECHO

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "3-还原ECHO会议数据.ps1" -DryRun

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "3-还原ECHO会议数据.ps1" -Mode Full
#>
param(
    [string]$Root = '',
    [string]$BackupPath = '',
    [ValidateSet('Meetings', 'Full')]
    [string]$Mode = 'Meetings',
    [switch]$Replace,
    [switch]$DryRun,
    [switch]$NoStart
)

$scriptDir = $PSScriptRoot
. (Join-Path $scriptDir '_echo-common.ps1')

Write-Head ('ECHO 会议数据还原（' + $(if ($Mode -eq 'Full') { '整库替换' } else { '只还原会议数据' }) + '）')
if ($DryRun) { Write-Warn '当前是 DRY-RUN 模式：只打印计划，不写入。' }

# ---------------------------------------------------------------- 1. 新装在哪
$base = Find-EchoBase $Root
if (-not $base) {
    Write-Fail '没有找到新装的 ECHO。'
    Write-Step '请先装好 ECHO（双击交付包里的 装我.cmd），或用 -Root 指定安装目录。'
    exit 2
}
$L = Get-EchoLayout $base
if (-not $L.Code) {
    Write-Fail ('该目录不像 ECHO 安装目录（找不到 echo-core\app\main.py）：' + $L.Base)
    exit 2
}
Write-Note ('新装目录   ' + $L.Base)
Write-Note ('程序目录   ' + $L.Code + '    (' + $L.Layout + ')')
Write-Note ('数据目录   ' + $L.Data)
Write-Note ('会议目录   ' + $L.Meetings)
if ($L.ServiceUp) { Write-Note ('服务状态   运行中 · 端口 ' + $L.Port + ' · 版本 ' + $L.Version) } else { Write-Note '服务状态   未运行' }

$py = Get-EchoPython $L.Base
$tool = Join-Path $scriptDir 'py\echo_db_tools.py'
if (-not $py) { Write-Fail '新装里没找到 Python（runtime-core / venv），无法还原数据库。'; exit 2 }
if (-not (Test-Path -LiteralPath $tool)) { Write-Fail ('缺少工具脚本：' + $tool); exit 2 }

# ---------------------------------------------------------------- 2. 备份在哪
$backup = $BackupPath
if (-not $backup) { $backup = Get-NewestBackup $L.Base }
if (-not $backup -or -not (Test-Path -LiteralPath $backup)) {
    Write-Fail '没有找到会议数据备份（ECHO-backup\ECHO-*）。'
    Write-Step '请用 -BackupPath 指定，例如：-BackupPath "D:\ECHO-backup\ECHO-20261102-103000"'
    exit 3
}
$backup = ([string]$backup).TrimEnd('\')
$man = Read-JsonFile (Join-Path $backup 'manifest.json')
$bakDb = Join-Path $backup 'echo.db'
if (-not $man) { Write-Fail ('备份目录里没有 manifest.json：' + $backup); exit 3 }
if (-not (Test-Path -LiteralPath $bakDb)) { Write-Fail ('备份里没有 echo.db：' + $bakDb); exit 3 }
$bakMeetingsSrc = Join-Path $backup 'meeting'
Write-Section '备份信息'
Write-Note ('备份目录   ' + $backup)
Write-Note ('备份时间   ' + $man.createdAt)
Write-Note ('来源安装   ' + $man.echo.base + '（' + $man.echo.layout + '，版本 ' + $man.echo.version + '）')
Write-Note ('数据库     ' + $man.database.meetings + ' 场 / 转写行 ' + $man.database.lines + ' / 完整性 ' + $man.database.integrity + ' / schema v' + $man.database.schemaVersion)
Write-Note ('会议目录   ' + $man.meetings.dirs + ' 个 / ' + $man.meetings.files + ' 个文件 / ' + (Format-Size ([double]$man.meetings.bytes)))
if (Test-Path -LiteralPath $bakMeetingsSrc) {
    $bakDirCount = @(Get-ChildItem -LiteralPath $bakMeetingsSrc -Directory -ErrorAction SilentlyContinue).Count
    if ($bakDirCount -ne [int]$man.meetings.dirs) {
        Write-Note ('   （备份目录里实际有 ' + $bakDirCount + ' 个会议目录；清单记的是备份当时的数量，以数据库为准）')
    }
}
if (-not $man.meetings.audioIncluded) { Write-Warn '该备份没有包含录音。' }
if (-not (Test-Path -LiteralPath $bakMeetingsSrc)) { Write-Warn ('备份里没有 meeting 目录，只会还原数据库：' + $bakMeetingsSrc) }

$targetDb = Join-Path $L.Data 'echo.db'

# 正在录音/转写时不许还原（DRY-RUN 只预览计划，不受此限）
if ($L.ServiceUp) {
    $stNow = Get-EchoStatus $L.Base
    if ($stNow -and (($stNow.meeting -and $stNow.meeting.active) -or $stNow.busy)) {
        if ($DryRun) {
            Write-Warn 'ECHO 正在录音或转写：DRY-RUN 只预览计划；真正还原会被拒绝。'
        } else {
            Write-Fail 'ECHO 正在录音或转写，还原要停服务、会丢数据。请先结束录音/等转写完成。'
            exit 3
        }
    }
}

Write-Section '还原计划'
if ($Mode -eq 'Full') {
    Write-Note ('1) 用备份 echo.db 整体替换 ' + $targetDb)
    Write-Note '   （旧设置、旧会话、旧指令历史都会回来；新装的设置被覆盖）'
} else {
    Write-Note ('1) 把会议相关 7 张表合并进 ' + $targetDb)
    Write-Note '   （meetings / speakers / lines / summary_runs / meeting_sessions / voiceprints / speaker_embeddings）'
    Write-Note '   新装里的设置、指令历史、日志保持不动'
}
Write-Note ('2) 合并会议目录 -> ' + $L.Meetings)
Write-Note '3) 启动 ECHO 并核对会议场数'

if ($DryRun) {
    Write-Host ''
    Write-Ok 'DRY-RUN 结束：没有写入任何数据。确认无误后去掉 -DryRun 再跑一次。'
    exit 0
}

# ---------------------------------------------------------------- 3. 停服务
Write-Section '停止 ECHO'
$killed = Stop-EchoAll $L.Base
if ($killed.Count -gt 0) { Write-Ok ('已结束进程：' + ($killed -join '、')) }
if (Wait-EchoDown $L.Base 30) { Write-Ok '服务已停止（数据库已解锁）' } else { Write-Warn '服务仍在响应，还原可能失败' }

# 数据目录 / 数据库兜底：新装若还没建库，先启动一次让它建库
if (-not (Test-Path -LiteralPath $targetDb)) {
    Write-Warn '新装还没生成数据库，先启动一次 ECHO 建库…'
    try {
        Start-EchoDetached $L.Code
        $st = Wait-EchoUp $L.Base 150
        if (-not $st) { Write-Fail 'ECHO 启动超时，无法建库。可先手工打开一次 ECHO 再重跑本脚本。'; exit 4 }
        [void](Stop-EchoAll $L.Base)
        [void](Wait-EchoDown $L.Base 30)
    } catch {
        Write-Fail ('启动失败：' + $_.Exception.Message)
        exit 4
    }
    if (-not (Test-Path -LiteralPath $targetDb)) { Write-Fail ('仍然没有数据库：' + $targetDb); exit 4 }
    Write-Ok '新库已生成'
}

# ---------------------------------------------------------------- 4. 还原数据库
Write-Section '还原数据库'
$restoreSummary = Join-Path $backup 'restore-summary.json'
if ($Mode -eq 'Full') {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $old = $targetDb + '.before-restore-' + $stamp
    try {
        Copy-Item -LiteralPath $targetDb -Destination $old -Force
        Write-Note ('新装的空库已备份到：' + $old)
    } catch { Write-Warn ('备份新库失败（继续）：' + $_.Exception.Message) }
    # 拷库时把 -wal/-shm 清掉，避免新旧日志混用
    foreach ($suffix in @('-wal', '-shm')) {
        $p = $targetDb + $suffix
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue }
    }
    Copy-Item -LiteralPath $bakDb -Destination $targetDb -Force
    try {
        $null = Invoke-EchoPython $py @($tool, 'counts', '--db', $targetDb, '--summary', $restoreSummary)
    } catch {
        Write-Fail ('还原后的库打不开：' + $_.Exception.Message)
        exit 4
    }
    Write-Ok '数据库已整体替换'
} else {
    $mergeArgs = @($tool, 'merge', '--backup-db', $bakDb, '--target-db', $targetDb, '--summary', $restoreSummary)
    if ($Replace) { $mergeArgs += '--replace' }
    try {
        $null = Invoke-EchoPython $py $mergeArgs
    } catch {
        $msg = $_.Exception.Message
        Write-Fail ('合并数据库失败：' + $msg)
        $rrFail = Read-JsonFile $restoreSummary
        if ($rrFail -and $rrFail.conflicts) {
            Write-Note '新库里已有的数据：'
            foreach ($prop in $rrFail.conflicts.PSObject.Properties) {
                Write-Note ('   ' + $prop.Name + ' 已有 ' + $prop.Value + ' 行')
            }
        }
        if ($msg -match 'target-not-empty|already has data') {
            Write-Step '新库里已经有会议数据了。确认要用备份覆盖，请加 -Replace 重跑。'
        }
        exit 4
    }
    $rr = Read-JsonFile $restoreSummary
    if ($rr -and -not $rr.ok) {
        Write-Fail ('合并被拒绝：' + ($rr.error | Out-String))
        if ($rr.conflicts) {
            foreach ($prop in $rr.conflicts.PSObject.Properties) { Write-Note ('   ' + $prop.Name + ' 已有 ' + $prop.Value + ' 行') }
        }
        Write-Step '加 -Replace 可覆盖（先删后灌）。'
        exit 4
    }
    Write-Ok ('会议相关表已合并（会议 ' + $rr.tables.meetings + ' 场 / 转写行 ' + $rr.tables.lines + ' / 完整性 ' + $rr.integrity + '）')
}

# ---------------------------------------------------------------- 5. 还原会议目录
Write-Section '还原会议目录'
if (Test-Path -LiteralPath $bakMeetingsSrc) {
    New-Item -ItemType Directory -Force -Path $L.Meetings | Out-Null
    if (-not (Test-Path -LiteralPath $L.Meetings)) { Write-Fail ('建不了会议目录：' + $L.Meetings); exit 4 }
    # AGENTS.md 是新装的工作区说明，保留新的，不覆盖
    $rcArgs = @($bakMeetingsSrc, $L.Meetings, '/E', '/R:2', '/W:2', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/XF', 'AGENTS.md')
    $rc = 0
    try { $null = & robocopy @rcArgs 2>&1; $rc = $LASTEXITCODE } catch { $rc = 16 }
    if ($rc -ge 8) { Write-Fail ('复制会议目录失败（robocopy 退出码 ' + $rc + '）'); exit 4 }
    $stats = Get-PathStats $L.Meetings
    Write-Ok ('会议目录已合并：' + $stats.Dirs + ' 个目录 / ' + $stats.Files + ' 个文件 / ' + (Format-Size $stats.Bytes))
} else {
    Write-Warn '备份里没有 meeting 目录，跳过。'
}

$bakDeleted = Join-Path $backup 'deleted-meetings'
if (Test-Path -LiteralPath $bakDeleted) {
    $dstDeleted = Join-Path $L.Data 'deleted-meetings'
    $null = & robocopy $bakDeleted $dstDeleted /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP 2>&1
    Write-Step '已还原 data\deleted-meetings（历史删除的会议）'
}

# ---------------------------------------------------------------- 6. 校验
Write-Section '校验'
$finalCounts = Join-Path $backup 'restore-final-counts.json'
try {
    $null = Invoke-EchoPython $py @($tool, 'counts', '--db', $targetDb, '--summary', $finalCounts)
} catch { Write-Warn ('读取还原结果失败：' + $_.Exception.Message) }
$fc = Read-JsonFile $finalCounts
$finalMeetings = -1
$finalLines = -1
if ($fc) {
    $finalMeetings = [int]$fc.tables.meetings
    $finalLines = [int]$fc.tables.lines
    Write-Note ('还原后数据库：会议 ' + $finalMeetings + ' 场 / 转写行 ' + $finalLines + ' / 完整性 ' + $fc.integrity)
    if ($finalMeetings -eq [int]$man.database.meetings) {
        Write-Ok ('会议场数与备份一致（' + $finalMeetings + '）')
    } else {
        Write-Warn ('会议场数与备份不一致：备份 ' + $man.database.meetings + ' / 现在 ' + $finalMeetings)
    }
} else {
    Write-Warn '拿不到还原后的统计，请启动 ECHO 后在面板里核对。'
}

# ---------------------------------------------------------------- 7. 启动
$apiMeetings = -1
if (-not $NoStart) {
    Write-Section '启动 ECHO'
    try {
        Start-EchoDetached $L.Code
        $st = Wait-EchoUp $L.Base 150
        if ($st) {
            Write-Ok ('ECHO 已启动：' + 'http://127.0.0.1:' + (Get-EchoPort $L.Base) + '/')
            $apiMeetings = Get-EchoMeetingCountFromApi $L.Base
            if ($apiMeetings -ge 0) {
                Write-Note ('面板 API 里看到的会议数：' + $apiMeetings)
                if ([int]$man.database.meetings -eq $apiMeetings) { Write-Ok 'API 与备份一致' } else { Write-Warn 'API 会议数与备份不一致，请在面板抽查一两场。' }
            }
        } else {
            Write-Warn 'ECHO 启动超时，请手工双击桌面快捷方式或 scripts\start.ps1 -Background。'
        }
    } catch {
        Write-Warn ('启动失败：' + $_.Exception.Message)
    }
}

# ---------------------------------------------------------------- 8. 记录
$record = @(
    'ECHO 会议数据还原记录 ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'),
    '新装目录：' + $L.Base + '（版本 ' + $L.Version + '）',
    '备份目录：' + $backup,
    '模式：' + $Mode + $(if ($Replace) { '（覆盖已有数据）' } else { '' }),
    '还原后会议数：' + $finalMeetings + '（备份 ' + $man.database.meetings + '）',
    '还原后转写行：' + $finalLines + '（备份 ' + $man.database.lines + '）',
    'API 会议数：' + $apiMeetings,
    '会议目录：' + $L.Meetings
) -join [Environment]::NewLine
$recordPath = Join-Path $scriptDir ('还原记录-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt')
Write-Utf8NoBom $recordPath $record
try { Write-Utf8NoBom (Join-Path $backup ('还原记录-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt')) $record } catch { }

Write-Host ''
Write-Ok '会议数据还原完成。'
Write-Note ('还原记录：' + $recordPath)
Write-Note '建议：打开面板抽查 1-2 场会议（录音能否播放、转写是否完整、纪要是否在）。'
exit 0
