"""FastAPI 入口。"""

import json
import logging
import os
import uuid
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api import artifacts, assets, auth, camera, drama, edits, frames, models, nodes, projects, provider_admin, sessions, skills, tasks
# 计费 API 模块与 core.billing（计费服务）同名：这里改名避开遮蔽
from app.api import billing as billing_api
from app.core import media_sign
from app.core.config import settings
from app.core.task_engine import engine
from app.core.gpu import detect_gpu
from app.db.session import init_db
from app.inference.pool import pool as inference_pool

# 文件日志：所有 app 模块与 uvicorn 日志落盘 data/logs/backend.log，
# 任务失败/模型加载异常的完整 traceback 据此定位（ Rotating 10MB × 5 备份）
_LOG_FILE = settings.data_dir / "logs" / "backend.log"
_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUPS = 5


def _setup_logging() -> None:
    """配置文件日志（幂等）：root + uvicorn 系 logger 统一接入 RotatingFileHandler。

    uvicorn 系 logger 自带控制台 handler 且不向上传播，需显式挂文件 handler；
    app 模块经 logging.getLogger(__name__) 输出，随 root handler 落盘。
    """
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler])
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.addHandler(file_handler)
        uv_logger.propagate = False  # 防止 uvicorn 自带控制台 handler 双写


_setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="鲸影视频创作工作台", version="0.1.0")


# ---------- 全局鉴权（2026-09-30）----------
#
# 为什么中间件而不是逐个挂依赖：业务接口共 122 个（projects/drama/assets/tasks/
# artifacts/nodes/frames/edits/camera/sessions），原先一个都没挂——「界面上有登录、
# 后端不查」是最危险的一种状态，且新增接口时极易再漏一个。中间件是**默认拒绝**，
# 新接口天然受保护。
#
# 白名单只有三类：
#   ① 登录接口与健康检查（没它们进不去）；
#   ② 媒体文件（`/files/**`、`/api/artifacts/*/file`）——浏览器 `<img>`/`<video>`
#      带不了 Authorization 头，只能靠方案 A 的签名链接；这类请求**要么签名有效、
#      要么带有效令牌**，两者都无则 401（不能对它们一律放行）。
#   ③ CORS 预检 OPTIONS。
_AUTH_PUBLIC_PREFIXES = ("/api/auth/login", "/api/health", "/docs", "/openapi.json",
                         "/redoc")

# 仍是出厂默认口令时**仍可访问**的路径（其余一律 403，把人挡在改密这一步之前）：
# 登录/退出/查看自身/改密 + 健康检查与接口文档。账号管理（/api/auth/users）不在内。
_ALLOW_WHEN_DEFAULT_PW = ("/api/auth/login", "/api/auth/logout", "/api/auth/me",
                          "/api/auth/change-password", "/api/health", "/docs",
                          "/openapi.json", "/redoc")


def _is_media_path(path: str) -> bool:
    """媒体文件路径判定（签名可能生效的入口）。"""
    return path.startswith("/files/") or (
        path.startswith("/api/artifacts/") and path.endswith("/file"))


def _media_query_ok(path: str, query: str) -> bool:
    """媒体签名校验（复用 core/media_sign；解析 query string 取 e/s）。"""
    from urllib.parse import parse_qs

    qs = parse_qs(query or "")
    e = (qs.get("e") or [None])[0]
    s = (qs.get("s") or [None])[0]
    if not e or not s:
        return False
    if path.startswith("/files/"):
        rel = path[len("/files/"):]
        return media_sign.verify(media_sign.file_resource(rel), e, s)
    # /api/artifacts/{id}/file
    aid = path[len("/api/artifacts/"):-len("/file")].strip("/")
    return media_sign.verify(media_sign.artifact_resource(aid), e, s)


def _auth_public(path: str) -> bool:
    return path.startswith(_AUTH_PUBLIC_PREFIXES)


def _path_params_from_routes(path: str) -> dict:
    """按路由模板把网址里的编号解析出来（中间件**必须自己算**）。

    为什么中间件拿不到：不管哪种中间件，都跑在**路由匹配之前**——实测
    `scope["path_params"]` 恒为空（连纯 ASGI 中间件也一样），所以「按参数名自动判
    归属」这道判定看不到 `/api/characters/{character_id}` 里的编号。

    做法：收集所有路由模板（这个 FastAPI 版本用惰性包含，被包含的子路由挂在
    `_IncludedRouter.original_router` 上），转成正则后匹配一次。结果缓存，不每次重算。
    """
    global _ROUTE_PATTERNS
    if _ROUTE_PATTERNS is None:
        import re as _re

        templates: list[str] = []

        def collect(routes) -> None:  # noqa: ANN001
            for r in routes:
                original = getattr(r, "original_router", None)
                if original is not None and getattr(original, "routes", None):
                    collect(original.routes)
                    continue
                sub = getattr(r, "routes", None)
                if sub and not getattr(r, "path", None):
                    collect(sub)
                    continue
                tpl = getattr(r, "path", "") or ""
                if tpl and "{" in tpl:
                    templates.append(tpl)

        try:
            collect(getattr(app.router, "routes", []))
        except Exception:  # noqa: BLE001 取不到就退化成「解析不出参数」
            logger.exception("路由模板收集失败，归属自动识别将退化为按清单判定")
            templates = []
        _ROUTE_PATTERNS = [
            (_re.compile("^" + _re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", t) + "$"), t)
            for t in templates
        ]
        logger.info("归属自动识别：已载入 %s 条带参数的路由模板", len(_ROUTE_PATTERNS))
    for pattern, _tpl in _ROUTE_PATTERNS:
        m = pattern.match(path)
        if m:
            return m.groupdict()
    return {}


_ROUTE_PATTERNS: list | None = None


def _check_resource_owner(path: str, query: dict, params: dict,
                          user_id: str) -> str:
    """按编号直取的资源做归属校验；命中越权时返回 404 文案，否则返回空串。

    **两道判定都走**：
    ① 清单（`core/resource_map.py` 的 RULES，按网址匹配）——处理网址里「第几段
       才是编号」有歧义的情况；
    ② **按参数名自动找表**——不看手写清单，直接从请求参数里认出 `xxx_id`，
       去同名表比 `owner_user_id`。这一道是为了兜住「新增接口忘了改清单」：
       只要参数按约定叫 `character_id`，就自动受保护；认不出名字的会**留日志告警**，
       把静默漏判变成看得见的信号。

    为什么两道都要：清单能处理复杂网址，自动识别能覆盖新增接口，互相补位。
    """
    from app.core.access import current_user_id
    from app.core.resource_map import owner_from_params, resource_owner
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        hit = resource_owner(db, path, query)
        if hit is not None:
            kind, value = hit
            if not value and kind == "artifact":
                # 查不到对应产物的编号/文件：**不放进读取口**（2026-09-30 补）。
                # 旧行为是「交给端点自己 404」，但 `/files` 端点在「磁盘上有这个
                # 文件、只是没登记成产物」时照样会把文件发出去——等于知道路径就能
                # 跨租户取图/取视频（配合「任务列表泄露 task_id/project_id」还能
                # 拼出完整路径）。管理员不受此限（上面已跳过非管理员分支）。
                return "文件不存在" if path.startswith("/files/") else "产物不存在"
            if value:
                if kind == "artifact":
                    # 产物归属：走同一份判定（看产物自己的 owner_user_id）
                    from app.core.access import ensure_artifact_access

                    try:
                        ensure_artifact_access(db, value, None, user_id=user_id,
                                              admin=False)
                    except HTTPException:
                        return "产物不存在"
                elif value != user_id:
                    # kind == "user"：值是这一行的权限归属，直接比，不再反查项目
                    return "资源不存在"
                return ""
        # ② 按参数名兜底（清单没覆盖到的新接口靠这条）
        denied, _seen = owner_from_params(db, params, user_id)
        return denied


class AuthGate:
    """全局鉴权闸（**纯 ASGI 中间件**，不是 `@app.middleware("http")`）。

    为什么必须是纯 ASGI 而不是常见的 `BaseHTTPMiddleware`（实测踩到，改了三次才
    找对）：**两种中间件都跑在路由匹配之前**——`scope["path_params"]` 恒为空
    （2026-10-07 计费方案评审时再次独立实测确认；也见 `_path_params_from_routes`
    的说明），所以「按参数名自动判归属」里的编号**必须由中间件自己按路由模板
    解析**（`_path_params_from_routes` 的职责），而不是指望 scope 里现成有。
    BaseHTTPMiddleware 还会把下游包进独立 task、scope 传递多一层转手；纯 ASGI
    直接读写 scope、行为最可控。代价是要自己拼 Request/Response，代码略长一点，值得。

    三层判定：
    ① 有没有有效令牌（媒体文件可用签名替代）；
    ② 请求里的资源是不是他的（`_check_resource_owner`）；
    ③ 匿名且都不过 → 401。
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = scope.get("path", "")

        if request.method == "OPTIONS" or path.startswith(_AUTH_PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        from app.api.auth import try_resolve_token
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            user = try_resolve_token(request.headers.get("authorization", ""), db)
        if user is not None:
            scope.setdefault("state", {})
            scope["state"]["user_id"] = user.id
            scope["state"]["is_admin"] = bool(user.is_admin)
            # 服务端强制改密（2026-09-30 复查 S10 补）：仍是出厂默认口令的用户，
            # 除登录/退出/查看自己/改密/健康检查外一律 403——旧实现「首登强制改密」
            # 只是前端把页面挡在改密页，curl 拿着默认口令换来的令牌可以畅通无阻。
            from app.api.auth import still_default_password
            if still_default_password(user) and not path.startswith(
                    _ALLOW_WHEN_DEFAULT_PW):
                await JSONResponse(
                    {"detail": "当前仍是出厂默认口令，请先修改密码后再使用"},
                    status_code=403)(scope, receive, send)
                return
            if not user.is_admin:
                # 路由参数中间件自己算（见 _path_params_from_routes 的说明）
                params = _path_params_from_routes(path)
                denied = _check_resource_owner(
                    path, dict(request.query_params), params, user.id)
                if denied:
                    logger.info("拒绝跨租户访问：%s %s", request.method, path)
                    await JSONResponse({"detail": denied},
                                       status_code=404)(scope, receive, send)
                    return
            await self.app(scope, receive, send)
            return

        # 未登录：媒体文件允许用有效签名替代令牌
        if _is_media_path(path) and _media_query_ok(path, request.url.query):
            await self.app(scope, receive, send)
            return

        if _is_media_path(path) and settings.media_require_signature:
            detail = "文件访问链接无效或已过期，请刷新页面重新加载"
        elif request.headers.get("authorization"):
            detail = "登录已过期，请重新登录"
        else:
            detail = "未登录"
        logger.info("拒绝匿名请求：%s %s（%s）", request.method, path, detail)
        await JSONResponse({"detail": detail}, status_code=401)(scope, receive, send)


# ---------- 积分计费闸（2026-10-07 用户拍板 33 号方案）----------


class BillingGate:
    """积分计费闸：收费端点**先原子扣分再放行**，失败自动退分。

    栈位置：CORS → AuthGate → BillingGate → 路由（本类必须在 AuthGate **之前**
    add——Starlette 后加的在更外层，AuthGate 要先解析出用户身份）。纯 ASGI
    中间件（同 AuthGate 的理由），从 scope["state"] 读 AuthGate 已填的 user_id。

    单请求流程：
    1. 只看 HTTP；OPTIONS / 公开路径 / 无身份（媒体签名等）→ 直接放行；
    2. 计费总开关关 → 放行；管理员且「管理员免费」配置开 → 放行；
    3. 价目匹配（内存缓存 + 30s TTL；**不用 scope 的 path_params**——实测纯
       ASGI 中间件也拿不到，路由匹配在它之后才发生）：未命中（免费端点）→ 放行；
    4. 原子扣费（run_in_threadpool，写事务不占事件循环）：
       - 余额不足 → **402**（结构化 body 供前端弹充值引导），业务完全不执行；
       - 成功 → txn_id 写 ContextVar（供 engine.submit 关联任务做失败退款）；
    5. 包装 send 抓 http.response.start 的**最终状态码**（不是「有没有异常」——
       客户端中途断开时响应头已发 2xx、业务实际已发生，不应退款）：
       2xx/3xx → 计费成立（mark_consumed）；其余 → 自动退款；
    6. 响应头未发就异常 → 退款后 re-raise（交给外层 500 处理）。

    失败安全取向：读配置/匹配价目出错 → fail-open 放行（宁可不收、不误拦；
    记日志）。「错收」比「少收」严重得多。
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if method == "OPTIONS" or _auth_public(path):
            await self.app(scope, receive, send)
            return
        state = scope.get("state") or {}
        user_id = state.get("user_id") or ""
        if not user_id:
            # AuthGate 放行但没身份的路径（媒体签名取件等）：与钱无关，放行
            await self.app(scope, receive, send)
            return

        from fastapi.concurrency import run_in_threadpool

        from app.core import billing

        try:
            # gate_config：带 TTL 缓存的只读配置（评审修正：每请求查 Setting
            # 是热路径开销——免费 GET/轮询也全都要过这道闸）
            cfg = await run_in_threadpool(billing.gate_config)
            if not cfg.get("enabled", True):
                await self.app(scope, receive, send)
                return
            if cfg.get("admin_free") and state.get("is_admin"):
                await self.app(scope, receive, send)
                return
            hit = await run_in_threadpool(billing.match_rule, method, path)
        except Exception:  # noqa: BLE001 fail-open：计费设施故障不拦业务
            logger.exception("计费配置/价目读取失败，本次请求按免费放行")
            await self.app(scope, receive, send)
            return
        if hit is None:
            await self.app(scope, receive, send)  # 免费端点
            return

        endpoint_key, price = hit  # (接口键, 预扣积分——内置清单，无需配置)
        if price <= 0:
            await self.app(scope, receive, send)
            return
        # 项目编号从路径确定性提取（路由模板都长这样 /api/projects/{id}/…；
        # 提取不到就留空，不影响计费）
        import re as _re
        _pm = _re.match(r"^/api/projects/([^/]+)", path)
        project_id = _pm.group(1) if _pm else None
        request_id = uuid.uuid4().hex

        try:
            result = await run_in_threadpool(
                billing.charge, user_id, endpoint_key, price,
                request_id=request_id, project_id=project_id)
        except Exception:  # noqa: BLE001 扣费本身异常：不放过（这次的账记不成）
            logger.exception("计费扣费异常：%s %s", method, path)
            await JSONResponse(
                {"detail": "计费服务暂时不可用，请稍后重试", "code": "billing_error"},
                status_code=503)(scope, receive, send)
            return

        if not result.get("ok"):
            logger.info("余额不足拦截：%s %s（需要 %s，现有 %s）",
                        method, path, result.get("required"), result.get("balance"))
            await JSONResponse(
                {
                    "detail": (f"积分余额不足：本次操作需要 {result.get('required')} 积分，"
                               f"当前余额 {result.get('balance')}。请充值后重试。"),
                    "code": "insufficient_credits",
                    "required": result.get("required"),
                    "balance": result.get("balance"),
                },
                status_code=402)(scope, receive, send)
            return

        txn_id = str(result.get("txn_id") or "")
        # ContextVar 必须在**事件循环线程**里 set（engine.submit 读它落任务关联、
        # providers 读它落 model_calls.request_id；若在 run_in_threadpool 内 set，
        # 只影响那份拷贝、传不回请求链）
        billing.set_request_ctx(txn_id, request_id)

        status_holder: dict = {"status": None}

        async def send_wrapper(message) -> None:  # noqa: ANN001
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status") or 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:  # noqa: BLE001 响应头未发就异常 → 退款后原样上抛
            try:
                await run_in_threadpool(
                    billing.refund_txn, txn_id,
                    reason="请求异常，自动退款")
            except Exception:  # noqa: BLE001 退款失败不可吞掉原异常
                logger.exception("请求异常退款失败（流水 %s，留给对账兜底）", txn_id)
            raise

        status = status_holder["status"]
        try:
            if status is not None and status < 400:
                # 按调用情况计费（2026-10-09 口径定稿）：有任务 → 等任务终态
                # 按成功任务的调用记录结算（此处统一触发一次兜全批 cached 等
                # 立即完成的场景——提交窗口保护保证 pending 时不会误判）；
                # 无任务（同步接口）→ 响应成功即可按本请求的调用记录结算
                task_n = await run_in_threadpool(
                    billing.count_txn_tasks, txn_id)
                if task_n:
                    await run_in_threadpool(billing.mark_consumed, txn_id)
                    await run_in_threadpool(
                        billing.settle_after_terminal, txn_id)
                else:
                    await run_in_threadpool(
                        billing.settle_usage, txn_id, request_id=request_id)
            else:
                if await run_in_threadpool(billing.auto_refund_enabled):
                    await run_in_threadpool(
                        billing.refund_txn, txn_id,
                        reason=f"请求失败（HTTP {status}），自动退款")
                else:
                    # 自动退款关闭（管理页开关）：失败也按调用成立收全款，
                    # 流水收尾为 consumed（不悬挂）
                    await run_in_threadpool(billing.mark_consumed, txn_id)
        except Exception:  # noqa: BLE001 结算写库失败不改变已发出的响应
            logger.exception("计费结算失败（流水 %s，留给启动恢复/对账兜底）", txn_id)


app.add_middleware(BillingGate)
app.add_middleware(AuthGate)

# CORS 白名单来源（逗号分隔；默认 `*`）：见下方 add_middleware 处说明
_CORS_ORIGINS = [o.strip() for o in
                 os.environ.get("AVS_CORS_ORIGINS", "*").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    # CORS 白名单（2026-09-30 复查 S12）：默认 `*` 保持既有本地/内网用法不变
    # （认证走 Authorization 头，`*` 不会自动带上凭证，风险有限）；公网部署
    # （云端服务）时用 `AVS_CORS_ORIGINS=https://你的域名` 收紧即可，不必改代码。
    allow_origins=_CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(billing_api.router)
app.include_router(projects.router)
app.include_router(assets.router)
app.include_router(tasks.router)
app.include_router(models.router)
app.include_router(provider_admin.router)
app.include_router(artifacts.router)
app.include_router(nodes.router)
app.include_router(frames.router)
app.include_router(edits.router)
app.include_router(edits.projects_router)
app.include_router(camera.router)
app.include_router(drama.router)
app.include_router(sessions.router)
app.include_router(skills.router)
app.include_router(tasks.ws_router)

# 产物静态服务：前端 <video>/<img> 直连。
#
# **安全说明（2026-09-30 方案 A）**：媒体标签带不了 Authorization 头，所以这里
# 不能挂登录门禁；保护方式改为**签名临时链接**（?e=&s=，见 core/media_sign.py）。
# 用一条显式路由取代 StaticFiles 挂载，理由有两条：
#   ① 静态挂载无法在服务前插校验（`StaticFiles` 的 `check_dir` 只管目录存在）；
#   ② 自己发文件能顺手做**路径归一化 + 目录归属校验**，把 `..` 穿越堵死。
# 严格模式由 AVS_MEDIA_REQUIRE_SIGNATURE 打开（默认关，保持向后兼容）——打开前
# 必须确认前端已全部走签名取址（apps/web/src/api/fileUrls.ts）。
@app.get("/files/{rel_path:path}")
def serve_file(rel_path: str, e: str | None = None, s: str | None = None):
    """旧产物静态文件（按项目分目录之前的产物散在 artifacts 根下）。

    鉴权由 `auth_gate` 中间件负责（签名或令牌二者之一）；这里做**路径归一化 +
    目录归属**校验，确保解析结果不越出产物根目录。
    """
    path = media_sign.resolve_within(settings.artifacts_dir, rel_path)
    if path is None or not path.is_file():
        raise HTTPException(404, "文件不存在")
    # 双重确认：resolve 后仍在产物根之内（软链接/大小写等边界情形）
    if not media_sign.is_within(settings.artifacts_dir, path):
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(path))


@app.on_event("startup")
async def startup() -> None:
    init_db()
    # 资产提示词回填（幂等；2026-09-30 用户规则：提示词框在解析阶段就应是完整的
    # ——旧数据只有点生成才补，这里启动时统一补齐，打开资产卡即见完整提示词）
    try:
        from app.api.drama import (backfill_asset_prompts,
                                   repair_scene_layout_figures,
                                   repair_storyboard_qa_scene_headers,
                                   repair_video_prompt_newlines)
        # 布局洗人物**先于**回填：回填按清洗后的布局重组装，一趟到位
        _fig = repair_scene_layout_figures()
        if _fig:
            print(f"[studio] 场景布局清洗：{_fig} 条（剔除混入的人物走位句）")
        _qh = repair_storyboard_qa_scene_headers()
        if _qh:
            print(f"[studio] 体检存档清理：{_qh} 条场次头误报（源行未认领）")
        _filled = backfill_asset_prompts()
        if _filled:
            print(f"[studio] 资产提示词回填：{_filled} 条（旧数据补上画风/约束/负向词）")
        _nl = repair_video_prompt_newlines()
        if _nl:
            print(f"[studio] 画面描述换行修补：{_nl} 条（【视频画面】后补回换行）")
    except Exception:  # noqa: BLE001 回填失败不阻塞启动
        logger.exception("资产提示词回填/换行修补失败（不阻塞启动）")
    recovered = engine.recover()
    # 计费（33 号方案）：价目与真实路由同步 + 匹配缓存装载 + 崩溃遗留流水恢复。
    # **必须在 engine.recover() 之后**——遗留 pending 流水的三分类依赖任务的最新
    # 状态（任务会被 recover 重新入队：仍在执行的置计费成立，不去退「还会跑的钱」）
    try:
        from app.core import billing
        # 计费闸清单自检（清单里的接口路径与真实路由对账；只告警不拦启动）
        # + 崩溃遗留流水恢复。费率在「模型管理」按模型服务端点配置，
        # 预扣额内置、无需配置（2026-10-09 用户口径定稿）。
        billing.verify_gated_endpoints(app)
        _handled = billing.recover_pending_transactions()
        print(f"[studio] 计费就绪：计费闸 {len(billing.GATED_ENDPOINTS)} 个"
              f"接口（费率按模型服务端点配置、预扣内置），"
              f"重启恢复 {_handled} 笔未结算流水")
    except Exception:  # noqa: BLE001 计费初始化失败不阻塞启动
        logger.exception("计费初始化失败（不阻塞启动）")
    await engine.start_workers()
    if recovered:
        print(f"[studio] 已清理重启遗留任务：{recovered} 条"
              "（中断时尚未开始的那部分已自动重新入队，见日志「重启恢复」）")
    # 本地推理资源池：启动空闲卸载器（AVS_MODEL_IDLE_UNLOAD 控制回收间隔，0 关闭）
    inference_pool.start_idle_unloader()
    info = detect_gpu()
    mode = "GPU 推理" if info.inference_ready else "mock 模式（推理依赖未安装）"
    print(f"[studio] 启动完成：{mode}")
    print(f"[studio] 数据目录：{settings.data_dir}")
    print(f"[studio] 模型目录：{settings.models_dir}")


@app.get("/api/health")
def health():
    return {"ok": True}
