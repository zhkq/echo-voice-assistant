<#
.SYNOPSIS
  启动 ECHO（后台服务 + 可选看门狗 / 面板），运维目录第 4 步。

.DESCRIPTION
  - 已经在运行：不重复启动，只报告状态
  - 未运行：后台启动服务，等端口就绪，打印面板地址
  - -Supervise：顺便拉起本安装的看门狗和切换器监督进程（ECHO 掉了会自动拉起）
  - -OpenPanel：启动后按 ECHO 设置打开面板（默认不打开；设置里「启动时自动显示折叠条」
    开着的话，服务启动后本来就会自动出现右缘折叠条）

.PARAMETER Root
  ECHO 安装目录（一般自动探测）

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "4-启动ECHO.ps1"

.EXAMPLE
  ... -Supervise -OpenPanel
#>
param(
    [string]$Root = '',
    [switch]$Supervise,
    [switch]$OpenPanel,
    [int]$TimeoutSec = 150
)

$scriptDir = $PSScriptRoot
. (Join-Path $scriptDir '_echo-common.ps1')

Write-Head '启动 ECHO'

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
Write-Note ('程序目录   ' + $L.Code + '    (' + $L.Layout + ')')

$st = Get-EchoStatus $L.Base
if ($st) {
    Write-Ok ('ECHO 已经在运行（版本 ' + $st.version + '，端口 ' + $L.Port + '），不会重复启动。')
} else {
    Write-Step '后台启动 ECHO …'
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
        Write-Step '排查：双击 7-状态ECHO.cmd -LogTail 30；或看 data\logs\echo-server.log.err'
        exit 4
    }
    Write-Ok ('ECHO 已启动（版本 ' + $st.version + '，端口 ' + $L.Port + '）')
}

if ($Supervise) {
    $wd = Start-EchoWatchdog $L.Code
    if ($wd -eq 'started') { Write-Ok '看门狗已启动（ECHO 意外退出会自动拉起）' }
    elseif ($wd -eq 'already') { Write-Note '看门狗已在运行，未重复启动' }
    else { Write-Warn '没找到 scripts\startup.ps1，跳过看门狗' }
    $sw = Start-EchoSwitchSupervisor $L.Base
    if ($sw -eq 'started') { Write-Ok '切换器监督进程已启动' }
    elseif ($sw -eq 'already') { Write-Note '切换器监督进程已在运行，未重复启动' }
    elseif ($sw -eq 'skip') { Write-Note '本安装不是切换器的当前实例，跳过切换器监督进程' }
    else { Write-Note '没有切换器（.echo-switch）登记，跳过' }
}

Write-Note ('面板地址   http://127.0.0.1:' + $L.Port + '/')
if ($OpenPanel) {
    $how = Open-EchoPanel $L.Code $L.Base
    if ($how) { Write-Ok ('已请求打开面板：' + $how) } else { Write-Warn '面板没能自动打开，请手工访问上面的地址' }
}
if ($st -and $st.meeting -and $st.meeting.active) {
    Write-Warn '注意：当前有一场会议正在进行（录音/转写中）。'
}
exit 0
