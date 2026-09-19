# 独立 DeepSeek Harness 接入（新增「独立 DeepSeek Harness」智能体）

> 2026-09-19 实测 + 落地。用户需求原话："可以在智能体那里增加一个新的 agent 类型，
> 然后随着 echo 一起启动"，背景是问"能不能只装 deepseek harness、不装 DSH Desktop"。

## 1. 结论先说

**可以**，而且 ECHO 侧几乎不用改协议层：官方独立发行版与 Desktop 的 **`/api` 接口面完全一致**，
只有"谁提供 web 服务"和"鉴权方式"两点不同。

| | DSH Desktop（现有） | 独立 harness（本次新增） |
|---|---|---|
| 发行形态 | 桌面客户端（内含 harness） | npm 包 **`@deepseek-ai/dsh`**（实测 `0.1.5-rc.2`） |
| 启动 | 用户装并运行 Desktop | **ECHO 作为子进程拉起**：`npx -y @deepseek-ai/dsh web --port 43199 --no-open` |
| 端口 | 43120 | 默认 **43199**（刻意避开，两者可并存） |
| 家目录（DSH_HOME） | 桌面版自己的（`~/.dsh`） | **`{DATA}/harness`**（默认），与 Desktop 完全分开 |
| 鉴权 | `/api` 校验**签名 Cookie**（密钥在凭据文件里，ECHO 自铸 HMAC） | 启动时打印 `…/?token=<token>`，用它访问一次换 `dsh-auth-…` Cookie |
| 接口面 | `session/*` `workspace/*` `settings/*` | **同上**（实测 `session/list`、`session/create`、`workspace/create` 全 200） |

所以"用户只装 harness + ECHO"这条路是通的；ECHO 现在把它做成了**第三个智能体**，
与 DSH Desktop、CodeBuddy 并列，选中它就随 ECHO 一起启动、切走就停。

## 2. 实测记录（隔离环境，不碰现有数据）

隔离条件：`DSH_HOME=C:\echo-dev\dist\_dsh-probe`、端口 **43199**、全程不动 43120 与桌面版数据。

```
npx -y @deepseek-ai/dsh --version          → 0.1.5-rc.2（与桌面自带 dsh 同版本）
npx -y @deepseek-ai/dsh web --port 43199 --no-open
                                           → dsh web: http://127.0.0.1:43199/?token=<token>
GET  /api/status                （无 cookie）→ 401      ← /api 有"浏览器信任围栏"
GET  /?token=<token>                        → 303 See Other + Set-Cookie: dsh-auth-…  (Location: /)
POST /api/session/list                      → 200 {"result":{"ok":true,"value":{"items":[]}}}
POST /api/session/create                    → 200 {"sessionId":"session-…","agentPreset":"standard"}
POST /api/workspace/create                  → 200 {"workspace":{"workspaceId":"…"}}
POST /api/settings/describe                 → 端点存在（参数形状不对会报 arguments-invalid）
```

请求体与 ECHO 现有实现**一模一样**（`{"type":"client-request","rpcId":…,"method":…,"payload":{"args":…}}`），
响应信封也一样（`{"type":"server-response","result":{"ok":…,"value":…}}`）。

### 两个必须记住的坑

1. **登录响应是 303，绝不能跟着跳转。**
   urllib 默认会跟随，而跟随的请求**不带**那一步拿到的 `Set-Cookie` → 跳到 `/` 就被围栏判 401。
   表现是"token 明明是对的，却报 `登录失败：HTTP 401`"（我就在这儿卡了一轮：
   同一个 token 用 PowerShell 请求 200、用 urllib 401）。
   修法见 `app/agents/harness_agent.py` 的 `_login()`：用不跟随的 opener，
   把 303 当**正常**响应读它的 `Set-Cookie`。
2. **杀进程要杀整棵树。** `npx` 会套 `cmd → node(npx-cli) → cmd → node(dsh)` 四层，
   只 `terminate()` 最外层 wrapper 等于没杀（实测：切回 DSH 后 43199 仍在监听）。
   现在用 `taskkill /PID <pid> /T /F`，并在端口仍被占用时按"监听该端口的 PID"补一刀。

## 3. ECHO 侧的实现

| 位置 | 做了什么 |
|---|---|
| `app/agents/harness_agent.py` | **继承** `DshAgent`（协议与会话语义全部复用），只覆写：`__init__`（换 base_url）、`_login/_cookie_header/rpc`（token→Cookie + 401 重登重试）、`available()`（给出"怎么把它跑起来"的人话原因） |
| `app/harness_proc.py` | 进程管理：`ensure_running()`（幂等 + 20s 冷却 + 串行锁）、`stop()`（杀树 + pid 记录 + 兜底按监听进程杀）、token 捕获（读子进程 stdout 正则取 `?token=`）、pid/token 落盘（`data/logs/harness.{pid,token.txt}`） |
| `app/boot.py` | 组件 `harness`（可启动/可停止），随 ECHO 启动：选中 + 开关打开时才真拉起；没选中但上次是 ECHO 起的 → **自愈收尾** |
| `app/api.py` | `PUT /settings` 的智能体联动：选中即拉起、切走即停止（只停 ECHO 自己起的） |
| `app/config.py` | 5 个 `hidden` 键（`agentHarnessEnabled` / `harnessHome` / `harnessPort` / `harnessCommand` / `harnessToken`），面板在智能体展开区里编辑；token 是 `secret`，永不回显 |
| `app/services.py` | 状态注册表加 `harness` 一行（启动页可见） |
| 面板 | 无需新代码：`/api/agents` 多一行「独立 DeepSeek Harness」，展开区自动显示它的启动命令/家目录/端口，配「检测」按钮 |

## 4. 怎么用

1. 设置 → 智能体 → 打开「**独立 DeepSeek Harness**」右侧开关（面板会同时把执行智能体切过去）；
   ECHO 立刻 `npx -y @deepseek-ai/dsh web --port 43199 --no-open` 起一个自己的 harness，
   等它就绪后状态变「可用 · API 可访问」（首次要 pnpm 装插件，可能要一两分钟）。
2. 切回「DSH Desktop」→ ECHO 自动停掉自己起的那个（桌面版的 43120 不受影响）。
3. 启动页会多出「独立 DeepSeek Harness」组件，可手动启动/停止。
4. 常见问题：
   * **需要 Node.js**：没有 npx 时状态会提示；装了但不在 PATH 里，就在展开区把
     「harness 启动命令」改成 npx 的全路径。
   * **端口**：默认 43199；不要填 43120（桌面版）或 18060（ECHO 自己），`port_conflict()` 会拒绝。
   * **你自己起的实例**：ECHO 不碰它；要用它就把启动时打印的 token 填进「harness 访问 token」。
   * **日志**：`data/logs/harness.log`（子进程全部输出）、`data/logs/harness-token.txt`、`data/logs/harness.pid`。

## 5. 还没验 / 待办

* **真跑一轮对话**：`session/prompt` + 回复轮询需要 harness 里配好模型 provider
  （在它的 `DSH_HOME` 里配，指向内网网关或 DeepSeek 官方）。本次只验到
  `session/create`、`session/list`、`workspace/create`、`settings/describe` 这一层。
* **归档链**：`workspace/archiveSession` 与 `settings/update`（工作日志归档的权限预置）
  尚未在独立 harness 上实测；`worklog.py` 目前仍读桌面版家目录下的会话存储（P6 待改）。
* **npm 包首次安装**：走 npx 缓存（实测约几十秒到 1 分钟），离线环境需要预热缓存。

## 6. 相关

* `docs/REFACTOR-PLAN.md` §发布形态：老的两条 PyPI 路线（`deepseek-harness-sdk` /
  `deepseek-harness-runtime-bin`）**已下架**，别再照老计划装；现行入口就是本文这条 npm 命令。
* `docs/能力页签重设计.md` / `docs/settings-重设计方案.md`：智能体区域在设置页的位置与展开逻辑。
