"""积分计费 API（33 号方案）：账户 / 流水 / 价目 / 充值 / 兑换码 / 对账。

权限划分（硬性）：
- 用户侧（登录即可）：只能看**自己**的账户与流水（查询只用当前用户 id，
  不接受任意 user_id 参数——天然无越权面）；兑换码核销也是记在自己名下。
- 管理员侧（require_admin）：价目编辑、全局配置、给他人充值/调整、兑换码生成、
  全局流水审计、对账。

记账全部在 core/billing.py 里完成（原子扣减/幂等退款/显式事务），本文件只做
参数校验与查询组织。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.auth import get_current_user, require_admin
from app.core import billing
from app.db.models import CreditTransaction, RedeemCode, User
from app.db.session import get_db

router = APIRouter(prefix="/api/billing", tags=["billing"])

# 流水分页上限（防一次拉全表）
TXN_PAGE_MAX = 200
TXN_PAGE_DEFAULT = 50


def _txn_dict(t, username: str = "") -> dict:  # noqa: ANN001
    """流水行的 API 结构（username 仅管理员视图带出）。"""
    return {
        "id": t.id, "user_id": t.user_id, "username": username,
        "kind": t.kind, "amount": t.amount, "balance_after": t.balance_after,
        "status": t.status, "endpoint": t.endpoint, "price": t.price,
        "project_id": t.project_id, "task_id": t.task_id,
        "ref_txn_id": t.ref_txn_id, "note": t.note,
        "operator_user_id": t.operator_user_id, "created_at": t.created_at,
    }


# ---------- 用户侧（登录即可；只涉及自己） ----------

@router.get("/account")
def my_account(user: User = Depends(get_current_user)):
    """我的积分账户（余额/累计充值/累计消费；顶栏与积分页用）。"""
    return billing.account_dict(user.id)


@router.get("/transactions")
def my_transactions(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=TXN_PAGE_DEFAULT, ge=1, le=TXN_PAGE_MAX),
    kind: str = Query(default=""),
    user: User = Depends(get_current_user),
    db=Depends(get_db),  # noqa: ANN001
):
    """我的积分明细（分页；可按类型过滤）。"""
    cond = [CreditTransaction.user_id == user.id]
    if kind:
        cond.append(CreditTransaction.kind == kind)
    total = db.scalar(select(func.count()).select_from(CreditTransaction)
                      .where(*cond)) or 0
    rows = db.scalars(
        select(CreditTransaction).where(*cond)
        # 同秒多条时用 id 兜底排序，保证「最新一笔」稳定在列表头部
        .order_by(CreditTransaction.created_at.desc(),
                  CreditTransaction.id.desc())
        .offset(offset).limit(limit)
    ).all()
    return {"ok": True, "total": int(total), "offset": offset, "limit": limit,
            "items": [_txn_dict(t) for t in rows]}


@router.get("/pricing")
def pricing(_user: User = Depends(get_current_user)):
    """计费闸清单（**前端按钮「预扣 N」徽标的唯一数据源**）。

    返回会产生模型调用的接口及其**预扣积分**（发起时的押金；实际扣费按
    「所用模型服务端点的费率 × 真实调用」结算、多退少补——费率在「模型管理」
    页按服务配置）。清单内置、无需配置；未列出的接口即免费（系统平台接口）。
    """
    return {"ok": True, "rules": billing.pricing_list()}


class RedeemIn(BaseModel):
    code: str


@router.post("/redeem")
def redeem(body: RedeemIn, user: User = Depends(get_current_user)):
    """兑换码充值（核销+加余额+写流水，同一事务；失败限速防枚举）。"""
    result = billing.redeem_code(user.id, body.code)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "兑换失败")
    return result


# ---------- 管理员侧（require_admin） ----------

class SettingsPatchIn(BaseModel):
    enabled: bool | None = None
    signup_grant: int | None = None
    auto_refund: bool | None = None
    admin_free: bool | None = None


@router.get("/settings")
def get_billing_settings(_admin: User = Depends(require_admin)):
    """计费全局配置。"""
    return {"ok": True, "settings": billing.get_settings()}


@router.patch("/settings")
def patch_billing_settings(body: SettingsPatchIn,
                           _admin: User = Depends(require_admin)):
    """保存计费全局配置（只写提供的键，即时生效）。"""
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        saved = billing.update_settings(patch)
    except ValueError as e:  # 参数校验类错误转 400
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "settings": saved}


class RechargeIn(BaseModel):
    user_id: str
    amount: int
    note: str = ""


@router.post("/recharge")
def recharge(body: RechargeIn, admin: User = Depends(require_admin),
             db=Depends(get_db)):  # noqa: ANN001
    """给指定用户充值（记操作人；界面带确认框防误触）。"""
    if db.get(User, body.user_id) is None:
        raise HTTPException(404, "用户不存在")
    try:
        result = billing.recharge(body.user_id, body.amount,
                                  operator_user_id=admin.id,
                                  note=body.note or f"管理员 {admin.username} 充值")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, **result}


class AdjustIn(BaseModel):
    user_id: str
    amount: int  # ± 均可；负调整不许把余额扣成负数
    note: str = ""


@router.post("/adjust")
def adjust(body: AdjustIn, admin: User = Depends(require_admin),
           db=Depends(get_db)):  # noqa: ANN001
    """调整用户余额（纠错用；amount 可正可负，负调整防负数）。"""
    if db.get(User, body.user_id) is None:
        raise HTTPException(404, "用户不存在")
    try:
        result = billing.adjust(body.user_id, body.amount,
                                operator_user_id=admin.id,
                                note=body.note or f"管理员 {admin.username} 调整")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, **result}


class RedeemCodesIn(BaseModel):
    credits: int
    count: int
    note: str = ""


@router.post("/redeem-codes")
def create_redeem_codes(body: RedeemCodesIn, admin: User = Depends(require_admin)):
    """批量生成兑换码（一次 1-500 张；返回码清单，仅此一次展示方便复制发放）。"""
    try:
        codes = billing.create_redeem_codes(body.credits, body.count,
                                            note=body.note, created_by=admin.id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "codes": codes}


@router.get("/redeem-codes")
def list_redeem_codes(
    status: str = Query(default="", description="unused=未使用 / used=已使用"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=TXN_PAGE_DEFAULT, ge=1, le=TXN_PAGE_MAX),
    _admin: User = Depends(require_admin),
    db=Depends(get_db),  # noqa: ANN001
):
    """兑换码列表（含使用状态与使用者）。"""
    cond = []
    if status == "unused":
        cond.append(RedeemCode.used_by_user_id.is_(None))
    elif status == "used":
        cond.append(RedeemCode.used_by_user_id.is_not(None))
    total = db.scalar(select(func.count()).select_from(RedeemCode)
                      .where(*cond)) or 0
    rows = db.scalars(select(RedeemCode).where(*cond)
                      .order_by(RedeemCode.created_at.desc())
                      .offset(offset).limit(limit)).all()
    # 使用者用户名联查（列表要显示「谁用的」）
    uids = {r.used_by_user_id for r in rows if r.used_by_user_id}
    names = dict(db.execute(select(User.id, User.username)
                            .where(User.id.in_(uids or {""}))).all())
    items = [{
        "id": r.id, "code": r.code, "credits": r.credits, "note": r.note,
        "used_by_user_id": r.used_by_user_id,
        "used_by_username": names.get(r.used_by_user_id or "", ""),
        "used_at": r.used_at, "created_by": r.created_by,
        "created_at": r.created_at,
    } for r in rows]
    return {"ok": True, "total": int(total), "offset": offset, "limit": limit,
            "items": items}


@router.get("/accounts")
def list_accounts(_admin: User = Depends(require_admin), db=Depends(get_db)):  # noqa: ANN001
    """全部用户账户概览（账号管理页余额列 / 计费页用户区用）。

    以 users 为主表左联账户——没发生过积分往来的用户余额显示 0（不等于没账户）。
    """
    from app.db.models import CreditAccount
    rows = db.execute(
        select(User.id, User.username, User.is_admin,
               func.coalesce(CreditAccount.balance, 0),
               func.coalesce(CreditAccount.total_recharged, 0),
               func.coalesce(CreditAccount.total_consumed, 0))
        .join(CreditAccount, CreditAccount.user_id == User.id, isouter=True)
        .order_by(User.created_at)
    ).all()
    return {"ok": True, "accounts": [
        {"user_id": uid, "username": name, "is_admin": bool(is_admin),
         "balance": int(bal), "total_recharged": int(rec),
         "total_consumed": int(cons)}
        for uid, name, is_admin, bal, rec, cons in rows
    ]}


@router.get("/transactions/all")
def all_transactions(
    user_id: str = Query(default=""),
    kind: str = Query(default=""),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=TXN_PAGE_DEFAULT, ge=1, le=TXN_PAGE_MAX),
    _admin: User = Depends(require_admin),
    db=Depends(get_db),  # noqa: ANN001
):
    """全局流水（审计）：可按用户/类型过滤。"""
    cond = []
    if user_id:
        cond.append(CreditTransaction.user_id == user_id)
    if kind:
        cond.append(CreditTransaction.kind == kind)
    total = db.scalar(select(func.count()).select_from(CreditTransaction)
                      .where(*cond)) or 0
    rows = db.scalars(select(CreditTransaction).where(*cond)
                      .order_by(CreditTransaction.created_at.desc())
                      .offset(offset).limit(limit)).all()
    uids = {t.user_id for t in rows}
    names = dict(db.execute(select(User.id, User.username)
                            .where(User.id.in_(uids or {""}))).all())
    return {"ok": True, "total": int(total), "offset": offset, "limit": limit,
            "items": [_txn_dict(t, names.get(t.user_id, "")) for t in rows]}


@router.post("/audit")
def run_audit(_admin: User = Depends(require_admin)):
    """手动触发对账（balance == 流水汇总；不平的账户 ERROR 日志 + 原样返回）。"""
    return {"ok": True, **billing.audit_consistency()}
