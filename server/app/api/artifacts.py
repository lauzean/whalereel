"""产物 API：文件流 + 缩略/元信息。

**媒体访问签名（方案 A，2026-09-30）**：`GET /{artifact_id}/file` 是浏览器
`<img>`/`<video>` 直接请求的入口——它带不了 Authorization 头，所以不能用
登录门禁保护。改成校验**签名临时链接**：`?e=<过期>&s=<签名>`，由
`POST /api/artifacts/sign` 签发（该签发接口挂登录鉴权）。
严格模式由 `AVS_MEDIA_REQUIRE_SIGNATURE` 控制，默认关闭以保持向后兼容。
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core import access, media_sign
from app.core.config import settings
from app.db.models import Asset, User
from app.db.session import get_db

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

MIME = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml", ".webp": "image/webp",
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".json": "application/json",
}


class SignIn(BaseModel):
    artifact_ids: list[str] = []
    files: list[str] = []


@router.post("/sign")
def sign_media(body: SignIn, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """签发短时访问链接（仅登录用户可签发）。

    一次可签多个（前端按屏批量续签，避免每张图一次请求）。返回
    `{artifacts: {id: url}, files: {rel: url}}`。

    **只给自己有权访问的产物签发**（2026-09-30 补齐）：签名一旦发出就是可访问的
    凭证（严格模式下更是唯一凭证），所以签发点必须与读取点同口径校验归属——
    否则「知道 id 就能拿签名」等于绕过 `get_file` 的校验。
    """
    ttl = settings.media_sign_ttl_sec
    signed: dict[str, str] = {}
    for aid in (body.artifact_ids or []):
        if not aid:
            continue
        try:
            access.ensure_artifact_access(db, aid, None, user_id=user.id,
                                          admin=bool(user.is_admin))
        except HTTPException:
            continue    # 无权访问的静默跳过（不泄露「这个 id 存在」）
        signed[aid] = media_sign.artifact_url(aid, ttl)
    # files 也要同口径判归属（2026-09-30 补）：签名一旦发出就是可访问凭证，
    # 旧实现给任意相对路径都签发，等于「知道路径就能跨租户取文件」。
    # 未登记成产物的文件不签发——读取侧（/files 无签名入口）同一口径按 404 处理。
    files_signed: dict[str, str] = {}
    for rel in (body.files or []):
        resolved = media_sign.resolve_artifact_relpath(rel)
        if resolved is None:
            continue
        asset = db.query(Asset).filter(Asset.path == str(resolved)).first()
        if asset is None:
            continue
        try:
            access.ensure_artifact_access(db, asset.id, None, user_id=user.id,
                                          admin=bool(user.is_admin))
        except HTTPException:
            continue
        url = media_sign.file_url(rel, ttl)
        if url:
            files_signed[rel] = url
    return {
        "ttl": ttl,
        "expires_at": int(__import__("time").time()) + ttl,
        "artifacts": signed,
        "files": files_signed,
    }


@router.get("/{artifact_id}/file")
def get_file(artifact_id: str, db: Session = Depends(get_db),
             e: str | None = Query(default=None),
             s: str | None = Query(default=None),
             authorization: str = Header(default="")):
    """产物文件流。

    两道校验（2026-09-30 补齐）：
    ① **归属**：产物必须属于调用者的某个项目（管理员不限）；
    ② **凭证**：严格模式下必须带有效签名；宽松模式下允许用登录令牌。

    旧实现只查「产物行存在」——登录后知道 id 就能取任意人的图/视频。
    """
    from app.api.auth import try_resolve_token

    user = try_resolve_token(authorization, db) if authorization else None
    signed_ok = media_sign.verify(
        media_sign.artifact_resource(artifact_id), e, s)
    if settings.media_require_signature:
        if not signed_ok:
            raise HTTPException(403, "产物访问链接无效或已过期，请刷新页面重新加载")
    elif user is None and not signed_ok:
        # 宽松模式：令牌或签名，二者必须有其一（不能裸奔）
        raise HTTPException(401, "未登录")

    # 归属校验：签名路径没有身份，靠签名本身授权（签名只发给有权访问的人）；
    # 令牌路径按用户校验归属
    if user is not None and not signed_ok:
        access.ensure_artifact_access(
            db, artifact_id, None, user_id=user.id, admin=bool(user.is_admin))

    asset = db.get(Asset, artifact_id)
    if not asset or not asset.path:
        raise HTTPException(404, "产物不存在")
    path = Path(asset.path)
    if not path.exists():
        raise HTTPException(410, "产物文件已被清理")
    # 文件必须在产物目录内（防「登记一条路径指向系统文件」的读取）
    if not media_sign.is_within(settings.artifacts_dir, path):
        raise HTTPException(404, "产物文件不存在")
    return FileResponse(str(path), media_type=MIME.get(path.suffix.lower(), "application/octet-stream"))


@router.get("/{artifact_id}")
def get_artifact(artifact_id: str, db: Session = Depends(get_db),
                 authorization: str = Header(default="")):
    """产物元信息（**校验归属**，2026-09-30 补齐）。"""
    from app.api.auth import try_resolve_token

    user = try_resolve_token(authorization, db) if authorization else None
    if user is None:
        raise HTTPException(401, "未登录")
    access.ensure_artifact_access(db, artifact_id, None,
                                  user_id=user.id, admin=bool(user.is_admin))
    asset = db.get(Asset, artifact_id)
    return {
        "id": asset.id, "kind": asset.kind, "path": asset.path,
        "meta": asset.meta_json, "created_at": asset.created_at,
    }
