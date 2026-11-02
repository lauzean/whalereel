"""视频抽帧 API（M2 P0，设计文档 4.9 抽帧工具 / 6.6 首尾帧转场）。

- 从节点最新视频版本抽帧（首帧/尾帧/当前时间点），产出图片产物
- 抽帧结果追加为节点新版本（source="edited"，编辑产物 = 新版本，可回退）
- ffmpeg 不可用或抽帧失败时优雅降级为 mock_image 占位（Windows 环境常无 ffmpeg）
- 分镜「全链首尾帧对齐」（drama.py link-frames）复用本模块的抽帧核心
"""

import hashlib
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.nodes import _next_seq, _version_dict
from app.core.config import artifact_dir_for, settings
from app.db.models import Asset, NodeVersion
from app.core.access import project_access_guard
from app.db.session import get_db
from app.runners.mock_helpers import mock_image

router = APIRouter(prefix="/api/nodes", tags=["frames"], dependencies=[Depends(project_access_guard)])

# 尾帧时间点预留余量（秒），避免 -ss 超出时长输出空帧
_TAIL_MARGIN = 0.1
# 时长未知时的默认值（与分镜默认时长一致）
_DEFAULT_DURATION = 5.0

_POSITION_LABEL = {"first": "首帧", "last": "尾帧", "current": "当前帧"}


class ExtractFrameIn(BaseModel):
    position: str = "current"  # first | last | current
    time_sec: float | None = None


def _ffprobe_bin() -> str | None:
    """定位 ffprobe：优先与 ffmpeg 同目录，其次查 PATH。"""
    ff = Path(settings.ffmpeg_bin)
    if str(ff.parent) not in ("", "."):
        candidate = ff.parent / f"ffprobe{ff.suffix}"
        if candidate.exists():
            return str(candidate)
    return shutil.which("ffprobe")


def _probe_duration(video_path: Path) -> float | None:
    """ffprobe 探测视频时长（秒）；不可用或失败返回 None。"""
    binary = _ffprobe_bin()
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _seek_time(position: str, time_sec: float | None, video_path: Path,
               duration_hint: float | None) -> float:
    """计算抽帧时间点：first=0，current=time_sec，last=时长-余量。"""
    if position == "first":
        return 0.0
    if position == "current":
        return max(0.0, float(time_sec or 0.0))
    # last：优先探测真实时长，其次调用方给的时长提示（mock 视频场景）
    duration = _probe_duration(video_path)
    if duration is None or duration <= 0:
        duration = duration_hint or _DEFAULT_DURATION
    return max(0.0, duration - _TAIL_MARGIN)


def _extract_frame_file(video_path: Path, out_path: Path,
                        seek: float) -> tuple[Path, bool]:
    """ffmpeg 抽单帧；ffmpeg 不可用或失败时回退 mock 占位图。返回 (文件, 是否降级)。"""
    try:
        result = subprocess.run(
            [settings.ffmpeg_bin, "-y", "-loglevel", "error",
             "-ss", f"{seek:.3f}", "-i", str(video_path),
             "-frames:v", "1", str(out_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return out_path, False
    except (OSError, subprocess.TimeoutExpired):
        pass
    # 降级：mock 占位（ffmpeg 缺失或抽帧失败，保证链路可继续）
    placeholder = mock_image(out_path, f"抽帧占位：{video_path.name} @ {seek:.1f}s")
    return placeholder, True


def _extract_last_frame_file(video_path: Path,
                             out_path: Path) -> tuple[Path, bool] | None:
    """从视频文件末尾抽尾帧：-sseof 自 EOF 前移定位，不依赖时长探测。

    此前按「探测时长-余量」seek 抽尾帧，探测失败用分镜时长兜底——兜底值
    大于视频实际时长时 -ss 越界输出空文件，会静默降级成 mock 占位图混进
    首帧链路。-sseof 彻底规避该类问题；返回 None 表示该方式不可用
    （ffmpeg 缺失/异常输出），交由调用方回退按 seek 的老链路。
    """
    try:
        result = subprocess.run(
            [settings.ffmpeg_bin, "-y", "-loglevel", "error",
             "-sseof", f"-{_TAIL_MARGIN}", "-i", str(video_path),
             "-frames:v", "1", str(out_path)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
        return out_path, False
    return None


def latest_video_artifact_id(db: Session, project_id: str,
                             node_id: str) -> str | None:
    """节点最新视频版本的视频产物 id（无视频版本返回 None）。"""
    src = _latest_video_version(db, project_id, node_id)
    return src.artifact_id if src else None


def has_frame_version_for(db: Session, project_id: str, dest_node_id: str,
                          src_artifact_id: str) -> bool:
    """dest 节点是否已存在引用同一源视频产物的抽帧版本（首尾帧幂等对齐判定）。"""
    versions = db.scalars(
        select(NodeVersion)
        .where(NodeVersion.node_id == dest_node_id,
               NodeVersion.project_id == project_id,
               NodeVersion.source == "edited")
    ).all()
    return any(src_artifact_id in (v.refs_json or []) for v in versions)


def _latest_video_version(db: Session, project_id: str,
                          node_id: str) -> NodeVersion | None:
    """节点最新的视频类型版本（按 seq 降序找第一个 artifact 为视频的版本）。"""
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
        if asset and asset.kind == "video":
            return v
    return None


def extract_frame_version(
    db: Session, project_id: str, src_node_id: str, dest_node_id: str,
    position: str, time_sec: float | None = None,
    duration_hint: float | None = None,
) -> tuple[NodeVersion, Asset] | None:
    """从 src_node 最新视频版本抽帧，追加为 dest_node 的新版本（source="edited"）。

    返回 (版本, 产物)；源节点没有视频版本时返回 None（由调用方决定跳过/报错）。
    """
    src = _latest_video_version(db, project_id, src_node_id)
    if not src or not src.artifact_id:
        return None
    video_asset = db.get(Asset, src.artifact_id)
    if not video_asset or not video_asset.path:
        return None

    video_path = Path(video_asset.path)
    out_path = artifact_dir_for(project_id) / f"frame_{uuid.uuid4().hex[:12]}.png"
    if position == "last":
        # 尾帧优先 -sseof 自 EOF 定位（不依赖时长探测，杜绝兜底时长越界出空帧）
        extracted = _extract_last_frame_file(video_path, out_path)
        if extracted is not None:
            frame_path, fallback = extracted
            frame_meta: dict = {"position": position, "eof_seek": True}
        else:
            # -sseof 不可用（ffmpeg 缺失/异常）：回退按 seek 的老链路
            seek = _seek_time(position, time_sec, video_path, duration_hint)
            frame_path, fallback = _extract_frame_file(video_path, out_path, seek)
            frame_meta = {"position": position, "seek_sec": round(seek, 3)}
    else:
        seek = _seek_time(position, time_sec, video_path, duration_hint)
        frame_path, fallback = _extract_frame_file(video_path, out_path, seek)
        frame_meta = {"position": position, "seek_sec": round(seek, 3)}

    digest = hashlib.sha256(frame_path.read_bytes()).hexdigest()[:16]
    asset = Asset(
        kind="image", path=str(frame_path), sha256=digest,
        meta_json={
            **frame_meta,
            "source_artifact_id": src.artifact_id, "fallback": fallback,
        },
    )
    db.add(asset)
    db.flush()

    seq = _next_seq(db, dest_node_id, project_id)
    version = NodeVersion(
        node_id=dest_node_id, project_id=project_id, seq=seq,
        source="edited", prompt=f"视频抽帧（{_POSITION_LABEL.get(position, position)}）",
        refs_json=[src.artifact_id], artifact_id=asset.id,
    )
    db.add(version)
    db.commit()
    return version, asset


@router.post("/{node_id}/extract-frame")
def extract_frame(node_id: str, project_id: str, body: ExtractFrameIn,
                  db: Session = Depends(get_db)):
    """视频抽帧：从节点最新视频版本抽帧，追加为该节点新版本（source=edited）。"""
    if body.position not in ("first", "last", "current"):
        raise HTTPException(400, f"无效抽帧位置：{body.position}")
    if body.position == "current" and (body.time_sec is None or body.time_sec < 0):
        raise HTTPException(400, "current 抽帧需要提供非负的 time_sec")

    result = extract_frame_version(
        db, project_id, node_id, node_id, body.position, body.time_sec,
    )
    if not result:
        raise HTTPException(400, "节点没有可抽帧的视频版本")
    version, asset = result
    return _version_dict(version, asset)
