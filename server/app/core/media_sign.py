"""媒体访问签名（方案 A：签名临时链接）。

**为什么需要它**（2026-09-30 安全整改）：

产物文件此前是「谁拿到 URL 谁就能看」——`/api/artifacts/{id}/file` 与
`/files/...` 静态挂载都没有任何校验。这是当时的**有意设计**：浏览器给
`<img src>` / `<video src>` 发请求时**不会带 Authorization 头**，前端只在
`fetch` 里注入令牌（`apps/web/src/api/client.ts`），所以「图片能显示」正建立在
「这两个入口不校验」之上。直接给它们挂 `Depends(get_current_user)` 会让全站
图片视频立刻全挂——这也是不能简单加门禁的原因。

**方案 A 的做法**：把「访问权」编码进 URL 本身。服务端用一把只存在于本机的
密钥对 `资源标识 + 过期时间` 做 HMAC 签名，签发短时链接；访问时校验签名与
有效期。好处：
- 媒体标签照旧能直接请求（不需要自定义头）；
- 链接会过期、且不能改（改一个字符签名就对不上），比"永久公开"强得多；
- 不依赖登录态，所以**已渲染的页面在令牌过期后仍能正常播放**。

**密钥来源**：优先环境变量 `AVS_MEDIA_SECRET`；未设置时首次启动生成随机密钥
并写入 `Setting` 表（`media_sign_secret`），保证重启后已签发的链接不失效。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# Setting 键：数据目录级密钥（进程重启后复用，避免已签发链接全部失效）
SECRET_SETTING_KEY = "media_sign_secret"
# 默认有效期（秒）：12 小时——覆盖一次创作会话，且足够短到降低链接外泄价值
DEFAULT_TTL_SEC = 12 * 3600
# 签名允许的时钟回拨/宽限（秒）
_CLOCK_SKEW_SEC = 60

_secret_cache: str | None = None


def _load_secret() -> str:
    """取签名密钥（环境变量 > Setting 表 > 首次生成并存库）。"""
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    env_secret = os.environ.get("AVS_MEDIA_SECRET", "").strip()
    if env_secret:
        _secret_cache = env_secret
        return _secret_cache
    from app.db.models import Setting

    try:
        with SessionLocal() as db:
            row = db.get(Setting, SECRET_SETTING_KEY)
            if row and row.value:
                _secret_cache = row.value
                return _secret_cache
            value = secrets.token_hex(32)
            if row:
                row.value = value
            else:
                db.add(Setting(key=SECRET_SETTING_KEY, value=value))
            db.commit()
            _secret_cache = value
            logger.info("媒体签名密钥已生成并落库（%s）", SECRET_SETTING_KEY)
            return _secret_cache
    except Exception:  # noqa: BLE001 落库失败时退回进程内随机密钥（重启后旧链接失效）
        logger.exception("媒体签名密钥读取失败，本次进程内临时生成")
        _secret_cache = secrets.token_hex(32)
        return _secret_cache


def reset_secret_cache() -> None:
    """清空密钥缓存（测试用：换目录/换库后必须重新加载）。"""
    global _secret_cache
    _secret_cache = None


def _sign(payload: str) -> str:
    secret = _load_secret().encode("utf-8")
    digest = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    # urlsafe 无填充：签名要直接进 query string，不能带 + / =
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_signature(resource: str, expires_at: int) -> str:
    """对 `资源标识::过期时间戳` 签名（资源标识 = artifact_id 或相对路径）。"""
    return _sign(f"{resource}::{int(expires_at)}")


def signed_query(resource: str, ttl_sec: int = DEFAULT_TTL_SEC,
                 now: int | None = None) -> str:
    """返回 `e=<过期>&s=<签名>`（拼在 URL 后面用；不含 `?`）。"""
    exp = int((now if now is not None else time.time()) + max(60, ttl_sec))
    return f"e={exp}&s={make_signature(resource, exp)}"


def verify(resource: str, expires_at: str | int | None,
           signature: str | None, now: int | None = None) -> bool:
    """校验签名与有效期（缺任一参数 / 过期 / 不匹配 → False）。"""
    if not expires_at or not signature:
        return False
    try:
        exp = int(str(expires_at).strip())
    except (TypeError, ValueError):
        return False
    current = int(now if now is not None else time.time())
    if exp + _CLOCK_SKEW_SEC < current:
        return False
    expected = make_signature(resource, exp)
    return hmac.compare_digest(expected, str(signature).strip())


# ---------- 产物 URL ----------

# 资源标识前缀（两类入口分开，避免「产物签名」被拿去访问任意文件）
ARTIFACT_PREFIX = "artifact"


def artifact_resource(artifact_id: str) -> str:
    return f"{ARTIFACT_PREFIX}:{artifact_id}"


def artifact_url(artifact_id: str, ttl_sec: int = DEFAULT_TTL_SEC) -> str:
    """产物的签名访问地址（前端 `<img>`/`<video>` 直接用）。"""
    return (f"/api/artifacts/{quote(str(artifact_id))}/file?"
            + signed_query(artifact_resource(str(artifact_id)), ttl_sec))


def file_resource(rel_path: str) -> str:
    """静态文件资源标识：**归一化后的相对路径**（`/files` 树内）。

    路径本身参与签名 ⇒ 不能把签名从 A 文件挪到 B 文件用；归一化顺手挡掉
    `..` 穿越（即便签名有效也不允许逃出产物根目录）。
    """
    return f"file:{normalize_rel_path(rel_path)}"


def file_url(rel_path: str, ttl_sec: int = DEFAULT_TTL_SEC) -> str | None:
    rel = normalize_rel_path(rel_path)
    if not rel:
        return None
    return f"/files/{quote(rel)}?" + signed_query(file_resource(rel), ttl_sec)


def normalize_rel_path(rel_path: str) -> str:
    """把用户传入的相对路径归一化：去绝对前缀、拒 `..`、统一为 posix 形式。

    返回空串表示非法（调用方按 404 处理）。这道归一化是**独立于签名**的第二
    道防线：即使签名有效，也只能落在产物根目录之内。
    """
    raw = (rel_path or "").replace("\\", "/").strip()
    if not raw:
        return ""
    pure = PurePosixPath(raw)
    parts: list[str] = []
    for part in pure.parts:
        if part in ("", "/", "."):
            continue
        if part == "..":
            return ""
        parts.append(part)
    if not parts:
        return ""
    return "/".join(parts)


def resolve_within(root: Path, rel_path: str) -> Path | None:
    """把相对路径解析到 root 之内；越界/非法返回 None。"""
    rel = normalize_rel_path(rel_path)
    if not rel:
        return None
    candidate = (root / rel).resolve()
    try:
        if not candidate.is_relative_to(root.resolve()):
            return None
    except OSError:
        return None
    return candidate


def is_within(root: Path, path: Path) -> bool:
    """path 是否落在 root 目录之内（用于「登记路径不得指向系统文件」的校验）。

    `assets.path` 是**绝对路径**来源多样（上传、编辑、外部登记），只校验「文件
    存在」不足以防读取任意本机文件——必须再确认它确实在产物目录里。
    """
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


def resolve_artifact_relpath(rel_path: str) -> Path | None:
    """把 `/files/<相对路径>` 解析成产物根目录下的真实路径（越界返回 None）。

    只读路径解析的**唯一入口**：调用方（如按 URL 反查 Asset 归属）不必自己拼
    目录，避免「同一件事两种拼法」。
    """
    from app.core.config import settings

    root = Path(getattr(settings, "artifacts_dir"))
    return resolve_within(root.joinpath("."), rel_path)
