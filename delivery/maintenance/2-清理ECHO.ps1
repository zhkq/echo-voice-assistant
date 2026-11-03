<#
.SYNOPSIS
  把 ECHO 从这台机器上清理干净（保留安装目录之外的备份），用于重装前的彻底清理。

.DESCRIPTION
  会做这些事（每一步都会先打印、再执行）：
    1. 检查「会议数据备份」是否已存在（没有备份默认拒绝清理）
    2. 停掉 ECHO 及其看门狗 / 边条 / 失败转移代理 / 切换器
    3. 删掉开机自启、开始菜单 / 桌面快捷方式、计划任务、防火墙规则、注册表里的 ECHO 登记
    4. 删掉安装目录、%LOCALAPPDATA%\ECHO、%LOCALAPPDATA%\echo-sidebar、.echo-switch 等
    5. 写一份《清理记录》，并提示下一步（重装 + 还原会议数据）

  默认不碰：C:\echo-dev 源码树、桌面\ECHO 工具箱、D:\ECHO-delivery 安装包、D:\ECHO-backup 备份。
  这些要用 -IncludeDev / -IncludeSourceTree 显式打开。

.PARAMETER Root
  ECHO 安装目录（一般自动探测）

.PARAMETER BackupPath
  指定用于校验的备份目录（默认自动找最新的 ECHO-backup\ECHO-*）

.PARAMETER DryRun
  只列清单，不删任何东西（第一次务必先跑这个）

.PARAMETER Yes
  跳过交互确认（用于脚本化）

.PARAMETER Force
  即使没找到可用备份也继续清理（不建议；请先跑备份脚本）

.PARAMETER IncludeDev
  同时清理开发残留：%USERPROFILE%\.echo-test、.echo-tls、echo-backups、桌面\ECHO 工具箱、
  C:\echo-dev 相关的计划任务

.PARAMETER IncludeSourceTree
  连同 C:\echo-dev（git 源码树）一起删——**危险**，只在确定不再需要源码时使用

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "2-清理ECHO.ps1" -DryRun

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "2-清理ECHO.ps1"
#>
param(
    [string]$Root = '',
    [string]$BackupPath = '',
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$Force,
    [switch]$IncludeDev,
    [switch]$IncludeSourceTree
)

$scriptDir = $PSScriptRoot
. (Join-Path $scriptDir '_echo-common.ps1')

Write-Head 'ECHO 清理（重装前）'
if ($DryRun) { Write-Warn '当前是 DRY-RUN 模式：只打印清单，不删除任何东西。' }

# ---------------------------------------------------------------- 1. 找安装
$base = Find-EchoBase $Root
if (-not $base) {
    Write-Fail '没有找到 ECHO 安装目录。请用 -Root 指定，例如：-Root "D:\ECHO"'
    exit 2
}
$L = Get-EchoLayout $base
if (-not $L.Code) {
    Write-Fail ('该目录不像 ECHO 安装目录（找不到 echo-core\app\main.py）：' + $L.Base)
    Write-Step '确实要删可加 -Force，但请先确认路径。'
    if (-not $Force) { exit 2 }
}
Write-Note ('安装目录   ' + $L.Base)
Write-Note ('程序目录   ' + $L.Code)
Write-Note ('数据目录   ' + $L.Data)
Write-Note ('会议目录   ' + $L.Meetings)
if ($L.ServiceUp) { Write-Note ('服务状态   运行中 · 端口 ' + $L.Port + ' · 版本 ' + $L.Version) } else { Write-Note '服务状态   未运行' }

# ---------------------------------------------------------------- 2. 安全护栏
$badTargets = @(
    ([System.Environment]::GetFolderPath('UserProfile')),
    $env:SystemDrive + '\',
    ($env:SystemDrive + '\Windows'),
    ($env:SystemDrive + '\Program Files'),
    ($env:SystemDrive + '\Program Files (x86)')
)
if ($base -match '^[A-Za-z]:$') { Write-Fail '安装目录不能是盘符根目录。'; exit 2 }
foreach ($b in $badTargets) {
    if ($b -and ($base.TrimEnd('\') -ieq ([string]$b).TrimEnd('\'))) { Write-Fail ('拒绝清理系统/用户目录：' + $base); exit 2 }
}
if ($scriptDir.TrimEnd('\').StartsWith($base.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Fail '本脚本就在 ECHO 安装目录里面，清理时会把脚本自己删掉。'
    Write-Step '请先把整个「ECHO-运维」目录移到安装目录之外（例如 D:\ECHO-delivery\）。'
    exit 2
}

# 正在录音/转写时不许真删（DRY-RUN 只预览清单，不受此限）
if ($L.ServiceUp) {
    $st = Get-EchoStatus $L.Base
    if ($st -and (($st.meeting -and $st.meeting.active) -or $st.busy)) {
        if ($DryRun) {
            Write-Warn 'ECHO 正在录音或转写：DRY-RUN 只预览清单；真正执行会被拒绝，请等录音/转写结束。'
        } else {
            Write-Fail 'ECHO 正在录音或转写，现在清理会丢数据。请先结束录音/等转写完成。'
            exit 3
        }
    }
}

# ---------------------------------------------------------------- 3. 备份前置校验
$backup = $BackupPath
if (-not $backup) { $backup = Get-NewestBackup $L.Base }
$backupOk = $false
$backupMeetings = -1
if ($backup -and (Test-Path -LiteralPath $backup)) {
    $man = Read-JsonFile (Join-Path $backup 'manifest.json')
    $bakDb = Join-Path $backup 'echo.db'
    if ($man -and (Test-Path -LiteralPath $bakDb)) {
        $backupMeetings = [int]$man.database.meetings
        $backupOk = ($backupMeetings -gt 0)
        Write-Ok ('找到会议数据备份：' + $backup)
        Write-Note ('   备份时间 ' + $man.createdAt + ' / 数据库会议 ' + $backupMeetings + ' 场 / 会议目录 ' + $man.meetings.dirs + ' 个')
        if ($man.meetings.audioIncluded -eq $false) {
            Write-Warn '该备份没有包含录音（-NoAudio 备的）。确认不需要录音再继续。'
        }
    }
}
if (-not $backupOk) {
    if (-not $Force) {
        if ($DryRun) {
            Write-Warn '没有找到可用备份：DRY-RUN 只预览清单；真正执行会被拒绝。'
        } else {
            Write-Fail '没有找到可用的会议数据备份，已中止。'
            Write-Step '请先双击 1-备份ECHO会议数据.cmd，完成后回来再清理。'
            Write-Step '（确实不需要备份时可加 -Force，但会议数据会永久丢失。）'
            exit 4
        }
    } else {
        Write-Warn '没有可用备份，但 -Force 已指定：继续清理。'
    }
}

# ---------------------------------------------------------------- 4. 建立清单
$planDirs = New-Object System.Collections.ArrayList
$planFiles = New-Object System.Collections.ArrayList
$planNotes = New-Object System.Collections.ArrayList

function Add-PlanDir([string]$Path, [string]$Label) {
    if (-not $Path) { return }
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $stats = Get-PathStats $Path
    [void]$planDirs.Add([pscustomobject]@{ Path = $Path.TrimEnd('\'); Label = $Label; Files = $stats.Files; Bytes = $stats.Bytes })
}
function Add-PlanFile([string]$Path, [string]$Label) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    $len = 0
    try { $len = (Get-Item -LiteralPath $Path).Length } catch { }
    [void]$planFiles.Add([pscustomobject]@{ Path = $Path; Label = $Label; Bytes = $len })
}

Add-PlanDir $L.Base 'ECHO 安装目录'
Add-PlanDir (Join-Path $env:LOCALAPPDATA 'ECHO') '本地缓存（DSH 视图）'
Add-PlanDir (Join-Path $env:LOCALAPPDATA 'echo-sidebar') '边条 WebView2 缓存'
Add-PlanDir (Join-Path $env:USERPROFILE '.echo-switch') '切换器 / 看门狗脚本'
Add-PlanFile (Join-Path $env:USERPROFILE '.echo-instances.json') '切换器实例登记'

# 快捷方式（启动文件夹 / 开始菜单 / 桌面）
$lnkFolders = @(
    ([System.Environment]::GetFolderPath('Startup')),
    ([System.Environment]::GetFolderPath('Programs')),
    ([System.Environment]::GetFolderPath('Desktop'))
)
$lnkCandidates = @()
foreach ($folder in $lnkFolders) {
    if (-not $folder -or -not (Test-Path -LiteralPath $folder)) { continue }
    foreach ($f in @(Get-ChildItem -LiteralPath $folder -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue)) {
        $lnkCandidates += [pscustomobject]@{ File = $f; Folder = $folder }
    }
    foreach ($f in @(Get-ChildItem -LiteralPath $folder -Filter '*.url' -Recurse -ErrorAction SilentlyContinue)) {
        $lnkCandidates += [pscustomobject]@{ File = $f; Folder = $folder }
    }
}
$seenLnk = @{}
$ws = $null
try { $ws = New-Object -ComObject WScript.Shell } catch { }
foreach ($item in $lnkCandidates) {
    $f = $item.File
    if ($seenLnk.ContainsKey($f.FullName)) { continue }
    $seenLnk[$f.FullName] = $true
    $target = ''
    if ($ws -and $f.Extension -ieq '.lnk') {
        try { $target = [string]($ws.CreateShortcut($f.FullName).TargetPath) } catch { }
    } else {
        try { $target = (Get-Content -LiteralPath $f.FullName -Raw -ErrorAction SilentlyContinue) } catch { }
    }
    $name = $f.Name
    $hit = $false
    if ($name -match 'ECHO') { $hit = $true }
    if ($target -and ($target.IndexOf($L.Base, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)) { $hit = $true }
    if ($target -and ($target -match '\.echo-switch')) { $hit = $true }
    if ($target -and ($target -match 'echo-core')) { $hit = $true }
    if ($hit) { Add-PlanFile $f.FullName ('快捷方式（' + $item.Folder + '）') }
}

# 计划任务 / 防火墙
$planTasks = @()
foreach ($t in (Get-ScheduledTask -ErrorAction SilentlyContinue)) {
    $actions = @($t.Actions)
    $related = $false
    foreach ($a in $actions) {
        $ex = [string]$a.Execute; $ar = [string]$a.Arguments
        if ($ex -and $ex.IndexOf($L.Base, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { $related = $true }
        if ($ar -and $ar.IndexOf($L.Base, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { $related = $true }
    }
    if ($t.TaskName -match '(?i)^ECHO' -and $IncludeDev) { $related = $true }
    if ($related) { $planTasks += $t.TaskName }
}
$planFirewall = @()
foreach ($r in (Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'ECHO' })) {
    $planFirewall += $r.DisplayName
}

# 可选：开发残留
if ($IncludeDev) {
    Add-PlanDir (Join-Path $env:USERPROFILE '.echo-test') '开发测试脚本'
    Add-PlanDir (Join-Path $env:USERPROFILE '.echo-tls') 'TLS/中继调试脚本'
    Add-PlanDir (Join-Path $env:USERPROFILE 'echo-backups') '旧备份目录'
    Add-PlanDir (Join-Path ([System.Environment]::GetFolderPath('Desktop')) 'ECHO') '桌面 ECHO 工具箱（含旧数据）'
}
if ($IncludeSourceTree) {
    Add-PlanDir 'C:\echo-dev' 'ECHO 源码树（git 仓库）'
}

# 兄弟目录提示（不删，只报告）
$siblings = @()
$parent = Split-Path -Parent $L.Base
if ($parent -and (Test-Path -LiteralPath $parent)) {
    foreach ($d in (Get-ChildItem -LiteralPath $parent -Directory -ErrorAction SilentlyContinue)) {
        if ($d.FullName.TrimEnd('\') -ieq $L.Base.TrimEnd('\')) { continue }
        if ($d.Name -match '(?i)^ECHO-' -and $d.Name -notmatch '(?i)^ECHO-(backup|delivery)') {
            $s = Get-PathStats $d.FullName
            $siblings += [pscustomobject]@{ Path = $d.FullName; Bytes = $s.Bytes }
        }
    }
}

# ---------------------------------------------------------------- 5. 打印清单
Write-Section '将要删除的目录'
$totalBytes = [double]0
foreach ($d in $planDirs) {
    Write-Host ('   - ' + $d.Path) -ForegroundColor White
    Write-Host ('       ' + $d.Label + ' · ' + $d.Files + ' 个文件 · ' + (Format-Size $d.Bytes)) -ForegroundColor DarkGray
    $totalBytes += $d.Bytes
}
Write-Section '将要删除的文件 / 快捷方式'
foreach ($f in $planFiles) {
    Write-Host ('   - ' + $f.Path) -ForegroundColor White
    Write-Host ('       ' + $f.Label) -ForegroundColor DarkGray
}
Write-Section '将要删除的登记'
if ($planTasks.Count -gt 0) { foreach ($t in $planTasks) { Write-Host ('   - 计划任务：' + $t) -ForegroundColor White } }
if ($planFirewall.Count -gt 0) { foreach ($rname in $planFirewall) { Write-Host ('   - 防火墙规则：' + $rname) -ForegroundColor White } }
Write-Host ('   - 注册表：HKCU 的 Run 项里指向本安装目录的项；HKCU\Software\Classes\echo（若存在）') -ForegroundColor White
Write-Section '将要结束的进程'
$planProcs = @(Get-EchoRelatedProcesses $L.Base)
if ($planProcs.Count -eq 0) {
    Write-Host '   - （当前没有在跑的 ECHO 相关进程）' -ForegroundColor DarkGray
} else {
    foreach ($p in $planProcs) {
        $cmd = $p.CommandLine
        if ($cmd.Length -gt 110) { $cmd = $cmd.Substring(0, 110) + ' …' }
        Write-Host ('   - ' + $p.Name + ' (pid ' + $p.PID + ')') -ForegroundColor White
        Write-Host ('       ' + $cmd) -ForegroundColor DarkGray
    }
}
Write-Section '合计'
Write-Host ('   预计释放 ' + (Format-Size $totalBytes)) -ForegroundColor Yellow
if ($siblings.Count -gt 0) {
    Write-Section '发现这些 ECHO 相关兄弟目录（默认不删，如需清理请手工处理或加 -IncludeDev 后确认）'
    foreach ($s in $siblings) {
        Write-Host ('   - ' + $s.Path + ' (' + (Format-Size $s.Bytes) + ')') -ForegroundColor DarkYellow
    }
}
if ($DryRun) {
    Write-Host ''
    Write-Ok 'DRY-RUN 结束：没有删除任何东西。确认无误后去掉 -DryRun 再跑一次。'
    exit 0
}

# ---------------------------------------------------------------- 6. 确认
Write-Host ''
if (-not $Yes) {
    Write-Warn '上面这些东西会被删除（会议数据备份不在其中）。'
    $ans = Read-Host '  确认清理请输入 CLEAN 并回车（其他任意输入=取消）'
    if ($ans -ne 'CLEAN') {
        Write-Note '已取消，什么都没删。'
        exit 0
    }
}

$startTime = Get-Date
$removed = New-Object System.Collections.ArrayList
$failed = New-Object System.Collections.ArrayList
$record = New-Object System.Collections.ArrayList
[void]$record.Add('ECHO 清理记录 ' + $startTime.ToString('yyyy-MM-dd HH:mm:ss'))
[void]$record.Add('安装目录：' + $L.Base)
[void]$record.Add('版本：' + $L.Version)
[void]$record.Add('会议数据备份：' + $backup)
[void]$record.Add('')

# ---------------------------------------------------------------- 7. 停进程
Write-Section '停止 ECHO'
$killed = Stop-EchoAll $L.Base
if ($killed.Count -gt 0) { Write-Ok ('已结束进程：' + ($killed -join '、')) } else { Write-Step '没有需要结束的进程' }
$down = Wait-EchoDown $L.Base 30
if ($down) { Write-Ok '服务已停止' } else { Write-Warn '服务仍在响应，稍后删除时可能失败（脚本会重试）' }

# ---------------------------------------------------------------- 8. 删登记
Write-Section '清理自启动 / 快捷方式 / 计划任务 / 防火墙'
foreach ($f in $planFiles) {
    try {
        Remove-Item -LiteralPath $f.Path -Force -ErrorAction Stop
        Write-Ok ('已删除 ' + $f.Path)
        [void]$record.Add('删除文件：' + $f.Path)
    } catch {
        Write-Warn ('删除失败 ' + $f.Path + '：' + $_.Exception.Message)
        [void]$failed.Add($f.Path)
    }
}
foreach ($t in $planTasks) {
    try {
        Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop
        Write-Ok ('已删除计划任务 ' + $t)
        [void]$record.Add('删除计划任务：' + $t)
    } catch {
        Write-Warn ('计划任务删除失败 ' + $t + '：' + $_.Exception.Message)
        [void]$failed.Add('计划任务:' + $t)
    }
}
foreach ($rname in $planFirewall) {
    try {
        Get-NetFirewallRule -DisplayName $rname -ErrorAction Stop | Remove-NetFirewallRule -ErrorAction Stop
        Write-Ok ('已删除防火墙规则 ' + $rname)
        [void]$record.Add('删除防火墙规则：' + $rname)
    } catch {
        Write-Warn ('防火墙规则删除失败 ' + $rname + '：' + $_.Exception.Message)
        [void]$failed.Add('防火墙:' + $rname)
    }
}

# 注册表：Run 项 + echo:// 协议
$runKeys = @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce'
)
foreach ($k in $runKeys) {
    if (-not (Test-Path -LiteralPath $k)) { continue }
    $props = (Get-ItemProperty -LiteralPath $k -ErrorAction SilentlyContinue).PSObject.Properties
    foreach ($prop in $props) {
        if ($prop.Name -match '^PS') { continue }
        $val = [string]$prop.Value
        if ($val -and $val.IndexOf($L.Base, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            try {
                Remove-ItemProperty -LiteralPath $k -Name $prop.Name -Force -ErrorAction Stop
                Write-Ok ('已删除注册表自启 ' + $prop.Name)
                [void]$record.Add('删除注册表 Run 项：' + $k + ' \ ' + $prop.Name)
            } catch {
                Write-Warn ('注册表项删除失败 ' + $prop.Name + '：' + $_.Exception.Message)
            }
        }
    }
}
foreach ($proto in @('HKCU:\Software\Classes\echo')) {
    if (Test-Path -LiteralPath $proto) {
        try {
            Remove-Item -LiteralPath $proto -Recurse -Force -ErrorAction Stop
            Write-Ok ('已删除协议登记 ' + $proto)
            [void]$record.Add('删除注册表：' + $proto)
        } catch {
            Write-Warn ('协议登记删除失败：' + $_.Exception.Message)
        }
    }
}

# ---------------------------------------------------------------- 9. 删目录
Write-Section '删除目录'
function Remove-PathHard([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    for ($i = 1; $i -le 3; $i++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        } catch { }
        if (-not (Test-Path -LiteralPath $Path)) { return $true }
        Start-Sleep -Seconds 2
    }
    try { $null = & cmd.exe /c rd /s /q $Path 2>&1 } catch { }
    Start-Sleep -Milliseconds 500
    return (-not (Test-Path -LiteralPath $Path))
}

foreach ($d in $planDirs) {
    Write-Step ('删除 ' + $d.Path + ' …')
    if (Remove-PathHard $d.Path) {
        Write-Ok ('已删除 ' + $d.Path)
        [void]$record.Add('删除目录：' + $d.Path + ' (' + (Format-Size $d.Bytes) + ')')
        [void]$removed.Add($d.Path)
    } else {
        Write-Warn ('删不干净 ' + $d.Path + '（可能有进程占用；重启后再跑一次本脚本）')
        [void]$failed.Add($d.Path)
        [void]$record.Add('未删净：' + $d.Path)
    }
}

# 停掉可能残留的看门狗 / 边条
Start-Sleep -Milliseconds 600
$killed2 = Stop-EchoAll $L.Base
if ($killed2.Count -gt 0) {
    Write-Warn ('删完又发现残留进程，已结束：' + ($killed2 -join '、'))
    [void]$record.Add('额外结束进程：' + ($killed2 -join '、'))
}

# ---------------------------------------------------------------- 10. 收尾
Write-Section '清理结果'
$left = @($planDirs | Where-Object { Test-Path -LiteralPath $_.Path })
if ($left.Count -eq 0) { Write-Ok '全部目标已删除' } else { Write-Warn ('仍有 ' + $left.Count + ' 个目录没删净：' + (($left | ForEach-Object { $_.Path }) -join '、')) }
if ($failed.Count -gt 0) { Write-Warn ('未完成项 ' + $failed.Count + ' 个，见清理记录。') }

$recordPath = Join-Path $scriptDir ('清理记录-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt')
$recordText = @($record) -join [Environment]::NewLine
Write-Utf8NoBom $recordPath $recordText
Write-Note ('清理记录：' + $recordPath)
if ($backup -and (Test-Path -LiteralPath $backup)) {
    try { Write-Utf8NoBom (Join-Path $backup ('清理记录-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt')) $recordText } catch { }
    Write-Note ('备份目录里也留了一份：' + $backup)
}

if ($siblings.Count -gt 0) {
    Write-Section '以下兄弟目录仍然存在（默认不删）'
    foreach ($s in $siblings) { Write-Host ('   - ' + $s.Path + ' (' + (Format-Size $s.Bytes) + ')') -ForegroundColor DarkYellow }
    Write-Note '确认不需要可手工删除，或加 -IncludeDev 重新跑本脚本。'
}

Write-Host ''
Write-Ok 'ECHO 已清理。'
Write-Host ''
Write-Note '下一步：'
Write-Note '  1) 重新安装：双击交付包里的「装我.cmd」（会问装到哪个目录，默认 D:\ECHO）'
Write-Note '  2) 装完双击「3-还原ECHO会议数据.cmd」把会议数据放回去'
Write-Note '  3) 提示：模型 / 后端配对 / 智能体 属于程序组件，重装后按向导重新装即可，不在会议数据备份里'
exit 0
