# Windows 单文件安装包（阶段 1）

生成 `dist/ECHO-Setup-<version>.exe`：一个双击安装的当前用户安装包，装完是
`ECHO.exe` 启动器 + `echo-core`（app/web/sidebar）+ `runtime-core`，不需要管理员权限。

## 安装后的布局

```text
%LOCALAPPDATA%\Programs\ECHO\        Inno 安装目录（升级/卸载整体替换）
  ECHO.exe                            .NET 8 NativeAOT 启动器
  echo-core\                          app / web / assets / docs / scripts / .dsh / sidebar
  runtime-core\                       可重定位 CPython 3.11 + requirements-core

%LOCALAPPDATA%\ECHO\                 数据根（卸载/升级都不碰）
  data\                               固定：echo.db、logs、pid、token、install-mode.json
  models\  meeting\  aide\  review\  backend\
```

启动器给 Python 注入 `ECHO_BASE=%LOCALAPPDATA%\ECHO` 与
`ECHO_DATA=%LOCALAPPDATA%\ECHO\data`，所以 `app/paths.py` 的数据类兄弟目录全部落在
数据根，程序目录只放可替换文件。DSH 的家目录仍是官方桌面端的 `%USERPROFILE%\.dsh`
（一套 DSH，桌面版与无头 dsh 共用）。

## 构建（Windows）

前置：Python 3.11、.NET 8 SDK（NativeAOT 需要 MSVC）、Inno Setup 6.7.4、`uv`、
能访问 PyPI/官方 DSH 的构建网络。

```powershell
# 1) 准备 runtime-core 的独立 CPython（build-package.ps1 会用到）
python -m pip install uv
uv python install 3.11.15

# 2) 一条命令：代码包（profile=installer）+ runtime-core + sidebar + 启动器 + ISCC
python scripts/build_installer.py --out dist
# 已有 ISCC 不在默认位置时：
# python scripts/build_installer.py --iscc "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"

# 只打印命令（任意平台可用）：
python scripts/build_installer.py --dry-run --skip-main --skip-runtime
```

产物：
- `dist/ECHO-Setup-<version>.exe`
- `dist/ECHO-Setup-<version>.exe.sha256`
- `dist/installer-build-info.json`（版本、SHA、构建时间、`signed=false`）

CI 的 `.github/workflows/windows-installer.yml` 会固定下载/校验 Inno Setup 6.7.4，
构建后跑 `installer/smoke-install.ps1`（静默安装 → 启动 → 停止 → 卸载保留数据 →
升级保留数据），并上传安装包与日志。

## 签名

阶段 1 **未签名**，接受 SmartScreen 的“未知发布者”。构建脚本没有任何签名步骤，
也不接受自签名证书；阶段 2 购买代码签名证书后再签 `ECHO-Setup.exe` 与 `ECHO.exe`。

## 首次启动向导

安装器写 `<数据根>\data\install-mode.json`：

- `fresh`：面板自动弹首次启用向导（4 步：DSH → 模型 → 笔记库 → 技能）；
- `upgrade`：不强制弹，入口留在「设置」。

DSH 步骤复用 `.dsh/skills/echo-install/scripts/harness-install-desktop.ps1`：
检测官方桌面端自带的 `dsh.cmd`（真跑 `--version`，不是“目录存在”），没有就下载官方
安装包、校验 Authenticode、`/S` 当前用户静默安装。安装器**不打包 DSH**；失败会显示
原因/日志/重试与官方手动下载链接，不会静默回退 npm。

## 安全边界

- 当前用户安装：`PrivilegesRequired=lowest`，只写 HKCU，不写 HKLM，不弹 UAC；
- 卸载只删除 `%LOCALAPPDATA%\Programs\ECHO` 的程序文件；
  `%LOCALAPPDATA%\ECHO`（data/models/meeting/backend 等）保留；
- 升级/卸载前调 `ECHO.exe --stop`；正在录音或处理指令时拒绝，并让用户先结束；
- 不打包能力后端（约 3 GB）与模型；
- 老 kit 装机（`D:\ECHO` / `C:\ECHO`）**不自动迁移**：数据根不同，自动搬容易覆盖；
  需要迁移时另做一次性导入（复制 data/models/backend，再改设置里的绝对路径）。

## 验收状态

- 自动化：Windows CI 构建 + 静默安装/卸载/升级冒烟；启动器 .NET 单测；Inno/脚本静态断言。
- 真机安装/卸载/升级、首次向导与 DSH 下载的**最终截图与记录由 Lead 在 Windows 10/11
  交互桌面上采集**（当前 worker 主机是 Linux，不能代替这一步）。PR 描述会如实标注。
