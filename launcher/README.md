# ECHO.exe 启动器

`ECHO.exe` 是安装包里的启动器，负责：

1. 找到安装目录下的 `runtime-core\pythonw.exe` 并启动 `-m app.main`（无控制台，
   stdout/stderr 写入 `%LOCALAPPDATA%\ECHO\logs\launcher.log`）；
2. 单实例：重复启动时探 `/api/status` 并唤出已有边条（没有 .NET Desktop Runtime 时
   用浏览器打开面板），不会起第二个服务；
3. 生命周期：把 ECHO 放进 Windows Job Object（`KILL_ON_JOB_CLOSE`），ECHO 退出/启动器
   被杀时整棵子进程树（边条、本机后端、无头 dsh）一起收掉；
4. `--stop`：安装/升级/卸载前停 ECHO；会议录音或指令处理中返回退出码 2，安装器据此
   中止，不强杀；
5. 失败时弹一个说明原因的 MessageBox（`--quiet` / `--autostart` 下只写日志）。

技术选型：**C# + .NET 8 NativeAOT（WinExe，Win32 P/Invoke）**。产物约 3–6 MB，目标机
不需要 .NET Desktop Runtime；与现有边条同语言，但不引入 C++/Go 新工具链。

```text
ECHO.exe                       启动/唤出
ECHO.exe --stop [--quiet]      停止（安装器用）
ECHO.exe --autostart           开机自启：启动但不弹面板
ECHO.exe --quiet               失败不弹窗（CI/无人值守）
ECHO.exe --wait=90             首次启动等待就绪上限（5–600 秒）
```

构建：

```powershell
dotnet publish launcher/ECHO.Launcher/ECHO.Launcher.csproj -c Release -r win-x64 `
  -p:PublishAot=true -p:DebugType=None -p:DebugSymbols=false -o dist/installer-stage
```

单测（纯逻辑，不启动 Windows）：

```powershell
dotnet test launcher/ECHO.Launcher.Tests/ECHO.Launcher.Tests.csproj -c Release
```
