"""第三方模型服务配置与调用层。

本地优先（对齐 AGENTS.md）：provider 默认 local（本地推理）；
第三方服务按任务类别（llm/t2i/t2v/i2v/tts）独立配置，
未配置时由调用方走本地推理或 mock；**已配置但调用失败时直接失败，不回退本地**
（云端与本地互不相干，2026-09-23 用户决策）。

provider 取值：
- local   本地推理（默认）
- openai  OpenAI 兼容 API（llama.cpp/vLLM/硅基流动/DeepSeek 官方等，
          覆盖 /v1/chat/completions 与 /v1/images/generations 兼容端点）
- ark     火山方舟（Volcengine Ark）：Seedream 文生图（images/generations）
          与 Seedance 视频生成（contents/generations/tasks 创建+轮询，
          支持本地图片转 base64 data URL 作为首帧/参考图）
- agnes   Agnes AI（https://apihub.agnes-ai.com/v1）：OpenAI 兼容网关，
          文本 chat/completions、图像 images/generations（size 档位+ratio，
          response_format 须放 extra_body，参考图走 extra_body.image），
          视频 POST /videos 异步任务 + GET /agnesapi 轮询（agnesapi 在主机根，
          不带 /v1）
- custom  自定义视频端点（POST /videos 提交 + GET /videos/{task_id} 轮询）
- comfyui 本机 ComfyUI 服务（POST /prompt 提交整份工作流 JSON + GET /history
          轮询 + GET /view 下载；本地模型服务，默认无鉴权、无费率即免费）
- vidu    Vidu 图生视频（POST /ent/v2/img2video 提交 + GET /ent/v2/tasks/{id}/
          creations 轮询；鉴权头为 `Token {key}` 而非 Bearer；首帧图必填——
          本协议只接「以首帧画面起像」的生成，纯文生视频由其它端点承担）

环境变量（每类任务独立，KIND 为 LLM/T2I/T2V/I2V/TTS）：
AVS_{KIND}_PROVIDER / AVS_{KIND}_BASE_URL / AVS_{KIND}_API_KEY / AVS_{KIND}_MODEL
Ark base_url 填 https://ark.cn-beijing.volces.com/api/v3；
LLM 走 Ark 时 provider 配 openai 并填同一 base_url（chat/completions 兼容）。
另 AVS_LLM_TIMEOUT（LLM 请求超时，秒，默认 120）。
持久化写入 server/.env（update_provider_env），由 config.Settings 启动时 load_dotenv。

配置来源优先级（_load_config 每次现读，保存即生效）：
1. DB 默认端点（provider_keys / provider_endpoints / settings.default_endpoints，
   系统配置-模型管理录入，存储层见 provider_store）
2. server/.env 五槽位（兼容回退层，旧 PUT /api/providers 仍可写）

任务级模型串（前端下拉选项）："{endpoint_id}::{model_id}" 编码串直接指定端点，
经 resolve_runtime_config 解析后把 cfg 显式传给各 provider 调用函数（cfg 参数）。
"""

import base64
import contextlib
import contextvars
import json
import logging
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.core import comfyui
from app.core.config import PROJECT_ROOT
from app.core.contracts import resolution_tier
from app.core.provider_store import (
    ENDPOINT_MODEL_SEP,
    decode_endpoint_model,
    is_endpoint_model,
    load_default_config,
    load_endpoint_config,
)

# 任务类别（与 runner 名称/模型注册表 task_type 对齐）
PROVIDER_KINDS = ("llm", "t2i", "t2v", "i2v", "tts")

logger = logging.getLogger(__name__)

# provider 合法取值
PROVIDER_NAMES = ("local", "openai", "ark", "agnes", "custom", "comfyui", "vidu")

# LLM 请求默认超时（秒），AVS_LLM_TIMEOUT 可覆盖
DEFAULT_LLM_TIMEOUT = 120.0

# 文生图请求超时（秒）
T2I_TIMEOUT = 300.0

# 自定义视频端点轮询：总超时 15 分钟，间隔 5 秒
# ---------------------------------------------------------------------------
# 生成能力画像（借鉴 Jellyfish 的声明式 capability，见 docs/design/15）
# 字段化「某端点某档位能接受什么参数」——调用前校验并**明确报错**，
# 取代散落在转换函数里的静默钳位（静默钳位是审计 14 号里「ARK 丢参考图 /
# 二切图 offset 越界 / 时长被静默改写」的共同形态：参数被改了但没人知道）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoCapability:
    """视频生成能力约束（参考图张数/时长/画幅/分辨率档）。"""

    min_seconds: int = 5
    max_seconds: int = 12
    max_ref_images: int = 4          # 不含首帧的额外参考图上限
    first_frame_occupies_slot: bool = True   # 首帧是否占用 images 总位
    # 有首帧时是否**整段丢弃**参考图（互斥生成模式）：方舟 seedance 2.0 的
    # 「首帧」与「参考图」互斥（混传 400），agnes 则把首帧并入 images 不丢。
    # 提示词组装据此把「（参考图N）」标注与实际上送对齐，避免悬空图号。
    first_frame_excludes_refs: bool = False
    supported_ratios: tuple[str, ...] = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
    resolutions: tuple[str, ...] = ()
    # 固定种子支持（实测所得，非文档推断）：None = 端点不支持/未验证，不下发
    seed_range: tuple[int, int] | None = None
    # 种子必须**显式下发**（不给就是固定值，同提示词每次出同片）：为 True 时
    # 任务没给种子由本机生成随机的并记账（comfyui：工作流模板的 noise_seed
    # 是固定值，服务端不会自随机；云端各家不下发即服务端随机，故默认 False）
    seed_autogen: bool = False
    # 成品提示词长度预算（字符）：超出会让模型注意力被约束句稀释、画面变空洞。
    # 取值来自各模型技能包的「长度预算」节（知识在包里，此处是运行时可判定的
    # 镜像；两边由 _verify_pipeline 断言 83 对账，防止各写一个数）。
    prompt_budget: int = 0
    # 提示词里的**精确秒数锚点**（「00—02秒：」时间小节前缀）对该端点是否有效。
    # False = 端点不响应精确时间：官方《Seedance 2.0 系列提示词指南》明确
    # 「模型对精确时间（如 0–3 秒）的支持不稳定，强行限制时长可能导致生成结果
    # 异常」，2.0 只响应镜头序号、**2.5 起才响应整数秒时间戳**。提交前剥离行首
    # 秒数（保留有序的动作描述段落与换行），秒数仍用于内部时序对账与首帧派生。
    # 取保守值 False 的理由是不对称风险：多剥一层只损失一个未必有效的抓手，
    # 不剥则可能触发官方点名的「生成结果异常」。
    prompt_timestamps: bool = True
    # **同一角色的多角度参考图**（正面主图之外再送背面/侧面）该端点认不认。
    # 两家官方口径相反（25 号 §7.7）：可灵/agnes 系「1 张正面主图 + 1~3 张其他
    # 角度」是官方推荐；火山方舟/Seedance 明确「不建议使用人物多视图——会被识别
    # 为**多个不同主体**，反而加剧 ID 漂移，三视图作参考易触发分身」。
    # 故背对/侧对镜头的「补一张背面视图」策略**必须按端点分叉**，不能跨端点套用。
    multi_view_refs: bool = False


# 固定种子合法域（**实测**，见 server/_probe_cloud_params.py）：
# - agnes：域 [-1, 999]，越界被 400 拒绝（"seed must be between -1 and 999"）；
#   同 seed 两次出图**字节完全相同** → 确实生效、可复现；
# - ark Seedream/Seedance：对 seed **静默忽略**（同 seed 两次出图字节不同，
#   请求仍返回 200）→ 绝不下发：记一个"seed=123"却不生效，比不记更危险
#   （用户会按"固定种子重试"的预期行事，而结果根本不可复现）。
_AGNES_SEED_RANGE = (-1, 999)

# Agnes 视频参考图上限（**唯一来源**，flash 5 张 / 2.5 8 张，超出截断）。
# 必须在能力表之前定义——能力表要按档位取它（见 `_VIDEO_CAPABILITIES` 与
# `_MODEL_CAPABILITY_OVERRIDES` 里的 agnes 两条）。此前能力表写死 5、载荷层
# 另写一套，而组装处还硬编码 `< 4`，**实际生效的是最小的那个**（25 号 1.6）。
_AGNES_REF_IMAGES_FLASH = 5
_AGNES_REF_IMAGES_PRO = 8
_SEED_RANDOM = -1
# 本机自动生成种子的上限（与管线自产种子同域：task_engine random.randrange(2**31)）
_SEED_AUTOGEN_MAX = 2**31


def resolve_seed(provider: str, raw) -> tuple[int | None, str]:
    """任务种子 → (实际下发的种子 | None, 说明文本)。

    按**端点能力**判定（不按模型名硬匹配，符合项目铁律）：
    - 端点不支持 seed（实测 ark）→ 不下发，说明"本端点不保证复现"；
    - 支持但越界（实测 agnes 域 [-1,999]）→ **取模映射**进域内并说明，让用户
      知道实际生效的不是他填的那个数（不能用钳位：钳位会把所有越界值压成同一个
      边界值，抽卡/重摇全部撞成同一张图，2026-10-10 实测事故）；
    - 空值 → 不下发、无说明（随机是默认行为，不需要提示）；**例外**：
      能力表声明 seed_autogen 的端点（comfyui）必须显式给种子，空值时由
      本机生成随机的并在说明里记账（否则工作流模板里的固定 noise_seed
      会让每次生成出同一部片）。
    说明文本进 trace 与参数弹窗——"传了但没生效"必须被看见。
    """
    text = str(raw or "").strip()
    cap = _VIDEO_CAPABILITIES.get(provider) or _T2I_CAPABILITIES.get(provider)
    if not text:
        if cap is not None and cap.seed_autogen:
            seed = random.randrange(_SEED_AUTOGEN_MAX)
            return seed, (f"未指定种子，本次随机种子 {seed}"
                          "（已随工作流下发，同种子可复现）")
        return None, ""
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None, f"种子「{text}」不是整数，本次按随机生成"
    rng = getattr(cap, "seed_range", None) if cap else None
    if not rng:
        return None, (f"当前端点（{provider}）不支持固定种子：同一 seed 不保证复现"
                      f"（未实测支持、或实测被静默忽略），本次按随机生成")
    lo, hi = rng
    if value < lo or value > hi:
        # **映射进域内，而不是钳到边界**（2026-10-10 修复）：本机抽卡种子生成域是
        # [0, 2^31)（task_engine），恒大于 agnes 上限 999——旧实现直接钳到 999，
        # 等于**每一次抽卡都下发同一个种子**：同一提示词下「重摇」永远重出同一张
        # 图（实测：整个项目 16 次调用 seed 全部 999、两次重摇出图像素级相同，
        # 用户「重摇 2 次还是双胞胎」的直接原因）。
        # 取模映射保留两件事：① 确定性（同输入种子 → 同映射结果，可复现契约不破）；
        # ② 分布均匀（换一次种子 ≈ 1000 分之一碰撞，重摇真正换画面）。
        # 下限抬到 0 避开负数：-1 在 agnes 语义为「随机」，映射到它会破坏复现。
        floor = max(lo, 0)
        span = hi - floor + 1
        mapped = floor + (value - floor) % span
        return mapped, (f"种子 {value} 超出本端点合法域 [{lo}, {hi}]，"
                        f"已映射为 {mapped}（同种子仍可复现）")
    return value, ""


# 文生图类 runner（与 `pipeline_class` 的判定同源）——用于按 kind 解析端点配置
_T2I_RUNNERS = ("t2i", "i2i")

# 端点×档位能力表（key = provider 名，value 为默认档位能力）
_VIDEO_CAPABILITIES: dict[str, VideoCapability] = {
    # agnes 端点默认档 = flash（5 张）；2.5（pro）由 _MODEL_CAPABILITY_OVERRIDES
    # 的模型档覆盖抬到 8——**两处都取自 _AGNES_REF_IMAGES_*，不再各写一份**
    "agnes": VideoCapability(min_seconds=4, max_seconds=12,
                             max_ref_images=_AGNES_REF_IMAGES_FLASH,
                             supported_ratios=("16:9", "9:16", "1:1", "4:3",
                                               "3:4", "21:9"),
                             seed_range=_AGNES_SEED_RANGE),
    # ark：首帧与参考图互斥（有首帧即整段丢参考图）；提示词不喂精确秒数——
    # 官方 2.0 文档点名「精确时间支持不稳定」。这一行是**端点默认/保守兜底**：
    # 配置为 2.5 家族时由模型家族覆盖改回 True（见 _MODEL_CAPABILITY_OVERRIDES），
    # 端点整体换代不必改这里。
    "ark": VideoCapability(min_seconds=5, max_seconds=12, max_ref_images=4,
                           resolutions=("1080p", "720p", "480p"),
                           first_frame_excludes_refs=True,
                           prompt_timestamps=False),
    "custom": VideoCapability(min_seconds=1, max_seconds=12, max_ref_images=0),
    # comfyui（本机 ComfyUI，MiniMax-H3 参考生视频工作流，2026-10-10 接入）：
    # 时长 3~15 秒——文档换算表从 3 秒起，训练区间 5.2~15.1 秒（更短的官方标
    # untested 但不拦，短镜头照常出片，画质自担）；参考图上限取自 comfyui 模块
    # 的模型硬约束（ref_image_0..8 ↔ <Picture 1..9>，单一来源）；首帧按 agnes
    # 同口径占位（挂末位并声明为起始画面）；种子域与管线自产的种子同域
    # （task_engine random.randrange(2**31)），且必须显式下发（seed_autogen）；
    # 秒数锚点保留（H3 时序敏感、按段落顺序执行，无「不响应精确时间」的官方口径）
    "comfyui": VideoCapability(
        min_seconds=3, max_seconds=15,
        max_ref_images=comfyui.MAX_REF_IMAGES,
        supported_ratios=("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"),
        seed_range=(0, 2**31 - 1),
        seed_autogen=True),
    # vidu（Vidu 图生视频 img2video，2026-10-14 接入）：
    # - 时长 3~16 秒（官方枚举），覆盖本仓全链路口径 [4,15]，**不需要任何钳位**；
    # - 只收 1 张图（`images` 必填且只能一张）= 首帧本身，没有「额外参考图」概念，
    #   故额外参考图预算恒 0；首帧与参考图互斥（有首帧即整段丢参考图）——
    #   与方舟 seedance 同型，组装侧据此外露「已脱去参考图标注」并改指首帧；
    # - **不接收画幅参数**（官方 Body 无 aspect_ratio）：成片画幅由首帧图决定，
    #   故不声明可校验档位（空 = 不拦任何画幅），画幅一致性靠首帧本身；
    # - seed 未实测（官方文档称支持，但本仓纪律是「实测生效才下发」，见
    #   resolve_seed），故不声明 seed_range = 不下发、不声称可复现；
    # - 秒数锚点未实测（同 agnes：不动提示词，保持默认 True）。
    "vidu": VideoCapability(
        min_seconds=3, max_seconds=16,
        max_ref_images=1,
        supported_ratios=(),
        first_frame_excludes_refs=True),
}

# 未声明能力的端点（含本地推理路径）的 t2i 参考图预算——**唯一来源**：
# 提交侧（编号分配）与 runner 侧（参考图提取）都从这里取，不再各写一个 8
DEFAULT_T2I_REF_BUDGET = 8

# 文生图端点能力（同上，实测所得）：seed_range=None 即不支持下发；
# max_ref_images = 该端点**真正会收到**的参考图张数——提示词里的「参考图N」
# 编号只允许分配到这个数（按固定 8 分配而端点上只发 4 张，会让第 5-8 号悬空，
# 模型面对不存在的图号，是串脸的直接成因）。
_T2I_CAPABILITIES: dict[str, VideoCapability] = {
    # 600：agnes_image_t2i.md「分镜首帧（含参考图）≤600 字」
    "agnes": VideoCapability(seed_range=_AGNES_SEED_RANGE, max_ref_images=8,
                             prompt_budget=600, multi_view_refs=True),
    # 300：ark_seedream_t2i.md「建议 ≤300 汉字」（本模型最敏感的一项）
    "ark": VideoCapability(max_ref_images=4, prompt_budget=300),
    "openai": VideoCapability(max_ref_images=0),   # 无参考图契约，保持纯文生图
}

# 视频侧两家的包都声明「合计 ≤900 字」，故用同一预算
_VIDEO_PROMPT_BUDGET = 900


def prompt_budget(runner: str, model_id: str = "") -> tuple[int, str]:
    """成品提示词长度预算 → (上限字符, 说明)。

    按**端点能力**取（不按模型名硬匹配）：方舟 Seedream 建议 ≤300 字、
    agnes 首帧 ≤600——写成固定 600 会让 Seedream 的超标永远不报（而它正是
    对长度最敏感的模型）。无预算的 runner 返回 (0, "")。
    """
    if runner == "t2i":
        try:
            cfg, _m = resolve_runtime_config("t2i", model_id or "")
            cap = _T2I_CAPABILITIES.get(cfg.name)
            if cap and cap.prompt_budget:
                return cap.prompt_budget, (
                    f"{cfg.name} 生图建议 ≤{cap.prompt_budget} 字；"
                    "超限会分散模型注意力、主体被稀释")
        except Exception:  # noqa: BLE001 解析失败按默认预算
            pass
        return DEFAULT_T2I_PROMPT_BUDGET, "生图提示词超限会分散模型注意力、主体被稀释"
    if runner in ("ref2v", "i2v", "fl2v"):
        return _VIDEO_PROMPT_BUDGET, ("画面描述规范：前四段 250-450 字 + 声音 20-60 字；"
                                      "含参考图用途声明可略增")
    return 0, ""


# 端点未声明时的生图预算兜底（与 runners/generate 的历史默认一致）
DEFAULT_T2I_PROMPT_BUDGET = 600


def t2i_multi_view_refs(model_id: str = "") -> bool:
    """本项目 t2i 端点**认不认同一角色的多角度参考图**（背面/侧面视图）。

    两家口径相反（25 号 §7.7）：agnes 系官方推荐「正面主图 + 1~3 张其他角度」；
    方舟/Seedance 明确「不建议人物多视图——会被识别为多个主体、反而加剧 ID 漂移」。
    解析失败按**不认**处理（保守：多送一张错角度的图比不送更糟）。
    """
    try:
        cfg, _m = resolve_runtime_config("t2i", model_id or "")
        cap = _T2I_CAPABILITIES.get(cfg.name)
        return bool(cap is not None and cap.multi_view_refs)
    except Exception:  # noqa: BLE001
        return False


def t2i_ref_budget(model_id: str = "") -> int:
    """本项目 t2i 端点可接收的参考图上限（编号预算与上送共用同一上限）。

    编号只分配给端点真的会收到的图：否则「参考图5..8」在端点上不存在——
    悬空图号让模型自由发挥（实测串脸的直接成因之一）。
    """
    try:
        cfg, _m = resolve_runtime_config("t2i", model_id or "")
        cap = _T2I_CAPABILITIES.get(cfg.name)
        return int(cap.max_ref_images) if cap else DEFAULT_T2I_REF_BUDGET
    except Exception:  # noqa: BLE001 解析失败按默认预算（保守不减少）
        return DEFAULT_T2I_REF_BUDGET


# 模型家族能力覆盖（key = 模型 ID 前缀；**最长前缀优先**）。
#
# 为什么需要第二层：端点能力表按 provider 分档，但**同一端点下不同代际模型的
# 提示词能力不同**——官方《Seedance 2.0 系列提示词指南》明确「对精确时间
# （如 0–3 秒）的支持不稳定」，而《2.5 提示词指南》明确「响应整数秒时间戳」。
# 只有端点档时，ark 上切到 2.5 会被错误地当成 2.0 剥掉它本来支持的秒数。
#
# 这里是**官方规格的登记**（新增模型家族时在此登记一行），不是按模型名推断
# 能力：命中的是显式声明的前缀、取最长者，未登记一律沿用端点默认——绝不因
# 「ID 看着像新模型」而放宽任何限制。
_MODEL_CAPABILITY_OVERRIDES: dict[str, dict] = {
    # 2.5 起响应整数秒时间戳（官方《Doubao Seedance 2.5 提示词指南》差异点）
    "doubao-seedance-2-5": {"prompt_timestamps": True},
    # 2.0 只认镜头序号，精确时间支持不稳定（官方 2.0 指南「注意事项」）
    "doubao-seedance-2-0": {"prompt_timestamps": False},
    # agnes 视频：参考图上限按档位（flash 5 / 2.5 8）。两条都在表里显式登记，
    # 靠「最长前缀优先」定档——不做模糊匹配（同本表的边界校验纪律）
    "agnes-video-2.5-flash": {"max_ref_images": _AGNES_REF_IMAGES_FLASH},
    "agnes-video-2.5": {"max_ref_images": _AGNES_REF_IMAGES_PRO},
}


# 模型 ID 里版本号后可能出现的分隔符（用于前缀命中的边界校验）
_MODEL_ID_SEP = "-._:/"


def model_capability_override(model_id: str) -> dict:
    """模型 ID → 家族能力覆盖（最长前缀优先；未登记返回空 dict）。

    纯查表：不做模糊匹配、不做版本号推断。前缀命中**必须落在分隔符边界上**
    ——否则 `doubao-seedance-2-50-x`（若存在该 ID）会被 `…-2-5` 前缀误吞，
    把一个未登记的模型当成 2.5 去放宽限制（正是"按名字猜能力"那类事故）。
    """
    mid = (model_id or "").strip().lower()
    if not mid:
        return {}
    hit = ""
    for prefix in _MODEL_CAPABILITY_OVERRIDES:
        if not mid.startswith(prefix):
            continue
        # 边界校验：前缀之后要么是串尾，要么是分隔符
        if len(mid) > len(prefix) and mid[len(prefix)] not in _MODEL_ID_SEP:
            continue
        if len(prefix) > len(hit):
            hit = prefix
    return dict(_MODEL_CAPABILITY_OVERRIDES.get(hit) or {})


def video_capability(provider: str, model_id: str = "") -> VideoCapability:
    """取端点能力（未知端点给保守默认：1 张参考图、[1,12] 秒）。

    给了 model_id 时在端点档之上叠加**模型家族覆盖**（如 ark 上 2.5 比 2.0
    多支持精确时间戳）——调用方应尽量传入**本次实际提交的模型**，模型空则
    只按端点档判定。
    """
    base = _VIDEO_CAPABILITIES.get(provider, VideoCapability(max_ref_images=1))
    override = model_capability_override(model_id)
    return replace(base, **override) if override else base


def provider_seed_effective(runner_name: str, encoded_model_id: str) -> bool:
    """该任务实际用的端点**是否真的认 seed**（25 号 2.2，17 号 §40.1 实测教训）。

    `encoded_model_id` 形如 `{endpoint_id}::{model}`（见 `is_endpoint_model`）——
    注意 `::` 前是**端点 UUID 而非 provider 名**，所以必须经
    `resolve_runtime_config` 解析出端点族名再查能力表（直接拿 UUID 查必然查不到，
    会把 agnes 误判成"不认 seed"）。本地推理一律 True（种子由我们下发扩散管线）。

    为什么必须记这一位：实测 **agnes 端点同 seed 两次逐字节相同（生效）**，而
    **ark/Seedream 端点同 seed 两次结果不同、请求仍返回 200（静默忽略）**。
    只记 seed 不记"是否生效"，用户按固定种子重试时拿到的是**假的可复现性**
    ——比不记更糟。判据取端点能力表的 `seed_range` 声明，不猜。
    """
    if not is_endpoint_model(encoded_model_id):
        # **fail-safe**：只有"明确不是端点形态"（连 `::` 分隔都没有）才敢说本地生效。
        # 形如 `xxx::model` 但 UUID 不合规的，按端点处理并往下解析——拿不准就不声称
        # 可复现（假的可复现性比"没记"更糟）。
        if "::" not in (encoded_model_id or ""):
            return True
    kind = "t2i" if runner_name in _T2I_RUNNERS else "t2v"
    try:
        cfg, _model = resolve_runtime_config(kind, encoded_model_id)
    except Exception:  # noqa: BLE001 解析不出端点就别声称可复现
        return False
    provider = getattr(cfg, "name", "") or ""
    cap = _T2I_CAPABILITIES.get(provider) or _VIDEO_CAPABILITIES.get(provider)
    return bool(cap is not None and cap.seed_range)


def provider_audio_capability(provider: str) -> bool | None:
    """该视频协议产物的音轨能力：True 有 / False 明确无 / **None 未知**。

    界面徽标（画布视频节点）用本函数——未知就**不显示断言**，不把「不知道」
    渲染成「无音频，建议接配音」（2026-10-14 修正的问题形态）。
    `local` 交给模型注册表的 `ModelSpec.audio`（同协议下各模型不同：H3/LTX 有、
    Wan 无）；`custom`/`openai` 等未实测的一律 None。
    """
    if provider in ("comfyui", "agnes", "ark"):
        return True
    if provider == "vidu":
        return _VIDU_AUDIO
    return None


def provider_outputs_audio(provider: str) -> bool:
    """该视频协议产出的成片**是否自带音轨**（按实测登记，不猜）。

    实测依据（2026-10-14：直接抽检库里落盘的产物文件，ffprobe 看流 +
    volumedetect 看有没有声音——不是照抄文档，也不是沿用旧注释）：
    - comfyui：H3 工作流原生带音频（VAEDecodeAudio → CreateVideo 合入 mp4），
      抽检平均音量 −15.5 dB；
    - agnes：**原生生成音频**（技能包 §四 的实测经验；抽检 90+ 个产物**全部**
      带 audio 流，平均 −20 ~ −28 dB）。本仓请求里**没有**音频开关——音频是
      模型默认产出，不存在「关掉」这回事；
    - ark：Seedance 2.0 系原生带音频（抽检 28 个产物全部带 audio 流，
      −21 ~ −23 dB）；
    - vidu：音画同出（官方能力，由 `_VIDU_AUDIO` 开关决定，默认开）。

    明确/按保守取 False 的：custom（未知，未实测就不声称）、本地推理
    （Wan 导出的 mp4 无音轨，抽检确认）与 mock。

    本函数是 `provider_audio_capability` 的**二值版**（记录用：未知按 False，
    不声称有音轨）；界面若需要区分「未知」，请直接用三态的那个。

    **边界**：ark 精选表里的 1.0 老型号（seedance-1-0-*）未实测。本函数按
    **协议**判定、没有 model 参数——若日后接入确认无声的型号，按模型家族分档
    （参照 `_MODEL_CAPABILITY_OVERRIDES` 的登记方式）再扩签名，别按名字猜。
    如实记录的意义：别让下游（导出/拼接/合规标注/界面徽标）按错的假设行事
    ——「标记写死成 False」这一错误形态此前就是这么发生的。
    """
    return bool(provider_audio_capability(provider))


def video_ref_budget(provider: str, model_id: str = "",
                     *, has_first_frame: bool = True) -> int:
    """视频链路的**额外**参考图预算（首帧占位时已扣减）——**唯一来源**。

    组装处（drama.py）此前硬编码 `< 4`：那个 4 是手算的「agnes flash 5 张 −
    首帧占 1 位」，于是 **agnes-PRO 实际能收 8 张却只送 4 张**，白扔一半角色/
    场景锚定（25 号 1.6）。所有需要该预算的地方都必须走这里，不要再手算或硬编码。
    """
    cap = video_capability(provider, model_id)
    budget = max(0, cap.max_ref_images
                 - (1 if (has_first_frame and cap.first_frame_occupies_slot) else 0))
    # 未登记能力的端点：兜底是 1 张，扣掉首帧占位后 = **0**——参考图会被整段
    # 丢掉而**没有任何提示**（25 号复查后端 #7）。行为保持保守（不冒险给未知
    # 端点多送图），但必须**说出来**：静默降级正是本仓最忌讳的形态
    if budget == 0 and has_first_frame and provider not in _VIDEO_CAPABILITIES:
        logger.warning(
            "端点「%s」未登记参考图能力，本次视频生成不带任何参考图"
            "（角色/场景一致性只剩首帧与文字描述撑着）——"
            "如需参考图请在 _VIDEO_CAPABILITIES 登记该端点",
            provider)
    return budget


def validate_video_request(provider: str, *, duration: int, aspect: str,
                           ref_count: int, has_first_frame: bool) -> list[str]:
    """生成前校验：返回违规项列表（空 = 通过）。

    违规不再静默钳位而是**报错**——用户改过的参数被悄悄改掉，比直接报错更糟。
    """
    cap = video_capability(provider)
    problems: list[str] = []
    if duration < cap.min_seconds or duration > cap.max_seconds:
        problems.append(
            f"时长 {duration}s 超出 {provider} 支持区间 "
            f"[{cap.min_seconds}, {cap.max_seconds}]s")
    if aspect and cap.supported_ratios and aspect not in cap.supported_ratios:
        problems.append(
            f"画幅 {aspect} 不在 {provider} 支持列表 {list(cap.supported_ratios)}")
    budget = cap.max_ref_images - (1 if (has_first_frame and cap.first_frame_occupies_slot) else 0)
    if ref_count > max(0, budget):
        problems.append(
            f"参考图 {ref_count} 张超出 {provider} 上限（首帧另占 1 位，"
            f"额外参考图最多 {max(0, budget)} 张）")
    return problems


T2V_POLL_TIMEOUT_SEC = 900
T2V_POLL_INTERVAL_SEC = 5

# ---------- 云端视频轮询心跳（2026-10-10 用户报障「生成视频有时候有进度、有时候没有」） ----------
# 现象与成因：本地 ComfyUI 出片有心跳进度（comfyui._tick 按等待时长估算）；而
# Agnes/方舟的轮询段**从不发布进度**——前端进度条停在「等待生成完成…」（0%），
# 同一批视频在不同端点上跑，用户看到的就是"有的有百分比、有的没有"。云端接口
# 本身不给百分比，采用与 comfyui 同款口径：排队态 5%、生成态按「已等待 ÷ 参考
# 耗时」缓推、封顶 85%，标签写明「参考耗时」不假装精确；完整进度只在真正完成
# 后由 runner 落 1.0。心跳同时是任务看门狗的保命线（20 分钟无进度判卡死）。
_T2V_HEARTBEAT_INTERVAL_SEC = 30.0
_T2V_PROGRESS_QUEUED = 0.05
_T2V_PROGRESS_RUNNING_START = 0.10
_T2V_PROGRESS_RUNNING_SPAN = 0.75
_T2V_PROGRESS_RUNNING_CAP = 0.85
# 参考耗时估计：云端视频（Agnes/方舟）实测普遍几分钟——按「视频秒数 × 20 秒」
# 估、下限 90 秒（只把进度条从 10% 缓推到 85%，标签写明参考值）
_T2V_EST_MIN_SEC = 90.0
_T2V_EST_SEC_PER_VIDEO_SEC = 20.0
# 云端排队态状态值（agnes：pending/queued/in_progress；方舟：queued/running；
# vidu：created/queueing——官方状态枚举，未排到的任务停在 created/queueing）
_T2V_QUEUE_STATUSES = ("pending", "queued", "queued_running", "submitted",
                       "created", "queueing", "waiting")


def _video_poll_tick(elapsed: float, duration: int,
                     status: str) -> tuple[float, str]:
    """云端视频轮询心跳 (进度值, 标签)——与 comfyui 的估算口径同语义。

    排队态固定 5%；生成态按「已等待 ÷ 参考耗时」推进、封顶 85%（完成后由
    runner 落 1.0，绝不提前到 100%）。标签带已等待时长与参考耗时。
    """
    est_sec = max(_T2V_EST_MIN_SEC,
                  _T2V_EST_SEC_PER_VIDEO_SEC * max(4, int(duration or 0)))
    if status in _T2V_QUEUE_STATUSES:
        return _T2V_PROGRESS_QUEUED, f"云端排队中，已等待 {int(elapsed // 60)} 分钟"
    ratio = min(1.0, elapsed / est_sec)
    progress = _T2V_PROGRESS_RUNNING_START + _T2V_PROGRESS_RUNNING_SPAN * ratio
    return min(_T2V_PROGRESS_RUNNING_CAP, progress), (
        f"云端生成中：已等待 {int(elapsed // 60)} 分 {int(elapsed % 60)} 秒"
        f"（参考耗时约 {max(1, int(est_sec // 60))} 分钟）")

# 文生图服务常见支持档（宽x高），width/height 就近映射
T2I_SIZE_PRESETS = (
    (1024, 1024),
    (1280, 720),
    (720, 1280),
    (1024, 576),
    (576, 1024),
    (512, 512),
)

# server/.env：第三方服务配置持久化位置（PROJECT_ROOT 即 server 目录）
ENV_FILE = PROJECT_ROOT / ".env"


class FailClass:
    """失败类别（**在抛出点确定**，不靠事后猜错误文本——项目铁律：分类是确定性事实）。

    用途：区分「基础设施故障」与「创作质量失败」——前者不应占用用户的重摇预算
    （网络抖动/限流/内容安全误杀都不是「这一镜拍得不好」），后者才是重拍要解决的。
    """

    CONTENT_SAFE = "content_safe"   # 平台内容安全拦截（含写实人脸误杀）
    RATE_LIMIT = "rate_limit"       # 429 限流
    TIMEOUT = "timeout"             # 请求/轮询超时
    NETWORK = "network"             # 连接失败/断开/非 JSON 响应
    QUOTA = "quota"                 # 配额/余额不足
    PARAM = "param"                 # 参数非法（4xx 除限流/配额）
    SERVER = "server"               # 5xx 服务端错误
    UNKNOWN = "unknown"
    # **远端已计费、只是取回失败**（视频提交成功后轮询/下载断了）。
    # 单列一类的理由：这类失败**绝不允许自动重试**——重试会把 runner 从头跑
    # 一遍、**再提交一个全新的远端任务（重复扣费）**，而远端任务号没有落库、
    # 无法续取回。它是「幂等是重试的前提」在异步链路上的边界：请求已落地，
    # 原样重提不是重试而是再买一次（25 号复查 R1 实测的重复计费路径）。
    BILLED = "billed"

    # 不占创作预算的类别（基础设施与平台策略，非生成质量）
    INFRA = (CONTENT_SAFE, RATE_LIMIT, TIMEOUT, NETWORK, QUOTA, SERVER, BILLED)


# 内容安全特征词（在抛出点识别——火山系 ark/agnes 同底层，措辞固定）
_CONTENT_SAFE_MARKERS = ("PrivacyInformation", "may contain real person",
                         "content policy", "sensitive", "risk")
# 配额/余额特征词（overdue = 火山方舟 AccountOverdueError「账户欠费」的声明码；
# 实测全库 13 条欠费全是这个码——漏了它，欠费就落不成 QUOTA，会去吃创作预算）
_QUOTA_MARKERS = ("quota", "insufficient", "balance", "overdue",
                  "余额", "配额", "欠费")
# 端点拥塞特征词（队列满——平台侧瞬时接不了单，分钟级自愈；25 号 1.5）
# 均为平台**声明的错误码**（agnes: video_queue_full / image queue full），
# 不是对错误文案的语义猜测
_CONGESTION_MARKERS = ("queue_full", "queue full", "队列已满", "队列满")


def classify_failure(message: str, status_code: int = 0) -> str:
    """按错误信息（与可选 HTTP 状态码）判定失败类别，返回 FailClass 常量。

    判定顺序：状态码优先（确定性最高）→ 特征词 → 兜底 unknown。
    """
    low = (message or "").lower()
    if status_code:
        if status_code == 429:
            return FailClass.RATE_LIMIT
        # ⚠️ 特征词必须**先于** 4xx 兜底跑（25 号 1.5 实测的误分类）：
        # 平台把「欠费」包在 403 里（ark AccountOverdueError / agnes
        # insufficient_user_quota）、把「内容安全拦截」包在 400 里
        # （InputImageSensitive）。先落 PARAM 的话，这些失败就从**基础设施类**
        # 变成「参数错」——而 PARAM 不在 INFRA 里，于是**内容安全误杀与欠费
        # 会吃掉用户的创作预算**（重摇两次就被「同缺陷两次盲摇」锁死），
        # 正是「故障不该占用质量预算」那条规则的漏洞。
        if status_code in (402, 403) and any(
                m.lower() in low for m in _QUOTA_MARKERS):
            return FailClass.QUOTA
        if status_code in (401, 403):
            return FailClass.PARAM          # 鉴权失败属配置问题
        if status_code >= 500:
            return FailClass.SERVER
        if status_code >= 400:
            if any(m.lower() in low for m in _CONTENT_SAFE_MARKERS):
                return FailClass.CONTENT_SAFE
            if any(m.lower() in low for m in _QUOTA_MARKERS):
                return FailClass.QUOTA
            return FailClass.PARAM
    if any(m.lower() in low for m in _CONTENT_SAFE_MARKERS):
        return FailClass.CONTENT_SAFE
    if any(m.lower() in low for m in _QUOTA_MARKERS):
        return FailClass.QUOTA
    if "超时" in message or "timeout" in low:
        return FailClass.TIMEOUT
    if "连接失败" in message or "disconnected" in low or "非 json" in low \
            or "响应格式异常" in message:
        return FailClass.NETWORK
    return FailClass.UNKNOWN


class ProviderError(RuntimeError):
    """第三方服务调用失败（未配置/超时/HTTP 错误/响应格式异常）。

    fail_class：结构化失败类别（抛出点设置，见 classify_failure）——重摇预算
    判定与错误提示按类别分流：基础设施类（限流/超时/网络/内容安全/配额）不占
    用户的创作预算，只有真实生成质量失败才计入。
    """

    def __init__(self, message: str, fail_class: str = "",
                 http_status: int = 0) -> None:
        super().__init__(message)
        self.fail_class = fail_class or classify_failure(message)
        # HTTP 状态码（有就记进模型调用日志，网络类失败为 0）——2026-09-30 复查 Q7：
        # `model_calls.http_status` 列此前**永远是 NULL**（构造参数里根本没这一项），
        # 前端「调用日志」页那个 HTTP 状态位也就永远不会渲染。
        self.http_status = int(http_status or 0)

    @property
    def is_infra(self) -> bool:
        """是否属基础设施/平台策略类失败（不占创作重摇预算）。"""
        return self.fail_class in FailClass.INFRA


@dataclass
class ProviderConfig:
    """单类任务的第三方服务配置。"""

    provider_kind: str  # llm|t2i|t2v|i2v|tts
    name: str = "local"  # local|openai|custom
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # 多模态标记（批次 3.5）：llm 端点勾选 = 可接收图像消息（视觉审片准入）
    multimodal: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.name == "local"


# ---------- 配置读取 ----------

def _load_env_config(kind: str) -> ProviderConfig:
    """从环境变量读取指定任务类别的 provider 配置（.env 兼容回退层，每次现读）。"""
    prefix = f"AVS_{kind.upper()}"
    name = os.environ.get(f"{prefix}_PROVIDER", "local").strip().lower()
    if name not in PROVIDER_NAMES:
        name = "local"
    return ProviderConfig(
        provider_kind=kind,
        name=name,
        base_url=os.environ.get(f"{prefix}_BASE_URL", "").strip().rstrip("/"),
        api_key=os.environ.get(f"{prefix}_API_KEY", "").strip(),
        model=os.environ.get(f"{prefix}_MODEL", "").strip(),
    )


def _load_db_config(kind: str) -> ProviderConfig | None:
    """从 DB 读取该任务类别的默认端点配置（模型管理录入；未设置返回 None）。"""
    found = load_default_config(kind)
    if not found:
        return None
    provider, base_url, api_key, model = found
    return ProviderConfig(provider_kind=kind, name=provider,
                          base_url=base_url.rstrip("/"), api_key=api_key, model=model,
                          multimodal=llm_endpoint_multimodal() if kind == "llm" else False)


def llm_endpoint_multimodal(model_id: str = "") -> bool:
    """LLM 端点是否勾选了「支持多模态」（批次 3.5 准入开关）。

    读 DB 端点行的 multimodal 列（默认配置回退 .env 层时视为不支持）。
    端点取自：显式 `model_id` > 执行期上下文里的项目预设 > 全局默认端点
    （与 `llm_chat` 的解析顺序**必须一致**——否则准入判定与实际调用的端点
    可能不是同一个，放行后必然失败）。
    """
    override = (model_id or "").strip() or current_llm_model()
    try:
        from app.core.provider_store import load_llm_multimodal
        return load_llm_multimodal(override)
    except Exception:  # noqa: BLE001 存储异常按不支持处理
        return False


def _load_config(kind: str) -> ProviderConfig:
    """读取指定任务类别的 provider 配置（每次现读，保存即生效）。

    优先级：DB 默认端点（系统配置-模型管理/默认配置）> .env 五槽位兼容回退层。
    """
    return _load_db_config(kind) or _load_env_config(kind)


def resolve_runtime_config(kind: str, model_id: str) -> tuple[ProviderConfig, str]:
    """任务级模型解析：编码串 "{endpoint_id}::{model}" 直接指定端点（覆盖全局默认）。

    返回 (provider 配置, 实际模型 ID)。编码串指向的端点已删除时回退全局默认
    配置并透传原模型（由 provider 调用抛出明确错误）；非编码串返回全局配置。
    """
    decoded = decode_endpoint_model(model_id)
    if not decoded:
        return _load_config(kind), (model_id or "").strip()
    endpoint_id, model = decoded
    found = load_endpoint_config(endpoint_id)
    if not found:
        # 端点已删除：回退全局默认（缓存键仍含编码串，任务间互不影响）
        return _load_config(kind), model
    provider, base_url, api_key, ep_model = found
    cfg = ProviderConfig(provider_kind=kind, name=provider,
                         base_url=base_url.rstrip("/"), api_key=api_key, model=ep_model)
    return cfg, model


def endpoint_host(kind: str, model_id: str = "") -> str:
    """该生成类别（kind）当前会打到的**服务域名**（端点健康账本的查表键）。

    与 `resolve_runtime_config` 同源解析——账本按 host 记（同一服务商的
    t2i/t2v 共用一个账户，欠费时两边一起不可用），就绪度聚合据此在**提交前**
    告知用户（25 号 1.5）。解析不出（本地推理/配置缺失）返回空串 = 无账本可查。
    """
    try:
        from urllib.parse import urlparse

        cfg, _model = resolve_runtime_config(kind, model_id or "")
        return urlparse(cfg.base_url or "").hostname or ""
    except Exception:  # noqa: BLE001 解析不出就没有账本可查（不阻断任何流程）
        return ""


def llm_timeout() -> float:
    """LLM 请求超时（秒），AVS_LLM_TIMEOUT 覆盖，非法值回退默认。"""
    raw = os.environ.get("AVS_LLM_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_LLM_TIMEOUT
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_LLM_TIMEOUT


def mask_api_key(key: str) -> str:
    """api_key 脱敏：仅显示前 4 后 4 位（过短则全遮）。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def load_providers() -> dict[str, dict]:
    """返回全部任务类别的 provider 配置状态（api_key 脱敏，供 API 展示）。"""
    result: dict[str, dict] = {}
    for kind in PROVIDER_KINDS:
        cfg = _load_config(kind)
        result[kind] = {
            "kind": kind,
            "provider": cfg.name,
            "base_url": cfg.base_url,
            "model": cfg.model,
            "api_key": mask_api_key(cfg.api_key),
            "is_local": cfg.is_local,
        }
    return result


def provider_enabled(kind: str) -> bool:
    """指定任务类别的第三方服务是否已启用（provider 非 local 且 base_url 已配置）。"""
    cfg = _load_config(kind)
    return not cfg.is_local and bool(cfg.base_url)


def llm_available() -> bool:
    """第三方 LLM 是否可用（provider 非 local 且 base_url 已配置）。"""
    return provider_enabled("llm")


# ---------- .env 持久化 ----------

def _format_env_line(key: str, value: str) -> str:
    """值含空白、# 或引号时加双引号，避免 dotenv 解析截断。"""
    if value and (any(ch.isspace() for ch in value) or "#" in value or '"' in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"'
    return f"{key}={value}"


def update_provider_env(kind: str, values: dict[str, str]) -> None:
    """写入 server/.env 并同步进程环境变量（保留其他行，不存在则创建）。

    values 的键为小写后缀（provider/base_url/api_key/model），
    映射为 AVS_{KIND}_{SUFFIX}；同步 os.environ 使当前进程立即读到新配置。
    """
    updates = {
        f"AVS_{kind.upper()}_{suffix.upper()}": value
        for suffix, value in values.items()
    }
    lines = (
        ENV_FILE.read_text(encoding="utf-8").splitlines()
        if ENV_FILE.exists() else []
    )
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(_format_env_line(key, remaining.pop(key)))
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(_format_env_line(key, value))
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ.update(updates)


# ---------- URL 拼接与公共请求 ----------

def _api_url(base_url: str, path: str) -> str:
    """在 base_url 上拼接 API 路径：兼容 OpenAI 的 /v1 与方舟的 /api/v3 前缀。

    base_url 已含版本前缀（/v1、/api/v3、/v3）时直接追加 path，
    否则按 OpenAI 惯例补 /v1。
    """
    base = base_url.rstrip("/")
    if base.endswith(("/v1", "/api/v3", "/v3")):
        return base + path
    return base + "/v1" + path


def _chat_completions_url(base_url: str) -> str:
    """拼 chat/completions 端点（/v1 与 /api/v3 双兼容）。"""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return _api_url(base, "/chat/completions")


def _images_generations_url(base_url: str) -> str:
    """拼 images/generations 端点（/v1 与 /api/v3 双兼容）。"""
    base = base_url.rstrip("/")
    if base.endswith("/images/generations"):
        return base
    return _api_url(base, "/images/generations")


def _auth_headers(cfg: ProviderConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}


# **本层不做原样重试**（25 号 1.3/1.7「重试只能有一个 owner」）：
# 限流的重试责任归**任务级**（`task_engine._fail_or_retry`：退避 5/20s、≤2 次、
# **不占泳道**）。此前这里同步 sleep 20/40/60s——最长把 GPU 泳道占死 120 秒，
# 而且与任务级**叠乘**（3 × 3 = 最多 9 次请求）。LLM 那 3 档阶梯是「换参数降级」
# 不是原样重试，轮询 GET 抖动是幂等短重试，两者保留。


def _usage_of(payload: dict, data: object) -> dict:
    """从**请求 payload 与响应**里取消费信息（确定性；取不到就留空，不猜语义）。

    - LLM：响应的 `usage`（端点原样给的 prompt/completion/total token 数）；
    - 文生图：响应 `data` 的条数 = 出图张数；请求侧 size/ratio/n 一并记；
    - 文生视频：远端任务号 + 请求侧 duration（秒数即计费量纲）。
    """
    usage: dict = {}
    if isinstance(data, dict):
        if isinstance(data.get("usage"), dict):
            usage["tokens"] = data["usage"]
        if isinstance(data.get("data"), list):
            usage["images"] = len(data["data"])
        # prompt_id：ComfyUI 的任务号（本地端点的「远端任务号」，供回捞对账）
        for _k in ("video_id", "task_id", "prompt_id", "id"):
            if data.get(_k):
                usage["remote_task_id"] = str(data[_k])
                break
    if isinstance(payload, dict):
        # seconds：agnes 视频载荷的时长键（`"seconds": N`）。25 号复查后端 #5：
        # 只认 duration 时，agnes/方舟这两个真正在用的视频端点记不到秒数，
        # 看板的「视频秒数」恒为 0（数字骗人比没数字更糟）
        for _k in ("duration", "seconds", "size", "ratio", "n"):
            if payload.get(_k) is not None:
                usage[f"req_{_k}"] = payload[_k]
    return usage


# ---------- 调用留痕的裁剪（2026-09-30，调用日志「查看详情」用） ----------
# 单条留痕的字符上限：两万字符足够看清完整提示词与返回结构，又不至于让日志表
# 被一次长文调用（解析任务的提示词可达十万字符）撑爆；超出部分留截断标记。
_TRACE_CLIP_CHARS = 20_000
# 单个字符串字段的上限（先按字段截、再按整条截，避免一个大字段吃掉整条预算）
_TRACE_FIELD_CHARS = 4_000
# 敏感字段名（请求体里本不该有密钥——密钥只在请求头——但日志可能被导出，
# 防御性打码：只看键名，不猜内容）
_SENSITIVE_KEY_HINTS = ("api_key", "apikey", "api-key", "authorization",
                        "token", "secret", "password")


def _redact_trace(obj: object, depth: int = 0) -> object:
    """递归打码敏感字段 + 截断超长字符串/超长列表（留痕用，不是精确数据）。"""
    if depth > 6:
        return "…（层级过深已截断）"
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in list(obj.items())[:200]:
            name = str(k).lower()
            if any(h in name for h in _SENSITIVE_KEY_HINTS):
                out[k] = "***"
            else:
                out[k] = _redact_trace(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        items = [_redact_trace(x, depth + 1) for x in list(obj)[:200]]
        if len(obj) > 200:
            items.append(f"…（共 {len(obj)} 项，已截断）")
        return items
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj
    text = obj if isinstance(obj, str) else str(obj)
    if len(text) > _TRACE_FIELD_CHARS:
        return text[:_TRACE_FIELD_CHARS] + f"…（截断，原长 {len(text)} 字符）"
    return text


def _clip_trace(obj: object) -> str:
    """留痕对象 → 打码 + JSON 字符串（超长截断，带原文长度标记）。"""
    if obj is None:
        return ""
    try:
        text = json.dumps(_redact_trace(obj), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    if len(text) > _TRACE_CLIP_CHARS:
        text = text[:_TRACE_CLIP_CHARS] + f"…（截断，原长 {len(text)} 字符）"
    return text


# ---------- 模型服务端点费率（33 号方案 2026-10-09：按服务端点配置费率） ----------

# 费率缓存：[(host, model, rates dict)] + 加载时间。TTL 30 秒惰性重载
# （改价保存会显式失效——见 provider_admin 端点的 PATCH 与
# billing.apply_suggested_endpoint_prices）；全 0 费率的端点不进表（免费）。
_ENDPOINT_RATES_TTL_SEC = 30.0
_endpoint_rates: dict = {"entries": [], "loaded_at": 0.0}


def invalidate_endpoint_rates() -> None:
    """显式让端点费率缓存失效（「模型管理」改价保存后调用，改价立即生效）。"""
    _endpoint_rates["loaded_at"] = 0.0


def _load_endpoint_rates() -> list[tuple[str, str, dict]]:
    """装载计费中的模型服务端点费率：[(服务域名, 默认模型, 费率 dict)]（TTL 缓存）。"""
    now = time.time()
    if (now - float(_endpoint_rates["loaded_at"] or 0)
            <= _ENDPOINT_RATES_TTL_SEC):
        return _endpoint_rates["entries"]
    entries: list[tuple[str, str, dict]] = []
    try:
        from urllib.parse import urlparse

        from app.db.models import ProviderEndpoint
        from app.db.session import SessionLocal
        with SessionLocal() as db:
            for r in db.query(ProviderEndpoint).all():
                if not getattr(r, "bill_enabled", True):
                    continue
                rates = {
                    "credits_per_1k_tokens": int(
                        getattr(r, "credits_per_1k_tokens", 0) or 0),
                    "credits_per_image": int(
                        getattr(r, "credits_per_image", 0) or 0),
                    "credits_per_video_second": int(
                        getattr(r, "credits_per_video_second", 0) or 0),
                }
                if not any(rates.values()):
                    continue  # 全 0 = 该服务免费，不必进表
                host = (urlparse(r.base_url or "").hostname or "").lower()
                if not host:
                    continue
                entries.append((host, (r.model or "").strip(), rates))
    except Exception:  # noqa: BLE001 费率读取失败按「无计费」处理（fail-open）
        logger.exception("端点费率装载失败（本次按不计费处理）")
    _endpoint_rates["entries"] = entries
    _endpoint_rates["loaded_at"] = now
    return entries


def _usage_units(usage: dict) -> dict:
    """调用日志的 usage_json → 计价用量结构 {tokens, images, video_seconds}。"""
    def _int(v) -> int:  # noqa: ANN001
        try:
            return max(0, int(float(v or 0)))
        except (TypeError, ValueError):
            return 0

    t = usage.get("tokens")
    tokens = _int(t.get("total_tokens")) if isinstance(t, dict) else 0
    seconds = 0
    for k in ("req_duration", "req_seconds"):
        if usage.get(k) is not None:
            seconds = _int(usage.get(k))
            break
    return {"tokens": tokens, "images": _int(usage.get("images")),
            "video_seconds": seconds}


def _call_credits(host: str, model: str, usage: dict) -> int:
    """这一行调用值多少积分：按**所用模型服务端点的费率 × 本次用量**。

    匹配是确定性的：调用日志的 host 与服务端点 base_url 的域名逐字比对，
    同域名多条时再用模型名逐字优先；匹配不到 = 0（本地推理 / 未配费率的
    服务 / 已删除的服务——宁缺毋滥，不猜）。
    """
    from app.core.billing import usage_credits
    host_l = (host or "").lower()
    if not host_l:
        return 0
    entries = _load_endpoint_rates()
    same_host = [(m, rates) for h, m, rates in entries if h == host_l]
    if not same_host:
        return 0
    model_s = (model or "").strip()
    exact = [rates for m, rates in same_host if m and m == model_s]
    rates = exact[0] if exact else same_host[0][1]
    return usage_credits(rates, _usage_units(usage))


def _record_model_call(what: str, url: str, payload: dict, *, ok: bool,
                       elapsed_ms: float, data: object = None,
                       fail_class: str = "", error: str = "",
                       http_status: int = 0) -> None:
    """落一行模型调用日志（25 号 2.3/2.6 的底座）。

    **fail-open**：记录失败绝不影响调用本身——日志是观测，不是业务。
    `host`/`model` 由 URL 与**请求 payload** 确定性取出（每个端点的 payload 都有
    `model` 字段），不做关键词猜测。
    """
    try:
        from urllib.parse import urlparse

        from app.db.models import ModelCall
        from app.db.session import SessionLocal
        from app.services import provider_health

        _pid, _tid = _current_task_ctx.get()
        # 计费关联（33 号方案）：当前请求编号由 BillingGate 经 ContextVar 传递
        # ——同步端点结算时按它汇总行级积分。任务工作线程里读不到属预期
        # （任务化计费走 task_id 汇总，不依赖它）。
        from app.core import billing as _billing
        _rid = _billing.current_request_id()
        host = (urlparse(url).hostname or "")[:128]
        model_name = str((payload or {}).get("model") or "")
        usage = _usage_of(payload or {}, data)
        # 行级计价（2026-10-09 口径定稿）：**成功的调用**按所用模型服务端点
        # 的费率算好「这一行值多少积分」存下（结算 = 按范围 SUM 它）；
        # 失败行 0（失败不计费——「远端已计费」类的补偿由退款规则统一处理）。
        credits = _call_credits(host, model_name, usage) if ok else 0
        with SessionLocal() as db:
            db.add(ModelCall(
                what=(what or "")[:64],
                host=host,
                model=model_name[:128],
                ok=bool(ok), fail_class=(fail_class or "")[:32],
                error=(error or "")[:500],
                http_status=int(http_status or 0) or None,
                elapsed_ms=int(elapsed_ms),
                usage_json=usage,
                credits=credits,
                project_id=_pid or None, task_id=_tid or None,
                request_id=_rid or "",
                # 调用留痕（调用日志「查看详情」）：完整地址 + 请求/响应正文，
                # 写入前打码敏感字段并截断超长内容（见 _clip_trace）
                url=(url or "")[:500],
                request_json=_clip_trace(payload or None),
                response_json=_clip_trace(data),
            ))
            # 端点健康账本（25 号 1.5）：**与调用日志同一处收口**——失败记标记
            # （只记欠费/队列满两类，见 provider_health.note_failure），成功即解除。
            # 自愈优先于 TTL：用户充值后第一次调用成功就会清掉标记。
            if ok:
                provider_health.clear_mark(db, host)
            else:
                provider_health.note_failure(
                    db, host, fail_class=fail_class, error=error,
                    congestion_markers=_CONGESTION_MARKERS)
            db.commit()
    except Exception as e:  # noqa: BLE001 日志不能拖垮调用
        logger.warning("模型调用日志写入失败（忽略）：%s", e)


def _post_json(url: str, payload: dict, headers: dict, timeout: float,
               what: str) -> dict:
    """POST JSON 并解析响应，超时/HTTP 错误抛 ProviderError（中文信息）。

    **本层不重试**（见上方常量处的说明）：限流照样抛 `FailClass.RATE_LIMIT`，
    由任务级退避重试接管——这样既不占死泳道，也不会与任务级叠乘。

    **模型调用日志的挂钩点**（25 号：每次调用一行）——包装内层实现，成功与失败
    都记（失败带 fail_class），记录本身 fail-open。
    """
    _t0 = time.perf_counter()
    try:
        data = _post_json_inner(url, payload, headers, timeout, what)
    except ProviderError as e:
        _record_model_call(what, url, payload, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=getattr(e, "fail_class", ""), error=str(e),
                           http_status=getattr(e, "http_status", 0))
        raise
    _record_model_call(what, url, payload, ok=True,
                       elapsed_ms=(time.perf_counter() - _t0) * 1000, data=data)
    return data


def _post_json_inner(url: str, payload: dict, headers: dict, timeout: float,
                     what: str) -> dict:
    """`_post_json` 的内层实现（真正发请求；日志挂钩在外层）。"""
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException as e:
        # 超时（连接/读/写）：TIMEOUT 不在自动重试名单里——读超时时请求可能
        # 已被服务端处理，原样重提有重复计费风险，只留人工重试入口
        raise ProviderError(f"{what}请求超时（{timeout:.0f} 秒）：{url}",
                            FailClass.TIMEOUT) from e
    except httpx.ConnectError as e:
        # **连都没连上**（DNS 解析失败/拒绝连接/TLS 握手失败）：请求确定没有
        # 发出去，自动重试不会重复计费 → NETWORK（可重试）
        raise ProviderError(f"{what}服务连接失败（{url}）：{e}",
                            FailClass.NETWORK) from e
    except httpx.TransportError as e:
        # 其余传输层错误（连接中途断开/响应读取中断/协议错）：请求**可能已经被
        # 服务端收到并计费**，原样重试等于再买一次——2026-09-30 复查 R4：旧实现
        # 一律归 NETWORK，而 NETWORK 在自动重试名单里，同步生图断连会重复扣费。
        # 改归 BILLED（语义=「可能已产生计费，不自动重试」，与远端取回失败同类）。
        raise ProviderError(
            f"{what}请求中断（响应未取回，可能已计费，不自动重试）：{url}：{e}",
            FailClass.BILLED) from e
    if resp.status_code == 429:
        raise ProviderError(f"{what}触发限流（HTTP 429）：{resp.text[:150]}",
                            FailClass.RATE_LIMIT, http_status=429)
    if resp.status_code >= 400:
        # 状态码优先分类（配额 402/内容安全 400 由 classify_failure 按特征词细分）
        raise ProviderError(
            f"{what}服务返回 HTTP {resp.status_code}：{resp.text[:200]}",
            classify_failure(resp.text[:400], resp.status_code),
            http_status=resp.status_code)
    try:
        return resp.json()
    except ValueError as e:
        raise ProviderError(f"{what}响应格式异常（非 JSON）：{e}",
                            FailClass.NETWORK) from e


# 轮询查询连续失败上限（网络抖动容错：单次 SSL EOF/超时重试，连续 3 次才失败）
_POLL_FAIL_MAX = 3


def _poll_get_json(url: str, headers: dict, what: str) -> dict:
    """轮询 GET 的**日志包装**：只在失败时记一行。

    为什么成功不记：视频轮询每 5 秒一次、单任务可上百次，逐次记账会把真正的
    模型调用记录淹掉；失败才是有信息量的那一次（25 号 2.3）。
    """
    _t0 = time.perf_counter()
    try:
        return _poll_get_json_inner(url, headers, what)
    except ProviderError as e:
        _record_model_call(what, url, {}, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=getattr(e, "fail_class", ""), error=str(e))
        raise


def _poll_get_json_inner(url: str, headers: dict, what: str) -> dict:
    """轮询用 GET：单次网络异常/非 JSON 容错重试，连续 3 次才抛 ProviderError。

    异步任务轮询期间的网络抖动（SSL EOF/超时/网关闪断）不应使整体任务失败；
    HTTP 4xx（如 key 无效）为确定性错误，立即抛出不重试。
    """
    last_err: Exception | None = None
    for _ in range(_POLL_FAIL_MAX):
        try:
            resp = httpx.get(url, headers=headers, timeout=30)
            if resp.status_code >= 400:
                raise ProviderError(
                    f"{what}查询失败（HTTP {resp.status_code}）：{resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as e:
                last_err = e
        except httpx.HTTPError as e:
            last_err = e
        time.sleep(2)
    raise ProviderError(f"{what}查询连续失败（{url}）：{last_err}")


# ---------- LLM（OpenAI 兼容 chat/completions）----------

# ---------- 任务级 LLM 端点覆盖（项目设置里的「LLM 模型」预设）----------
# 为什么用 contextvar 而不是逐层传参：`llm_parser._llm_call` 有 18 个调用方、
# 还嵌着若干私有辅助函数，逐个加参数要改 40+ 处，且这是**执行期上下文**而非
# 函数参数（同一次任务的所有 LLM 调用共用一个端点）。
#
# **必须显式复制上下文**：`loop.run_in_executor` **不传播** contextvar
# （实测；那是 `asyncio.to_thread` 才做的）。调用方要
# `ctx = contextvars.copy_context()` 后 `run_in_executor(pool, ctx.run, fn)`。
#
# 空串 = 用全局默认端点（未设置项目预设时的行为，向后兼容）。
# 任务上下文（project_id, task_id）：供**模型调用日志**归属用。与 llm 预设同一套
# 传递方式（提交线程池前 set + copy_context），没设就是 None（LLM 调用常发生在任务外）。
_current_task_ctx: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "avs_task_ctx", default=("", ""))


@contextlib.contextmanager
def use_task_ctx(project_id: str, task_id: str):
    """with 块内让**模型调用日志**带上项目/任务归属（写法同 `use_llm_model`）。"""
    token = _current_task_ctx.set((str(project_id or ""), str(task_id or "")))
    try:
        yield
    finally:
        _current_task_ctx.reset(token)


_current_llm_model: contextvars.ContextVar[str] = contextvars.ContextVar(
    "avs_llm_model", default="")


@contextlib.contextmanager
def use_llm_model(model_id: str):
    """with 块内让 LLM 调用走指定端点（`{端点id}::{模型}` 编码串；空串=全局默认）。

    用法（**在提交给线程池之前设置，并对线程池复制上下文**）：
        with use_llm_model(preset):
            ctxvars = contextvars.copy_context()
            await loop.run_in_executor(pool, ctxvars.run, fn, ...)
    """
    token = _current_llm_model.set(str(model_id or "").strip())
    try:
        yield
    finally:
        _current_llm_model.reset(token)


def current_llm_model() -> str:
    """当前上下文里的 LLM 端点覆盖（空串 = 未覆盖）。"""
    return _current_llm_model.get()


def llm_chat(messages: list[dict], *, json_mode: bool = False,
             temperature: float = 0.7, max_tokens: int = 4096,
             timeout: float | None = None,
             thinking_disabled: bool = False,
             model_id: str = "",
             on_delta: Callable[[str], None] | None = None,
             cancel_check: Callable[[], bool] | None = None) -> str:
    """调用第三方 LLM，返回完整回复文本。

    json_mode=True 时携带 response_format json_object（要求模型输出 JSON）；
    timeout 缺省用 AVS_LLM_TIMEOUT（长输出调用可自行放大）；
    thinking_disabled=True 时携带 thinking 关闭参数（推理型模型的思考计入
    max_tokens 会导致密集任务正文零输出，结构化提取场景关闭可根治）；
    model_id：端点编码串 `{端点id}::{模型}`（空 = 走执行期上下文里的项目预设，
    再空 = 全局默认）——见 `use_llm_model`；
    on_delta 提供时走 SSE 流式：思考（reasoning）与正文增量逐块回调——
    解析智能体的思考过程经此实时观测。端点不认识 thinking 参数时通常忽略。
    未配置/失败抛 ProviderError（中文错误信息），调用方自行回退。
    """
    # 端点解析：显式传入 > 执行期上下文（项目预设）> 全局默认。
    # `resolve_runtime_config` 对「非编码串/端点已删」都会回退全局配置，
    # 所以这里无需额外判空——项目预设失效时行为退化为全局默认，不报错。
    cfg, _ = resolve_runtime_config("llm", model_id or _current_llm_model.get())
    if cfg.is_local:
        raise ProviderError(
            "LLM 未配置：请在「系统配置-模型管理」录入 LLM 端点并在「默认配置」中启用"
        )
    if not cfg.base_url:
        raise ProviderError("LLM 服务地址未配置（请在默认配置中选择 LLM 端点）")
    payload: dict = {
        "model": cfg.model or "default",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if thinking_disabled:
        # DeepSeek 系推理模型支持（实测生效）；不支持的端点忽略或返回 400，
        # 400 时由调用方换不带该参数的尝试兜底
        payload["thinking"] = {"type": "disabled"}

    if on_delta is None:
        return _llm_chat_once(cfg, payload,
                              timeout if timeout is not None else llm_timeout())
    return _llm_chat_stream(cfg, payload,
                            timeout if timeout is not None else llm_timeout(),
                            on_delta, cancel_check)


def _llm_chat_once(cfg: ProviderConfig, payload: dict, timeout: float) -> str:
    """非流式调用（原路径）。"""
    data = _post_json(
        _chat_completions_url(cfg.base_url), payload,
        _auth_headers(cfg), timeout, "LLM",
    )
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(f"LLM 响应格式异常（解析 choices 失败）：{e}") from e
    if not isinstance(content, str) or not content.strip():
        # 空内容需区分原因：length = 推理/输出耗尽 token，其余透出 finish_reason 便于定位
        finish = str(choice.get("finish_reason") or "unknown")
        if finish == "length":
            raise ProviderError(
                "LLM 输出被 max_tokens 截断且未产出正文（推理耗时超出输出上限），"
                "请减小单次拆解篇幅或调大 max_tokens 后重试")
        raise ProviderError(f"LLM 返回内容为空（finish_reason={finish}）")
    return content


def _llm_chat_stream(cfg: ProviderConfig, payload: dict, timeout: float,
                     on_delta: Callable[[str], None],
                     cancel_check: Callable[[], bool] | None = None) -> str:
    """SSE 流式调用：思考（reasoning_content）与正文（content）增量逐块回调。

    与非流式共享空内容/截断的失败语义；增量仅经 on_delta 外送（观测用），
    返回值仍是拼接后的完整正文。
    """
    payload = {**payload, "stream": True}
    parts: list[str] = []
    finish = "unknown"
    url = _chat_completions_url(cfg.base_url)
    _t0 = time.perf_counter()
    try:
        with httpx.stream("POST", url, json=payload,
                          headers=_auth_headers(cfg), timeout=timeout) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", "replace")
                # 状态码要显式参与分类：这里原先只给文案，ProviderError 兜底按
                # 文案猜——实测 `LLM 服务返回 HTTP 429：…` 会被判成 unknown，
                # 于是**限流既不自动重试、也不算基础设施失败**（25 号复查：
                # 逐集解析/审核/资产全走流式，等于主力路径上这两条都没生效）
                raise ProviderError(
                    f"LLM 服务返回 HTTP {resp.status_code}：{body[:200]}",
                    classify_failure(body[:400], resp.status_code))
            for line in resp.iter_lines():
                # 取消探针：SSE 逐块高频检查，停止解析可秒级中止在途调用
                if cancel_check is not None and cancel_check():
                    raise ProviderError("LLM 调用已取消")
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                if not data_str:
                    continue
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    continue  # 心跳/注释行跳过
                choices = chunk.get("choices") or []
                choice = choices[0] if choices else {}
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    on_delta(str(reasoning))
                piece = delta.get("content")
                if piece:
                    parts.append(str(piece))
                    on_delta(str(piece))
                if choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])
    except httpx.TimeoutException as e:
        _record_model_call("LLM（流式）", url, payload, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=FailClass.TIMEOUT, error=str(e))
        raise ProviderError(f"LLM 请求超时（{timeout:.0f} 秒）",
                            FailClass.TIMEOUT) from e
    except httpx.HTTPError as e:
        _record_model_call("LLM（流式）", url, payload, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=FailClass.NETWORK, error=str(e))
        raise ProviderError(f"LLM 服务连接失败：{e}", FailClass.NETWORK) from e
    except ProviderError as e:
        _record_model_call("LLM（流式）", url, payload, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=getattr(e, "fail_class", ""), error=str(e))
        raise
    content = "".join(parts)
    if not content.strip():
        _err = ("LLM 输出被 max_tokens 截断且未产出正文（推理耗时超出输出上限），"
                "请减小单次拆解篇幅或调大 max_tokens 后重试") \
            if finish == "length" else f"LLM 返回内容为空（finish_reason={finish}）"
        _record_model_call("LLM（流式）", url, payload, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=FailClass.UNKNOWN, error=_err)
        raise ProviderError(_err)
    _record_model_call("LLM（流式）", url, payload, ok=True,
                       elapsed_ms=(time.perf_counter() - _t0) * 1000,
                       # 流式响应正文也留痕（调用日志「查看详情」要能看返回内容；
                       # 超长由 _clip_trace 截断）
                       data={"content": content})
    return content


# ---------- 文生图（OpenAI 兼容 images/generations）----------

def _map_size(width: int, height: int) -> str:
    """width/height 按宽高比（优先）与分辨率就近映射到服务支持档。"""
    try:
        w = int(width) or 1024
        h = int(height) or 1024
    except (TypeError, ValueError):
        w = h = 1024

    def distance(preset: tuple[int, int]) -> tuple[float, int]:
        pw, ph = preset
        return (abs(pw / ph - w / h), abs(pw - w) + abs(ph - h))

    pw, ph = min(T2I_SIZE_PRESETS, key=distance)
    return f"{pw}x{ph}"


def _download_image(image_url: str) -> bytes:
    """下载图片 URL 为字节（部分兼容服务以 url 而非 b64_json 返回）。"""
    try:
        resp = httpx.get(image_url, timeout=T2I_TIMEOUT)
    except httpx.TimeoutException as e:
        raise ProviderError(f"图片下载超时（{image_url}）") from e
    except httpx.HTTPError as e:
        raise ProviderError(f"图片下载失败（{image_url}）：{e}") from e
    if resp.status_code >= 400:
        raise ProviderError(f"图片下载失败（HTTP {resp.status_code}）：{image_url}")
    return resp.content


def t2i_openai(prompt: str, width: int = 1024, height: int = 1024,
               count: int = 1, size_hint: str = "",
               model_id: str = "", cfg: ProviderConfig | None = None) -> list[bytes]:
    """调用 OpenAI 兼容 images/generations，返回图片字节列表。

    size 由 width/height 就近映射到服务支持档（size_hint 非空时优先）；
    model_id 非空时优先于环境变量默认模型；兼容 b64_json 与 url 两种返回
    形式；cfg 缺省读全局 t2i 配置（编码串任务显式传入）；失败抛 ProviderError。
    """
    cfg = cfg or _load_config("t2i")
    if cfg.name not in ("openai", "ark"):
        raise ProviderError(
            "文生图未配置 OpenAI 兼容服务（AVS_T2I_PROVIDER 应为 openai）")
    if not cfg.base_url:
        raise ProviderError("文生图 base_url 未配置（AVS_T2I_BASE_URL）")
    payload = {
        "model": model_id or cfg.model or "default",
        "prompt": prompt,
        "size": size_hint or _map_size(width, height),
        "n": max(1, count),
        "response_format": "b64_json",
    }
    data = _post_json(
        _images_generations_url(cfg.base_url), payload,
        _auth_headers(cfg), T2I_TIMEOUT, "文生图",
    )
    images: list[bytes] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        b64 = item.get("b64_json")
        if b64:
            images.append(base64.b64decode(b64))
        elif item.get("url"):
            images.append(_download_image(item["url"]))
    if not images:
        raise ProviderError("文生图服务未返回图片数据")
    return images


# ---------- 火山方舟（Ark：Seedream 文生图 / Seedance 视频生成）----------

# Seedream 文生图尺寸档（宽x高，按宽高比就近映射）
# 实测 seedream-5-0-lite 要求 ≥3686400 像素（400 InvalidParameter），各档取
# 满足该约束的**最小**尺寸以省成本（1:1 用 1920² 比 2048² 省 44% 像素计费）；
# 旧版本模型（seedream 4-0 等）支持 1024-4096 区间，本表对其同样合法
_ARK_T2I_SIZES = {
    "1:1": "1920x1920",
    "16:9": "2560x1440",
    "9:16": "1440x2560",
    "4:3": "2304x1728",
    "3:4": "1728x2304",
    "2:3": "1568x2352",
}

# Seedance 视频分辨率档（按高度就近选择）
# 已废弃：档位判定统一走 contracts.resolution_tier（按短边，单一来源）。
# 该表曾按 height 判定，导致竖屏 720p（720×1280）误判为 1080p 档。

# 方舟文生图参考图上限（Seedream image 字段支持多图参考，此处按一致性
# 生成的典型用量收紧到 4 张，超出截断）
# 已废弃：上限统一取自 _T2I_CAPABILITIES（单一来源，见 t2i_ref_budget）
_ARK_T2I_MAX_REFS_LEGACY = 4

# 精选模型列表（方舟未提供公开模型列表 API 时的回退，均为官方模型 ID）
_ARK_CURATED_MODELS = {
    "t2i": (
        "doubao-seedream-4-0-250828",
        "doubao-seedream-3-0-t2i-250415",
    ),
    "t2v": (
        "doubao-seedance-1-0-pro-250528",
        "doubao-seedance-1-0-lite-t2v-250428",
        "doubao-seedance-1-0-lite-i2v-250428",
    ),
}


def _ark_base(cfg: ProviderConfig) -> str:
    """方舟 base_url 校验（须含 /api/v3 或为官方域名）。"""
    if not cfg.base_url:
        raise ProviderError("方舟 base_url 未配置（AVS_T2I_BASE_URL / AVS_T2V_BASE_URL，"
                            "如 https://ark.cn-beijing.volces.com/api/v3）")
    return cfg.base_url.rstrip("/")


# 参考图上送规格（实测教训，见 docs/design/15 §36）：参考图较多/较大时端点会
# 直接断连（Server disconnected without sending a response）——5 张 PNG 约 3MB
# 即触发。统一压到长边 ≤1024 的 JPEG，payload 降到 ~500KB 量级。
REF_UPLOAD_MAX_EDGE = 1024
REF_UPLOAD_JPEG_QUALITY = 85
# 触发压缩的**原始字节**阈值（单图）：小图保留无损质量，大图才付压缩代价
REF_UPLOAD_AUTO_COMPRESS_BYTES = 1_500_000
# 连接级失败后的"再压一档"目标（**显式传参，不外改全局**——见 _shrink_ref_payload）
REF_SHRINK_MAX_EDGE = 768
REF_SHRINK_JPEG_QUALITY = 75


def _file_data_url(path: str, mime: str = "image/png",
                   compress: bool | None = None,
                   max_edge: int | None = None,
                   quality: int | None = None) -> str:
    """本地图片转 base64 data URL（端点 image_url 支持，突破本地图无法上送的约束）。

    compress 三态，**按用途选而不是按厂商选**：
    - `None`（默认，**参考图**用）：按大小触发——只有原始字节超过阈值才压。
      参考图只作身份/陈设锚点，小图保留无损质量对相似度有利，大图才付压缩
      代价（实测 5 张 PNG ≈3MB 时端点直接断连，见 docs/design/13 §7）；
    - `False`（**首帧**用）：从不压缩。首帧是视频开场画面本身、是要被"动起来"
      的那一帧，压缩会直接劣化成片首帧观感——与"只作锚点"的参考图性质不同；
    - `True`：总是压缩（显式要求时）。
    """
    data: bytes | None = None
    if compress is None:
        try:
            data = Path(path).read_bytes()
        except OSError:
            data = b""
        compress = len(data) > REF_UPLOAD_AUTO_COMPRESS_BYTES
    if compress:
        try:
            import io

            from PIL import Image

            with Image.open(path) as img:
                img = img.convert("RGB")
                w, h = img.size
                edge = max_edge or REF_UPLOAD_MAX_EDGE
                scale = edge / max(w, h)
                if scale < 1:
                    img = img.resize((max(1, int(w * scale)),
                                      max(1, int(h * scale))),
                                     Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG",
                         quality=quality or REF_UPLOAD_JPEG_QUALITY)
            return ("data:image/jpeg;base64,"
                    + base64.b64encode(buf.getvalue()).decode())
        except Exception:  # noqa: BLE001 压缩失败回退原图（不阻断生成）
            pass
    if data is None:
        data = Path(path).read_bytes()
    if len(data) > 20 * 1024 * 1024:
        raise ProviderError(f"参考图过大（{len(data) // 1024 // 1024} MB），请压缩后重试")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def t2i_ark(prompt: str, width: int = 1024, height: int = 1024,
            count: int = 1, model_id: str = "",
            ref_paths: list[str] | None = None,
            cfg: ProviderConfig | None = None) -> list[bytes]:
    """方舟 Seedream 文生图（images/generations），返回图片字节列表。

    尺寸按宽高比映射到官方支持档；count > 1 时逐次调用（方舟以
    sequential_image_generation 控制组图，此处保持逐张语义）。
    ref_paths 非空时以本地图为参考图（base64 data URL）走参考图生图，
    用于角色一致性生成（四视图/妆造以基础形象为参考）；
    cfg 缺省读全局 t2i 配置（编码串任务显式传入）。
    """
    cfg = cfg or _load_config("t2i")
    if cfg.name != "ark":
        raise ProviderError("文生图未配置方舟服务（AVS_T2I_PROVIDER 应为 ark）")
    base = _ark_base(cfg)
    # 宽高比就近映射官方尺寸档（与本地 _map_size 的对数距离一致）
    ratio = width / max(1, height)
    size = min(_ARK_T2I_SIZES.values(),
               key=lambda s: abs((int(s.split("x")[0]) / int(s.split("x")[1])) - ratio))
    payload = {
        "model": model_id or cfg.model or _ARK_CURATED_MODELS["t2i"][0],
        "prompt": prompt,
        "size": size,
        "response_format": "b64_json",
        "watermark": False,
        "sequential_image_generation": "disabled",
    }
    if ref_paths:
        # 参考图生图：image 字段支持单图或多图数组（本地图转 base64 上送）
        _ark_max = _T2I_CAPABILITIES["ark"].max_ref_images
        if len(ref_paths) > _ark_max:
            # 截断告警：被裁的是「角色→场景→道具」序尾部的场景/道具参考图
            # （角色已优先占号），提示词中的对应「参考图N」标注会悬空——
            # 排查生成质量时先看这条日志
            logger.warning(
                "方舟文生图参考图超出 %d 张上限，尾部截断 %d→%d 张"
                "（被裁参考图将失去提示词标注对应）",
                _ark_max, len(ref_paths), _ark_max)
        # 参考图：按大小触发压缩（小图保无损，大图防端点断连）
        payload["image"] = [_file_data_url(p, compress=None)
                            for p in ref_paths[:_ark_max]]
    images: list[bytes] = []
    for _ in range(max(1, count)):
        data = _post_json(_images_generations_url(base), payload,
                          _auth_headers(cfg), T2I_TIMEOUT, "方舟文生图")
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json")
            if b64:
                images.append(base64.b64decode(b64))
            elif item.get("url"):
                images.append(_download_image(str(item["url"])))
    if not images:
        raise ProviderError("方舟文生图未返回图片数据")
    return images


def _humanize_content_safe(msg: str) -> str:
    """内容安全拦截错误的中文指引（审计 13 号 #5）。

    方舟/agnes 同底层，写实 AI 人脸首帧易被误判「疑似真人」。此前只有 ark
    提交段做了友好化，而文档明确「提交成功后轮询阶段才被内容安全拦」且
    agnes 是异步生成本身失败——用户在最高频的失败点上只看到裸英文。
    统一在提交/轮询各形态调用本函数。
    """
    if "PrivacyInformation" not in msg and "may contain real person" not in msg:
        return msg
    return ("视频平台内容安全拦截：首帧图片被判定「疑似真人」（写实 AI "
            "人脸的常见误杀，agnes/ark 同底层均会触发）。建议：①改用"
            "二次元/3D 等非写实画风重新生成资产与首帧；②或换用非火山系"
            "视频供应商。原文：" + msg[:160])


def t2v_ark(prompt: str, duration: int = 5, width: int = 1280,
            height: int = 720, model_id: str = "",
            first_frame_path: str | None = None,
            ref_paths: list[str] | None = None,
            cancel_check: Callable[[], bool] | None = None,
            cfg: ProviderConfig | None = None) -> dict:
    """方舟 Seedance 视频生成（提交段 + 轮询段，等价 t2v_ark_submit + fetch）。

    参数以文本后缀注入官方约定（--resolution / --duration / --watermark false）；
    首帧与参考图互斥（有首帧走首帧模式）。保留本函数供需要「一次调用拿到结果」
    的调用方使用；runner 侧改用 submit/fetch 两段式以区分「已提交但取回失败」。
    """
    cfg = cfg or _load_config("t2v")
    task_id = t2v_ark_submit(prompt, duration, width, height, model_id,
                             first_frame_path=first_frame_path,
                             ref_paths=ref_paths, cfg=cfg)
    # 轮询段的 base 与 fetch 段同源（_ark_base(cfg)）：此前误写成未定义的局部名
    # `base`，一旦真被调用即 NameError
    return _poll_ark_video_task(cfg, _ark_base(cfg), str(task_id), cancel_check)


def t2v_ark_submit(prompt: str, duration: int = 5, width: int = 1280,
                   height: int = 720, model_id: str = "",
                   first_frame_path: str | None = None,
                   ref_paths: list[str] | None = None,
                   cfg: ProviderConfig | None = None) -> str:
    """方舟视频【提交段】：返回 provider task_id（审计 13 号 L）。

    与 t2v_ark 拆开的原因是「已提交但取回失败」与「未提交成功」必须区分：
    前者远端已计费，回退本地重算＝双份成本，且无 id 可回捞。
    """
    cfg = cfg or _load_config("t2v")
    if cfg.name != "ark":
        raise ProviderError("文生视频未配置方舟服务（AVS_T2V_PROVIDER 应为 ark）")
    base = _ark_base(cfg)
    # 档位按**短边**判定（契约层单一来源）：此前按 height 判，竖屏 720p
    # （720×1280）被误判成 1080p 档提交——多付一档成本且档位非预期
    resolution = resolution_tier(width, height)
    text = (f"{prompt} --resolution {resolution} --duration {max(5, min(12, duration))}"
            " --watermark false")
    content: list[dict] = [{"type": "text", "text": text}]
    if first_frame_path:
        content.append({"type": "image_url", "role": "first_frame",
                        # 首帧不压缩：它就是视频开场画面本身，压缩直接劣化成片
                        "image_url": {"url": _file_data_url(first_frame_path,
                                                            compress=False)}})
    else:
        for p in (ref_paths or [])[:4]:
            content.append({"type": "image_url", "role": "reference_image",
                            "image_url": {"url": _file_data_url(p,
                                                                compress=None)}})
    payload = {"model": model_id or cfg.model or _ARK_CURATED_MODELS["t2v"][0],
               "content": content}
    try:
        data = _post_json(f"{base}/contents/generations/tasks", payload,
                          _auth_headers(cfg), 60, "方舟文生视频提交")
    except ProviderError as e:
        # 内容安全友好化（终验实测）：写实 AI 人脸首帧会被火山系平台误判
        # 「可能含真人」——错误转中文指引，而非让用户面对裸 HTTP 响应
        raise ProviderError(_humanize_content_safe(str(e))) from e
    task_id = data.get("id")
    if not task_id:
        raise ProviderError(f"方舟视频任务响应缺少 id：{str(data)[:200]}")
    return str(task_id)


def t2v_ark_fetch(cfg: ProviderConfig, provider_task_id: str,
                  cancel_check: Callable[[], bool] | None = None,
                  on_tick: Callable[[float, str], None] | None = None,
                  duration: int = 0) -> dict:
    """方舟视频【轮询段】：按 provider task_id 取回产物（可重入）。

    on_tick 由 runner 传 ctx.publish_progress：心跳既给用户看进度（云端接口
    没有百分比，按等待时长估算、标签写明参考耗时），也保任务不被看门狗判卡死。
    """
    return _poll_ark_video_task(cfg, _ark_base(cfg), provider_task_id,
                                cancel_check, on_tick=on_tick, duration=duration)


def _poll_ark_video_task(cfg: ProviderConfig, base: str, task_id: str,
                         cancel_check: Callable[[], bool] | None,
                         on_tick: Callable[[float, str], None] | None = None,
                         duration: int = 0) -> dict:
    """轮询方舟视频生成任务直到终态（间隔 5 秒，超时 15 分钟）。"""
    url = f"{base}/contents/generations/tasks/{task_id}"
    started = time.monotonic()
    deadline = started + T2V_POLL_TIMEOUT_SEC
    last_tick = 0.0
    _status = ""
    while time.monotonic() < deadline:
        if cancel_check is not None and cancel_check():
            raise ProviderError(f"方舟视频任务已取消（{task_id}）")
        time.sleep(T2V_POLL_INTERVAL_SEC)
        data = _poll_get_json(url, _auth_headers(cfg), "方舟视频任务")
        _status = str(data.get("status", "")).lower()
        if _status == "succeeded":
            video_url = ((data.get("content") or {}).get("video_url"))
            if not video_url:
                raise ProviderError(f"方舟视频任务完成但缺少 video_url（{task_id}）")
            return {"video_url": video_url}
        if _status in ("failed", "cancelled", "canceled"):
            error = data.get("error") or {}
            # 内容安全误杀在轮询阶段才浮现（提交成功后判定）——同样给中文指引
            raise ProviderError(_humanize_content_safe(
                f"方舟视频任务失败（{task_id}）：{error.get('message') or _status}"))
        # 心跳（与 comfyui 同款估算口径）：用户看得见进度、看门狗有活信号
        elapsed = time.monotonic() - started
        if on_tick is not None and elapsed - last_tick >= _T2V_HEARTBEAT_INTERVAL_SEC:
            last_tick = elapsed
            on_tick(*_video_poll_tick(elapsed, duration, _status))
    raise ProviderError(
        f"方舟视频任务超时未完成（{task_id}，上限 {T2V_POLL_TIMEOUT_SEC // 60} 分钟）")


# ---------- Agnes AI（OpenAI 兼容网关：文本 / 图像 / 视频）----------

# 精选模型列表（/models 列表接口不可用时的回退，均为官方文档模型 ID；
# 已弃用的 agnes-2.0-flash / agnes-2.5-pro-alpha 不收录）
_AGNES_CURATED_MODELS = {
    "llm": (
        "agnes-2.5-flash",
        "agnes-2.5-pro",
        "agnes-2.5-pro-beta",
    ),
    "t2i": (
        "agnes-image-2.5-flash",
        "agnes-image-2.1-flash",
        "agnes-image-2.0-flash",
    ),
    "t2v": (
        "agnes-video-2.5-flash",
        "agnes-video-2.5",
    ),
    "i2v": (
        "agnes-video-2.5-flash",
        "agnes-video-2.5",
    ),
}

# Agnes 视频输出宽高比档（就近映射）
_AGNES_VIDEO_ASPECTS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")

# Agnes 图像输出宽高比档（就近映射）
_AGNES_IMAGE_ASPECTS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")

# Agnes 图像分辨率档位（按目标面积就近选择，对齐官方 1K-4K 档）
_AGNES_IMAGE_TIERS = ((1_500_000, "1K"), (4_500_000, "2K"), (10_000_000, "3K"))

def _agnes_ratio(width: int, height: int, aspects: tuple[str, ...]) -> str:
    """宽高比就近映射到 Agnes 支持的 ratio 档。"""
    target = width / max(1, height)

    def distance(r: str) -> float:
        rw, rh = r.split(":")
        return abs(int(rw) / int(rh) - target)

    return min(aspects, key=distance)


def _agnes_video_base(cfg: ProviderConfig) -> str:
    """Agnes base_url 校验（默认官方地址，兼容已带 /v1 的填写）。"""
    if not cfg.base_url:
        raise ProviderError(
            "Agnes base_url 未配置（如 https://apihub.agnes-ai.com/v1）")
    return cfg.base_url.rstrip("/")


def _agnes_poll_url(cfg: ProviderConfig, video_id: str, model_id: str) -> str:
    """Agnes 视频任务轮询地址（agnesapi 挂在主机根，不带 /v1 前缀）。"""
    root = cfg.base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    query = urlencode({"video_id": video_id, "model_name": model_id})
    return f"{root}/agnesapi?{query}"


def _shrink_ref_payload(payload: dict) -> None:
    """把 payload 里的参考图整体再压一档（长边 768 / 质量 75），就地生效。

    仅用于连接级失败后的重试：端点对 payload 大小敏感，压一档往往即可通过；
    图像信息量对上送参考足够（参考图只作身份/陈设锚点）。

    **只改本次 payload，不碰模块全局**：旧实现把全局 REF_UPLOAD_MAX_EDGE 改成
    768 且永不恢复——一次断连会让该进程**后续所有任务**都用低档参考图上传
    （静默的质量劣化，且无任何记录）。压缩档位一律显式传参。
    """
    imgs = (payload.get("extra_body") or {}).get("image")
    if not imgs:
        return
    import io

    from PIL import Image

    shrunk: list[str] = []
    for url in imgs:
        try:
            b64 = url.split(",", 1)[1]
            with Image.open(io.BytesIO(base64.b64decode(b64))) as img:
                img = img.convert("RGB")
                w, h = img.size
                scale = REF_SHRINK_MAX_EDGE / max(w, h)
                if scale < 1:
                    img = img.resize((max(1, int(w * scale)),
                                      max(1, int(h * scale))), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=REF_SHRINK_JPEG_QUALITY)
            shrunk.append("data:image/jpeg;base64,"
                          + base64.b64encode(buf.getvalue()).decode())
        except Exception:  # noqa: BLE001 单张失败保留原图
            shrunk.append(url)
    payload["extra_body"]["image"] = shrunk


def t2i_agnes(prompt: str, width: int = 1024, height: int = 1024,
              count: int = 1, model_id: str = "",
              ref_paths: list[str] | None = None,
              seed: int | None = None,
              cfg: ProviderConfig | None = None) -> list[bytes]:
    """Agnes 文生图 / 参考图生图（images/generations），返回图片字节列表。

    官方契约要点：response_format 必须放 extra_body（顶层会 400）；
    size 用档位（1K-4K）配合 ratio；参考图走 extra_body.image（支持
    base64 data URL）；参考图生图无需 tags。count > 1 时逐次调用；
    cfg 缺省读全局 t2i 配置（编码串任务显式传入）。
    """
    cfg = cfg or _load_config("t2i")
    if cfg.name != "agnes":
        raise ProviderError("文生图未配置 Agnes 服务（AVS_T2I_PROVIDER 应为 agnes）")
    base = _agnes_video_base(cfg)
    tier = "1K"
    for floor, name in _AGNES_IMAGE_TIERS:
        if width * height >= floor:
            tier = name
    payload: dict = {
        "model": model_id or cfg.model or _AGNES_CURATED_MODELS["t2i"][0],
        "prompt": prompt,
        "size": tier,
        "ratio": _agnes_ratio(width, height, _AGNES_IMAGE_ASPECTS),
        "extra_body": {"response_format": "url"},
    }
    # 固定种子（实测生效、可复现；域 [-1,999] 由调用方经 resolve_seed 钳好）
    if seed is not None:
        payload["seed"] = int(seed)
    if ref_paths:
        # 参考图生图：本地图压缩后转 base64 data URL 上送（角色一致性生成）
        # 压缩是可靠性修复：实测未压缩时 5 张 PNG ≈3MB，端点直接断连（§36）
        payload["extra_body"]["image"] = [
            _file_data_url(p, compress=None)
            for p in ref_paths[:_T2I_CAPABILITIES["agnes"].max_ref_images]]
    images: list[bytes] = []
    for _ in range(max(1, count)):
        # 连接级错误重试（见 docs/design/15 §36）：端点在大 payload 下会直接断连
        # （Server disconnected），属瞬时/可重试错误——重试前再压一档参考图。
        # 旧行为把这类错误当普通失败上交，调用方随即静默回退本地推理（当时
        # 单图 5+ 小时）。回退已在 2026-09-23 移除（云端与本地互不相干），
        # 但连接级重试仍保留：一次 40 秒的远端调用不该因瞬时断连就报废。
        data = None
        for attempt in range(2):
            try:
                data = _post_json(_images_generations_url(base), payload,
                                  _auth_headers(cfg), T2I_TIMEOUT, "Agnes 文生图")
                break
            except ProviderError as e:
                msg = str(e)
                transient = ("disconnected" in msg.lower()
                             or "超时" in msg or "连接失败" in msg)
                if not transient or attempt == 1:
                    raise
                logger.warning("Agnes 文生图连接级失败，压缩参考图后重试：%s",
                               msg[:120])
                _shrink_ref_payload(payload)
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json")
            if b64:
                images.append(base64.b64decode(b64))
            elif item.get("url"):
                images.append(_download_image(str(item["url"])))
    if not images:
        raise ProviderError("Agnes 文生图未返回图片数据")
    return images


def t2v_agnes(prompt: str, duration: int = 5, width: int = 1280,
              height: int = 720, model_id: str = "",
              first_frame_path: str | None = None,
              ref_paths: list[str] | None = None,
              seed: int | None = None,
              cancel_check: Callable[[], bool] | None = None,
              cfg: ProviderConfig | None = None) -> dict:
    """Agnes 视频生成（POST /videos 异步任务 + GET /agnesapi 轮询）。

    模式映射：无图像输入 → text（文生视频）；仅首帧 → keyframe（保真首帧）；
    带参考图 → reference（首帧并入 images[0]，prompt 注入 <Picture N> 引用
    引导）。flash 模型仅支持 720P；duration 钳位 [4, 12] 秒（官方区间）。
    返回 {"video_url": ...}（契约与 ark/custom 一致）；
    cfg 缺省读全局 t2v 配置（编码串任务显式传入）。
    """
    cfg = cfg or _load_config("t2v")
    if cfg.name != "agnes":
        raise ProviderError("文生视频未配置 Agnes 服务（AVS_T2V_PROVIDER 应为 agnes）")
    base = _agnes_video_base(cfg)
    model = model_id or cfg.model or _AGNES_CURATED_MODELS["t2v"][0]
    # payload 组装复用 _build_agnes_payload（与 t2v_agnes_submit 同源，
    # 避免参考图预算/编号对齐逻辑两处漂移）
    payload = _build_agnes_payload(prompt, duration, width, height, model,
                                   first_frame_path, ref_paths, seed)
    data = _post_json(f"{_api_url(base, '/videos')}", payload,
                      _auth_headers(cfg), 60, "Agnes 视频提交")
    video_id = str(data.get("video_id") or data.get("id") or "")
    if not video_id:
        raise ProviderError(f"Agnes 视频任务响应缺少 video_id：{str(data)[:200]}")
    return _poll_agnes_video_task(cfg, video_id, model, cancel_check)


def t2v_agnes_submit(prompt: str, duration: int = 5, width: int = 1280,
                     height: int = 720, model_id: str = "",
                     first_frame_path: str | None = None,
                     ref_paths: list[str] | None = None,
                     seed: int | None = None,
                     cfg: ProviderConfig | None = None) -> tuple[str, str]:
    """Agnes 视频【提交段】：返回 (provider video_id, 实际使用的 model)。

    与 t2v_agnes 拆开的原因同 t2v_ark_submit：agnes 是异步生成本身失败（含
    内容安全），提交成功即计费——轮询/下载失败必须能与「未提交」区分。
    """
    import copy as _copy

    cfg = cfg or _load_config("t2v")
    if cfg.name != "agnes":
        raise ProviderError("文生视频未配置 Agnes 服务（AVS_T2V_PROVIDER 应为 agnes）")
    base = _agnes_video_base(cfg)
    model = model_id or cfg.model or _AGNES_CURATED_MODELS["t2v"][0]
    built = _build_agnes_payload(prompt, duration, width, height, model,
                                 first_frame_path, ref_paths, seed)
    data = _post_json(f"{_api_url(base, '/videos')}", _copy.deepcopy(built),
                      _auth_headers(cfg), 60, "Agnes 视频提交")
    video_id = str(data.get("video_id") or data.get("id") or "")
    if not video_id:
        raise ProviderError(f"Agnes 视频任务响应缺少 video_id：{str(data)[:200]}")
    return video_id, model


def t2v_agnes_fetch(cfg: ProviderConfig, video_id: str, model_id: str = "",
                    cancel_check: Callable[[], bool] | None = None,
                    on_tick: Callable[[float, str], None] | None = None,
                    duration: int = 0) -> dict:
    """Agnes 视频【轮询段】：按 provider video_id 取回产物（可重入）。

    on_tick 由 runner 传 ctx.publish_progress：心跳既给用户看进度，也保任务
    不被看门狗判卡死（云端接口没有百分比，估算口径见 _video_poll_tick）。
    """
    return _poll_agnes_video_task(cfg, video_id, model_id, cancel_check,
                                  on_tick=on_tick, duration=duration)


def _frame_declaration(picture_no: int) -> str:
    """「第 N 张图是本镜头起始画面」声明句（agnes 声明块与 comfyui 占位声明**共用一份**）。

    两家端点的首帧都挂末位（第 <Picture 总张数> 张），编号对不齐会让模型
    认错起始画面——文案只此一处，改动两边同步生效。
    """
    return (f"<Picture {picture_no}> 是本镜头的起始画面，视频必须从该画面"
            "开始并保持人物与场景连贯。")


def _picture_ref_note(ref_total: int, has_first_frame: bool) -> str:
    """「（参考图N）↔ <Picture N>」对应声明（agnes 用；**comfyui 不走这条**）。

    agnes 的提示词里保留「（参考图N）」中文标注，靠这段说明告诉模型编号与
    <Picture N> 的对应。comfyui 端点则不同：H3 要求提示词里**字面写出**
    <Picture N> 占位（N = 图片参数下标 0 起 + 1），由 `comfyui.finalize_prompt`
    在组装时就改写落字（2026-10-10 用户实测纠正），所以不需要这段说明。
    """
    note = ("提示词中的（参考图N）对应 <Picture N>：出场人物的长相、发型与"
            "服装以对应参考图人物为准，场景陈设以场景参考图为准；")
    if has_first_frame:
        note += _frame_declaration(ref_total)
    return note


def _build_agnes_payload(prompt: str, duration: int, width: int, height: int,
                         model: str, first_frame_path: str | None,
                         ref_paths: list[str] | None,
                         seed: int | None = None) -> dict:
    """组装 agnes 视频 payload（模式判定 + 参考图编号对齐），供 submit 复用。"""
    is_flash = "flash" in model.lower()
    max_refs = _AGNES_REF_IMAGES_FLASH if is_flash else _AGNES_REF_IMAGES_PRO
    refs_budget = max_refs - (1 if first_frame_path else 0)
    refs = list(ref_paths or [])[:max(0, refs_budget)]
    # 档位同样按短边（契约层）；agnes 未声明 480P 档，低档归并到 720P
    # （保持既有行为，不引入未经确认的档位）
    size = "720P" if is_flash else (
        "1080P" if resolution_tier(width, height) == "1080p" else "720P")
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "seconds": str(max(4, min(12, duration))),
        "mode": "text",
        "size": size,
        "aspect_ratio": _agnes_ratio(width, height, _AGNES_VIDEO_ASPECTS),
    }
    # 固定种子（同 t2i：实测生效、可复现；域由 resolve_seed 钳好）
    if seed is not None:
        payload["seed"] = int(seed)
    if refs:
        payload["mode"] = "reference"
        # 参考图：按大小触发压缩
        payload["images"] = [_file_data_url(p, compress=None) for p in refs]
        if first_frame_path and first_frame_path not in refs:
            payload["images"].append(
                _file_data_url(first_frame_path, compress=False))
        payload["prompt"] = (_picture_ref_note(len(payload["images"]),
                                               bool(first_frame_path)) + prompt)
    elif first_frame_path:
        payload["mode"] = "keyframe"
        payload["first_frame"] = _file_data_url(first_frame_path,
                                                compress=False)
    return payload


def _poll_agnes_video_task(cfg: ProviderConfig, video_id: str, model_id: str,
                           cancel_check: Callable[[], bool] | None,
                           on_tick: Callable[[float, str], None] | None = None,
                           duration: int = 0) -> dict:
    """轮询 Agnes 视频任务直到 completed/failed（间隔 5 秒，超时 15 分钟）。

    状态实测集合：pending / queued / in_progress / completed / failed
    （pending 为官方文档未列的实测值，归入继续等待）。on_tick 心跳见
    `_video_poll_tick`（云端无进度接口，按等待时长估算、标签写明参考耗时）。
    """
    url = _agnes_poll_url(cfg, video_id, model_id)
    started = time.monotonic()
    deadline = started + T2V_POLL_TIMEOUT_SEC
    last_tick = 0.0
    _status = ""
    while time.monotonic() < deadline:
        if cancel_check is not None and cancel_check():
            raise ProviderError(f"Agnes 视频任务已取消（{video_id}）")
        time.sleep(T2V_POLL_INTERVAL_SEC)
        data = _poll_get_json(url, _auth_headers(cfg), "Agnes 视频任务")
        _status = str(data.get("status", "")).lower()
        if _status == "completed":
            # 实测双端点响应形态不同：agnesapi 结果在顶层 url，
            # /v1/videos 在 metadata.url（文档口径）——两处都取
            video_url = ((data.get("metadata") or {}).get("url")) or data.get("url")
            if not video_url:
                raise ProviderError(
                    f"Agnes 视频任务完成但缺少产物 URL（{video_id}）")
            return {"video_url": video_url}
        if _status in ("failed", "cancelled", "canceled"):
            error = data.get("error") or {}
            # agnes 是异步生成本身失败（含内容安全）——轮询阶段的中文指引
            raise ProviderError(_humanize_content_safe(
                f"Agnes 视频任务失败（{video_id}）：{error.get('message') or _status}"))
        # 心跳（与 comfyui 同款估算口径）：用户看得见进度、看门狗有活信号
        elapsed = time.monotonic() - started
        if on_tick is not None and elapsed - last_tick >= _T2V_HEARTBEAT_INTERVAL_SEC:
            last_tick = elapsed
            on_tick(*_video_poll_tick(elapsed, duration, _status))
    raise ProviderError(
        f"Agnes 视频任务超时未完成（{video_id}，上限 {T2V_POLL_TIMEOUT_SEC // 60} 分钟）")


def _filter_models_for_kind(provider: str, kind: str, ids: list[str]) -> list[str]:
    """按任务类别过滤远端模型列表（各协议的官方模型命名前缀）。"""
    if provider == "agnes":
        # Agnes：按任务类别过滤官方模型（agnes-image-* / agnes-video-* / agnes-2.5*）
        prefixes = {"t2i": "agnes-image-", "t2v": "agnes-video-",
                    "i2v": "agnes-video-", "llm": "agnes-"}
        prefix = prefixes.get(kind)
        return [i for i in ids if prefix and i.lower().startswith(prefix)]
    if kind == "t2i":
        return [i for i in ids if "seedream" in i.lower()]
    if kind == "t2v":
        return [i for i in ids if "seedance" in i.lower()]
    return ids


def probe_provider_models(provider: str, kind: str, base_url: str,
                          api_key: str) -> dict:
    """探测指定服务端点的远端模型列表（模型管理表单/前端下拉共用）。

    优先请求 OpenAI 兼容 GET {base}/models（按任务类别过滤）；失败或为空时
    回退精选官方模型表（仅 ark/agnes）。纯只读探测，不落任何状态。
    """
    if not base_url:
        return {"models": [], "source": "none"}
    if provider == "comfyui":
        # 本地 ComfyUI 没有 OpenAI 兼容 /models：探测服务连通性（/system_stats）
        # 并列出**内置工作流模板**——管理页的「模型」字段就填模板名
        return comfyui.probe(base_url)
    if provider == "vidu":
        # Vidu 无公开的模型列表接口（官方只在文档里枚举模型名）：直接给精选表。
        # **不做无鉴权的连通性探测**——本端点每次调用都计费，且没有只读探测接口
        return {"models": [{"id": i, "display_name": i}
                           for i in _VIDU_CURATED_MODELS.get(kind, ())],
                "source": "curated"}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    models: list[dict] = []
    source = "none"
    try:
        resp = httpx.get(_api_url(base_url.rstrip("/"), "/models"),
                         headers=headers, timeout=10)
        if resp.status_code < 400:
            ids = [str(item.get("id")) for item in (resp.json().get("data") or [])
                   if isinstance(item, dict) and item.get("id")]
            models = [{"id": i, "display_name": i}
                      for i in _filter_models_for_kind(provider, kind, ids)]
            source = provider
    except (httpx.HTTPError, ValueError):
        pass  # 列表接口不可用：回退精选表
    if not models and provider == "ark":
        models = [{"id": i, "display_name": i} for i in _ARK_CURATED_MODELS.get(kind, ())]
        source = "curated"
    if not models and provider == "agnes":
        models = [{"id": i, "display_name": i} for i in _AGNES_CURATED_MODELS.get(kind, ())]
        source = "curated"
    return {"models": models, "source": source}


def list_provider_models(kind: str) -> dict:
    """拉取当前配置下指定任务类别的远端模型列表（兼容接口）。

    委托 probe_provider_models（配置经 _load_config：DB 默认端点优先）。
    仅返回列表数据，不做可用性校验。
    """
    cfg = _load_config(kind)
    if cfg.is_local or not cfg.base_url:
        return {"models": [], "source": "none"}
    return probe_provider_models(cfg.name, kind, cfg.base_url, cfg.api_key)




def _poll_video_task(cfg: ProviderConfig, task_id: str,
                     cancel_check: Callable[[], bool] | None) -> dict:
    """轮询 GET {base_url}/videos/{task_id} 直到 done，间隔 5 秒，超时 15 分钟。"""
    url = f"{cfg.base_url}/videos/{task_id}"
    deadline = time.monotonic() + T2V_POLL_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if cancel_check is not None and cancel_check():
            raise ProviderError(f"文生视频任务已取消（{task_id}）")
        time.sleep(T2V_POLL_INTERVAL_SEC)
        data = _poll_get_json(url, _auth_headers(cfg), "文生视频状态")
        status = str(data.get("status", "")).lower()
        if status in ("done", "succeeded", "success", "completed"):
            if not data.get("video_url"):
                raise ProviderError(f"文生视频任务完成但缺少 video_url（{task_id}）")
            return data
        if status in ("failed", "error", "canceled", "cancelled"):
            raise ProviderError(
                f"文生视频任务失败（{task_id}）：{data.get('error') or status}")
    raise ProviderError(
        f"文生视频任务超时未完成（{task_id}，上限 {T2V_POLL_TIMEOUT_SEC // 60} 分钟）")


# ---------- 文生视频（custom 自定义端点）----------

def t2v_custom(prompt: str, duration: int = 5, width: int = 1280,
               height: int = 720, input_refs: list | None = None,
               cancel_check: Callable[[], bool] | None = None,
               cfg: ProviderConfig | None = None) -> dict:
    """调用自定义视频端点（custom 提供方契约），返回含 video_url 的字典。

    POST {base_url}/videos 提交（入参 prompt/duration/width/height，可选 model）：
    - 直接返回 {video_url}：同步出片，原样返回
    - 返回 {task_id}：轮询 GET {base_url}/videos/{task_id} 直到
      {status: done, video_url}（超时 15 分钟，间隔 5 秒）
    cancel_check 返回 True 时中止轮询（任务取消）；
    cfg 缺省读全局 t2v 配置（编码串任务显式传入）。
    """
    cfg = cfg or _load_config("t2v")
    if cfg.name != "custom":
        raise ProviderError("文生视频未配置自定义端点（AVS_T2V_PROVIDER 应为 custom）")
    if not cfg.base_url:
        raise ProviderError("文生视频 base_url 未配置（AVS_T2V_BASE_URL）")
    payload: dict = {
        "prompt": prompt,
        "duration": duration,
        "width": width,
        "height": height,
    }
    if cfg.model:
        payload["model"] = cfg.model
    if input_refs:
        payload["input_refs"] = input_refs
    data = _post_json(
        f"{cfg.base_url}/videos", payload,
        _auth_headers(cfg), 60, "文生视频提交",
    )
    if data.get("video_url"):
        return data
    task_id = data.get("task_id")
    if not task_id:
        raise ProviderError("文生视频响应缺少 video_url/task_id")
    return _poll_video_task(cfg, str(task_id), cancel_check)


def download_video(video_url: str, timeout: float = 600.0) -> bytes:
    """下载第三方视频产物为字节（runner 保存为 mp4 产物）。"""
    try:
        resp = httpx.get(video_url, timeout=timeout)
    except httpx.TimeoutException as e:
        raise ProviderError(f"视频下载超时（{video_url}）") from e
    except httpx.HTTPError as e:
        raise ProviderError(f"视频下载失败（{video_url}）：{e}") from e
    if resp.status_code >= 400:
        raise ProviderError(f"视频下载失败（HTTP {resp.status_code}）：{video_url}")
    return resp.content


# ---------- 文生视频（comfyui 本地服务）----------

def _comfyui_model(cfg: ProviderConfig, model_id: str) -> str:
    """本次使用的工作流模板名：任务编码串 > 端点默认模型 > 内置默认模板。"""
    return (model_id or cfg.model or comfyui.DEFAULT_WORKFLOW_ID).strip()


def t2v_comfyui_submit(prompt: str, duration: int = 5, width: int = 1280,
                       height: int = 720, model_id: str = "",
                       first_frame_path: str | None = None,
                       ref_paths: list[str] | None = None,
                       seed: int | None = None,
                       run_tag: str = "",
                       cfg: ProviderConfig | None = None) -> str:
    """ComfyUI 本地出片【提交段】：返回 ComfyUI prompt_id（远端任务号）。

    与 t2v_ark_submit 拆开的原因同源：提交成功即 GPU 已开始出片，取回失败
    必须能与「未提交」区分——未提交可自动重试（连不上归 NETWORK），已提交
    不得重提（白烧一次十几分钟的出片，且产物已在远端输出目录产生）。

    参考图挂载顺序（参考图在前、首帧挂末位）下，图片参数下标从 0 起、
    **提示词占位 = 下标 + 1** 写成字面 `<Picture N>`（H3/Qwen3-VL 的硬要求，
    2026-10-10 用户实测纠正）；改写由 `comfyui.finalize_prompt` 完成，且提示词
    组装侧（drama）已用同一函数改过一遍——两边幂等同源，**参数弹窗与调用日志
    的提示词逐字一致**。宽高须已过 `comfyui.plan_size`（32 的倍数 + 1.0MP 封顶）：
    由调用方算好并把吸附/缩回的说明外露给用户——参数被系统改过必须看得见。
    """
    cfg = cfg or _load_config("t2v")
    if cfg.name != "comfyui":
        raise ProviderError("文生视频未配置 ComfyUI 服务（端点协议应为 comfyui）")
    if not cfg.base_url:
        raise ProviderError("ComfyUI 服务地址未配置（端点 base_url 为空）")
    ff = str(first_frame_path or "").strip()
    refs = [str(p) for p in (ref_paths or []) if str(p).strip()]
    if ff and ff not in refs:
        refs.append(ff)  # 首帧挂末位（编号 = 总张数，起始画面声明句指的就是它）
    # 图片为空时**不做占位改写**：提示词里若有「参考图N」字样，改写会造出指向
    # 不存在图片的悬空 <Picture N>（模型面对不存在的图号会自由发挥，串脸成因之一）
    full_prompt = prompt
    if refs:
        full_prompt = comfyui.finalize_prompt(prompt, picture_total=len(refs),
                                              has_first_frame=bool(ff))
    try:
        return comfyui.submit_video(
            cfg.base_url, _comfyui_model(cfg, model_id), prompt=full_prompt,
            width=int(width), height=int(height), duration=int(duration),
            image_paths=refs, seed=seed, run_tag=run_tag)
    except comfyui.ComfyWorkflowError as e:
        # 模板缺文件/缺节点/名字非法：确定性错误，不自动重试（重试也是同一个错）
        raise ProviderError(str(e), FailClass.PARAM) from e


def t2v_comfyui_fetch(cfg: ProviderConfig, prompt_id: str, *, duration: int = 0,
                      cancel_check: Callable[[], bool] | None = None,
                      on_tick: Callable[[float, str], None] | None = None) -> dict:
    """ComfyUI 本地出片【轮询段】：按 prompt_id 取回产物（可重入）。

    on_tick 由 runner 传 `ctx.publish_progress`：心跳既给用户看进度，也是
    任务看门狗的保命线——本地长片十几分钟属正常，20 分钟无进度会被判卡死。
    """
    if cfg.name != "comfyui":
        raise ProviderError("文生视频未配置 ComfyUI 服务（端点协议应为 comfyui）")
    if not prompt_id:
        raise ProviderError("ComfyUI 取回缺少任务号（prompt_id）")
    return comfyui.fetch_result(cfg.base_url, prompt_id, duration=int(duration),
                                cancel_check=cancel_check, on_tick=on_tick)


# ---------- 文生视频（vidu 图生视频端点，2026-10-14 接入）----------

# Vidu ent/v2 契约：base_url 填**主机根**（如 https://api.vidu.cn），提交与轮询
# 都在 /ent/v2 前缀下（既不是 OpenAI 的 /v1，也不是方舟的 /api/v3）；已带
# /ent/v2 的填写自动剥掉，避免拼出双前缀
_VIDU_API_PREFIX = "/ent/v2"
_VIDU_SUBMIT_PATH = "/ent/v2/img2video"
_VIDU_TASK_PATH = "/ent/v2/tasks/{task_id}/creations"

# 精选模型（Vidu 无公开的模型列表接口，探测直接给精选表）：官方文档当前枚举
# 仅 viduq4-preview（最新旗舰预览版，支持音画同出与自动切镜）
_VIDU_CURATED_MODELS = {
    "t2v": ("viduq4-preview",),
    "i2v": ("viduq4-preview",),
}
_VIDU_DEFAULT_MODEL = "viduq4-preview"

# 分辨率档映射：本仓档位（短边语义）→ Vidu 枚举（540p/720p/1080p/2K/4K）。
# 本仓无 540p 档，480p **就近上取** 540p——下取无档可下，且上取只贵一档、
# 画质不掉（静默改变档位才是本仓忌讳的形态，此映射是显式登记）
_VIDU_RESOLUTION_TIERS = {"480p": "540p", "720p": "720p", "1080p": "1080p"}

# 提交超时（秒）：首帧图**不压缩**转 base64（压缩会劣化成片开场），payload 可达
# 数 MB，上传留足时间（其它端点 60 秒的档位是给小 body 的）
_VIDU_SUBMIT_TIMEOUT_SEC = 120.0

# 提示词扩写（is_rec）：**关**——「系统组装完成的提示词到端点零改写」是本仓
# 纪律（参数弹窗/调用日志与实际上送逐字一致；台词原句不得被端点润色改写）
_VIDU_PROMPT_EXPAND = False
# 音画同出（audio）：**开**（2026-10-14 用户拍板）——台词与音效随画面同步生成
# （官方能力，口型同步对平台合规有意义）；成片自带音轨，如实记入 has_audio
_VIDU_AUDIO = True
# 水印：不加（与方舟/agnes/comfyui 同口径；AI 标注由项目自身的合规环节负责）
_VIDU_WATERMARK = False


def _vidu_base(cfg: ProviderConfig) -> str:
    """Vidu base_url 校验（主机根；已带 /ent/v2 的填写自动剥掉）。"""
    base = (cfg.base_url or "").rstrip("/")
    if not base:
        raise ProviderError("Vidu base_url 未配置（端点服务地址为空，"
                            "如 https://api.vidu.cn）")
    if base.endswith(_VIDU_API_PREFIX):
        base = base[: -len(_VIDU_API_PREFIX)]
    return base


def _vidu_headers(cfg: ProviderConfig) -> dict[str, str]:
    """Vidu 鉴权头：`Token {key}`（**不是 Bearer**，官方契约）。

    无 Key 直接报错而不是发匿名请求：本端点按次计费，匿名请求只会在远端吃
    401，不如在本地把话说清楚。失败类别与远端 401 同档（classify_failure 对
    401/403 归 PARAM——「鉴权失败属配置问题」），两处一致，排查口径不分叉。
    """
    key = (cfg.api_key or "").strip()
    if not key:
        raise ProviderError(
            "Vidu 端点未绑定 API Key：请在「模型管理」为该端点选择密钥",
            FailClass.PARAM)
    return {"Authorization": f"Token {key}"}


def _vidu_resolution(width: int, height: int) -> str:
    """宽高 → Vidu 分辨率枚举（档位按**短边**判定，单一来源 resolution_tier）。"""
    tier = resolution_tier(width, height)
    return _VIDU_RESOLUTION_TIERS.get(tier, _VIDU_RESOLUTION_TIERS["720p"])


def _build_vidu_payload(prompt: str, duration: int, width: int, height: int,
                        model: str, first_frame_path: str) -> dict:
    """组装 Vidu img2video 请求体（提交段与门禁对账共用一份，杜绝两处各写一套）。

    固化项（开关见各常量）：is_rec=False（提示词逐字不改写）、audio=True
    （音画同出）、watermark=False（不加水印）；**不下发 seed**（未实测，见能力表）。
    """
    return {
        "model": model,
        "images": [_file_data_url(first_frame_path, compress=False)],
        "prompt": prompt,
        "is_rec": _VIDU_PROMPT_EXPAND,
        "duration": int(duration),
        "resolution": _vidu_resolution(width, height),
        "audio": _VIDU_AUDIO,
        "watermark": _VIDU_WATERMARK,
    }


def t2v_vidu_submit(prompt: str, duration: int = 5, width: int = 1280,
                    height: int = 720, model_id: str = "",
                    first_frame_path: str | None = None,
                    cfg: ProviderConfig | None = None) -> str:
    """Vidu 图生视频【提交段】：返回 Vidu 任务号 task_id。

    与其它云端端点拆开的原因同源（审计 13 号 L）：提交成功即远端计费，取回
    失败必须能与「未提交」区分——后者可自动重试，前者不得重提（重复计费）。

    首帧图为**必填**（本协议只接「以首帧画面起像」的生成）：没有首帧时
    **明确报错、不发任何请求**——静默改用别的端点或降级出片都是本仓禁止的形态。
    """
    cfg = cfg or _load_config("t2v")
    if cfg.name != "vidu":
        raise ProviderError("文生视频未配置 Vidu 服务（端点协议应为 vidu）")
    image = str(first_frame_path or "").strip()
    if not image:
        raise ProviderError(
            "本端点（Vidu 图生视频）需要首帧图：请先生成/接入该镜首帧再重试"
            "（该端点不支持纯文生视频，换端点或换模型即可）",
            FailClass.PARAM)
    base = _vidu_base(cfg)
    model = model_id or cfg.model or _VIDU_DEFAULT_MODEL
    payload = _build_vidu_payload(prompt, duration, width, height, model, image)
    data = _post_json(f"{base}{_VIDU_SUBMIT_PATH}", payload,
                      _vidu_headers(cfg), _VIDU_SUBMIT_TIMEOUT_SEC,
                      "Vidu 图生视频提交")
    task_id = str(data.get("task_id") or data.get("id") or "")
    if not task_id:
        raise ProviderError(f"Vidu 视频任务响应缺少 task_id：{str(data)[:200]}")
    return task_id


def t2v_vidu_fetch(cfg: ProviderConfig, task_id: str,
                   cancel_check: Callable[[], bool] | None = None,
                   on_tick: Callable[[float, str], None] | None = None,
                   duration: int = 0) -> dict:
    """Vidu 图生视频【轮询段】：按任务号取回产物（可重入）。

    on_tick 由 runner 传 ctx.publish_progress：心跳既给用户看进度（云端接口
    没有百分比，按等待时长估算、标签写明参考耗时），也保任务不被看门狗按
    「20 分钟无进度」判卡死。
    """
    if cfg.name != "vidu":
        raise ProviderError("文生视频未配置 Vidu 服务（端点协议应为 vidu）")
    if not task_id:
        raise ProviderError("Vidu 取回缺少任务号（task_id）")
    return _poll_vidu_video_task(cfg, task_id, cancel_check,
                                 on_tick=on_tick, duration=duration)


def _poll_vidu_video_task(cfg: ProviderConfig, task_id: str,
                          cancel_check: Callable[[], bool] | None,
                          on_tick: Callable[[float, str], None] | None = None,
                          duration: int = 0) -> dict:
    """轮询 Vidu 生成任务直到终态（间隔 5 秒，超时 15 分钟，与其它云端端点同口径）。

    官方状态集合：created / queueing / processing / success / failed；产物取
    `creations[0].url`（**无水印**那条——另有 watermarked_url，与本端点
    watermark=false 的口径一致）。失败时官方只给 err_code（无文案字段），
    原样透出供用户核对官方错误码表，不做任何猜测性翻译。
    """
    url = f"{_vidu_base(cfg)}{_VIDU_TASK_PATH.format(task_id=task_id)}"
    started = time.monotonic()
    deadline = started + T2V_POLL_TIMEOUT_SEC
    last_tick = 0.0
    _status = ""
    while time.monotonic() < deadline:
        if cancel_check is not None and cancel_check():
            raise ProviderError(f"Vidu 视频任务已取消（{task_id}）")
        time.sleep(T2V_POLL_INTERVAL_SEC)
        data = _poll_get_json(url, _vidu_headers(cfg), "Vidu 视频任务")
        _status = str(data.get("state", "")).lower()
        if _status == "success":
            video_url = ""
            for item in (data.get("creations") or []):
                if isinstance(item, dict) and item.get("url"):
                    video_url = str(item["url"])
                    break
            if not video_url:
                raise ProviderError(
                    f"Vidu 视频任务完成但缺少产物 URL（{task_id}）")
            return {"video_url": video_url}
        if _status == "failed":
            err = str(data.get("err_code") or "").strip()
            raise ProviderError(
                f"Vidu 视频任务失败（{task_id}）："
                f"{err or '远端未返回错误码（err_code 为空）'}")
        # 心跳（与其它云端端点同款估算口径）：用户看得见进度、看门狗有活信号
        elapsed = time.monotonic() - started
        if on_tick is not None and elapsed - last_tick >= _T2V_HEARTBEAT_INTERVAL_SEC:
            last_tick = elapsed
            on_tick(*_video_poll_tick(elapsed, duration, _status))
    raise ProviderError(
        f"Vidu 视频任务超时未完成（{task_id}，上限 {T2V_POLL_TIMEOUT_SEC // 60} 分钟）")


# ---------- 语音合成（OpenAI 兼容 audio/speech）----------

def _audio_speech_url(base_url: str) -> str:
    """拼 audio/speech 端点：base_url 已含 /v1 时不再重复拼。"""
    base = base_url.rstrip("/")
    if base.endswith("/audio/speech"):
        return base
    if base.endswith("/v1"):
        return base + "/audio/speech"
    return base + "/v1/audio/speech"


def tts_openai(text: str, voice: str = "alloy",
               cfg: ProviderConfig | None = None) -> bytes:
    """调用 OpenAI 兼容 /v1/audio/speech，返回音频字节（mp3）。

    voice 直接透传（OpenAI 音色名，如 alloy/echo；与 edge-tts 音色名不互通）；
    cfg 缺省读全局 tts 配置。
    """
    cfg = cfg or _load_config("tts")
    if cfg.name != "openai":
        raise ProviderError("TTS 未配置 OpenAI 兼容服务（AVS_TTS_PROVIDER 应为 openai）")
    if not cfg.base_url:
        raise ProviderError("TTS base_url 未配置（AVS_TTS_BASE_URL）")
    payload = {"model": cfg.model or "tts-1", "input": text, "voice": voice}
    url = _audio_speech_url(cfg.base_url)
    _t0 = time.perf_counter()
    try:
        resp = httpx.post(url, json=payload, headers=_auth_headers(cfg), timeout=120)
    except httpx.TimeoutException as e:
        _record_model_call("TTS", url, payload, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=FailClass.TIMEOUT, error=str(e))
        raise ProviderError(f"TTS 请求超时（{url}）", FailClass.TIMEOUT) from e
    except httpx.HTTPError as e:
        _record_model_call("TTS", url, payload, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=FailClass.NETWORK, error=str(e))
        raise ProviderError(f"TTS 服务连接失败（{url}）：{e}",
                            FailClass.NETWORK) from e
    if resp.status_code >= 400:
        # 状态码参与分类（同流式 LLM 那条：只给文案会被兜底判成 unknown，
        # 于是限流既不重试也不算基础设施失败）
        _cls = classify_failure(resp.text[:400], resp.status_code)
        _record_model_call("TTS", url, payload, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=_cls, error=resp.text[:200])
        raise ProviderError(f"TTS 服务返回 HTTP {resp.status_code}：{resp.text[:200]}",
                            _cls)
    if not resp.content:
        _record_model_call("TTS", url, payload, ok=False,
                           elapsed_ms=(time.perf_counter() - _t0) * 1000,
                           fail_class=FailClass.UNKNOWN, error="空音频")
        raise ProviderError("TTS 服务返回空音频")
    _record_model_call("TTS", url, payload, ok=True,
                       elapsed_ms=(time.perf_counter() - _t0) * 1000)
    return resp.content


# ---------- 一次性迁移：.env 五槽位 → DB 端点体系 ----------

def migrate_env_providers() -> None:
    """把 .env 五槽位的非 local 配置导入 DB（provider_keys/provider_endpoints/默认映射）。

    由 init_db 在建表后调用（幂等：端点表非空即跳过）；相同 api_key 值复用同一
    Key 记录。.env 行保留不删，作为 DB 未配置时的回退层。
    """
    from app.core.provider_store import (
        ProviderEndpoint, ProviderKey, Setting,  # noqa: F401 建表依赖已由 create_all 完成
        DEFAULT_ENDPOINTS_KEY, _read_defaults,
    )
    from sqlalchemy import select

    import app.db.session as db_session

    with db_session.SessionLocal() as db:
        existing = db.scalar(select(ProviderEndpoint.id).limit(1))
        if existing:
            return  # 已有端点（含此前迁移）：不重复导入

    key_ids: dict[str, str] = {}  # api_key 明文 → ProviderKey.id（跨 kind 复用）
    defaults: dict[str, str] = {}
    for kind in PROVIDER_KINDS:
        cfg = _load_env_config(kind)
        if cfg.is_local or not cfg.base_url:
            continue
        with db_session.SessionLocal() as db:
            if cfg.api_key:
                if cfg.api_key not in key_ids:
                    key = ProviderKey(name=f"{cfg.name}-{kind}（迁移）", api_key=cfg.api_key)
                    db.add(key)
                    db.flush()
                    key_ids[cfg.api_key] = key.id
                key_id: str | None = key_ids[cfg.api_key]
            else:
                key_id = None
            ep = ProviderEndpoint(
                name=f"{cfg.name}-{kind}（迁移）", kind=kind, provider=cfg.name,
                base_url=cfg.base_url, key_id=key_id, model=cfg.model)
            db.add(ep)
            db.flush()
            defaults[kind] = ep.id
            db.commit()
    if defaults:
        with db_session.SessionLocal() as db:
            merged = {**_read_defaults(db), **defaults}
            db.merge(Setting(key=DEFAULT_ENDPOINTS_KEY,
                             value=json.dumps(merged, ensure_ascii=False)))
            db.commit()
        print(f"[migrate] 已迁移 {len(defaults)} 类服务配置到数据库: "
              f"{', '.join(sorted(defaults))}")
