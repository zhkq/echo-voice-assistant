# ECHO 能力后端 · 服务端设计

> 定位：**无状态能力服务**。客户端持有全部业务数据与状态，服务端只提供算力能力。
> 它只认识"能力槽（slot）"，不认识会议 / 命令 / 纪要。
>
> 前置文档：
> - `docs/前后分离-能力服务化设计.md`（切面原则、槽分解、向量空间）
> - `docs/能力路由-三后端与轻客户端.md`（三后端路由、轻客户端预算）
>
> 本文回答：这个服务端**具体怎么建**。

---

## 0. 结论速览

服务端**难的不是接口，是模型生命周期与并发**。接口只有 6 个，一页写得完；
但 GPU 是唯一的稀缺资源，而现有引擎层是按"单用户单进程"写的（全局单例 + 全局锁），
直接搬到服务端会在第二个并发客户端上就串行化。

四条核心设计：

| # | 决策 | 理由 |
|---|---|---|
| **S1** | 抽一个 **`EnginePool`**；客户端是"容量 1 的池" | 统一客户端与服务端的引擎生命周期，一处实现两处用 |
| **S2** | 并发度由**模型实例数**决定，不由 CPU 核数决定；有界队列 + 背压 | GPU 才是瓶颈 |
| **S3** | **服务端不许静默回退 CPU** | 客户端回退 CPU 是对的；服务端回退会拖垮所有客户端 |
| **S4** | 临时文件**允许**，但必须**自有 + 可证明清理 + 有兜底** | 见 §5 |

---

## 1. 职责边界（钉死，并用测试保证）

### 1.1 做什么

音频 → 文本 / 时间轴 / 说话人 / 嵌入。纯函数语义：**请求自带全部上下文，响应即全部结果。**

### 1.2 不做什么

| 不做 | 原因 |
|---|---|
| 不存会议 / 命令 / 纪要 / 说话人名 | 业务数据 |
| 不存声纹库 | 敏感个人信息 |
| 不缓存转写结果 | 客户端负责去重；缓存 = 变相存储 |
| 不做 job 队列 | job 表 = 服务端状态，破坏无状态扩展（见前置文档 §2.2） |
| 不记录**内容**（音频 / 文本 / 嵌入）到日志或数据库 | 内容是最容易泄的地方 |
| 不认识"会议"这个概念 | 服务端只认识槽 |

> **2026-09-23 修订**：管理面（§8.4）需要持久化凭据与调用元数据，
> 所以"服务端没有数据库"这条**不再成立**。但边界没变 ——
> **数据库里只允许出现"关于请求的元数据"和"关于客户端的管理数据"，不允许出现"请求的内容"。**
> 完整的表白名单与判定见 §8.5。

### 1.3 禁令 → 护栏测试

沿用本仓库"铁律 + 测试钉住"的做法：

```
server/ 源码里不出现：meeting / command / summary / voiceprint
server/ 不 import：app.db、app.config.settings、app.meeting、app.assistant
服务端路由表：/v1/* 里没有任何写端点（白名单见 §6.1）
跑完一次能力请求：工作目录无新增文件、临时目录为空、日志 grep 不到输入内容
服务端数据库的表：必须在 §8.5 的白名单里
服务端数据库的列：不得出现承载内容的列（text / transcript / embedding / audio / content…）
```

**最后两条是这次修订新增的** —— 有了库之后，"不存储业务数据"就必须从
"没有库"这种**结构性保证**，降级为**"表白名单 + 列黑名单"的契约性保证**，
并靠测试守住。这是这次修订付出的代价，要如实记在文档里。

---

## 2. 目录结构，以及怎么复用客户端引擎层

### 2.1 好消息：引擎层本来就是干净的

实测依赖：

| 模块 | import | 结论 |
|---|---|---|
| `app/audio/stt.py` | `os/re/sys/threading` + `app.paths` | ✅ 可直接复用 |
| `app/audio/diarize.py` | `os/threading/wave` + `app.paths` + `numpy` | ✅ 可直接复用 |

业务编排全在 `meeting.py` / `assistant.py` —— 那两个**不进服务端**。

### 2.2 一个现成的接缝

`app/paths.py:128-134` 的 `_settings_get()` 注释里写着"测试可替换本函数"：

```python
def _settings_get(name: str) -> str:
    """读配置项（惰性导入，避免 config ↔ paths 循环导入）。测试可替换本函数。"""
```

服务端把它的取值源从"DB 的 settings 表"换成"服务端 YAML"，就拿到了
`models_root()` 等路径解析，**不需要改 `paths.py` 一行**。这是当年留的接缝，正好用在这里。

### 2.3 目录结构

```
server/
  __init__.py
  main.py          # FastAPI app 工厂 + 生命周期（启动预热 / 退出清理）
  settings.py      # 服务端配置：YAML + 环境变量（独立于客户端 config）
  errors.py        # 机器可读错误码（§6.3）
  auth.py          # mTLS / JWT / scopes / 配额
  audit.py         # 只记元数据
  pool.py          # ★ EnginePool（§4）
  tmp.py           # ★ TempWorkspace（§5.3）
  audio.py         # 上传校验 + 解码 + 重采样
  routes/
    capabilities.py
    asr.py
    diarize.py
    embed.py
    tts.py
    health.py
  tests/           # 服务端自己的护栏测试（§12）
```

**服务端不放进 `app/`** —— 放进去就会忍不住 import 业务模块。物理隔离比纪律可靠。

---

## 3. 并发与进程模型（最难的部分）

### 3.1 现有引擎层为什么不能直接用

| 现状 | 位置 | 在服务端的问题 |
|---|---|---|
| `_ENGINES = {}` + `_ENGINE_LOCK = threading.Lock()` | `stt.py:154-155` | 全局字典 + 一把全局锁；不同模型互相阻塞 |
| `_load_pipeline()` 把**整个加载过程**包在 `_lock` 里 | `diarize.py:116-144` | 首次 diarize 请求会**独占锁数十秒**，期间所有 ASR 也被阻塞（如果共用一个池） |
| `_pipeline` 模块级单例 | `diarize.py:56` | 无法卸载、无法多实例、显存被永久占住 |
| 加载失败 → **静默回退 CPU** | `stt.py:170-174, 200-205, 286-296` | 见 §3.4，这在服务端是有害的 |

### 3.2 并发度的决定因素

```
并发上限 = min( Σ 各模型可用实例数 , 队列预算 , 显存预算 )
```

**不是 CPU 核数。** 一个 8 核机器如果只有一张 24 GB 卡，能同时跑的大模型可能只有 1–2 个。

### 3.3 v1：单进程 + EnginePool + **两级并发闸门**（不排队）

```
                    ┌─────────────── ECHO server (单进程) ───────────────┐
  N 个客户端 ──────▶│  uvicorn (async)                                   │
                    │    ├─ 鉴权 / 配额 / 上限检查（无阻塞，不占额度）    │
                    │    ├─ 音频 → TempWorkspace（自有临时文件）          │
                    │    └─ ★ 准入闸门（per-client=1 → global=2）         │
                    │           拿不到 → 立刻拒（带明确 code）            │
                    │           拿到 ──▶ ThreadPoolExecutor              │
                    │                    └─ EnginePool: asr / diarize / embed
                    └────────────────────────────────────────────────────┘
```

要点：

- **HTTP 层全异步**（校验、鉴权、落临时文件、SSE 推进度），**推理在受控线程里跑**。
  PyTorch / onnxruntime 的 C++ 算子会释放 GIL，所以线程模型对 GPU 推理是够用的。
- **每个模型一把信号量**（`threading.Semaphore(n)`），`n = 该模型允许的并发实例数`。
  pyannote pipeline 不是线程安全的，`n` 只能是 1。
- **两级并发闸门**（2026-09-23 定，替代原来的"有界队列"）：

  | 闸门 | 上限 | 超了返 | 客户端该怎么办 |
  |---|---|---|---|
  | **每客户端** | **1**（硬性） | `409 client_busy` | **不重试** —— 是它自己上一个还没完 |
  | **服务端总计** | **2**（暂定，实验后定） | `503 server_busy` + `Retry-After` | **退避重试** |

- **v1 不排队**（`queue_max = 0`）：并发满了**立刻拒**。
  理由：服务端无状态、排队要占内存与连接，而**客户端本来就有离线队列** ——
  把"等待"的责任放在客户端，服务端只负责"如实说满了"。
  如果实验发现尖峰丢请求太多，再开一个小队列（`queue_max` 配置项保留，见 §3.6）。

**为什么不用 `uvicorn --workers N`**：每个 worker 会**各自加载一份模型** → 显存 × N。
4 个 worker 就是 4 份 Qwen3-ASR，直接把卡撑爆。

### 3.4 服务端不许静默回退 CPU

客户端这么做是对的（`stt.py` 三处 `except → cpu`，单用户、宁可慢也要出结果）。
**服务端这么做的后果完全不同**：

### 3.4 服务端不许静默回退 CPU

客户端这么做是对的（`stt.py` 三处 `except → cpu`，单用户、宁可慢也要出结果）。
**服务端这么做的后果完全不同**：

- 一个请求偷偷占满 CPU → **所有客户端的请求一起变慢**（延迟毛刺无法定位）
- `/v1/capabilities` 声明"我有 GPU"，实际在跑 CPU → **客户端无法据实决策**
- 显存不足的真正原因被掩盖，运维看不到

**服务端的正确行为**：显存不足 → 尝试卸载 LRU 未使用模型 → 仍不足 → **`503` + 明确错误码
`gpu_oom` + `Retry-After`**。宁可让这一个客户端降级到内网公共服务，也不拖垮整个服务端。

> 这条要在 `EnginePool` 里写死，并且**注释里说明为什么与客户端不同**，
> 否则下一个人会"顺手对齐"客户端的行为。

### 3.5 v2：按模型分进程（需要时再做）

当出现这些信号时升级：

- 显存够但模型不 thread-safe，想要真并行
- 一个模型的加载/卸载影响其它模型的服务质量
- 需要按模型独立重启/升级

形态：每个模型族一个推理进程，主进程通过 IPC 派发。**v1 不做** —— 复杂度换来的收益在单卡场景下不明显。

### 3.6 并发准入与拒绝类型（2026-09-23 定）

#### 闸门的确切语义

| 闸门 | 上限 | 计数口径 |
|---|---|---|
| **每客户端** | **1**（硬性） | 该 `client_id` 当前"已准入且推理未结束"的请求数 |
| **服务端总计** | **2**（暂定，实验后定） | 全服务端同口径之和 |

**什么算"占用通道"** —— 这是最容易搞错的地方：

```
占通道：从「准入通过」到「推理结束 + 临时文件清理完」
不占通道：TLS 握手、鉴权、上传接收、解码、重采样、响应序列化
```

审批与接收**不占**通道，否则一个 20 MB 的上传会把坑占几十秒；
但推理阶段**确实占**，因为 GPU 才是那个真正的稀缺资源。
代价是"可能传完了才发现被拒" → 用下面的乐观预检缓解。

#### 拒绝响应（三种，客户端行为完全不同）

| 场景 | HTTP | `code` | `Retry-After` | 客户端应当 |
|---|---|---|---|---|
| 该客户端自己已有请求在跑 | **409** | `client_busy` | 不给 | **不要重试** —— 是它自己上一个还没完（并发应配 1，属客户端 bug） |
| **服务端通道满** | **503** | `server_busy` | **给**（建议 5 s） | **系统忙，稍后再试**（退避，见下） |
| 该客户端当日配额用完 | **429** | `quota_exceeded` | 到次日重置的秒数 | **今天别再试** |

**为什么必须分成三个 code**：统一回一个 `429`/`503` 的话，客户端只能盲目重试 ——
而 `client_busy` 重试**永远不会成功**，`quota_exceeded` 重试只是白烧额度。
**"明确拒绝的类型"就是这个意思。**

响应体（`message` 给人看、`code` 给程序看）：

```jsonc
HTTP/1.1 503 Service Unavailable
Retry-After: 5

{
  "code": "server_busy",
  "message": "系统忙，请稍后再试",
  "retryAfter": 5
}
```

#### 乐观预检（省掉"传完才被拒"）

客户端上传前可先读 `GET /v1/capabilities` 的
`activeRequests` / `maxConcurrent` / `perClientActive` —— 已满就**先不发**。
但要注意：

- 预检是**乐观**的（你看到空闲、发过去时别人先占了）→ **真正的准入仍然在服务端**
- 它只是**减少无谓的上传**，**不是保证**

#### 重试归客户端（服务端只给两样东西）

服务端**不排队、不重试、不占着连接等**。它只负责给：

1. **明确的 `code`** —— 让客户端知道"该不该重试"
2. **`Retry-After`** —— 让客户端知道"多久之后再来"（**服务端比客户端更清楚自己有多忙**）

**客户端侧**（契约要求；落地细节见 3.0 总览 §12 的离线队列）：

- 优先听 `Retry-After`；没有则**指数退避 + 抖动**（1s → 2s → 4s → … 上限 60s）
- **退避作用于整个队列，不是单个请求** —— 服务端忙是后端整体忙，
  不能让 6 个分段各自去撞一次墙
- 一直重试到成功或用户取消；连续失败 N 次后面板提示"服务端持续繁忙"（**但不是"失败"**）
- **`client_busy` 视为客户端 bug**（并发必须配成 1）：记日志，**不重试**

#### 并发闸门 ≠ 配额（两道都要，别合并）

| | 并发闸门 | 配额 |
|---|---|---|
| 时间尺度 | **此刻** | 当日累计 |
| 拒了之后 | 稍后就好了 | **今天都不会好** |
| 返 | `503 server_busy` / `409 client_busy` | `429 quota_exceeded` |

#### `queue_max` 保留但默认 0

配置项留着（`queue_max` / `queue_wait_timeout_s`），**默认 0 = 不排队**。
将来若实验发现尖峰丢请求太多，可以开一个小队列 —— 那时 `429 queue_full`
才会出现。**现在这个 code 用不到，但先在错误码表里占位**，
免得以后新增时老客户端不认识。

---

## 4. `EnginePool`：核心组件

> 客户端与服务端**共用**这个抽象；客户端只是"池容量 1、无 LRU 卸载"的特例。

### 4.1 职责

| 职责 | 说明 |
|---|---|
| **单飞加载** | 并发 N 个请求要同一个模型，只加载一次，其余等待同一个 Future |
| **引用计数** | 推理中不许卸载 |
| **LRU 卸载** | 显存压力下按"最久未使用且引用计数为 0"卸载 |
| **显存预算** | 加载前预估，超预算先卸载，仍不够则拒绝（**不 OOM、不回退 CPU**） |
| **状态上报** | 每个模型 `absent / loading / ready / failed / evicting`，供 `/v1/capabilities` 与 `/v1/health` |
| **版本冻结** | `modelVersion` / `vectorSpaceId` 在**进程生命周期内不变** |
| **加载可等待不可取消** | 加载是长操作，请求**排队等待**（有超时），不能取消半途的加载 |

### 4.2 接口草案

```python
class ModelSpec(NamedTuple):
    id: str                # "pyannote-3.1"
    slot: str              # "diarize.turns"
    loader: Callable[[], Any]
    est_vram_mb: int
    max_concurrency: int   # pyannote = 1（非线程安全）
    model_version: str
    vector_space_id: str | None   # 只有向量类模型有
    resident: bool         # True = 常驻不参与 LRU（如 speaker.embed）

class EnginePool:
    async def acquire(self, model_id: str, *, timeout: float) -> Lease: ...
    # Lease 是 context manager：__enter__ 保证模型已 ready 且引用计数 +1
    #                     __exit__ 引用计数 -1，触发 LRU 检查

    def status(self) -> dict: ...       # 给 /v1/capabilities
    def unload(self, model_id: str) -> bool: ...
    def shutdown(self) -> None: ...     # 退出时释放显存
```

### 4.3 常驻 vs 按需（沿用客户端 `boot.py` 的心智）

| 模型 | 策略 | 理由 |
|---|---|---|
| speaker embedding | **常驻** | 小、高频、加载快 → 冷启动就预热 |
| sherpa / 短音频 ASR | **常驻** | 命令路径要低延迟 |
| 会议 ASR（中型） | 按需 + LRU | 用量中等 |
| 大模型（Qwen3-ASR / whisper-large） | 按需 + LRU | 吃显存 |
| pyannote 三件套 | 按需 + LRU | 加载慢、显存大 |

这与客户端 `boot.py` 的「`stt-cmd` 常驻 / `stt-meeting` 按需并可卸载」是**同一个模型** ——
可以直接复用那套组件状态机的心智（`pending→starting→online/failed/disabled/idle`）。

### 4.4 版本冻结（关键）

`modelVersion` 与 `vectorSpaceId` **在进程生命周期内不得变化**。理由：
客户端按 `vectorSpaceId` 分组比较嵌入，并做**会议级锁定**。
如果服务端在同一个 `vectorSpaceId` 下悄悄换了模型（比如运维换了个权重文件），
客户端的跨段质心与声纹匹配会**静默失效** —— 余弦相似度不会崩，只会开始认错人。

所以：换模型 = **换 `vectorSpaceId` = 重启服务**。配置里写死，运行时不可热改。

### 4.5 面板上那两张卡，已经是 `EnginePool` 的 UI 原型

现网面板「启动」页签里已经有这两张卡：

| 面板 | 含义 | 服务端对应 |
|---|---|---|
| 🔨 **命令转写引擎（常驻）** · `sensevoice` · `cuda:0` · `在线` · `15.2s` | 常驻引擎 + 设备 + 状态机 + 加载耗时 | `specs[asr-short]`：`resident: true` |
| 📄 **会议转写引擎（按需）** · `qwen3asr` · `空闲` | 按需引擎，可卸载 | `specs[asr-long]`：`resident: false` |
| 引擎下拉 + 启动/停止 按钮 | 选实现 + 显式生命周期控制 | `specs[].impl` + `pool.acquire/unload` |

**这张卡片上的每一个元素都是 `EnginePool` 需要的**：常驻/按需、状态（在线/空闲/模型加载）、
加载耗时、当前实现、显式启停。所以：

1. **服务端直接复用 `app/boot.py` 的组件状态机**（`pending→starting→online/failed/disabled/idle`）
   与 `app/services.py` 的状态上报，不要另造一套。
2. **服务端的 `/v1/capabilities` 就是这两张卡的数据源** —— 它必须能渲染出同样的东西。
3. **客户端组件页应该分成两块：「本机组件」+「服务端引擎」。** 后者是服务端
   `/v1/capabilities` 的渲染。理由：用户要知道"我现在能不能走服务端、它在不在加载中"，
   否则"会议转写卡住"时无从判断是本机问题还是服务端问题。

### 4.6 澄清：torch 禁令只针对客户端

前置文档说的"默认档不含 torch"是**客户端**的约束（办公本装不下、启动慢）。
**服务端可以也应该装 torch** —— SenseVoice（funasr）与 Qwen3-ASR（qwen-asr + transformers）
都是 torch 系，而它们的中文效果已在实机验证，是服务端的**主力**而非可选项。

服务端的对应约束不是"别装 torch"，而是：

- 显存预算有界（§4.1），加载前预估、超预算拒绝
- **不许静默回退 CPU**（§3.4）
- 模型加载是启动成本，不是请求成本（常驻预热）

---

## 5. 临时文件与音频管线

> **📌 2026-09-23 简化说明**：本章的落盘策略已按"**按日期落盘 + 定时清理**"放宽
> ——**不再用内存盘、不做容量硬门禁、不做出入口封装**。
> **权威口径在 §9.4**，本章其余部分（为什么不能交给框架管、raw body、请求生命周期）
> 仍然有效，但凡是与 §9.4 冲突的，**以 §9.4 为准**。

### 5.1 政策：允许，但必须"有主"

**临时文件是允许的** —— 引擎确实需要文件（`funasr` 要路径、`diarize._read_wav()` 读文件、
调试要留样）。约束不是"禁止落盘"，而是：

| 要求 | 含义 |
|---|---|
| **自有** | 由我们的代码创建和删除，**不交给框架管** |
| **可证明清理** | 请求结束（含异常、含客户端断开）后必然删除，有测试断言 |
| **有兜底** | 崩溃/断电残留 → 启动时清扫；删除失败 → 登记并重试 |
| **有上限** | 临时目录容量有界（tmpfs `size=` 或磁盘配额），满了拒绝而不是写爆盘 |
| **有命名空间** | 按实例隔离，多实例/多进程互不清扫对方的文件 |

### 5.2 为什么不能把清理交给框架（实测证据）

Starlette 1.6.0 的 multipart 解析器：

```python
spool_max_size = 1024 * 1024   # 1 MB —— 类属性，实测
```

**任何超过 1 MB 的文件字段都会 spool 到磁盘临时文件。** 一个 10 分钟的会议分段是 19 MB，
所以走 `UploadFile` / `File(...)` 的路径**必然落盘** —— 而且那个文件由框架创建、
由框架决定何时关闭，**我们的 `finally` 看不到它**。

`app/api.py:582/623/650` 的 `_save_upload_wav` / `stt_transcribe` / `stt_sentences`
就是这个形态。它们今天是环回接口，无所谓；**但绝不能照搬进服务端**。

### 5.3 `TempWorkspace`：唯一的临时文件入口

```python
# server/tmp.py
class TempWorkspace:
    """一次请求的临时文件工作区。所有落盘必须经由它。

    - 目录按实例命名空间隔离：<tmp_root>/<instance_id>/<request_id>/
    - 退出（含异常 / 客户端断开）时整目录删除
    - 删除失败进入重试队列并写 warn 日志（不静默）
    """
    def __enter__(self) -> "TempWorkspace": ...
    def __exit__(self, *exc) -> None: ...
    def path(self, name: str) -> str: ...        # 申请一个文件路径
    def write_stream(self, chunks) -> str: ...   # 流式落盘 + 边写边限长
```

配套三个兜底：

1. **启动清扫**：进程启动时删除 `<tmp_root>/<instance_id>/` 下所有残留（上次崩溃留下的）。
2. **TTL 兜底**：后台定时器扫超过 N 分钟未修改的文件并删除，写日志。
3. **自检端点**：`GET /v1/health` 返回 `tmp: {root, files, bytes, oldest}`。
   **数字不归零就是 bug 的信号** —— 运维一眼能看出泄漏。

### 5.4 协议上仍然建议用 raw body

即使临时文件合法，**仍建议音频用 `application/octet-stream` 原始 body**，不用 multipart：

| 理由 | 说明 |
|---|---|
| 所有权清晰 | 我们自己 `async for chunk in request.stream()` 写进 `TempWorkspace`，框架不碰 |
| 提前拒绝 | 先看 `Content-Length`，超限直接 `413`，**不读一个字节** |
| 边写边限长 | 没有 `Content-Length`（chunked）时，写超限即中止 |
| 客户端更简单 | 不用拼 multipart —— 现有 `providers/openai.py:134-149` 那 18 行手工 multipart 可以不要 |
| 少一个解析器 = 少一个攻击面 | 不解析边界，不担心 parser 的 spool 行为变化 |

参数（语言、模型、是否要时间戳）走 query string / header。

### 5.5 请求生命周期

```
1. 鉴权 + 配额检查（不读 body）
2. Content-Length / Content-Type 校验 → 不合格立刻 413/415
3. with TempWorkspace() as ws:
4.     流式接收 → ws 落盘（边写边限长）
5.     解码 + 重采样到 16k mono float32（可能再写一个引擎要的 wav）
6.     pool.acquire(model) → 推理（带超时）
7.     组装响应（纯数据，不含文件路径）
8. # __exit__ 删除整个工作区
9. 记审计日志（client_id / request_id / 端点 / 音频秒数 / 耗时 / 状态码）
```

**关键：响应里绝不返回文件路径** —— 那会把临时文件的生命周期泄漏给客户端。

---

## 6. HTTP 契约

### 6.1 端点白名单（全部无状态）

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/v1/capabilities` | 能力 + 模型 + `modelVersion` + `vectorSpaceId` + 限制 + 当前实际可用性 |
| GET | `/v1/health` | 存活（永远 200）+ 队列深度 + 显存 + 临时目录统计 |
| GET | `/v1/ready` | 就绪（模型池可服务则 200，否则 503） |
| POST | `/v1/asr` | 音频 → 文本（`?variant=short\|long`、`?timestamps=1`） |
| POST | `/v1/diarize` | 音频 → turns + 嵌入（`?mode=segment\|turns`） |
| POST | `/v1/speaker/embed` | 音频 → 嵌入 |
| POST | `/v1/tts` | 文本 → 音频（可选能力） |

**没有任何写端点。** `POST /v1/voiceprints` 这类一旦出现，护栏测试就该红。

### 6.2 `capabilities` 必须反映**真实**可用性

不能只声明"我支持 diarize"，还要声明此刻能不能用：

```jsonc
{
  "protocol": 1,
  "server": {"id": "gpu-01", "version": "1.0.0"},
  "limits": {
    "maxAudioSeconds": 1800,
    "maxUploadBytes": 67108864,
    "maxConcurrent": 2,            // 服务端总通道（暂定，实验后定）
    "perClientConcurrent": 1,      // 每客户端硬性 1（§3.6）
    "queueMax": 0                  // 不排队：满了直接拒
  },
  "busy": {                        // 客户端做上传前的乐观预检（§3.6）
    "activeRequests": 1,
    "perClientActive": 0,
    "retryAfterHint": 5
  },
  "capabilities": {
    "diarize": [{
      "id": "pyannote-3.1",
      "slot": "diarize.turns",
      "state": "ready",              // absent | loading | ready | failed | evicting
      "modelVersion": "pyannote-3.1-wespeaker-v1",
      "vectorSpaceId": "ws-resnet34-v1",
      "dim": 256,
      "maxConcurrency": 1,
      "queueDepth": 3
    }]
  }
}
```

客户端据此决定：`state != ready` 且 `estWait` 太长 → 直接走别的后端，不必白等。

### 6.3 错误模型：**机器可读**

客户端路由层要按**原因**决定降级。所以错误体必须带稳定 code：

```jsonc
{"code": "model_loading", "message": "diarize model is loading", "retryAfter": 30}
```

| HTTP | code | 客户端应做的事 |
|---|---|---|
| 400 | `bad_request` | 不重试（客户端 bug） |
| 401 | `unauthorized` | 刷新凭据；失败则标记后端不可用 |
| 403 | `forbidden` | 该 scope 未授权 → 跳过此后端 |
| **409** | **`client_busy`** | **不重试** —— 自己上一个请求还没完（并发应配 1，属客户端 bug，记日志） |
| 413 | `audio_too_long` / `payload_too_large` | 改分段重试；否则跳过 |
| 415 | `unsupported_media` | 换编码；否则跳过 |
| 429 | `quota_exceeded` | **今天别再试**（`Retry-After` = 到次日重置的秒数） |
| 429 | `queue_full` | 退避重试（**当前 `queue_max=0`，用不到**，占位见 §3.6） |
| **503** | **`server_busy`** | **系统忙，稍后再试** —— 退避重试，尊重 `Retry-After` |
| 503 | `model_loading` / `gpu_oom` / `model_failed` | **降级到链上下一个后端** |
| 504 | `inference_timeout` | 降级；并回报"此后端对这么长的音频不可用" |

**`409 client_busy` 与 `503 server_busy` 必须分开**（§3.6）：
前者的重试**永远不会成功**，后者重试才是对的。合并成一个码，客户端就只能盲目重试。

这张表要与前置文档 §5.2 的 9 类降级原因**一一对齐**（`unavailable` / `timeout` / `quota` /
`auth` / `privacy-denied` / `unsupported` / `vector-mismatch` / `quality-rejected`）。
契约不对齐，路由层就只能靠猜 —— 那等于没有降级逻辑。

### 6.4 长请求与进度

- `Accept: text/event-stream` → 推 `{"stage": "decode|infer|done", "progress": 0.4}`
- **连接状态只在内存**；客户端断开 → 中止推理（`Lease.__exit__` 释放）+ 清理临时文件
- 断了客户端整段重试，服务端不补偿 —— 这就是"无状态"的代价，已在幂等契约里说明
- **但 v1 其实不需要 SSE**，理由见 §6.5 的"进度"一段

### 6.5 逐端点功能规格

#### 公共约定（所有端点）

| 项 | 约定 |
|---|---|
| 方法 | 只有 `GET` 与 `POST`（无写端点，§6.1） |
| 音频 | `application/octet-stream`，**raw body**（理由见 §5.4） |
| 其它参数 | **query string** —— body 被音频占了，参数没地方放 |
| 认证 | `Authorization: Bearer <token>`，或 mTLS 证书（§7.1） |
| 协议版本 | `X-Echo-Protocol: 1` |
| 对账 | `X-Request-Id`（客户端给，服务端回显；没给就生成）——两边日志靠它对齐（§7.3） |
| 段落标识 | `X-Segment-Id`（如 `<会议名>#03`；服务端**只回显与记日志**，不解析） |

**音频规格**

| 项 | 约定 |
|---|---|
| 采样率 | 任意（服务端重采样到模型需要的），**推荐 16 kHz** |
| 声道 | 1 或 N；**N>1 时服务端下混为单声道**（会议录音可能多声道） |
| 位深 | `pcm_s16le` 为基准；也接受 `wav`、`opus` |
| 编码声明 | `Content-Type: audio/wav` / `audio/opus`；不声明按 PCM 处理 |
| 上限 | `limits.maxAudioSeconds` 与 `maxUploadBytes`，超了 `413` |

---

#### A. `POST /v1/asr` —— 音频 → 文本

**做什么**：一段音频转成文本，可选带句子级时间戳。

**参数**

| 参数 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `variant` | `short` / `long` | `long` | **语义档位**，映射到不同模型（见下） |
| `timestamps` | `0` / `1` | `0` | 是否要句子级时间轴 |
| `lang` | `zh` / `en` / `auto` … | `auto` | 语言提示 |
| `model` | 模型 id | — | **高级覆盖**：绕过 variant 直接点名 |

**两个 variant 的区别**（这是功能层面的关键）

| | `short` | `long` |
|---|---|---|
| 用途 | 指令增强（几秒音频） | 会议分段（10 分钟） |
| 模型 | 小模型（SenseVoice） | 大模型（Qwen3-ASR） |
| 常驻 | **是**（冷启动付一次） | 否（按需 + LRU） |
| 时间戳 | 通常无 | 有（可选 ForcedAligner） |
| 延迟目标 | < 300 ms | 秒级到几十秒 |

> **为什么第二个值叫 `long` 而不是 `meeting`**（2026-09-24 定）
>
> 这两个值是**服务端源码里的字面量**。而我们对 `server/` 有一条硬护栏：
> **源码里不出现业务词**（`meeting` / `command` / `summary` / `voiceprint`），
> 由 `tests/test_server_contract.py` 机械检查 —— 它是"服务端不认识业务"这条原则
> 唯一不靠人自觉的保证。
>
> 若把值写成 `meeting`，护栏只有两个出路：给这个文件开白名单，或者把它弱化。
> 前者等于在护栏上开第一个洞，后者等于拆掉护栏。都不如**换个中立的名字**。
> `short` / `long` 描述的是**音频长度档位**，本来就更贴近服务端看到的东西：
> 它只知道"这段音频要按短档还是长档处理"，不知道那是不是一场会议。
>
> 文档里说"会议转写引擎"是**给面板和人的话**（客户端自己的措辞）；协议里不出现。

**响应**

```jsonc
{
  "text": "……整段文本……",
  "sentences": [                       // timestamps=1 且模型支持时才有
    {"start": 0.31, "end": 4.92, "text": "……"}
  ],
  "timestamps": "exact",               // exact | none
  "modelId": "qwen3-asr-0.6b",
  "modelVersion": "qwen3-asr-0.6b-2025.09",
  "audioSeconds": 601.4,
  "durationMs": 8421
}
```

**三条语义约定**

1. **`timestamps` 只有 `exact` 与 `none`，服务端不编 `estimated`。**
   "按字数均摊"是**客户端**拿不到时间戳时做的兜底（现有 `_split_provider_text`），
   而且必须把 `estimated` 标进数据里 —— 那个标记属于客户端，
   服务端不该假装自己有精确时间戳。
2. **服务端不认识"会议"。** `variant=long` 只是"用哪个模型"的提示，不带业务含义（§1.2）。
   名字取 `long` 而非 `meeting`，正是为了让这条原则**在源码里也成立** —— 见上面的说明。
3. **不分段、不切句**（除模型自身能力）。一段进、一段文本 + 可选句子出；**切分是客户端的事**。

---

#### B. `POST /v1/diarize` —— 音频 → 说话人时间轴 + 嵌入

**做什么**：分段 + 嵌入 + 聚类。**这是全系统唯一产生说话人向量的地方。**

**参数**

| 参数 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `mode` | `segment` / `turns` | `segment` | 档 1 / 档 2（见下） |
| `maxSpeakers` | 整数 | — | 已知人数时的提示（**能显著提升准确率**） |
| `model` | 模型 id | — | 高级覆盖 |

**档 1 `segment`（默认，省流量）** —— 服务端**做段内聚类**：

```jsonc
{
  "modelVersion": "pyannote-3.1-wespeaker-v1",
  "vectorSpaceId": "ws-resnet34-v1",     // 客户端比较嵌入的唯一依据
  "dim": 256,
  "turns": [
    {"start": 0.31, "end": 4.92, "speaker": "S0"},
    {"start": 5.10, "end": 8.40, "speaker": "S1"}
  ],
  "speakers": {"S0": [/* 256 floats */], "S1": [/* 256 floats */]},
  "audioSeconds": 601.4,
  "durationMs": 14200
}
```

**档 2 `turns`（精修，流量大）** —— 服务端**只做分段 + 嵌入，不做聚类**：

```jsonc
{
  "modelVersion": "...", "vectorSpaceId": "...", "dim": 256,
  "turns": [{"start":.., "end":.., "speaker": "S0", "embedding": [/*256*/]}, ...]
}
```

**两条必须写进契约的话**

1. **`speaker` 是 local label，只在本次响应内有意义。** 这一段里的 `S0` 与下一个分段里的
   `S0` **没有任何关系** —— 跨段同一性是**客户端**做的（现有 `SpeakerRegistry`）。
2. **客户端绝不能把两个不同 `vectorSpaceId` 的结果混进同一场会议。** 这是 L5，不是建议。
   服务端的责任是**如实声明** `vectorSpaceId`；**拒绝混用**是客户端的责任。

---

#### C. `POST /v1/speaker/embed` —— 音频 → 嵌入

**做什么**：把一段音频变成说话人嵌入。**主要用途是"现场注册联系人"** ——
现在 `voiceprint.py` 只有 `enroll_from_meeting()`，入库的唯一路径是"在会议里改名为联系人"，
门槛太高。

**参数**

| 参数 | 取值 | 说明 |
|---|---|---|
| `count` | `1` / `N` | 音频里预期几个说话人；`N>1` 时返回 N 个嵌入（服务端自己聚） |
| `model` | 模型 id | 高级覆盖 |

**响应**

```jsonc
{
  "embeddings": [[/* 256 floats */]],
  "dim": 256,
  "vectorSpaceId": "ws-resnet34-v1",
  "modelVersion": "wespeaker-voxceleb-resnet34-LM-v1",
  "audioSeconds": 8.2
}
```

**一条边界**：**服务端只提嵌入，不做任何"这是谁"的判断。**
匹配、阈值、间隔门、库管理全在客户端（`VoiceMatcher`）——
库是敏感个人信息，而且阈值是每个客户私有的调参（前置文档 §5）。

**与 `diarize` 的关系**：`vectorSpaceId` **必须一致**，否则"现场注册的人"在会议里认不出来。
两者由**同一个模型**提供，服务端要保证这一点**并声明出来**。

---

#### D. `POST /v1/tts` —— 文本 → 音频（可选，低优先）

**v1 可以是空实现**，`capabilities` 里完全可以不声明它。

理由：客户端已有系统自带 TTS（SAPI / `say` / espeak，**零依赖**）与 edge-tts（纯 Python）。
服务端 TTS 只在"想统一音色"或"客户端没有可用音色"时才有价值。

---

#### 横切功能

**批量：不做批量端点，靠客户端并发**

会议有几十个分段，逐个 POST 会嫌啰嗦。但：

- **不做 multipart 批量** —— §5.2 的实测：框架会把大文件 spool 到磁盘
- **不做 `/v1/batch`** —— 请求体要装多段音频，等于自己发明一个容器格式

**做法**：客户端**逐段串行** POST —— **并发固定为 1**（服务端也强制每客户端 1，
见 §3.6）。被拒时按 §3.6 的退避策略重试。

**所以这里不再有"客户端的可配并发数"** —— 2026-09-23 定了"每客户端一个转写请求"，
客户端那一侧也就不需要并发旋钮了。原先写"默认 2–4 路并发"是按吞吐直觉推的，
与"每客户端 1 路"的公平性规则冲突，已作废。

**进度：v1 不需要 SSE**

一个 10 分钟分段的转写是秒级到几十秒，而**分段本身已经提供了天然的进度粒度** ——
客户端只需要知道"第 3 段完了、第 4 段在跑"。所以：

- **v1 只返回最终结果**
- SSE 留给"一次传两小时整场文件"那种场景 —— 而那个场景**已经被排除**
  （前置文档 §4.5：不做整场单请求，坚持分段）

**超时：三段都要有**

| 谁 | 配置 | 行为 |
|---|---|---|
| 服务端通道满 | — （**不排队，立刻拒**） | `503 server_busy` + `Retry-After`，客户端退避重试 |
| （留空给未来的 `queue_max>0`） | `queue_wait_timeout_s` | 默认不启用 |
| 服务端推理 | `inference_timeout_s`（默认 900） | 中止 + `504 inference_timeout` |
| 客户端 HTTP | 客户端定（要比服务端长） | 断开 → 服务端检测到并**中止推理 + 清临时文件** |

**最后一行是必须的** —— 否则用户取消了，GPU 还在为一个没人要的结果烧着。

---

#### 服务端功能边界（一眼表）

| 做 | 不做 |
|---|---|
| 音频 → 文本（+ 可选时间轴） | 不认识"会议 / 命令 / 纪要" |
| 音频 → 说话人时间轴 + 嵌入 | **不做跨段同一性**（客户端的） |
| 音频 → 说话人嵌入 | **不做身份判断/匹配**（客户端的） |
| 文本 → 音频（可选） | 不存任何东西（临时文件除外，§5） |
| 声明能力、版本、真实可用性 | 不做 job 队列 / 批量容器格式 / 分段 |

---

## 7. 鉴权、配额与多租户

### 7.1 身份模型：**传输层与应用层分开**

先把一个容易混的概念拆开 —— **"链路可信"和"这是谁"是两件事**：

| 层 | 解决什么 | 用什么 | 可替换？ |
|---|---|---|---|
| **传输层** | 链路加密、防窃听 | **TLS（强制，即使内网）** | 自签 / 内网 CA / mTLS |
| **应用层** | **这是哪个客户端**、能做什么、用了多少配额 | **`client_id` + 短期 JWT** | **不可替换** |

**为什么应用层身份永远是 `client_id`，而不是"证书即身份"**

用一个显式对比说明（这是原稿写错的地方 —— 曾把 mTLS 写成"推荐的身份模型"）：

| | 证书即身份（CN = client_id） | **`client_id` + JWT** |
|---|---|---|
| 配额 / 日志 / scope | 得从证书字段里解析 | 就是一个库字段 |
| **撤销** | CRL / OCSP，**慢且难自建** | **改一个 `token_version` 字段** |
| 轮换 | 重新签发证书 | 换 secret，客户端自动生效 |
| 要 PKI | **要**（单位 PKI 未必有，或很慢） | 不要 |
| 客户端实现 | 装证书 + 信任链 | 存一个字符串 |

所以 3.0 的选型是：

- **v1 = 配对码 → `client_id` + `secret` → 短期 JWT**（自包含，不依赖 PKI）
- **mTLS 是可选加固**，**不替代** `client_id`：它只多一道"网络层准入"，
  身份、配额、撤销仍然走应用层（否则撤销就退化成 CRL 问题）

**兜底的"每请求 HMAC 签名"也去掉** —— 它只在完全无法上 TLS 时才有意义，
而"无法上 TLS"的内网场景不该被支持（上行的是音频与声纹衍生数据）。

### 7.2 scopes 与配额

```
scopes:  asr | diarize | embed | tts
配额维度：并发数 / 单请求最大音频秒数 / 每日音频分钟数 / 队列优先级
```

**按槽分别限额与排队**（前置文档已定）：
`asr.short`（高频小请求）与 `diarize`（吃 GPU）分开队列，**避免高频小请求饿死长任务**。

**配额计数放内存，不每请求查库**（§8.5 的硬约束）：

| 数据 | 在哪 | 为什么 |
|---|---|---|
| 剩余分钟数 / 并发数 | **进程内计数**（滑动窗口） | 每请求查库会让库变成瓶颈 |
| 长期统计 | 定期由 `calls_rollup` 承接 | 给运营看，不给鉴权看 |
| 配额**上限** | 库（`clients` 表） | 改动少，可缓存 |

代价：多实例时计数不共享 → **同客户的并发配额按实例各算一份**。
如果这个不可接受，就按 §9.4 把该客户粘到固定实例（按 client_id 哈希一路上游）。
**这是"能力面不查库"换来的必然取舍，要写在明处。**

### 7.3 审计日志：只有元数据

```jsonc
{"ts":"...","request_id":"...","client_id":"cli-07","endpoint":"/v1/diarize",
 "audio_seconds":601.4,"bytes":19200000,"backend":"pyannote-3.1",
 "duration_ms":8421,"status":200,"queue_wait_ms":120}
```

**没有**音频、文本、嵌入、说话人数。两边用 `request_id` 对齐 ——
排障能对上，服务端侧无内容。这是"不存储"与"可排障"的平衡点。

`request_id` 优先取客户端传入的 `X-Request-Id`（没有则服务端生成并回传），
这样客户端能把它写进自己的业务日志（`db.add_log(source="capability", ...)`）。

### 7.4 客户端配对（pairing）：凭据怎么发出去

**问题**：客户端要拿到 `client_id` + `secret`。手工复制粘贴有两个毛病 ——
secret 会留在聊天记录里，而且管理员得一台台填。

**做法：一次性配对码**（管理面「新建客户端」时生成）

```
① 管理员在管理面新建客户端：填名字（"张三的办公本"）、scope、配额
   → 服务端生成一次性配对码（8 位、有效期 15 分钟、只存哈希）
   → 界面上只显示这一次（之后再也读不到）

② 用户在客户端面板输入配对码（或由安装脚本带参传入）
   POST /v1/pair  {code, clientName, clientVersion}
   → 校验：未过期 / 未用过 / 未撤销
   → 服务端生成 client_id + secret（**secret 只在这一个响应里出现**）
   → 配对码标记已用、绑定到该 client_id

③ 客户端把 client_id + secret 落到**本机**（`{echoBase}/data/`，权限收紧）
   之后用它们换短期 JWT（§7.1），不再拿 secret 直接调能力
```

**为什么比"复制 secret"好**

| | 复制 secret | 配对码 |
|---|---|---|
| 泄漏面 | secret 进了聊天记录 / 截图 | 只能用一次、15 分钟过期、用掉即废 |
| 绑定 | 无（谁拿到谁能用） | 绑到具体客户端名与版本 |
| 撤销 | 只能撤 secret | 既能撤客户端，也能作废未用的码 |
| 管理员负担 | 手工发 | 报一个码 |

**四条安全约定**

1. **`/v1/pair` 是唯一不需要既有凭据的能力端点**（它本来就是换凭据的）。
   所以要额外防滥用：**限速 + 失败计数 + 可选择"关闭配对"**（关掉后新机器进不来）。
2. **配对码与 secret 都只存哈希**；明文只在生成/下发那一次出现。
3. **配对绑机器，不绑人。** 一个客户端一份凭据 —— 撤销时"这台机器失去权限"
   比"这个人失去权限"更好操作，而且配额本来就按机器算。
4. **撤销必须立即生效。** 短期 JWT（1 h）意味着撤销最多滞后 1 小时，不可接受。
   所以 JWT 里带 `tokenVersion`、`clients` 表里也有一个：**撤销 = 版本 +1**；
   服务端校验时比对**进程内缓存**（不每请求查库，见 §8.5 最后一段）。

### 7.5 令牌、撤销与本地存储（完整生命周期）

#### 全部状态转换一张图

```
[未配对] ──配对码──▶ [已配对·持 secret]
                          │  每小时用 secret 换 JWT
                          ▼
                    [持 JWT] ──1h 过期──▶ 用 secret 再换（**不做 refresh token**）
                          │
   撤销 / 禁用 ───────────┴──▶ 下一个请求 401（ver 不匹配）
   secret 泄漏 ──管理员轮换──▶ 旧 secret 宽限 24h，客户端自动切新
```

**为什么不设 refresh token**：secret 本来就在客户端本地。
多一层 refresh token 只是把同一个东西存两份，复杂度换不到安全性。

#### ① 配对（一次，带外）

```
【管理面】管理员新建客户端：填 名称 / scopes / 配额
  → 服务端生成配对码，只存哈希，15 分钟过期
  → 界面给出**一个可直接粘贴的配对串**：
        echo://pair?host=gpu-01:8900&code=7K2M9QX4&fp=sha256:1f3a…
        （fp = 服务端证书指纹 → 见下"信任交换"）

【客户端】用户粘贴配对串
  POST https://gpu-01:8900/v1/pair   { code, clientName, clientVersion }
  ← 200 { clientId: "cli-07a3f2", secret: "…", serverName, protocol: 1 }
```

**配对串里带证书指纹，是一石二鸟。** 配对本来就是**带外**的（人工传递），
正好用来交换信任：客户端记下指纹，之后连这个后端就校验它（防中间人）——
**用户不需要去装自签证书的根**。这也是"配对"这件事除了发凭据之外的第二个价值。

#### ② 换令牌

```
POST /v1/token
  Authorization: Basic base64(clientId:secret)
← 200 { accessToken, expiresIn: 3600, scopes, quota }
```

**JWT 里放什么（关键）**

| 声明 | 内容 | 为什么 |
|---|---|---|
| `sub` | `client_id` | 一切配额/日志的挂载点 |
| `scopes` | 允许的槽 | 免得每请求查 scope |
| `ver` | 签发时的 `token_version` | **撤销靠它** |
| `exp` | 1 小时 | |
| `iat` / `jti` | 签发时间 / 令牌 id | 排障 |

**不放**：任何业务信息、配额余额 —— 余额必须实时读内存，签死在 token 里必然不准。

#### ③ 调能力时的校验（**全部在内存，不查库**）

```
① 验签名 + exp（允许 60 s 时钟偏差 —— 内网机器时钟未必准）
② client_id 在缓存里？不在 → 查库一次并缓存；查不到 → 401
③ 缓存里的 ver == token.ver ？不等 = 已撤销 → 401
④ disabled ？→ 403
⑤ scopes 含所需槽？→ 403
⑥ 并发 / 队列 / 日配额没超？→ 429
⑦ 放行 → `calls` 记录**异步写库**，不阻塞请求
```

#### ④ 撤销：能到什么程度

```
撤销 → clients.token_version += 1
     → （多实例）各实例靠轻量轮询发现：SELECT MAX(updated_at) FROM clients
     → 下一个请求 ver 不匹配 → 401
```

| 部署 | 撤销延迟 |
|---|---|
| 单实例 | **立即**（直接改内存缓存） |
| 多实例 | **≤ 5 秒**（轻量轮询） |

**多实例不做实例间广播** —— 那要引入消息通道，而 5 秒撤销延迟对办公场景够用。
**但这个延迟要如实写进文档，不能说成"立即"。**

#### ⑤ 轮换，而不是重新配对

secret 泄漏或例行轮换时，**不要让用户再跑一趟输配对码**：

```
clients 表：secret_hash / secret_rotated_at
            prev_secret_hash / prev_secret_expires_at    ← 宽限期
轮换 → 生成新 secret；旧的还能用 24 小时（prev_*）
     客户端下次换令牌时服务端回 X-Echo-Secret-Rotated: 1 + 新 secret
     → 客户端自动落盘新 secret、平滑切换，用户无感
```

#### ⑥ 客户端本地存什么（**这是客户端侧的安全边界**）

`{echoBase}/data/backend.json`：

| 字段 | 说明 |
|---|---|
| `baseUrl` | 后端地址 |
| `clientId` | |
| `secret` | **必须保护**：Linux/macOS `0600`；Windows 走 **DPAPI**（绑用户账户） |
| `certFingerprint` | 配对时记下，之后校验（防中间人） |
| `accessToken` / `expiresAt` | 可只放内存，或与上同权限落盘 |

**明文 secret 放普通文件是不够的** —— 这台机器上任何用户态程序都能读。
Windows 上至少要走 DPAPI（`CryptProtectData`）；这与"业务数据留客户端"是同一类纪律。

#### ⑦ 与"内网公共 ASR"的凭据**不要混为一谈**

| | ECHO 能力后端 | 内网公共 ASR |
|---|---|---|
| 身份 | 我们签发的 `client_id` + JWT | **别人家的凭据**（常见就是一把 API key） |
| 配对 | 我们的配对流程 | 没有，管理员手工填 |
| 审计 | 我们的 `calls` 表 | **无**（只有对方能看） |
| 撤销 | 我们的管理面 | 找对方管理员 |

所以别把它俩抽象成一套凭据模型 —— 在客户端里它们是**两份独立配置**：
`{echoBase}/data/backend.json`（我们的）与设置里的 `providerAsrBaseUrl/ApiKey`（别人的）。

---

## 8. 可观测性

### 8.1 两个探针要分开

| 端点 | 语义 | 何时非 200 |
|---|---|---|
| `/v1/health` | **存活**（liveness） | 几乎不失败；挂了才失败 |
| `/v1/ready` | **就绪**（readiness） | 模型池全 failed / 显存耗尽 → 503 |

混在一起会导致：模型加载中 → 被编排系统反复重启 → 永远起不来。

### 8.2 指标清单

```
每端点：QPS / p50 / p95 / 错误码分布
队列：深度 / 等待时长分布
模型：state / 加载次数 / 加载耗时 / 显存占用 / 命中率（复用 vs 新加载）
降级：各错误码计数（客户端侧也统计，两边对账）
临时文件：文件数 / 字节数 / 最老文件年龄
```

**最后一项是"不存储"的运行期体检指标。** 它不归零就说明有泄漏。

### 8.3 日志规范

| 应该记 | 不该记 |
|---|---|
| `request_id / client_id / endpoint / 音频秒数 / 耗时 / 状态码 / 队列等待` | 音频、转写文本、嵌入、请求体 |
| `model_id / 加载耗时 / 显存` | 说话人名、会议名 |
| `evict / oom / timeout / 降级原因` | 任何用户内容 |

> 反例警示：客户端 `app/assistant.py:349` 写着
> `db.add_log("info", "assistant", f"识别: {text[:60]}（{note}）")`。
> 这在客户端是合理的（本机日志），**照搬到服务端就是泄漏**。

### 8.4 管理面（Admin Console）

**一句话**：服务端自带一个管理网页，给运维看**模型跑得怎么样、客户端有谁在连、谁调了什么**。
它读的是 §8.2 的指标与 §7.3 的审计数据，**不读任何内容**。

#### 与能力面严格隔离

| | 能力面 | 管理面 |
|---|---|---|
| 路径 | `/v1/*` | `/admin/*`（页面）+ `/admin/api/*` |
| 鉴权 | 客户端凭据（mTLS / `client_id:secret`→JWT） | **管理员用户名 + 密码** → 会话 |
| 端口 | 对客户端网段开放 | **建议独立端口**，只对运维网段开放 |
| 状态 | **仍然无状态**（可水平扩） | 有状态（会话 + 读写库） |
| 内容 | 只过音频与结果 | **只有元数据，永不显示内容** |

**建议独立端口的理由**：防火墙规则一条就够
（`客户端网段 → :8900`、`运维网段 → :8901`），比在同一个端口上做路径级 ACL 可靠。
也让"管理面能不能水平扩"这个问题变得无关紧要。

#### 五个页签

**① 概览**

```
能力后端 gpu-01 · v1.0.0 · 运行 3d 12h
当前并发 3 / 4     队列 2     今日音频 412 分钟
成功率 99.2%       p95 8.4s   活跃客户端 7      就绪 ✓
```

**② 模型** —— 就是 §4.5 里那两张卡的放大版 + 历史统计

| 模型 | 槽 | 状态 | 常驻 | 显存 | 加载耗时 | 并发 | 队列 | 累计调用 | 平均耗时 | 操作 |
|---|---|---|---|---|---|---|---|---|---|---|
| `sensevoice` | `asr.text` | ● 在线 | 是 | 1.8 GB | 15.2 s | 1/2 | 0 | 12,483 | 0.21 s | 重载 / 卸载 |
| `qwen3-asr-0.6b` | `asr.timestamps` | ○ 空闲 | 否 | 3.9 GB | 31.4 s | 0/1 | 0 | 312 | 8.4 s | 加载 / 卸载 |
| `pyannote-3.1` | `diarize.turns` | ○ 空闲 | 否 | 2.6 GB | 42.0 s | 0/1 | 2 | 88 | 14.2 s | 加载 / 卸载 |

操作就是客户端面板那两个按钮 —— §4.5 已经确认这是同一套状态机，不是新东西。

**③ 客户端** —— 配对与鉴权管理

| 客户端 | 状态 | 版本 | 最后活跃 | 当前并发 | 今日调用 | 今日音频 | 配额 | 操作 |
|---|---|---|---|---|---|---|---|---|
| 张三的办公本 | ● 在线 | 3.0.0 | 12 s 前 | 1 | 342 | 121 min | 500 min/天 | 撤销 / 轮换 / 改配额 |
| 会议室主机 | ● 在线 | 3.0.0 | 3 s 前 | 2 | 1,204 | 890 min | 不限 | … |
| 李四的笔记本 | ○ 离线 | 2.9.1 | 6 天前 | 0 | — | — | 500 min/天 | … |
| 未配对 | — | — | — | — | — | — | — | 显示有效配对码 + 剩余时间 |

**操作**：新建（生成配对码）/ 撤销 / 轮换 secret / 改 scope 与配额 / 禁用。
撤销立即生效靠 JWT 里的 `tokenVersion`（§7.4）。

**④ 调用日志** —— 排障主战场

按 `client / 端点 / 状态码 / 时间范围 / request_id` 过滤。每行：

```
时间       客户端        端点               模型        音频   排队   耗时    状态
12:04:11  张三的办公本   POST /v1/asr       qwen3-asr   601s   0.2s   8.4s    200
12:04:09  会议室主机     POST /v1/diarize   pyannote    601s   1.8s   14.2s   200
12:03:55  李四的笔记本   POST /v1/asr       —           —      —      —       503 model_loading
```

**只有元数据，没有内容。** 点开一行能看到：完整错误码、队列等待、是否命中熔断/降级、
以及关联的 `request_id`（**用它去客户端日志里查那条业务记录**）。

> 这正是 §7.3"两边用 `request_id` 对齐"的兑现处：
> 服务端这行说"12:04:11 那一段耗时 8.4 秒、成功"；
> 客户端那边用同一个 `request_id` 说"那是《周三例会》第 3 段"。
> **合起来能排障，分开都不越界。**

**⑤ 统计** —— 运营对账用

- 按客户端：调用数 / 音频分钟 / 错误率 / 平均延迟
- 按端点：QPS / p50 / p95 / 错误码分布
- 按模型：加载次数 / 平均推理耗时 / 显存峰值
- 时间序列：小时 / 天

#### 一个必须做的页面：「存了什么」自证页

管理面里放一页，把 §8.5 的白名单**渲染成实际状态**：

```
服务端数据库：echo-server.db（12.4 MB）
✅ admin_users      3 行   管理员账号（密码哈希）
✅ clients          7 行   客户端凭据（secret 哈希）
✅ pairing_codes    0 行   有效配对码（用掉即删）
✅ calls       48,213 行   调用元数据 —— 列：ts/client/endpoint/model/…（无内容列）
✅ model_events    216 行  模型加载 / 卸载 / 失败
❌ 不存在任何承载内容的表
最后自检：2026-09-23 12:04  通过
```

**价值**：客户/合规问"你们服务端到底存了什么"时不用解释，指给他看。
这一页自己也要有护栏测试（§12）—— **页面行数与实际库不一致就是 bug**。

#### 管理面自己的安全

| 项 | 要求 |
|---|---|
| 密码存储 | **argon2id**（或 bcrypt），绝不明文/可逆 |
| 会话 | 短期 cookie（HttpOnly + Secure + SameSite=Strict），**不用 localStorage** |
| 登录限速 | 失败计数 + 指数退避 + 锁定 |
| CSRF | 管理面**只做同源**，改动用 POST + CSRF token |
| 审计 | **管理动作也要记**（谁在什么时候撤销了哪个客户端） |
| 初始账号 | 首次启动生成随机密码并**打到 stdout 一次**，强制首次登录改密；**不设默认密码** |

---

### 8.5 服务端到底存什么（边界，替代原来的"没有 DB"）

**判据一句话**：存**关于请求的元数据**和**关于客户端的管理数据**；
不存**请求的内容**，也不存任何业务概念。

#### 表白名单（唯一白名单，多一张都要走评审）

| 表 | 存什么 | 关键列 |
|---|---|---|
| `admin_users` | 管理员账号 | `username` / `password_hash` / `disabled` / `last_login` |
| `clients` | 客户端注册与凭据 | `client_id` / `name` / `secret_hash` / `scopes` / `quota` / `token_version` / `last_seen` / `version` / `disabled` |
| `pairing_codes` | 待用的配对码 | `code_hash` / `expires_at` / `created_by`（**用掉即删**） |
| `calls` | 调用元数据 | `ts` / `client_id` / `endpoint` / `model_id` / `audio_seconds` / `queue_wait_ms` / `duration_ms` / `status` / `error_code` / `request_id` |
| `calls_rollup` | 小时/天聚合 | `bucket` / `client_id` / `endpoint` / `count` / `errors` / `p50` / `p95` / `audio_seconds` |
| `model_events` | 模型生命周期 | `ts` / `model_id` / `event`(load/evict/fail) / `duration_ms` / `vram_mb` |
| `admin_audit` | 管理动作 | `ts` / `admin` / `action` / `target` |

#### 列黑名单（任何表都不许有）

```
text  transcript  content  body  audio  wav  embedding  vector
speaker_name  meeting  command  summary  voiceprint  prompt  reply
```

护栏测试直接扫 `PRAGMA table_info` 的列名，**命中就红**。

#### 三件与"不存储"有关的具体约束

1. **`calls` 会涨，必须有保留策略**：默认 90 天，再由 `calls_rollup` 承接长期统计。
   rollup 表是纯计数与分位，**不含单条记录**。
2. **`request_id` 是关联键，不是内容** —— 它本身不泄漏任何东西，
   但它是服务端与客户端对账的**唯一**手段，**不能去掉**。
3. **`last_seen` / `version` 属于运营数据**，可以存；
   但**不许存客户端的业务状态**（比如"这台机器正在录哪场会议"）。

#### 与多实例的关系（这次修订带来的新取舍）

有了库之后，**"服务端无状态"要拆成两句**：

| | 能力面 | 管理面 + 库 |
|---|---|---|
| 状态 | **仍然无状态**（请求自带上下文） | 有状态（会话 + 读写库） |
| 水平扩 | 随便加实例、不需要共享存储 | **需要共享库**，或只在一个实例上开管理面 |
| 对能力面的影响 | — | **能力面绝不每请求查库** |

**关键约束：能力路径上不能每请求查一次库**，否则库就成了能力面的单点与瓶颈。
凭据校验与配额计数走**进程内缓存 + 事件驱动刷新**（撤销靠 `token_version` 推送）。
这是这次修订**唯一**真正削弱了原设计的地方，必须记在明处。

---

## 9. 部署与运维

### 9.1 宿主系统：要用 Docker 的话，"容器里就是 Linux"

**关键推论**：Docker 在 Windows 上跑的**也是 Linux 容器**（WSL2 后端）。
所以在"要用 Docker"这个前提下，Windows / Linux 的选择**只影响宿主，不影响我们的代码**。

**结论：代码只需支持 Linux 容器一套。**
不为 Windows 原生写第二套 —— 这是这次决定省下的最大一块工作量。

| 方案 | 宿主 | 容器 | GPU 直通 | 评价 |
|---|---|---|---|---|
| **A. Linux + Docker** | Linux | Linux | `--gpus all` + nvidia-container-toolkit，**成熟** | ⭐ **推荐** |
| **B. Windows + Docker Desktop** | Windows | Linux（WSL2） | 驱动装在 Windows、WSL2 透传；`--gpus all` 同样可用，但多一层、性能略损 | 可行 |
| C. Windows 原生 | Windows | 无 | 无直通问题，性能最好 | **不推荐**：丢掉只读 rootfs 这层保护，还要为 Windows 单独适配（信号、coredump、路径…） |

**如果单位只能给 Windows 机器** → 选 B。代价是 GPU 性能通常比裸 Linux 低 **5–15%**（WSL2 的 CUDA 层），
外加一层排障面。

### 9.2 GPU 直通：不是难点，但要配对

**Linux（三件事）**：

```bash
# ① 宿主装 NVIDIA 驱动
# ② 装 nvidia-container-toolkit
# ③ 跑容器时加 --gpus all
docker run --gpus all ...
```

容器里 `torch.cuda.is_available()` 即为真。

**Windows + Docker Desktop（两个坑）**：

- NVIDIA 驱动装**在 Windows 上**，**不要**装进 WSL 里
- Docker Desktop 必须用 **WSL2 后端**（不是 Hyper-V）
- `--gpus all` 同样可用；`nvidia-smi` 在容器里可能显示不全（**不影响推理**）

**所以 GPU 直通本身不难**，难的是"Windows + Docker"这一层 —— 这也是推荐 A 的原因。

### 9.3 容器怎么配：只读根 + 一个可写临时卷

```bash
docker run -d --name echo-backend \
  --gpus all \
  --read-only \                                # 根文件系统只读
  -v /srv/echo/tmp:/var/echo/tmp \             # ★ 唯一可写的地方
  -v /srv/echo/models:/opt/echo/models:ro \    # 模型只读
  -v /srv/echo/db:/var/echo/db \               # 管理面的库（很小）
  -p 8900:8900 -p 8901:8901 \
  -e ECHO_TMP_ROOT=/var/echo/tmp \
  echo-backend:1.0.0
```

**`--read-only` 仍是这里最有价值的一条**：它让"不小心把音频写到某个目录"**直接报错**，
而不是静默发生。临时目录成了**唯一**可写的地方 —— **审计范围因此缩小到一个目录**。

> 修订（2026-09-23）：原稿写的是 `--tmpfs /tmp`（全内存）。按"临时文件落盘 + 定时清理"
> 的决定改成**可写卷** —— 更简单，而且**对性能几乎没有影响**（见 9.4）。

### 9.4 临时文件：按日期落盘 + 定时清理

**决定（2026-09-23）**：不搞内存盘、不搞容量硬门禁 —— **按日期落盘，定时清理**。

```
{tmp_root}/
├── 2026-09-23/
│   ├── <request_id>/seg.wav
│   └── …
└── 2026-09-22/          ← 整目录删，比逐文件快
```

**必须保留的三条**（少一条就破"不存客户数据"）：

| # | 要求 | 为什么不能省 |
|---|---|---|
| **1** | **请求结束立即删**（`finally`） | 这是**第一道、也是最重要的一道**；定时清理只是兜底 |
| **2** | **定时清理**（每小时扫一次，删超过 4 小时的） | 崩溃 / 断电的残留靠它收 |
| **3** | **日志里没有内容** | **零成本**，没有理由不做 |

**放宽掉的三条**（原来的过度设计）：

| 原设计 | 放宽为 | 理由 |
|---|---|---|
| `TempWorkspace` 上下文管理器 + 启动清扫 | 直接按日期目录写 | 定时清理能覆盖崩溃残留（最多留 1 小时） |
| 容量硬上限（超了拒请求） | 清理时顺带看总大小，超阈值**先删最老的** | 不引入新的拒绝路径 |
| tmpfs / `/dev/shm` | **落盘** | 见下 |

**为什么落盘不牺牲性能**：一段 10 分钟音频是 19 MB，写一次、读一次。
相对于转写本身的几秒到几十秒，**磁盘 IO 是噪声级**。真正影响性能的是 GPU，不是这块盘。

**一个免费的 Linux 优化（零代码改动）**：把 `tmp_root` 指到 `/dev/shm` 即可白拿内存盘。
**默认不这么做**（保持"落盘 + 定时清理"的简单默认），但如果实测发现 IO 是瓶颈，改一个配置项就行。

**建议**：临时目录放 **SSD**，别放网络盘 / 机械盘。

### 9.5 反向代理（最容易破的一环）

```nginx
proxy_request_buffering off;        # 别把大 body 缓到磁盘
proxy_max_temp_file_size 0;         # 禁止代理临时文件
client_max_body_size 64m;           # 与 maxUploadBytes 对齐
proxy_read_timeout 1800s;           # 长请求
proxy_buffering off;                # 流式响应必须
# 若确实开了代理临时文件，指到我们的临时根，便于统一清理
client_body_temp_path /var/echo/tmp/nginx;
# access_log 不含 $request_body
```

**漏了这段，应用层再干净也没用** —— 音频会被 nginx 缓存到磁盘，而应用层完全看不见。

### 9.6 单实例 vs 多实例（真实取舍）

无状态 → 多实例可以直接轮询扩。**但模型权重不共享**：

- 每个实例各自加载模型 → **显存 × 实例数**
- 所以单卡上"多实例"通常是**负优化**

| 场景 | 方案 |
|---|---|
| 单卡、显存够 | **单实例**，靠 EnginePool 内部并发（v1） |
| 单机多卡 | 每卡一个实例，端口不同，前置负载均衡**按模型亲和** |
| 多机 | 按**能力**分实例：一台专做 `asr`，一台专做 `diarize`/`embed`；客户端按槽分别配置地址 |

最后一行很重要：**因为路由是"按槽选后端"，客户端天然支持"asr 在这台、diarize 在那台"** ——
不需要服务端做集群。这是把复杂度留在客户端编排的又一个好处。

### 9.7 启动与退出

- **启动**：先起 HTTP（`/v1/health` 立刻 200），再**后台预热常驻模型**（复用 `boot.py` 的分阶段编排）
- **退出**：`pool.shutdown()` 释放显存 + 等在飞请求结束（有超时）。
  **临时文件不在这里清** —— 交给定时清理，免得退出被慢盘拖住
- **崩溃**：定时清理兜底

### 9.8 换模型的纪律：把"尽量统一"变成机制

**决定（2026-09-23）**：服务端**不轻易换模型**，效果稳定后尽量统一。

这是**好消息**：`vectorSpaceId` 稳定 ⟹ 客户端声纹库不会莫名失效，
会议级向量空间锁定（L5）**实际不会被触发**。

但"人不换"不等于"机制不让换"，所以要三道闸（成本都很低）：

| 闸 | 做什么 |
|---|---|
| **① 声明** | `capabilities` 里如实报 `vectorSpaceId` 与 `modelVersion`（**已有**）→ 客户端能**发现**变化 |
| **② 显式操作** | 改模型**不是改一行配置就生效** —— 必须在管理面确认，且**重启才生效** |
| **③ 提示后果** | 确认时明说：**"这会改变向量空间，已有声纹库需要重新入库（旧样本保留、但不参与匹配）"** |

这样"尽量统一"就有**机制保障**，而不是靠记性。

---

## 10. 服务端配置

独立 YAML + 环境变量覆盖。**不读客户端的 settings 表**。

```yaml
server:
  id: gpu-01
  listen: 0.0.0.0:8900
  tls: {cert: /etc/echo/server.crt, key: /etc/echo/server.key}
  instance_id: gpu01-a            # 临时目录命名空间

models:
  root: /opt/echo/models
  vram_budget_mb: 20000
  resident: [speaker-embed, asr-short]
  specs:
    # ── 与现网一致：沿用已在实机验证效果的两个引擎，服务端不要换 ──
    # 对应面板「命令转写引擎（常驻）」与「会议转写引擎（按需）」两张卡。
    - id: asr-short                    # 面板：命令转写引擎（常驻）
      slot: asr.text
      impl: sensevoice                 # funasr SenseVoiceSmall
      resident: true                   # 常驻：实测加载 ~15 s，冷启动付一次
      max_concurrency: 2
      device: cuda:0
    - id: asr-long                     # 面板：会议转写引擎（按需）
      slot: asr.long
      supports: [asr.text, asr.timestamps]   # 一个模型同时给文本与句级时间戳
      impl: qwen3asr                   # Qwen3-ASR-0.6B (+ ForcedAligner)
      resident: false                  # 按需 + LRU：显存约 4 GB
      max_concurrency: 1
    - id: pyannote-3.1
      slot: diarize.turns
      max_concurrency: 1            # 非线程安全
      vectorSpaceId: ws-resnet34-v1 # 换模型 = 换这个 id = 重启
    - id: speaker-embed
      slot: speaker.embed
      modelVersion: sherpa-campplus-zh-v1
      vectorSpaceId: ws-campplus-zh-v1

limits:
  max_audio_seconds: 1800
  max_upload_bytes: 67108864
  # 并发：服务端总通道 2（暂定，实验后定）；每客户端硬性 1（§3.6）
  max_concurrent: 2
  per_client_concurrent: 1
  # 不排队：通道满了立刻拒（503 server_busy + Retry-After），重试由客户端负责
  queue_max: 0
  busy_retry_after_s: 5
  # 仅当上面 queue_max > 0 时才生效（v1 不启用）
  queue_wait_timeout_s: 120
  inference_timeout_s: 900

tmp:
  root: /dev/shm/echo
  max_bytes: 4294967296
  ttl_s: 600
```

---

## 11. 失败模式总表

| 失败 | 服务端行为 | 客户端应对 |
|---|---|---|
| 服务端不可达 | — | 降级链下一个后端 |
| **该客户端自己已有请求在跑** | **`409 client_busy`**（不排队） | **不重试** —— 并发应配 1，属客户端 bug，记日志 |
| **服务端通道满**（总 2 路） | **`503 server_busy` + `Retry-After: 5`**（**不排队**） | **系统忙，稍后再试**：退避重试（退避作用于**整个队列**） |
| 模型加载中 | `503 model_loading` + `Retry-After` | 等或降级（看 `estWait`） |
| 加载失败 | `503 model_failed`；`/v1/capabilities` 标 `failed` | 跳过此后端，直到 capabilities 变化 |
| 显存不足 | 卸载 LRU → 仍不够 → `503 gpu_oom` | **绝不回退 CPU**；降级 |
| （`queue_max>0` 时才会有）队列满 | `429 queue_full` + `Retry-After` | 退避重试 —— **v1 用不到** |
| 配额用尽 | `429 quota_exceeded` | 提示用户 / 按日重置（**今天别再试**） |
| 音频过长 | `413 audio_too_long` | 客户端重新分段 |
| 推理超时 | `504 inference_timeout` | 降级（并记住"此后端不适合这么长的音频"） |
| 客户端断开 | 中止推理 + 清临时文件 | 整段重试（幂等在客户端） |
| 服务端崩溃 | 启动时清扫残留临时文件 | 重试；`request_id` 用于对账 |
| 删除临时文件失败 | 登记 + 重试 + `warn` 日志；`/v1/health` 可见 | — |

---

## 12. 护栏测试清单

**表里写的是"断言"，不是"文件名"。** 这些断言全部落在 `tests/test_server_contract.py`
一个文件里（按断言意图分组成若干 `TestCase`）—— 拆成 22 个文件只会让人不去跑它们。
「状态」列的 ✅ 表示 v1 骨架已实现，⏳ 表示等对应功能落地时补（见 §13 阶段）。

| 断言 | 状态 | 落在哪 |
|---|---|---|
| `server/` 不 import `app.db` 等客户端业务层 | ✅ | `NoBusinessCouplingTests.test_no_business_imports` |
| `server/` 源码不出现 `meeting`/`command`/`summary`/`voiceprint` | ✅ | `NoBusinessCouplingTests.test_no_business_words_in_source` |
| 路由表里没有写端点（白名单外） | ✅ | `NoBusinessCouplingTests.test_only_the_documented_routes_exist` |
| 每个错误码都带 `code` + 能不能重试（契约对齐） | ✅ | `ErrorContractTests` |
| `409 client_busy` 与 `503 server_busy` 是两回事，不混用 | ✅ | `ErrorContractTests.test_client_busy_and_server_busy_are_different` |
| 两级闸门：每客户端 1 → `409`；全局 2 满 → `503` + `Retry-After` | ✅ | `AdmissionTests` + `AdmissionWiringTests` |
| `queue_max=0`：满了**立即拒绝**，不留堆积请求 | ✅ | `AdmissionTests` |
| 异常路径也要**还回槽位**（一次失败不许把服务端锁死） | ✅ | `AdmissionTests.test_slots_are_released_on_exception` |
| 鉴权关 = 所有人 `anonymous`（谁都能用 GPU，所以启动要吼一声） | ✅ | `AdmissionWiringTests.test_auth_off_means_everyone_is_anonymous` |
| 一次 `load()` 不许污染全局默认配置 | ✅ | `test_config_is_not_shared_between_loads` |
| 请求成功与失败，两种路径都清干净临时目录 | ✅ | `TempWorkspaceTests.test_success_and_failure_both_clean_up` |
| 超龄文件被清扫删掉，新鲜的留着 | ✅ | `TempWorkspaceTests.test_sweep_removes_old_and_keeps_fresh` |
| 超过 `max_bytes` 时按最旧优先删，**不写爆盘** | ✅ | `TempWorkspaceTests.test_sweep_by_size_removes_oldest_first` |
| 显存不足 → 拒绝（`gpu_oom`），**不是** OOM、**不是**悄悄用 CPU | ✅ | `EnginePoolTests.test_vram_budget_refuses_instead_of_oom` |
| 加载失败如实报 `model_failed`，不静默回退 CPU | ✅ | `EnginePoolTests.test_load_failure_does_not_fall_back_to_cpu` |
| 设备判据**按 impl 分开问**：sensevoice/qwen3asr/pyannote 看 torch，whisper 看 ctranslate2 | ✅ | `DeviceAssertionTests` |
| 向量类加载器**也**查设备（pyannote 内部那句静默 CPU 回退必须够不到） | ✅ | `DeviceAssertionTests.test_vector_loaders_actually_assert_the_device` |
| 新加 impl 必须显式声明运行时，不许靠默认值蒙 | ✅ | `DeviceAssertionTests.test_every_built_impl_has_a_declared_runtime` |
| 显式 `device: cpu` 是**接受** CPU，不该被拦 | ✅ | `DeviceAssertionTests.test_cpu_specs_are_never_blocked` |
| 并发要同一个模型 → loader 只被调用一次（单飞） | ✅ | `EnginePoolTests.test_single_flight` |
| 推理中引用计数 > 0 的模型不被卸载 | ✅ | `EnginePoolTests.test_refcount_blocks_unload` |
| LRU 驱逐最久未用的，**常驻的永不驱逐** | ✅ | `EnginePoolTests.test_lru_evicts_idle_but_never_resident` |
| 模型状态如实上报（`absent`/`ready`/`failed`），不撒谎 | ✅ | `EnginePoolTests.test_status_reflects_real_state` |
| 铁律 L5：所有产出向量的模型共用**同一个** `vectorSpaceId` | ✅ | `VectorSpaceFrozenTests` |
| 版本/向量空间在进程内**冻结**（`ModelSpec` 不可变） | ✅ | `VectorSpaceFrozenTests.test_model_spec_is_immutable` |
| 客户端拿得到 `vectorSpaceId`（否则它没法判断"能不能比"） | ✅ | `EndpointTests.test_capabilities_reports_slots_and_vector_space` |
| 不要时间戳时**不许**编一个 `estimated` 出来 | ✅ | `EndpointTests.test_asr_returns_text_and_no_fake_timestamps` |
| 没实现的能力**如实拒绝**，不假装（`mode=turns`） | ✅ | `EndpointTests.test_diarize_turns_mode_is_refused_not_faked` |
| 超长音频在读之前就被拒（不是读完再拒） | ✅ | `EndpointTests.test_declared_oversize_is_refused_before_reading` |
| 跑完请求无 `.db` 文件生成 | ⏳ | 等鉴权/审计落库时一起查（v1 没有任何落库路径） |
| 跑一次请求后日志 grep 不到输入文本/嵌入 | ⏳ | v2 上 metrics/日志时补 |
| `loading` 期间 `capabilities` 标 `loading`，不是撒谎说 ready | ⏳ | 需要能卡住 loader 的假引擎 |
| 客户端中途断开后临时目录为空 | ⏳ | 需要真 ASGI 断连才能测 |
| 预置残留文件 → 启动时清空（sweep on startup） | ⏳ | `Sweeper` 已实现，用例待补 |
| 只读 rootfs + tmpfs 下跑通全部端点（容器冒烟） | ⏳ | v3，且只能在 Linux 上跑 |
| 客户端引擎层内部那句 `except -> CPU` 也要能被关掉（`allow_cpu_fallback=False`） | ⏳ | 客户端侧改动：现在只拦得住"运行时说没 CUDA"，拦不住"运行时说能用、建模型时炸了" |

> **为什么 §12 值得这么细。** 前面每一节的设计都有"如果没人看着就会退化"的地方：
> 服务端会慢慢认识业务、临时文件会慢慢漏、GPU 会慢慢被 OOM 掉。
> 这份清单是把那些"慢慢"变成"立刻红"。


---

## 13. 实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| **v1** | 单进程 / 单机 / 静态 token；`EnginePool`（常驻 + 按需 + 单飞 + LRU）；`TempWorkspace`；4 个能力端点；`/health` `/ready` `/capabilities` | §12 全部护栏测试绿；两个客户端并发不互相阻塞 |
| **v2** | JWT + scopes + 配额 + 机器可读错误码契约 + metrics + SSE 进度 | 与客户端路由层的降级原因**逐条对齐**；断网/降级演练 |
| **v3** | 只读 rootfs 容器 + tmpfs + 反代配置 + 按模型分进程（按需）+ 多实例按能力拆分 | 容器冒烟；`/v1/health` 的临时目录统计长期归零 |

**v1 就能满足"办公本可用"的全部需求** —— 办公本的会议转写、说话人分离、声纹都只需要 v1 的能力。

---

## 14. 待拍板

1. **服务端跑不跑 GPU 的 pyannote 4.x？** 若跑，服务端就有两套分离实现（pyannote 与
   sherpa-onnx ONNX），`vectorSpaceId` 会有两个。**建议：服务端只提供一套并明确声明它的
   `vectorSpaceId`**，另一套只在客户端本地。多一套的收益远小于"两个向量空间"带来的复杂度。
2. **`asr.short`（指令增强）要不要开？** 它会给服务端引入高频小请求。开了就要独立队列 +
   独立配额（前置文档已设计），不开则路由表里少一行。**建议 v1 不开**，先只服务会议链路。

   > **v1 现状 ≠ 这条建议（2026-09-24，需要拍板）。** v1 骨架里**没有配额机制**
   > （配额随 v2 的 JWT/scopes 一起做），所以"默认配额为 0"这句话今天是**没有执行者**的：
   > `variant=short` 现在**是通的**，由常驻的 `asr-short`（SenseVoice）服务。
   >
   > 而且 `asr-short` 的 `resident: true` 在当前实现里是**纯开销** —— 文档给的理由是
   > "会议档也用 SenseVoice"，但本实现的 `asr-long` 走的是 qwen3asr，没有任何内部调用方。
   > 于是现在这一档"又占着显存、又对外开着"，与建议正好相反。
   >
   > 三条路，选一条（**建议第 3 条**，它最小且不会说谎）：
   > 1. 真的实现配额（`specs[].quota` + 准入时校验）→ 最贴原设计，但属于 v2 工作量；
   > 2. 给 spec 加 `served: false`（模型留着但不对外路由）→ 语义清楚，但是**新机制**，
   >    不是文档里写的那个；
   > 3. **v1 就把 `asr-short` 从出厂清单里去掉**，`variant=short` 让 `asr-long` 通过
   >    `supports: [asr.text, ...]` 兜住（短请求只是"用大模型跑几秒音频"，慢一点但正确）。
   >    等真要做指令增强时再把小模型加回来 —— 那时配额机制也一起有了。
3. **TLS 用什么形态？** mTLS（内网证书，需 PKI）还是自签 + `client_id:secret`→JWT
   （自包含，无外部依赖）。**建议 v1 用后者**，把 mTLS 留给单位 PKI 就绪之后。
4. **两级闸门在收完音频之后才判"忙"，是不是太晚？**（2026-09-24 实现时发现）
   现在的顺序是 `收音频 → 转 wav → 抢闸门 → 推理`：忙的判定发生在**上传完成之后**。
   好处是"慢上传不占着 GPU 通道"（通道是给推理的，不是给网络的）；
   坏处是一个客户端可以同时开很多条上传，**每条都把最多 64 MB 落盘**，
   然后才被 `409 client_busy` 顶回来 —— `tmp` 的容量上限会兜住盘，但那是一次真实的写放大。
   **建议：在收音频之前先做一次"便宜的预检"**（这个 client 或全局是否已经满了 → 立刻拒，
   不读 body），真正的槽位仍然只在推理前后持有。这样"满了立刻说系统忙"（用户的原始要求）
   才在**字节层面**也成立，同时不改变"通道只归推理"的语义。
