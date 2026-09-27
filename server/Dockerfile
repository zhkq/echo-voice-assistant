# =====================================================================
# ECHO 能力后端 —— Linux + Docker 镜像
#
# 为什么容器里一律是 Linux（设计 §9.1）：Docker 在 Windows 上跑的**也是 Linux 容器**
# （WSL2 后端），所以宿主选 Windows 还是 Linux **只影响宿主，不影响这份代码** ——
# 服务端只需要支持 Linux 一套，不用为 Windows 原生再写一遍。
#
# 构建（**上下文是仓库根**，因为要带上 app/audio 那层引擎）：
#     docker build -f server/Dockerfile -t echo-backend:0.1.0 .
#
# 带上高精度转写与说话人分离（体积大、要 GPU）：
#     docker build -f server/Dockerfile --build-arg ECHO_EXTRA=1 -t echo-backend:0.1.0-full .
#
# 跑（只读根 + 两个可写挂载：临时卷 + 状态卷；GPU 直通见 §9.2）：
#     docker run -d --name echo-backend --gpus all \
#       --read-only \
#       -v /srv/echo/tmp:/var/echo/tmp \
#       -v /srv/echo/state:/var/echo/state \
#       -v /srv/echo/models:/opt/echo/models:ro \
#       -p 8900:8900 \
#       -p 127.0.0.1:8901:8901 \
#       -e ECHO_MODELS_ROOT=/opt/echo/models \
#       -e ECHO_TMP_ROOT=/var/echo/tmp \
#       -e ECHO_STATE_ROOT=/var/echo/state \
#       -e ECHO_ADMIN_LISTEN=0.0.0.0:8901 \
#       echo-backend:0.1.0
#
# **管理面（8901）这两行是配套的，缺一个都白搭**：容器里必须绑 `0.0.0.0`
# （绑 127.0.0.1 只有容器自己进得去），而宿主只发布到回环（`127.0.0.1:8901:8901`），
# 所以实际可达范围仍然只是宿主本机 —— 与"绑回环"一致。启动日志里那句
# "管理面监听在 0.0.0.0:8901（不是回环）"的告警因此是**预期**的，别去消掉它。
# 用 compose 的话这些都在 `server/compose.yaml` 里写好了，见 `docs/后端容器部署.md`。
#
# **两个卷的保留策略是相反的，别合并、别搞错：**
#
#   /var/echo/tmp    临时文件（按日期分目录 + 定时清理，见 server/tmp.py）。
#                    丢了没关系，甚至可以换成内存盘白拿性能：
#                        --tmpfs /var/echo/tmp:size=4g
#   /var/echo/state  **耐久**状态：鉴权库（clients / pairing_codes）。
#                    **绝对不要挂 tmpfs、也不要和 tmp 共用一个卷** ——
#                    丢了就要把所有客户端重新配对一遍。
# =====================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ECHO_TMP_ROOT=/var/echo/tmp \
    ECHO_STATE_ROOT=/var/echo/state

WORKDIR /opt/echo

# soundfile 要 libsndfile；其余全是 pip wheel（不编译）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# pip 的包索引（**构建期参数**，不是运行时配置）。默认就是 PyPI，行为与从前一致。
#
# 为什么留这个开关（2026-09-28 在一台国内 GPU 宿主上实测）：
#   * 宿主机上 `curl` 拉 PyPI 的 CDN 能到 17 MB/s，但**构建里的 `pip install`** 每个 wheel
#     只有 85~190 kB/s（同一时刻、同一台机器，走的是另一批 CDN 节点）——
#     那一层 requirements 就够慢，`ECHO_EXTRA=1` 那层（funasr / qwen-asr / transformers /
#     pyannote.audio / speechbrain 加起来几百 MB）会慢到不可接受；
#   * 同一时刻清华镜像的**真实 wheel** 是 **26 MB/s**（差 170 倍）。
#   * ⚠️ 它**只管普通依赖**：torch 走上面的 `ECHO_TORCH_INDEX`（cu118/cu126），两者别混 ——
#     torch 的 cu118 索引自带它要的那批依赖（sympy 1.14 / typing-extensions 4.16 / filelock /
#     networkx / jinja2 / fsspec / nvidia-*-cu11 都在，2026-09-28 实地核过），
#     所以 torch 那一句**不需要** `--extra-index-url`。
ARG ECHO_PIP_INDEX=https://pypi.org/simple

COPY server/requirements.txt /tmp/echo-req.txt
RUN pip install --index-url "$ECHO_PIP_INDEX" -r /tmp/echo-req.txt

# 可选的重依赖层。**默认不装** —— 起了但不装只会让对应模型如实报 failed，
# 不会假装就绪（设计 §3.4 / §6.2）。
#
# ⚠️ **torch 的 CUDA 版本必须按显卡选**（下面两个 ARG 就是给"老卡"留的开关）：
#   * 默认 `cu126`，面向 Turing 及以后（sm_75+；Ampere / Ada / Hopper / Blackwell）。
#   * **Pascal（GTX 10xx、P4/P40/P100…，sm_6x）与 Volta（V100，sm_70）**：
#     CUDA 12.8/12.9 的构建里已经**没有**这些架构，CUDA 13.x 更是只支持 sm_75 及以上；
#     PyTorch 2.15 起连 CUDA 12.6 的 wheel 都不再发布（Maxwell/Pascal/Volta 一起被丢下）。
#     所以老卡要**钉到最后一个还带 Pascal 的构建**：
#         --build-arg ECHO_TORCH_INDEX=https://download.pytorch.org/whl/cu118
#         --build-arg ECHO_TORCH_VERSION=2.7.1     # cu118 源上目前最新的一版
#     来源：PyTorch 开发者邮件列表公告（2.15 弃 CUDA 12.6 = 弃 Maxwell/Pascal/Volta）
#     https://dev-discuss.pytorch.org/t/notice-cuda-12-6-wheels-will-no-longer-be-published-from-pytorch-2-15-drops-maxwell-pascal-volta/3432
#   * **这两个 ARG 是构建期的**（不是运行时配置），所以它们不出现在上面的 ENV 里 ——
#     与 `ECHO_EXTRA` 同一类（`tests/test_server_contract.py:EnvOverrideTests` 只扫 ENV 块）。
#   * 装完会**打印**实际装到的 torch/CUDA 版本；钉了版本时还会**校验它没被后面的依赖改掉**
#     （老卡上这是最容易被静默破坏的一步：`pip install pyannote.audio` 顺手把 torch 升级，
#      镜像构建成功、容器里却跑不了那块卡）。
ARG ECHO_EXTRA=0
ARG ECHO_TORCH_INDEX=https://download.pytorch.org/whl/cu126
ARG ECHO_TORCH_VERSION=""
# ⚠️ 老卡（`ECHO_TORCH_VERSION` 非空 = 钉住 cu118 那条路）**装不了 pyannote**，
#    所以这一档刻意只装 SenseVoice 那条链（funasr）。2026-09-28 在 GTX 1070 上实测：
#      * `pip install ... pyannote.audio` 现在会拿到 **4.0.x**，而 4.0.0 起
#        `Requires-Dist: torch>=2.8.0, torchaudio>=2.8.0, torchcodec>=0.6.0` ——
#        pip 于是**把 torch 2.7.1+cu118 卸掉**换成 cu13 的 torch（triton 3.3.1 也被换掉，
#        nvidia-*-cu13 全下来），也就是"镜像构建成功、这块卡上跑不了"的那个坑里
#        最贵的一种。而 **torch 2.7.1+cu118 是最后一个带 Pascal(sm_61) 的构建**，
#        所以老卡上二者**不可兼得** —— 下面那条版本校验会在这里把构建拦下来（别删它）。
#      * 退回 `pyannote.audio==3.4.0`（它只要 torch>=2.0）也不行：3.x 的
#        `SpeakerDiarization.__init__` **没有 `plda=` 形参**（3.4.0 里 "plda" 出现 0 次），
#        而 `app/audio/diarize.py` 传的正是 `plda={"checkpoint": ...}`（4.x 的 API）。
#      * 结论：**sm_<7.5 的卡上做不了说话人分离**（`/v1/diarize` 与 `/v1/speaker/embed`
#        如实不可用）。配置里也就**不该**声明 diarize / speaker-embed 两档
#        （`scripts/prepare-backend.sh --asr-impl` 在老卡上只会写 asr-long）——
#        宣告一个永远 model_failed 的槽，与"对齐器缺失却宣告 asr.timestamps"是同一种假话。
#      * ⚠️ `torchaudio` 必须**和 torch 一起钉**：funasr 依赖它，不钉就会被解析成
#        2.10（PyPI 默认轮子）→ 转头把 torch 也顶上去，同样触发那条校验。
#    新卡（不钉 torch 版本）走的是下面 else 那一支，`pyannote.audio` 仍按最新装
#    （开发机实测的组合：torch 2.10+cu128 / pyannote 4.0.7）。
RUN if [ "$ECHO_EXTRA" = "1" ]; then \
        if [ -n "$ECHO_TORCH_VERSION" ]; then \
            pip install "torch==$ECHO_TORCH_VERSION" "torchaudio==$ECHO_TORCH_VERSION" --index-url "$ECHO_TORCH_INDEX" ; \
        else \
            pip install torch --index-url "$ECHO_TORCH_INDEX" ; \
        fi \
     && if [ -n "$ECHO_TORCH_VERSION" ]; then \
            pip install funasr transformers --index-url "$ECHO_PIP_INDEX" ; \
        else \
            pip install funasr qwen-asr transformers pyannote.audio speechbrain \
                --index-url "$ECHO_PIP_INDEX" ; \
        fi \
     && python -c "import torch; print('torch', torch.__version__, '| cuda', torch.version.cuda)" \
     && if [ -n "$ECHO_TORCH_VERSION" ]; then \
            python -c "import torch,sys; sys.exit(0 if torch.__version__.startswith('$ECHO_TORCH_VERSION') else 1)" \
            || { echo "ERROR: torch 被后面的依赖改版了（想要 $ECHO_TORCH_VERSION）—— 老卡（Pascal/Volta）会因此跑不起来。请把 funasr/pyannote 那组也钉版本，或用 pip 的 --constraint 锁住 torch。" >&2 ; exit 1 ; } ; \
        fi ; \
    fi

# 只拷服务端要的两层：能力后端 + 引擎层（app/audio 及其依赖的 paths / platform）。
# app/ 里那些业务模块**在源码层面**被禁用（tests/test_server_contract.py 扫 import），
# 这里整份拷进来只是为了省事，不代表服务端能用它们。
COPY server/ /opt/echo/server/
COPY app/ /opt/echo/app/

# 两个可写挂载点（见文件头的说明：保留策略相反，别合并）
VOLUME ["/var/echo/tmp", "/var/echo/state"]
EXPOSE 8900

# ⚠️ 这个探针打的是**容器自己的** 127.0.0.1:8900（`/v1/health` 免鉴权）。
#    容器里启用 TLS（挂一份配了 server.tls 的配置 + `--config`）时它必须换成 https，
#    否则容器会一直显示 unhealthy 而服务其实是好的：用 compose 时那条 healthcheck 认
#    `ECHO_HEALTH_SCHEME`（见 server/compose.yaml）；直接 `docker run` 的话自己加
#    `--health-cmd`（本镜像这一层是编译进镜像的，运行时改不了它）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8900/v1/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "server.main"]
