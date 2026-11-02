"""节点版本（抽卡记录）API。

设计对齐 docs/design.md v0.2 第 4.5 / 9 章：
- 每次生成 = 追加新版本（任务完成时由 task_engine 写入）
- 上传 = 追加 uploaded 版本
- 当前生效版本（selected）由前端保存在画布 graph_json 中
"""

import hashlib
import time

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, NodeVersion
from app.core.access import project_access_guard
# 上传大小上限与分块读取的实现收口在 `core/upload.py`（2026-09-30 安全整改）：
# 旧实现先 `await file.read()` 把整个文件读进内存、再判非空——没有任何上限，
# 一个请求就能把内存吃光。这里改走共用入口（四个上传点同一份实现）。
from app.core.upload import read_upload_capped as _read_upload_capped
from app.db.session import get_db

router = APIRouter(prefix="/api/nodes", tags=["nodes"], dependencies=[Depends(project_access_guard)])

# 上传扩展名 → 产物类型
EXT_KIND = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".mp4": "video", ".webm": "video", ".mov": "video",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio",
}


def _version_dict(v: NodeVersion, asset: Asset | None) -> dict:
    return {
        "id": v.id, "node_id": v.node_id, "seq": v.seq, "source": v.source,
        "name": getattr(v, "name", "") or "",
        "prompt": v.prompt, "refs": v.refs_json, "task_id": v.task_id,
        "artifact_id": v.artifact_id,
        "kind": asset.kind if asset else None,
        "meta": asset.meta_json if asset else {},
        "created_at": v.created_at,
    }


@router.get("/{node_id}/versions")
def list_versions(node_id: str, project_id: str, db: Session = Depends(get_db)):
    versions = db.scalars(
        select(NodeVersion)
        .where(NodeVersion.node_id == node_id, NodeVersion.project_id == project_id)
        .order_by(NodeVersion.seq.asc())
    ).all()
    result = []
    for v in versions:
        asset = db.get(Asset, v.artifact_id) if v.artifact_id else None
        result.append(_version_dict(v, asset))
    return result


@router.delete("/{node_id}/versions/{version_id}")
def delete_node_version(node_id: str, version_id: str, project_id: str, db: Session = Depends(get_db)):
    """删除单个抽卡版本（仅删除版本记录；产物 Asset 保留资产库可找回）。

    删除「当前生效」版本后，展示自动回退到剩余版本中最新的一个
    （展示层按 _latest_artifact 动态推导，无需额外处理）。
    被删版本若为该节点「设为生效」的版本，同步清除指针（场景/道具/形象
    三类节点统一处理：scene_{id} / prop_{id} / char_{cid}_{imgId}）。

    取数说明（2026-09-30 修复）：越权收口那次（`1db7627`）把 `v = db.get(...)`
    连同旧校验一起删掉、却留下了 `v.artifact_id` 的引用——结果**资源主人自己
    删版本会 500**（跨租户由中间件先挡成 404，越权用例反而走不到这行，所以
    112 项全过也没抓到）。这里恢复取数并带回「版本必须属于该节点、该项目」
    的一致性校验。
    """
    v = db.get(NodeVersion, version_id)
    if v is None or v.node_id != node_id or v.project_id != project_id:
        raise HTTPException(404, "版本不存在或不属于该节点")
    if not v.artifact_id:
        db.delete(v)
        db.commit()
        return {"ok": True}
    from app.db.models import CharacterImage, Prop, Scene

    # 指针清理：按 node_id 前缀定位宿主记录，指针指向被删版本则清空（回退最新）
    if node_id.startswith("char_"):
        # 形象节点 node_id = char_{cid}_{imgId}，宿主是第三段（形象 id）
        image = db.get(CharacterImage, node_id.split("_")[2])
        if image and image.preferred_version_id == version_id:
            image.preferred_version_id = None
    elif node_id.startswith("scene_"):
        scene = db.get(Scene, node_id[len("scene_"):])
        if scene and scene.preferred_version_id == version_id:
            scene.preferred_version_id = None
    elif node_id.startswith("prop_"):
        prop = db.get(Prop, node_id[len("prop_"):])
        if prop and prop.preferred_version_id == version_id:
            prop.preferred_version_id = None
    db.delete(v)
    db.commit()
    return {"ok": True}


class VersionRenameIn(BaseModel):
    """版本改名人参（空名 = 恢复默认命名「版本N」）。"""
    name: str = ""


@router.patch("/{node_id}/versions/{version_id}")
def rename_node_version(node_id: str, version_id: str, body: VersionRenameIn,
                        project_id: str, db: Session = Depends(get_db)):
    """版本命名（三卡通用）：写入 NodeVersion.name，前端据此替换默认名展示。"""
    v = db.get(NodeVersion, version_id)
    if v is None or v.node_id != node_id or v.project_id != project_id:
        raise HTTPException(404, "版本不存在或不属于该节点")
    v.name = body.name.strip()[:100]
    db.commit()
    return _version_dict(v, db.get(Asset, v.artifact_id) if v.artifact_id else None)


@router.post("/{node_id}/upload", status_code=201)
async def upload_version(
    node_id: str,
    project_id: str,
    file: UploadFile,
    db: Session = Depends(get_db),
):
    """上传本地文件：落产物 + 追加 uploaded 版本（复刻小云雀空白节点上传）。"""

    from app.core.config import artifact_dir_for, settings

    if not file.filename:
        raise HTTPException(400, "文件名为空")
    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in EXT_KIND:
        raise HTTPException(400, f"不支持的文件类型：{ext}")

    content = await _read_upload_capped(file)
    if not content:
        raise HTTPException(400, "文件为空")

    digest = hashlib.sha256(content).hexdigest()[:16]
    path = artifact_dir_for(project_id) / f"upload_{int(time.time())}_{digest}{ext}"
    path.write_bytes(content)

    asset = Asset(kind=EXT_KIND[ext], path=str(path),
                  sha256=digest, meta_json={"name": file.filename, "size": len(content)})
    db.add(asset)
    db.flush()

    seq = _next_seq(db, node_id, project_id)
    version = NodeVersion(
        node_id=node_id, project_id=project_id, seq=seq,
        source="uploaded", prompt="", refs_json=[],
        artifact_id=asset.id,
    )
    db.add(version)
    db.commit()
    return _version_dict(version, asset)


def _next_seq(db: Session, node_id: str, project_id: str) -> int:
    last = db.scalars(
        select(NodeVersion.seq)
        .where(NodeVersion.node_id == node_id, NodeVersion.project_id == project_id)
        .order_by(NodeVersion.seq.desc())
        .limit(1)
    ).first()
    return (last or 0) + 1
