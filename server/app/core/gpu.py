"""GPU 探测：torch 可用时报告显存，否则 CPU-only。"""

import shutil
from dataclasses import dataclass, field


@dataclass
class GpuInfo:
    available: bool = False
    device_name: str = ""
    total_vram_mb: int = 0
    free_vram_mb: int = 0
    total_ram_mb: int = 0
    torch_version: str = ""
    cuda_version: str = ""
    inference_ready: bool = False  # torch+diffusers 是否可用
    notes: list[str] = field(default_factory=list)


_cached: GpuInfo | None = None


def detect_gpu(force: bool = False) -> GpuInfo:
    global _cached
    if _cached is not None and not force:
        return _cached

    info = GpuInfo()
    try:
        import torch

        info.torch_version = torch.__version__
        info.available = torch.cuda.is_available()
        if info.available:
            info.cuda_version = torch.version.cuda or ""
            props = torch.cuda.get_device_properties(0)
            info.device_name = props.name
            info.total_vram_mb = props.total_memory // (1024 * 1024)
            free, _total = torch.cuda.mem_get_info(0)
            info.free_vram_mb = free // (1024 * 1024)
        else:
            info.notes.append("CUDA 不可用，视频/图像生成将以 mock 模式运行")
    except ImportError:
        info.notes.append("torch 未安装（可选依赖组 inference），推理功能以 mock 模式运行")

    try:
        import diffusers  # noqa: F401

        info.inference_ready = info.available
    except ImportError:
        info.notes.append("diffusers 未安装")

    # 系统内存（跨平台粗略获取）
    try:
        import psutil

        info.total_ram_mb = int(psutil.virtual_memory().total // (1024 * 1024))
    except ImportError:
        if shutil.which("wmic") is not None:
            try:
                import subprocess

                out = subprocess.run(
                    ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory"],
                    capture_output=True, text=True, timeout=5,
                ).stdout
                for line in out.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        info.total_ram_mb = int(line) // (1024 * 1024)
                        break
            except Exception:
                pass

    _cached = info
    return info
