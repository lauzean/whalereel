"""HiDream-O1-Image 运行时胶水层。

HiDream-O1 是基于 Qwen3-VL 改造的 Pixel-DiT（UiT）：无 VAE、无独立文本编码器，
官方推理不走 diffusers 管线，而是 transformers 加载模型 + 官方仓库自带 pipeline
（同目录 pipeline.py）采样。本模块提供：

- HiDreamO1Bundle：主模型 + processor 轻量封装，暴露 .to/.components，
  复用资源池的驻留、显存估算与落位调优（apply_performance）。
- hidream_o1_loader：流式加载器（磁盘→显存直达，免 RAM 常驻权重副本）。

权重以 fp32 存储（约 35GB），device_map="cuda" 流式加载：逐分片读盘、
逐张量转 bf16 直上显存（约 16GB），内存峰值仅单分片（~5GB），无 RAM 常驻副本；
显存预检不过则不启动加载（明确报错）。
版本约束说明：官方钉版 transformers==4.57.1，但其 huggingface-hub<1.0 约束与
diffusers 0.40（要求 hub>=1.23）不可共存；当前环境 transformers 5.x 下运行，
vendored 代码含本地 5.x 兼容补丁（qwen3_vl_transformers.py 内标注「本地补丁」）：
- _default_rope_init：ROPE_INIT_FUNCTIONS 已移除 "default" 键，默认 RoPE 内联
  标准逆频率公式（theta 从 rope_parameters/rope_scaling 字典读取）。
- compute_default_rope_parameters：5.x 权重初始化钩子按此方法名重置 inv_freq，
  4.57 类缺此方法（device_map 真实构造路径触发 AttributeError）。
- pipeline use_flash_attn=False：Windows 无 flash-attn 官方 wheel，改走模型
  自带的标准 4D attention mask 路径（数学等价，SDPA 计算，速度略慢）。
auto_docstring 的 [ERROR] 输出为文档工具链噪声，非运行时错误。
若后续版本再遇 5.x 不兼容点，沿同一模式补丁；勿单独降级 transformers
（会连带降 hub 破坏 diffusers）。
"""

import logging
from pathlib import Path

from app.inference.pool import Loader, PipelineLike

logger = logging.getLogger(__name__)

# 官方 add_special_tokens（inference.py）：采样管线依赖的快捷 token 属性
_SPECIAL_TOKEN_ATTRS = (
    ("boi_token", "<|boi_token|>"),
    ("bor_token", "<|bor_token|>"),
    ("eor_token", "<|eor_token|>"),
    ("bot_token", "<|bot_token|>"),
    ("tms_token", "<|tms_token|>"),
)


class HiDreamO1Bundle:
    """HiDream-O1 主模型 + processor 封装（复用资源池驻留与显存调优接口）。

    components 属性以 {"model": ...} 形式暴露主模型，使资源池的权重体积
    估算（_pipe_weight_mb）与普通 diffusers 管线同构。
    """

    def __init__(self, model, processor) -> None:
        self.model = model
        self.processor = processor

    @property
    def components(self) -> dict:
        """组件字典（仅主模型），供资源池权重体积估算。"""
        return {"model": self.model}

    def to(self, *args, **kwargs) -> "HiDreamO1Bundle":
        """设备迁移委托主模型（apply_performance 全 GPU 落位调用）。"""
        self.model.to(*args, **kwargs)
        return self

    @property
    def device(self):
        """主模型当前设备。"""
        return self.model.device


def _fix_meta_buffers(model) -> None:
    """无条件重置 RoPE 频率表（本地补丁，幂等）。

    transformers 5.x 的 from_pretrained(device_map=...) 对非持久 buffer
    （RoPE inv_freq，persistent=False 不入 checkpoint）的处理链：
    meta 构造（include_buffers=True）→ to_empty(device) 转为**未初始化空张量**
    （非 meta 但数据是垃圾）→ 初始化钩子的 init.copy_ 无法可靠覆盖。
    而 original_inv_freq 是普通属性（非 buffer），不跟随 to_empty，保持 meta。
    两者任一都会让 forward 抛 "Cannot copy out of meta tensor" 或产生错误频率。

    故对每个 Qwen3VLTextRotaryEmbedding 无条件按标准公式（theta=5e6）重建
    inv_freq 并同步 original_inv_freq 引用——标准值恒定，重置即恒等正确。
    """
    import torch

    from .qwen3_vl_transformers import (
        Qwen3VLTextRotaryEmbedding, _default_rope_init,
    )

    device = next((p.device for p in model.parameters() if not p.is_meta), None)
    if device is None:
        logger.warning("模型参数均未就绪（meta），跳过 RoPE 重置")
        return
    fixed = 0
    for m in model.modules():
        if isinstance(m, Qwen3VLTextRotaryEmbedding):
            inv_freq, _ = _default_rope_init(m.config, device)
            m.inv_freq = inv_freq  # nn.Module 对已注册 buffer 名原地替换 _buffers
            m.original_inv_freq = inv_freq  # 普通属性引用同步
            fixed += 1
    logger.warning("已重置 RoPE 频率表 %d 处（device=%s）", fixed, device)


def hidream_o1_loader(path: Path) -> Loader:
    """构建 HiDream-O1 流式加载器：device_map="cuda" 磁盘→显存直达。

    加载前显存预检（不足直接报错，不浪费读盘时间）；加载过程中逐分片
    读盘、逐张量转 bf16 上卡——内存峰值仅单分片，无 RAM 常驻权重副本。
    """
    def _load() -> PipelineLike:
        import torch
        from transformers import AutoProcessor

        from app.runners.base import RunnerError

        from .qwen3_vl_transformers import Qwen3VLForConditionalGeneration

        # 显存预检：流式加载放不下会在半路 OOM，先查够不够（预留激活余量）。
        # 确定性错误须抛 RunnerError：任务引擎按「失败原因原样透出」处理，
        # 普通 RuntimeError 会落「内部错误」并丢失分类（取消/失败语义混淆）
        from app.inference.pool import _HEADROOM_FULL_GPU_MB, _cuda_vram_mb, _dir_size_mb

        vram = _cuda_vram_mb()
        if vram is None:
            raise RunnerError("HiDream-O1 推理需要 CUDA GPU，当前环境不可用")
        need_mb = _dir_size_mb(path) // 2 + _HEADROOM_FULL_GPU_MB  # fp32→bf16 体积减半
        if need_mb > vram[0]:
            raise RunnerError(
                f"显存不足：HiDream-O1 需约 {need_mb}MB，当前可用 {vram[0]}MB"
                "（建议关闭占用显存的程序）"
            )
        processor = AutoProcessor.from_pretrained(str(path))
        # 官方 inference 约定：bf16 + device_map="cuda" 流式上卡
        try:
            model = Qwen3VLForConditionalGeneration.from_pretrained(
                str(path), torch_dtype=torch.bfloat16, device_map="cuda",
            ).eval()
        except torch.cuda.OutOfMemoryError as e:
            raise RunnerError(
                f"显存不足：HiDream-O1 流式加载中途 OOM（{e}），"
                "请关闭占用显存的程序后重试"
            ) from e
        # 本地补丁：重置 meta 状态的非持久 buffer（RoPE inv_freq）
        _fix_meta_buffers(model)
        # 官方 add_special_tokens：采样管线依赖的快捷 token 属性
        tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor
        for attr, token in _SPECIAL_TOKEN_ATTRS:
            setattr(tokenizer, attr, token)
        return HiDreamO1Bundle(model, processor)

    return _load
