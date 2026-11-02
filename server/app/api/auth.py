"""用户体系 API：登录 / 登出 / 当前用户 / 改密 / 账号管理（仅管理员）。

鉴权方式：Authorization: Bearer <token>（登录签发，持久化于 auth_tokens 表）。
依赖注入：get_current_user（登录用户）、require_admin（管理员，403 拦截）——
系统设置类 API 经 dependencies=[Depends(require_admin)] 挂门禁。

**全局鉴权（2026-09-30）**：业务接口不再逐个挂依赖，改由 main.py 的
`AuthGateMiddleware` 统一拦（白名单只有登录/健康检查/媒体文件），本模块提供
`try_resolve_token` 给它用；`get_current_user` / `require_admin` 继续给需要
「拿到用户是谁」的端点用。
"""

import re
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.security import (
    DEFAULT_ADMIN_PASSWORD,
    PASSWORD_MIN_LENGTH, TOKEN_TTL_SECONDS, USERNAME_PATTERN,
    hash_password, new_token, verify_password,
)
from app.db.models import AuthToken, User
from app.db.session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 用户名形态校验（2-32 位中文/字母/数字/下划线/连字符）
_USERNAME_RE = re.compile(USERNAME_PATTERN)

# 登录失败限速（2026-09-30）：防止默认口令被在线穷举。按「用户名」计数，
# 5 次失败后锁 60 秒——单机场景够用，且不会因为别人扫端口把正常用户锁死很久。
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 60
_login_fails: dict[str, list[float]] = {}


def _now() -> int:
    return int(time.time())


# 「是否仍用出厂默认口令」的计算缓存：PBKDF2 校验一次约几十毫秒，不能每个请求
# 都算（服务端强制改密要对每个请求生效）。键=用户 id，值=(password_hash, 判定)。
# 散列一变（改密/重置）缓存自然失效，不需要手工清理；进程重启重算一次。
_default_pw_cache: dict[str, tuple[str, bool]] = {}


def still_default_password(u: User) -> bool:
    """该用户是否仍在用出厂默认口令（服务端强制改密判据，2026-09-30 补）。"""
    digest = str(getattr(u, "password_hash", "") or "")
    uid = str(getattr(u, "id", "") or "")
    cached = _default_pw_cache.get(uid)
    if cached is not None and cached[0] == digest:
        return cached[1]
    result = bool(digest) and verify_password(DEFAULT_ADMIN_PASSWORD, digest)
    if uid:
        _default_pw_cache[uid] = (digest, result)
    return result


def _user_dict(u: User) -> dict:
    """用户行的 API 响应结构（不含散列）。

    `must_change_password`：仍是出厂默认口令时置真，前端据此把用户挡在改密页
    （2026-09-30 首登强制改密）；**服务端同样会拦**（见 main.py 的 AuthGate），
    不能只靠前端——旧实现 curl 拿着默认口令换来的令牌可以绕过改密页直接用全部
    接口（2026-09-30 复查 S10：这条此前是「纯前端软引导」，措辞夸大）。
    """
    must_change = still_default_password(u)
    return {
        "id": u.id, "username": u.username, "is_admin": u.is_admin,
        "disabled": u.disabled, "created_at": u.created_at,
        "last_login_at": u.last_login_at,
        "must_change_password": bool(must_change),
    }


def _resolve_token(authorization: str, db: Session) -> tuple[str, User]:
    """解析 Bearer 令牌 → (令牌串, 用户)。无效/过期/禁用一律 401。"""
    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise HTTPException(401, "未登录")
    row = db.get(AuthToken, token)
    if not row or row.expires_at < _now():
        raise HTTPException(401, "登录已过期，请重新登录")
    user = db.get(User, row.user_id)
    if not user or user.disabled:
        raise HTTPException(401, "账号不可用")
    return token, user


def try_resolve_token(authorization: str, db: Session) -> User | None:
    """尝试解析令牌：有效返回用户，其余（空令牌/过期/禁用）返回 None。

    供**全局鉴权中间件**使用——它要自己决定返回什么状态码与文案，而不是让
    依赖抛异常（中间件层拿不到依赖的异常语义）。
    """
    try:
        return _resolve_token(authorization or "", db)[1]
    except HTTPException:
        return None


def get_current_user(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> User:
    """当前登录用户依赖（未登录 401）。"""
    return _resolve_token(authorization, db)[1]


def require_admin(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> User:
    """管理员门禁依赖：未登录 401，非管理员 403。"""
    _token, user = _resolve_token(authorization, db)
    if not user.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return user


def get_optional_user(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> User | None:
    """当前登录用户或 None（**不抛 401**）。

    用途：同一个端点对不同身份返回不同详略的字段（如端点列表对管理员附
    `key_id`、对普通用户不含），而不是把整个端点封死——封死会让普通用户的
    模型下拉拿不到任何端点选项。
    """
    if not (authorization or "").strip():
        return None
    try:
        return _resolve_token(authorization, db)[1]
    except HTTPException:
        return None


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


class UserCreateIn(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserPatchIn(BaseModel):
    password: str | None = None  # 提供即重置密码
    is_admin: bool | None = None
    disabled: bool | None = None


def _validate_credentials(username: str, password: str) -> tuple[str, str]:
    """账号管理入参校验：用户名形态 + 密码最短长度（strip 后返回）。"""
    username = username.strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(
            400, f"无效用户名：{username}（2-32 位中文/字母/数字/下划线/连字符）")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(400, f"密码至少 {PASSWORD_MIN_LENGTH} 位")
    return username, password


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    """登录：校验账密 → 签发令牌（过期令牌顺手清理）。

    **失败限速（2026-09-30）**：同一用户名连续失败 5 次锁 60 秒。出厂默认口令是
    `admin/admin123`，没有限速时在线穷举是分钟级的事。
    """
    username = body.username.strip()
    now = _now()
    fails = [t for t in _login_fails.get(username, []) if now - t < LOGIN_LOCK_SECONDS]
    if len(fails) >= LOGIN_MAX_FAILS:
        wait = LOGIN_LOCK_SECONDS - (now - fails[0])
        raise HTTPException(
            429, f"登录失败次数过多，请 {max(1, int(wait))} 秒后重试")
    user = db.scalar(select(User).where(User.username == username))
    if not user or not verify_password(body.password, user.password_hash):
        fails.append(now)
        _login_fails[username] = fails
        raise HTTPException(401, "用户名或密码错误")
    if user.disabled:
        raise HTTPException(403, "账号已禁用，请联系管理员")
    _login_fails.pop(username, None)
    # 签发新令牌并清理全部过期令牌（防 token 表无限增长）
    token = AuthToken(token=new_token(), user_id=user.id,
                      created_at=now, expires_at=now + TOKEN_TTL_SECONDS)
    db.add(token)
    db.execute(delete(AuthToken).where(AuthToken.expires_at < now))
    user.last_login_at = now
    db.commit()
    return {"token": token.token, "expires_at": token.expires_at,
            "user": _user_dict(user)}


@router.post("/logout")
def logout(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """登出：删除当前令牌（其余端不受影响）。"""
    token, _user = _resolve_token(authorization, db)
    db.delete(db.get(AuthToken, token))
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    """当前登录用户信息。"""
    return _user_dict(user)


@router.post("/change-password")
def change_password(
    body: PasswordChangeIn,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """修改自己的密码：校验旧密码，成功后吊销其他端令牌（当前端保留）。"""
    _token, user = _resolve_token(authorization, db)
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(400, "旧密码不正确")
    if len(body.new_password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(400, f"新密码至少 {PASSWORD_MIN_LENGTH} 位")
    user.password_hash = hash_password(body.new_password)
    db.execute(delete(AuthToken).where(
        AuthToken.user_id == user.id, AuthToken.token != _token))
    db.commit()
    return {"ok": True}


# ---------- 账号管理（仅管理员） ----------

@router.get("/users")
def list_users(_admin: User = Depends(require_admin),
               db: Session = Depends(get_db)):
    """用户列表（管理页数据源）。"""
    users = db.scalars(select(User).order_by(User.created_at)).all()
    return [_user_dict(u) for u in users]


@router.post("/users", status_code=201)
def create_user(body: UserCreateIn, _admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    """新建用户（默认普通成员，可勾选管理员）。

    首个用户创建时顺带**认领无归属项目**（多租户，2026-09-30）：单机场景常见
    「先用了库、后建账号」，不认领的话新用户看不到自己原来的项目。只在库里
    此前没有任何用户时发生，所以不存在抢别人项目的问题。
    """
    from app.db.session import claim_orphan_projects

    username, password = _validate_credentials(body.username, body.password)
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(400, f"用户名已存在：{username}")
    first_user = db.scalar(select(User.id)) is None
    user = User(username=username, password_hash=hash_password(password),
                is_admin=body.is_admin)
    db.add(user)
    db.flush()
    if first_user:
        claim_orphan_projects(db, user.id)
    db.commit()
    return _user_dict(user)


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatchIn,
               admin: User = Depends(require_admin),
               db: Session = Depends(get_db)):
    """更新用户：重置密码 / 管理员标记 / 禁用。禁止自我降权与自禁（防锁死）。"""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    # 自我保护：唯一管理员不可自摘权限/自禁/自删，否则系统失去管理入口
    if user.id == admin.id:
        if body.is_admin is False:
            raise HTTPException(400, "不能取消自己的管理员权限")
        if body.disabled:
            raise HTTPException(400, "不能禁用自己的账号")
    if body.password is not None:
        if len(body.password) < PASSWORD_MIN_LENGTH:
            raise HTTPException(400, f"密码至少 {PASSWORD_MIN_LENGTH} 位")
        user.password_hash = hash_password(body.password)
        # 重置密码后吊销该用户全部令牌（强制重新登录）
        db.execute(delete(AuthToken).where(AuthToken.user_id == user.id))
    if body.is_admin is not None:
        user.is_admin = body.is_admin
    if body.disabled is not None:
        user.disabled = body.disabled
        if user.disabled:
            db.execute(delete(AuthToken).where(AuthToken.user_id == user.id))
    db.commit()
    return _user_dict(user)


@router.delete("/users/{user_id}")
def delete_user(user_id: str, admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    """删除用户（级联删除其令牌）。禁止删除自己。"""
    if user_id == admin.id:
        raise HTTPException(400, "不能删除自己的账号")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    db.execute(delete(AuthToken).where(AuthToken.user_id == user_id))
    db.delete(user)
    db.commit()
    return {"ok": True}
