# 给同事机器的 Agent：部署 ECHO 能力后端（老卡 / cu118）

> 这份说明是**给"另一台机器上的 AI 助手"照着执行**的。请逐步做、每步把输出留下；
> 遇到"必须过"的验收项不过就停手报告，**不要绕过去**。
> 配套文件（同一个交付包里）：`compose.yaml` · `.env.example` · `server.yaml` · `先读我.md`（人读版）。

---

## 0. 先决条件（缺一不可，先检查并把结果贴出来）

```bash
nvidia-smi --query-gpu=name,compute_cap,memory.total,driver_version --format=csv
docker --version; docker compose version
nvidia-ctk --version || echo "缺 nvidia-container-toolkit"
df -h / | tail -1        # 需要 ≥ 60 GB 可用
```

判定：

| `compute_cap` | 结论 |
|---|---|
| **≤ 7.0**（Maxwell / Pascal / Volta：GTX 1070/1080Ti、V100、M40、P100…） | ✅ **本包适用**：只有 SenseVoice 转写（**没有**分离 / 声纹 / 原生时间戳，见 §6 与 §9） |
| **7.5**（Turing：2080/2080Ti、T4、GTX 16xx…） | ❌ **本包不适用** —— 用 `backend-cu126` 包（Turing 上 qwen3asr 仍不可用，但分离/声纹在） |
| **≥ 8.0**（Ampere/Ada/Hopper：30/40/50 系、A100/H100…） | ❌ **本包不适用** —— 用 `backend-cu126` 包 |

**判据一句话**：`<= 7.0` 用本包；`>= 7.5` 用 `backend-cu126`。

`nvidia-container-toolkit` 没装就先装（加 NVIDIA 官方源 → `apt install nvidia-container-toolkit`），
然后 `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`。

---

## 1. Docker Hub 若被墙（中国大陆常见）

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{ "runtimes": { "nvidia": { "args": [], "path": "nvidia-container-runtime" } },
  "registry-mirrors": ["https://docker.1panel.live", "https://dockerproxy.net"] }
JSON
sudo systemctl restart docker

# 【坑】配了 registry-mirrors 之后，**裸名拉取可能长时间挂住**。
# 解法：用显式前缀直拉，再打标准名字（Dockerfile 的 FROM 才认得）：
docker pull docker.1panel.live/library/python:3.11-slim
docker tag  docker.1panel.live/library/python:3.11-slim python:3.11-slim
```

（可选加速器，可达性需现场验：`dockerproxy.net` / `docker.m.daocloud.io` / `docker.xuanyuan.me`。）

---

## 2. 解包交付包

```bash
unzip ECHO-backend-kit-cu118-*.zip
cd ECHO-backend-kit-cu118-*/
ls        # 应有：server/ app/ scripts/ 先读我.md compose.yaml .env.example server.yaml
```

---

## 3. 构建镜像（**关键：四个 build args，版本必须钉死**）

```bash
docker compose build \
  --build-arg ECHO_EXTRA=1 \
  --build-arg ECHO_TORCH_INDEX=https://download.pytorch.org/whl/cu118 \
  --build-arg ECHO_TORCH_VERSION=2.7.1 \
  --build-arg ECHO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
```

* **`ECHO_TORCH_VERSION=2.7.1` 不许省。** 2.7.1+cu118 是**最后一版带 Pascal(sm_61)** 的构建；
  不钉它，装到的新版本在这块卡上直接跑不了（而且构建可能照样成功）。
* 期望在构建日志里看到 **`torch 2.7.1+cu118 | cuda 11.8`**。
* Dockerfile 里有**版本一致性校验**：若某个依赖（典型是 `pyannote.audio`，它现在要
  `torch>=2.8`）想把 torch 换成 cu13 那套，**构建会直接失败** —— 那是在保护你，
  **不要绕过**。本变体因此只装 `funasr`（+ 依赖），不装 `pyannote.audio` / `qwen-asr`。
* 需要下载 10~15 GB，30~60 分钟。`ECHO_PIP_INDEX` 很关键：实测官方 pypi 只有 85~190 kB/s，
  清华源 26 MB/s（差 170×）。**torch 不走它**（cu118 索引自带它要的依赖）。
* `download.pytorch.org` 若不通，改用可用的 cu118 wheel 镜像（如 SJTU/清华的
  pytorch-wheels 镜像）并把 `ECHO_TORCH_INDEX` 指过去。

---

## 4. 放模型（**镜像里没有模型**，模型是只读挂载的；**本变体只要 SenseVoice**）

> **先看包根有没有 `models/`**：有（还有一份 `MODELS-INCLUDED.txt`）= 这是**含权重的包**，
> 权重就在包里 —— 本节只剩"把 `ECHO_HOST_MODELS_DIR` 指向解包后的 `models/`"这一步，
> 下面的 rsync / 下载全部跳过。没有 = 纯代码包，按下面做。

```bash
sudo mkdir -p /srv/echo/models && sudo chown -R "$USER" /srv/echo
# 推荐从源机 rsync（内网最快）：
#   rsync -aL <user>@<源机>:/path/to/models/sensevoice/ /srv/echo/models/sensevoice/
# 或从 ModelScope 下（见 先读我.md §4.2）；或解开随交付给到的模型包。
```

期望布局（**摆错位置 = 看起来装了其实加载不到**）：

```
/srv/echo/models/sensevoice/snapshots/master/     # config.yaml + model.pt（本变体唯一必需）
```

**不用放的**（放了也不会被加载 —— 镜像里根本没有对应的运行时）：

| 不用放 | 为什么 |
|---|---|
| `hub/models--Qwen--Qwen3-ASR-0.6B/` | 老卡**没有 bf16**（要 sm_80+），而 `app/audio/stt.py` 对 qwen3asr 写死请求 bf16；退 fp32 会让显存翻倍 |
| `hub/models--Qwen--Qwen3-ForcedAligner-0.6B/` | 它是给 qwen3asr 配句级时间戳的；本变体不跑 qwen3asr |
| `pyannote/**` | 老卡镜像**没装** `pyannote.audio`（4.x 要 torch>=2.8，而这块卡上限 2.7.1+cu118） |

> ⚠️ **不能只指望 ModelScope 缓存**：容器里根文件系统是 `read_only: true`，
> 必须走 compose 里那个 `cache` 卷（`MODELSCOPE_CACHE` / `HOME`）——
> 少了它 SenseVoice 加载时会去下 `fsmn-vad` 然后失败（2026-09-28 在 1070 上踩过，
> 这是当时最要紧的一条）。compose 里已经配好了，**别删那两个环境变量**。

---

## 5. 起服务 + 建管理员

```bash
cp .env.example .env
sed -i "s|^ECHO_JWT_SECRET=.*|ECHO_JWT_SECRET=$(openssl rand -hex 32)|" .env
docker compose up -d
docker exec echo-backend python -m server.main --config /etc/echo/server.yaml --list-admins
docker exec echo-backend python -m server.main --config /etc/echo/server.yaml --new-admin <管理员名>
#   口令只打印这一次。想设成记得住的：用 tools/set-admin-password.py（改一行后跑）或按客户端的说明改。
```

* 缺 `ECHO_JWT_SECRET` 时服务**照常起、`/v1/health` 也 200**，但配对之后 `/v1/token` 会回 `503 auth_misconfigured`。
* 报错一般看：`docker logs --tail 50 echo-backend`。
* 本变体默认**只宣告一个模型档**（`impl: sensevoice`，`supports: [asr.text]`）——
  这是刻意的，不是少配了：宣告一个永远 `model_failed` 的档等于说假话。
  重新跑 `prepare-backend.sh` 会重写 `server.yaml`，所以**要改引擎档位就用
  `--asr-impl sensevoice` 这个参数**，别手改文件（会被抹掉）。

---

## 6. 验收（**这几条必须全过**，不过就停手报告）

```bash
curl -s http://127.0.0.1:8900/v1/health | python3 -m json.tool
python3 scripts/smoke-echo-backend.py --base-url http://127.0.0.1:8900 --skip-inference
#   ↑ 只验查询端点 + 拒绝路径 + 临时文件归零；**证明不了权重是对的**
python3 scripts/smoke-echo-backend.py --base-url http://127.0.0.1:8900 --wav /path/to/16k.wav
#   ↑ 这一条才是"模型真能用"的证据（几秒的 16k 单声道 wav 就够）
docker exec echo-backend python -c \
  "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_capability())"
#   ↑ 必须是 2.7.1+cu118 / is_available()=True / cap 与显卡一致；否则就是"装上了但卡用不了"
```

**`/v1/health` 的期望值**（老卡这边 `models` 只有一条，是新卡的一半）：

| 字段 | 期望 | 说明 |
|---|---|---|
| `ok` | `true` | |
| `vram` | `budgetMb=7000`（或你改的值）、空转 `usedMb=0` | 模型按需加载，**空转 usedMb=0 是对的** |
| `tmp` | `files=0, bytes=0` | **不为 0 就是泄漏信号** |
| `models` | **只有 `asr-long`** | **没有** `diarize` / `speaker-embed` —— 本变体的预期形状 |
| `metrics` / `quota` | 数字 | |

`/v1/capabilities` 应含 `asr.long` / `asr.text`，**不含** `asr.timestamps` / `diarize.turns` /
`speaker.embed`（老卡上这三档跑不了，服务端**不许宣告**）。

**跑完把成绩记下来**（这一步不是形式）：`vram.usedMb`、一段真音频的**音频秒数 / 耗时 / RTF /
文本长度 / 显存峰值**，回填 `server.yaml` 里 `est_vram_mb` 的注释。

> **本机参考值**（2026-09-28 在 GTX 1070 8 GB / Ubuntu 26.04 / Docker 29.1.3 上实测）：
> torch `2.7.1+cu118`、cap `(6,1)`；`/v1/health` 空转 `usedMb=0`、用过之后 **`usedMb=1800`**；
> **90 秒真会议音频**：冷启动 **9.7~15.7 s**、热 **1.17 s（RTF 0.013）**、490 字、
> `timestamps=none`、**显存峰值 1701~1737 MiB**；冒烟 `--skip-inference` **18/18 通过**
> （要带 `--pair-code`，见 `先读我.md` 的排错节）。

---

## 7. 管理面（**只发布在宿主回环**，别改）

```bash
ssh -L 8901:127.0.0.1:8901 <这台机器>
# 浏览器： http://127.0.0.1:8901/admin/  → 登录 → 「客户端 / 发授权」→ 生成配对串给客户端
```

* **别**把端口发布改成 `8901:8901` —— 那等于把管理面开到网段上。容器内绑 `0.0.0.0:8901`
  是必需的，可达范围由宿主发布规则限定在回环。
* 日志里那句"管理面监听在 `0.0.0.0:8901`（不是回环）"是**预期告警**，不要去消。
* ⚠️ 如果你用**别的本地端口**做隧道（例如 `-L 18901:…`），浏览器的 Origin 会与该端口不符 →
  **写操作（发码/撤销/改配额）会被 403 拦**（这是防 DNS-rebinding 的必要设计）。
  此时改用 CLI 发码：
  ```bash
  docker exec echo-backend python -m server.main --config /etc/echo/server.yaml --new-client 名字 --scopes asr,diarize
  ```
  注意 **scopes 用逗号分隔**（带空格会被参数解析切开）。

---

## 8. 六个已知坑（照做即可，别自由发挥）

1. **Docker Hub 被墙** → 显式前缀直拉 + `docker tag`（见 §1）；只配 `registry-mirrors` 可能让裸名拉取挂住。
2. **变体必须对卡**：老卡 cu118 / 新卡 cu126。**构建成功 ≠ 卡能用** —— 必须过 §6 的
   `is_available()` 与设备算力。
3. **torch 版本必须钉 2.7.1**（§3）：不钉就可能装到一个**没有 Pascal 内核**的版本，
   而构建照样成功。
4. **模型不随镜像**；本变体**只需要 SenseVoice**（§4），别把新卡那套 Qwen/对齐器拷过来占地方。
5. **容器里要有可写的 ModelScope 缓存**（compose 的 `cache` 卷）：少了它 SenseVoice 加载失败。
6. **管理面只绑回环、走 `ssh -L`**，不要把 8901 发布到网段。

---

## 9. 交付边界（诚实说明）

* **模型不进镜像**（设计如此：换模型不必重出镜像）。
* **本变体只提供转写（SenseVoice）**：`asr.timestamps` / `diarize.turns` / `speaker.embed`
  **都不提供**。客户端那边的表现是**诚实的降级**：会议显示「说话人分离未执行：<真原因>」，
  时间轴按字数均摊并如实标为**估算**，文字照常产出。
  要分离 / 声纹 / 原生时间戳，换 `compute_cap >= 7.5` 的卡 + `backend-cu126` 包。
* **本变体已在真机上端到端跑通**（GTX 1070 / Ubuntu 26.04 / Docker 29.1.3，2026-09-28）：
  构建、起容器、建管理员、冒烟、一段 90 秒真音频的转写都过了 —— 数字见 §6 的参考值，
  完整记录（含当时发现并修掉的 7 个问题）在仓库的 `docs/后端容器部署.md` §13。
  仍然**没有**压测过并发（`ECHO_MAX_CONCURRENT=2` 是按卡估的），也没有第二个人的机器验过。
  （这个 2 是 **2026-09-28 那次真机验证当时用的数**，属历史记录；现出厂默认是 **6**，
  见 `compose.yaml` 的 `${ECHO_MAX_CONCURRENT:-6}`。）
* `est_vram_mb` 的注释里仍是**占位估算**（1200）；真机实测约 **1700~1737 MiB**（峰值，含激活）——
  把你这台的数字回填进去，下一个人就不用猜了。
* 官方已宣布 PyTorch 2.15 起不再发布 CUDA 11.8/12.6 wheel（同时弃 Maxwell/Pascal/Volta）→
  **新卡走 cu126、老卡走 cu118** 这条分界要长期维护。
