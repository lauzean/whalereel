"""多租户归属校验（2026-09-30 第 1 层）。

**目标**：普通用户只能看到/操作自己的项目；管理员可看全部。默认拒绝由
main.py 的 `auth_gate` 中间件负责（业务接口必须带有效令牌），本模块负责
**第二层**——「你有令牌，但这个项目不是你的」。

**为什么用 404 而不是 403**：403 等于告诉对方「这个 id 是存在的」，
枚举一遍就能摸清别人有多少项目。项目内数据（分镜/角色/任务/产物）同理，
一律按不存在处理。

**为什么写在函数里而不是每个端点挂依赖**：端点里几乎都已经 `db.get(Project, id)`
了，就地校验一处即可；挂依赖反而会出现「同一份判定散落两处」的老问题
（审计 14 号报告里 6 条严重缺陷的共同形态）。新端点漏校验的风险由中间件兜底
（至少挡匿名），管理员场景不受影响。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, NodeVersion, Project, User
from app.db.session import get_db


def is_admin(request: Request | None) -> bool:
    """当前请求是否管理员（中间件已把结论放进 request.state）。"""
    return bool(getattr(getattr(request, "state", None), "is_admin", False))


def current_user_id(request: Request | None) -> str:
    """当前请求的用户 id（未登录为空串；中间件已拦，这里只做读取）。"""
    return str(getattr(getattr(request, "state", None), "user_id", "") or "")


def _identity(request: Request | None, user_id: str | None,
              admin: bool | None) -> tuple[str, bool]:
    """解析调用者身份（显式传参优先；否则从 request.state 读）。

    `request is None` 表示**没有请求上下文**（自检脚本直接调用端点函数）：
    那条路径不经过网络，不存在越权面，按「放行」处理——由调用方
    （ensure_* 系列）识别并短路。
    """
    if admin is None:
        admin = is_admin(request)
    if user_id is None:
        user_id = current_user_id(request)
    return user_id, bool(admin)


def _no_request_context(request: Request | None) -> bool:
    """是否为「直调」场景（无 request，也没有显式传身份）。"""
    return request is None


def owned_project_ids(db: Session, user_id: str) -> list[str]:
    """该用户名下的全部项目 id。"""
    return list(db.scalars(
        select(Project.id).where(Project.owner_user_id == user_id)).all())


def ensure_project_access(db: Session, project_id: str, request: Request | None,
                          *, need: str = "read",
                          user_id: str | None = None,
                          admin: bool | None = None) -> Project:
    """取项目并校验归属：无权限时按 404「项目不存在」处理。

    need="read" 允许管理员与归属者；need="write" 目前语义相同（本项目没有只读
    共享的概念），保留参数是为了后续区分——避免调用点写死后又要改一遍。

    身份来源三选一：① 常规 HTTP 请求（从 `request.state` 读中间件放好的结论）；
    ② **显式传 `user_id` / `admin`**——WebSocket 场景没有 HTTP 中间件，那条路径
    自己解析令牌后直接传进来；③ `request is None` 且未显式传身份 = **直调场景**
    （自检脚本按函数调用端点），无网络越权面，放行。
    """
    if _no_request_context(request) and user_id is None and admin is None:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        return project
    if admin is None:
        admin = is_admin(request)
    if user_id is None:
        user_id = current_user_id(request)
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    if admin:
        return project
    if not owner_matches(project, user_id):
        raise HTTPException(404, "项目不存在")
    return project


def owner_matches(row, user_id: str) -> bool:
    """权限归属判定（2026-09-30 两层模型）：**一步直达，只看 owner_user_id**。

    这是全项目唯一的归属比较函数——所有资源鉴权都走它，避免「同一份判定散落多处」。

    约定：
    - `owner_user_id` 有值 → 必须与调用者完全相等（管理员在调用方另行放行）；
    - 空串 → 无归属（如全局角色、历史遗留行）：**谁都不算主人**，只有管理员可见；
    - `UNASSIGNED_OWNER`（查不出归属的旧产物）→ 同上，只有管理员可见（用户拍板 A）。
    """
    owner = (getattr(row, "owner_user_id", "") or "")
    return bool(user_id) and owner == user_id


def set_owner_from_project(db: Session, row, project_id: str) -> None:
    """落库前给「产物」这类没有项目外键的行补上权限归属。

    为什么单独有这一步：产物的生成/上传/编辑/抽帧/合成**五条路径**都会新建 `Asset`，
    而 Asset 表既没有 project_id 也没有父级外键，`before_flush` 钩子推导不出来。
    调用方（各写入点）本来就知道自己属于哪个项目，顺手带出归属即可——
    「写什么都要知道自己是哪个项目的」是这些路径的常识，不算额外负担。

    只补空值：已有归属不动（实测库里没有一件产物被两个项目共用，不需要覆盖逻辑）。
    项目查不到时留空 → 该产物退化为「仅管理员可见」，不会变成公共可读。
    """
    if row is None or (getattr(row, "owner_user_id", "") or ""):
        return
    if not project_id:
        return
    project = db.get(Project, project_id)
    owner = (getattr(project, "owner_user_id", "") or "") if project else ""
    if owner:
        row.owner_user_id = owner


def ensure_artifact_access(db: Session, artifact_id: str, request: Request | None,
                           *, user_id: str | None = None,
                           admin: bool | None = None) -> Asset:
    """校验产物归属：**看产物自己的 `owner_user_id`**（2026-09-30 改为一步直达）。

    历史说明：旧方案是「产物 → 引用它的 node_versions → 那个节点的 project_id →
    项目主人」，绕三跳，而且**实测 302 条产物根本反查不到**（占 27%），只能退化成
    「仅管理员可见」。现在产物表有自己的权限归属列，比一次即可；查不出归属的旧产物
    在迁移时标成 `UNASSIGNED_OWNER`（同样只管理员可见，语义更明确）。

    实测口径：正式库 1117 个产物里**被多个项目共用的是 0 个**，所以「一件产物归一个人」
    成立。
    """
    # 直调场景（无 request 也无显式身份）：不经过网络，只按存在性处理。
    # HTTP 路径恒有 request，所以这道短路不会削弱线上的归属校验。
    if _no_request_context(request) and user_id is None and admin is None:
        asset = db.get(Asset, artifact_id)
        if asset is None:
            raise HTTPException(404, "产物不存在")
        return asset
    uid, is_admin_user = _identity(request, user_id, admin)
    asset = db.get(Asset, artifact_id)
    if asset is None:
        raise HTTPException(404, "产物不存在")
    if is_admin_user:
        return asset
    if not owner_matches(asset, uid):
        raise HTTPException(404, "产物不存在")
    return asset


def ensure_session_access(db: Session, session_id: str, request: Request | None,
                          *, user_id: str | None = None,
                          admin: bool | None = None):
    """校验 Agent 会话归属（会话带 project_id，按项目归属判）。"""
    from app.db.models import Session as SessionModel

    _uid, is_admin_user = _identity(request, user_id, admin)
    row = db.get(SessionModel, session_id)
    if row is None:
        raise HTTPException(404, "会话不存在")
    if is_admin_user:
        return row
    # 复用项目校验（会话的 project_id 可能为空——匿名画布会话，按无归属处理）
    if not row.project_id:
        raise HTTPException(404, "会话不存在")
    ensure_project_access(db, row.project_id, request,
                          user_id=user_id, admin=admin)
    return row


def ensure_task_access(db: Session, task_id: str, request: Request | None, *,
                       user_id: str | None = None,
                       admin: bool | None = None):
    """校验任务归属（`GET /api/tasks/{id}`、`POST /api/tasks/{id}/cancel`）。

    判据两级：任务的 `owner_user_id`（权限归属）一步直达；空串（历史遗留行）按
    「谁都不属于」处理，只有管理员可见。

    为什么必须判（2026-09-30 越权排查）：旧实现只查「任务行存在」，于是任意登录
    用户都能读别人的任务详情（含完整 trace、错误原文、产物 id），也能**取消别人
    正在跑的任务**。
    """
    from app.db.models import Task

    _uid, is_admin_user = _identity(request, user_id, admin)
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if is_admin_user:
        return task
    # 一步直达：看任务自己的权限归属（2026-09-30 两层模型）
    if not owner_matches(task, _uid):
        raise HTTPException(404, "任务不存在")
    return task


def ensure_version_access(db: Session, version_id: str, node_id: str,
                          project_id: str, request: Request | None) -> NodeVersion:
    """校验抽卡版本归属：版本必须属于该节点、该节点属于该项目，且项目归调用者。

    三重一致（2026-09-30 补齐）：旧实现只比 `v.node_id` 与 `v.project_id`，
    于是**跨租户也能改别人项目里的版本名/删别人的抽卡记录**——只要知道 id。
    """
    version = db.get(NodeVersion, version_id)
    if version is None or version.node_id != node_id \
            or version.project_id != project_id:
        raise HTTPException(404, "版本不存在或不属于该节点")
    ensure_project_access(db, project_id, request, need="write")
    return version


def ensure_resource_project_access(db: Session, row, request: Request | None, *,
                                   what: str = "资源") -> None:
    """校验资源行的归属：**看它自己的 `owner_user_id`**（2026-09-30 一步直达）。

    为什么需要它（越权排查实测）：`PATCH /characters/{id}`、`/scenes/{id}`、
    `/props/{id}`、`/shots/{id}`、`/episodes/{id}` 这些端点网址里**没有项目编号**，
    而旧实现只查「这一行存不存在」——实测确认：任意登录用户都能改**别人项目**的
    角色名、场景描述、道具提示词、分镜画面描述、分集正文（全部返回 200）。

    判定：直接比 `owner_user_id`（由系统自动填）；空串 = 无归属的**共享资源**
    （如全局角色）→ 只在管理员手里放行，避免变成公共可写。

    `request is None` = 直调场景（自检脚本按函数调用端点），只按存在性放行。
    """
    if _no_request_context(request):
        if row is None:
            raise HTTPException(404, f"{what}不存在")
        return
    if row is None:
        raise HTTPException(404, f"{what}不存在")
    if is_admin(request):
        return
    if not owner_matches(row, current_user_id(request)):
        raise HTTPException(404, f"{what}不存在")


def ensure_owner_or_admin(db: Session, request: Request | None,
                          owner_user_id: str) -> None:
    """按「资源归属用户」校验（不查项目表时的轻量入口，如按 id 取分镜再取其项目）。"""
    if is_admin(request):
        return
    if not owner_user_id or owner_user_id != current_user_id(request):
        raise HTTPException(404, "资源不存在")


def user_of(request: Request, db: Session) -> User | None:
    """当前请求的 User 行（中间件只放了 id，需要完整行时用）。"""
    uid = current_user_id(request)
    return db.get(User, uid) if uid else None


def project_access_guard(request: Request,
                         db: Session = Depends(get_db)) -> None:
    """**挂到 router 上**的归属守卫：只要请求里带 `project_id` 就自动校验。

    用法（见各 api 模块）：`APIRouter(..., dependencies=[Depends(project_access_guard)])`

    为什么用依赖而不是改 122 个端点：① 不带 project_id 的路由自动跳过，不需要
    任何分支；② 「每个端点各写一行」正是历史上漏掉六处的形态（审计 14 号）；
    ③ 新增带 project_id 的路由自动受保护。

    **两个位置都查**（2026-09-30 补）：路径参数（`/projects/{project_id}/...`）
    与查询参数（`/nodes/{node_id}/rotate?project_id=...`）。只查路径会漏掉后者
    ——实测 `edits.py` 的三个编辑端点就是这么传的，等于鉴权开了个后门。

    路径参数**从 `request.path_params` 读**，不声明成函数参数——声明了 FastAPI 会
    把 `project_id` 当成必填查询参数，命中没有该参数的路由时直接 422。
    """
    project_id = (request.path_params.get("project_id")
                  or request.query_params.get("project_id"))
    if not project_id:
        return
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    if is_admin(request):
        return
    owner = getattr(project, "owner_user_id", "") or ""
    if not owner or owner != current_user_id(request):
        raise HTTPException(404, "项目不存在")
