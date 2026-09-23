# install-qwen3asr.ps1 — 安装 Qwen3-ASR 会议转写引擎（依赖 + 模型下载）
# 依赖：qwen-asr==0.0.6 + transformers==4.57.6（会从 5.x 降级，仅影响 Qwen3-ASR 相关）
# 模型：Qwen/Qwen3-ASR-0.6B（~4GB 显存，modelscope 缓存，无中文路径问题）
$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
# 解释器解析顺序与 scripts\start.ps1 / startup.ps1 / check-windows.ps1 **保持一致**：
# 2.0 的运行时目录叫 runtime-core（uv 建的），只有老树才叫 venv。
# 这里原来只认 venv\Scripts\python.exe，于是 runtime-core 安装上点面板的
# 「下载 Qwen3-ASR」必然报 "缺少 venv"（2026-09-23 实测）。
$py = Join-Path $root 'runtime-core\python.exe'
if (-not (Test-Path $py)) { $py = Join-Path $root 'runtime-core\Scripts\python.exe' }
if (-not (Test-Path $py)) { $py = Join-Path $root 'venv\Scripts\python.exe' }
# 只有"自己路径含非 ASCII"的树才借 ECHO_PYTHON（指向 venv 的 ASCII 目录联接）：
# 路径本来就是 ASCII 的树若借了，会把依赖装进**别人的**运行时里（与 start.ps1 同一条规则）。
$pyAlt = $env:ECHO_PYTHON
if ($pyAlt -and (Test-Path $pyAlt) -and ($root -match '[^\x20-\x7E]')) { $py = $pyAlt }
if (-not (Test-Path $py)) { Write-Host "runtime missing (runtime-core\ or venv\): $py" -ForegroundColor Red; exit 1 }

Write-Host '[1/2] 安装 qwen-asr 依赖（清华镜像）...' -ForegroundColor Cyan
& $py -m pip install --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple `
    "qwen-asr==0.0.6" "transformers==4.57.6" "accelerate==1.12.0"
if ($LASTEXITCODE -ne 0) {
    Write-Host '[!] 依赖安装失败' -ForegroundColor Red
    exit 1
}

Write-Host '[2/3] 下载 Qwen/Qwen3-ASR-0.6B 模型（modelscope）...' -ForegroundColor Cyan
& $py -c "from modelscope import snapshot_download; p = snapshot_download('Qwen/Qwen3-ASR-0.6B'); print('模型就绪:', p)"
if ($LASTEXITCODE -ne 0) {
    Write-Host '[!] 模型下载失败（可稍后重试，模型已下部分会续传）' -ForegroundColor Red
    exit 1
}

Write-Host '[3/3] 下载 Qwen/Qwen3-ForcedAligner-0.6B（句子时间戳对齐器）...' -ForegroundColor Cyan
& $py -c "from modelscope import snapshot_download; p = snapshot_download('Qwen/Qwen3-ForcedAligner-0.6B'); print('对齐器就绪:', p)"
if ($LASTEXITCODE -ne 0) {
    Write-Host '[!] 对齐器下载失败（转写仍可用，但句子时间戳会退回 whisper 骨架）' -ForegroundColor Yellow
}

Write-Host ''
Write-Host '完成！在面板 设置→会议→会议转写模型 选择 qwen3asr 即可使用' -ForegroundColor Green
