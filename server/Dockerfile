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
# 跑（只读根 + 唯一可写处是临时卷；GPU 直通见 §9.2）：
#     docker run -d --name echo-backend --gpus all \
#       --read-only \
#       -v /srv/echo/tmp:/var/echo/tmp \
#       -v /srv/echo/models:/opt/echo/models:ro \
#       -p 8900:8900 \
#       -e ECHO_MODELS_ROOT=/opt/echo/models \
#       -e ECHO_TMP_ROOT=/var/echo/tmp \
#       echo-backend:0.1.0
#
# 临时文件是**落盘**的（按日期分目录 + 定时清理，见 server/tmp.py）。
# 想白拿内存盘就把挂载点换成 tmpfs：
#     --tmpfs /var/echo/tmp:size=4g
# =====================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ECHO_TMP_ROOT=/var/echo/tmp

WORKDIR /opt/echo

# soundfile 要 libsndfile；其余全是 pip wheel（不编译）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt /tmp/echo-req.txt
RUN pip install -r /tmp/echo-req.txt

# 可选的重依赖层。**默认不装** —— 起了但不装只会让对应模型如实报 failed，
# 不会假装就绪（设计 §3.4 / §6.2）。
ARG ECHO_EXTRA=0
RUN if [ "$ECHO_EXTRA" = "1" ]; then \
        pip install torch funasr qwen-asr transformers pyannote.audio speechbrain ; \
    fi

# 只拷服务端要的两层：能力后端 + 引擎层（app/audio 及其依赖的 paths / platform）。
# app/ 里那些业务模块**在源码层面**被禁用（tests/test_server_contract.py 扫 import），
# 这里整份拷进来只是为了省事，不代表服务端能用它们。
COPY server/ /opt/echo/server/
COPY app/ /opt/echo/app/

VOLUME ["/var/echo/tmp"]
EXPOSE 8900

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8900/v1/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "server.main"]
