"""数据库会话管理。"""

import logging
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


settings.data_dir.mkdir(parents=True, exist_ok=True)

_engine = create_engine(
    f"sqlite:///{(settings.data_dir / 'studio.db').as_posix()}",
    connect_args={"check_same_thread": False, "timeout": 15.0},
)


@event.listens_for(_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
    """每条连接启用 WAL + busy_timeout（审计 13 号：SQLite 并发写保护）。

    多 runner 线程与事件循环线程会并发写（trace 单次 commit 可达数百 KB），
    默认 journal 模式下容易 `database is locked`——该异常若落在失败处理路径
    会连锁触发泳道死亡（13 号 A）。WAL 允许读写并发，busy_timeout 让写锁
    等待而非立刻抛错（15s 覆盖常规写入窗口）。
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=15000")
    except sqlite3.Error:
        pass  # 非 SQLite 或权限受限：不阻断启动
    finally:
        cur.close()


SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


# ---------- 权限归属自动填充（2026-09-30 用户定的两层模型）----------
#
# `project_id` = 业务归属（属于哪个项目）；`owner_user_id` = 权限归属（归谁）。
# 判权限只看 owner_user_id，一步直达、不再沿「产物→哪个节点→哪个项目→项目主人」
# 反查（旧方案实测有 302 条产物根本反查不到归属）。
#
# **纪律：owner_user_id 一律由系统填，业务代码不许手填。** 这条纪律是这套方案
# 成立的前提——否则同一件事记两遍，早晚不一致。做法是挂 before_flush 钩子：
# 任何新行只要有这列、且还是空，就按它的 project_id（或经父级）推导出归属。
# 这样 20 多个写入点（产物一条就有生成/上传/编辑/抽帧/合成五条路径）一处覆盖，
# 不存在「漏了某个写入点」的可能。
def _owner_fill_models() -> dict:
    """延迟导入模型（session.py 被 models.py 间接导入，模块级 import 会成环）。"""
    from app.db.models import (
        AgentMessage, Asset, Character, CharacterAssetLink, CharacterImage,
        CreditAccount, CreditTransaction, Episode, ModelCall, Project, Prop,
        Scene, ScriptVersion, Session, Shot, ShotReview, Task,
    )
    from app.db.models import Canvas as CanvasModel
    from app.db.models import Message as MessageModel
    from app.db.models import NodeVersion

    return {
        "AgentMessage": AgentMessage, "Asset": Asset, "Canvas": CanvasModel,
        "Character": Character, "CharacterAssetLink": CharacterAssetLink,
        "CharacterImage": CharacterImage,
        "CreditAccount": CreditAccount, "CreditTransaction": CreditTransaction,
        "Episode": Episode,
        "Message": MessageModel, "ModelCall": ModelCall,
        "NodeVersion": NodeVersion, "Project": Project, "Prop": Prop,
        "Scene": Scene, "ScriptVersion": ScriptVersion, "Session": Session,
        "Shot": Shot, "ShotReview": ShotReview, "Task": Task,
    }


# 用户级表（没有 project_id、也没有项目父级）：owner_user_id 直接 = user_id。
# 积分两表属此列——用户级表的归属就是本人，不走项目推导（2026-10-07 计费方案）。
_OWNER_IS_USER_FIELDS = ("user_id",)


# (父级外键字段, 父级模型名)：自己没有 project_id 的表靠它一路查到项目
_OWNER_PARENT_CHAIN = (
    ("episode_id", "Episode"),        # 分镜 → 分集
    ("character_id", "Character"),    # 角色形象 → 角色
    ("session_id", "Session"),        # 会话消息 → 会话
)


@event.listens_for(SessionLocal, "before_flush")
def _auto_fill_owner(session, _flush_context, _instances) -> None:  # noqa: ANN001
    """新行落库前自动补 `owner_user_id`（按项目归属推导，失败则留空）。

    - 自己带 project_id 的表：查项目的 owner，同一次 flush 内缓存复用；
    - 只有父级外键的表（分镜→分集、角色形象→角色、消息→会话）：先取父级再取项目；
    - 产物这种两者都没有的：**留空**（不是漏，而是查不出归属）——迁移把历史数据
      标成 UNASSIGNED_OWNER（只管理员可见）；新产物在落库后由引用它的 node_version
      带出归属（见 task_engine._save_artifact）。
    """
    pending = [obj for obj in session.new
               if hasattr(type(obj), "owner_user_id")
               and not getattr(obj, "owner_user_id", "")]
    if not pending:
        return
    models = _owner_fill_models()
    project_owner: dict[str, str] = {}

    def owner_of_project(pid: str) -> str:
        if not pid:
            return ""
        if pid not in project_owner:
            row = session.get(models["Project"], pid)
            project_owner[pid] = (getattr(row, "owner_user_id", "") or "") if row else ""
        return project_owner[pid]

    for obj in pending:
        try:
            # 用户级表（如积分账户/流水）：归属 = 本人（user_id 列），一步直达。
            # **必须在 project_id 之前判**：这类表没有项目维度（2026-10-07 计费方案，
            # 评审核实：只按 project_id/父级链推导会恒填不上）。
            uid = getattr(obj, "user_id", "") or ""
            if uid:
                obj.owner_user_id = uid
                continue
            pid = getattr(obj, "project_id", "") or ""
            if pid:
                obj.owner_user_id = owner_of_project(pid)
                continue
            for fk, model_name in _OWNER_PARENT_CHAIN:
                parent_id = getattr(obj, fk, "") or ""
                if not parent_id:
                    continue
                parent = session.get(models[model_name], parent_id)
                if parent is None:
                    break
                ppid = getattr(parent, "project_id", "") or ""
                if ppid:
                    obj.owner_user_id = owner_of_project(ppid)
                elif getattr(parent, "owner_user_id", ""):
                    # 父级自己也没有项目（全局角色）→ 继承父级的归属
                    obj.owner_user_id = parent.owner_user_id
                break
        except Exception:  # noqa: BLE001 归属填充失败不该挡住业务写入
            logger.exception("自动填归属失败：%s", type(obj).__name__)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    import app.db.models  # noqa: F401 确保模型注册

    _rebuild_stale_characters()
    Base.metadata.create_all(_engine)
    _migrate_costume_columns()
    _seed_builtin_admin()
    # 多租户：补 projects.owner_user_id 并把历史项目归给管理员（必须在 seed 之后，
    # 否则全新库里还没有用户可归属）
    _migrate_project_owner()
    # 计费相关列（33 号方案：tasks.billing_txn_id / billing_rules 新列等）。
    # **必须排在 _migrate_owner_columns 之前**——后者会用 ORM 查各表全列
    # （leftovers 统计等），新列还没 ALTER 出来时会 `no such column` 启动崩溃。
    # 2026-10-07 正式库重启实测抓出：临时库因 create_all 建表即含新列而被掩盖。
    _migrate_billing_columns()
    # 两层模型（2026-09-30）：给其余表补 owner_user_id 并回填历史数据
    _migrate_owner_columns()
    # .env 五槽位服务配置一次性导入 DB 端点体系（幂等，端点表非空即跳过）
    from app.core.providers import migrate_env_providers

    migrate_env_providers()


def _alter_ignore_dup(conn, sql: str) -> None:  # noqa: ANN001
    """执行 ALTER，忽略「列已存在」（审计 13 号：并发迁移保护）。

    两个进程同时 init_db（服务启动 + 验证脚本）时，双方都可能读到「列不存在」
    再各自 ALTER——后到者抛 `duplicate column name` 会让 init_db 上抛、服务
    起不来。列已存在即视为迁移完成。

    **2026-09-30 修**：只捕 `sqlite3.OperationalError` 不够——SQLAlchemy 2.x 的
    `exec_driver_sql` 会把 DBAPI 异常**包成 `sqlalchemy.exc.OperationalError`**
    再抛，于是幂等保护形同虚设：第二次启动/第二个进程直接崩在迁移上（实测
    「库已建好后再跑一次 init_db」即复现）。改为按**错误文案**判定，两种类型都放行。
    """
    try:
        conn.exec_driver_sql(sql)
    except Exception as e:  # noqa: BLE001 类型不止一种，按文案判更可靠
        if "duplicate column name" not in str(e).lower():
            raise
        logger.debug("列已存在，跳过迁移：%s", sql[:80])


def _migrate_costume_columns() -> None:
    """旧库补列（create_all 不做列变更）：妆造映射 + 角色定位 + 画面描述 + 风格锚点。"""
    with _engine.connect() as conn:
        # 风格锚点（20 号 §2）：空值 = 不指定，旧项目行为不变
        proj_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(projects)")]
        if proj_cols and "style_anchor" not in proj_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE projects ADD COLUMN style_anchor VARCHAR(40) DEFAULT ''"
            )
        # 首帧精炼开关（23 号批次 3）：**默认 1（开）**——新老项目一致。
        # 存量首帧不受影响（只在提交新首帧时读取），关掉即退回改造前行为。
        if proj_cols and "refine_first_frame" not in proj_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE projects ADD COLUMN refine_first_frame BOOLEAN DEFAULT 1"
            )
        # 三视图自动补齐开关（2026-09-29）：**默认 1（开）**——新老项目一致。
        # 存量角色不会被追溯补图（只在"基础形象**生成完成**"这个时点触发），
        # 关掉即回到手动点按钮的行为。
        if proj_cols and "auto_three_view" not in proj_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE projects ADD COLUMN auto_three_view BOOLEAN DEFAULT 1"
            )
        img_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(character_images)")]
        if img_cols and "episodes_json" not in img_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE character_images ADD COLUMN episodes_json JSON DEFAULT '[]'"
            )
        shot_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(shots)")]
        if shot_cols and "costume_map_json" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN costume_map_json JSON DEFAULT '{}'"
            )
        # 导演级视频画面描述（@角色/@场景 引用，生成视频时转换为图N 引用）
        if shot_cols and "video_prompt" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN video_prompt TEXT DEFAULT ''"
            )
        # 剧本原文摘抄（1:1 对齐凭据，分镜卡可查原文）
        if shot_cols and "excerpt" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN excerpt TEXT DEFAULT ''"
            )
        # 道具引用（ID 级）+ 转场类型 + 镜头状态账本（跨镜连续性锚点）
        if shot_cols and "prop_ref_ids_json" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN prop_ref_ids_json JSON DEFAULT '[]'"
            )
        if shot_cols and "transition_in" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN transition_in VARCHAR(20) DEFAULT ''"
            )
        if shot_cols and "state_json" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN state_json JSON DEFAULT '{}'"
            )
        # 镜头结束状态账本（批次 2c §32.6：状态链对账的出口态）
        if shot_cols and "end_state_json" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN end_state_json JSON DEFAULT '{}'"
            )
        # 多切关键帧（批次 4 §35.2：key_scenes 段内 2 切）
        if shot_cols and "cuts_json" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN cuts_json JSON DEFAULT '[]'"
            )
        # 镜头目的与已知脆弱点（批次 2b §32.5：审片重点核对 + 模型路由建议）
        if shot_cols and "purpose" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN purpose VARCHAR(20) DEFAULT ''"
            )
        if shot_cols and "risks_json" not in shot_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shots ADD COLUMN risks_json JSON DEFAULT '[]'"
            )
        char_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(characters)")]
        scene_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(scenes)")]
        # 场景空间布局声明（批次 4 补充：出入口/门窗方位——空间连续性法典）
        if scene_cols and "layout" not in scene_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE scenes ADD COLUMN layout TEXT DEFAULT ''"
            )
        # 端点多模态标记（批次 3.5：视觉审片判级的准入开关）
        ep_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(provider_endpoints)")]
        if ep_cols and "multimodal" not in ep_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE provider_endpoints ADD COLUMN multimodal BOOLEAN DEFAULT 0"
            )
        # 项目画幅比例（新建项目设定，文生视频按此指定参数）
        proj_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(projects)")]
        # 项目视频分辨率档（480p/720p/1080p；端点按能力映射）
        if proj_cols and "video_resolution" not in proj_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE projects ADD COLUMN video_resolution VARCHAR(10) DEFAULT '720p'"
            )
        # 项目描述（首页卡片展示，可空）
        if proj_cols and "description" not in proj_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE projects ADD COLUMN description TEXT DEFAULT ''"
            )
        if proj_cols and "aspect_ratio" not in proj_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE projects ADD COLUMN aspect_ratio VARCHAR(10) DEFAULT '16:9'"
            )
        # 国家预设（默认中国，文生图/文生视频据此约束人物外貌）
        if proj_cols and "country" not in proj_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE projects ADD COLUMN country VARCHAR(50) DEFAULT '中国'"
            )
        # 项目生成模型预设（{"t2i": "...", "t2v": "..."}，空对象 = 未设置用系统默认）
        if proj_cols and "model_presets" not in proj_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE projects ADD COLUMN model_presets JSON DEFAULT '{}'"
            )
        # 分集流水线字段（18 号 §55）：当集正文/原文区间/分集要点表/审核结果/
        # 制作状态。旧库补列后 body 为空，由建档阶段从剧本原文按集头切分回填
        epi_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(episodes)")]
        for col, ddl in (
            ("body", "TEXT DEFAULT ''"),
            ("char_start", "INTEGER DEFAULT 0"),
            ("char_end", "INTEGER DEFAULT 0"),
            ("synopsis", "TEXT DEFAULT ''"),
            ("beats_json", "JSON DEFAULT '[]'"),
            ("hooks_json", "JSON DEFAULT '[]'"),
            ("plan_source", "VARCHAR(10) DEFAULT 'head'"),
            ("audit_json", "JSON DEFAULT '{}'"),
            ("produce_status", "VARCHAR(20) DEFAULT 'planned'"),
        ):
            if epi_cols and col not in epi_cols:
                _alter_ignore_dup(conn,
                    f"ALTER TABLE episodes ADD COLUMN {col} {ddl}")
        # 资产来源集号（18 号：资产改为逐集增量解析，资产页要标注「第 N 集解析」）
        # 出场顺序（资产列表按「来源集号 + 出场顺序」排；0 = 未记录，回退原排序）
        for table in ("characters", "scenes", "props"):
            cols = [r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")]
            if cols and "origin_seq" not in cols:
                _alter_ignore_dup(conn,
                    f"ALTER TABLE {table} ADD COLUMN origin_seq INTEGER DEFAULT 0")
            if cols and "order_seq" not in cols:
                _alter_ignore_dup(conn,
                    f"ALTER TABLE {table} ADD COLUMN order_seq INTEGER DEFAULT 0")
        # 任务过程轨迹（解析可观测：进度步骤 + 思考轮次）
        task_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(tasks)")]
        if task_cols and "trace_json" not in task_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE tasks ADD COLUMN trace_json JSON DEFAULT '{}'"
            )
        # 角色定位（主角/配角，LLM 按剧本定义）
        if char_cols and "role" not in char_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE characters ADD COLUMN role VARCHAR(20) DEFAULT '配角'"
            )
        # 定妆图/场景主图生图提示词（LLM 基于剧本生成，可编辑）
        if char_cols and "image_prompt" not in char_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE characters ADD COLUMN image_prompt TEXT DEFAULT ''"
            )
        if scene_cols and "image_prompt" not in scene_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE scenes ADD COLUMN image_prompt TEXT DEFAULT ''"
            )
        # 角色音色参考音频（用户上传的 Asset.id，生成视频时作配音音色参考）
        if char_cols and "voice_ref_asset_id" not in char_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE characters ADD COLUMN voice_ref_asset_id VARCHAR(32)"
            )
        # 场景「设为生效」版本指针（空 = 用最新版本）
        if scene_cols and "preferred_version_id" not in scene_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE scenes ADD COLUMN preferred_version_id VARCHAR(32)"
            )
        # 组内环境锁定开关（32 号）：旧项目默认开
        if proj_cols and "scene_group_lock" not in proj_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE projects ADD COLUMN scene_group_lock BOOLEAN DEFAULT 1"
            )
        # 场景组（32 号方案）：同一真实空间里的成员；空 = 不入组（旧数据零变化）。
        # 组表 scene_groups 由 create_all 新建，无需 ALTER。
        if scene_cols and "group_id" not in scene_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE scenes ADD COLUMN group_id VARCHAR(32)"
            )
        # 重出靶子（2026-10-10）：盲摇预算按「首帧 / 视频」分账——旧口径把首帧
        # 重摇算进视频预算，实测把镜的视频重出整个锁死。旧行空串=归属不可辨，
        # 预算计数不计入（见 drama.generate_shots 的注释）。
        sr_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(shot_reviews)")]
        if sr_cols and "target" not in sr_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shot_reviews ADD COLUMN target VARCHAR(10) DEFAULT ''"
            )
        # 形象「设为生效」版本指针（同场景机制：同形象记录抽卡多版后指定生效版）
        if img_cols and "preferred_version_id" not in img_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE character_images ADD COLUMN preferred_version_id VARCHAR(32)"
            )
        # 抽卡版本命名（用户可改；空 = 前端展示默认名「版本N」）
        ver_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(node_versions)")]
        if ver_cols and "name" not in ver_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE node_versions ADD COLUMN name VARCHAR(100) DEFAULT ''"
            )
        # 版本号唯一约束（新库由 create_all 建；旧库补索引，先清理历史撞号行）
        if ver_cols:
            try:
                conn.exec_driver_sql(
                    "DELETE FROM node_versions WHERE rowid NOT IN (SELECT MIN(rowid) "
                    "FROM node_versions GROUP BY project_id, node_id, seq)")
                conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_node_version_seq "
                    "ON node_versions (project_id, node_id, seq)")
            except sqlite3.Error:
                logger.exception("node_versions 唯一索引创建失败（继续启动）")
        # 重摇原因（25 号 2.4）：旧库补列——历史记录一律为空（当时的界面没有
        # 采集入口），新记录由「重摇必选原因」写入
        review_cols = [r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(shot_reviews)")]
        if review_cols and "reason" not in review_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shot_reviews ADD COLUMN reason VARCHAR(20) DEFAULT ''"
            )
        # 记录来源（submit/verdict/auto，见 drama.REVIEW_ORIGIN_*）：统计口径
        # 必须能区分「用户真的摇过」与「人工裁决/模型判级」
        if review_cols and "origin" not in review_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE shot_reviews ADD COLUMN origin VARCHAR(10) DEFAULT ''"
            )
        # 模型调用留痕（2026-09-30，调用日志页「查看详情」）：完整 URL 与
        # 请求/响应正文（写入时已打码敏感字段并截断，见 providers._clip_trace）
        mc_cols = [r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(model_calls)")]
        if mc_cols and "url" not in mc_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE model_calls ADD COLUMN url VARCHAR(500) DEFAULT ''"
            )
        if mc_cols and "request_json" not in mc_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE model_calls ADD COLUMN request_json TEXT DEFAULT ''"
            )
        if mc_cols and "response_json" not in mc_cols:
            _alter_ignore_dup(conn,
                "ALTER TABLE model_calls ADD COLUMN response_json TEXT DEFAULT ''"
            )
        # 任务表查询索引（审计 13 号：list_tasks 按 project_id + created_at 排序
        # 且带 batch_id 过滤，此前无索引 → 前端 2s 轮询最长 30 分钟全表扫描）
        try:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_tasks_project_created "
                "ON tasks (project_id, created_at DESC)")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_tasks_batch ON tasks (batch_id)")
        except sqlite3.Error:
            logger.exception("tasks 索引创建失败（继续启动）")
        conn.commit()


def _rebuild_stale_characters() -> None:
    """旧版 characters 表（voice/updated_at 列）与 M3 结构不符。

    create_all 不做列变更；旧表从未有写入方（无 API 使用），直接删除重建。
    """
    with _engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(characters)")]
        if cols and "voice_desc" not in cols:
            conn.exec_driver_sql("DROP TABLE characters")
            conn.commit()


def _seed_builtin_admin() -> None:
    """内置管理员种子（幂等）：仅 users 表为空时创建 admin 账号。

    默认口令取自 app.core.security 常量（admin/admin123），首启后建议
    立即在「账号管理」改密。

    **安全提示（2026-09-30）**：默认口令仍在（改动它会破坏"首次进得去"的体验），
    但登录后 `/api/auth/me` 会带 `must_change_password`，前端据此强制引导改密
    ——见 api/auth.py 与 apps/web/src/pages/LoginPage.tsx。
    """
    from sqlalchemy import select

    from app.core.security import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME, hash_password
    from app.db.models import User

    with SessionLocal() as db:
        if db.scalars(select(User.id)).first() is not None:
            return  # 已有用户（含历史库），不再种子
        db.add(User(username=DEFAULT_ADMIN_USERNAME,
                    password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
                    is_admin=True))
        db.commit()
        logger.info("已创建内置管理员账号：%s（请尽快修改默认密码）",
                    DEFAULT_ADMIN_USERNAME)


def _migrate_project_owner() -> None:
    """给 projects 补 `owner_user_id` 并把历史项目归给管理员（2026-09-30 多租户）。

    为什么必须显式迁移：`create_all` 不会给已存在的表加列，而多租户的读侧过滤
    依赖这一列——不补列会在第一次查询时报 `no such column`。

    归属规则：历史项目（owner 为空）一律归**最早创建的管理员**；库里没有管理员时
    归第一个用户；一个用户都没有（全新库）时留空，等首个用户创建后由
    `claim_orphan_projects` 认领。
    """
    from sqlalchemy import select

    from app.db.models import Project, User

    with SessionLocal() as db:
        # ① 补列（幂等：已存在会抛 duplicate，由 _alter_ignore_dup 吞掉）
        with _engine.connect() as conn:
            _alter_ignore_dup(conn, "ALTER TABLE projects ADD COLUMN owner_user_id VARCHAR(32)")
            _alter_ignore_dup(conn, "ALTER TABLE tasks ADD COLUMN initiator_user_id VARCHAR(32)")
            try:
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_projects_owner ON projects (owner_user_id)")
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_tasks_initiator "
                    "ON tasks (initiator_user_id)")
            except sqlite3.Error:
                logger.exception("projects/tasks 归属索引创建失败（继续启动）")
            conn.commit()

        # ② 历史项目归管理员（无管理员则归第一个用户）
        orphans = db.scalars(
            select(Project).where((Project.owner_user_id == "") |
                                  (Project.owner_user_id.is_(None)))).all()
        if not orphans:
            return
        owner = db.scalars(
            select(User).where(User.is_admin.is_(True))
            .order_by(User.created_at)).first() or db.scalars(
            select(User).order_by(User.created_at)).first()
        if owner is None:
            logger.warning("库中有 %s 个项目但没有任何用户，暂不归属"
                           "（首个用户创建后自动认领）", len(orphans))
            return
        for p in orphans:
            p.owner_user_id = owner.id
        db.commit()
        logger.info("已把 %s 个历史项目归给用户 %s", len(orphans), owner.username)


def _migrate_owner_columns() -> None:
    """给各表补 `owner_user_id` 并回填历史数据（2026-09-30 两层模型）。

    回填规则（全部是确定性推导，不做语义猜测）：
    1. 自己有 project_id 的表 → 取该项目的 owner；
    2. 分镜 → 按 episode_id 查分集所属项目；角色形象 → 按 character_id 查角色；
       会话消息 → 按 session_id 查会话；
    3. **产物** → 按「哪个 node_version 引用过它」查该版本的项目；查不到的
       （实测 302 条）统一标 `UNASSIGNED_OWNER`：权限上等于「谁都不属于」，
       于是只有管理员可见（用户 2026-09-30 拍板选 A）。

    幂等：只补「还是空」的行，重复启动不会覆盖已有归属。
    """
    from app.db.models import (
        UNASSIGNED_OWNER, Asset, Canvas, Character, CharacterImage, Episode,
        Message, NodeVersion, Project, Prop, Scene, ScriptVersion,
        Session as SessionModel, Shot, ShotReview, Task,
    )
    from sqlalchemy import text

    with SessionLocal() as db:
        # ① 补列（create_all 不给已存在的表加列）
        with _engine.connect() as conn:
            for table in ("projects", "episodes", "shots", "characters", "scenes",
                          "props", "character_images", "node_versions", "assets",
                          "tasks", "shot_reviews", "sessions", "messages",
                          "agent_messages", "script_versions", "model_calls",
                          "canvases", "character_asset_links"):
                _alter_ignore_dup(
                    conn, f"ALTER TABLE {table} ADD COLUMN owner_user_id VARCHAR(32)")
                try:
                    conn.exec_driver_sql(
                        f"CREATE INDEX IF NOT EXISTS ix_{table}_owner "
                        f"ON {table} (owner_user_id)")
                except sqlite3.Error:
                    logger.exception("%s.owner 索引创建失败（继续启动）", table)
            conn.commit()

        # ② 回填：项目 → 子表（一条 UPDATE ... WHERE project_id = ? 一次搞定）
        #
        # **必须先 flush**：下面用的是批量 UPDATE（绕过 ORM 的对象状态），而会话里
        # 可能还有未落库的改动；不先 flush，批量更新会作用在**旧数据**上，导致
        # 「一行都没回填」而毫无报错（实测踩到：61 个分集全部漏填）。
        db.flush()
        # 先按项目归属批量刷一遍（一条 SQL 覆盖所有子表，比逐行 ORM 快且稳）
        for model, table in ((Episode, "episodes"), (Scene, "scenes"),
                             (Prop, "props"), (NodeVersion, "node_versions"),
                             (Task, "tasks"), (ShotReview, "shot_reviews"),
                             (SessionModel, "sessions"),
                             (ScriptVersion, "script_versions"),
                             (Canvas, "canvases")):
            db.execute(text(
                f"UPDATE {table} SET owner_user_id = ("
                f"  SELECT p.owner_user_id FROM projects p "
                f"  WHERE p.id = {table}.project_id)"
                f" WHERE (owner_user_id IS NULL OR owner_user_id = '')"))
        # 角色表有「全局角色」（project_id 为空），同一句 SQL 天然跳过它们
        db.execute(text(
            "UPDATE characters SET owner_user_id = ("
            "  SELECT p.owner_user_id FROM projects p "
            "  WHERE p.id = characters.project_id)"
            " WHERE (owner_user_id IS NULL OR owner_user_id = '')"))
        db.commit()

        # ③ 经父级回填：分镜 → 分集
        db.execute(text(
            "UPDATE shots SET owner_user_id = ("
            "  SELECT e.owner_user_id FROM episodes e WHERE e.id = shots.episode_id)"
            " WHERE (owner_user_id IS NULL OR owner_user_id = '')"))
        # 角色形象 → 角色
        db.execute(text(
            "UPDATE character_images SET owner_user_id = ("
            "  SELECT c.owner_user_id FROM characters c "
            "  WHERE c.id = character_images.character_id)"
            " WHERE (owner_user_id IS NULL OR owner_user_id = '')"))
        # 会话消息 → 会话
        db.execute(text(
            "UPDATE messages SET owner_user_id = ("
            "  SELECT s.owner_user_id FROM sessions s WHERE s.id = messages.session_id)"
            " WHERE (owner_user_id IS NULL OR owner_user_id = '')"))
        # ④ 产物 → 引用它的 node_version 的项目归属
        db.execute(text(
            "UPDATE assets SET owner_user_id = COALESCE(("
            "  SELECT v.owner_user_id FROM node_versions v "
            "  WHERE v.artifact_id = assets.id AND v.owner_user_id <> '' LIMIT 1), '')"
            " WHERE (owner_user_id IS NULL OR owner_user_id = '')"))
        # ⑤ 仍查不到归属的产物 → 标 UNASSIGNED_OWNER（只管理员可见）
        unassigned = db.execute(text(
            "UPDATE assets SET owner_user_id = :u "
            "WHERE owner_user_id IS NULL OR owner_user_id = ''"),
            {"u": UNASSIGNED_OWNER}).rowcount
        # ⑥ 其余表的「孤儿行」——**有 project_id，但那个项目已经不在了**
        # （实测：删项目不会级联删子表数据，所以库里躺着一批指向已删项目的行）。
        # 它们和产物同理：查不出归属 → 标 UNASSIGNED_OWNER，语义明确、只管理员可见。
        #
        # 注意与「全局共享资源」区分：全局角色（`characters.project_id` 为空）
        # **不是**孤儿，它是跨项目资产库的一份共享数据，保持无归属即可（原本就
        # 只允许管理员改）。
        for table in ("episodes", "characters", "scenes", "props",
                      "node_versions", "tasks", "shot_reviews",
                      "script_versions", "canvases", "model_calls"):
            db.execute(text(
                f"UPDATE {table} SET owner_user_id = :u "
                f"WHERE (owner_user_id IS NULL OR owner_user_id = '') "
                f"AND project_id IS NOT NULL AND project_id <> ''"),
                {"u": UNASSIGNED_OWNER})
        # 分镜没有 project_id：按「它的分集还在不在」判定孤儿
        db.execute(text(
            "UPDATE shots SET owner_user_id = :u "
            "WHERE (owner_user_id IS NULL OR owner_user_id = '') "
            "AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id = shots.episode_id)"),
            {"u": UNASSIGNED_OWNER})
        # 角色形象也没有 project_id：按「它的角色还在不在 / 角色有没有归属」判
        db.execute(text(
            "UPDATE character_images SET owner_user_id = :u "
            "WHERE (owner_user_id IS NULL OR owner_user_id = '') "
            "AND NOT EXISTS (SELECT 1 FROM characters c "
            "                WHERE c.id = character_images.character_id "
            "                AND c.owner_user_id <> '')"),
            {"u": UNASSIGNED_OWNER})
        db.commit()
        if unassigned:
            logger.info("已把 %s 条查不出归属的旧产物标为 %s（仅管理员可见）",
                        unassigned, UNASSIGNED_OWNER)

        # ⑥ 汇总留痕：还剩多少行没有归属（便于排查写入点遗漏）
        leftovers: dict[str, int] = {}
        for model in (Episode, Shot, Character, Scene, Prop, CharacterImage,
                      NodeVersion, Task, ShotReview, SessionModel, Message,
                      ScriptVersion):
            n = db.query(model).filter(model.owner_user_id == "").count()
            if n:
                leftovers[model.__name__] = n
        if leftovers:
            logger.warning("以下表仍有 %s 行无权限归属（多为全局共享资源）：%s",
                           sum(leftovers.values()), leftovers)


def _migrate_billing_columns() -> None:
    """计费相关既有表加列（33 号方案，2026-10-07）：tasks / billing_rules /
    credit_transactions / model_calls。

    四张计费表本体由 create_all 建（新库直接带全部列）；这里只管**已存在表**
    的后补列（旧库升级）。幂等：列已存在由 _alter_ignore_dup 吞掉。
    """
    with _engine.connect() as conn:
        _alter_ignore_dup(
            conn, "ALTER TABLE tasks ADD COLUMN billing_txn_id VARCHAR(32)")
        # 按用量计费（2026-10-07 用户口径）：规则表加计价方式与三类单价 + 预扣
        for col, ddl in (
            ("mode", "VARCHAR(16) DEFAULT 'per_call'"),
            ("credits_per_1k_tokens", "INTEGER DEFAULT 0"),
            ("credits_per_image", "INTEGER DEFAULT 0"),
            ("credits_per_video_second", "INTEGER DEFAULT 0"),
            ("precharge", "INTEGER DEFAULT 0"),
        ):
            _alter_ignore_dup(conn,
                              f"ALTER TABLE billing_rules ADD COLUMN {col} {ddl}")
        # 流水加结算快照与结算幂等时间戳
        _alter_ignore_dup(conn,
                          "ALTER TABLE credit_transactions ADD COLUMN rule_json JSON DEFAULT '{}'")
        _alter_ignore_dup(conn,
                          "ALTER TABLE credit_transactions ADD COLUMN settled_at INTEGER DEFAULT 0")
        # 调用日志加请求编号（同步端点按它汇总用量结算）
        _alter_ignore_dup(conn,
                          "ALTER TABLE model_calls ADD COLUMN request_id VARCHAR(36) DEFAULT ''")
        # 模型服务端点加计费费率（2026-10-09 口径：按模型服务端点配置费率）
        for col, ddl in (
            ("credits_per_1k_tokens", "INTEGER DEFAULT 0"),
            ("credits_per_image", "INTEGER DEFAULT 0"),
            ("credits_per_video_second", "INTEGER DEFAULT 0"),
            ("bill_enabled", "BOOLEAN DEFAULT 1"),
        ):
            _alter_ignore_dup(
                conn, f"ALTER TABLE provider_endpoints ADD COLUMN {col} {ddl}")
        # 调用日志加行级积分（记录时按所用端点费率 × 用量算好存下；结算=SUM）
        _alter_ignore_dup(conn,
                          "ALTER TABLE model_calls ADD COLUMN credits INTEGER DEFAULT 0")
        try:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_tasks_billing_txn "
                "ON tasks (billing_txn_id)")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_model_calls_request "
                "ON model_calls (request_id)")
        except sqlite3.Error:
            logger.exception("计费相关索引创建失败（继续启动）")
        conn.commit()


def claim_orphan_projects(db, user_id: str) -> int:
    """把无归属项目划给指定用户（新建首个用户时调用），返回认领数量。

    单机自用场景：库是先用起来的、账号是后建的，不认领的话用户会"看不到自己
    原来的项目"——比"看到别人的"更伤。仅在**库中此前没有任何用户**时调用，
    所以不存在"抢别人项目"的问题（api/auth.py 的 create_user 里判断）。
    """
    from sqlalchemy import select

    from app.db.models import Project

    orphans = db.scalars(
        select(Project).where((Project.owner_user_id == "") |
                              (Project.owner_user_id.is_(None)))).all()
    for p in orphans:
        p.owner_user_id = user_id
    if orphans:
        db.commit()
        logger.info("首次创建用户，已认领 %s 个无归属项目", len(orphans))
    return len(orphans)
