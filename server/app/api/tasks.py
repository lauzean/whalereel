"""任务 API + WebSocket。"""

import asyncio

from fastapi import (APIRouter, Depends, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.task_engine import engine
from app.core.access import (current_user_id, ensure_project_access, is_admin,
                             owned_project_ids, project_access_guard)
from app.db.session import get_db
from app.ws.bus import bus

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(project_access_guard)])


class TaskIn(BaseModel):
    project_id: str
    node_id: str
    runner: str
    model_id: str = ""
    params: dict = {}
    input_refs: dict = {}


# 视频类 runner（其参数受端点能力约束，提交前须校验）
_VIDEO_RUNNERS = ("t2v", "ref2v", "i2v", "fl2v")


def _validate_video_params(body: TaskIn) -> None:
    """画布入口的生成前能力校验（与分镜链路同一份判定）。

    此前只有分镜链路调 `validate_video_request`，画布直接 `engine.submit`——
    超时长/超参考图张数/不支持的画幅会被 provider 层**静默钳位**（参数被改了
    而用户不知道，是审计 14 号反复抓到的形态）。端点按**任务指定的模型**解析。
    """
    from app.core.providers import resolve_runtime_config, validate_video_request

    try:
        cfg, _ = resolve_runtime_config("t2v", body.model_id or "")
        provider = getattr(cfg, "name", "") or "custom"
    except Exception:  # noqa: BLE001 配置解析失败交 provider 层兜底
        provider = "custom"
    refs = body.input_refs or {}
    params = body.params or {}
    problems = validate_video_request(
        provider,
        duration=int(params.get("duration") or 0),
        aspect=str(params.get("aspect") or ""),
        ref_count=len(refs.get("extra_images") or []),
        has_first_frame=bool(str(refs.get("image") or refs.get("first") or "").strip()),
    )
    if problems:
        raise HTTPException(
            400, f"生成参数超出端点能力：{'；'.join(problems)}。"
                 "请调整参数或改用支持该参数的模型")


@router.post("", status_code=201)
def create_task(body: TaskIn, request: Request, db: Session = Depends(get_db)):
    """提交生成任务。

    2026-09-30 补：**请求体里的 project_id 也要判归属**——middleware 看不到请求体，
    此前任何登录用户都能往别人的项目里提交付费生成任务（花对方的钱、往对方的项目
    写版本与产物）。提交人随任务落库（`initiator_user_id`），此前全仓没有一处传它。
    """
    if body.runner in _VIDEO_RUNNERS:
        _validate_video_params(body)
    ensure_project_access(db, body.project_id, request, need="write")
    try:
        return engine.submit(
            project_id=body.project_id, node_id=body.node_id,
            runner_name=body.runner, model_id=body.model_id,
            params=body.params, input_refs=body.input_refs,
            actor_user_id=current_user_id(request),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@router.get("")
def list_tasks(request: Request, project_id: str | None = None,
               batch_id: str | None = None, db: Session = Depends(get_db)):
    """任务摘要列表。

    2026-09-30 补：不带项目编号时，普通用户只能看到**自己项目**的任务——旧实现
    无租户过滤，任何登录用户都能拉全库最近 200 条任务（含别人的 project_id/
    node_id/artifact_id/error，也是拼别人产物 URL 的弹药）。
    """
    if not project_id and not is_admin(request):
        return engine.list_tasks(
            None, batch_id,
            project_ids=owned_project_ids(db, current_user_id(request)))
    return engine.list_tasks(project_id, batch_id)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    ok = engine.cancel(task_id)
    if not ok:
        raise HTTPException(404, "任务不存在或不可取消")
    return {"ok": True}


@router.get("/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)):
    from app.db.models import Task

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    # 详情接口给完整轨迹（解析观测窗恢复依赖）；列表接口只给摘要（省流量）
    return engine._to_dict(task, include_trace=True)


ws_router = APIRouter()


@ws_router.websocket("/ws/tasks")
async def ws_tasks(websocket: WebSocket):
    """任务事件订阅（按项目）。

    **鉴权（2026-09-30 第 1 层）**：HTTP 中间件不覆盖 WebSocket，所以这里自己校验。
    令牌走 query（`?token=`）——浏览器 WebSocket 构造器**不能自定义请求头**，
    这是 WS 场景的通行做法。校验两件事：令牌有效 + 该项目归当前用户
    （管理员可订阅全部）。任一不满足直接以 4401 关闭，不订阅任何事件。
    """
    token = websocket.query_params.get("token", "")
    project_id = websocket.query_params.get("project_id", "")

    from fastapi import HTTPException as _HTTPException

    from app.api.auth import try_resolve_token
    from app.core.access import ensure_project_access
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        user = try_resolve_token(token, db)
        if user is None:
            await websocket.close(code=4401, reason="未登录或登录已过期")
            return
        try:
            ensure_project_access(db, project_id, None,
                                  user_id=user.id, admin=bool(user.is_admin))
        except _HTTPException:
            await websocket.close(code=4403, reason="项目不存在")
            return

    await websocket.accept()
    queue = bus.subscribe(project_id)
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=15)
                await websocket.send_text(message)
            except asyncio.TimeoutError:
                await websocket.send_text('{"event":"ping"}')
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(project_id, queue)
