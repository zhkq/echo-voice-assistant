"""Install ECHO's optional diarization dependencies and official local model files."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = ROOT / "models" / "pyannote"
ASSETS = (
    ("pyannote/segmentation-3.0", "pyannote-segmentation-3.0-local", "pytorch_model.bin"),
    ("pyannote/wespeaker-voxceleb-resnet34-LM", "pyannote-wespeaker-local", "pytorch_model.bin"),
    ("pyannote/speaker-diarization-community-1", "pyannote-plda-local", "plda/plda.npz"),
    ("pyannote/speaker-diarization-community-1", "pyannote-plda-local", "plda/xvec_transform.npz"),
)
ACCESS_HELP = (
    "请先在 Hugging Face 官方页面同意模型使用条件：\n"
    "https://huggingface.co/pyannote/segmentation-3.0\n"
    "https://huggingface.co/pyannote/speaker-diarization-community-1\n"
    "然后使用当前环境的 hf auth login 登录（只读 Token），或设置 HF_TOKEN，再重试。"
)


def complete(root=MODEL_ROOT):
    return all((root / folder / filename).is_file()
               and (root / folder / filename).stat().st_size > 0
               for _, folder, filename in ASSETS)


def download():
    # This script runs in a separate process: do not change the server's offline mode.
    os.environ["HF_HUB_OFFLINE"] = "0"
    # ① ModelScope 优先：三个仓库在那边**同名且匿名可下**（2026-09-21 实测），
    #    既不用 HF Token，也不碰公司代理对 hf 证书的拦截。
    try:
        from modelscope import snapshot_download
        for repo, folder, filename in ASSETS:
            print(f"下载 {repo} / {filename}（ModelScope）", flush=True)
            snapshot_download(repo, local_dir=str(MODEL_ROOT / folder),
                              allow_patterns=[filename])
        if complete():
            print("模型文件下载完成（来自 ModelScope）。")
            return
        print("ModelScope 下来的文件不完整，改用 Hugging Face。", flush=True)
    except Exception as exc:
        print(f"ModelScope 这条路没成（{type(exc).__name__}），改用 Hugging Face。", flush=True)
    # ② 回落 HF（gated：需要同意条款 + Token）
    os.environ["HF_ENDPOINT"] = "https://huggingface.co"
    from huggingface_hub import get_token, hf_hub_download

    token = get_token()
    # ECHO sets HF_HOME to its model directory; also recognize the ordinary CLI login.
    default_token = Path.home() / ".cache" / "huggingface" / "token"
    if not token and default_token.is_file():
        token = default_token.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(ACCESS_HELP)
    for repo, folder, filename in ASSETS:
        print(f"下载 {repo} / {filename}", flush=True)
        hf_hub_download(repo_id=repo, filename=filename, token=token,
                        endpoint="https://huggingface.co", local_dir=str(MODEL_ROOT / folder))
    if not complete():
        raise RuntimeError("模型文件不完整，请重试下载。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-only", action="store_true", help="仅下载模型，不改 Python 依赖")
    parser.add_argument("--check", action="store_true", help="仅检查文件，不联网或安装")
    args = parser.parse_args()
    if args.check:
        print("模型文件完整" if complete() else "模型文件缺失或不完整")
        return 0 if complete() else 1
    if not args.download_only:
        if sys.prefix == sys.base_prefix:
            print("请使用项目 venv 中的 Python 执行此命令，避免修改系统环境。")
            return 1
        print("安装可选依赖（包含 PyTorch，体积较大）。请先停止 ECHO，完成后重新启动。", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "pyannote.audio>=4.0,<5", "speechbrain>=1.0,<2", "huggingface-hub>=0.34,<2"],
                       check=True)
    try:
        download()
    except Exception as exc:
        # Do not expose request objects, headers, URLs containing credentials, or tokens.
        if type(exc).__name__ in ("GatedRepoError", "HfHubHTTPError"):
            print("下载未完成，请检查网络和账号的模型访问权限。\n" + ACCESS_HELP)
        elif isinstance(exc, RuntimeError):
            print(str(exc))
        else:
            print(f"下载失败（{type(exc).__name__}），请检查网络、磁盘或 Python 依赖后重试。")
        return 1
    if args.download_only:
        print("模型文件下载完成；运行依赖请使用「复制安装命令」安装，然后重启 ECHO。")
    else:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
        subprocess.run([sys.executable, "-c",
                        "from app.audio.diarize import _load_pipeline; _load_pipeline(); "
                        "print('离线加载检查通过，请重启 ECHO 后启用说话人分离。')"],
                       cwd=str(ROOT), check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
