# install-desktop-shortcut.ps1 — 在桌面创建 ECHO 的启动/停止入口（可重复运行）
#   * ECHO 个人助理.lnk     一键启动（后台）+ 自动拉起 DSH + 打开面板
#   * 启动 ECHO.cmd         安装包同步提供的显式启动脚本
#   * 停止 ECHO.cmd         安装包同步提供的显式停止脚本（会停 watchdog/supervisor）
# 卸载时加 -Remove；-StartStopOnly 只补启动/停止脚本，不碰已有快捷方式（升级用）
param([switch]$Remove, [string]$DesktopDir = '', [switch]$StartStopOnly)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$desktop = if ($DesktopDir) { $DesktopDir } else { [Environment]::GetFolderPath('Desktop') }
$lnkPath = Join-Path $desktop 'ECHO 个人助理.lnk'
$startCmd = Join-Path $desktop '启动 ECHO.cmd'
$stopCmd = Join-Path $desktop '停止 ECHO.cmd'
$target = Join-Path $root 'scripts\launch-desktop.ps1'

if ($Remove) {
    foreach ($p in @($lnkPath, $startCmd, $stopCmd)) {
        if (Test-Path $p) { Remove-Item $p -Force; Write-Host "已移除桌面入口: $p" }
    }
    exit 0
}

# Desktop 上两个**真正的 .cmd 脚本**（不是快捷方式）：用户要求安装包同步提供
# 启动/停止脚本。脚本内容在安装时写入绝对路径，双击即可执行；路径用引号包住。
function Write-DesktopCmd([string]$path, [string]$scriptName, [string[]]$extra, [switch]$PauseAtEnd) {
    $psArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$root\scripts\$scriptName`"") + $extra
    $line = 'powershell ' + ($psArgs -join ' ')
    $body = "@echo off`r`n" + $line + "`r`n"
    if ($PauseAtEnd) { $body += "echo.`r`npause`r`n" }
    [System.IO.File]::WriteAllText($path, $body, [System.Text.Encoding]::ASCII)
    Write-Host "已创建桌面脚本: $path"
}

Write-DesktopCmd $startCmd 'start.ps1' @('-Background')
Write-DesktopCmd $stopCmd 'stop.ps1' @() -PauseAtEnd

# 升级模式：核心代码更新了，但用户的桌面快捷方式/开机自启属于他自己的环境，
# 不覆盖；只补两个启停脚本（2026-10-31 用户要求安装包在桌面提供它们）。
if ($StartStopOnly) {
    exit 0
}

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = 'powershell.exe'
# 注意：不能带 -WindowStyle Hidden（组策略会拦截隐藏启动 powershell.exe，
# 导致双击无反应）；launch-desktop.ps1 内部会自行隐藏窗口。
$lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$target`""
$lnk.WorkingDirectory = $root
$lnk.Description = 'ECHO 个人助理：一键启动（后台）+ 自动拉起 DSH + 打开面板'
# 图标：用 ECHO 自己的图标（从 web\icon-512.png 生成 assets\echo.ico）。
# 不提交 .ico 到仓库：它是可复现的派生物，每次运行本脚本重新生成即可。
# 这样桌面上的几个 ECHO 快捷方式一眼可辨，而不是都顶着通用 Python 图标。
function Get-EchoIcon([string]$Root) {
    $ico = Join-Path $Root 'assets\echo.ico'
    $png = Join-Path $Root 'web\icon-512.png'
    if (-not (Test-Path $png)) { return '' }
    if ((Test-Path $ico) -and ((Get-Item $ico).LastWriteTime -ge (Get-Item $png).LastWriteTime)) {
        return $ico
    }
    try {
        Add-Type -AssemblyName System.Drawing
        $dir = Split-Path $ico -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $img = [System.Drawing.Image]::FromFile($png)
        $bmp = New-Object System.Drawing.Bitmap 64, 64
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.DrawImage($img, 0, 0, 64, 64)
        $g.Dispose()
        $icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
        $fs = [System.IO.File]::Create($ico)
        $icon.Save($fs); $fs.Close()
        $img.Dispose(); $bmp.Dispose()
        return $ico
    } catch {
        Write-Host "（图标生成失败，用默认图标：$_）"
        return ''
    }
}
$icon = Get-EchoIcon $root
if ($icon) { $lnk.IconLocation = "$icon,0" }
else { $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,220" }
$lnk.Save()
Write-Host "已创建桌面快捷方式: $lnkPath"
Write-Host '双击即可启动 ECHO 并打开控制面板（端口以 data\echo-port.txt 为准）'
