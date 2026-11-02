"""本地推理资源池：管线驻留复用、LRU 换载、GPU+CPU 混合 offload。"""

from app.inference.pool import PipelinePool, pool

__all__ = ["PipelinePool", "pool"]
