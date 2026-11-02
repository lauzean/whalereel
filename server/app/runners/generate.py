"""生成类 runner：剧本拆解 / 文生图 / 文生视频 / 图生视频 / 首尾帧 / 配音。

real 路径统一走 app.inference.pool 资源池：同模型驻留零加载、LRU 换载、
GPU+CPU 混合 offload（apply_performance 由池在加载时自动应用）；
参数按各模型官方推荐值（HiDream-O1 full 50 步 / guidance 5.0、
Qwen-Image 官方分辨率档位、Wan 16fps / guidance 5.0、LTX-2 8 步蒸馏）。
依赖缺失时回退 mock；模型未安装 / 管线不支持时抛 RunnerError 明确报错，
不静默回退。
"""

import logging
import math
import random
from pathlib import Path

from app.core import comfyui as comfyui_client
from app.core.config import settings
from app.core.provider_store import ENDPOINT_MODEL_SEP
from app.core.providers import (
    FailClass,
    ProviderError,
    download_video,
    is_endpoint_model,
    llm_available,
    llm_chat,
    provider_enabled,
    provider_outputs_audio,
    resolve_runtime_config,
    resolve_seed,
    DEFAULT_T2I_REF_BUDGET,
    t2i_ref_budget,
    video_capability,
    t2i_agnes,
    t2i_ark,
    t2i_openai,
    tts_openai,
    t2v_agnes,
    t2v_agnes_fetch,
    t2v_agnes_submit,
    t2v_ark,
    t2v_ark_fetch,
    t2v_ark_submit,
    t2v_comfyui_fetch,
    t2v_comfyui_submit,
    t2v_custom,
    t2v_vidu_fetch,
    t2v_vidu_submit,
)
from app.core.providers import prompt_budget as provider_prompt_budget
from app.core.prompt_metrics import prompt_quality_metrics
from app.script.first_frame_refine import (
    REFINE_PARAM_KEY,
    RefineOutcome,
    edit_notes,
    refine_first_frame,
)
from app.inference import conditioning
from app.inference.conditioning import conditioning_key
from app.inference.faceid import apply_faceid, faceid_available, faceid_note
from app.inference.pool import (
    SLOW_PERF_MODES,
    default_loader,
    pipeline_class,
    pool,
)
from app.runners.base import (
    BaseRunner, RunnerContext, RunnerError, RunnerResult, inference_available,
)
from app.runners.mock_helpers import mock_audio, mock_image, mock_video

logger = logging.getLogger(__name__)

# 视频比例 → 分辨率（768p 档）
# 画幅/自适应尺寸统一取自契约层（core/contracts，单一来源）——此前本文件与
# drama API 各持一份画幅表，1:1 一处 960 一处 1024（同类漂移是审计反复抓到的
# 缺陷形态：同一份保证散落多条路径）。此处保留别名以兼容既有引用。
from app.core.contracts import (  # noqa: E402  位置：紧随常量区，保持既有引用可用
    ADAPTIVE_SIZE, ASPECT_WH as ASPECTS,
)

# 本地模型家族前缀：任务显式选择这些模型时跳过第三方 provider，直接走本地推理
LOCAL_MODEL_PREFIXES = (
    "minimax-h3", "ltx", "wan", "flux", "qwen", "hidream", "sdxl",
    "stable-diffusion", "qwen3-8b",
)


def _resolve_provider_seed(ctx: RunnerContext, kind: str, params: dict) -> tuple[int | None, str]:
    """任务种子 → (下发给云端端点的值 | None, 说明文本)。

    **端点能力不同，实测所得**（见 server/_probe_cloud_params.py）：agnes 的 seed
    合法域是 [-1, 999] 且同 seed 可复现；ark Seedream 对 seed **静默忽略**
    （同 seed 两次出图不同、请求仍 200）。判定按端点名走 providers.resolve_seed，
    此处只负责取任务原始值并转交——不做任何模型名匹配（项目铁律）。

    说明文本会随 generation 记录落 trace：**"传了但没生效"必须被看见**，
    否则用户会按"固定种子重试"的预期行事，而结果根本不可复现。
    """
    raw = (params or {}).get("seed")
    if raw in (None, ""):
        return None, ""
    cfg, _model = resolve_runtime_config(kind, ctx.raw_model_id)
    return resolve_seed(cfg.name, raw)


def _with_country(params: dict, prompt: str) -> str:
    """项目国家预设 → 提示词前缀约束（人物外貌符合项目设定国家，如 中国人=东亚面孔）。

    由任务引擎在提交时注入 params["country_hint"]；提示词已含该约束时不重复。
    """
    hint = str(params.get("country_hint") or "").strip()
    if not hint or hint in prompt:
        return prompt
    return f"{hint}，{prompt}"


def _is_local_model(model_id: str) -> bool:
    """任务选择的模型是否为本地注册表模型（决定 provider 分发去留）。"""
    mid = (model_id or "").lower()
    if ENDPOINT_MODEL_SEP in mid:
        return False  # 端点编码串必然是第三方模型
    return any(mid.startswith(p) for p in LOCAL_MODEL_PREFIXES)


# 文生图官方分辨率档位（质量优先，非任意尺寸）：
# Qwen-Image 官方推荐档（模型卡），约 1.0-1.8MP
T2I_ASPECTS_QWEN = {
    "1:1": (1328, 1328),
    "16:9": (1664, 928),
    "9:16": (928, 1664),
    "4:3": (1472, 1140),
    "3:4": (1140, 1472),
    "2:3": (1024, 1536),
}
# HiDream-O1 官方训练档（generate_image 内部 snap 至该列表，最大 2048²）
T2I_ASPECTS_HIDREAM = {
    "1:1": (2048, 2048),
    "16:9": (2560, 1440),
    "9:16": (1440, 2560),
    "4:3": (2304, 1728),
    "3:4": (1728, 2304),
    "2:3": (1664, 2496),
}
# 无官方档位表模型的画幅基准（方舟 Seedream 由 provider 层就近映射官方档、
# OpenAI 兼容服务按宽高就近映射支持档）：保证 params.aspect 对所有模型路径
# 都生效——角色定妆图的竖版 3:4 依赖此表（横画幅易把站立人物生成横躺）
T2I_ASPECT_FALLBACK = {
    "1:1": (1024, 1024),
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "4:3": (1024, 768),
    "3:4": (768, 1024),
    "2:3": (832, 1248),
}
# VAE tiling 按需启用阈值（像素面积）：达到才启用防 OOM；低于则关闭——
# QwenImage VAE tiling 在常规分辨率下有块状伪影（diffusers 已知问题）
_T2I_VAE_TILING_MIN_PIXELS = 1_800_000

# HiDream-O1 官方推理参数（github.com/HiDream-ai/HiDream-O1-Image inference.py）
_HIDREAM_FULL_STEPS = 50
_HIDREAM_FULL_GUIDANCE = 5.0
_HIDREAM_FULL_SHIFT = 3.0
_HIDREAM_FULL_SCHEDULER = "default"
_HIDREAM_DEV_STEPS = 28
_HIDREAM_DEV_GUIDANCE = 0.0  # 蒸馏版无 CFG（guidance <= 1.0 时不构建负样本）
_HIDREAM_DEV_SHIFT = 1.0
_HIDREAM_DEV_SCHEDULER = "flash"
_HIDREAM_NOISE_SCALE = 7.5  # dev 档 flash 调度器噪声参数（官方默认）
_HIDREAM_NOISE_CLIP_STD = 2.5

# HiDream-O1 本地参考图上限：pipeline 按参考图张数 K 分档降采样
# （K=1 满分辨率、K=2 约 0.75、K<=4 减半、更多再降），超过 4 张单图参考
# 分辨率掉到 1/2 以下，锁脸细节反而变差——限量并外露截断，不静默丢图。
_HIDREAM_MAX_REFS = 4

# 本地参考图的 DiT 侧 token 预算（patch=16px）：官方档按张数把参考图放大到与
# 目标图**同量级**（K=1 时 2048² → 16384 token），叠在 2048² 目标图的 16384
# token 上使 DiT 序列翻倍；Windows 无 flash-attn 走 4D mask 路径（mask 本身
# 即 GB 级）——**真机 RTX 4090/24GB 实测在第一个去噪步就 CUDA OOM**。
# 预算 4096 ≈ 目标图的 1/4（+25% 序列），K 张均分；只压不放大（与官方档取小）。
# 取 4096 而非"目标图一半"是**按实测余量定的保守值**：24GB 卡在 2048² 目标图
# 下留给激活的余量有限，且 Windows 走 4D mask 路径（mask 随序列平方增长）。
_HIDREAM_REF_TOKEN_BUDGET = 4096
_HIDREAM_PATCH = 16


def _hidream_ref_max_size(k: int) -> int:
    """按 token 预算推导参考图边长上限（patch 对齐，K 张均分预算）。"""
    if k <= 0:
        return 0
    side = math.isqrt(_HIDREAM_REF_TOKEN_BUDGET // k)
    return max(1, side) * _HIDREAM_PATCH


def _resolve_seed(params: dict) -> int:
    """解析 params.seed：留空/非法时随机（前端留空提交为随机种子）。"""
    raw = str(params.get("seed") or "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw) % 2**31  # 负数归一到正种子域
    return random.randrange(2**31)


# 文生图参考图上限 = providers 的默认预算（**唯一来源**）。历史上限 4 曾把
# 「角色→场景→道具」序尾部的角色参考图静默裁掉，提示词「参考图N」悬空导致
# 角色串脸；端点自身的更小上限（如方舟 4 张）由提交侧按端点能力分配编号、
# 由 provider 层截断并告警——角色已优先占号，不受影响。
_T2I_MAX_REFS = DEFAULT_T2I_REF_BUDGET


def _t2i_ref_paths(input_refs: dict | None) -> list[str]:
    """提取文生图参考图路径（input_refs.image 优先 + extra_images，保序去重）。"""
    refs: list[str] = []
    main = str((input_refs or {}).get("image") or "").strip()
    if main:
        refs.append(main)
    for extra in (input_refs or {}).get("extra_images") or []:
        p = str(extra or "").strip()
        if p and p not in refs:
            refs.append(p)
    return refs[:_T2I_MAX_REFS]


def _count_input_refs(input_refs: dict | None) -> int:
    """统计 input_refs 中的图片引用总数（image/first 主图 + extra_images）。"""
    refs = input_refs or {}
    total = 0
    if str(refs.get("image") or refs.get("first") or "").strip():
        total += 1
    total += len(refs.get("extra_images") or [])
    return total


# 提示词长度预算（字符）：来源为各模型技能包的官方建议（见 app/agent/skills/*.md）
# ——技能包只"声明"约束、代码无执行层时，规范会被静默突破（实测首帧 1171 字
# vs Seedream 建议 ≤300）。此处做【记录 + 超标外露】，不做截断（截断会丢信息，
# 收敛应由组装侧完成：约束样板压缩、画面内容前置）。
#
# **预算按端点取**（providers.prompt_budget）：方舟 Seedream ≤300、agnes 首帧
# ≤600——旧实现写成固定 600 且文案写死「Seedream ≤300」，结果是 Seedream 上
# 超标永远不报（它恰是对长度最敏感的模型），agnes 上又给出别的模型的建议。
# 具体数值与技能包的对账见 _verify_pipeline 断言 83。


def _prompt_budget_report(runner: str, prompt: str, model_id: str = "") -> dict:
    """提示词长度预算核对：{length, limit, over, hint}（无预算的 runner 返回空）。"""
    limit, hint = provider_prompt_budget(runner, model_id)
    if not limit:
        return {}
    length = len(prompt or "")
    return {"length": length, "limit": limit, "over": max(0, length - limit),
            "hint": hint if length > limit else ""}


def _record_generation(ctx: RunnerContext, runner: str, model_id: str,
                       prompt: str, params: dict, input_refs: dict,
                       ref_paths: list[str], seed_note: str = "") -> None:
    """生成参数全链路记录：trace_json.generation（前端「参数」弹窗可查）
    + backend.log（事后排障）。

    记录「最终发给模型的参数」：prompt（含国家/画风注入后）、参考图清单
    （顺序即提示词「参考图N」编号）、截断数量——排查串脸/构图错/方向反时
    逐项对账。记录失败不影响生成（try 包裹）。
    """
    if ctx.publish_generation is None:
        return
    refs_in = _count_input_refs(input_refs)
    payload = {
        "runner": runner,
        "model_id": model_id,
        "prompt": prompt,
        # 生成参数（prompt 已单列，其余原样：aspect/seed/duration 等）；
        # 下划线前缀键是记录元数据，单列到 prompt_notes、不混进生成参数
        "params": {k: v for k, v in (params or {}).items()
                   if k != "prompt" and not k.startswith("_")},
        # 系统对提示词做过的改动（脱参考图标注 / 剥离秒数锚点等）：改动必须
        # 外露——「参数被改了而用户不知道」是本项目反复出现的缺陷形态
        "prompt_notes": list((params or {}).get("_prompt_notes") or []),
        # 原始引用（artifact id，顺序即参考图编号）与实际上送路径
        "input_refs": input_refs or {},
        "ref_paths_used": [str(p) for p in ref_paths],
        "refs_in": refs_in,
        "refs_used": len(ref_paths),
        "refs_dropped": max(0, refs_in - len(ref_paths)),
        # 提示词长度预算（见 core/providers.prompt_budget）：只记录不截断——
        # 长度超标会让模型注意力被约束句稀释（实测首帧 1171 字 vs 模型上限 300），
        # 是「画面空洞/细节丢失」的隐性来源。超标项随 generation 记录外露，
        # 前端「查看参数」与后端日志都可见，供组装侧收敛。
        "prompt_budget": _prompt_budget_report(runner, prompt, model_id),
        # 提示词质量指标（见 core/prompt_metrics）：长度之外的「干不干净」——
        # 约束占比/冗余率/禁令词次数/分段字数/疑似截断。20 号文档的验收纪律要求
        # 「改动前后对同一批镜头跑对比」，这些数字就是对比依据（全部确定性计算）。
        # segments 由组装侧显式给出（首帧四段结构无段名标记，反推不出）；
        # 未给则回退按段名解析（视频五段式）
        "prompt_quality": prompt_quality_metrics(
            prompt, (params or {}).get("prompt_segments")),
        # 种子下发说明（空 = 无异常）：端点不支持/种子被钳位时才非空
        "seed_note": seed_note,
    }
    try:
        ctx.publish_generation(payload)
        logger.info(
            "生成参数 runner=%s model=%s refs=%d→%d prompt=%d字 task=%s",
            runner, model_id, refs_in, len(ref_paths), len(prompt), ctx.task_id)
    except Exception:  # noqa: BLE001 记录失败不影响生成
        logger.exception("生成参数记录失败 task=%s", ctx.task_id)


def _generator(pipe, seed: int):
    """按管线执行设备构造生成器（CPU offload 管线的执行设备可能不在 CUDA）。"""
    import torch

    device = str(getattr(pipe, "_execution_device", "cuda"))
    try:
        return torch.Generator(device).manual_seed(seed)
    except Exception:
        return torch.Generator("cpu").manual_seed(seed)


def _resolve_t2i_size(model_id: str, params: dict) -> tuple[int, int]:
    """文生图分辨率：优先 params.aspect 官方档位；否则按宽高比就近映射。

    Qwen-Image / HiDream-O1 均按官方训练分辨率档生成（低分辨率/非常规比例
    会明显掉质量）；无档位表的其他模型按 T2I_ASPECT_FALLBACK 取画幅基准
    （aspect 合法时），保证竖版等画幅要求全模型生效；完全无尺寸线索时沿用
    params 宽高（默认 1280×720）。
    """
    aspects: dict | None = None
    if model_id.startswith("qwen-image"):
        aspects = T2I_ASPECTS_QWEN
    elif model_id.startswith("hidream-o1"):
        aspects = T2I_ASPECTS_HIDREAM
    if not aspects:
        aspect = params.get("aspect")
        if aspect in T2I_ASPECT_FALLBACK:
            return T2I_ASPECT_FALLBACK[aspect]
        return int(params.get("width", 1280)), int(params.get("height", 720))
    aspect = params.get("aspect")
    if aspect in aspects:
        return aspects[aspect]
    width = int(params.get("width", 0))
    height = int(params.get("height", 0))
    if width > 0 and height > 0:
        # 宽高比就近匹配（对数空间比较，避免大比例偏差主导）
        ratio = math.log(width / height)
        return min(aspects.values(),
                   key=lambda wh: abs(math.log(wh[0] / wh[1]) - ratio))
    # 无任何尺寸线索：默认 1:1 档
    return aspects["1:1"]


def _publish_perf_notice(ctx: RunnerContext, pipe) -> None:
    """低性能档位提示：串行 offload 逐层换装极慢，向前端明确告警避免无声等待。"""
    if getattr(pipe, "_avs_perf_mode", "") in SLOW_PERF_MODES:
        ctx.publish_progress(
            0.0, "低显存串行换层模式：生成极慢（可能数十分钟），建议在模型管理中更换小显存模型",
        )


def _apply_vae_tiling(pipe, width: int, height: int) -> None:
    """按生成面积启停 VAE tiling：高分辨率启用防 OOM，常规分辨率关闭避伪影。"""
    vae = getattr(pipe, "vae", None)
    if vae is None:
        return
    if width * height >= _T2I_VAE_TILING_MIN_PIXELS and hasattr(vae, "enable_tiling"):
        try:
            vae.enable_tiling()
        except Exception:
            pass
    elif hasattr(vae, "disable_tiling"):
        try:
            vae.disable_tiling()
        except Exception:
            pass


class ScriptRunner(BaseRunner):
    """剧本节点：LLM 拆解 角色/场景/分镜（OpenAI 兼容 provider，JSON 输出）。"""

    name = "script"
    default_model = "qwen3-8b-q4"
    allowed_models = ["qwen3-8b-q4"]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        text = params.get("text") or input_refs.get("text") or ""
        if not text.strip():
            return RunnerResult(ok=False, error="剧本内容为空")
        # 显式 mock 模式（AVS_MOCK=1）保留占位拆解用于链路演示；
        # 真实模式下只走 LLM：未配置或调用失败直接任务失败，
        # 不再回退 mock 拆解（假数据会污染下游角色/场景/分镜，且难以察觉）
        if settings.mock_mode:
            return self._mock(text)
        if not llm_available():
            return RunnerResult(ok=False, error=(
                "LLM 未配置，无法拆解剧本：请设置 AVS_LLM_PROVIDER=openai、"
                "AVS_LLM_BASE_URL 与 AVS_LLM_API_KEY（可在模型管理-服务配置中填写）"
            ))
        return self._llm(ctx, model_id, text)

    def _mock(self, text: str) -> RunnerResult:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        shots = []
        for i, ln in enumerate(lines[:8] or [text[:60]]):
            shots.append({
                "shot_no": i + 1,
                "duration": 5,
                "description": ln[:80],
                "dialogue": "",
                "characters": [],
            })
        script = {
            "title": (lines[0] if lines else text)[:30],
            "synopsis": text[:200],
            "characters": [
                {"name": "主角", "persona": "根据剧本自动生成（mock）",
                 "appearance": "待定妆", "voice": ""},
            ],
            "scenes": [{"name": "场景一", "description": text[:60]}],
            "shots": shots,
        }
        return RunnerResult(ok=True, kind="json", meta={"script": script})

    def _llm(self, ctx: RunnerContext, model_id: str, text: str) -> RunnerResult:
        """第三方 LLM（OpenAI 兼容，providers 层）+ JSON 输出；失败任务失败并透出原因。"""
        import json

        prompt = (
            "你是短视频编剧。将输入剧本拆解为 JSON，字段："
            "title, synopsis, characters[{name,persona,appearance,voice}], "
            "scenes[{name,description}], shots[{shot_no,duration,description,dialogue,characters}]。"
            "每个镜头 4-8 秒。只输出 JSON。\n\n剧本：\n" + text[:20000]
        )
        try:
            content = llm_chat(
                [{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=4096,
            )
            script = json.loads(content)
            return RunnerResult(ok=True, kind="json", meta={"script": script})
        except Exception as e:  # noqa: BLE001 ProviderError 与 JSON 解析异常
            # 调用失败/输出非 JSON：任务失败并透出原因（不回退 mock 假拆解）
            return RunnerResult(ok=False, error=f"LLM 剧本拆解失败：{e}")


class TextToImageRunner(BaseRunner):
    name = "t2i"
    default_model = "hidream-o1/image"
    allowed_models = ["hidream-o1/image", "hidream-o1/dev", "qwen-image"]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        prompt = params.get("prompt") or input_refs.get("text") or ""
        if not prompt.strip():
            return RunnerResult(ok=False, error="提示词为空")
        # 首帧精炼（23 号批次 3）：**在 runner 内做**——每镜一次 LLM 调用，
        # 放在同步 API 端点里会让「批量生成首帧」阻塞到请求超时。上下文由
        # API 侧打包进 params（runner 不查库，见 RunnerContext 注释）；params
        # 里没有该键 = 本镜不精炼（开关关闭 / 无 video_prompt / LLM 不可用）。
        prompt, params = self._refine(ctx, prompt, params)
        prompt = _with_country(params, prompt)
        model_id = self.resolve_model(model_id)
        width, height = _resolve_t2i_size(model_id, params)
        count = max(1, min(4, int(params.get("count", 1))))

        # 第三方已配置时优先真实调用（无 GPU 环境同样可用，provider 不依赖本地推理）。
        # **必须与 `_provider_t2i` 的入口条件同口径**（含「显式选了本地模型就不走
        # 第三方」那一条）：两处条件不一致时，外层进了云端分支、内层又判定为本地，
        # `_real` 会掉进本地推理路径去加载与用户所选无关的权重（分叉条件必须同源）
        if (not settings.mock_mode and provider_enabled("t2i")
                and not _is_local_model(ctx.raw_model_id)):
            return self._real(ctx, model_id, prompt, width, height, count, params, input_refs)
        if ctx.mock or not inference_available():
            from app.runners.base import sleep_with_progress

            sleep_with_progress(ctx, 1.5, "生成图像（mock）")
            paths = [mock_image(self.artifact_path(ctx, f"{i}.svg"), prompt, width, height)
                     for i in range(count)]
            # mock 标记入产物 meta（2026-09-30 复查 R6）：占位图/彩条/提示音
            # 以前和真成品在库里长得一样，只有点开图才看到 [MOCK]；下游把占位
            # 首帧当参考送去付费生成时毫无察觉。打标后可被挑选逻辑与界面识别。
            return RunnerResult(ok=True, kind="image",
                                meta={"paths": [str(p) for p in paths],
                                      "count": count, "mock": True})

        return self._real(ctx, model_id, prompt, width, height, count, params, input_refs)

    def _refine(self, ctx: RunnerContext, prompt: str, params: dict
                ) -> tuple[str, dict]:
        """首帧精炼（**fail-open**）：返回 (成品提示词, 可能已更新的 params)。

        精炼失败/超时/护栏不过时，`refine_first_frame` 原样返回初稿——出图
        不受影响（精炼是增益不是门禁）。成功时同步刷新两处随任务记录的字段：
        `prompt_segments`（分段度量按精炼后的三段重算，键与组装侧一致）与
        `_prompt_notes`（**系统改过的地方必须外露**到「查看参数」）。
        """
        refine_ctx = params.get(REFINE_PARAM_KEY)
        if not refine_ctx:
            return prompt, params
        outcome: RefineOutcome = refine_first_frame(
            prompt, refine_ctx, trace=ctx.publish_trace)
        if not outcome.applied:
            return prompt, params
        new_params = dict(params)
        new_params["prompt_segments"] = {
            name: len(text) for name, text in outcome.sections.items()}
        notes = list(params.get("_prompt_notes") or [])
        new_params["_prompt_notes"] = notes + edit_notes(outcome)
        return outcome.text, new_params

    def _real(self, ctx: RunnerContext, model_id: str, prompt: str,
              width: int, height: int, count: int, params: dict,
              input_refs: dict) -> RunnerResult:
        """真实推理：第三方服务（openai/ark 兼容）已配置时走云端，否则走本地。

        本地路径经资源池获取管线（同模型驻留零加载，性能调优已应用）；
        HiDream-O1 家族走 transformers UiT 专用路径（非 diffusers）。
        count > 1 时逐张生成（同 batch 种子递增），避免 batch 峰值显存。

        参考图（锁脸 / 角色一致性）按模型家族三条路走：方舟 Seedream 与 Agnes
        在 provider 层上送；本地 HiDream-O1 走**原生图文条件**（与提示词联合
        编码，见 `_real_hidream`）；其余本地 diffusers 家族走 IP-Adapter FaceID
        旁路（`app.inference.faceid`）。都不可用时**明确外露"参考图被丢弃"**，
        不留给用户"以为已锁脸"的错觉——静默丢弃正是审计抓到的缺陷形态。
        """
        # 种子按端点能力解析（实测：agnes 生效可复现 / ark 静默忽略）
        seed_send, seed_note = _resolve_provider_seed(ctx, "t2i", params)
        if seed_note:
            ctx.publish_progress(0.0, seed_note)
        # 参考图按**端点实际上限**裁剪并外露截断：旧实现把全量清单记进
        # generation（refs_used=8 / refs_dropped=0），而 provider 层随后把它
        # 悄悄截到 4（方舟）——「查看参数」弹窗据此渲染 8 张缩略图，与实际
        # 上送不符（可核对口径失真），且截断本身用户无从知晓。
        refs_in = _t2i_ref_paths(input_refs)
        ref_budget = t2i_ref_budget(model_id)
        if ref_budget and len(refs_in) > ref_budget:
            ctx.publish_progress(
                0.0, f"参考图 {len(refs_in)} 张超出本端点上限 {ref_budget} 张，"
                     f"已截断 {len(refs_in) - ref_budget} 张（尾部场景/道具优先被裁，"
                     "角色已优先占号）")
            logger.warning("参考图超端点上限截断 model=%s %d→%d task=%s",
                           model_id, len(refs_in), ref_budget, ctx.task_id)
            refs_in = refs_in[:ref_budget]
            # 同步裁剪传给 provider 的引用（记录与实际上送必须是同一份清单）
            input_refs = dict(input_refs or {})
            if refs_in:
                input_refs["extra_images"] = list(refs_in)
            else:
                input_refs.pop("extra_images", None)
                input_refs.pop("image", None)
        # 生成参数全链路记录（provider 分发前：裁剪后的实际上送清单）
        _record_generation(ctx, self.name, model_id, prompt, params,
                           input_refs, refs_in, seed_note)
        via = self._provider_t2i(ctx, prompt, width, height, count, model_id,
                                 input_refs, seed_send)
        if via is not None:
            return via
        # 参考图（角色一致性）落地路径分流：HiDream-O1 走原生图文条件（见
        # _real_hidream），其余 diffusers 家族走 IP-Adapter FaceID 旁路；两者
        # 都不可用时明确外露"参考图被丢弃"，不留给用户"以为已锁脸"的错觉。
        ref_paths = _t2i_ref_paths(input_refs)
        if ref_paths and not model_id.startswith("hidream-o1"):
            note = faceid_note(model_id, True)
            if note:
                ctx.publish_progress(0.0, note)
        if model_id.startswith("hidream-o1"):
            return self._real_hidream(ctx, model_id, prompt, width, height, count,
                                      params, ref_paths)
        cls = pipeline_class(model_id, self.name)
        pipe = pool.acquire(model_id, default_loader(model_id, self.name, ctx.models_dir),
                            expect=cls)
        try:
            _publish_perf_notice(ctx, pipe)
            _apply_vae_tiling(pipe, width, height)
            base_seed = _resolve_seed(params)
            is_flux = model_id.startswith("flux")
            # 锁脸旁路（可选）：可用时把角色参考图并入 IP-Adapter 条件；
            # 参考图只读一次，count 张共用同一份条件（不重复读盘/编码）。
            faceid_kwargs = apply_faceid(pipe, model_id, ref_paths,
                                         params.get("faceid_scale"))
            if faceid_kwargs:
                ctx.publish_progress(
                    0.0, f"已启用锁脸：{len(ref_paths)} 张参考图经 IP-Adapter "
                         f"FaceID 注入（强度 {params.get('faceid_scale') or '默认'}）")
            paths: list[str] = []
            for i in range(count):
                BaseRunner.check_cancel(ctx)
                kwargs: dict = {
                    "prompt": prompt,
                    "width": width,
                    "height": height,
                    "num_inference_steps": 28,
                    # 每张独立种子：params.seed 可指定，缺省随机且可复现
                    "generator": _generator(pipe, base_seed + i),
                    **faceid_kwargs,
                }
                if is_flux:
                    kwargs["guidance_scale"] = 3.5
                    kwargs["max_sequence_length"] = 256
                # 步级进度 + 取消检查（与视频管线一致；progress 区间按第 i 张分段）
                self.attach_step_progress(ctx, pipe, kwargs, base=i / count, frac=1 / count)
                image = pipe(**kwargs).images[0]
                p = self.artifact_path(ctx, f"{i}.png")
                image.save(str(p))  # PNG 无损保存
                paths.append(str(p))
                ctx.publish_progress((i + 1) / count, f"已生成 {i + 1}/{count} 张")
            return RunnerResult(ok=True, kind="image",
                                meta={"paths": paths, "count": len(paths),
                                      "seed": base_seed})
        finally:
            # 任务结束不卸载（驻留提速连发），仅解除占用
            pool.release(model_id)

    def _real_hidream(self, ctx: RunnerContext, model_id: str, prompt: str,
                      width: int, height: int, count: int, params: dict,
                      ref_paths: list[str] | None = None) -> RunnerResult:
        """HiDream-O1 本地推理：transformers UiT 主模型 + 官方采样管线（非 diffusers）。

        full 变体：50 步 / guidance 5.0 / shift 3.0；dev（蒸馏）变体：28 步 /
        无 CFG / flash 调度器。分辨率已在 run() 阶段映射至官方训练档。

        锁脸（路线 1）：HiDream-O1 把参考图与提示词**联合编码**为条件，是本地
        最强的一致性通路——`ref_image_paths` 交给官方管线即可，无需 IP-Adapter。
        条件构建与种子无关，经 conditioning 缓存复用（count 张 / 重摇直接命中）。
        """
        from app.inference.hidream_o1.pipeline import (
            DEFAULT_TIMESTEPS, generate_image,
        )

        pipe = pool.acquire(model_id, default_loader(model_id, self.name, ctx.models_dir),
                            expect=None)
        try:
            _publish_perf_notice(ctx, pipe)
            base_seed = _resolve_seed(params)
            # 变体参数对齐官方 inference.py 默认值
            if model_id.endswith("/dev"):
                steps = _HIDREAM_DEV_STEPS
                guidance = _HIDREAM_DEV_GUIDANCE
                shift = _HIDREAM_DEV_SHIFT
                timesteps_list: list[int] | None = DEFAULT_TIMESTEPS
                scheduler_name = _HIDREAM_DEV_SCHEDULER
                noise_kwargs = {
                    "noise_scale_start": _HIDREAM_NOISE_SCALE,
                    "noise_scale_end": _HIDREAM_NOISE_SCALE,
                    "noise_clip_std": _HIDREAM_NOISE_CLIP_STD,
                }
            else:
                steps = _HIDREAM_FULL_STEPS
                guidance = float(params.get("guidance", _HIDREAM_FULL_GUIDANCE))
                shift = _HIDREAM_FULL_SHIFT
                timesteps_list = None
                scheduler_name = _HIDREAM_FULL_SCHEDULER
                noise_kwargs = {}

            # 原生参考图条件 + 条件缓存键（键含模型前缀与逐字输入，重摇同键）
            use_refs = list(ref_paths or [])[:_HIDREAM_MAX_REFS]
            if ref_paths and len(ref_paths) > _HIDREAM_MAX_REFS:
                ctx.publish_progress(
                    0.0, f"本地参考图上限 {_HIDREAM_MAX_REFS} 张（单图参考分辨率随"
                         f"张数分档下降），已截断：共 {len(ref_paths)} 张")
            # 参考图 token 预算：官方档会把它放大到与目标图同量级致 OOM（真机实测），
            # 这里按预算给边长上限；上限改变会改变条件张量，故必须并入缓存键。
            ref_cap = _hidream_ref_max_size(len(use_refs)) if use_refs else 0
            if use_refs:
                ctx.publish_progress(
                    0.0, f"已启用本地锁脸：{len(use_refs)} 张参考图与提示词联合编码"
                         f"（边长上限 {ref_cap}px，按显存预算）")
            negative = str(params.get("negative_prompt") or "")
            cond_key = conditioning_key(model_id, prompt, use_refs, width, height,
                                        guidance > 1.0, negative,
                                        extra=f"refmax={ref_cap}")

            paths: list[str] = []
            for i in range(count):
                BaseRunner.check_cancel(ctx)
                # 每步进度回调（官方管线吞掉回调内异常，取消逻辑不放在这里）
                step_base = i / count

                def _on_step(step: int, total: int, _preview,
                             _base: float = step_base) -> None:
                    ctx.publish_progress(
                        _base + (step + 1) / total / count,
                        f"推理 {step + 1}/{total} 步（{i + 1}/{count}）",
                    )

                image = generate_image(
                    model=pipe.model, processor=pipe.processor, prompt=prompt,
                    ref_image_paths=use_refs,
                    ref_max_size=ref_cap or None,
                    height=height, width=width,
                    num_inference_steps=steps, guidance_scale=guidance,
                    shift=shift, timesteps_list=timesteps_list,
                    scheduler_name=scheduler_name, seed=base_seed + i,
                    negative_prompt=negative,
                    callback=_on_step,
                    # 条件缓存：同任务多张 / 重摇时复用条件张量（种子不参与键）
                    cond_cache=conditioning.cache, cond_key=cond_key,
                    **noise_kwargs,
                )
                p = self.artifact_path(ctx, f"{i}.png")
                image.save(str(p))  # PNG 无损保存
                paths.append(str(p))
            return RunnerResult(ok=True, kind="image",
                                meta={"paths": paths, "count": len(paths),
                                      "seed": base_seed})
        finally:
            # 任务结束不卸载（驻留提速连发），仅解除占用
            pool.release(model_id)

    def _provider_t2i(self, ctx: RunnerContext, prompt: str, width: int,
                      height: int, count: int, model_id: str = "",
                      input_refs: dict | None = None,
                      seed: int | None = None) -> RunnerResult | None:
        """第三方文生图（openai / ark / agnes）：已配置且任务未选本地模型时优先。

        任务级解析：编码串（端点::模型）直接指定端点，否则全局默认配置。
        input_refs 携带参考图（角色一致性生成）时方舟与 Agnes 走参考图生图
        （本地图转 base64 data URL 上送）；OpenAI 兼容服务无参考图契约，
        降级纯文生图并明确提示。
        **云端与本地是两条互不相干的链路**（2026-09-23 用户决策）：provider 已
        配置时失败即失败并透出真实原因，不回退本地推理（回退会静默加载一小时
        的本地权重并占死 GPU 泳道）。未配置 provider 时才走本地路径。
        """
        if settings.mock_mode or not (provider_enabled("t2i")
                                      or is_endpoint_model(ctx.raw_model_id)):
            return None
        # 本地模型判定用任务原始选择（ctx.raw_model_id）：run 收到的 model_id
        # 已被默认值填充，用它判断会把「未指定模型」误拦成本地路径
        if _is_local_model(ctx.raw_model_id):
            return None  # 显式选择本地模型：不走第三方
        ref_paths = _t2i_ref_paths(input_refs)
        try:
            # 编码串任务直接指定端点（cfg 覆盖全局默认）；raw 为空 → 用
            # provider 配置的默认模型
            cfg, provider_model = resolve_runtime_config("t2i", ctx.raw_model_id)
            if cfg.name == "ark":
                images = t2i_ark(prompt, width, height, count, provider_model,
                                 ref_paths=ref_paths, cfg=cfg)
            elif cfg.name == "agnes":
                images = t2i_agnes(prompt, width, height, count, provider_model,
                                   ref_paths=ref_paths, seed=seed, cfg=cfg)
            else:
                if ref_paths:
                    ctx.publish_progress(
                        0.0, "当前文生图服务不支持参考图，已按纯文生图生成")
                images = t2i_openai(prompt, width, height, count,
                                    model_id=provider_model, cfg=cfg)
        except ProviderError as e:
            if ctx.cancel_event.is_set():
                raise RunnerError("任务已取消") from e
            # 云端与本地是**两条互不相干的链路**：provider 已配置时失败即失败，
            # 绝不静默回退本地推理。实测事故（2026-09-23）：42 镜首帧批量提交
            # 并发触发 agnes 失败 → 静默回退 → 后端开始加载 HiDream-O1 的 759
            # 个权重分片（首次约 1 小时）→ 占死 GPU 泳道，整条出图队列停摆；
            # 而回退只发了一条前端提示、不打日志，事后从日志里查不到真因。
            logger.warning("第三方文生图失败（不回退本地）task=%s raw_model=%r：%s",
                           ctx.task_id, ctx.raw_model_id, e)
            # 错误原因透出：用户可据此重试或换端点（占位图会冒充成品欺骗
            # 用户，浪费排队与后续依赖它的生成）。fail_class 透传：基础设施/
            # 内容安全类失败不占用户重摇预算
            raise RunnerError(f"第三方文生图失败：{e}",
                              getattr(e, "fail_class", "")) from e
        paths: list[str] = []
        for i, data in enumerate(images[:count]):
            p = self.artifact_path(ctx, f"{i}.png")
            p.write_bytes(data)
            paths.append(str(p))
            ctx.publish_progress((i + 1) / len(images),
                                 f"第三方已生成 {i + 1}/{len(images)} 张")
        return RunnerResult(ok=True, kind="image",
                            meta={"paths": paths, "count": len(paths),
                                  "via": "provider"})


class VideoRunner(BaseRunner):
    """视频生成基类：t2v / i2v / fl2v 共用。"""

    name = "t2v"
    default_model = "minimax-h3/fl2va-int8"
    allowed_models = [
        "minimax-h3/fl2va-int8", "ltx-2/distilled",
        "wan-2.2/a14b-gguf-q5", "wan-2.2/ti2v-5b",
    ]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        prompt = params.get("prompt") or input_refs.get("text") or ""
        if not prompt.strip():
            return RunnerResult(ok=False, error="提示词为空")
        prompt = _with_country(params, prompt)
        model_id = self.resolve_model(model_id)
        duration = max(4, min(15, int(params.get("duration", 5))))
        aspect = params.get("aspect", "16:9")
        # 尺寸优先取提交侧算好的宽高（含项目分辨率档），缺省按画幅比例查表——
        # 分辨率档是项目级设置（480p/720p/1080p），由 drama API 转换成宽高下发
        if params.get("width") and params.get("height"):
            width, height = int(params["width"]), int(params["height"])
        elif aspect == "adaptive":
            width, height = ADAPTIVE_SIZE
        else:
            width, height = ASPECTS.get(aspect, ASPECTS["16:9"])

        # 第三方已配置时优先真实调用（无 GPU 环境同样可用，provider 不依赖本地推理）
        if not settings.mock_mode and provider_enabled("t2v"):
            return self._real(ctx, model_id, prompt, duration, width, height, input_refs, params)
        if ctx.mock or not inference_available():
            from app.runners.base import sleep_with_progress

            sleep_with_progress(ctx, 3.0, f"生成视频（mock，{model_id}）")
            p = mock_video(self.artifact_path(ctx, "mp4"), prompt, duration, width, height)
            return RunnerResult(ok=True, kind="video",
                                meta={"path": str(p), "has_audio": False,
                                      "mock": True})

        return self._real(ctx, model_id, prompt, duration, width, height, input_refs, params)

    def _real(self, ctx: RunnerContext, model_id: str, prompt: str, duration: int,
              width: int, height: int, input_refs: dict, params: dict) -> RunnerResult:
        """按模型家族分发到 diffusers 管线，全部经资源池（同模型驻留零加载）。

        第三方服务（ark / custom 端点）已配置且任务未选本地模型时优先走
        provider，未配置时才走本地。性能调优（group offload / 全 GPU /
        分层 offload / 串行 offload、VAE 分块）由池在加载时统一应用；
        minimax-h3 等未接入家族在 pipeline_class 探测阶段即抛出明确错误。
        """
        # 生成参数全链路记录（主图/首帧在前 + extra_images，与上送顺序一致）
        _ff = str((input_refs or {}).get("image")
                  or (input_refs or {}).get("first") or "").strip()
        _extra = [str(e) for e in (input_refs or {}).get("extra_images") or []]
        seed_send, seed_note = _resolve_provider_seed(ctx, "t2v", params)
        if seed_note:
            ctx.publish_progress(0.0, seed_note)
        _record_generation(ctx, self.name, model_id, prompt, params,
                           input_refs, ([_ff] if _ff else []) + _extra, seed_note)
        via = self._provider_t2v(ctx, prompt, duration, width, height,
                                 input_refs, model_id, seed_send)
        if via is not None:
            return via
        cls = pipeline_class(model_id, self.name)
        pipe = pool.acquire(model_id, default_loader(model_id, self.name, ctx.models_dir),
                            expect=cls)
        try:
            _publish_perf_notice(ctx, pipe)
            if model_id.startswith("wan-2.2"):
                return self._run_wan(ctx, pipe, prompt, duration, width, height, input_refs)
            if model_id.startswith("ltx-2"):
                return self._run_ltx(ctx, pipe, prompt, duration, width, height, input_refs)
            # 其余家族已在 pipeline_class 探测阶段拦截，此处不可达
            raise RunnerError(f"{model_id} 暂无本地推理路径")
        finally:
            # 任务结束不卸载（驻留提速连发），仅解除占用
            pool.release(model_id)

    def _provider_t2v(self, ctx: RunnerContext, prompt: str, duration: int,
                      width: int, height: int, input_refs: dict,
                      model_id: str = "",
                      seed: int | None = None) -> RunnerResult | None:
        """第三方文生视频（ark / agnes / custom）：已配置且任务未选本地模型时优先。

        任务级解析：编码串（端点::模型）直接指定端点，否则全局默认配置。
        - ark：本地首帧/参考图转 base64 data URL 上送（first_frame /
          reference_image），因此带图像输入的 ref2v 同样可走方舟。
        - agnes：异步任务（POST /videos + agnesapi 轮询）；仅首帧走
          keyframe 模式保真首帧，带参考图走 reference 模式（首帧并入
          参考图首位），无图像输入走 text 模式。
        - custom：本地图无法传给远端端点，有图像输入时跳过（保持原契约）。
        取消时中止；云端失败即失败不回退本地（云端与本地互不相干）。
        """
        if settings.mock_mode or not (provider_enabled("t2v")
                                      or is_endpoint_model(ctx.raw_model_id)):
            return None
        # 本地模型判定用任务原始选择（ctx.raw_model_id），理由同 _provider_t2i
        if _is_local_model(ctx.raw_model_id):
            return None  # 显式选择本地模型：不走第三方
        cfg, provider_model = resolve_runtime_config("t2v", ctx.raw_model_id)
        first_frame = (input_refs.get("image")
                       or input_refs.get("first")) or None
        extra_refs = list(input_refs.get("extra_images") or [])
        # 参考图按**端点能力**裁剪并外露（与 t2i 同一纪律）：分镜链路在提交前
        # 已校验并 400，但画布入口 POST /api/tasks 不校验，provider 层此前是
        # 静默截断（agnes 超预算连日志都没有）——不声不响地少送图 = 用户按
        # 「已锁脸」预期行事而实际没有。
        # 带上实际模型：同一端点下代际能力可能有差异（模型家族覆盖表）
        _cap = video_capability(cfg.name, provider_model)
        _budget = _cap.max_ref_images - (
            1 if (first_frame and _cap.first_frame_occupies_slot) else 0)
        if len(extra_refs) > max(0, _budget):
            ctx.publish_progress(
                0.0, f"参考图 {len(extra_refs)} 张超出本端点上限 {max(0, _budget)} 张，"
                     "已截断（角色已优先占号，尾部场景/道具先被裁）")
            logger.warning("视频参考图超端点上限截断 provider=%s %d→%d task=%s",
                           cfg.name, len(extra_refs), max(0, _budget), ctx.task_id)
            extra_refs = extra_refs[:max(0, _budget)]
        # 提交段（审计 13 号 L）：提交成功即远端计费，此后任何失败都不能回退本地
        # 重算（旧实现把 15 分钟轮询超时/下载失败一并算作「第三方失败」→ 本地
        # 重跑整条视频＝双份成本，且远端产物无 id 可回捞）
        provider_task_id = ""
        try:
            if cfg.name == "ark":
                provider_task_id = t2v_ark_submit(
                    prompt, duration, width, height, provider_model,
                    first_frame_path=first_frame, ref_paths=extra_refs, cfg=cfg)
            elif cfg.name == "agnes":
                provider_task_id, provider_model = t2v_agnes_submit(
                    prompt, duration, width, height, provider_model,
                    first_frame_path=first_frame, ref_paths=extra_refs,
                    seed=seed, cfg=cfg)
            elif cfg.name == "custom":
                if first_frame or extra_refs:
                    return None  # custom 端点无图像上送契约
            elif cfg.name == "comfyui":
                # 本地 ComfyUI：宽高按 H3 硬约束吸附（32 的倍数 + 1.0MP 封顶，
                # 2026-10-10 用户拍板跟随项目档），吸附/缩回说明推给用户——
                # 「参数被系统改过」必须看得见（与参考图截断同一纪律）
                _cw, _ch, _size_note = comfyui_client.plan_size(width, height)
                if _size_note:
                    ctx.publish_progress(0.0, _size_note)
                    logger.info("ComfyUI 分辨率调整 %s×%s → %s×%s task=%s",
                                width, height, _cw, _ch, ctx.task_id)
                provider_task_id = t2v_comfyui_submit(
                    prompt, duration, _cw, _ch, provider_model,
                    first_frame_path=first_frame, ref_paths=extra_refs,
                    seed=seed, run_tag=ctx.task_id, cfg=cfg)
            elif cfg.name == "vidu":
                # Vidu 图生视频：首帧必填、只收 1 张图（额外参考图由能力表
                # 归零并在上游截断）；种子不下发（未实测，见能力表）；
                # 没有首帧时由 provider 层明确报错，不回退、不静默换端点
                provider_task_id = t2v_vidu_submit(
                    prompt, duration, width, height, provider_model,
                    first_frame_path=first_frame, cfg=cfg)
            if not provider_task_id:
                data = t2v_custom(
                    prompt, duration, width, height,
                    cancel_check=lambda: ctx.cancel_event.is_set(), cfg=cfg)
                video_url = str(data["video_url"])
                content = download_video(video_url)
            else:
                # 取回段：失败只报错（含 provider 任务号供回捞），不触发本地重算
                ctx.publish_progress(0.0, "第三方视频已提交，等待生成完成…")
                if cfg.name == "ark":
                    data = t2v_ark_fetch(
                        cfg, provider_task_id,
                        cancel_check=lambda: ctx.cancel_event.is_set(),
                        on_tick=ctx.publish_progress, duration=duration)
                elif cfg.name == "vidu":
                    # Vidu 图生视频轮询：on_tick 同其它云端端点（估算进度 +
                    # 看门狗保命）；失败带任务号回捞，不自动重提（避免重复计费）
                    data = t2v_vidu_fetch(
                        cfg, provider_task_id,
                        cancel_check=lambda: ctx.cancel_event.is_set(),
                        on_tick=ctx.publish_progress, duration=duration)
                elif cfg.name == "comfyui":
                    # on_tick 传进度回调：本地出片十几分钟，进度心跳既给用户看
                    # 也保任务不被看门狗按「20 分钟无进度」判卡死
                    data = t2v_comfyui_fetch(
                        cfg, provider_task_id, duration=duration,
                        cancel_check=lambda: ctx.cancel_event.is_set(),
                        on_tick=ctx.publish_progress)
                else:
                    data = t2v_agnes_fetch(
                        cfg, provider_task_id, provider_model,
                        cancel_check=lambda: ctx.cancel_event.is_set(),
                        on_tick=ctx.publish_progress, duration=duration)
                video_url = str(data["video_url"])
                content = download_video(video_url)
        except ProviderError as e:
            if ctx.cancel_event.is_set():
                raise RunnerError("任务已取消") from e
            if provider_task_id:
                # 已提交：远端已计费，禁止回退本地重算（双份成本）；错误带任务号。
                # 类别**必须**是 BILLED（不可自动重试）：取回段的网络抖动原本会
                # 被判成 network，而 network 在自动重试名单里 ⇒ 任务重新入队、
                # runner 从头再跑、**又提交一个全新的远端任务 = 重复扣费**
                # （25 号复查 R1：文案写着「避免重复计费」，行为却是再买一次）。
                if cfg.name == "comfyui":
                    # 本地出片没有远端计费，但已提交同样不得重提：重提白烧一次
                    # 十几分钟的 GPU，且产物可能已在 ComfyUI 输出目录产生
                    raise RunnerError(
                        f"本地出片已提交（ComfyUI 任务号 {provider_task_id}）"
                        f"但取回失败：{e}。产物可能仍在生成，可稍后在 ComfyUI "
                        "界面或输出目录查看；不会自动重提（避免重复出片）",
                        FailClass.BILLED) from e
                raise RunnerError(
                    f"第三方视频已提交（远端任务号 {provider_task_id}）但取回失败："
                    f"{e}。产物可能仍在生成，请稍后重试或联系平台核对；"
                    "不会自动重提或改用本地模型（避免重复计费）",
                    FailClass.BILLED) from e
            # 云端与本地互不相干：provider 已配置时失败即失败，不回退本地
            # （理由同 _provider_t2i；视频侧回退还会把 GPU 泳道一并占死）
            logger.warning("第三方文生视频失败（不回退本地）task=%s raw_model=%r：%s",
                           ctx.task_id, ctx.raw_model_id, e)
            # 彩条占位视频冒充成品是事故（用户无法分辨、后续抽卡建立在假
            # 产物上），错误透出可重试
            raise RunnerError(f"第三方文生视频失败：{e}",
                              getattr(e, "fail_class", "")) from e
        p = self.artifact_path(ctx, "mp4")
        p.write_bytes(content)
        ctx.publish_progress(1.0, "第三方视频已下载")
        # 音轨：按端点如实记录（判定在 providers.provider_outputs_audio）。
        # **2026-10-14 修正**：旧注释写「其余云端端点返回的都是无音轨文件」并
        # 只给 comfyui 记 True——抽检库内产物证明 agnes（90+ 个）与方舟
        # seedance 2.0（28 个）的成片**都带真实音轨**，是被标记错的那一边。
        return RunnerResult(ok=True, kind="video",
                            meta={"path": str(p),
                                  "has_audio": provider_outputs_audio(cfg.name),
                                  "via": "provider"})

    def _run_wan(self, ctx: RunnerContext, pipe, prompt: str, duration: int,
                 width: int, height: int, input_refs: dict) -> RunnerResult:
        """Wan 2.2：帧数 = 时长 * 16（16fps），guidance 5.0，i2v 首帧走 image 入参。"""
        from diffusers.utils import export_to_video

        image_path = input_refs.get("image") or input_refs.get("first")
        last_path = input_refs.get("last")
        extra_images = input_refs.get("extra_images") or []
        if last_path:
            raise RunnerError(
                "WanPipeline 不支持首尾帧双约束（fl2v）：请仅传首帧，或改用 H3 provider 路径"
            )
        if extra_images:
            raise RunnerError(
                "WanPipeline 不支持多参考图（ref2v）：请改用 H3，或仅传单张首帧"
            )
        kwargs: dict = {
            "prompt": [prompt],
            "negative_prompt": [""],
            "height": height,
            "width": width,
            "num_frames": int(duration * 16),  # Wan 16fps
            "guidance_scale": 5.0,
        }
        if image_path:
            from PIL import Image

            kwargs["image"] = Image.open(image_path).convert("RGB")  # i2v 首帧
        self.attach_step_progress(ctx, pipe, kwargs)
        out = pipe(**kwargs).frames[0]
        p = self.artifact_path(ctx, "mp4")
        export_to_video(out, str(p), fps=16)
        return RunnerResult(ok=True, kind="video",
                            meta={"path": str(p), "has_audio": False})

    def _run_ltx(self, ctx: RunnerContext, pipe, prompt: str, duration: int,
                 width: int, height: int, input_refs: dict) -> RunnerResult:
        """LTX-2：蒸馏版 8 步出片（24fps），i2v 首帧走 image 入参。"""
        from diffusers.utils import export_to_video

        image_path = input_refs.get("image") or input_refs.get("first")
        if input_refs.get("last"):
            raise RunnerError("LTX 管线不支持首尾帧双约束（fl2v），请改用 H3 provider 路径")
        kwargs: dict = {
            "prompt": prompt,
            "negative_prompt": "",
            "height": height,
            "width": width,
            "num_frames": int(duration * 24),  # LTX 24fps
            "num_inference_steps": 8,  # 蒸馏版 8 步
        }
        if image_path:
            from PIL import Image

            kwargs["image"] = Image.open(image_path).convert("RGB")
        self.attach_step_progress(ctx, pipe, kwargs)
        out = pipe(**kwargs).frames[0]
        p = self.artifact_path(ctx, "mp4")
        export_to_video(out, str(p), fps=24)
        return RunnerResult(ok=True, kind="video",
                            meta={"path": str(p), "has_audio": False})

class ImageToImageRunner(TextToImageRunner):
    """图生图：输入图 + 提示词改写。mock 复用 t2i 占位路径；real 走 FluxImg2Img。"""

    name = "i2i"

    def _real(self, ctx: RunnerContext, model_id: str, prompt: str,
              width: int, height: int, count: int, params: dict,
              input_refs: dict) -> RunnerResult:
        """图生图真实推理：FluxImg2ImgPipeline，strength 控制改写强度（默认 0.8）。

        当前仅 Flux 家族支持（qwen-image 图生图未接入、HiDream-O1 参考图
        编辑待接入），其余家族在管线探测/守卫阶段明确报错。
        """
        from PIL import Image

        if model_id.startswith("hidream-o1"):
            raise RunnerError("HiDream-O1 图生图暂未接入（参考图编辑待接入），请使用文生图")
        image_path = input_refs.get("image") or ""
        if not image_path:
            raise RunnerError("图生图缺少输入图（input_refs.image）")
        cls = pipeline_class(model_id, self.name)
        pipe = pool.acquire(model_id, default_loader(model_id, self.name, ctx.models_dir),
                            expect=cls)
        try:
            image = Image.open(image_path).convert("RGB")
            _publish_perf_notice(ctx, pipe)
            _apply_vae_tiling(pipe, *image.size)
            base_seed = _resolve_seed(params)
            kwargs: dict = {
                "prompt": prompt,
                "image": image,
                "strength": float(params.get("strength", 0.8)),
                "num_inference_steps": 28,
                "guidance_scale": 3.5,
                "generator": _generator(pipe, base_seed),
            }
            if model_id.startswith("flux"):
                kwargs["max_sequence_length"] = 256
            self.attach_step_progress(ctx, pipe, kwargs)  # 步级进度 + 取消检查
            out = pipe(**kwargs).images[0]
            p = self.artifact_path(ctx, "png")
            out.save(str(p))
            return RunnerResult(ok=True, kind="image",
                                meta={"paths": [str(p)], "count": 1, "seed": base_seed})
        finally:
            # 任务结束不卸载（驻留提速连发），仅解除占用
            pool.release(model_id)


class ImageToVideoRunner(VideoRunner):
    name = "i2v"


class FirstLastVideoRunner(VideoRunner):
    name = "fl2v"


class Ref2VideoRunner(VideoRunner):
    """H3 Ref2VA：图 + 多参考混合生成（参考生视频）。

    mock 模式与 t2v 共用占位骨架；real 路径经池探测 H3 管线，
    diffusers 集成未就绪时明确报错引导 provider 第三方路径。
    """

    name = "ref2v"


class TtsRunner(BaseRunner):
    name = "tts"
    default_model = "edge-tts"
    allowed_models = ["edge-tts"]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        text = params.get("text") or input_refs.get("text") or ""
        if not text.strip():
            return RunnerResult(ok=False, error="台词为空")
        voice = params.get("voice", "zh-CN-XiaoxiaoNeural")

        # 第三方 TTS（OpenAI 兼容）已配置时优先，失败回退 edge-tts
        if not settings.mock_mode and provider_enabled("tts"):
            via = self._provider_tts(ctx, text, voice)
            if via is not None:
                return via

        if ctx.mock:
            from app.runners.base import sleep_with_progress

            sleep_with_progress(ctx, 1.0, "合成语音（mock）")
            p = mock_audio(self.artifact_path(ctx, "wav"), text)
            return RunnerResult(ok=True, kind="audio",
                                meta={"path": str(p), "mock": True})

        try:
            import asyncio as _a

            import edge_tts

            async def _gen() -> None:
                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(str(self.artifact_path(ctx, "mp3")))

            _a.run(_gen())
            return RunnerResult(ok=True, kind="audio",
                                meta={"path": str(self.artifact_path(ctx, "mp3"))})
        except ImportError:
            from app.runners.base import sleep_with_progress

            sleep_with_progress(ctx, 1.0, "edge-tts 未安装，回退 mock")
            p = mock_audio(self.artifact_path(ctx, "wav"), text)
            # 缺依赖时的静默回退也打标（2026-09-30 复查 R6）：这条与 mock 开关
            # 无关，实例上就是这么静悄悄把提示音当配音产物交出去的。
            return RunnerResult(ok=True, kind="audio",
                                meta={"path": str(p), "mock": True,
                                      "mock_reason": "edge-tts 未安装"})
        except Exception as e:  # noqa: BLE001
            return RunnerResult(ok=False, error=f"TTS 失败：{e}")

    def _provider_tts(self, ctx: RunnerContext, text: str,
                      voice: str) -> RunnerResult | None:
        """第三方 TTS（OpenAI 兼容 audio/speech）：失败返回 None 走 edge-tts。

        voice 直接透传（OpenAI 音色名，如 alloy/echo）。
        """
        try:
            content = tts_openai(text, voice)
        except ProviderError as e:
            ctx.publish_progress(0.0, f"第三方 TTS 失败，回退 edge-tts：{e}")
            return None
        p = self.artifact_path(ctx, "mp3")
        p.write_bytes(content)
        return RunnerResult(ok=True, kind="audio",
                            meta={"path": str(p), "via": "provider"})


# 注册表
RUNNERS: dict[str, BaseRunner] = {
    r.name: r
    for r in (
        ScriptRunner(), TextToImageRunner(), ImageToImageRunner(),
        VideoRunner(), ImageToVideoRunner(), FirstLastVideoRunner(),
        Ref2VideoRunner(), TtsRunner(),
    )
}


def get_runner(name: str) -> BaseRunner:
    runner = RUNNERS.get(name)
    if not runner:
        raise RunnerError(f"未知 runner：{name}")
    return runner
