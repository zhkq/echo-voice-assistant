@echo off
REM =====================================================================
REM  ECHO 一键安装入口（双击运行）
REM  用法：把本文件与 ECHO-交付包-*.zip 放在同一文件夹，双击
REM  本文件即开始安装（自动发现 zip，也可以手动指定）。
REM
REM  可用开关（可选，追加在 install.ps1 后面）：
REM    -Zip <path>    指定交付包 zip（默认自动发现同目录）
REM    -DestDir <dir> 安装目录（默认 D:\ECHO）
REM    -DryRun        模拟模式（只预览不安装）
REM    -Silent        无人值守（全默认值）
REM =====================================================================

setlocal
cd /d "%~dp0"

echo.
echo  ============================================
echo   ECHO 个人语音助理 - 一键安装程序
echo  ============================================
echo.

REM 自动发现同目录下的交付包 zip（若有，传给 install.ps1）
set "ZIP="
for %%f in ("%~dp0ECHO-*.zip") do (
    echo %%~nxf | findstr /I /C:"-offline-" /C:"-component-" /C:"-kit-" >nul || set "ZIP=%%f"
)
if defined ZIP (
    echo  发现交付包: %ZIP%
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Zip "%ZIP%" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
)

echo.
echo  安装向导已结束（详情看上面输出，日志见 %%TEMP%%\ECHO-install.log）
echo.
pause
