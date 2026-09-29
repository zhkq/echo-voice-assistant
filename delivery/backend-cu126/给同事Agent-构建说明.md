# 给同事机器的 Agent：部署 ECHO 能力后端（新卡 / cu126）

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
| **≥ 8.0**（30/40/50 系、A100/H100…） | ✅ 本包全能力：qwen3asr（bf16）+ 原生句级时间戳 + 分离 + 声纹 |
| **7.5**（2080/2080Ti/魔改、T1600/T4…） | ⚠️ 本包可用，但 **qwen3asr 跑不了（无 bf16）** → 把 `server.yaml` 里 `impl: qwen3asr` 换成 `impl: sensevoice`，`supports: [asr.text]`（文件里已备好可替换片段） |
| **≤ 7.0**（1070/1080Ti/V100/M40…） | ❌ **本包不适用** —— 用 cu118 包 |

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
unzip ECHO-backend-kit-cu126-*.zip
cd ECHO-backend-kit-cu126-*/
ls        # 应有：server/ app/ scripts/ 先读我.md compose.yaml .env.example server.yaml
```

---

## 3. 构建镜像（**关键：三个 build args**）

```bash
docker compose build \
  --build-arg ECHO_EXTRA=1 \
  --build-arg ECHO_TORCH_INDEX=https://download.pytorch.org/whl/cu126 \
  --build-arg ECHO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
```

* 期望在构建日志里看到 **`torch <版本>+cu126 | cuda 12.6`**。
* Dockerfile 里有**版本一致性校验**：若后续依赖把 torch 换版，**构建会直接失败** —— 那是在保护你（否则就是"构建成功但卡上跑不了"），**不要绕过**。
* 需要下载 10~15 GB，30~60 分钟。`ECHO_PIP_INDEX` 很关键：实测官方 pypi 只有 85~190 kB/s，清华源 26 MB/s（差 170×）。
* `download.pytorch.org` 若不通，改用可用的 cu126 wheel 镜像（如 SJTU/清华的 pytorch-wheels 镜像）并把 `ECHO_TORCH_INDEX` 指过去。

---

## 4. 放模型（**镜像里没有模型**，模型是只读挂载的）

> **先看包根有没有 `models/`**：有（还有一份 `MODELS-INCLUDED.txt`）= 这是**含权重的包**，
> 权重就在包里 —— 本节只剩"把 `ECHO_HOST_MODELS_DIR` 指向解包后的 `models/`"这一步，
> 下面的 rsync / 下载全部跳过。没有 = 纯代码包，按下面做。

```bash
sudo mkdir -p /srv/echo/models && sudo chown -R "$USER" /srv/echo
# 推荐从源机 rsync（内网最快）：
#   rsync -aL <user>@<源机>:/path/to/models/ /srv/echo/models/
# 或从 ModelScope 下（见 先读我.md §4）；或解开随交付给到的模型包。
```

期望布局（**摆错位置 = 看起来装了其实加载不到**）：

```
/srv/echo/models/hub/models--Qwen--Qwen3-ASR-0.6B/snapshots/<rev>/
/srv/echo/models/hub/models--Qwen--Qwen3-ForcedAligner-0.6B/snapshots/<rev>/
/srv/echo/models/sensevoice/          # 可选（Turing 退路）
/srv/echo/models/pyannote/            # 分离 / 声纹
```

⚠️ **强制对齐器不可缺**：缺了服务端仍会宣告 `supports: [asr.timestamps]`，而实际给不出句级时间戳 ——
那是**说假话**（`/v1/health` 照样绿，所以必须自己摆齐）。

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

---

## 6. 验收（**这几条必须全过**，不过就停手报告）

```bash
curl -sk https://127.0.0.1:8900/v1/health        # ok=true；看 vram/models 字段
docker exec echo-backend python -c "import torch;print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_capability())"
#   ↑ 必须 is_available()=True，且 cap 与显卡一致；否则就是"装上了但卡用不了"
docker exec echo-backend python -c "import torch;print('bf16:', torch.cuda.is_bf16_supported())"
#   新卡应为 True（qwen3asr 走 bf16）；False → 按 §0 换 SenseVoice（或等引擎侧 fp16 改动）
curl -sk https://127.0.0.1:8900/v1/capabilities
#   应包含 asr.text / asr.timestamps / diarize.turns / speaker.embed
```

再用**一段真音频**打一次 `/v1/asr`，把成绩记下来：**音频秒数 / 耗时 / RTF / 文本长度 / 显存峰值**。
（参考：本机 5060 上 qwen3asr 修复后 600 秒音频 30~47 秒；旧卡 1070 上 SenseVoice 600 秒 5~7 秒。）

---

## 7. 管理面（**只发布在宿主回环**，别改）

```bash
ssh -L 8901:127.0.0.1:8901 <这台机器>
# 浏览器： http://127.0.0.1:8901/admin/  → 登录 → 「客户端 / 发授权」→ 生成配对串给客户端
```

* **别**把端口发布改成 `8901:8901` —— 那等于把管理面开到网段上。容器内绑 `0.0.0.0:8901` 是必需的，
  可达范围由宿主发布规则限定在回环。
* 日志里那句"管理面监听在 `0.0.0.0:8901`（不是回环）"是**预期告警**，不要去消。
* ⚠️ 如果你用**别的本地端口**做隧道（例如 `-L 18901:…`），浏览器的 Origin 会与该端口不符 →
  **写操作（发码/撤销/改配额）会被 403 拦**（这是防 DNS-rebinding 的必要设计）。
  此时改用 CLI 发码：
  ```bash
  docker exec echo-backend python -m server.main --config /etc/echo/server.yaml --new-client 名字 --scopes asr,diarize
  ```
  注意 **scopes 用逗号分隔**（带空格会被参数解析切开）。

---

## 8. 四个已知坑（照做即可，别自由发挥）

1. **Docker Hub 被墙** → 显式前缀直拉 + `docker tag`（见 §1）；只配 `registry-mirrors` 可能让裸名拉取挂住。
2. **变体必须对卡**：新卡 cu126 / 老卡 cu118。**构建成功 ≠ 卡能用** —— 必须过 §6 的 `is_available()` 与 `bf16`。
3. **模型不随镜像**，且**缺强制对齐器会让 `asr.timestamps` 变成假话**。
4. **管理面只绑回环、走 `ssh -L`**，不要把 8901 发布到网段。

---

## 9. 交付边界（诚实说明）

* **模型不进镜像**（设计如此：换模型不必重出镜像）；
* 本说明与交付包**尚未在"新卡"真机上端到端跑过** —— 首个跑通的人请把 §6 的成绩回传，用于校准
  `est_vram_mb` / `vram_budget_mb` / `max_concurrent` 与文档；
* 官方已宣布 PyTorch 2.15 起不再发布 CUDA 12.6 wheel（同时弃 Maxwell/Pascal/Volta）→  **新卡走 cu126、老卡走 cu118** 这条分界要长期维护。
