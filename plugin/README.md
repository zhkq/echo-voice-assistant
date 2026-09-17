# plugin/ —— 已退役（2026-09-17）

这里只剩 `echo-host/` 的源码存档，**没有任何东西会加载它**：DSH 的 profile 补丁层注册行、DSH 安装目录里的部署副本、以及所有自动安装/自愈脚本都已移除。

## 为什么退役

1. **边条面板那半早就死了**：DSH Desktop 2.0.9 起，插件位置只能拿到 Electron 的工具/渲染进程变体（`own=[net,systemPreferences]`，没有 `app`/`BrowserWindow`/`screen`/`globalShortcut`），插件无法再开侧边栏窗口；2.0.11 实测结论不变。`Ctrl+Shift+E` 仪表盘由 ECHO 自己的 .NET 侧栏（`sidebar/`）+ `app/hotkey.py` 的 `RegisterHotKey` 提供，与插件无关。
2. **注册方式注定被升级抹掉**：注册行指向 `<DSH 安装目录>\resources\app.asar.unpacked\echo-host\index.js`，而 DSH 每次升级都重建 `app.asar.unpacked`。2026-09-12（2.0.5→2.0.9）静默消失一次，2026-09-17 今晚又连续 5 次 `ERR_MODULE_NOT_FOUND`（21:02 / 21:03 / 21:05 / 21:14 / 21:37）。
3. **端口来源失配**：插件 `resolveEchoPort()` 读 `~/.dsh/settings.yaml` 的 `# serverPort:` 注释行（回退 8970），而 ECHO 早已改为把实际端口写到 `data/echo-port.txt`（当晚实际是 18060）。所有 `.ps1` 脚本都已迁移到新来源，只有插件漏了 —— 于是它的探活/守护形同虚设。
4. **DSH 2.0.11 已有正式插件体系**：profile 内 `node_modules` 装包 + 插件市场/设置页清单（`dsh-community-market`、`dshmarket`、`dsh.profile.bundles`）。继续往安装目录写文件的打法已经是遗留方式。

## 现在由谁负责这些事

| 原先由插件负责 | 现在 |
|---|---|
| 开机/登录后拉起 ECHO | 启动文件夹快捷方式 → `scripts\echo-startup.vbs` → `scripts\startup.ps1`（自带守护重启） |
| 手动启动 | `scripts\start.ps1`、桌面快捷方式 `scripts\launch-desktop.ps1` |
| `Ctrl+Shift+E` 仪表盘边条 | ECHO 自己的 .NET 侧栏（`sidebar/`）+ `app/hotkey.py` |
| 面板端口发现 | `data\echo-port.txt`（由 `app/main.py` 写出） |

## 已清理的东西

- `~/.dsh/profiles/{web,desktop}/cordis.patch.yml`：删掉 `echo-host` 注册行，恢复为合法空列表 `[]`
- `<DSH 安装目录>\resources\app.asar.unpacked\echo-host\`：删除部署副本
- `scripts\install-echo-host-plugin.ps1`、`heal-dsh-plugin.ps1`、`install-upgrade-heal.ps1`、`rollback-echo-host-plugin.ps1`：删除
- `plugin\asar-get.cjs`、`deploy-check.cjs`、`electron-esm-probe.cjs`、`require-shape-probe.cjs`、`echo-host\electron-{main-api,probe-entry}.js`：一次性诊断探针，删除
- `scripts\{launch-desktop,start,startup,setup}.ps1`：去掉每次启动的 "self-heal 部署插件" 调用

退役当天的两份补丁层与部署副本备份在 `data\logs\retire-echo-host-<时间戳>\`（仅本机）。

## 想复活怎么办

源码在 git 历史里（`plugin/`、那 4 个脚本）。但**不要照原样复活**：注册路径会被升级抹掉、端口也读不准。若确实需要"DSH 启动时顺带守护 ECHO"，正确做法是按 2.0.11 的插件体系做成 profile 内的本地包（`~/.dsh/profiles/<active>/node_modules/echo-host/`，用包名注册），并把端口来源改成 `data\echo-port.txt`。

## 顺带清掉的 ECHO 侧残留

- `app/api.py`：删除 `POST /api/guard/log` 端点与 `GuardLogIn` 模型（唯一上报方就是插件）
- `web/index.html` + `web/app.js`：删除「启动」页签的「守护进程关键事件」卡片与 `loadGuardLogs()`
  （没有上报方，只会永远显示"尚未上报"）
- 注释同步：`app/main.py`（重复实例判定）、`app/failover_proxy.py`（ECHO 常驻由谁保证）、
  `app/audio/stt.py`（`ECHO_PYTHONW` 提示）、`docs/DEPLOY.md`（不再需要设 `ECHO_PYTHONW`）

`GET /api/logs?source=guard` 这个通用过滤能力保留（数据库里历史 guard 日志仍可查）。
