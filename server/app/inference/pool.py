"""本地推理管线资源池：模型驻留复用、LRU 换载、GPU+CPU 混合 offload。

对齐设计文档 4.7 调度与「榨干显卡性能」目标：
- 同模型重复任务零加载：管线常驻池内，任务结束不卸载（驻留提速连发）
- 不同模型换载：显存余量不足时按 LRU 卸载最久未用的空闲管线腾位
- apply_performance 统一调优（GPU+CPU 混合核心，按当前可用显存决策）：
  全 GPU > 分层 offload > fp8 layerwise casting（transformer 权重减半重判）>
  group offloading（仅非 Windows，流式近零卡顿）> 串行 offload（极慢，前端告警）
- 空闲卸载器：超过 settings.model_idle_unload_sec 自动回收（AVS_MODEL_IDLE_UNLOAD=0 关闭）
- torch/diffusers 全部延迟导入：依赖缺失时模块仍可导入，调用时抛 RunnerError 提示安装
"""

import gc
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from app.core.config import settings
from app.core.model_manager import get_spec
from app.runners.base import RunnerError

# 管线对象类型（diffusers 未安装时无法静态引用，统一按 Any）
PipelineLike = Any
Loader = Callable[[], PipelineLike]

# 依赖缺失安装提示（错误文案需保持包含 pip install -e .[inference]）
_INSTALL_HINT = "推理依赖未安装：pip install -e .[inference]"

logger = logging.getLogger(__name__)

# 性能档位标识（写入 pipe._avs_perf_mode，供 runner 告警与 pool.stats 展示）
PERF_MODE_FULL_GPU = "full_gpu"
PERF_MODE_MODEL_OFFLOAD = "model_offload"
PERF_MODE_SEQUENTIAL_OFFLOAD = "sequential_offload"
PERF_MODE_FP8_FULL_GPU = "fp8_full_gpu"
PERF_MODE_FP8_MODEL_OFFLOAD = "fp8_model_offload"
# 慢速档位：串行 offload 逐层 CPU 换装（每步数十秒），runner 需向前端明确告警
SLOW_PERF_MODES = frozenset({PERF_MODE_SEQUENTIAL_OFFLOAD})

# 显存决策余量（MB）：全 GPU 档为激活/驱动预留；offload 档为单组件前向激活预留
_HEADROOM_FULL_GPU_MB = 4096
_HEADROOM_OFFLOAD_MB = 2048

# 仅视频家族在加载期启用 VAE tiling（防多帧解码 OOM）；t2i 家族改为运行期按
# 分辨率启停（QwenImage VAE tiling 在常规分辨率下有块状伪影，质量优先）
_VIDEO_FAMILIES = ("wan-2.2", "ltx-2", "minimax-h3")


def _require_torch() -> Any:
    try:
        import torch
    except ImportError as e:
        raise RunnerError(_INSTALL_HINT) from e
    return torch


def _require_diffusers() -> Any:
    try:
        import diffusers
    except ImportError as e:
        raise RunnerError(_INSTALL_HINT) from e
    return diffusers


def _cuda_vram_mb() -> tuple[int, int] | None:
    """返回 (free_mb, total_mb)；torch 缺失或无 CUDA 时返回 None。"""
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    free_b, total_b = torch.cuda.mem_get_info(0)
    return free_b // (1024 * 1024), total_b // (1024 * 1024)


def _pipe_weight_mb(pipe: PipelineLike) -> tuple[float, float]:
    """估算管线真实权重体积：返回 (全管线参数 MB, 最大单组件 MB)。

    逐组件按当前 dtype 求参数字节数（from_pretrained 后即最终推理 dtype）。
    tokenizer / processor 等无参数组件自然计 0。
    """
    total = 0
    max_comp = 0
    for comp in getattr(pipe, "components", {}).values():
        params = getattr(comp, "parameters", None)
        if not callable(params):
            continue
        comp_bytes = sum(p.numel() * p.element_size() for p in params())
        total += comp_bytes
        max_comp = max(max_comp, comp_bytes)
    return total / (1024 * 1024), max_comp / (1024 * 1024)


def _transformer_is_fp8(transformer: Any, fp8_dtype: Any) -> bool:
    """判定 transformer 权重是否已是 fp8 存储（fp8 缓存加载路径的抽查判定）。"""
    params = getattr(transformer, "parameters", None)
    if not callable(params):
        return False
    for p in params():
        return p.dtype == fp8_dtype  # 权重 dtype 统一，抽查首个参数即可
    return False


def _dir_size_mb(path: Path) -> float:
    """递归统计目录体积（MB），不存在返回 0（流式加载显存预检/进度估算分母）。"""
    if not path.exists():
        return 0.0
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total / (1024 * 1024)


def pipeline_class(model_id: str, runner_name: str) -> type | None:
    """按模型家族与 runner 类型选择 diffusers 管线类。

    版本不支持或家族未接入时抛 RunnerError（明确指引，不静默回退）。
    HiDream-O1 家族返回 None：其为 transformers 自定义 UiT 管线（非 diffusers），
    加载走 default_loader 的 hidream 分支，资源池跳过 expect 管线类校验。
    """
    if model_id.startswith("hidream-o1"):
        return None
    diffusers = _require_diffusers()

    def _probe(*names: str, hint: str) -> type:
        for name in names:
            cls = getattr(diffusers, name, None)
            if cls is not None:
                return cls
        raise RunnerError(hint)

    if runner_name in ("t2i", "i2i"):
        if model_id.startswith("qwen-image"):
            if runner_name == "i2i":
                raise RunnerError("qwen-image 图生图暂未接入（i2i 当前仅支持 Flux）")
            return _probe(
                "QwenImagePipeline",
                hint="当前 diffusers 不含 QwenImagePipeline，请升级：pip install -U diffusers",
            )
        if model_id.startswith("flux"):
            name = "FluxImg2ImgPipeline" if runner_name == "i2i" else "FluxPipeline"
            return _probe(name, hint=f"当前 diffusers 不含 {name}，请升级：pip install -U diffusers")
        raise RunnerError(f"模型 {model_id} 不支持 {runner_name} 本地推理")

    # 视频类 runner：t2v / i2v / fl2v / ref2v
    if model_id.startswith("wan-2.2"):
        return _probe(
            "WanPipeline", hint="当前 diffusers 不含 WanPipeline，请升级：pip install -U diffusers"
        )
    if model_id.startswith("ltx-2"):
        if runner_name == "i2v":
            return _probe(
                "LTXImageToVideoPipeline",
                hint="当前 diffusers 不含 LTXImageToVideoPipeline，请升级：pip install -U diffusers",
            )
        return _probe(
            "LTXPipeline", "LTX2Pipeline", "LTXPipeline2",
            hint="当前 diffusers 不含 LTX 管线（LTX-2 需较新版本），请升级：pip install -U diffusers",
        )
    if model_id.startswith("minimax-h3"):
        # 社区集成尚未进 diffusers 主线：探测常见命名，未就绪则明确指引
        return _probe(
            "MiniMaxH3Pipeline", "MiniMaxPipeline", "MiniMaxFlf2vaPipeline",
            hint="H3 diffusers 集成未就绪，请使用 provider 第三方路径或 Wan/LTX 本地路径",
        )
    raise RunnerError(f"模型 {model_id} 暂不支持本地 diffusers 推理（runner={runner_name}）")


# fp8 权重落盘缓存目录名（模型目录下）：首次 fp8 转档后由后台线程写出，
# 后续冷加载直接读 fp8 权重（transformer 体积减半，磁盘 IO 与内存峰值同步下降）
_FP8_CACHE_DIRNAME = "_fp8_cache"


def _fp8_cache_transformer_dir(path: Path) -> Path:
    """fp8 transformer 缓存目录（模型目录/_fp8_cache/transformer）。"""
    return path / _FP8_CACHE_DIRNAME / "transformer"


def default_loader(model_id: str, runner_name: str,
                   models_dir: Path | None = None) -> Loader:
    """按注册表构建 from_pretrained 加载器（preload 预热与 runner 真实路径共用）。

    qwen-image 存在 fp8 transformer 缓存时走缓存加载（读盘体积减半），
    缓存损坏时自动回退标准全量加载。
    """
    path = (models_dir or settings.models_dir) / model_id
    if not path.exists():
        raise RunnerError(f"模型未安装：{model_id}，请先在模型管理中下载")
    if model_id.startswith("hidream-o1"):
        # HiDream-O1：transformers 自定义 UiT 管线，走专用加载器
        from app.inference.hidream_o1.runtime import hidream_o1_loader

        return hidream_o1_loader(path)
    cls = pipeline_class(model_id, runner_name)
    cache_dir = _fp8_cache_transformer_dir(path)
    if model_id.startswith("qwen-image") and (cache_dir / "config.json").exists():
        return _fp8_cached_loader(path, cls, cache_dir)

    def _load() -> PipelineLike:
        import torch

        return cls.from_pretrained(str(path), torch_dtype=torch.bfloat16)

    return _load


def _fp8_cached_loader(path: Path, cls: Any, cache_dir: Path) -> Loader:
    """fp8 缓存加载器：transformer 读 fp8 缓存，其余组件按 bf16 标准加载。

    传入组件实例时 diffusers from_pretrained 只加载其余组件、不改动传入实例
    的 dtype。缓存加载失败（文件损坏等）回退标准全量加载，保证可用性。
    """

    def _load() -> PipelineLike:
        import torch

        try:
            diffusers = _require_diffusers()
            transformer_cls = getattr(diffusers, "QwenImageTransformer2DModel", None)
            if transformer_cls is None:
                raise RunnerError("当前 diffusers 不含 QwenImageTransformer2DModel")
            # fp8 safetensors 以 float8_e4m3fn 原生加载（体积即减半态）
            transformer = transformer_cls.from_pretrained(
                str(cache_dir), torch_dtype=torch.float8_e4m3fn,
            )
            return cls.from_pretrained(
                str(path), transformer=transformer, torch_dtype=torch.bfloat16,
            )
        except Exception:  # noqa: BLE001 缓存不可用一律回退全量加载
            logger.warning("fp8 缓存加载失败，回退全量加载 dir=%s", cache_dir, exc_info=True)
            return cls.from_pretrained(str(path), torch_dtype=torch.bfloat16)

    return _load


def _default_runner_for(model_id: str) -> str:
    """按注册表 task_type 推断 runner 名（preload 未显式指定 runner 时使用）。"""
    spec = get_spec(model_id)
    return "t2i" if spec and spec.task_type == "t2i" else "t2v"


class _Entry:
    """池内条目：驻留管线 + 最后使用时间 + 占用计数（防止生成中被卸载）。"""

    __slots__ = ("model_id", "pipe", "last_used", "busy")

    def __init__(self, model_id: str, pipe: PipelineLike) -> None:
        self.model_id = model_id
        self.pipe = pipe
        self.last_used = time.time()
        self.busy = 0


class PipelinePool:
    """管线资源池（模块底部创建全局单例 pool）。

    线程安全：RLock 可重入。GPU 道由任务引擎串行，但 runner 线程池
    max_workers=2、fast 道与 API 线程可能并发访问，统一由锁保护；
    加载（可能数分钟）在锁内执行，保证同模型不被并发重复加载。
    """

    def __init__(self) -> None:
        self._pipes: dict[str, _Entry] = {}
        self._lock = threading.RLock()
        self._unloader_started = False

    # ---------- 核心接口 ----------
    def acquire(self, model_id: str, loader: Loader,
                expect: type | None = None) -> PipelineLike:
        """获取管线：同模型命中缓存零加载；管线类不符（如 t2i 与 i2i 切换）时卸载重建。

        显存余量不足 spec.vram_mb 时按 LRU 腾挪（del + empty_cache + gc）。
        返回的管线标记为占用中，任务结束须调 release（不卸载，驻留提速连发）。
        新加载的管线自动应用一次 apply_performance 性能调优。
        """
        _require_torch()
        with self._lock:
            entry = self._pipes.get(model_id)
            if entry is not None:
                if expect is None or isinstance(entry.pipe, expect):
                    entry.last_used = time.time()
                    entry.busy += 1
                    return entry.pipe
                # 同权重换管线类（t2i 与 i2i）：卸载重建；被占用时明确报错
                if not self._unload_locked(model_id):
                    raise RunnerError(f"管线 {model_id} 正被占用，无法切换管线类型，请稍后重试")
            self._ensure_vram_locked(model_id)
            pipe = loader()
            entry = _Entry(model_id, pipe)
            entry.busy = 1
            self._pipes[model_id] = entry
            # 显存调优失败必须把 entry 摘出池（审计 13 号 E）：此前异常直接上抛，
            # 调用方拿不到 pipe 自然也不会 release → entry 永久 busy=1，
            # _unload_locked 拒绝卸载、显存再也回收不了；更隐蔽的是后续同模型
            # 任务命中该 entry 后跳过调优，拿到「未落位」管线静默变慢/失败
            try:
                self.apply_performance(pipe, model_id)
            except Exception:
                self._pipes.pop(model_id, None)
                entry.pipe = None
                del pipe, entry
                gc.collect()
                try:
                    import torch

                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001 torch 不可用时不阻断清理
                    pass
                # raise 使控制流永不落到下方 return：上面的 del pipe 是安全的
                # （静态检查会报 "undefined name 'pipe'" —— 已知误报，勿改）
                raise
            return pipe

    def release(self, model_id: str) -> None:
        """任务结束调用：不卸载（驻留提速连发），仅解除占用并刷新使用时间。"""
        with self._lock:
            entry = self._pipes.get(model_id)
            if entry is not None and entry.busy > 0:
                entry.busy -= 1
                entry.last_used = time.time()

    def preload(self, model_id: str, runner_name: str = "") -> float:
        """预热加载（loading_model 阶段调用）：预创建 loader 并 acquire，返回耗时秒数。

        仅填充缓存、不保持占用（生成期的占用由 runner 的 acquire/release 配对管理）。
        """
        runner = runner_name or _default_runner_for(model_id)
        t0 = time.time()
        self.acquire(model_id, default_loader(model_id, runner),
                     expect=pipeline_class(model_id, runner))
        self.release(model_id)
        return time.time() - t0

    def apply_performance(self, pipe: PipelineLike, model_id: str) -> None:
        """GPU+CPU 混合性能调优（幂等；acquire 加载新管线时自动应用一次）。

        显存决策统一使用**当前可用显存 free_mb**（而非总显存）：桌面程序/其他
        进程占用的显存同样不可用，按总显存判断会误入全 GPU 档并触发 Windows
        驱动溢出共享内存（实测 45 倍减速的教训）。

        决策顺序（diffusers 管线）：
        a) 全管线权重 + 4GB 激活缓冲 <= free：pipe.to("cuda") 全 GPU 最快路径
        b) 最大单组件 + 2GB 缓冲 <= free：enable_model_cpu_offload 分层 offload
        c) b) 仍放不下时先尝试 fp8 layerwise casting（权重 fp8 存储、前向逐层
           上转 bf16 计算，transformer 体积减半；norm/patch 等精度敏感层默认跳过，
           质量损失可忽略），转换后按真实体积重新走 a)/b)，并后台落盘 fp8
           缓存供后续冷加载直读（磁盘 IO 减半）；
        d) fp8 也放不下：非 Windows 先试 group offloading（Windows 下 use_stream
           实测慢 40 倍，禁用），否则 enable_sequential_cpu_offload 串行 offload
           （极慢档，pipe._avs_perf_mode 标记后由 runner 向前端告警）。

        纯 transformers 模型（HiDream-O1 UiT，非 diffusers 管线）：不支持
        diffusers offload，仅全 GPU 一条路径，放不下直接明确报错。

        显存需求按加载后管线真实权重体积估算（参数逐项按当前 dtype 求和），
        注册表 vram_mb 仅作无组件信息时的兜底。实测教训：Wan TI2V-5B 全管线
        约 23GB 略超 24GB 显存时，Windows 驱动把溢出权重放入共享内存，
        步速从 4.4s/step 跌至 197s/step（约 45 倍），宁可分层 offload 不可溢出。
        e) 通用增强：VAE tiling 仅视频家族加载期启用（t2i 家族由 runner 按生成
           分辨率启停）；注意力切片仅在低显存档（b/c/d）启用。
        """
        import sys

        if getattr(pipe, "_avs_perf_applied", False):
            return
        torch = _require_torch()
        spec = get_spec(model_id)
        vram_mb = spec.vram_mb if spec else 0
        # 真实权重体积：全管线参数字节数与最大单组件（分层 offload 的实际门槛）
        param_mb, max_comp_mb = _pipe_weight_mb(pipe)
        if param_mb <= 0:
            param_mb = vram_mb  # 无组件信息时回退注册表估算
        if max_comp_mb <= 0:
            max_comp_mb = param_mb
        is_diffusers = hasattr(pipe, "enable_model_cpu_offload")
        vram = _cuda_vram_mb()
        low_vram = False
        mode = ""
        if vram is None:
            if not is_diffusers:
                # 非 diffusers 模型（如 HiDream-O1）必须有 GPU，无法降级
                raise RunnerError(f"模型 {model_id} 推理需要 CUDA GPU，当前环境不可用")
            # diffusers 管线无 CUDA（mock/CPU 环境）：不做 GPU 调优
        else:
            free_mb = vram[0]
            if not is_diffusers:
                # 纯 transformers 模型：仅全 GPU 一条路径，放不下明确报错（不静默降速）
                bundle_model = getattr(pipe, "model", None)
                already_on_gpu = getattr(bundle_model, "device", None) is not None \
                    and str(bundle_model.device).startswith("cuda")
                if already_on_gpu:
                    # 流式加载（device_map="cuda"）已上卡：加载成功即证明显存足够，免落位
                    mode = PERF_MODE_FULL_GPU
                else:
                    need_mb = param_mb + _HEADROOM_FULL_GPU_MB
                    if need_mb > free_mb:
                        raise RunnerError(
                            f"显存不足：{model_id} 全 GPU 约需 {need_mb}MB，"
                            f"当前可用 {free_mb}MB（建议关闭占用显存的程序或更换模型）"
                        )
                    pipe.to("cuda")
                    mode = PERF_MODE_FULL_GPU
            else:
                mode, low_vram = self._tune_diffusers(
                    pipe, torch, param_mb, max_comp_mb, free_mb,
                    model_path=settings.models_dir / model_id,
                )
        # e) 通用增强（VAE tiling 仅视频家族；t2i 家族由 runner 按分辨率启停）
        if is_diffusers and model_id.startswith(_VIDEO_FAMILIES) \
                and hasattr(pipe, "enable_vae_tiling"):
            try:
                pipe.enable_vae_tiling()
            except Exception:
                pass
        if low_vram and hasattr(pipe, "enable_attention_slicing"):
            try:
                pipe.enable_attention_slicing()
            except Exception:
                pass
        pipe._avs_perf_mode = mode
        pipe._avs_perf_applied = True

    @staticmethod
    def _fit_perf_mode(param_mb: float, max_comp_mb: float,
                       free_mb: int) -> str | None:
        """按可用显存匹配落位档位：全 GPU > 分层 offload，均放不下返回 None。"""
        if param_mb + _HEADROOM_FULL_GPU_MB <= free_mb:
            return PERF_MODE_FULL_GPU
        if max_comp_mb + _HEADROOM_OFFLOAD_MB <= free_mb:
            return PERF_MODE_MODEL_OFFLOAD
        return None

    def _tune_diffusers(self, pipe: PipelineLike, torch: Any, param_mb: float,
                        max_comp_mb: float, free_mb: int,
                        model_path: Path) -> tuple[str, bool]:
        """diffusers 管线落位：a/b 直接决策 → fp8 layerwise casting 兜底重判 → 串行。

        fp8 权重（缓存加载或当场转档）无论最终落在哪个档位都必须挂
        layerwise casting 上转 hooks（fp8 权重直接 forward 会 dtype 报错），
        故 hooks 挂载与档位决策解耦。fp8 转档成功后后台落盘缓存（幂等，
        已有缓存跳过），后续冷加载直接读 fp8 权重省一半磁盘 IO。

        返回 (性能档位, 是否低显存档)；低显存档在调用方追加注意力切片。
        """
        import sys

        transformer = getattr(pipe, "transformer", None)
        fp8_dtype = getattr(torch, "float8_e4m3fn", None)
        can_fp8 = (fp8_dtype is not None and transformer is not None
                   and hasattr(transformer, "enable_layerwise_casting"))

        def _ensure_fp8_hooks() -> bool:
            """确保 fp8 存储权重挂上逐层上转 hooks；已挂/不支持返回 False。"""
            if not can_fp8:
                return False
            try:
                transformer.enable_layerwise_casting(
                    storage_dtype=fp8_dtype, compute_dtype=torch.bfloat16,
                )
                return True
            except Exception:  # noqa: BLE001 casting 特性不可用时按 bf16 处理
                return False

        # 缓存加载的 fp8 transformer：权重已是 fp8 存储，必须先挂 hooks
        if can_fp8 and _transformer_is_fp8(transformer, fp8_dtype):
            _ensure_fp8_hooks()

        mode = self._fit_perf_mode(param_mb, max_comp_mb, free_mb)
        if mode == PERF_MODE_FULL_GPU:
            pipe.to("cuda")  # a) 全 GPU（后续不再 offload，互斥）
            return mode, False
        if mode == PERF_MODE_MODEL_OFFLOAD:
            pipe.enable_model_cpu_offload()  # b) 分层 offload
            return mode, True

        # c) bf16 装不下：transformer 权重转 fp8 存储（体积减半），转换后
        # 按真实体积重新走 a)/b) 决策；精度敏感层由 diffusers 默认 skip 保护
        if can_fp8 and not _transformer_is_fp8(transformer, fp8_dtype):
            try:
                transformer.enable_layerwise_casting(
                    storage_dtype=fp8_dtype, compute_dtype=torch.bfloat16,
                )
                fp8_param_mb, fp8_max_comp_mb = _pipe_weight_mb(pipe)
                if fp8_param_mb > 0:
                    param_mb, max_comp_mb = fp8_param_mb, fp8_max_comp_mb
                # 后台落盘 fp8 缓存（下次冷加载免转档 + 读盘减半）
                self._schedule_fp8_cache_save(transformer, model_path)
                mode = self._fit_perf_mode(param_mb, max_comp_mb, free_mb)
                if mode == PERF_MODE_FULL_GPU:
                    pipe.to("cuda")
                    return PERF_MODE_FP8_FULL_GPU, False
                if mode == PERF_MODE_MODEL_OFFLOAD:
                    pipe.enable_model_cpu_offload()
                    return PERF_MODE_FP8_MODEL_OFFLOAD, True
            except Exception:  # noqa: BLE001 casting 特性不可用时退回串行档
                logger.warning(
                    "fp8 layerwise casting 失败 model=%s，退回串行 offload",
                    model_path.name, exc_info=True,
                )

        # d) 超显存：非 Windows 先试 group offloading（流式），失败或
        # Windows（use_stream 实测慢 40 倍）则串行 offload（极慢档）
        group_ok = False
        if sys.platform != "win32" and hasattr(pipe, "enable_group_offloading"):
            try:
                pipe.enable_group_offloading(use_stream=True)
                group_ok = True
            except Exception:
                group_ok = False
        if not group_ok:
            pipe.enable_sequential_cpu_offload()
        return PERF_MODE_SEQUENTIAL_OFFLOAD, True

    def _schedule_fp8_cache_save(self, transformer: Any, model_path: Path) -> None:
        """后台落盘 fp8 transformer 缓存（幂等：已有缓存跳过）。

        save 先写临时目录、完成后原子改名，避免半截缓存被下次加载误用；
        失败静默（缓存只是加速手段，不影响推理正确性）。保存期间后台线程
        持有 transformer 引用，不阻塞推理（只读参数）。
        """
        cache_dir = _fp8_cache_transformer_dir(model_path)
        if (cache_dir / "config.json").exists():
            return
        if not hasattr(transformer, "save_pretrained"):
            return

        def _save() -> None:
            import tempfile

            tmp_dir = Path(tempfile.mkdtemp(
                prefix="_fp8_saving_", dir=str(cache_dir.parent.parent),
            ))
            try:
                transformer.save_pretrained(str(tmp_dir), safe_serialization=True)
                cache_dir.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(tmp_dir), str(cache_dir))
            except Exception:  # noqa: BLE001 缓存写出失败不影响推理
                logger.warning("fp8 缓存落盘失败 dir=%s", cache_dir, exc_info=True)
                shutil.rmtree(tmp_dir, ignore_errors=True)

        threading.Thread(target=_save, name="avs-fp8-cache-save", daemon=True).start()

    def stats(self) -> dict:
        """池状态：驻留模型 / 最后使用时间 / 显存占用（依赖缺失时不抛异常）。"""
        with self._lock:
            now = time.time()
            resident = [
                {
                    "model_id": e.model_id,
                    "pipeline": type(e.pipe).__name__,
                    "perf_mode": getattr(e.pipe, "_avs_perf_mode", ""),
                    "busy": e.busy,
                    "last_used": round(e.last_used, 3),
                    "idle_sec": round(now - e.last_used, 1),
                }
                for e in self._pipes.values()
            ]
        vram: dict = {}
        info = _cuda_vram_mb()
        if info is None:
            vram = {"note": "torch 未安装或 CUDA 不可用（mock 模式）"}
        else:
            try:
                import torch

                vram = {
                    "allocated_mb": torch.cuda.memory_allocated(0) // (1024 * 1024),
                    "free_mb": info[0],
                    "total_mb": info[1],
                }
            except ImportError:
                vram = {"note": "torch 未安装"}
        return {
            "resident": resident,
            "vram": vram,
            "idle_unload_sec": settings.model_idle_unload_sec,
        }

    def unload(self, model_id: str) -> bool:
        """卸载指定模型管线（正在使用时拒绝卸载），返回是否成功。"""
        with self._lock:
            return self._unload_locked(model_id)

    def unload_all(self) -> int:
        """卸载全部空闲管线，返回成功卸载数量。"""
        with self._lock:
            count = 0
            for model_id in list(self._pipes):
                if self._unload_locked(model_id):
                    count += 1
            return count

    def start_idle_unloader(self) -> None:
        """启动空闲卸载后台线程（应用 startup 调用；重复调用安全）。"""
        with self._lock:
            if self._unloader_started:
                return
            self._unloader_started = True
        threading.Thread(
            target=self._idle_loop, name="avs-model-idle-unloader", daemon=True,
        ).start()

    # ---------- 内部实现 ----------
    def _idle_loop(self) -> None:
        """每 30 秒巡检：空闲超过 model_idle_unload_sec 的管线自动卸载。"""
        while True:
            time.sleep(30)
            idle_sec = settings.model_idle_unload_sec
            if idle_sec <= 0:
                continue  # AVS_MODEL_IDLE_UNLOAD=0：关闭空闲回收
            now = time.time()
            with self._lock:
                stale = [(e.model_id, now - e.last_used) for e in self._pipes.values()
                         if e.busy == 0 and now - e.last_used > idle_sec]
                for model_id, idle_for in stale:
                    if self._unload_locked(model_id):
                        # 静默卸载不可观测：用户会误以为驻留失效（跨画布重载问题排查）
                        logger.info(
                            "空闲自动卸载 model=%s（空闲 %.0f 秒，超过阈值 %d 秒）",
                            model_id, idle_for, idle_sec,
                        )

    def _ensure_vram_locked(self, model_id: str) -> None:
        """加载前确保显存余量：不足时按 LRU 卸载空闲管线，直到余量够或无可卸载。"""
        vram = _cuda_vram_mb()
        if vram is None:
            return
        free_mb, _total_mb = vram
        spec = get_spec(model_id)
        need_mb = spec.vram_mb if spec else 0
        while free_mb < need_mb:
            candidates = [e for e in self._pipes.values() if e.busy == 0]
            if not candidates:
                break  # 池内已无可卸载的空闲管线：直接尝试加载，由 offload 兜底
            victim = min(candidates, key=lambda e: e.last_used)
            if not self._unload_locked(victim.model_id):
                break
            vram = _cuda_vram_mb()
            if vram is None:
                return
            free_mb = vram[0]

    def _unload_locked(self, model_id: str) -> bool:
        """卸载并释放显存（须持锁调用）：del 引用 + empty_cache + gc。"""
        entry = self._pipes.pop(model_id, None)
        if entry is None:
            return False
        if entry.busy > 0:
            # 正在生成：拒绝卸载（放回池内）
            self._pipes[model_id] = entry
            return False
        pipe = entry.pipe
        entry.pipe = None
        del pipe, entry
        gc.collect()
        # 条件缓存随模型一并作废：缓存的条件张量驻留在设备上，模型卸载若只释放
        # 权重而留下条件张量，显存回收就不彻底（缓存是纯加速层，重算即可，无损失）
        try:
            from app.inference import conditioning

            dropped = conditioning.cache.invalidate_model(model_id)
            if dropped:
                logger.info("条件缓存随模型卸载作废 model=%s 条目=%d", model_id, dropped)
        except Exception:  # 缓存作废失败不应阻断卸载流程
            logger.debug("条件缓存作废失败 model=%s", model_id, exc_info=True)
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        return True


# 全局单例：任务引擎与各 runner 共用
pool = PipelinePool()
