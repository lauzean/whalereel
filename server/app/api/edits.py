"""节点编辑工具 API（M2 P1，设计文档 4.9）。

- 旋转（图片）/ 裁剪（图片按比例居中裁）/ 剪辑（视频时间区间）/ 拼接（多选拼图拼视频）
- 编辑动作与生成一样走「追加版本」模型（source="edited"），保证可回退
- 图片编辑在 ffmpeg 缺失或失败时降级 mock 占位（meta.fallback 标记），链路不断
- 视频剪辑/拼接依赖 ffmpeg，失败直接报错（避免静默产出失真结果）
"""

import hashlib
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.frames import _ffprobe_bin
from app.api.nodes import _next_seq, _version_dict
from app.core.config import artifact_dir_for, settings
from app.db.models import Asset, NodeVersion
from app.core.access import project_access_guard
from app.db.session import get_db
from app.runners.mock_helpers import mock_image

router = APIRouter(prefix="/api/nodes", tags=["edits"], dependencies=[Depends(project_access_guard)])
projects_router = APIRouter(prefix="/api/projects", tags=["edits"], dependencies=[Depends(project_access_guard)])

# 图片裁剪支持的预设比例
CROP_RATIOS = {"1:1", "4:3", "3:4", "16:9", "9:16"}


class RotateIn(BaseModel):
    direction: str = "cw"  # cw | ccw


class CropIn(BaseModel):
    ratio: str = "16:9"


class TrimIn(BaseModel):
    start: float
    end: float


class ConcatIn(BaseModel):
    kind: str  # image | video
    artifact_ids: list[str]
    dest_node_id: str


def _probe_size(path: Path) -> tuple[int, int] | None:
    """ffprobe 探测媒体宽高；不可用或失败返回 None。"""
    binary = _ffprobe_bin()
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        width, height = result.stdout.strip().split("x")
        return int(width), int(height)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _latest_version_of_kind(db: Session, project_id: str, node_id: str,
                            kind: str) -> tuple[NodeVersion, Asset] | None:
    """节点最新的指定类型版本（按 seq 降序找第一个产物类型匹配的版本）。"""
    versions = db.scalars(
        select(NodeVersion)
        .where(NodeVersion.node_id == node_id,
               NodeVersion.project_id == project_id)
        .order_by(NodeVersion.seq.desc())
    ).all()
    for v in versions:
        if not v.artifact_id:
            continue
        asset = db.get(Asset, v.artifact_id)
        if asset and asset.kind == kind:
            return v, asset
    return None


def _edit_image_file(src_path: Path, out_path: Path, vf: str,
                     fallback_label: str) -> tuple[Path, bool]:
    """ffmpeg 单滤镜编辑图片；失败时降级 mock 占位。返回 (文件, 是否降级)。"""
    try:
        result = subprocess.run(
            [settings.ffmpeg_bin, "-y", "-loglevel", "error",
             "-i", str(src_path), "-vf", vf, str(out_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return out_path, False
    except (OSError, subprocess.TimeoutExpired):
        pass
    return mock_image(out_path, fallback_label), True


def _append_edited_version(db: Session, project_id: str, node_id: str,
                           file_path: Path, kind: str, prompt: str,
                           refs: list[str], meta: dict) -> tuple[NodeVersion, Asset]:
    """编辑产物落库并追加为新版本（source="edited"）。"""
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()[:16]
    asset = Asset(kind=kind, path=str(file_path), sha256=digest, meta_json=meta)
    db.add(asset)
    db.flush()
    seq = _next_seq(db, node_id, project_id)
    version = NodeVersion(
        node_id=node_id, project_id=project_id, seq=seq,
        source="edited", prompt=prompt, refs_json=refs, artifact_id=asset.id,
    )
    db.add(version)
    db.commit()
    return version, asset


@router.post("/{node_id}/rotate")
def rotate_image(node_id: str, project_id: str, body: RotateIn,
                 db: Session = Depends(get_db)):
    """图片旋转 90 度：追加新版本（4.9 旋转工具）。"""
    if body.direction not in ("cw", "ccw"):
        raise HTTPException(400, "direction 仅支持 cw / ccw")
    found = _latest_version_of_kind(db, project_id, node_id, "image")
    if not found:
        raise HTTPException(400, "节点没有可编辑的图片版本")
    src_version, src_asset = found
    if not src_asset.path or not Path(src_asset.path).exists():
        raise HTTPException(400, "图片产物文件缺失")

    label = "右旋 90°" if body.direction == "cw" else "左旋 90°"
    out_path = artifact_dir_for(project_id) / f"rotate_{uuid.uuid4().hex[:12]}.png"
    transpose = "1" if body.direction == "cw" else "2"
    file_path, fallback = _edit_image_file(
        Path(src_asset.path), out_path, f"transpose={transpose}",
        f"旋转占位：{Path(src_asset.path).name} {label}",
    )
    version, asset = _append_edited_version(
        db, project_id, node_id, file_path, "image",
        f"图片旋转（{label}）", [src_asset.id],
        {"edit": "rotate", "direction": body.direction,
         "source_artifact_id": src_asset.id, "fallback": fallback},
    )
    return _version_dict(version, asset)


@router.post("/{node_id}/crop")
def crop_image(node_id: str, project_id: str, body: CropIn,
               db: Session = Depends(get_db)):
    """图片按比例居中裁剪：追加新版本（4.9 裁剪工具）。"""
    if body.ratio not in CROP_RATIOS:
        raise HTTPException(400, f"ratio 仅支持 {'/'.join(sorted(CROP_RATIOS))}")
    found = _latest_version_of_kind(db, project_id, node_id, "image")
    if not found:
        raise HTTPException(400, "节点没有可编辑的图片版本")
    src_version, src_asset = found
    if not src_asset.path or not Path(src_asset.path).exists():
        raise HTTPException(400, "图片产物文件缺失")

    size = _probe_size(Path(src_asset.path))
    if not size:
        raise HTTPException(400, "无法读取图片尺寸（ffprobe 不可用或文件异常）")
    rw, rh = (float(x) for x in body.ratio.split(":"))
    width, height = size
    if width / height > rw / rh:
        crop_w, crop_h = int(height * rw / rh), height
    else:
        crop_w, crop_h = width, int(width * rh / rw)
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    x = (width - crop_w) // 2
    y = (height - crop_h) // 2

    out_path = artifact_dir_for(project_id) / f"crop_{uuid.uuid4().hex[:12]}.png"
    file_path, fallback = _edit_image_file(
        Path(src_asset.path), out_path,
        f"crop={crop_w}:{crop_h}:{x}:{y}",
        f"裁剪占位：{Path(src_asset.path).name} {body.ratio}",
    )
    version, asset = _append_edited_version(
        db, project_id, node_id, file_path, "image",
        f"图片裁剪（{body.ratio} 居中）", [src_asset.id],
        {"edit": "crop", "ratio": body.ratio,
         "source_artifact_id": src_asset.id, "fallback": fallback},
    )
    return _version_dict(version, asset)


@router.post("/{node_id}/trim")
def trim_video(node_id: str, project_id: str, body: TrimIn,
               db: Session = Depends(get_db)):
    """视频时间区间裁取：追加新版本（4.9 剪辑工具）。"""
    if body.start < 0 or body.end <= body.start:
        raise HTTPException(400, "时间区间无效（需 0 <= start < end）")
    found = _latest_version_of_kind(db, project_id, node_id, "video")
    if not found:
        raise HTTPException(400, "节点没有可编辑的视频版本")
    src_version, src_asset = found
    if not src_asset.path or not Path(src_asset.path).exists():
        raise HTTPException(400, "视频产物文件缺失")

    out_path = artifact_dir_for(project_id) / f"trim_{uuid.uuid4().hex[:12]}.mp4"
    try:
        result = subprocess.run(
            [settings.ffmpeg_bin, "-y", "-loglevel", "error",
             "-ss", f"{body.start:.3f}", "-i", str(src_asset.path),
             "-t", f"{body.end - body.start:.3f}", "-c", "copy", str(out_path)],
            capture_output=True, text=True, timeout=300,
        )
        ok = result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        raise HTTPException(400, "视频剪辑失败（ffmpeg 不可用或编码不兼容）")

    version, asset = _append_edited_version(
        db, project_id, node_id, out_path, "video",
        f"视频剪辑（{body.start:.1f}s - {body.end:.1f}s）", [src_asset.id],
        {"edit": "trim", "start": body.start, "end": body.end,
         "source_artifact_id": src_asset.id},
    )
    return _version_dict(version, asset)


@projects_router.post("/{project_id}/concat", status_code=201)
def concat_assets(project_id: str, body: ConcatIn, db: Session = Depends(get_db)):
    """多选拼接（4.9 拼图/拼视频）：产物拼接后追加为 dest_node 新版本。"""
    if body.kind not in ("image", "video"):
        raise HTTPException(400, "kind 仅支持 image / video")
    if len(body.artifact_ids) < 2:
        raise HTTPException(400, "拼接至少需要 2 个产物")

    assets: list[Asset] = []
    for aid in body.artifact_ids:
        asset = db.get(Asset, aid)
        if not asset:
            raise HTTPException(400, f"产物不存在：{aid}")
        if asset.kind != body.kind:
            raise HTTPException(400, f"产物类型不匹配：{aid}（{asset.kind}）")
        if not asset.path or not Path(asset.path).exists():
            raise HTTPException(400, f"产物文件缺失：{aid}")
        assets.append(asset)

    n = len(assets)
    suffix = ".png" if body.kind == "image" else ".mp4"
    prefix = "concat" if body.kind == "video" else "merge"
    out_path = artifact_dir_for(project_id) / f"{prefix}_{uuid.uuid4().hex[:12]}{suffix}"
    prompt = f"拼接 {n} 张图片" if body.kind == "image" else f"拼接 {n} 段视频"

    try:
        if body.kind == "image":
            # 统一宽度 1280 后纵向拼接；mock SVG 输入不支持时降级占位
            inputs: list[str] = []
            for a in assets:
                inputs += ["-i", str(a.path)]
            chains = ";".join(f"[{i}:v]scale=1280:-2[v{i}]" for i in range(n))
            flow = "".join(f"[v{i}]" for i in range(n))
            result = subprocess.run(
                [settings.ffmpeg_bin, "-y", "-loglevel", "error", *inputs,
                 "-filter_complex", f"{chains};{flow}vstack=inputs={n}[out]",
                 "-map", "[out]", "-frames:v", "1", str(out_path)],
                capture_output=True, text=True, timeout=120,
            )
            ok = result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
            if not ok:
                out_path = mock_image(out_path, f"拼图占位：{n} 张图片")
                fallback = True
            else:
                fallback = False
        else:
            inputs = []
            for a in assets:
                inputs += ["-i", str(a.path)]
            chains = ";".join(
                f"[{i}:v]scale=1280:720,setsar=1[v{i}]" for i in range(n))
            result = subprocess.run(
                [settings.ffmpeg_bin, "-y", "-loglevel", "error", *inputs,
                 "-filter_complex",
                 f"{chains};{''.join(f'[v{i}]' for i in range(n))}concat=n={n}:v=1:a=0[out]",
                 "-map", "[out]", "-pix_fmt", "yuv420p", str(out_path)],
                capture_output=True, text=True, timeout=600,
            )
            ok = result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
            if not ok:
                raise HTTPException(400, "视频拼接失败（ffmpeg 不可用或编码不兼容）")
            fallback = False
    except HTTPException:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        if body.kind == "video":
            raise HTTPException(400, f"视频拼接失败：{exc}")
        out_path = mock_image(out_path, f"拼图占位：{n} 张图片")
        fallback = True

    version, asset = _append_edited_version(
        db, project_id, body.dest_node_id, out_path, body.kind,
        prompt, [a.id for a in assets],
        {"edit": f"concat_{body.kind}", "count": n, "fallback": fallback},
    )
    return _version_dict(version, asset)
