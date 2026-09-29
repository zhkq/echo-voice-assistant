#!/bin/bash
# 1 秒采样 5 分钟：GPU 利用率/显存/温度/功耗、CPU 空闲率、内存、容器占用、后端 health。
# 用法：bash mon5.sh [秒数]
N=${1:-300}
OUT=/home/zysk/echo/mon.csv
: > "$OUT"
for i in $(seq 1 "$N"); do
  ts=$(date +%H:%M:%S)
  g=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
  idle=$(top -bn1 2>/dev/null | grep -m1 '^%Cpu' | sed 's/.*, *\([0-9.]*\) id.*/\1/')
  cpu=$(awk -v i="$idle" 'BEGIN{printf "%.0f", 100-i}')
  mem=$(free -m | awk '/^Mem:/{printf "%d/%dMB used=%d%%", $3,$2,$3*100/$2}')
  ctr=$(docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' echo-backend 2>/dev/null | tr -d ' ')
  h=$(curl -s --max-time 2 http://127.0.0.1:8900/v1/health 2>/dev/null)
  b=$(printf '%s' "$h" | python3 -c "import sys,json;d=json.load(sys.stdin);print('active=%s vramMb=%s %s'%(d['busy']['active'],d['vram']['usedMb'],';'.join('%s:%s'%(k,v) for k,v in d['models'].items())))" 2>/dev/null)
  echo "$ts gpu=$g cpu=${cpu}% $mem ctr=$ctr $b" >> "$OUT"
  sleep 1
done
echo "END $(date +%H:%M:%S)" >> "$OUT"
