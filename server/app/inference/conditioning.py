"""生图条件预编码缓存（借鉴 Jellyfish H3 Conditioning Cache，见 docs/design/15）。

**为什么需要这一层**：本地出图每次调用都要把「提示词 + 参考图」送进 VLM 处理器
做一次 prefill——参考图还要 resize、patch 化、重算 rope 索引。这步**与随机种子
无关**，却在两处被反复执行：

1. 同一任务出多张（`count > 1`）：runner 循环里每张都重建同一份条件；
2. 重摇：同提示词、同参考图、同尺寸，只换种子。

本模块给出**内容寻址**的条件缓存：键由「模型 + 参考图文件内容 + 提示词 + 尺寸 +
CFG 档 + 负面提示词」逐字哈希而成，值是可直接喂给采样循环的条件张量。命中的唯一
前提是"输入逐字相同"——**不做任何近似、相似或语义猜测**（项目铁律：只在确定处
做确定性复用；语义判断一律交模型，见 CLAUDE.md）。

键自带模型前缀（`{model_id}|{digest}`），因此按模型作废无需额外索引：
`invalidate_model(model_id)` 摘除该模型的全部条目（模型卸载释放显存时调用，
防止陈留条件张量占住显存）。

本模块不导入 torch/diffusers（保持依赖缺失环境下可导入）：张量字节量按 duck
typing 估算，估算不到时退化为 0（仅影响容量核算，不影响正确性）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认容量：条目数上限 + 总字节上限（近似显存预算，超限按 LRU 淘汰）
DEFAULT_MAX_ENTRIES = 64
DEFAULT_MAX_BYTES = 512 * 1024 * 1024  # 512MB

# 键分隔符：`{model_id}|{内容摘要}`（模型前缀支撑按模型作废）
KEY_SEP = "|"

# 参考图内容摘要长度（截断 sha1：足够避免碰撞，键更短便于日志阅读）
_DIGEST_LEN = 20

# 文件摘要缓存上限（路径 → (size, mtime, digest)）：避免同一次任务里
# 对同一批参考图反复读盘哈希。进程级，文件变更（size/mtime 变化）自动失效。
_DIGEST_CACHE_MAX = 4096
_digest_lock = threading.Lock()
_digest_cache: OrderedDict[str, tuple[int, float, str]] = OrderedDict()


def file_digest(path: str) -> str:
    """参考图文件内容摘要（sha1 截断）；文件缺失/不可读时返回**路径绑定**标记。

    缺失文件不抛错：缓存键必须永远可构造（缓存是加速层，不能成为新的失败点）。
    但缺失标记必须与路径绑定（`missing:{路径摘要}`）——曾用统一的 `"missing"`
    常量，导致「两个不同的缺失路径」摘要相同而撞键（验证断言 77(a) 抓到）；
    虽然缺失参考图本就无法完成条件构建、撞键不会命中陈旧条件，但让不同输入
    产生相同键是错的语义，会随实现演变成真缺陷。文件稍后出现时摘要变化，
    缓存自然失效重算。
    """
    p = str(path or "").strip()
    if not p:
        return "empty"
    try:
        st = Path(p).stat()
        size, mtime = int(st.st_size), float(st.st_mtime)
    except OSError:
        return "missing:" + hashlib.sha1(p.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    with _digest_lock:
        hit = _digest_cache.get(p)
        if hit is not None and hit[0] == size and hit[1] == mtime:
            _digest_cache.move_to_end(p)
            return hit[2]
    try:
        h = hashlib.sha1()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()[:_DIGEST_LEN]
    except OSError:
        return "missing"
    with _digest_lock:
        _digest_cache[p] = (size, mtime, digest)
        _digest_cache.move_to_end(p)
        while len(_digest_cache) > _DIGEST_CACHE_MAX:
            _digest_cache.popitem(last=False)
    return digest


def conditioning_key(model_id: str, prompt: str, ref_paths: list[str] | None = None,
                     width: int = 0, height: int = 0, cfg: bool = False,
                     negative_prompt: str = "", extra: str = "") -> str:
    """构造条件缓存键：`{model_id}|{sha1(逐字输入)}`。

    参与哈希的每一项都是**影响条件张量内容**的输入；种子、进程、时间不参与
    （种子只影响噪声初始化，在条件之外）。`cfg` 表示是否构建无条件分支
    （guidance>1 时成立），它改变 samples 的条数，必须入键。
    """
    h = hashlib.sha1()
    # 逐字段喂入并带字段名与长度前缀：避免「a|b」与「a|b」歧义拼接
    h.update(f"w={int(width)};h={int(height)};cfg={int(bool(cfg))};".encode())
    for name, val in (("prompt", prompt), ("neg", negative_prompt),
                      ("extra", extra)):
        raw = (val or "").encode("utf-8")
        h.update(f"{name}#{len(raw)}#".encode())
        h.update(raw)
    for i, rp in enumerate(ref_paths or []):
        d = file_digest(rp).encode()
        h.update(f"ref{i}#{len(d)}#".encode())
        h.update(d)
    return f"{model_id or 'unknown'}{KEY_SEP}{h.hexdigest()}"


def tensor_nbytes(obj: Any) -> int:
    """张量字节量估算（duck typing，不导入 torch）；估算不到返回 0。

    识别 `.element_size()` + `.numel()`（torch.Tensor 的稳定接口），
    容器/list/dict 递归求和。仅用于容量核算——为 0 只是不参与淘汰计算，
    不会造成正确性问题。
    """
    if obj is None:
        return 0
    es = getattr(obj, "element_size", None)
    ne = getattr(obj, "numel", None)
    if callable(es) and callable(ne):
        try:
            return int(es()) * int(ne())
        except Exception:
            return 0
    if isinstance(obj, dict):
        return sum(tensor_nbytes(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return sum(tensor_nbytes(v) for v in obj)
    return 0


def bundle_nbytes(bundle: Any) -> int:
    """条件包字节量（bundle 为 (samples, ref_patches, tgt_image_len)）。"""
    return tensor_nbytes(bundle)


class ConditioningCache:
    """内容寻址的条件张量缓存（LRU + 字节预算，进程级单例使用）。

    线程安全：任务引擎的泳道 worker 是独立线程，缓存必须可并发读写。
    命中/未命中计数供监控与验证断言使用（`stats()`）。
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_bytes = max(0, int(max_bytes))
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, Any] = OrderedDict()
        self._bytes: dict[str, int] = {}
        self._total_bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> Any | None:
        """取条件包；未命中返回 None（含"值为 None 不缓存"的约定）。"""
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                self._hits += 1
                return self._entries[key]
            self._misses += 1
            return None

    def put(self, key: str, value: Any, nbytes: int | None = None) -> None:
        """存条件包；单条超过总预算则不缓存（避免一条挤掉全部）。"""
        size = tensor_nbytes(value) if nbytes is None else int(nbytes)
        if self._max_bytes and size > self._max_bytes:
            logger.info("条件包 %s 超过缓存预算（%d > %d），不缓存",
                        key[:40], size, self._max_bytes)
            return
        with self._lock:
            if key in self._entries:
                self._total_bytes -= self._bytes.get(key, 0)
                self._entries.pop(key, None)
            self._entries[key] = value
            self._bytes[key] = size
            self._total_bytes += size
            self._evict_locked()

    def _evict_locked(self) -> None:
        """LRU 淘汰：先按条目数，再按字节预算（两条上限都要满足）。"""
        while len(self._entries) > self._max_entries:
            self._pop_oldest_locked()
        while self._total_bytes > self._max_bytes and self._entries:
            self._pop_oldest_locked()

    def _pop_oldest_locked(self) -> None:
        key, _ = self._entries.popitem(last=False)
        self._total_bytes -= self._bytes.pop(key, 0)
        self._evictions += 1

    def invalidate_model(self, model_id: str) -> int:
        """按模型作废（模型卸载时调用，释放条件张量占用的显存）。"""
        prefix = f"{model_id or 'unknown'}{KEY_SEP}"
        with self._lock:
            doomed = [k for k in self._entries if k.startswith(prefix)]
            for k in doomed:
                self._entries.pop(k, None)
                self._total_bytes -= self._bytes.pop(k, 0)
            return len(doomed)

    def clear(self) -> int:
        """清空全部条目（返回清掉的条数）。"""
        with self._lock:
            n = len(self._entries)
            self._entries.clear()
            self._bytes.clear()
            self._total_bytes = 0
            return n

    def stats(self) -> dict:
        """运行指标：命中率、条目数、当前/预算字节、淘汰数。"""
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
                "bytes": self._total_bytes,
                "max_bytes": self._max_bytes,
                "evictions": self._evictions,
            }


# 进程级共享缓存：runner 各家族共用（键含模型前缀，互不干扰）
cache = ConditioningCache()
