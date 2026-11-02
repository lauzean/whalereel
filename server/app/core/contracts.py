"""生成参数契约层（借鉴 Jellyfish 的 core/contracts，见 docs/design/15/16）。

**为什么需要这一层**：生成参数此前以裸 dict 在调用方、runner、provider 三处
各自读写（`params.get("aspect")` / `params.get("width", 1280)` 等），口径靠约定
维持——审计与本轮改造中反复出现「同一语义两处定义、默认值不一致」类漂移
（如画幅尺寸表在 runner 与 drama API 各有一份，1:1 一处 960 一处 1024）。
本模块把「一次生成任务的输入」收敛为类型化 DTO 与**唯一一份**常量表：

- 画幅尺寸/分辨率档/命名串：只在此定义，runner 与 API 都从这里取；
- `parse_t2i_input` / `parse_t2v_input`：把裸 dict 归一为契约对象（缺失给默认、
  非法给兜底），runner 只消费契约对象，不再各自 `params.get`；
- 契约是**宽松**的（不是校验门）：归一后永远可用，保证既有调用方零改动兼容。

本层不做语义判断、不调模型——纯粹的结构归一与常量集中。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 唯一一份画幅/分辨率常量表
# ---------------------------------------------------------------------------

# 画幅比例 → 目标宽高（横屏短边=高、竖屏短边=宽，见 drama._video_size）
ASPECT_WH: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "4:3": (1024, 768),
    "1:1": (1024, 1024),      # 生成侧方形基准（视频侧按短边档推导，见 drama）
    "3:4": (768, 1024),
    "9:16": (720, 1280),
    "21:9": (1680, 720),
}
# 自适应：尺寸交给端点按首帧/参考图决定（不强制宽高，走方形基准）
ASPECT_ADAPTIVE = "adaptive"
ADAPTIVE_SIZE = (1024, 1024)

# 默认画幅（未指定时）
DEFAULT_ASPECT = "16:9"

# 分辨率档 → 短边像素（480p/720p/1080p；无该档时按高度就近）
RESOLUTION_SHORT_EDGE: dict[str, int] = {"480p": 480, "720p": 720, "1080p": 1080}
DEFAULT_RESOLUTION = "720p"

# 档位判定阈值（按**短边**）：≥1000→1080p、≥700→720p、否则 480p
_TIER_FLOORS: tuple[tuple[int, str], ...] = ((1000, "1080p"), (700, "720p"), (0, "480p"))


def resolution_tier(width: int, height: int) -> str:
    """宽高 → 分辨率档（**按短边**判定）。

    为什么必须是短边：档位语义就是「短边像素」（见 `api/drama._video_size` 的
    推导与注释）。按**高度**判会把竖屏 720p（720×1280）判成 1080p 档——实测
    端点侧（ark / agnes）正是这么判的，于是 9:16 @720p 的请求实际以 1080p
    提交：多付一档成本、拿到的还不是预期档位。推导侧与端点侧现在共用本函数，
    结构上不可能再分叉（这是契约层存在的意义，与画幅表同源同理）。
    """
    short = min(int(width or 0), int(height or 0))
    for floor, tier in _TIER_FLOORS:
        if short >= floor:
            return tier
    return _TIER_FLOORS[-1][1]

# 时长区间（秒）：全链路统一口径（解析侧 SHOT_DURATION_* 同源）
DURATION_MIN = 4
DURATION_MAX = 15
DURATION_DEFAULT = 5

# 单请求出图张数上限（防误传超大值打爆端点配额）
COUNT_MAX = 4


def aspect_size(aspect: str) -> tuple[int, int]:
    """画幅 → 目标宽高（未知/空走默认；adaptive 给方形基准）。"""
    a = (aspect or "").strip()
    if a == ASPECT_ADAPTIVE:
        return ADAPTIVE_SIZE
    return ASPECT_WH.get(a, ASPECT_WH[DEFAULT_ASPECT])


# ---------------------------------------------------------------------------
# 输入契约（宽松归一：缺失给默认、非法给兜底，永不抛错）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class T2IInput:
    """文生图任务输入契约。"""

    prompt: str
    aspect: str
    width: int
    height: int
    count: int
    seed: int | None
    negative_prompt: str


@dataclass(frozen=True)
class T2VInput:
    """文生视频/参考图生视频任务输入契约。"""

    prompt: str
    duration: int
    aspect: str
    width: int
    height: int
    resolution: str
    seed: int | None


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_t2i_input(params: dict | None, input_refs: dict | None = None) -> T2IInput:
    """裸 dict → T2I 契约（宽高可由 aspect 推导；显式 width/height 优先）。"""
    p = dict(params or {})
    refs = dict(input_refs or {})
    aspect = str(p.get("aspect") or DEFAULT_ASPECT)
    dw, dh = aspect_size(aspect)
    width = _int_or(p.get("width"), 0) or dw
    height = _int_or(p.get("height"), 0) or dh
    seed_raw = p.get("seed")
    return T2IInput(
        prompt=str(p.get("prompt") or refs.get("text") or ""),
        aspect=aspect,
        width=max(64, width),
        height=max(64, height),
        count=max(1, min(COUNT_MAX, _int_or(p.get("count"), 1))),
        seed=_int_or(seed_raw, -1) if seed_raw not in (None, "") else None,
        negative_prompt=str(p.get("negative_prompt") or ""),
    )


def parse_t2v_input(params: dict | None, input_refs: dict | None = None) -> T2VInput:
    """裸 dict → T2V 契约（时长钳位到全链路口径；宽高缺省按画幅推导）。"""
    p = dict(params or {})
    refs = dict(input_refs or {})
    aspect = str(p.get("aspect") or DEFAULT_ASPECT)
    dw, dh = aspect_size(aspect)
    width = _int_or(p.get("width"), 0) or dw
    height = _int_or(p.get("height"), 0) or dh
    seed_raw = p.get("seed")
    return T2VInput(
        prompt=str(p.get("prompt") or refs.get("text") or ""),
        duration=max(DURATION_MIN,
                     min(DURATION_MAX, _int_or(p.get("duration"), DURATION_DEFAULT))),
        aspect=aspect,
        width=max(64, width),
        height=max(64, height),
        resolution=str(p.get("resolution") or DEFAULT_RESOLUTION),
        seed=_int_or(seed_raw, -1) if seed_raw not in (None, "") else None,
    )
