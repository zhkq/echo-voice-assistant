# -*- coding: utf-8 -*-
"""modelinfo.py — 客户端本地模型清单（**已退役**）

## 2026-10-31 用户拍板

客户端默认档**不再装任何本地 STT 模型/引擎**；指令、回顾、会议转写统一走
能力后端（echo-server + Qwen3-ASR）。语音唤醒取消。因此：

* `CATALOG` 为空列表 —— 客户端没有可下载/可清理的本地模型；
* `inventory()` / `model_paths()` / `jobs()` 都返回空形状（**公开签名保持不变**，
  `/api/models`、向导、清理页仍可安全调用）；
* `start_download()` 明确拒绝：不再提供本地模型下载；
* `ready()` 对任何 id 返回 `None`（查不到这个模型）。

保留下来的只有：
  * `models_dir()` / `MS_CACHE` —— 其它模块（如 `app/audio/stt.py`）还要问"模型根在哪"；
  * `job_state()` / `explain_download_error()` —— 历史任务/错误话术的归一化函数，
    仍有调用方按老形状读；
  * `_snapshot()` —— 通用的"先 ModelScope 后 HF"下载助手（只有历史排障/单测会用）。

服务端（能力后端）自己的模型清单在 `server/engines.py`，与客户端无关。
"""
import os
import threading
import time
from typing import Dict, Optional

from app import paths

#: 安装根由路径层给（含 ECHO_ROOT 覆盖）；HF_HOME 的兜底值也用它，所以先定义。
#: 2026-10-31 客户端虽已不再下本地模型，但 **HF_HOME 仍是 `models_root()`** 这条
#: 约定要留着：历史缓存/迁移脚本/排障都以它为唯一落点（测试 test_install_base_layout
#: 钉着；改成别的值会让"权重下到 A、去 B 找"重新出现）。
BASE_DIR = paths.echo_root()
os.environ.setdefault("HF_HOME", paths.models_root())

#: 模型根（客户端仍用它判断/显示路径；默认档下可以不存在）。
def models_dir() -> str:
    return paths.models_root()


#: 与 `app/audio/stt.py` 同一份 ModelScope 缓存约定（那边懒导入本常量）。
MS_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "modelscope", "models")

#: pyannote 三件套的仓库/落点映射（**历史常量**）：服务端打包脚本与契约用例仍拿它比对；
#: 客户端已不再下载/检查 pyannote（分离在能力后端那侧做）。
PYANNOTE_ASSETS = (
    ("pyannote/segmentation-3.0", "pyannote-segmentation-3.0-local", ["pytorch_model.bin"]),
    ("pyannote/wespeaker-voxceleb-resnet34-LM", "pyannote-wespeaker-local", ["pytorch_model.bin"]),
    ("pyannote/speaker-diarization-community-1", "pyannote-plda-local",
     ["plda/plda.npz", "plda/xvec_transform.npz"]),
)


#: 客户端不再有本地模型条目（保留变量名：外部按 CATALOG 遍历的代码不用改）。
CATALOG = []

#: 下载任务表（保留形状：`/api/models` 仍返回 jobs）。
_JOBS: Dict[str, dict] = {}
_ACTIVE = {"id": None}
_JOB_LOCK = threading.Lock()

#: 下载任务的状态词（归一后）。生产端只写 running/done/failed，
#: 但调用方历史上有人判断 "error" —— 见 job_state() 的说明。
JOB_STATES = ("queued", "running", "done", "failed")


def _by_id(mid):
    """按 id 取条目（客户端已无本地模型，恒为 None；保留给清理页/旧调用方）。"""
    return None


def ready(model_id):
    """客户端本地模型是否就绪：**恒为 None**（不再有本地模型）。"""
    return None


def ready_pyannote():
    """客户端不再做本地说话人分离 —— 分离在能力后端那侧（pyannote + wespeaker）。

    保留函数名只是为了让历史调用方（boot/安装检查）拿到一个明确的 False。
    """
    return False


def inventory():
    """`/api/models` 的 items：客户端已没有本地模型 → 空列表。"""
    return []


def dependency_problem(model_id) -> dict:
    """客户端不再需要模型下载客户端/引擎依赖 → 没有"缺依赖"这回事。"""
    return {}


def model_paths(mid):
    """这个模型在本机的落点：客户端已无本地模型 → 空列表。"""
    return []


def start_download(mid, force=False):
    """客户端不再提供本地模型下载（转写统一走能力后端）。"""
    return False, ("客户端已不再安装本地模型：指令/回顾/会议转写统一走能力后端"
                   "（echo-server + Qwen3-ASR）。")


def jobs():
    """当前/历史下载任务（客户端没有本地模型 → 空）。"""
    return {"active": None, "items": {}}


def job_state(job) -> str:
    """把一条下载任务归一成 ``queued | running | done | failed``。

    保留这个函数是因为面板/向导仍按老形状读历史任务；归一规则本身与模型无关。
    """
    s = str((job or {}).get("status") or "").strip().lower()
    if s in ("failed", "error", "fail"):
        return "failed"
    if s in ("running", "downloading"):
        return "running"
    if s in ("done", "ok", "ready"):
        return "done"
    return "queued"


#: 失败原因的关键词 → 人话（历史下载/排障话术；客户端已不再下载模型，但保留兜底）。
_ERROR_HINTS = (
    (("certificate verify failed", "certificate_verify_failed", "sslerror", "self signed",
      "self-signed", "unable to get local issuer", "certverifyfailed"),
     "证书校验失败（公司代理常见的中间人证书问题）—— 不要关校验，改用 ModelScope 源或换网络"),
    (("proxy", "407", "tunnel connection failed", "connect tunnel"),
     "被代理拦下（需要认证或代理不通）—— 检查系统代理设置，或把模型源地址加进白名单"),
    (("403", "forbidden", "gated"),
     "源站拒绝（403 / 需要同意条款）—— 到该模型页面同意条款，或改用 ModelScope 镜像"),
    (("401", "unauthorized"),
     "需要登录（401）—— 填 HF Token（设置 → 模型）或改用 ModelScope 源"),
    (("getaddrinfo", "name or service not known", "nodename nor servname",
      "temporary failure in name resolution", "max retries exceeded", "connection refused",
      "connection reset", "timed out", "timeout"),
     "网络不通或超时 —— 稍后重试；公司网里多半要走代理"),
)


def explain_download_error(raw):
    """把底层下载异常翻成一句**能照着做**的人话；认不出返回空串（不硬编）。"""
    low = str(raw or "").lower()
    if not low:
        return ""
    for keys, tip in _ERROR_HINTS:
        if any(k in low for k in keys):
            return tip
    return ""


def _snapshot(ms_id: str = "", hf_id: str = "", local_dir=None, allow=None) -> str:
    """按「先 ModelScope、失败再 HF」的顺序拉一个模型仓库，返回实际用的源。

    **客户端已不再用它下载 STT 模型**；保留它只为历史排障脚本/单测，
    新的、与客户端无关的模型下载应当由能力后端自己管。
    """
    errs = []
    if ms_id:
        try:
            from modelscope import snapshot_download as ms_dl
            ms_dl(ms_id, local_dir=local_dir, allow_patterns=allow)
            return "modelscope"
        except Exception as e:                       # noqa: BLE001 - 要把原因带上去
            errs.append("ModelScope(%s): %s" % (ms_id, e))
    if hf_id:
        try:
            from huggingface_hub import snapshot_download as hf_dl
            hf_dl(hf_id, local_dir=local_dir, allow_patterns=allow)
            return "huggingface"
        except Exception as e:                       # noqa: BLE001
            errs.append("HF(%s): %s" % (hf_id, e))
    detail = "；".join(errs) or "没有可用的下载源"
    tip = explain_download_error(detail)
    raise RuntimeError(("%s\n怎么办：%s" % (detail, tip)) if tip else detail)
