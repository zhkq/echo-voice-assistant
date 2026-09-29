#!/bin/bash
# 修 torchaudio：pip 从 pypi 镜像装到了 CUDA 13 的 torchaudio 2.11（要 libcudart.so.13），
# 而 torch 是 2.14.0+cu126（提供 libcudart.so.12）→ _torchaudio.abi3.so 加载失败 → pyannote 崩。
# 正确做法：torchaudio 必须**与 torch 同版本、同源（cu126）**。
set -e
cd /home/zysk/echo/backend
cat > Dockerfile.torchaudio <<'EOF'
FROM echo-backend:0.1.0-cu126
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu126 torchaudio==2.14.0
EOF
echo "== 构建补丁层"
docker build -f Dockerfile.torchaudio -t echo-backend:0.1.0-cu126 . 2>&1 | tail -n 8
echo "== 重建容器"
docker compose up -d --force-recreate 2>&1 | tail -n 2
sleep 20
echo "== 验 import"
docker exec echo-backend python -c "import torchaudio, pyannote.audio as p; print('  torchaudio', torchaudio.__version__, '| pyannote', p.__version__, 'OK')" 2>&1 | tail -n 4
docker exec echo-backend python -c "import torchcodec; print('  torchcodec OK')" 2>&1 | tail -n 2
echo "== 容器状态"
docker ps --format "{{.Names}} | {{.Status}}" | grep echo-backend
echo PATCH2_DONE
