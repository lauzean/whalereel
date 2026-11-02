"""积分计费服务（33 号方案，2026-10-07 拍板；2026-10-09 口径定稿）——计费体系的
全部记账逻辑。

白话总览（计费怎么运作）：
- **费率配置在「模型服务端点」上**（后台「模型管理」里每个服务多少钱：
  每千 token / 每张图 / 每秒视频的积分数）——见 provider_endpoints 的费率列；
- **按调用情况计费**：每一次真实模型调用（调用日志 model_calls 一行）在记录时
  按「所用服务的费率 × 本次用量」把积分算好存下（`usage_credits` 是那把尺子，
  providers 调用）；一次操作的实际消耗 = 它名下成功调用行的积分之和
  （`collect_credits`）。
- **发起时押金 + 完成后多退少补**：会产生模型调用的 HTTP 接口在内置清单
  `GATED_ENDPOINTS` 里（预扣额内置默认、无需配置）；请求进门先原子扣押金
  （BillingGate 调 charge），完成后 `settle_usage` 按实际调用结算退差额；
  失败/取消全额退。系统平台接口（保存/编辑/查询/管理）不在清单 = 免费。
- 每一次余额变动都写一行流水（credit_transactions），「流水累计 = 余额」
  随时对账（audit_consistency）。

**钱的安全线（改动本模块前必读）**：
1. 余额扣减只允许走这里的一条原子 SQL（`UPDATE ... WHERE balance >= :c`）——
   绝不在别处「先读再写」改 balance（并发会透支）；
2. 退款只允许走 refund_txn 的一条 CAS（pending/consumed → refunded）——
   rowcount==1 才继续加钱，天然幂等防双退；结算有 settled_at 的 CAS 同款兜底；
3. 每个记账动作一个短事务、**显式 commit**（Session 上下文退出不 commit，见
   script/persist.py 的静默事故先例）；失败让异常冒泡（close 隐式回滚），绝不半提交；
4. 账户在首次往来时幂等建户（INSERT OR IGNORE），**必须先建户再扣**——否则新用户
   第一次调用会被误判「余额不足」（UPDATE 的 rowcount==0 同时表示「没账户」与
   「钱不够」，建户先行让两者可区分）。

任务关联（哪笔扣费对应哪些任务）：BillingGate 扣费成功后 set_request_ctx
（ContextVar），`engine.submit()`（跑在请求线程）读取并落到 tasks.billing_txn_id
——18 处调用点零改动。评审核实：纯 ASGI 中间件 set 的 ContextVar 能被同步线程池
端点与 async 端点读到（anyio 会把调用方上下文拷进工作线程）；fail-safe：读不到 =
空串 = 无退款关联（只会少退不会错退）；test_billing.py 有全链路守护测试。
"""

from __future__ import annotations

import contextvars
import json
import logging
import secrets
import time
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.models import (
    TXN_ADJUST, TXN_BONUS, TXN_CONSUME, TXN_CONSUMED, TXN_PENDING, TXN_RECHARGE,
    TXN_REFUND, TXN_REFUNDED, CreditAccount, CreditTransaction,
    RedeemCode, Setting,
)
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


def _now() -> int:
    return int(time.time())


def _uuid() -> str:
    return uuid.uuid4().hex


# ---------- 全局配置（Setting 表，billing_* snake_case 键，同现有惯例） ----------

BILLING_ENABLED_KEY = "billing_enabled"        # 计费总开关（默认开；价目全 0 时无感）
BILLING_SIGNUP_GRANT_KEY = "billing_signup_grant"  # 新用户初始赠送积分（默认 200）
BILLING_AUTO_REFUND_KEY = "billing_auto_refund"    # 失败自动退款（默认开）
BILLING_ADMIN_FREE_KEY = "billing_admin_free"      # 管理员免计费（默认关 = 也计费）

DEFAULT_SIGNUP_GRANT = 200
DEFAULT_AUTO_REFUND = True
DEFAULT_ADMIN_FREE = False


def _get_setting(db, key: str) -> str | None:  # noqa: ANN001
    row = db.get(Setting, key)
    return row.value if row is not None else None


def _set_setting(db, key: str, value: str) -> None:  # noqa: ANN001
    db.merge(Setting(key=key, value=value))


def _get_bool(db, key: str, default: bool) -> bool:  # noqa: ANN001
    raw = _get_setting(db, key)
    if raw is None or raw == "":
        return default
    try:
        return bool(json.loads(raw))
    except (ValueError, TypeError):
        return default


def _get_int(db, key: str, default: int) -> int:  # noqa: ANN001
    raw = _get_setting(db, key)
    if raw is None or raw == "":
        return default
    try:
        return int(json.loads(raw))
    except (ValueError, TypeError):
        return default


def get_settings() -> dict:
    """读取全部计费全局配置（管理页与网关用）。"""
    with SessionLocal() as db:
        return {
            "enabled": _get_bool(db, BILLING_ENABLED_KEY, True),
            "signup_grant": _get_int(db, BILLING_SIGNUP_GRANT_KEY,
                                     DEFAULT_SIGNUP_GRANT),
            "auto_refund": _get_bool(db, BILLING_AUTO_REFUND_KEY,
                                     DEFAULT_AUTO_REFUND),
            "admin_free": _get_bool(db, BILLING_ADMIN_FREE_KEY,
                                    DEFAULT_ADMIN_FREE),
        }


class _GateConfigCache:
    """网关热路径配置缓存（enabled / admin_free，TTL 30 秒）。

    BillingGate 每个请求都要读这两个开关；直接查 Setting 是每请求一次线程池
    切换 + 多条 SELECT（评审指出的热路径开销，免费 GET/轮询也全都要过）。
    update_settings 保存后显式失效，改开关即时生效。
    """

    TTL_SEC = 30.0

    def __init__(self) -> None:
        self.data: dict | None = None
        self.loaded_at = 0.0

    def get(self) -> dict:
        if self.data is None or time.time() - self.loaded_at > self.TTL_SEC:
            self.data = get_settings()
            self.loaded_at = time.time()
        return self.data

    def invalidate(self) -> None:
        self.data = None
        self.loaded_at = 0.0


_gate_cfg = _GateConfigCache()


def gate_config() -> dict:
    """BillingGate 用：{enabled, admin_free}（带 TTL 缓存的只读视图）。"""
    return _gate_cfg.get()


def auto_refund_enabled() -> bool:
    """失败自动退款开关（billing_auto_refund，默认开；管理页可关）。

    关闭语义 = 「失败也按调用成立收全款」——相关调用点把流水收尾为
    consumed（而不是悬挂在 pending 等人工处理），补偿交给管理员「调整」。
    """
    return bool(_gate_cfg.get().get("auto_refund", DEFAULT_AUTO_REFUND))


def update_settings(patch: dict) -> dict:
    """保存计费全局配置（只写提供的键；校验取值后 json 落库）。"""
    with SessionLocal() as db:
        if "enabled" in patch:
            _set_setting(db, BILLING_ENABLED_KEY, json.dumps(bool(patch["enabled"])))
        if "signup_grant" in patch:
            grant = int(patch["signup_grant"])
            if grant < 0:
                raise ValueError("初始赠送不能为负数")
            _set_setting(db, BILLING_SIGNUP_GRANT_KEY, json.dumps(grant))
        if "auto_refund" in patch:
            _set_setting(db, BILLING_AUTO_REFUND_KEY,
                         json.dumps(bool(patch["auto_refund"])))
        if "admin_free" in patch:
            _set_setting(db, BILLING_ADMIN_FREE_KEY, json.dumps(bool(patch["admin_free"])))
        db.commit()
    _gate_cfg.invalidate()  # 网关缓存即时失效（改开关立即生效）
    return get_settings()


# ---------- 请求 → 流水/用量 关联（ContextVar；BillingGate set、下游读） ----------

_request_txn: contextvars.ContextVar[str] = contextvars.ContextVar(
    "avs_billing_txn", default="")
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "avs_billing_request", default="")


def set_request_ctx(txn_id: str, request_id: str = "") -> None:
    """BillingGate 扣费成功后调用（必须在**事件循环线程**里 set）：
    - txn_id 供 engine.submit 落 tasks.billing_txn_id（失败退款关联）；
    - request_id 供 providers 落 model_calls.request_id（按用量计费的
      同步端点结算凭它汇总用量）。"""
    _request_txn.set(txn_id or "")
    _request_id.set(request_id or "")


def current_txn_id() -> str:
    """当前请求的消费流水 id（无计费上下文的调用返回空串，fail-safe）。"""
    return _request_txn.get()


def current_request_id() -> str:
    """当前请求的编号（providers 写调用日志时读取；无计费上下文返回空串）。"""
    return _request_id.get()


# ---------- 账户（幂等建户 + 记账辅助） ----------

def _ensure_account_in_session(db, user_id: str) -> bool:  # noqa: ANN001
    """幂等建户（同一事务内）；**新建成功时**按配置发放初始赠送。返回是否新建。

    并发安全：INSERT OR IGNORE + 唯一索引，多个并发首扣只会有一个真插入，
    赠送只发一次（rowcount 判定）。必须先于任何扣减/加钱调用——否则
    `UPDATE ... WHERE balance >= :c` 的 rowcount==0 无法区分「没账户」与「钱不够」。
    """
    grant = _get_int(db, BILLING_SIGNUP_GRANT_KEY, DEFAULT_SIGNUP_GRANT)
    now = _now()
    n = db.execute(
        sqlite_insert(CreditAccount).values(
            id=_uuid(), user_id=user_id,
            balance=max(0, grant), total_recharged=0, total_consumed=0,
            owner_user_id=user_id, created_at=now, updated_at=now,
        ).on_conflict_do_nothing(index_elements=["user_id"])
    ).rowcount
    if n and grant > 0:
        # 赠送也要有流水（账本完整性：SUM(amount) 必须 == balance）
        balance = db.scalar(select(CreditAccount.balance).where(
            CreditAccount.user_id == user_id))
        db.add(CreditTransaction(
            id=_uuid(), user_id=user_id, kind=TXN_BONUS, amount=grant,
            balance_after=balance if balance is not None else grant,
            status="", note=f"新用户初始赠送 {grant} 积分",
            owner_user_id=user_id, created_at=now,
        ))
    return bool(n)


def account_dict(user_id: str) -> dict:
    """账户摘要（余额/累计；未建户返回零值不建户——查询不产生副作用）。"""
    with SessionLocal() as db:
        row = db.scalar(select(CreditAccount).where(
            CreditAccount.user_id == user_id))
        if row is None:
            return {"user_id": user_id, "balance": 0, "total_recharged": 0,
                    "total_consumed": 0, "created_at": 0}
        return {"user_id": row.user_id, "balance": row.balance,
                "total_recharged": row.total_recharged,
                "total_consumed": row.total_consumed,
                "created_at": row.created_at}


# ---------- 扣费 / 结算 / 退款（钱的三条安全线） ----------

def charge(user_id: str, endpoint_key: str, price: int, *,
           request_id: str = "", project_id: str | None = None) -> dict:
    """原子扣费（BillingGate 在放行业务之前调用；在线程池里跑）。

    price：**预扣押金**（GATED_ENDPOINTS 内置额）——完成后按实际调用记录
    结算、多退少补（见 settle_usage）。
    返回两种形态：
    - 成功：{"ok": True, "txn_id": ..., "balance": 扣后余额}
    - 余额不足：{"ok": False, "balance": 当前余额, "required": price}
    一个事务内完成「建户 → 原子扣减 → 写 pending 消费流水」，异常整体回滚
    （绝不出现「扣了钱没记账」）。
    """
    if price <= 0:
        raise ValueError("扣费金额必须为正整数")
    now = _now()
    with SessionLocal() as db:
        _ensure_account_in_session(db, user_id)
        # 原子扣减：余额够才扣（并发下多个请求在此串行判定，天然防透支）
        n = db.execute(
            update(CreditAccount)
            .where(CreditAccount.user_id == user_id,
                   CreditAccount.balance >= price)
            .values(balance=CreditAccount.balance - price,
                    total_consumed=CreditAccount.total_consumed + price,
                    updated_at=now)
        ).rowcount
        if n == 0:
            # 余额不足：**不能 rollback**——同一事务里可能刚建了账户（含初始
            # 赠送），回滚会把赠送丢掉、402 里显示的余额也变 0（评审抓到的
            # 边界：新用户首个高价调用后赠送一直不落库）。扣减 UPDATE 无匹配
            # 行 = 无改动，直接读余额 + 提交落建户即可。
            balance = db.scalar(select(CreditAccount.balance).where(
                CreditAccount.user_id == user_id))
            db.commit()
            return {"ok": False, "balance": int(balance or 0),
                    "required": price}
        balance = db.scalar(select(CreditAccount.balance).where(
            CreditAccount.user_id == user_id))
        txn = CreditTransaction(
            id=_uuid(), user_id=user_id, kind=TXN_CONSUME, amount=-price,
            balance_after=int(balance or 0), status=TXN_PENDING,
            endpoint=endpoint_key, price=price, request_id=request_id,
            project_id=project_id,
            note=f"调用 {endpoint_key}（预扣）",
            owner_user_id=user_id, created_at=now,
        )
        db.add(txn)
        db.commit()
        return {"ok": True, "txn_id": txn.id, "balance": int(balance or 0)}


def mark_consumed(txn_id: str) -> bool:
    """请求成功 → 计费成立（CAS：pending → consumed）。

    只在流水**仍是 pending** 时生效——若期间已被结算/退款（如批量任务在响应
    发出前就全部终态的极快路径），rowcount==0，不动（不把 refunded 改回来）。
    """
    if not txn_id:
        return False
    with SessionLocal() as db:
        n = db.execute(
            update(CreditTransaction)
            .where(CreditTransaction.id == txn_id,
                   CreditTransaction.status == TXN_PENDING)
            .values(status=TXN_CONSUMED)
        ).rowcount
        db.commit()
        return n == 1


def refund_txn(txn_id: str, *, reason: str = "") -> bool:
    """退款（**幂等**：同一笔流水无论从哪个入口退，只会退一次）。

    做法是一条 CAS：`pending/consumed → refunded`，只有把它改成 refunded 的那
    一次调用（rowcount==1）才继续恢复余额 + 写退款流水（同一事务）；其余调用
    直接返回 False。两道守卫：`settled_at == 0`（已按用量结算的流水不再退——
    否则退款行会撞 ref_txn_id 唯一约束）；ref_txn_id 唯一约束本身是双退保险。
    """
    if not txn_id:
        return False
    now = _now()
    with SessionLocal() as db:
        row = db.get(CreditTransaction, txn_id)
        if row is None or row.kind != TXN_CONSUME:
            return False
        n = db.execute(
            update(CreditTransaction)
            .where(CreditTransaction.id == txn_id,
                   CreditTransaction.settled_at == 0,
                   CreditTransaction.status.in_((TXN_PENDING, TXN_CONSUMED)))
            .values(status=TXN_REFUNDED)
        ).rowcount
        if n == 0:
            db.rollback()
            return False  # 已退过 / 已结算（或状态异常），幂等吸收
        amount = -row.amount  # 消费流水是负数，退款金额 = 绝对值
        db.execute(
            update(CreditAccount)
            .where(CreditAccount.user_id == row.user_id)
            .values(balance=CreditAccount.balance + amount,
                    # 累计消费按「净消费」口径回减——被退掉的不算真实消费，
                    # 否则「扣了又退」会虚增展示的累计消费
                    total_consumed=CreditAccount.total_consumed - amount,
                    updated_at=now)
        )
        balance = db.scalar(select(CreditAccount.balance).where(
            CreditAccount.user_id == row.user_id))
        db.add(CreditTransaction(
            id=_uuid(), user_id=row.user_id, kind=TXN_REFUND, amount=amount,
            balance_after=int(balance or 0), status="",
            endpoint=row.endpoint, price=row.price, project_id=row.project_id,
            ref_txn_id=row.id, note=reason or f"退款：{row.endpoint}",
            owner_user_id=row.user_id, created_at=now,
        ))
        db.commit()
        logger.info("积分退款：用户 %s 退回 %s（流水 %s，原因：%s）",
                    row.user_id, amount, txn_id, reason or "自动")
        return True


def count_txn_tasks(txn_id: str) -> int:
    """该流水关联的任务数（BillingGate 区分「任务化端点 vs 同步端点」用：
    有任务 → 按用量结算推迟到任务终态；无任务 → 响应成功即可按请求用量结算）。"""
    from app.db.models import Task
    with SessionLocal() as db:
        return int(db.scalar(
            select(func.count()).select_from(Task)
            .where(Task.billing_txn_id == txn_id)) or 0)


def collect_credits(*, request_id: str = "",
                    task_ids: list[str] | None = None) -> int:
    """按范围汇总「行级积分」——按调用情况计费的结算依据（2026-10-09 口径）。

    数据源是 model_calls.credits：每一次对外调用在**记录时**就按「所用模型
    服务端点的费率 × 本次用量」把积分算好存下（providers._record_model_call），
    这里只做一条 SUM。**只统计成功的调用**（ok=True）——失败调用按「失败不
    计费」原则（成本已发生的「远端已计费」类由退款规则另行处理）。
    范围二选一：request_id（同步端点，本次请求内的全部调用）
    或 task_ids（任务化端点，这些任务产生的全部调用；批量任务分散在多次
    请求与请求之外，按任务号才是准确口径）。
    """
    from app.db.models import ModelCall
    if not request_id and not task_ids:
        return 0
    with SessionLocal() as db:
        q = select(func.coalesce(func.sum(ModelCall.credits), 0)).where(
            ModelCall.ok.is_(True))
        if task_ids:
            q = q.where(ModelCall.task_id.in_(task_ids))
        else:
            q = q.where(ModelCall.request_id == request_id)
        return int(db.scalar(q) or 0)


def usage_credits(rule: dict, usage: dict) -> int:
    """把一次调用的用量按**模型服务端点费率**折算成积分（确定性整数运算）。

    providers 记录调用日志时调用它算「这一行值多少分」。各类分量分别向上
    取整后求和：token 不足 1 千按 1 千记、张/秒按实际数；单价为 0 的类别
    不参与（该服务没配这类价 = 这类消耗不计费）。
    """
    def _n(v) -> int:  # noqa: ANN001 容错：JSON 里可能是 None/字符串
        try:
            return max(0, int(float(v or 0)))
        except (TypeError, ValueError):
            return 0

    total = 0
    tk = _n(rule.get("credits_per_1k_tokens"))
    tokens = _n(usage.get("tokens"))
    if tk > 0 and tokens > 0:
        total += -(-tokens // 1000) * tk  # 向上取整的整数写法
    im = _n(rule.get("credits_per_image"))
    images = _n(usage.get("images"))
    if im > 0 and images > 0:
        total += images * im
    vs = _n(rule.get("credits_per_video_second"))
    seconds = _n(usage.get("video_seconds"))
    if vs > 0 and seconds > 0:
        total += seconds * vs
    return total


def settle_usage(txn_id: str, *, request_id: str = "",
                 task_ids: list[str] | None = None) -> int | None:
    """按实际调用结算预扣（**幂等**：settled_at 的 CAS 保证只结算一次）。

    流程（2026-10-09 口径：按调用情况计费）：汇总该范围的**行级积分**
    （model_calls.credits，记录时按所用模型服务端点费率算好）→ 与预扣对账：
    - 实际 < 预扣 → 退回差额（写退款流水，note 注明按调用结算）；
    - 实际 > 预扣 → 补扣差额（原子条件扣；余额不足时差额记账不收，
      留 ERROR 日志与流水备注——正常预扣押金应覆盖典型用量，这是异常路径）；
    - 实际 == 预扣 → 只落结算时间戳。
    返回实际积分（未结算/流水不存在返回 None）。
    """
    if not txn_id:
        return None
    now = _now()
    with SessionLocal() as db:
        # 幂等闸：只有把 settled_at 从 0 改成时间戳的那一次调用能继续。
        # **状态守卫**（评审抓到的时序）：已 refunded 的流水不得再结算——
        # 否则「先退款（请求失败）、后任务却成功」会对已退款流水重算差额
        # （撞 ref_txn_id 唯一约束或反向补扣用户余额）。
        n = db.execute(
            update(CreditTransaction)
            .where(CreditTransaction.id == txn_id,
                   CreditTransaction.settled_at == 0,
                   CreditTransaction.kind == TXN_CONSUME,
                   CreditTransaction.status.in_((TXN_PENDING, TXN_CONSUMED)))
            .values(settled_at=now)
        ).rowcount
        if n == 0:
            db.rollback()
            return None  # 已结算过、已退款（或流水不存在）
        txn = db.get(CreditTransaction, txn_id)
        # 结算即终态：原流水若是 pending（同步端点走同一函数结算时可能还是
        # pending）一并置 consumed——否则重启恢复会把「已结算的 pending」
        # 误当成崩溃遗留去退款（幂等防线之外的状态一致性）
        if txn.status == TXN_PENDING:
            txn.status = TXN_CONSUMED
        # 汇总范围：任务化优先按任务号；同步端点按请求号（回退流水自带的）
        actual = collect_credits(
            request_id="" if task_ids else (request_id or txn.request_id),
            task_ids=task_ids)
        pre = int(txn.price or 0)
        diff = pre - actual  # > 0 退差额；< 0 补扣
        note_parts = [
            f"按实际调用结算：实际 {actual} 分（所用模型服务费率之和），"
            f"预扣 {pre} 分",
        ]
        if diff > 0:
            db.execute(
                update(CreditAccount)
                .where(CreditAccount.user_id == txn.user_id)
                .values(balance=CreditAccount.balance + diff,
                        total_consumed=CreditAccount.total_consumed - diff,
                        updated_at=now)
            )
            balance = db.scalar(select(CreditAccount.balance).where(
                CreditAccount.user_id == txn.user_id))
            db.add(CreditTransaction(
                id=_uuid(), user_id=txn.user_id, kind=TXN_REFUND,
                amount=diff, balance_after=int(balance or 0), status="",
                endpoint=txn.endpoint, price=txn.price,
                project_id=txn.project_id, ref_txn_id=txn.id,
                note="；".join(note_parts) + "，退回差额",
                owner_user_id=txn.user_id, created_at=now,
            ))
        elif diff < 0:
            need = -diff
            n2 = db.execute(
                update(CreditAccount)
                .where(CreditAccount.user_id == txn.user_id,
                       CreditAccount.balance >= need)
                .values(balance=CreditAccount.balance - need,
                        total_consumed=CreditAccount.total_consumed + need,
                        updated_at=now)
            ).rowcount
            if n2 == 0:
                # 余额不足补扣失败：差额收不到（预扣额应覆盖典型用量，属异常路径）
                logger.error(
                    "按实际调用结算补扣失败（余额不足）：流水 %s 应补 %s 分",
                    txn_id, need)
                note_parts.append(f"差额 {need} 分补扣失败（余额不足）")
            else:
                balance = db.scalar(select(CreditAccount.balance).where(
                    CreditAccount.user_id == txn.user_id))
                db.add(CreditTransaction(
                    id=_uuid(), user_id=txn.user_id, kind=TXN_CONSUME,
                    amount=-need, balance_after=int(balance or 0),
                    status=TXN_CONSUMED, endpoint=txn.endpoint,
                    price=need, project_id=txn.project_id,
                    note="；".join(note_parts) + "，补扣差额",
                    owner_user_id=txn.user_id, created_at=now,
                ))
        db.commit()
        logger.info("按实际调用结算：流水 %s %s", txn_id, "；".join(note_parts))
        return actual


def settle_after_terminal(txn_id: str, *, fail_class: str = "") -> bool:
    """任务到达终态后的结算判定（33 号 §3.3；幂等，可被多个触发点重复调用）。

    规则（防「批量部分失败退全款白嫖」的关键）：
    1. 该流水关联的任务还有没到终态的 → 不动作（等最后一个到终态时再判）；
    2. 有 ≥1 个 done（部分成功）：
       - **按用量计费**（rule 快照 mode=per_usage）→ 按**成功任务的真实用量**
         结算（多退少补，见 settle_usage；失败任务的已发生用量按「失败不计费」
         原则不收，平台自担）；
       - 按次计费 → 计费成立不动（现状）。
    3. 全是 failed/canceled/cached（无任何 done）：
       - 单任务且失败类别为「远端已计费（billed）」→ 不退（平台成本已发生）；
       - 其余 → 全额退预扣。
    返回是否执行了**全额退款**（按用量结算另算，看流水的 settled_at）。
    """
    if not txn_id:
        return False
    from app.core.task_engine import TERMINAL_STATUSES  # 延迟导入破环
    from app.db.models import Task
    with SessionLocal() as db:
        txn = db.get(CreditTransaction, txn_id)
        if txn is None:
            return False
        # **提交窗口保护**（评审修正）：请求还没返回（流水仍 pending）时，
        # 批量端点的循环提交可能只建了一部分任务——此刻任何「全终态」判定
        # 都不可信（第一个任务极快 cached/失败会误退整笔预扣；done 结算会
        # 用不完整的成功集少算用量）。等 BillingGate 成功返回置 consumed 后
        # 的统一触发（main.py），或任务终态时（那时全部任务必已提交完）。
        if txn.status == TXN_PENDING:
            return False
        rows = db.execute(
            select(Task.status, Task.id).where(Task.billing_txn_id == txn_id)
        ).all()
    if not rows:
        return False  # 非任务化端点（同步 LLM 等）：结算走响应判定，不在任务侧
    if any(st not in TERMINAL_STATUSES for st, _ in rows):
        return False
    done_ids = [tid for st, tid in rows if st == "done"]
    if done_ids:
        # 有成功任务：按实际调用结算——成功任务名下的行级积分之和
        # （model_calls.credits，记录时按所用模型服务端点费率算好），
        # 与预扣多退少补（settle_usage 幂等）
        settle_usage(txn_id, task_ids=done_ids)
        return False  # 部分/全部成功：不走全退路径
    if len(rows) == 1 and fail_class == _fail_class_billed():
        return False  # 单任务 + 远端已计费：不退
    if not auto_refund_enabled():
        # 自动退款被管理员关闭：按「调用成立」收全款，流水收尾为 consumed
        # （不能悬挂在 pending——否则启动恢复会反复扫到它）
        mark_consumed(txn_id)
        return False
    return refund_txn(txn_id, reason="任务失败/取消自动退款"
                      + (f"（{fail_class}）" if fail_class else ""))


def _fail_class_billed() -> str:
    """「远端已计费」失败类别常量（延迟取 providers 的单一真源，防循环导入）。"""
    from app.core.providers import FailClass
    return FailClass.BILLED


# ---------- 充值 / 调整（管理员接口调用） ----------

def recharge(user_id: str, amount: int, *, operator_user_id: str = "",
             note: str = "") -> dict:
    """管理员充值：原子加余额 + 写充值流水（记操作人）。amount 必须为正。"""
    if amount <= 0:
        raise ValueError("充值金额必须为正整数")
    return _apply_delta(user_id, amount, kind=TXN_RECHARGE,
                        operator_user_id=operator_user_id,
                        note=note or "管理员充值", require_non_negative=False)


def adjust(user_id: str, amount: int, *, operator_user_id: str = "",
           note: str = "") -> dict:
    """管理员调整（纠错用）：± 均可；负调整同样不许把余额扣成负数。"""
    if amount == 0:
        raise ValueError("调整金额不能为 0")
    return _apply_delta(user_id, amount, kind=TXN_ADJUST,
                        operator_user_id=operator_user_id,
                        note=note or "管理员调整", require_non_negative=True)


def _apply_delta(user_id: str, amount: int, *, kind: str,
                 operator_user_id: str, note: str,
                 require_non_negative: bool) -> dict:
    """原子加余额 + 写流水（充值/调整共用；同一事务，防负数条件更新）。"""
    now = _now()
    with SessionLocal() as db:
        _ensure_account_in_session(db, user_id)
        cond = [CreditAccount.user_id == user_id]
        if require_non_negative and amount < 0:
            cond.append(CreditAccount.balance + amount >= 0)
        n = db.execute(
            update(CreditAccount).where(*cond).values(
                balance=CreditAccount.balance + amount,
                total_recharged=CreditAccount.total_recharged
                + (amount if amount > 0 and kind == TXN_RECHARGE else 0),
                updated_at=now)
        ).rowcount
        if n == 0:
            db.rollback()
            raise ValueError("余额不足：该调整会把余额扣成负数")
        balance = db.scalar(select(CreditAccount.balance).where(
            CreditAccount.user_id == user_id))
        db.add(CreditTransaction(
            id=_uuid(), user_id=user_id, kind=kind, amount=amount,
            balance_after=int(balance or 0), status="", note=note,
            operator_user_id=operator_user_id, owner_user_id=user_id,
            created_at=now,
        ))
        db.commit()
        return {"user_id": user_id, "balance": int(balance or 0),
                "delta": amount}


# ---------- 兑换码（管理员生成 / 用户核销） ----------

# 兑换失败限速（仿登录限速的进程内字典；当前单进程部署够用）
REDEEM_MAX_FAILS = 5
REDEEM_LOCK_SECONDS = 60
_redeem_fails: dict[str, list[float]] = {}

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去容易混淆的 I/O/0/1
_CODE_LEN = 16
_CODE_PREFIX = "WR"  # 鲸影（WhaleReel）前缀，便于识别


def _new_code() -> str:
    """生成高熵兑换码（16 位、去混淆字符表，约 79bit——防在线枚举）。"""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    return f"{_CODE_PREFIX}{body}"


def create_redeem_codes(credits: int, count: int, *, note: str = "",
                        created_by: str = "") -> list[str]:
    """批量生成兑换码（管理员）。面额与数量校验后一次性落库，返回码清单。"""
    if credits <= 0:
        raise ValueError("兑换码面额必须为正整数")
    if not 1 <= count <= 500:
        raise ValueError("单次生成数量需在 1-500 之间")
    now = _now()
    codes: list[str] = []
    with SessionLocal() as db:
        for _ in range(count):
            code = _new_code()
            db.add(RedeemCode(id=_uuid(), code=code, credits=credits,
                              note=note, created_by=created_by,
                              owner_user_id="", created_at=now))
            codes.append(code)
        db.commit()
    return codes


def redeem_code(user_id: str, code: str) -> dict:
    """核销兑换码（用户自助充值）。

    安全线：原子核销（WHERE used_by_user_id IS NULL + rowcount 判定，并发同码
    只有一个成功）→ 加余额 + 写充值流水，同一事务；失败限速防在线枚举。
    返回 {"ok": True, "credits", "balance"} 或 {"ok": False, "error"}。
    """
    norm = (code or "").strip().upper().replace(" ", "").replace("-", "")
    if not norm:
        return {"ok": False, "error": "请输入兑换码"}
    now = _now()
    # 失败限速：同名（用户）连续 5 次失败锁 60 秒
    fails = [t for t in _redeem_fails.get(user_id, []) if now - t < REDEEM_LOCK_SECONDS]
    if len(fails) >= REDEEM_MAX_FAILS:
        wait = REDEEM_LOCK_SECONDS - (now - fails[0])
        return {"ok": False,
                "error": f"尝试次数过多，请 {max(1, int(wait))} 秒后再试"}

    def _fail(msg: str) -> dict:
        fails.append(now)
        _redeem_fails[user_id] = fails
        return {"ok": False, "error": msg}

    with SessionLocal() as db:
        row = db.scalar(select(RedeemCode).where(RedeemCode.code == norm))
        if row is None:
            db.rollback()
            return _fail("兑换码不存在或已失效")
        n = db.execute(
            update(RedeemCode)
            .where(RedeemCode.id == row.id, RedeemCode.used_by_user_id.is_(None))
            .values(used_by_user_id=user_id, used_at=now)
        ).rowcount
        if n == 0:
            db.rollback()
            return _fail("该兑换码已被使用")
        _ensure_account_in_session(db, user_id)
        db.execute(
            update(CreditAccount)
            .where(CreditAccount.user_id == user_id)
            .values(balance=CreditAccount.balance + row.credits,
                    total_recharged=CreditAccount.total_recharged + row.credits,
                    updated_at=now)
        )
        balance = db.scalar(select(CreditAccount.balance).where(
            CreditAccount.user_id == user_id))
        db.add(CreditTransaction(
            id=_uuid(), user_id=user_id, kind=TXN_RECHARGE,
            amount=row.credits, balance_after=int(balance or 0), status="",
            note=f"兑换码充值 {norm}", owner_user_id=user_id, created_at=now,
        ))
        db.commit()
    _redeem_fails.pop(user_id, None)  # 成功即清零失败计数
    logger.info("兑换码核销：用户 %s 充入 %s 积分", user_id, row.credits)
    return {"ok": True, "credits": row.credits, "balance": int(balance or 0)}


# ---------- 对账（可观测的最终防线） ----------

def audit_consistency() -> dict:
    """全账户对账：`balance == SUM(流水 amount)`。

    返回 {"checked": 账户数, "bad": [{"user_id","balance","ledger_sum"}]}；
    不平的账户 ERROR 日志告警（人工介入——账本是钱的唯一真相）。
    """
    with SessionLocal() as db:
        accounts = db.scalars(select(CreditAccount)).all()
        bad: list[dict] = []
        for a in accounts:
            total = db.scalar(
                select(func.coalesce(func.sum(CreditTransaction.amount), 0))
                .where(CreditTransaction.user_id == a.user_id))
            if int(total or 0) != a.balance:
                bad.append({"user_id": a.user_id, "balance": a.balance,
                            "ledger_sum": int(total or 0)})
    if bad:
        logger.error("计费对账不平：%s 个账户（余额与流水汇总不一致）：%s",
                     len(bad), bad[:10])
    return {"checked": len(accounts), "bad": bad}


def recover_pending_transactions() -> int:
    """服务启动时处理上次崩溃遗留的 pending 流水（33 号 §3.2 第 4 条）。

    在 `engine.recover()` **之后**调用（三分法依赖任务的最新状态）：
    - 关联任务仍非终态（recover 已重新入队、将真实执行）→ 置 consumed（不退款）；
    - 关联任务全终态 → 走结算判定 settle_after_terminal；
    - 无关联任务（业务没发生，或非任务化端点）→ 全额退款。
    绝不出现「任务重新跑了、钱也退了」的白嫖窗口。返回处理的笔数。
    """
    from app.core.task_engine import TERMINAL_STATUSES  # 延迟导入破环
    from app.db.models import Task
    with SessionLocal() as db:
        pendings = db.scalars(
            select(CreditTransaction).where(
                CreditTransaction.kind == TXN_CONSUME,
                CreditTransaction.status == TXN_PENDING,
                # 已结算（settled_at>0）的不再是「遗留」——双保险
                CreditTransaction.settled_at == 0)
        ).all()
    handled = 0
    for t in pendings:
        handled += 1
        with SessionLocal() as db:
            rows = db.execute(
                select(Task.status).where(Task.billing_txn_id == t.id)
            ).all()
        statuses = [st for (st,) in rows]
        if statuses and any(st not in TERMINAL_STATUSES for st in statuses):
            mark_consumed(t.id)
            logger.warning("重启恢复：流水 %s 的关联任务仍在执行，计费置为成立",
                           t.id)
        elif statuses:
            # 崩溃遗留的 pending 先收尾（请求已死，计费成立与否交给结算判定；
            # 提交窗口保护只对「进行中的请求」设防，启动时不存在活跃请求）
            mark_consumed(t.id)
            settle_after_terminal(t.id)  # 任务全终态：同一结算判定（含开关）
        else:
            if auto_refund_enabled():
                refund_txn(t.id, reason="服务重启，业务未发生，自动退款")
            else:
                mark_consumed(t.id)  # 自动退款关闭：按调用成立收全款并收尾
    if handled:
        logger.warning("重启恢复：处理了 %s 笔未结算的消费流水", handled)
    return handled


# ---------- 价目：规则缓存 / 匹配 / 同步 / 建议价目 ----------

# ---------- 计费闸清单（2026-10-09 用户口径定稿） ----------
#
# **计费配置分两层，各自只有一个来源**：
# ① 费率（每个模型服务端点多少钱）→ provider_endpoints 表，在「模型管理」页
#    按服务配置（方舟/Agnes/…）——见 providers.endpoint_rates；
# ② 预扣（发起前先押一笔、完成后按实际调用多退少补的押金）→ 本清单内置，
#    **无需配置**（2026-10-09 用户拍板「系统内置默认」）。
#
# 本清单 = 会产生大模型调用的 HTTP 接口（新增烧钱接口时在此登记一行）；
# 系统平台接口（保存/编辑/查询/管理/认证）不在清单里 = 免费放行。
# 键与 FastAPI 路由模板**逐字一致**；值为预扣积分（按各类接口的典型用量估）。
GATED_ENDPOINTS: dict[tuple[str, str], int] = {
    ("POST", "/api/projects/{project_id}/script/parse"): 60,
    ("POST", "/api/episodes/{episode_id}/build"): 40,
    ("POST", "/api/projects/{project_id}/episodes/replan"): 40,
    ("POST", "/api/episodes/{episode_id}/audit"): 20,
    ("POST", "/api/episodes/{episode_id}/assets"): 20,
    ("POST", "/api/episodes/{episode_id}/optimize"): 20,
    ("POST", "/api/episodes/{episode_id}/auto-review"): 40,
    ("POST", "/api/episodes/{episode_id}/shots/generate"): 50,
    ("POST", "/api/characters/{character_id}/images"): 20,
    ("POST", "/api/characters/{character_id}/images/fourview"): 5,
    ("POST", "/api/projects/{project_id}/costume-suggestions/{suggestion_id}/accept"): 5,
    ("POST", "/api/scenes/{scene_id}/generate"): 20,
    ("POST", "/api/scenes/{scene_id}/extra-view"): 12,
    ("POST", "/api/tasks"): 30,
    ("POST", "/api/props/{prop_id}/generate"): 5,
    ("POST", "/api/projects/{project_id}/script/expand"): 15,
    ("POST", "/api/projects/{project_id}/prompts/optimize"): 10,
    ("POST", "/api/shots/{shot_id}/regenerate-video-prompt"): 10,
    ("POST", "/api/sessions/{session_id}/chat"): 10,
}

# 接口中文名（「我的积分」等展示与接口清单用；键同上）
GATED_ENDPOINT_LABELS: dict[tuple[str, str], str] = {
    ("POST", "/api/projects/{project_id}/script/parse"): "剧本解析",
    ("POST", "/api/episodes/{episode_id}/build"): "单集制作（生成本集分镜）",
    ("POST", "/api/projects/{project_id}/episodes/replan"): "按剧情重新分集",
    ("POST", "/api/episodes/{episode_id}/audit"): "单集剧情审核",
    ("POST", "/api/episodes/{episode_id}/assets"): "本集资产解析",
    ("POST", "/api/episodes/{episode_id}/optimize"): "单集一键修复",
    ("POST", "/api/episodes/{episode_id}/auto-review"): "AI 审片（多模态判级）",
    ("POST", "/api/episodes/{episode_id}/shots/generate"): "分镜首帧/视频生成",
    ("POST", "/api/characters/{character_id}/images"): "角色定妆图/妆造",
    ("POST", "/api/characters/{character_id}/images/fourview"): "角色三视图",
    ("POST", "/api/projects/{project_id}/costume-suggestions/{suggestion_id}/accept"): "接受妆造建议并生成",
    ("POST", "/api/scenes/{scene_id}/generate"): "场景主图",
    ("POST", "/api/scenes/{scene_id}/extra-view"): "场景补充视角",
    ("POST", "/api/tasks"): "画布生成任务",
    ("POST", "/api/props/{prop_id}/generate"): "道具定妆图",
    ("POST", "/api/projects/{project_id}/script/expand"): "一句话梗概扩写",
    ("POST", "/api/projects/{project_id}/prompts/optimize"): "AI 优化提示词",
    ("POST", "/api/shots/{shot_id}/regenerate-video-prompt"): "画面描述重算",
    ("POST", "/api/sessions/{session_id}/chat"): "Agent 对话",
}

# 模型服务端点的建议费率（「模型管理」页「应用建议费率」按钮用；按服务类型给）：
# llm 按千 token、t2i 按张、t2v/i2v 按秒——基准 1 分/千 token、5 分/张、3 分/秒，
# 管理员按实际成本在端点里自行调整（本地推理端点不需要费率）。
SUGGESTED_ENDPOINT_PRICES: dict[str, dict[str, int]] = {
    "llm": {"credits_per_1k_tokens": 1},
    "t2i": {"credits_per_image": 5},
    "t2v": {"credits_per_video_second": 3},
    "i2v": {"credits_per_video_second": 3},
    # tts：按次计费的量纲还没有（调用日志暂不记字符数），暂不给建议值
}




# 计费闸清单的编译正则（模块级惰性编译一次；清单是代码常量，无失效问题）
_gate_patterns: list[tuple] | None = None


def _ensure_gate_patterns() -> None:
    """把 GATED_ENDPOINTS 编译成锚定正则（首次调用时编译一次）。"""
    global _gate_patterns
    if _gate_patterns is not None:
        return
    import re
    entries = []
    for (method, tpl), precharge in GATED_ENDPOINTS.items():
        # 模板转锚定正则（{param} 段匹配任意非斜杠内容；与 FastAPI 同款口径）
        pattern = "^" + re.sub(r"\{[^/{}]+\}", r"[^/]+", tpl) + "$"
        entries.append((re.compile(pattern), f"{method} {tpl}",
                        int(precharge)))
    _gate_patterns = entries


def match_rule(method: str, path: str) -> tuple[str, int] | None:
    """按「方法 + 请求路径」匹配计费闸清单；命中返回 (接口键, 预扣积分)。

    清单 = GATED_ENDPOINTS（内置 19 个大模型调用接口，预扣额内置默认、
    无需配置）；预扣只是发起时的押金——实际扣费按「模型服务端点费率 ×
    真实调用记录」结算（多退少补）。未命中 = 免费放行（系统平台接口
    都走这里，2026-10-09 用户口径）。
    """
    _ensure_gate_patterns()
    for pattern, key, precharge in _gate_patterns or []:
        if pattern.match(path):
            return key, precharge
    return None


def pricing_list() -> list[dict]:
    """计费闸清单的接口视图（前端按钮「预扣 N」徽标的数据源）。"""
    return [
        {"key": f"{m} {p}", "method": m, "path_template": p,
         "precharge": int(pre), "label": GATED_ENDPOINT_LABELS.get((m, p), "")}
        for (m, p), pre in GATED_ENDPOINTS.items()
    ]


def verify_gated_endpoints(app) -> list[str]:  # noqa: ANN001
    """启动自检：GATED_ENDPOINTS 里的路由模板是否都真实存在（防开发改路由
    忘改清单——漏配的后果是「该接口整个免费」）。只日志告警不拦启动；
    测试 test_billing 里有同一断言的守护用例。返回缺失清单（测试用）。"""
    from app.core.routes import collect_route_entries
    known = set(collect_route_entries(app))
    missing = [f"{m} {p}" for (m, p) in GATED_ENDPOINTS if (m, p) not in known]
    if missing:
        logger.warning("计费闸清单里 %s 个接口不在路由表（路径写错或路由已删？）：%s",
                       len(missing), missing[:5])
    return missing


def apply_suggested_endpoint_prices(*, overwrite: bool = False) -> int:
    """「模型管理」页按钮：给各模型服务端点填入建议费率（按服务类型）。

    建议值见 SUGGESTED_ENDPOINT_PRICES（llm 1 分/千 token、t2i 5 分/张、
    t2v/i2v 3 分/秒）；管理员在各端点里按实际成本再改。
    overwrite=False（默认）：只填**完全未配置**（三单价全 0）的端点——
    不覆盖已手调的值；overwrite=True：全部按建议值重写。
    返回更新端点数。
    """
    from app.db.models import ProviderEndpoint
    now = _now()
    updated = 0
    with SessionLocal() as db:
        for row in db.scalars(select(ProviderEndpoint)).all():
            spec = SUGGESTED_ENDPOINT_PRICES.get(row.kind)
            if not spec:
                continue  # 该类型暂无建议值（如 tts）
            if not overwrite and any((getattr(row, f) or 0) > 0 for f in (
                    "credits_per_1k_tokens", "credits_per_image",
                    "credits_per_video_second")):
                continue  # 已配置过，不覆盖
            for field, value in spec.items():
                setattr(row, field, int(value))
            row.bill_enabled = True
            row.updated_at = now
            updated += 1
        db.commit()
    # 让 providers 的费率缓存即时失效（改价立即生效；多进程时靠 TTL 兜底）
    try:
        from app.core import providers
        providers.invalidate_endpoint_rates()
    except Exception:  # noqa: BLE001 失效失败不致命（TTL 兜底）
        logger.exception("模型端点费率缓存失效失败（TTL 兜底）")
    logger.info("模型服务费率：已应用建议费率 %s 个端点（overwrite=%s）",
                updated, overwrite)
    return updated
