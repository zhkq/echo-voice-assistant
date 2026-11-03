<#
.SYNOPSIS
  停止 ECHO（服务 + 边条 + 看门狗 + 切换器监督进程），运维目录第 5 步。

.DESCRIPTION
  - 正在录音/转写时默认拒绝停止（会丢数据），确认要中断请加 -Force
  - 删除/清理由 2-清理ECHO 负责；本脚本只停，不动数据
  - 开机自启不受影响：下次登录 ECHO 仍会自动启动

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "5-停止ECHO.ps1"
#>
param(
    [string]$Root = '',
    [switch]$Force
)

$scriptDir = $PSScriptRoot
. (Join-Path $scriptDir '_echo-common.ps1')

Write-Head '停止 ECHO'

$base = Find-EchoBase $Root
if (-not $base) {
    Write-Fail '没有找到 ECHO 安装目录。请用 -Root 指定，例如：-Root "D:\ECHO"'
    exit 2
}
$L = Get-EchoLayout $base
Write-Note ('安装目录   ' + $L.Base)
Write-Note ('服务状态   ' + $(if ($L.ServiceUp) { '运行中 · 端口 ' + $L.Port + ' · 版本 ' + $L.Version } else { '未运行' }))

$procs = @(Get-EchoRelatedProcesses $L.Base)
if (-not $L.ServiceUp -and $procs.Count -eq 0) {
    Write-Ok 'ECHO 没有在运行，无需停止。'
    exit 0
}
if (Test-EchoMeetingBusy $L.Base) {
    if (-not $Force) {
        Write-Fail 'ECHO 正在录音或转写，停止会中断并丢数据。'
        Write-Step '等录音/转写结束后再停；确认要强行中断请加 -Force。'
        exit 3
    }
    Write-Warn '正在录音/转写：按 -Force 继续停止（会中断当前任务）。'
}

$killed = Stop-EchoAll $L.Base
if ($killed.Count -gt 0) {
    Write-Ok ('已结束进程：' + ($killed -join '、'))
} else {
    Write-Step '没有需要结束的进程'
}
if (Wait-EchoDown $L.Base 30) {
    Write-Ok 'ECHO 已停止（服务、边条、看门狗、切换器监督进程）'
} else {
    Write-Fail '服务仍在响应，停止失败。'
    Write-Step '可以再跑一次；或双击 7-状态ECHO.cmd 看进程与日志。'
    exit 4
}
Write-Note '开机自启仍在：下次登录会自动启动。现在要再开就双击 4-启动ECHO.cmd。'
exit 0
