# ECHO LLM 路由（ECHO AUTO）

把 DSH 的模型调用接到本机路由上，**按「模型组」在多个上游之间按通道号顺序派发**：

```
DSH（选「ECHO AUTO」）
   │  baseURL = http://127.0.0.1:8899      ← settings.yaml 的 echo-auto 路由
   ▼
dsh-failover/proxy.py（本机 8899，ECHO 启动时由 boot 组件守护）
   │  组 echo-auto：  通道1 大ep（内网大EP）  通道2 外4.1F（DeepSeek 官方）
   │  通道1 连不上/首字节不来/401/402/403/404/429/5xx → 自动切通道2
   ▼
内网可达 → 全走内网；内网不可达 → 自动走官方（流式照常，用户无感）
```

> 端口默认 `8899`，改 `config.json` 的 `port` 即可（**本机现为 `18061`**，因为 8899 之类
> 的低端口段容易被 Windows 保留段占用）；下文示例都按默认端口写。ECHO 面板端口同理，
> 默认 `8970`，权威值见 ECHO 的 `data/echo-port.txt`。

代理只监听 `127.0.0.1`，DSH 永远看得到它 → 不会触发 DSH 的重试风暴，也不会整轮失败。

## 为什么需要它（DSH 本身不支持跨 provider 回退）

经源码确认：`agent-default-model` 只存单一 `{provider, model}`，没有 fallback 字段；
`dsh-llm-retry` 只在**同一 provider 内**重试，失败后整轮报错，不会切另一家。
所以「多上游按优先级派发」只能在本机放一个路由。

组、优先级、成员**全部是配置**（`config.json` 的 `groups`，或面板里点选）——
没有写死的「内网/公网」概念；内网那两条 provider 也可以继续直连、不经过本路由。
目录名 `dsh-failover` 是历史遗留（早期只做单一内网→公网回退），功能上它就是模型路由。

## 在 ECHO 面板里管理（推荐）

面板顶部页签 **「模型路由」** 就是这个模型组的控制台，不用手改 JSON。
界面上一律用 **通道** 说话：**通道号 = 列表里的位置 = 派发顺序**（自动，勿手填），
每条通道再给一个 **昵称**（点一下就能改，`config.json` 里就是成员的 `name`），
所以成员在界面上显示成 `通道1 大ep`、`通道2 外4.1F`。

| 位置 | 作用 |
|---|---|
| 仪表盘小卡片 | 第一行 `🛰️ 模型路由  (通道1 大ep) ……… 详情 ›`：通道包成椭圆徽章（跟会议录音卡同一套 `.badge` 样式，绿=走通道1、黄=换了通道、红=全挂/未运行）<br>第二行只有各通道命中次数 `通道1 0 · 通道2 35`，末尾靠右是上次派发时刻（只写 `20:43:27`，不写"最近派发"）。**失败/总数这类运营汇总不在卡片上**，见模型路由页的「派发情况」卡片 |
| 模型路由页 · 卡片结构（2026-09-13 拆分 + 重排 + 改名） | 两张独立 `.card`：①「🛰️ 通道设置」= 注册状态行 + 成员列表（`#rtMembers`）+ 添加入口 + 底部动作；②「📊 派发情况」（原「运营数据」，2026-09-13 改名并去掉标题后的括号说明）在①正下方，分两块：上排 4 个指标块 `累计派发 / 失败 / 成功率 / 最近命中`，下排「各通道命中」每行 `① 昵称 ▓▓▓░░ 40`（条长按最大值归一，各行可直接比长短，零命中的通道整行转灰）。路由进程没跑时整卡只显示一行黄色提示；数据由 2 秒轮询写 `#rtStats` |
| 设置页（界面小改，2026-09-13） | 全局「折叠/展开」由文字按钮改成**标题行最右的双箭头图标**（箭头方向表示点下去的结果，全折叠后翻转；折叠状态照旧记在 `localStorage`）；「📦 模型」卡片首行只留名称 + 就绪数 `已就绪 n/m`，说明文字挪到清单上方 |
| 折叠条 · 状态区（顺序：DSH → 路由 → 状态 → 时间 → 快捷键） | 模型路由灯显示**当前在服通道的昵称**（绿 = 排在最前且在服；黄 = 最前那颗挂了、请求已改走后面的通道；红 = 全挂），灯上不出现「通道N」；状态灯就是 ECHO 服务本身（空闲 / 命令处理中 / 录音中 / 离线）；时间行是连续在线时长。启动页「启动日志」标题右侧同口径显示 `已在线 1时20分 (空闲)`（原在顶栏右上角，2026-09-12 搬到这里） |
| 折叠条 · 底部两个箭头 | 左 `‹`（`#btnExpand`）= **展开成仪表盘边条**（与 `Ctrl+Shift+E` 同效；命令行等价 `echo-sidebar.exe --signal=rail-expand`），右 `›`（`#btnHide`）= 隐藏折叠条（鼠标贴屏右缘唤回）；折叠条 ⇄ 边条 ⇄ 隐藏三种切换共用同一套滑动动画（抽屉感，不做逐帧重排） |
| 模型路由页 · 成员行 | 一行一条通道：`① [昵称] ● ↑ ↓ 开关 ✕`。绿点=可用、黄=熔断、红=不可达、灰=未探测，**悬停看原因/首字节/成功失败**；点通道号展开详情（模型、端点、凭据、声明能力） |
| 模型路由页 · 添加成员 | 下拉里是 **DSH 里已配置的模型**（内置 deepseek 官方 4 个 + 你配过的每条 pi-ai 路由），选中即加入，自动给一个可改的短昵称 |
| 模型路由页 · 底部一行 | `立即探测` · `重载配置` · `注册到 DSH`（次要动作，平时不用点） |
| 设置 → 模型路由 | 启动时注册到 DSH、模型组显示名、健康探测间隔、成员连接/首字节超时、熔断阈值与冷却 |

行为约定：

- **列表顺序就是通道号**，从上往下派发；开关关掉 = 暂时停用（保留配置与统计，不进派发序列）。
- 保存后自动做三件事：写 `config.json` → `POST /admin/reload` 热重载（**不重启路由进程**）→ 重新注册进 DSH。
- 「缺凭据」只在展开的详情里提示：该成员的凭据引用不在 `~/.dsh/.credentials.yaml` 里，调它必然失败（提示但不拦保存）。
- 声明能力（上下文/输出）由**启用成员的最小值**自动算出，避免把超长请求发给弱成员。

## 注册进 DSH（ECHO AUTO）

DSH 的 `dsh-llm-pi-ai` 适配器**按请求**读取家目录里的 `settings.yaml`，provider 路由集合
变化会原子重新注册——所以不需要写 DSH 插件、也不需要重启 DSH。ECHO 启动时由
`app/llm_router.py` 自动完成（设置里可关）：

1. 读 `dsh-failover/config.json` 的 `groups`：每个组 = DSH 里一个可选模型；
2. 在**每个实际存在的 DSH 家目录**的 `settings.yaml` 里 upsert
   `llm-pi-ai.providers.echo-auto`（`baseURL: http://127.0.0.1:8899`，
   `apiKeyEnv: ECHO_ROUTER_TOKEN`）——用 ruamel.yaml 往返写入，保留用户注释，
   写前备份 `settings.yaml.bak-echo-auto-*`；
3. 在同一个家目录的 `.credentials.yaml` 的 `refs` 下确保存在 `ECHO_ROUTER_TOKEN`，
   而且**每个家目录里是同一个值**（路由只认 config.json 里那一个令牌，两份不一致
   必然有一边 401）。

### 用户不一定两个都装（2026-09-22 同事反馈）

同一台机器上可能只有桌面版、只有标准版，也可能两个都没有 —— 四种情况都要自洽：

| 这台机器有 | 写到哪 | 说明 |
|---|---|---|
| 只有桌面版 | 桌面版家目录（`DSH_HOME`，缺省用户家目录下的 .dsh） | 老行为 |
| 只有标准版 | 标准版家目录（`harnessHome`，缺省 `{DATA}/harness`） | 向导默认就是标准版，**这条以前是坏的**：注册不到、令牌读成空串 |
| 两个都有 | 两处都写 | 令牌统一，两个 DSH 里都选得到 ECHO AUTO |
| 两个都没有 | 哪都不写 | 回报"没找到 DSH 家目录"，路由本身照常可用（纪要可直连上游） |

判据是**家目录里已经有 `settings.yaml`**（DSH 首次运行会自己写下它）：只有目录名、
没有这个文件 = 那台 DSH 还没初始化过，**不替它造配置**（D25 的纪律）。

boot 的闸（`boot._agent_dsh_available()`）同步放宽：桌面版或标准版**任一**可用就注册；
标准版的家目录是"选中它"之后才存在的，所以 `boot._start_harness()` 就绪后、
以及面板里改智能体设置后的联动（`settings_effects._agent()`）都会再复核一次注册。

### 路由进程去哪儿找密钥

`app/llm_router.py` 每次注册都会把"存在的家目录"写进 **`dsh-failover/homes.json`**
（只有路径，不含密钥）；路由进程（`proxy.py` 的 `cred_paths()`）按 mtime **热读**它，
逐份找 `refs`。于是成员密钥（内网网关令牌这类）写在哪个家目录里都找得到，
家目录**后来才出现**（用户在面板里选中标准版）也不用手动重启路由进程。
没有 `homes.json` 时退回启动时的 `ECHO_DSH_HOMES` 环境变量，再退回桌面版那一份（老行为）；
`FAILOVER_<REF>` / `<REF>` 环境变量始终最优先。

手动执行 / 查看（**用 `-m` 跑**：脚本要能 `import app`，直接 `python app\llm_router.py`
会以 `ModuleNotFoundError: No module named 'app'` 收场 —— 2026-09-22 实测）：

```powershell
cd <安装目录>          # 例如 C:\echo-dev
# 立即注册（幂等；写到每个存在的家目录）
& .\venv\Scripts\python.exe -m app.llm_router
# 看当前注册态（不写任何文件；homes 里逐家目录列出注册没注册）
& .\venv\Scripts\python.exe -m app.llm_router --check
```

DSH 侧的默认模型（`agent-default-model`）由你自己决定（桌面版本机当前是
`deepseek-official/deepseek-flash`，标准版是内网 `bjunicom-deepseek-v4-flash`）。
ECHO AUTO 是「可选模型」，ECHO 没运行时选它会明确失败（路由不在），不会静默走别的上游。

内网那条 provider（`bjunicom-deepseek-v4-flash`）保持**直连内网**，不经过本路由；
要内网优先、内网不通自动换官方，就在面板里把它加成 ECHO AUTO 的通道（本机就是这么配的）。
**两个 DSH 各有一份 settings.yaml**，直连 provider 与 `agent-default-model` 要各配一次
（ECHO 只管 `echo-auto` 这一条）。

## 文件结构

```
dsh-failover/
├── proxy.py                 # 路由主体（FastAPI + httpx）
├── config.json              # 组/成员/超时/探测参数（不含密钥）
├── dashboard.html           # 自带实时状态页（通道健康 + 按通道命中次数）
├── check.py                 # 命令行自检：健康表 + 注册态 + 真实派发一次
├── start.ps1 / stop.ps1 / status.ps1 / install-autostart.ps1
├── fix-ps1-encoding.ps1     # 改完 .ps1 后跑一下：给本目录脚本补回 UTF-8 BOM
└── logs/                    # proxy.log / proxy.err.log
app/llm_router.py            # 把模型组注册进 DSH（settings.yaml + credentials）
app/router_admin.py          # 管理面：DSH 候选发现 / 组成员增删改 / 健康聚合
app/failover_proxy.py        # ECHO 启动时守护 8899（30s 复查，挂了自动拉起）
web/{app.js,index.html,rail.html,app.css}   # 面板：卡片 / 折叠条 / 模型路由页
```

> ⚠️ 改 `.ps1` 的老坑：这些脚本里有中文，Windows PowerShell 5.1 没有 BOM 就会把中文当乱码，
> 报 `The Try statement is missing its Catch or Finally block` 之类莫名其妙的错。
> 编辑工具常常会抹掉 BOM，改完跑一次 `dsh-failover\fix-ps1-encoding.ps1` 即可（幂等）。

密钥不落本目录：运行时按成员配置的 `credential`（ref 名）去
`~/.dsh/.credentials.yaml` 取，也支持环境变量 `FAILOVER_<REF>` 覆盖。

## 配置项（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8899` | 监听地址，仅本机 |
| `groups.<id>` | — | 一个模型组；`<id>` 就是 DSH 里选的模型 id |
| `groups.<id>.display_name` | 组 id | DSH 模型名 + 注册时的显示名 |
| `groups.<id>.require_token` | `false` | 是否校验 `ECHO_ROUTER_TOKEN`（ECHO AUTO 为 `true`） |
| `groups.<id>.failover_on_status` | `[401,402,403,404,429]` | 命中这些状态码就切下一个成员 |
| `groups.<id>.failover_on_5xx` | `true` | 上游 5xx 是否切下一个 |
| `groups.<id>.context_window` / `max_tokens` | 自动取 min | 对 DSH 声明的最小能力（面板保存时按启用成员算） |
| `groups.<id>.members[]` | — | 成员：`name` / `priority` / `enabled` / `base_url` / `model` / `credential` / `headers` / `body_mode` / `first_byte_timeout` / `context_window` / `max_tokens` |
| `members[].enabled` | `true` | `false` = 停用（保留配置，不进派发序列） |
| `members[].body_mode` | `passthrough` | `passthrough` 原样转发；`openai-safe` 只保留标准 OpenAI 字段（给官方 API 用） |
| `connect_timeout` | 1.5s | 连接耐心：**故意很短**，DNS/握手失败要「秒切」 |
| `first_byte_timeout` | 20.0s | 首字节耐心（超时视为「不通」，切下一个） |
| `read_timeout` / `write_timeout` | 360 / 120s | 长流读取/写入上限 |
| `probe_enabled` | `true` | 关掉后健康表只由真实请求驱动（每个请求真试一遍优先级序列） |
| `probe_interval` / `probe_timeout` | 45s / 4s | 后台探测节奏 |
| `breaker_threshold` / `breaker_cooldown` | 2 / 30s | 连续失败 N 次进入熔断，冷却多少秒后放行试水 |

改完 `config.json` 不必重启：面板点「重载配置」，或 `POST http://127.0.0.1:8899/admin/reload`
（带 `Authorization: Bearer <ECHO_ROUTER_TOKEN>`）。连接/读写超时挂在 httpx client 上，
只有这两项要重启进程。

## 管理接口（只给本机面板用）

| 方法与路径 | 作用 |
|---|---|
| `GET /health` | 全部组的成员健康 + 命中统计 + 切换历史 |
| `GET /models` | 组与成员模型清单（OpenAI 风格，DSH 发现用） |
| `POST /admin/reload` | 重读 config.json 并原地换组（保留同名成员的健康计数） |
| `POST /admin/probe` | 立即探测所有成员，返回最新健康表 |
| ECHO 侧 `GET /api/router/status` | 组成员（配置+健康）+ DSH 注册态 + 候选模型（面板用） |
| ECHO 侧 `PUT /api/router/members` | 保存成员（顺序=通道号）→ 热重载 → 重新注册 |
| ECHO 侧 `POST /api/router/{probe,reload,register}` | 立即探测 / 重载 / 注册 |
| ECHO 侧 `GET /api/failover/health` | 路由 `/health` 的转发（面板卡片与折叠条在用；端点名沿用旧称） |

## 可达性怎么判（三层，越往下越贵）

| 层 | 手段 | 成本 | 结论 |
|---|---|---|---|
| L1 | TCP 连接（1.5s） | ~10ms | 网络路径通不通 |
| L3 | `GET {baseURL}/models` + Bearer | ~50ms | 端点活着 + 凭据有效（DSH 自己也用这条做发现） |
| L5 | 真实请求（建流到首字节） | 请求本身 | 最真实；连续 2 次失败即熔断该成员 |

后台按 `probe_interval` 跑 L1+L3 维护健康表；**请求时先跳过「探测不可达 / 熔断中」的成员**，
全都不可用时不再跳过（照样按序试一遍，别把唯一活路挡死）。4xx 参数类错误
（含上下文超长）**不切换**，原样透传给 DSH——换模型解决不了，反而会掩盖真实错误。

## 使用与验证

```powershell
powershell -ExecutionPolicy Bypass -File dsh-failover\start.ps1     # 启动
powershell -ExecutionPolicy Bypass -File dsh-failover\status.ps1    # 状态
powershell -ExecutionPolicy Bypass -File dsh-failover\stop.ps1      # 停止
& $HOME\.echo-venv\Scripts\python.exe app\llm_router.py             # 重新注册进 DSH
```

1. **健康表**：浏览器开 `http://127.0.0.1:8899/`（仪表盘）或 `/health`（JSON）——
   每个成员的状态、最近探测结论、命中次数、TTFB 一目了然。
2. **真实派发**：`python dsh-failover\check.py --call`（不传令牌会自动从 DSH 凭据库读），
   看响应头 `X-ECHO-Channel`（通道号）与 `X-ECHO-Route`（`通道号-昵称`）。
3. **DSH 侧**：在 DSH 里把模型切到「ECHO AUTO」发一句话；或让子代理用
   `provider=echo-auto, model=echo-auto` 跑一次，路由的请求计数会 +1。
4. **面板侧**：「模型路由」页点 `立即探测`，健康点应立刻刷新；
   把通道1 的开关关掉再保存，卡片/折叠条应改显示「通道2 …」。

## 已知边界

- **只能在首个数据块之前切换**：一旦向 DSH 出了流就只能中断（DSH 会按路由的
  `retryPolicy` 重试，此时坏通道已熔断，重试自然落到下一个通道）。
- 组内成员能力不同（上下文/推理档位/是否支持图片）时，DSH 看到的是配置里声明的
  **最小能力**；声明成 min 才不会把超长请求发给弱成员。
- `body_mode: openai-safe` 会剔除内网网关专有字段，**换到官方通道时推理档位可能与内网不等价**。
- 路由与本机 ECHO 共用 venv python；venv 换位置要改 `start.ps1` 的路径。
- 连接/读写超时改完要重启路由进程（其余参数热重载即生效）。

## 开源化注意事项（ECHO-public）

- `config.json` 里的公司内网域名（`your-intranet-gateway.example.com`）、`userId` 属于**个人环境**，
  对外发布前应删掉或改成示例占位。
- 通用默认应当是「用户在面板里勾选任意 OpenAI 兼容端点组成模型组」——
  这条链路（`app/router_admin.py` 从 DSH 配置发现候选 + 面板勾选）本身不含任何
  厂商/内网常量，可直接复用；要清掉的只是 `config.json` 里的既有成员。
