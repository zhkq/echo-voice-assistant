# setup.ps1 — ECHO 一次性初始化（venv 校验 / 补装依赖 / 模型校验 / 建库）
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
# D22: main package has no bundled runtime; runtime-core component provides it.
$py = Join-Path $root 'runtime-core\python.exe'
if (-not (Test-Path $py)) { $py = Join-Path $root 'runtime-core\Scripts\python.exe' }
if (-not (Test-Path $py)) { $py = Join-Path $root 'venv\Scripts\python.exe' }
# 若存在 ASCII junction（解决 nagisa/dynet 无法读中文路径的问题），优先使用
$pyAlt = $env:ECHO_PYTHON   # 可选：非 ASCII 路径下的解释器覆盖，见 docs/DEPLOY.md
if ($pyAlt -and (Test-Path $pyAlt)) { $py = $pyAlt }

Write-Host '=== ECHO setup ===' -ForegroundColor Cyan

# 1. venv
if (-not (Test-Path $py)) {
    Write-Host '[!] 未找到运行时（runtime-core\python.exe 或 venv\Scripts\python.exe）' -ForegroundColor Yellow
    Write-Host '    主包：让 install.ps1 准备 runtime-core；整包/源码：按 docs/DEPLOY.md 建 venv' -ForegroundColor Yellow
    exit 1
}
Write-Host "[1/4] venv OK: $py"

# 2. 补装轻量依赖（fastapi/uvicorn/pydantic；重依赖已随 venv 就位）
& $py -c "import fastapi, uvicorn, pydantic, multipart, httpx, yaml, ruamel.yaml, soxr" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[2/4] 安装 Web/API 依赖（fastapi/uvicorn/pydantic/multipart/httpx/yaml/ruamel/soxr）...'
    & $py -m pip install --disable-pip-version-check -q fastapi "uvicorn[standard]" pydantic python-multipart httpx PyYAML ruamel.yaml soxr
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[!] 安装失败，尝试清华镜像...' -ForegroundColor Yellow
        & $py -m pip install --disable-pip-version-check -q -i https://pypi.tuna.tsinghua.edu.cn/simple fastapi "uvicorn[standard]" pydantic python-multipart httpx PyYAML ruamel.yaml soxr
    }
} else {
    Write-Host '[2/4] Web 依赖已就位'
}

# 3. 模型
$need = @('models\faster-whisper\small\model.bin', 'models\sensevoice', 'models\sherpa-onnx-streaming',
          'models\wakeword\kws-zh-en-3m\tokens.txt', 'models\pyannote\pyannote-segmentation-3.0-local')
$missing = @($need | Where-Object { -not (Test-Path (Join-Path $root $_)) })
if ($missing.Count -gt 0) {
    Write-Host "[!] 模型缺失（面板 → 设置 → 模型 里一键下载）:" -ForegroundColor Yellow
    $missing | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
} else {
    Write-Host '[3/4] 本地模型齐全（faster-whisper/SenseVoice/sherpa/KWS/pyannote）'
}

# 4. 建库 + 种子配置
Write-Host '[4/4] 初始化数据库...'
& $py -c "import app.db, app.config; app.db.init(); app.config.settings.seed_defaults(); print('DB OK:', app.db.DB_FILE)"
if ($LASTEXITCODE -ne 0) { Write-Host '[!] 数据库初始化失败' -ForegroundColor Red; exit 1 }

# 5. (retired 2026-09-17) The DSH Desktop host plugin (echo-host) is no longer
#    deployed. ECHO starts through scripts\start.ps1 / scripts\startup.ps1 (its own
#    autostart) and the Ctrl+Shift+E dashboard is ECHO's own .NET sidebar.
#    Details: plugin/README.md

Write-Host ''
Write-Host '完成！启动:  scripts\start.ps1   （面板地址见 data\echo-port.txt）' -ForegroundColor Green
