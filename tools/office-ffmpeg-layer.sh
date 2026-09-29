#!/bin/bash
# 在 echo-backend:0.1.0-cu126 之上补一层 FFmpeg（torchaudio / torchcodec / pyannote 需要）。
# 为什么不重建整个镜像：pip 层要 10~30 分钟，而这是纯 apt 层，1~2 分钟。
set -e
cd /home/zysk/echo/backend
cat > Dockerfile.ffmpeg <<'EOF'
FROM echo-backend:0.1.0-cu126
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
EOF
echo "== 构建补丁层"
docker build -f Dockerfile.ffmpeg -t echo-backend:0.1.0-cu126 . 2>&1 | tail -n 6
echo "== 重建容器"
docker compose up -d --force-recreate 2>&1 | tail -n 3
sleep 20
docker ps --format "{{.Names}} | {{.Status}}" | grep echo-backend
echo "== 再验 import"
docker exec echo-backend python -c "import torchaudio, torchcodec, pyannote.audio as p; print('  torchaudio', torchaudio.__version__, '| torchcodec ok | pyannote', p.__version__)" 2>&1 | tail -n 4
echo "== ffmpeg 在不在"
docker exec echo-backend bash -c "command -v ffmpeg; ldconfig -p | grep -c libavcodec" 2>&1 | tail -n 3
echo PATCH_DONE
