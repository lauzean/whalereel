"""端点健康账本（25 号 1.5：配额/欠费/队列满的**前置探测**）。

**为什么需要**：全库实测有 **10 条欠费**（403 `AccountOverdueError`）与
**10 条队列满**（503 `video_queue_full`）——全都是**提交之后**才知道的：用户
排了一轮队、白等一次调用，而批量提交会让同一个原因连续失败 N 次（42 镜的批次
就是 42 次白跑，每次都要走完「提交 → 等待 → 收到 4xx/5xx」）。

**做法**（确定性、零额外网络调用——平台侧没有免费探针）：

1. **写入**：任何一次对外调用失败、且类别/错误码表明「端点现在接不了单」时记一笔
   （欠费 → 长时间标记；队列满 → 短时间标记）；
2. **清除**：同一端点**任一调用成功**即清除——自愈优先于等 TTL 到点
   （用户充值后第一次调用就会成功，标记立刻解除）；
3. **读取**：就绪度聚合（`services/readiness.py`）按 host 查——提交前就能告诉
   用户「这个端点欠费了，先充值」，而不是让他再失败一次。

存储用 `Setting`（`provider_health:<host>`）：无需建表、无并发写压力（同一 host
的标记天然收敛到一行），**带 TTL 的惰性过期**不需要任何定时任务——读到过期即删。
"""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy.orm import Session

from app.db.models import Setting

logger = logging.getLogger(__name__)

# Setting 键前缀（同一 host 一行）
PROVIDER_HEALTH_KEY_PREFIX = "provider_health:"

# 标记类别（决定 TTL 与给用户的处置建议）
MARK_QUOTA = "quota"              # 欠费/余额不足：不充值必然继续失败
MARK_CONGESTION = "congestion"    # 队列满：平台侧拥堵，分钟级自愈

# TTL：欠费要等充值（30 分钟；期间任一调用成功即提前解除）——
# 队列满自愈很快，10 分钟足够覆盖一轮批量提交的窗口
QUOTA_TTL_SEC = 1800
CONGESTION_TTL_SEC = 600

# 错误详情入库长度（够看清平台返回的错误码，不把整段 body 存进 Setting）
_DETAIL_MAX = 200


def _key(host: str) -> str:
    """账本键：按 **host** 记（同一服务商的 t2i/t2v 共用一行——账户级故障）。"""
    return f"{PROVIDER_HEALTH_KEY_PREFIX}{host}"


def _ttl_of(kind: str) -> int:
    """标记类别的存活时长（秒）。"""
    return QUOTA_TTL_SEC if kind == MARK_QUOTA else CONGESTION_TTL_SEC


def note_failure(db: Session, host: str, fail_class: str = "",
                 error: str = "", congestion_markers: tuple[str, ...] = ()) -> None:
    """调用失败时记一笔（**只记「端点接不了单」这两类**，别的失败不进来）。

    判据是确定性的：`fail_class`（抛出点已确定的类别）与平台**声明的错误码**
    （agnes 的 `video_queue_full` / `image queue full`）——不做语义猜测。
    传入 `congestion_markers` 的调用方（`providers._record_model_call`）已把
    特征词表定义在那边（与内容安全/配额特征词同处），此处只做匹配。
    """
    try:
        kind = ""
        if fail_class == "quota":
            kind = MARK_QUOTA
        elif error and any(m.lower() in error.lower()
                           for m in congestion_markers):
            kind = MARK_CONGESTION
        if not kind or not host:
            return
        now = int(time.time())
        db.merge(Setting(key=_key(host), value=json.dumps({
            "kind": kind, "host": host, "at": now, "until": now + _ttl_of(kind),
            "detail": (error or "")[:_DETAIL_MAX],
        }, ensure_ascii=False)))
        logger.warning("端点健康标记：%s 记为 %s（%s 秒后过期）：%s",
                       host, kind, _ttl_of(kind), (error or "")[:120])
    except Exception as e:  # noqa: BLE001 账本只是观测，绝不影响调用本身
        logger.warning("端点健康标记写入失败（忽略）：%s", e)


def clear_mark(db: Session, host: str) -> None:
    """调用**成功**时解除标记（自愈优先于 TTL：充值后第一次调用即恢复）。"""
    try:
        if not host:
            return
        row = db.get(Setting, _key(host))
        if row is not None:
            db.delete(row)
            logger.info("端点健康标记已解除（调用成功）：%s", host)
    except Exception as e:  # noqa: BLE001
        logger.warning("端点健康标记解除失败（忽略）：%s", e)


def health_of(db: Session, host: str, *, now: int | None = None) -> dict | None:
    """读该 host 当前是否处于「接不了单」状态（过期即清、返回 None）。"""
    if not host:
        return None
    try:
        row = db.get(Setting, _key(host))
        if row is None or not row.value:
            return None
        data = json.loads(row.value)
        if not isinstance(data, dict):
            return None
        ts = int(now if now is not None else time.time())
        if int(data.get("until") or 0) <= ts:
            db.delete(row)          # 惰性过期：不需要定时任务
            db.commit()
            return None
        data["age_sec"] = max(0, ts - int(data.get("at") or ts))
        return data
    except Exception as e:  # noqa: BLE001 读不到就当健康（保守：不拦用户）
        logger.warning("端点健康读取失败（按健康处理）：%s", e)
        return None


def describe(mark: dict) -> str:
    """标记 → 给用户看的一句中文（含处置建议）。"""
    kind = str(mark.get("kind") or "")
    mins = int(mark.get("age_sec") or 0) // 60
    when = f"{mins} 分钟前" if mins else "刚刚"
    if kind == MARK_QUOTA:
        return (f"端点账户欠费/余额不足（{when}一次调用被拒）："
                "请先充值；充值后该端点第一次调用成功即自动解除限制")
    return (f"平台侧队列已满（{when}一次调用被拒）："
            "服务商排队拥堵、通常几分钟自愈，可稍后重试或改用其他端点")
