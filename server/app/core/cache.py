"""产物缓存：runner+model+quant+params+输入哈希 → 产物。

对齐设计文档 6.3。命中返回 artifact_id，未命中返回 None。
"""

import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.db.models import Asset
from app.db.session import SessionLocal


def make_key(runner: str, model_id: str, params: dict, input_refs: dict) -> str:
    payload = json.dumps(
        {"runner": runner, "model": model_id, "params": params, "inputs": input_refs},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def lookup(runner: str, model_id: str, params: dict, input_refs: dict,
           key: str = "") -> str | None:
    """缓存命中则返回 artifact_id。

    key 非空时直接用它（调用方在**随机 seed 注入之前**算好的键）；空则现算。
    提交侧固化键的理由见 task_engine.submit：执行侧重算必然带 seed、必然不同。
    """
    key = key or make_key(runner, model_id, params, input_refs)
    marker = settings.cache_dir / f"{key}.json"
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        artifact_id = data.get("artifact_id", "")
        # 校验产物文件仍在
        with SessionLocal() as db:
            asset = db.get(Asset, artifact_id)
            if asset and Path(asset.path).exists():
                return artifact_id
    except Exception:
        pass
    marker.unlink(missing_ok=True)
    return None


def store(runner: str, model_id: str, params: dict, input_refs: dict,
          artifact_id: str, key: str = "") -> None:
    key = key or make_key(runner, model_id, params, input_refs)
    marker = settings.cache_dir / f"{key}.json"
    marker.write_text(
        json.dumps({"artifact_id": artifact_id, "runner": runner, "model": model_id}),
        encoding="utf-8",
    )
    _evict_if_needed()


def _referenced_bytes(markers: list[Path]) -> dict[Path, int]:
    """每个 marker 指向的**产物文件真实字节数**（读不到按 0 计）。

    为什么不能统计 marker 自身大小（2026-09-30 修）：marker 是约 120 字节的
    json，而它指向的产物是 MB 级 mp4/png。旧实现 `sum(p.stat().st_size for p
    in markers)` 统计的是 marker ⇒ 上限 20GB 需要约 1.7 亿个 marker 才可能触发，
    也就是**这个淘汰循环永远不会执行**，注释里「磁盘占用有确定上限」是假的。
    """
    sizes: dict[Path, int] = {}
    with SessionLocal() as db:
        for marker in markers:
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
                asset = db.get(Asset, data.get("artifact_id", ""))
                size = Path(asset.path).stat().st_size if asset and asset.path else 0
            except Exception:  # noqa: BLE001 单个 marker 损坏不影响整体统计
                size = 0
            sizes[marker] = size
    return sizes


def _evict_if_needed() -> None:
    """缓存目录容量治理：超出上限时按 mtime 从旧到新删 marker。

    **只管「缓存命中能力」，不删产物**：产物本身由资产库管理（可能仍被分镜
    引用），删掉会毁掉用户已付钱的成品。这里淘汰的是「键 → 产物」的映射，
    效果是「旧产物不再能零成本复用」，磁盘回收由产物侧（孤儿清理）负责。
    统计口径 = 被引用产物的真实字节数，见 `_referenced_bytes`。
    """
    try:
        limit = float(getattr(settings, "cache_limit_gb", 0) or 0)
        if limit <= 0:
            return
        markers = sorted(
            (p for p in settings.cache_dir.glob("*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime)
        sizes = _referenced_bytes(markers)
        total = sum(sizes.values())
        budget = int(limit * 1024 ** 3)
        while markers and total > budget:
            old = markers.pop(0)
            total -= sizes.pop(old, 0)
            old.unlink(missing_ok=True)
    except OSError:
        pass  # 清理失败不影响生成主流程
