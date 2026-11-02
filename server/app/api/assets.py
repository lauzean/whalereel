"""项目产物资产库 API（M2 资产三方联动，设计文档 4.1 第 6 条 / 4.14）。

- 项目资产 = 项目全部节点版本的产物；删除节点不清产物，可从资产库找回
- GET 列表按产物去重、时间倒序，支持 kind 过滤与关键词搜索（提示词/文件名）
- POST restore 为画布节点挂载已有产物版本（source=uploaded），实现「找回删除节点」
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.nodes import _next_seq, _version_dict
from app.db.models import Asset, NodeVersion
from app.core.access import project_access_guard
from app.db.session import get_db

router = APIRouter(prefix="/api/projects", tags=["assets"], dependencies=[Depends(project_access_guard)])


@router.get("/{project_id}/assets")
def list_assets(project_id: str, kind: str | None = None, q: str = "",
                db: Session = Depends(get_db)):
    """项目产物列表：同一产物多版本引用时去重，保留最新一条引用信息。"""
    versions = db.scalars(
        select(NodeVersion)
        .where(NodeVersion.project_id == project_id)
        .order_by(NodeVersion.created_at.desc(), NodeVersion.seq.desc())
    ).all()
    seen: dict[str, NodeVersion] = {}
    for v in versions:
        if v.artifact_id and v.artifact_id not in seen:
            seen[v.artifact_id] = v

    keyword = q.strip().lower()
    result = []
    for artifact_id, v in seen.items():
        asset = db.get(Asset, artifact_id)
        if not asset:
            continue
        if kind and asset.kind != kind:
            continue
        name = str(asset.meta_json.get("name") or "")
        prompt = v.prompt or ""
        if keyword and keyword not in name.lower() and keyword not in prompt.lower():
            continue
        result.append({
            "artifact_id": artifact_id,
            "kind": asset.kind,
            "name": name or (prompt[:40] if prompt else "未命名产物"),
            "prompt": prompt,
            "node_id": v.node_id,
            "source": v.source,
            "created_at": v.created_at,
        })
    return result


class RestoreIn(BaseModel):
    artifact_id: str


# ---------- 孤儿产物（25 号 2.5：付过钱的东西不许静默消失） ----------

# 节点 id 前缀 → 归属对象（孤儿判定的唯一依据：**该对象是否还存在**）
NODE_PREFIX_SHOT = "shot_"
NODE_PREFIX_CHAR = "char_"
NODE_PREFIX_SCENE = "scene_"
NODE_PREFIX_PROP = "prop_"
# 分镜节点 id 的产物后缀（shot_{分镜id}_{后缀}）——从长到短匹配
SHOT_NODE_SUFFIXES = ("_ff_c2", "_ff", "_v")
# 产物类别码（前端按码给中文名；不在后端拼文案）
ORPHAN_KIND_SHOT_V = "shot_v"
ORPHAN_KIND_SHOT_FF = "shot_ff"
ORPHAN_KIND_CHAR = "char"
ORPHAN_KIND_SCENE = "scene"
ORPHAN_KIND_PROP = "prop"


def _shot_node_kind(node_id: str) -> tuple[str, str] | None:
    """分镜节点 id → (产物类别码, 分镜 id)；不认识的形态返回 None。

    形态由生成侧决定：`shot_{分镜id}_ff`（首帧）、`shot_{分镜id}_v`（视频）、
    `shot_{分镜id}_ff_c2`（多切第二关键帧）。
    """
    for sfx in SHOT_NODE_SUFFIXES:
        if node_id.endswith(sfx):
            return (ORPHAN_KIND_SHOT_V if sfx == "_v"
                    else ORPHAN_KIND_SHOT_FF), node_id[len(NODE_PREFIX_SHOT):-len(sfx)]
    return None


def _live_ids(db: Session, project_id: str) -> set[str]:
    """本项目**现存对象**的 id 全集（分镜 / 形象 / 场景 / 道具）。

    孤儿判定用「产物挂着的对象是否还在」——与 25 号 2.5 的口径一致：重建分集
    会删掉旧分镜，版本记录与产物文件都还在，只是再没有任何分镜引用它。
    """
    from app.db.models import (
        Character, CharacterImage, Episode, Prop, Scene, Shot,
    )

    live: set[str] = set(db.scalars(
        select(Shot.id).where(Shot.episode_id.in_(
            select(Episode.id).where(Episode.project_id == project_id)))).all())
    live |= set(db.scalars(
        select(CharacterImage.id).where(CharacterImage.character_id.in_(
            select(Character.id).where(
                Character.project_id == project_id)))).all())
    live |= set(db.scalars(select(Scene.id).where(
        Scene.project_id == project_id)).all())
    live |= set(db.scalars(select(Prop.id).where(
        Prop.project_id == project_id)).all())
    return live


@router.get("/{project_id}/orphan-versions")
def list_orphan_versions(project_id: str, limit: int = 200,
                         db: Session = Depends(get_db)):
    """孤儿产物清单（25 号 2.5 的**可回溯入口**）：不再被任何现存对象引用的版本。

    重建分集/删除角色后，已产出的图与视频会失去所有引用——它们**没被删**
    （产物文件与版本记录都在），只是任何分镜卡/资产卡都不再显示它们。全库实测
    孤儿率 60%（572 条分镜类版本只有 228 条仍挂着）。本端点把它们列出来：
    看得到、能下载（前端按 artifact_id 拼文件地址）、能对账磁盘占用。

    **不提供自动清理**：清理是破坏性的、且用户可能还要找回（重建后重新分集时
    旧镜与新镜的内容往往重叠）。这里只做「看得见 + 占多少」，删不删由用户决定。
    """
    live = _live_ids(db, project_id)
    rows = db.scalars(
        select(NodeVersion)
        .where(NodeVersion.project_id == project_id)
        .order_by(NodeVersion.created_at.desc(), NodeVersion.seq.desc())
    ).all()

    items: list[dict] = []
    by_kind: dict[str, dict] = {}
    total_bytes = 0
    for v in rows:
        node_id = v.node_id or ""
        kind = entity = ""
        if node_id.startswith(NODE_PREFIX_SHOT):
            parsed = _shot_node_kind(node_id)
            if parsed is None:
                continue
            kind, entity = parsed
        elif node_id.startswith(NODE_PREFIX_CHAR):
            parts = node_id[len(NODE_PREFIX_CHAR):].split("_")
            if len(parts) < 2:
                continue
            kind, entity = ORPHAN_KIND_CHAR, parts[-1]   # 形象记录 id（末段）
        elif node_id.startswith(NODE_PREFIX_SCENE):
            kind, entity = ORPHAN_KIND_SCENE, node_id[len(NODE_PREFIX_SCENE):]
        elif node_id.startswith(NODE_PREFIX_PROP):
            kind, entity = ORPHAN_KIND_PROP, node_id[len(NODE_PREFIX_PROP):]
        else:
            continue          # 非资产类节点（画布节点等）不算孤儿账
        if entity in live:
            continue
        asset = db.get(Asset, v.artifact_id) if v.artifact_id else None
        size = 0
        name = ""
        on_disk = False
        if asset:
            name = str((asset.meta_json or {}).get("name") or "")
            try:
                on_disk = Path(asset.path).is_file()
                if on_disk:
                    size = Path(asset.path).stat().st_size
            except OSError:
                on_disk = False
        total_bytes += size
        bucket = by_kind.setdefault(kind, {"kind": kind, "count": 0, "bytes": 0})
        bucket["count"] += 1
        bucket["bytes"] += size
        if len(items) < max(1, min(limit, 1000)):
            items.append({
                "version_id": v.id, "node_id": node_id, "seq": v.seq,
                "kind": kind, "entity_id": entity,
                "artifact_id": v.artifact_id or "",
                "asset_kind": (asset.kind if asset else ""),
                "name": name, "prompt": (v.prompt or "")[:200],
                "bytes": size, "on_disk": on_disk,
                "created_at": v.created_at,
            })
    return {"ok": True, "total": sum(b["count"] for b in by_kind.values()),
            "bytes": total_bytes,
            "by_kind": sorted(by_kind.values(), key=lambda b: -b["count"]),
            "items": items}


@router.post("/{project_id}/nodes/{node_id}/restore", status_code=201)
def restore_asset(project_id: str, node_id: str, body: RestoreIn,
                  db: Session = Depends(get_db)):
    """找回产物：为画布节点挂载已有产物为新版本（删除节点后从资产库重建）。"""
    asset = db.get(Asset, body.artifact_id)
    if not asset:
        raise HTTPException(404, "产物不存在")
    used = db.scalars(
        select(NodeVersion.id).where(
            NodeVersion.project_id == project_id,
            NodeVersion.artifact_id == body.artifact_id,
        ).limit(1)
    ).first()
    if not used:
        raise HTTPException(400, "产物不属于当前项目")

    seq = _next_seq(db, node_id, project_id)
    version = NodeVersion(
        node_id=node_id, project_id=project_id, seq=seq,
        source="uploaded", prompt="从资产库找回", refs_json=[],
        artifact_id=asset.id,
    )
    db.add(version)
    db.commit()
    return _version_dict(version, asset)
