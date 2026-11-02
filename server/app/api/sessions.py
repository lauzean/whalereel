"""会话（Agent 对话）API（对齐设计文档 5.1/5.5/9 章）。

- 多会话并行：会话与画布解耦，任何会话的生成结果都落同一画布
- chat 走 agent.orchestrator 编排；批量/长耗时先出计划确认卡（confirmed 确认后执行）
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from app.agent.orchestrator import orchestrate_chat
from app.core import access
from app.db.models import Message, Project
from app.db.models import Session as SessionModel
from app.core.access import project_access_guard
from app.db.session import get_db

router = APIRouter(prefix="/api", tags=["sessions"], dependencies=[Depends(project_access_guard)])


class SessionIn(BaseModel):
    title: str | None = None


class SessionPatchIn(BaseModel):
    title: str | None = None
    status: str | None = None  # active|closed
    pane_side: str | None = None  # left|right


class ChatRef(BaseModel):
    """@引用（ID 级，见设计文档 4.6）。"""

    node_id: str
    version_id: str | None = None


class ChatIn(BaseModel):
    text: str
    refs: list[ChatRef] = []
    confirmed: bool = False


def _session_dict(s: SessionModel) -> dict:
    return {"id": s.id, "project_id": s.project_id, "title": s.title,
            "status": s.status, "pane_side": s.pane_side,
            "created_at": s.created_at, "updated_at": s.updated_at}


def _message_dict(m: Message) -> dict:
    return {"id": m.id, "session_id": m.session_id, "role": m.role,
            "content": m.content, "refs": m.refs_json,
            "tool_calls": m.tool_calls_json, "created_at": m.created_at}


def _project(db: Session, project_id: str) -> Project:
    """取项目（**归属校验由 router 级 project_access_guard 负责**：这两个端点
    的路径里带 {project_id}）。"""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    return project


def _session(db: Session, session_id: str) -> SessionModel:
    """取会话（**归属校验由 main.py 的 `_check_resource_owner` 中间件统一负责**：
    `/api/sessions/{id}` 命中 core/resource_map.py 的会话规则，按会话的 project_id
    判归属）。

    这里只做存在性检查——不在端点里重复一份归属判定（审计 14 号的教训：同一份
    保证散落多处必然漂移）。
    """
    row = db.get(SessionModel, session_id)
    if not row:
        raise HTTPException(404, "会话不存在")
    return row


@router.get("/projects/{project_id}/sessions")
def list_sessions(project_id: str, db: Session = Depends(get_db)):
    _project(db, project_id)
    rows = db.scalars(
        select(SessionModel)
        .where(SessionModel.project_id == project_id)
        .order_by(SessionModel.created_at.desc())
    ).all()
    return [_session_dict(s) for s in rows]


@router.post("/projects/{project_id}/sessions", status_code=201)
def create_session(project_id: str, body: SessionIn,
                   db: Session = Depends(get_db)):
    _project(db, project_id)
    row = SessionModel(
        project_id=project_id,
        title=(body.title or "").strip() or "新对话",
        status="active", pane_side="right",
    )
    db.add(row)
    db.commit()
    return _session_dict(row)


@router.patch("/sessions/{session_id}")
def patch_session(session_id: str, body: SessionPatchIn, db: Session = Depends(get_db)):
    """重命名/关闭会话/切换面板位置偏好。"""
    row = _session(db, session_id)
    if body.title is not None:
        row.title = body.title
    if body.status is not None:
        if body.status not in ("active", "closed"):
            raise HTTPException(400, f"无效状态：{body.status}")
        row.status = body.status
    if body.pane_side is not None:
        if body.pane_side not in ("left", "right"):
            raise HTTPException(400, f"无效面板位置：{body.pane_side}")
        row.pane_side = body.pane_side
    db.commit()
    return {"ok": True}


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, db: Session = Depends(get_db)):
    _session(db, session_id)
    # 同秒内的消息按入库顺序返回（SQLite rowid）
    rows = db.scalars(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at, literal_column("messages.rowid"))
    ).all()
    return [_message_dict(m) for m in rows]


@router.post("/sessions/{session_id}/chat")
def chat(session_id: str, body: ChatIn, db: Session = Depends(get_db)):
    """发消息：编排规划 →（计划确认）→ 建节点/提交任务 → 双向消息落库。"""
    session = _session(db, session_id)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "消息内容为空")
    refs = [r.model_dump() for r in body.refs]
    try:
        return orchestrate_chat(db, session, text, refs, body.confirmed)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
