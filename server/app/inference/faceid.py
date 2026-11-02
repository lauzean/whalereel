"""本地锁脸：参考图条件通路（IP-Adapter FaceID 接线，可用性门控）。

**两条锁脸路线**（按模型家族分工，都是「把角色参考图喂进生成条件」）：

1. **HiDream-O1 家族 → 原生参考图条件**（首选，无新增依赖）：
   HiDream-O1 是 Qwen3-VL 改造的 Pixel-DiT，`pipeline.generate_image` 本就
   接受 `ref_image_paths` 并与提示词**联合编码**（图文交错条件）。runner 此前
   从不传参考图，等于把这条最强的本地一致性通路闲置。接线见
   `runners/generate.py::_real_hidream`。

2. **diffusers 家族（flux / qwen-image / sdxl）→ IP-Adapter FaceID**（本模块）：
   这些管线的文本编码器与图像条件解耦，需要外挂 IP-Adapter 才能吃参考图。
   依赖 `diffusers` 的 IP-Adapter mixin + `insightface`（人脸检测/识别嵌入）。
   竞品调研显示该路线 + 专属 LoRA 可把角色一致性做到 ≈97%（见
   docs/design/13 第 5 节）。

**设计原则**：本模块是**可用性门控**的旁路——依赖或硬件任一缺失时，全部函数
退化为 no-op（返回 None/空 kwargs），调用方行为与本模块引入前**逐字一致**，
绝不因为「多了个可选增强」而让原路径失败。可用性判定只看确定性事实
（包是否可导入、CUDA 是否可用、管线是否具名 mixin），不猜、不试探性加载。

**验证状态**：本机为 CPU-only（`torch.cuda.is_available() == False`）且
`diffusers`/`insightface`/`ip_adapter` 均未安装，路线 2 的**实际加载与推理
路径未在本机运行验证**，只做了导入安全与可用性判定的静态验证。有卡环境
首次启用时请按 `docs/design/13` 第 5 节的验证清单跑一遍再投产。
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from typing import Any

logger = logging.getLogger(__name__)

# IP-Adapter FaceID 权重仓库（diffusers 官方组织，SDXL / SD1.5 两档）
FACEID_REPO_SDXL = "h94/IP-Adapter-FaceID"
FACEID_REPO_SD15 = "h94/IP-Adapter"
FACEID_WEIGHT_SDXL = "ip-adapter-faceid_sdxl.bin"
FACEID_WEIGHT_SD15 = "ip-adapter-faceid_sd15.bin"

# IP-Adapter 条件强度：过高会盖掉提示词（画面变"贴脸"），过低无锁脸效果。
# 0.6 为竞品与社区常用起点；经参数下发可覆盖（params.faceid_scale）。
DEFAULT_SCALE = 0.6
SCALE_MIN = 0.0
SCALE_MAX = 1.5

# 环境开关：默认启用（依赖缺失时自动 no-op）；置 0 可强制关闭该旁路
ENV_ENABLE = "AVS_FACEID"

# 已应用过 IP-Adapter 的管线（弱引用集合：GC 后自动移除，不存在 id 复用误判）
_applied: "weakref.WeakSet[Any]" = weakref.WeakSet()
_lock = threading.Lock()


def _env_enabled() -> bool:
    """环境开关判定（未设置视为启用）。"""
    return str(os.environ.get(ENV_ENABLE, "1")).strip().lower() not in ("0", "false", "no")


def faceid_requirements() -> dict:
    """逐项列出 FaceID 的前置条件与达成情况（供状态外露与断言使用）。

    返回 `{"env": bool, "cuda": bool, "deps": bool, "missing": [...]}`。
    拆成逐项而非单个布尔，是为了让"不可用"**说得出原因**——只报「不可用」
    会让人误以为是硬件问题（本机有 4090 却因 insightface 未装而不可用）。
    """
    env = _env_enabled()
    cuda = False
    try:
        import torch

        cuda = bool(torch.cuda.is_available())
    except Exception:
        cuda = False
    missing: list[str] = []
    for mod in ("diffusers", "insightface"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    return {"env": env, "cuda": cuda, "deps": not missing, "missing": missing}


def faceid_available() -> bool:
    """IP-Adapter FaceID 是否可用（环境开关 + CUDA + 依赖，全部满足才算）。

    只做**可导入性**检查：真正加载放到首次使用时（加载要占显存，不能在
    探活阶段付出代价）。
    """
    req = faceid_requirements()
    return bool(req["env"] and req["cuda"] and req["deps"])


def supports_ip_adapter(pipe: Any) -> bool:
    """管线是否具备 IP-Adapter mixin 接口（不支持则不该尝试加载）。"""
    return all(hasattr(pipe, name) for name in ("load_ip_adapter", "set_ip_adapter_scale"))


def is_faceid_family(model_id: str) -> bool:
    """模型是否属于需要外挂 IP-Adapter 的 diffusers 家族。

    HiDream-O1 走原生参考图条件（见模块 docstring 路线 1），不在此列。
    """
    mid = (model_id or "").lower()
    if mid.startswith("hidream"):
        return False
    return mid.startswith(("flux", "qwen-image", "qwen_image", "sdxl", "stable-diffusion"))


def _pick_weight(model_id: str) -> tuple[str, str, str | None]:
    """按模型家族选 FaceID 权重（仓库, 权重文件名, 子目录）。"""
    mid = (model_id or "").lower()
    if mid.startswith(("sdxl", "stable-diffusion-xl")):
        return FACEID_REPO_SDXL, FACEID_WEIGHT_SDXL, "sdxl_models"
    return FACEID_REPO_SD15, FACEID_WEIGHT_SD15, None


def _clamp_scale(raw: Any) -> float:
    """条件强度钳位（非法值回落到默认）。"""
    try:
        return max(SCALE_MIN, min(SCALE_MAX, float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_SCALE


def apply_faceid(pipe: Any, model_id: str, ref_paths: list[str],
                 scale: Any = None) -> dict:
    """按需给管线挂上 IP-Adapter FaceID，返回要并入采样调用的 kwargs。

    - 不可用 / 不支持的管线 / 无参考图 → 返回 `{}`（调用方原样继续）；
    - 可用 → 幂等加载权重、设定强度，返回 `{"ip_adapter_image": [PIL, ...]}`。

    任何异常都吞掉并降级为 `{}`：锁脸是**增强**，不能让原本能出的图变成失败。
    """
    if not ref_paths or not faceid_available() or not is_faceid_family(model_id):
        return {}
    if not supports_ip_adapter(pipe):
        logger.info("管线不支持 IP-Adapter（%s），跳过锁脸", model_id)
        return {}

    repo, weight, subfolder = _pick_weight(model_id)
    try:
        with _lock:
            if pipe not in _applied:
                kwargs = {"weight_name": weight}
                if subfolder:
                    kwargs["subfolder"] = subfolder
                if hasattr(pipe, "image_encoder"):
                    kwargs["image_encoder_folder"] = "models/image_encoder"
                pipe.load_ip_adapter(repo, **kwargs)
                _applied.add(pipe)
            pipe.set_ip_adapter_scale(_clamp_scale(scale))
        from PIL import Image

        images = [Image.open(p).convert("RGB") for p in ref_paths]
        return {"ip_adapter_image": images}
    except Exception as exc:  # 增强失败不拖垮主路径
        logger.warning("IP-Adapter FaceID 挂载失败（%s），按纯文生图继续：%s",
                       model_id, exc)
        with _lock:
            _applied.discard(pipe)
        return {}


def faceid_note(model_id: str, has_refs: bool) -> str:
    """给用户看的锁脸状态说明（无参考图时返回空串）。

    不可用时要**说清缺哪一项**：只报「不可用」会让人误判为硬件问题——
    实测本机有 4090，却因为 `insightface` 未装而不可用。
    """
    if not has_refs:
        return ""
    if not is_faceid_family(model_id) or faceid_available():
        return ""
    req = faceid_requirements()
    why: list[str] = []
    if not req["env"]:
        why.append(f"环境开关 {ENV_ENABLE}=0 已关闭")
    if not req["cuda"]:
        why.append("无可用 CUDA")
    if req["missing"]:
        why.append("缺依赖 " + "/".join(req["missing"]))
    return ("锁脸（IP-Adapter FaceID）不可用（" + "、".join(why) +
            "）；当前按纯文生图生成，人物一致性依赖提示词描述")


def forget_pipe(pipe: Any) -> None:
    """管线被卸载/重建时清掉挂载记录（弱集合本会自动回收，此处供显式调用）。"""
    with _lock:
        _applied.discard(pipe)
