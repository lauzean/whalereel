"""项目与画布 API。"""

import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core import access
from app.core.model_manager import PRESET_KINDS
from app.core.task_engine import engine
from app.db.models import (
    AgentMessage, Canvas, Character, CharacterAssetLink, CharacterImage, Episode,
    Message, ModelCall, NodeVersion, Project, Prop, Scene, ScriptVersion, Setting,
    Shot, ShotReview, Task,
)
from app.db.models import Session as SessionModel
from app.db.session import get_db
from app.script.persist import SCRIPT_META_KEY_PREFIX, load_style_anchors

router = APIRouter(prefix="/api/projects", tags=["projects"])

# 任务非终态状态（删除项目前需先取消，避免 runner 完成后回写孤儿数据）
_ACTIVE_TASK_STATUSES = ("queued", "loading_model", "running")

# 项目模型预设值的形态校验：本地模型 id、provider 远端模型名或端点编码串
# "{endpoint_id}::{model_id}"（SQLite 不 enforce VARCHAR 长度，放宽到 140）
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._/\-:]{1,140}$")

# 项目描述最大长度（首页卡片展示，超长截断无意义）
_DESCRIPTION_MAX_LEN = 500


class ProjectIn(BaseModel):
    name: str
    mode: str = "canvas"  # canvas（创作Agent）| drama（短剧&漫剧）
    style_preset: str = "real"  # real | 3d | anime | guofeng
    # 风格锚点（美学第二级，可空）：取值见 data/style_presets.json 的 anchors；
    # 必须与 style_preset 相容（写实档不得选动画流派）
    style_anchor: str = ""
    # 视频画幅比例：16:9 | 4:3 | 1:1 | 3:4 | 9:16 | 21:9 | adaptive（自适应）
    aspect_ratio: str = "16:9"
    # 视频分辨率档：480p(8bit) | 720p(8bit) | 1080p(10bit)
    video_resolution: str = "720p"
    country: str = "中国"  # 国家预设（文生图/文生视频据此约束人物外貌）
    description: str = ""  # 项目描述（首页卡片展示，可空）
    model_presets: dict[str, str] = {}  # 生成模型预设：{"t2i": "...", "t2v": "..."}


class ProjectPatchIn(BaseModel):
    model_presets: dict[str, str] | None = None
    # 国家预设（提供时整体替换；文生图/文生视频据此约束人物外貌）
    country: str | None = None
    # 视频画幅比例（提供时替换；含 adaptive 自适应）
    aspect_ratio: str | None = None
    # 视频分辨率档（提供时替换）：480p | 720p | 1080p
    video_resolution: str | None = None
    # 项目描述（提供时替换）
    description: str | None = None
    # 风格锚点（提供时替换；空串 = 清除锚点）。相容性按**项目当前画风**校验
    style_anchor: str | None = None
    # 首帧提示词精炼（23 号批次 3，提供时替换）：默认开。关掉即退回改造前行为；
    # 只影响**之后提交**的首帧任务，已生成的不变（同分辨率档的口径）
    refine_first_frame: bool | None = None
    # 组内环境锁定（32 号场景组）：None = 不改
    scene_group_lock: bool | None = None


class CanvasIn(BaseModel):
    graph: dict


def _now() -> int:
    return int(time.time())


def _normalize_model_presets(raw: dict[str, str]) -> dict[str, str]:
    """校验并归一化项目模型预设：键白名单 t2i/t2v；值 strip 后按模型 id 形态校验，空值剔除。

    宽松校验不做存在性校验：provider 模型列表动态（依赖 .env 配置与远端 /models）、
    本地模型可后装，可用性兜底在提交链路 _preflight_model。
    """
    presets: dict[str, str] = {}
    for kind, mid in (raw or {}).items():
        if kind not in PRESET_KINDS:
            raise HTTPException(400, f"无效的模型预设类别：{kind}")
        mid = str(mid or "").strip()
        if not mid:
            continue  # 空值 = 未设置，剔除该键
        if not _MODEL_ID_RE.match(mid):
            raise HTTPException(400, f"无效的模型 ID：{mid}")
        presets[kind] = mid
    return presets


def _validate_style_anchor(preset: str, anchor: str) -> str:
    """风格锚点白名单 + 与画风预设的**相容性校验**（确定性，不做语义判断）。

    不相容（如写实项目选「京都动画风格」）直接 400：锚点是**全局风格限定词**，
    选错会让资产 / 关键帧 / 视频三阶段一起风格穿帮。
    返回归一化后的锚点 key（空串 = 不指定）。
    """
    anchor = (anchor or "").strip()
    if not anchor:
        return ""
    anchors = load_style_anchors()
    info = anchors.get(anchor)
    if not info:
        raise HTTPException(
            400, f"无效风格锚点：{anchor}（可选：{'、'.join(anchors)}）")
    if info.get("form") != preset:
        raise HTTPException(
            400, f"风格锚点「{info.get('label')}」与当前画风不相容（该锚点属于 "
                 f"{info.get('form')} 形式）——请先切换画风或改选锚点")
    return anchor


def _project_dict(p: Project, logline: str = "") -> dict:
    """项目行的 API 响应结构（list/create/patch 共用）。

    logline：剧本一句话简介（首页卡片描述展示；未解析项目为空）。
    """
    return {
        "id": p.id, "name": p.name, "mode": p.mode,
        "style_preset": p.style_preset,
        "style_anchor": getattr(p, "style_anchor", None) or "",
        "aspect_ratio": getattr(p, "aspect_ratio", None) or "16:9",
        "video_resolution": (getattr(p, "video_resolution", None)
                             or "720p"),
        "country": getattr(p, "country", None) or "中国",
        # 首帧精炼开关（23 号批次 3）：缺省视为开（旧库补列前的行按默认行为）
        "refine_first_frame": bool(getattr(p, "refine_first_frame", True)),
        "scene_group_lock": bool(getattr(p, "scene_group_lock", True)),
        "description": (getattr(p, "description", None) or ""),
        "logline": logline,
        "model_presets": p.model_presets or {},
        "created_at": p.created_at,
        "updated_at": p.updated_at, "cover_artifact_id": p.cover_artifact_id,
        # 归属用户（多租户）：前端可据此显示「谁的项目」，管理员据此辨认他人项目
        "owner_user_id": getattr(p, "owner_user_id", "") or "",
    }


def _script_loglines(db: Session) -> dict[str, str]:
    """项目 ID → 剧本一句话简介（解析元信息 Setting 批量读取）。

    解析元信息以 Setting 键 drama_script:{project_id} 存档（JSON），
    未解析过的项目无记录，logline 取空串。
    """
    rows = db.scalars(select(Setting).where(
        Setting.key.like(f"{SCRIPT_META_KEY_PREFIX}%"))).all()
    loglines: dict[str, str] = {}
    for row in rows:
        try:
            meta = json.loads(row.value or "{}")
        except ValueError:
            continue  # 手改损坏的元信息行不阻断列表
        loglines[row.key[len(SCRIPT_META_KEY_PREFIX):]] = str(meta.get("logline") or "")
    return loglines


def _logline_of(db: Session, project_id: str) -> str:
    """单项目的剧本一句话简介（get/patch 响应用）。"""
    return _script_loglines(db).get(project_id, "")


@router.get("")
def list_projects(request: Request, db: Session = Depends(get_db)):
    """项目列表：普通用户只看自己的，管理员看全部（多租户，2026-09-30）。

    排序 = **创建时间倒序**（2026-09-30 用户要求：新项目排最前；此前按更新时间，
    老项目一被编辑就顶到最上面，找刚刚建的项目反而要翻）。
    """
    query = select(Project).order_by(Project.created_at.desc(),
                                     Project.id.desc())
    if not access.is_admin(request):
        query = query.where(Project.owner_user_id == access.current_user_id(request))
    projects = db.scalars(query).all()
    loglines = _script_loglines(db)
    return [_project_dict(p, loglines.get(p.id, "")) for p in projects]


@router.get("/style-anchors")
def list_style_anchors():
    """风格锚点选项表（前端选择器数据源；**必须声明在 `/{project_id}` 之前**，
    否则被动态路由吃掉当成 project_id）。

    单一真源在 `data/style_presets.json` 的 `anchors`——前端不硬编码锚点列表，
    避免「后端加了锚点而前端看不到」的漂移。开放读（与模型选择器同口径）。
    """
    return {"anchors": [
        {"key": key, "label": info.get("label", key),
         "prompt": info.get("prompt", ""), "form": info.get("form", ""),
         "hint": info.get("hint", "")}
        for key, info in load_style_anchors().items()
    ]}


@router.get("/{project_id}")
def get_project(project_id: str, request: Request, db: Session = Depends(get_db)):
    """单个项目详情（路由化刷新后按 ID 恢复项目元信息）。

    归属校验见 core/access：非归属者（且非管理员）按 404 处理。
    """
    access.ensure_project_access(db, project_id, request)
    return _project_dict(db.get(Project, project_id), _logline_of(db, project_id))


@router.post("", status_code=201)
def create_project(body: ProjectIn, request: Request,
                   db: Session = Depends(get_db)):
    if not body.name.strip():
        raise HTTPException(400, "项目名不能为空")
    if body.mode not in ("canvas", "drama"):
        raise HTTPException(400, f"无效模式：{body.mode}")
    if body.style_preset not in ("real", "3d", "anime", "guofeng"):
        raise HTTPException(400, f"无效画风：{body.style_preset}")
    style_anchor = _validate_style_anchor(body.style_preset, body.style_anchor)
    from app.api.drama import ASPECT_WH, ASPECT_ADAPTIVE, VIDEO_RESOLUTION_HEIGHTS

    if body.aspect_ratio != ASPECT_ADAPTIVE and body.aspect_ratio not in ASPECT_WH:
        raise HTTPException(
            400, f"无效画幅比例：{body.aspect_ratio}"
                 f"（可选：{'、'.join([*ASPECT_WH, ASPECT_ADAPTIVE])}）")
    if body.video_resolution not in VIDEO_RESOLUTION_HEIGHTS:
        raise HTTPException(
            400, f"无效分辨率档：{body.video_resolution}"
                 f"（可选：{'、'.join(VIDEO_RESOLUTION_HEIGHTS)}）")
    country = body.country.strip() or "中国"
    if len(country) > 50:
        raise HTTPException(400, "国家名称过长")
    description = body.description.strip()
    if len(description) > _DESCRIPTION_MAX_LEN:
        raise HTTPException(400, f"项目描述最长 {_DESCRIPTION_MAX_LEN} 字")
    project = Project(name=body.name.strip(), mode=body.mode,
                      style_preset=body.style_preset,
                      style_anchor=style_anchor,
                      aspect_ratio=body.aspect_ratio,
                      video_resolution=body.video_resolution, country=country,
                      description=description,
                      model_presets=_normalize_model_presets(body.model_presets),
                      # 归属当前用户（多租户）：管理员建的项目也归自己，
                      # 管理员权限体现在「能看全部」，不是「项目不属于任何人」
                      owner_user_id=access.current_user_id(request))
    db.add(project)
    db.flush()
    db.add(Canvas(project_id=project.id, graph_json="{}"))
    db.commit()
    return _project_dict(project)


@router.patch("/{project_id}")
def patch_project(project_id: str, body: ProjectPatchIn, request: Request,
                  db: Session = Depends(get_db)):
    """更新项目设置（model_presets 整体替换；country 提供时替换；返回更新后的项目）。"""
    project = access.ensure_project_access(db, project_id, request, need="write")
    if body.model_presets is not None:
        project.model_presets = _normalize_model_presets(body.model_presets)
    if body.country is not None:
        country = body.country.strip() or "中国"
        if len(country) > 50:
            raise HTTPException(400, "国家名称过长")
        project.country = country
    if body.aspect_ratio is not None:
        # 画幅比例可选值校验（含 adaptive 自适应——由首帧/参考图决定尺寸）
        from app.api.drama import ASPECT_ADAPTIVE, ASPECT_WH

        if (body.aspect_ratio != ASPECT_ADAPTIVE
                and body.aspect_ratio not in ASPECT_WH):
            raise HTTPException(
                400, f"无效画幅比例：{body.aspect_ratio}"
                     f"（可选：{'、'.join([*ASPECT_WH, ASPECT_ADAPTIVE])}）")
        project.aspect_ratio = body.aspect_ratio
    if body.video_resolution is not None:
        # 分辨率档校验（改设置只影响之后提交的任务，已生成的不变）
        from app.api.drama import VIDEO_RESOLUTION_HEIGHTS

        if body.video_resolution not in VIDEO_RESOLUTION_HEIGHTS:
            raise HTTPException(
                400, f"无效分辨率档：{body.video_resolution}"
                     f"（可选：{'、'.join(VIDEO_RESOLUTION_HEIGHTS)}）")
        project.video_resolution = body.video_resolution
    if body.description is not None:
        description = body.description.strip()
        if len(description) > _DESCRIPTION_MAX_LEN:
            raise HTTPException(400, f"项目描述最长 {_DESCRIPTION_MAX_LEN} 字")
        project.description = description
    if body.style_anchor is not None:
        # 相容性按**项目当前画风**校验（PATCH 不允许改画风，故无切换场景）
        project.style_anchor = _validate_style_anchor(
            project.style_preset, body.style_anchor)
    if body.refine_first_frame is not None:
        project.refine_first_frame = bool(body.refine_first_frame)
    if body.scene_group_lock is not None:
        project.scene_group_lock = bool(body.scene_group_lock)
    db.commit()
    return _project_dict(project, _logline_of(db, project_id))


@router.delete("/{project_id}")
def delete_project(project_id: str, request: Request,
                   db: Session = Depends(get_db)):
    """删除项目及其全部关联数据：画布/任务/抽卡版本/Agent 会话/短剧数据。

    级联范围：Canvas、NodeVersion、Task、AgentMessage、Scene、Episode→Shot、
    本项目 Character→CharacterImage、Session→Message，以及道具、审片记录、
    剧本版本、模型调用日志、角色-素材关联（2026-09-30 复查 T1：这五张表此前
    漏删，实测库里躺着一批指向已删项目的孤儿行）。assets 表与产物文件为跨项目
    共享的资产库（无项目归属字段），保留不删。
    删除前先取消项目内进行中的任务：runner 结束时查不到任务行会安全跳过落库。
    """
    project = access.ensure_project_access(db, project_id, request, need="write")
    # 先取消进行中任务（engine 自持事务，须在本会话写库前调用，避免交叉提交）
    active_tasks = db.scalars(select(Task.id).where(
        Task.project_id == project_id,
        Task.status.in_(_ACTIVE_TASK_STATUSES),
    )).all()
    for task_id in active_tasks:
        engine.cancel(task_id)

    # 分集 → 分镜（分镜经 episode_id 关联，须先查分集 ID 再删）
    episode_ids = db.scalars(select(Episode.id).where(
        Episode.project_id == project_id)).all()
    if episode_ids:
        db.execute(delete(Shot).where(Shot.episode_id.in_(episode_ids)))
    db.execute(delete(Episode).where(Episode.project_id == project_id))
    # 角色 → 角色形象（project_id 为 NULL 的是全局角色，仅删本项目角色）
    character_ids = db.scalars(select(Character.id).where(
        Character.project_id == project_id)).all()
    if character_ids:
        db.execute(delete(CharacterImage).where(
            CharacterImage.character_id.in_(character_ids)))
    db.execute(delete(Character).where(Character.project_id == project_id))
    db.execute(delete(Scene).where(Scene.project_id == project_id))
    # Agent 会话 → 会话消息
    session_ids = db.scalars(select(SessionModel.id).where(
        SessionModel.project_id == project_id)).all()
    if session_ids:
        db.execute(delete(Message).where(Message.session_id.in_(session_ids)))
    db.execute(delete(SessionModel).where(SessionModel.project_id == project_id))
    db.execute(delete(NodeVersion).where(NodeVersion.project_id == project_id))
    db.execute(delete(Task).where(Task.project_id == project_id))
    db.execute(delete(AgentMessage).where(AgentMessage.project_id == project_id))
    db.execute(delete(Canvas).where(Canvas.project_id == project_id))
    # 五张此前漏删的子表（2026-09-30 复查 T1）：不删就会留下指向已删项目的
    # 孤儿行（只有管理员可见，但用户永远不会再有机会清理它们）
    db.execute(delete(Prop).where(Prop.project_id == project_id))
    db.execute(delete(ShotReview).where(ShotReview.project_id == project_id))
    db.execute(delete(ScriptVersion).where(
        ScriptVersion.project_id == project_id))
    db.execute(delete(ModelCall).where(ModelCall.project_id == project_id))
    db.execute(delete(CharacterAssetLink).where(
        CharacterAssetLink.project_id == project_id))
    db.delete(project)
    db.commit()
    return {"ok": True}


@router.get("/{project_id}/canvas")
def get_canvas(project_id: str, request: Request, db: Session = Depends(get_db)):
    access.ensure_project_access(db, project_id, request)
    canvas = db.get(Canvas, project_id)
    if not canvas:
        raise HTTPException(404, "画布不存在")
    import json

    return {"project_id": project_id, "graph": json.loads(canvas.graph_json or "{}")}


@router.put("/{project_id}/canvas")
def save_canvas(project_id: str, body: CanvasIn, request: Request,
                db: Session = Depends(get_db)):
    project = access.ensure_project_access(db, project_id, request, need="write")
    canvas = db.get(Canvas, project_id)
    import json

    graph_json = json.dumps(body.graph, ensure_ascii=False)
    if canvas:
        canvas.graph_json = graph_json
        canvas.updated_at = _now()
    else:
        db.add(Canvas(project_id=project_id, graph_json=graph_json))
    project.updated_at = _now()
    db.commit()
    return {"ok": True}
