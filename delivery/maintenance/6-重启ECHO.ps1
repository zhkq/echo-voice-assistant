<#
.SYNOPSIS
  重启 ECHO（先停再起），运维目录第 6 步。

.DESCRIPTION
  - 正在录音/转写时默认拒绝（加 -Force 可强行中断）
  - 重启后默认恢复看门狗 + 切换器监督进程（-NoSupervise 可关掉）
  - 数据库/会议数据不受影响；转写中的任务会中断

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "6-重启ECHO.ps1"

.EXAMPLE
  ... -NoSupervise -OpenPanel
#>
param(
    [string]$Root = '',
    [switch]$Force,
    [switch]$NoSupervise,
    [switch]$OpenPanel,
    [int]$TimeoutSec = 150
)

$scriptDir = $PSScriptRoot
. (Join-Path $scriptDir '_echo-common.ps1')

Write-Head '重启 ECHO'

$base = Find-EchoBase $Root
if (-not $base) {
    Write-Fail '没有找到 ECHO 安装目录。请用 -Root 指定，例如：-Root "D:\ECHO"'
    exit 2
}
$L = Get-EchoLayout $base
if (-not $L.Code) {
    Write-Fail ('该目录不像 ECHO 安装目录（找不到 echo-core\app\main.py）：' + $L.Base)
    exit 2
}
Write-Note ('安装目录   ' + $L.Base)
Write-Note ('服务状态   ' + $(if ($L.ServiceUp) { '运行中 · 端口 ' + $L.Port + ' · 版本 ' + $L.Version } else { '未运行' }))

if (Test-EchoMeetingBusy $L.Base) {
    if (-not $Force) {
        Write-Fail 'ECHO 正在录音或转写，重启会中断并丢数据。'
        Write-Step '等录音/转写结束后再重启；确认要强行中断请加 -Force。'
        exit 3
    }
    Write-Warn '正在录音/转写：按 -Force 继续重启（会中断当前任务）。'
}

Write-Section '停止'
$killed = Stop-EchoAll $L.Base
if ($killed.Count -gt 0) { Write-Ok ('已结束进程：' + ($killed -join '、')) } else { Write-Step '没有需要结束的进程' }
if (-not (Wait-EchoDown $L.Base 30)) {
    Write-Fail '停不下来，已中止重启（避免半开状态）。'
    exit 4
}
Write-Ok '已停止'

Write-Section '启动'
try {
    Start-EchoDetached $L.Code
} catch {
    Write-Fail ('启动失败：' + $_.Exception.Message)
    exit 4
}
$st = Wait-EchoUp $L.Base $TimeoutSec
if (-not $st) {
    Write-Fail ('启动超时（' + $TimeoutSec + ' 秒）。')
    $tail = Get-EchoLogTail $L.Base 12
    if ($tail.Count -gt 0) {
        Write-Note '最近的服务日志：'
        foreach ($line in $tail) { Write-Host ('     ' + $line) -ForegroundColor DarkGray }
    }
    exit 4
}
Write-Ok ('ECHO 已启动（版本 ' + $st.version + '，端口 ' + $L.Port + '）')

if (-not $NoSupervise) {
    $wd = Start-EchoWatchdog $L.Code
    if ($wd -eq 'started') { Write-Ok '看门狗已启动' }
    elseif ($wd -eq 'already') { Write-Note '看门狗已在运行' }
    $sw = Start-EchoSwitchSupervisor $L.Base
    if ($sw -eq 'started') { Write-Ok '切换器监督进程已启动' }
    elseif ($sw -eq 'already') { Write-Note '切换器监督进程已在运行' }
}

Write-Note ('面板地址   http://127.0.0.1:' + $L.Port + '/')
if ($OpenPanel) {
    $how = Open-EchoPanel $L.Code $L.Base
    if ($how) { Write-Ok ('已请求打开面板：' + $how) } else { Write-Warn '面板没能自动打开，请手工访问上面的地址' }
}
$st2 = Get-EchoStatus $L.Base
if ($st2 -and $st2.meeting -and $st2.meeting.active) {
    Write-Warn '注意：当前有一场会议正在进行（录音/转写中）。'
}
exit 0
