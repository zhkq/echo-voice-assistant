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
| 不记录内容（音频/文本/嵌入）到日志 | 日志是最容易泄的地方 |
| 不认识"会议"这个概念 | 服务端只认识槽 |

### 1.3 四条禁令 → 四条护栏测试

沿用本仓库"铁律 + 测试钉住"的做法：

```
server/ 目录源码里不出现：meeting / command / summary / voiceprint
server/ 不 import：app.db、app.config.settings、app.meeting、app.assistant
服务端路由表：没有任何写端点（POST/PUT/DELETE 只在 §6 白名单内）
跑完一次请求：工作目录无新增文件、临时目录为空、日志 grep 不到输入内容
```

**"不存储业务数据"必须是能被测试钉住的属性，不是承诺。**

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

### 3.3 v1：单进程 + EnginePool + 每模型信号量 + 有界队列

```
                    ┌─────────────── ECHO server (单进程) ───────────────┐
  N 个客户端 ──────▶│  uvicorn (async)                                   │
                    │    ├─ 请求校验 / 鉴权 / 配额（无阻塞）              │
                    │    ├─ 音频 → TempWorkspace（自有临时文件）          │
                    │    └─ 提交到有界队列 ──▶ ThreadPoolExecutor         │
                    │                          ├─ EnginePool: asr       │
                    │                          ├─ EnginePool: diarize   │
                    │                          └─ EnginePool: embed     │
                    └────────────────────────────────────────────────────┘
```

要点：

- **HTTP 层全异步**（校验、鉴权、落临时文件、SSE 推进度），**推理在受控线程里跑**。
  PyTorch / onnxruntime 的 C++ 算子会释放 GIL，所以线程模型对 GPU 推理是够用的。
- **每个模型一把信号量**（`threading.Semaphore(n)`），`n = 该模型允许的并发实例数`。
  pyannote pipeline 不是线程安全的，`n` 只能是 1。
- **队列有界**，满了返回 `429 + Retry-After`（背压），**不是无限排队直到 OOM**。
- **队列等待有超时**，超时返回 `503`，客户端据此降级到别的后端。

**为什么不用 `uvicorn --workers N`**：每个 worker 会**各自加载一份模型** → 显存 × N。
4 个 worker 就是 4 份 Qwen3-ASR，直接把卡撑爆。

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
| 📄 **会议转写引擎（按需）** · `qwen3asr` · `空闲` | 按需引擎，可卸载 | `specs[asr-meeting]`：`resident: false` |
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
| POST | `/v1/asr` | 音频 → 文本（`?variant=short\|meeting`、`?timestamps=1`） |
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
  "limits": {"maxAudioSeconds": 1800, "maxConcurrent": 4, "maxUploadBytes": 67108864},
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
| 413 | `audio_too_long` / `payload_too_large` | 改分段重试；否则跳过 |
| 415 | `unsupported_media` | 换编码；否则跳过 |
| 429 | `quota_exceeded` / `queue_full` | 退避重试（尊重 `Retry-After`） |
| 503 | `model_loading` / `gpu_oom` / `model_failed` | **降级到链上下一个后端** |
| 504 | `inference_timeout` | 降级；并回报"此后端对这么长的音频不可用" |

这张表要与前置文档 §5.2 的 9 类降级原因**一一对齐**（`unavailable` / `timeout` / `quota` /
`auth` / `privacy-denied` / `unsupported` / `vector-mismatch` / `quality-rejected`）。
契约不对齐，路由层就只能靠猜 —— 那等于没有降级逻辑。

### 6.4 长请求与进度

- `Accept: text/event-stream` → 推 `{"stage": "decode|infer|done", "progress": 0.4}`
- **连接状态只在内存**；客户端断开 → 中止推理（`Lease.__exit__` 释放）+ 清理临时文件
- 断了客户端整段重试，服务端不补偿 —— 这就是"无状态"的代价，已在幂等契约里说明

---

## 7. 鉴权、配额与多租户

### 7.1 身份模型（三级递进）

| 级别 | 方式 | 适用 |
|---|---|---|
| 推荐 | **mTLS** | 内网：证书即身份，无需自建凭据生命周期 |
| 标准 | `client_id:secret` → **短期 JWT（1h）** | 有 scopes + 配额声明 |
| 兜底 | 每请求 HMAC 签名 | 完全无法上 TLS 时 |

**强制 TLS，即使内网** —— 上行的是音频与声纹衍生数据。

现有 `api_keys` 表（`app/db.py:72`）是"单机自用"模型：全局一个开关 + 一串 token 哈希。
多客户端要补：`client_id` 维度、轮换、撤销、最后使用时间。**这张表在客户端，不在服务端。**

### 7.2 scopes 与配额

```
scopes:  asr | diarize | embed | tts
配额维度：并发数 / 单请求最大音频秒数 / 每日音频分钟数 / 队列优先级
```

**按槽分别限额与排队**（前置文档已定）：
`asr.short`（高频小请求）与 `diarize`（吃 GPU）分开队列，**避免高频小请求饿死长任务**。

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

---

## 9. 部署与运维

### 9.1 Linux only

| 需求 | Linux | Windows |
|---|---|---|
| 临时目录落内存 | `/dev/shm`（tmpfs） | 无原生等价物 |
| 临时目录容量上限 | `tmpfs size=` | 需配额工具 |
| core dump 控制 | `coredump_filter` / `ulimit -c 0` | 较麻烦 |

（客户端仍支持 Windows/macOS —— 这里说的是**服务端**。）

### 9.2 用容器把"不存储"变成**结构性保证**

这是容器在这里最大的价值，比"好部署"重要得多：

```dockerfile
# 只读根文件系统 + 内存临时目录 = 写业务数据到持久介质在结构上不可能
--read-only
--tmpfs /tmp:size=2g,mode=1777
--tmpfs /dev/shm:size=4g
--cap-drop=ALL
```

**只读 rootfs** 让"不小心把音频写到某个目录"直接报错，而不是静默发生。
这比任何代码评审都可靠。

### 9.3 反向代理（最容易破的一环）

```nginx
proxy_request_buffering off;        # 别把大 body 缓到磁盘
proxy_max_temp_file_size 0;         # 禁止代理临时文件
client_max_body_size 64m;           # 与 maxUploadBytes 对齐
proxy_read_timeout 1800s;           # 长请求
proxy_buffering off;                # SSE 必须
access_log ...  # 不含 $request_body
client_body_temp_path /dev/shm/nginx_tmp;
```

**漏了这段，应用层再干净也没用** —— 音频会被 nginx 缓存到磁盘，而应用层完全看不见。

### 9.4 单实例 vs 多实例（真实取舍）

无状态 → 多实例可以直接轮询扩。**但模型权重不共享**：

- 每个实例各自加载模型 → **显存 × 实例数**
- 所以单卡上"多实例"通常是**负优化**

正确做法：

| 场景 | 方案 |
|---|---|
| 单卡、显存够 | **单实例**，靠 EnginePool 内部并发（v1） |
| 单机多卡 | 每卡一个实例，端口不同，前置负载均衡**按模型亲和** |
| 多机 | 按**能力**分实例：一台专做 `asr`，一台专做 `diarize`/`embed`；客户端按槽分别配置地址 |

最后一行很重要：**因为路由是"按槽选后端"，客户端天然支持"asr 在这台、diarize 在那台"** ——
不需要服务端做集群。这是把复杂度留在客户端编排的又一个好处。

### 9.5 启动与退出

- **启动**：先起 HTTP（`/v1/health` 立刻 200），再**后台预热常驻模型**（复用 `boot.py` 的分阶段编排）
- **退出**：`pool.shutdown()` 释放显存 + `TempWorkspace` 清扫 + 等待在飞请求（有超时）

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
    - id: asr-meeting                  # 面板：会议转写引擎（按需）
      slot: asr.timestamps
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
  queue_max: 32
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
| 模型加载中 | `503 model_loading` + `Retry-After` | 等或降级（看 `estWait`） |
| 加载失败 | `503 model_failed`；`/v1/capabilities` 标 `failed` | 跳过此后端，直到 capabilities 变化 |
| 显存不足 | 卸载 LRU → 仍不够 → `503 gpu_oom` | **绝不回退 CPU**；降级 |
| 队列满 | `429 queue_full` + `Retry-After` | 退避重试 |
| 配额用尽 | `429 quota_exceeded` | 提示用户 / 按日重置 |
| 音频过长 | `413 audio_too_long` | 客户端重新分段 |
| 推理超时 | `504 inference_timeout` | 降级（并记住"此后端不适合这么长的音频"） |
| 客户端断开 | 中止推理 + 清临时文件 | 整段重试（幂等在客户端） |
| 服务端崩溃 | 启动时清扫残留临时文件 | 重试；`request_id` 用于对账 |
| 删除临时文件失败 | 登记 + 重试 + `warn` 日志；`/v1/health` 可见 | — |

---

## 12. 护栏测试清单

| 测试 | 断言 |
|---|---|
| `test_server_no_db.py` | `server/` 不 import `app.db`；跑完请求无 `.db` 文件生成 |
| `test_server_no_business_words.py` | `server/` 源码不出现 `meeting`/`command`/`summary`/`voiceprint` |
| `test_server_no_write_endpoints.py` | 路由表里没有写端点（白名单外） |
| `test_server_logs_no_content.py` | 跑一次请求后日志 grep 不到输入文本/嵌入 |
| `test_tmp_cleaned_on_success.py` | 请求成功后临时目录为空 |
| `test_tmp_cleaned_on_exception.py` | 推理抛异常后临时目录为空 |
| `test_tmp_cleaned_on_client_disconnect.py` | 客户端中途断开后临时目录为空 |
| `test_tmp_swept_on_startup.py` | 预置残留文件 → 启动后清空 |
| `test_tmp_ttl_sweeper.py` | 超龄文件被删除并写日志 |
| `test_tmp_capacity_refuses.py` | 超过 `max_bytes` 时返回 429/507，**不写爆盘** |
| `test_no_silent_cpu_fallback.py` | 模拟显存不足 → 返回 `503 gpu_oom`，**不是**悄悄用 CPU |
| `test_pool_single_flight.py` | 10 个并发请求同一模型 → loader 只被调用 1 次 |
| `test_pool_no_evict_while_in_use.py` | 推理中引用计数 > 0 的模型不被卸载 |
| `test_pool_vram_budget.py` | 超预算时先卸载 LRU；仍不够则拒绝而非 OOM |
| `test_vector_space_frozen.py` | 进程生命周期内 `vectorSpaceId` 不变；改配置需重启 |
| `test_error_codes_client_mappable.py` | 每个错误码都能映射到前置文档的 9 类降级原因（契约对齐） |
| `test_capabilities_reflects_state.py` | 模型 loading 时 `capabilities` 标 `loading`，不是撒谎说 ready |
| `test_readonly_rootfs_smoke.py` | 只读 rootfs + tmpfs 下跑通全部端点（容器冒烟） |

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
3. **TLS 用什么形态？** mTLS（内网证书，需 PKI）还是自签 + `client_id:secret`→JWT
   （自包含，无外部依赖）。**建议 v1 用后者**，把 mTLS 留给单位 PKI 就绪之后。
