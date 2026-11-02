# 「查不出归属」的行统一用它（如无任何引用记录的旧产物）：权限上等同于
# 「谁都不属于」，因此只有管理员可见（2026-09-30 用户拍板）。
UNASSIGNED_OWNER = "_unassigned"


"""ORM 模型（对齐设计文档第 8 章）。"""

import json
import time
import uuid
from pathlib import Path

from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> int:
    return int(time.time())


# 地区规范包文件（外貌约束 + 社会常识规范，按项目国家注入提示词；
# 枪支法律/官员着装等常识与国家强相关，写死单国会给其他国家的项目制造漏洞）
_NORMS_FILE = Path(__file__).resolve().parent.parent / "data" / "country_norms.json"


def load_country_norms() -> dict:
    """地区规范包（读取失败返回空 dict，不阻断解析与生成）。"""
    try:
        return json.loads(_NORMS_FILE.read_text(encoding="utf-8")).get("countries", {})
    except (OSError, json.JSONDecodeError):
        return {}


# 地区规范域 → 中文标签（拼接顺序即注入提示词的顺序）
_NORM_FIELD_LABELS = (
    ("legal_guns", "法律与枪支"),
    ("police_procedure", "警务与急救"),
    ("government", "政务与着装"),
    ("education", "教育"),
    ("medical", "医疗"),
    ("finance_payment", "金融支付"),
    ("communication_tech", "通讯科技"),
    ("transport", "交通"),
    ("food_etiquette", "饮食礼仪"),
    ("festival_rites", "婚丧节庆"),
    ("religion_taboo", "宗教禁忌"),
    ("climate_geography", "气候地理"),
    ("workplace", "职场"),
    ("housing", "居住"),
    ("media", "媒体"),
    ("units_currency", "计量货币"),
)


def country_face_hint(country: str | None) -> str:
    """国家 → 人物外貌约束短语（决定生成面孔的人种特征；未收录国家用泛化描述）。"""
    c = (country or "").strip()
    if not c:
        return ""
    face = load_country_norms().get(c, {}).get("face", "")
    return face or f"{c}人，符合{c}本地人的外貌与气质特征"


def country_norm_text(country: str | None) -> str:
    """国家 → 完整地区规范（外貌 + 分域社会常识），供剧本解析/画面描述等
    LLM 提示词注入；norms 支持分域 dict（推荐，按域标签拼接）与旧版长字符串
    两种格式；未收录国家只给外貌泛化约束，空国家返回空串。"""
    c = (country or "").strip()
    if not c:
        return ""
    info = load_country_norms().get(c, {})
    face = info.get("face") or f"{c}人，符合{c}本地人的外貌与气质特征"
    norms = info.get("norms")
    if isinstance(norms, str):
        # 旧版长字符串格式（手改兜底）
        return f"人物外貌：{face}；社会常识：{norms}" if norms else f"人物外貌：{face}"
    parts = [f"{label}：{norms.get(field)}"
             for field, label in _NORM_FIELD_LABELS if norms.get(field)]
    if not parts:
        return f"人物外貌：{face}"
    return f"人物外貌：{face}。" + "；".join(parts)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    # 归属用户（多租户最小实现，2026-09-30）：每个项目属于一个用户，普通用户
    # 只能看到/操作自己的项目，管理员可看全部。空串 = 历史数据（迁移时补成
    # 管理员 id；不用 nullable 是为了少一处 None 分支）。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    # canvas（创作Agent）| drama（短剧&漫剧）
    mode: Mapped[str] = mapped_column(String(20), default="canvas")
    # real（写实电影感）| 3d | anime（二次元）| guofeng（国风漫画）
    style_preset: Mapped[str] = mapped_column(String(20), default="real")
    # 风格锚点（美学第二级，20 号 §2）：preset 决定形式（画风规范/负面词），
    # anchor 决定具体美学锚点（如「王家卫风格的电影」）——官方两步走里的第二步，
    # 取值见 data/style_presets.json 的 anchors（空 = 不指定，行为与改造前一致）
    style_anchor: Mapped[str] = mapped_column(String(40), default="")
    # 首帧提示词精炼（23 号批次 3）：开启后每镜首帧在提交生图前，先用 LLM 精炼
    # 一次（删噪音 / 补画面细节 / 修自相矛盾）。**默认开**——它是增益不是门禁：
    # 任何失败、超时或护栏不过都会 fail-open 回退初稿。关掉即退回改造前行为。
    refine_first_frame: Mapped[bool] = mapped_column(Boolean, default=True)
    # 组内环境锁定（32 号场景组）：开 = 同组场景生成时带「环境母版图」作参考图
    # （让同一真实空间建材/色调一致）；关 = 只用组环境设定文本打底
    scene_group_lock: Mapped[bool] = mapped_column(Boolean, default=True)
    # 【遗留列，勿用】auto_three_view：2026-09-29 的「三视图自动补齐」开关。
    # 2026-09-30 用户定稿「基础形象直接生成三视图」后**已废弃**——生成基础形象
    # 就是出三视图，不再需要开关。列保留（SQLite 删列要重建表，风险不划算），
    # 代码里已无任何读写；新逻辑见 task_engine._after_character_sheet。
    auto_three_view: Mapped[bool] = mapped_column(Boolean, default=True)
    # 国家预设（新建项目时设定，文生图/文生视频据此约束人物外貌）：默认中国
    country: Mapped[str] = mapped_column(String(50), default="中国")
    # 视频画幅比例（新建项目时设定，文生视频按此指定参数）：
    # 16:9 | 4:3 | 1:1 | 3:4 | 9:16 | 21:9 | adaptive（自适应=由首帧/参考图定）
    aspect_ratio: Mapped[str] = mapped_column(String(10), default="16:9")
    # 视频分辨率档（新建项目时设定）：480p(8bit) | 720p(8bit) | 1080p(10bit)
    # 端点按各自能力映射（ark 走 --resolution 后缀，agnes 走 720P/1080P 档）
    video_resolution: Mapped[str] = mapped_column(String(10), default="720p")
    # 项目描述（首页卡片展示，可空）
    description: Mapped[str] = mapped_column(Text, default="")
    # 生成模型预设：{"t2i": "hidream-o1/image", "t2v": "minimax-h3/fl2va-int8"}
    # 空对象/缺键 = 未设置（该类任务用系统默认模型）；改预设仅影响之后的任务
    model_presets: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now, onupdate=_now)
    cover_artifact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Canvas(Base):
    """画布图（React Flow nodes/edges 的 JSON 快照）。"""

    __tablename__ = "canvases"

    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    graph_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[int] = mapped_column(Integer, default=_now, onupdate=_now)


class Character(Base):
    """短剧&漫剧全局角色（对齐设计文档第 10 章）。"""

    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # NULL 表示全局角色（跨项目资产库）
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    name: Mapped[str] = mapped_column(String(100))
    # 角色定位（LLM 按剧本定义）：主角 | 配角
    role: Mapped[str] = mapped_column(String(20), default="配角")
    persona: Mapped[str] = mapped_column(Text, default="")
    appearance: Mapped[str] = mapped_column(Text, default="")
    # 定妆图生图提示词（LLM 基于剧本大背景生成，可编辑）
    image_prompt: Mapped[str] = mapped_column(Text, default="")
    # 音色文本描述（LLM 按人设生成，映射 edge-tts/CosyVoice 音色）
    voice_desc: Mapped[str] = mapped_column(Text, default="")
    # 音色参考音频（用户上传的 Asset.id，后续生成视频时作角色配音音色参考）
    voice_ref_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 按集解析的来源集号（18 号：资产在每集制作时增量解析，此列记录首次解析出它
    # 的那一集；0 = 手工新建或旧数据未记录）——资产页据此标注「第 N 集解析」
    origin_seq: Mapped[int] = mapped_column(Integer, default=0)
    # 出场顺序（该集解析时它在剧本里的先后，1 起；0 = 手工新建或旧数据未记录）。
    # 资产列表按「来源集号 + 出场顺序」排；0 的一律回退原有排序（老项目行为不变）
    order_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class CharacterImage(Base):
    """角色形象（定妆图记录）：任务完成时由 task_engine 按 node_id 追加 node_versions，
    列表接口查最新版本的 artifact 展示。"""

    __tablename__ = "character_images"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    character_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    # basic（基础形象）| costume（妆造）| triple（三视图，历史）| fourview（四视图）
    kind: Mapped[str] = mapped_column(String(20), default="basic")
    appearance_desc: Mapped[str] = mapped_column(Text, default="")
    artifact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 用户「设为生效」的版本（NodeVersion.id；空 = 用最新版本）——同一条形象
    # 记录多次抽卡后可指定某版作为分镜参考图与卡片展示的主图
    preferred_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 适用集数（seq 列表，空 = 未设置；妆造按集映射分镜，6.4 第 2 条）
    episodes_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class SceneGroup(Base):
    """场景组（32 号方案，2026-09-30）：同一个**真实空间**里的一组场景卡。

    同一套房子的客厅/卧室/饭桌、同一家店的店内/门口/后厨、同一段城墙的城头/瓮城
    ——它们拍出来应当是"同一个地方"（建材/色调/门窗/照明一致）。**不是"地点"**：
    地点是房间级语义（不同房间=不同场景卡，规则不变），场景组是真实空间级。

    用途：
    - `env_setting`（组环境设定）：一句组级描述，注入组内每个场景的提示词（文本打底）；
    - `anchor_asset_id`（环境母版图，可空）：用户显式指定的环境基准；**为空时
      生成链路自动取"组内首个已有产物的成员"当母版**（首张自动，无需状态同步）。
    """

    __tablename__ = "scene_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（两层模型）：判权限只看这一列；由 db/session.py 钩子按项目自动填
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 组名（如「林家老宅」）：解析阶段由 LLM 标注，界面可改
    name: Mapped[str] = mapped_column(String(200))
    # 组环境设定：建筑年代/装修档次/主材/墙地色调/门窗样式/照明基调（一句话）
    env_setting: Mapped[str] = mapped_column(Text, default="")
    # 环境母版图（Asset.id；空 = 自动取组内首个有产物的成员）
    anchor_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class Scene(Base):
    """短剧&漫剧全局场景（全剧集共享）。"""

    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # 场景主图生图提示词（LLM 基于剧本世界观生成，可编辑）
    image_prompt: Mapped[str] = mapped_column(Text, default="")
    # 空间布局声明（批次 4 补充 §35.1）：出入口位置/门窗与玻璃面分布/固定
    # 陈设方位——跨镜人物进出与站位调度的「空间法典」，注入首帧与视频提示词
    layout: Mapped[str] = mapped_column(Text, default="")
    artifact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 出现的集数（seq 列表）
    episodes_json: Mapped[list] = mapped_column(JSON, default=list)
    # 出现频数（len(episodes_json)）
    freq: Mapped[int] = mapped_column(Integer, default=0)
    # 按集解析的来源集号（18 号：资产逐集增量解析；0 = 手工新建或旧数据）
    origin_seq: Mapped[int] = mapped_column(Integer, default=0)
    # 出场顺序（该集解析时它在剧本里的先后，1 起；0 = 手工新建或旧数据未记录）
    order_seq: Mapped[int] = mapped_column(Integer, default=0)
    # 用户「设为生效」的版本（NodeVersion.id；空 = 用最新版本）——抽卡多版后
    # 可指定某版作为分镜参考图与展示的主图
    preferred_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 场景组（32 号）：同一真实空间里的成员；空 = 不入组（行为与旧数据一致）
    group_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class Prop(Base):
    """关键叙事道具资产（跨镜头外观一致性的锚点）：一碗面/写字板/轿车/风铃等。

    只提取影响叙事或易漂移的关键道具（codex review：不为所有物件建卡），
    每个道具生成定妆图（白底物件图），分镜通过 prop 引用绑定参考图。
    """

    __tablename__ = "props"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    # 道具定妆图生图提示词（白底物件图，可编辑）
    image_prompt: Mapped[str] = mapped_column(Text, default="")
    artifact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 出现的集数（seq 列表）
    episodes_json: Mapped[list] = mapped_column(JSON, default=list)
    freq: Mapped[int] = mapped_column(Integer, default=0)
    # 按集解析的来源集号（18 号：资产逐集增量解析；0 = 手工新建或旧数据）
    origin_seq: Mapped[int] = mapped_column(Integer, default=0)
    # 出场顺序（该集解析时它在剧本里的先后，1 起；0 = 手工新建或旧数据未记录）
    order_seq: Mapped[int] = mapped_column(Integer, default=0)
    # 用户「设为生效」的版本（NodeVersion.id；空 = 用最新版本）
    preferred_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


# 分集制作状态（18 号 §55）：与 status（合成态 draft|composed）分列，互不混用。
# 流转：planned →（审核）→ needs_fix | audited →（制作）→ building → built；
# 任一阶段异常 → failed（可重跑，不影响其他集）。
PRODUCE_PLANNED = "planned"      # 已分集、未审核
PRODUCE_NEEDS_FIX = "needs_fix"  # 审核出 critical，待用户处理
PRODUCE_AUDITED = "audited"      # 审核通过或用户已修复/忽略，可制作
PRODUCE_BUILDING = "building"    # 拆解与画面描述进行中
PRODUCE_BUILT = "built"          # 分镜已落库
PRODUCE_FAILED = "failed"        # 该集制作失败（仅影响本集）
PRODUCE_STATUSES = (PRODUCE_PLANNED, PRODUCE_NEEDS_FIX, PRODUCE_AUDITED,
                    PRODUCE_BUILDING, PRODUCE_BUILT, PRODUCE_FAILED)

# 分集来源（plan_source）：原文集头切分 / LLM 按剧情切集
PLAN_SOURCE_HEAD = "head"
PLAN_SOURCE_LLM = "llm"


class Episode(Base):
    """分集。

    18 号起该表同时承载「分集计划」与「逐集制作状态」：body 是当集正文的
    制作真源（修复直接改它），char_start/char_end 是在剧本原文中的字符区间
    （修复后回写原文的确定性锚，按集号从后往前替换规避偏移漂移）；
    synopsis/beats/hooks 构成「分集要点表」，是逐集审核注入上下集剧情要点的
    数据源；audit_json 存最近一次该集审核结果。
    """

    __tablename__ = "episodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200), default="")
    # draft（默认）| composed（已合成）
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # 当集正文（制作真源；修复稿写回此处）
    body: Mapped[str] = mapped_column(Text, default="")
    # 在剧本原文中的字符区间 [start, end)：回写原文时按此定位
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    # 一句话梗概（分集要点表）
    synopsis: Mapped[str] = mapped_column(Text, default="")
    # 剧情要点：关键情节点 3-6 条（逐集审核的上下文来源）
    beats_json: Mapped[list] = mapped_column(JSON, default=list)
    # 钩子/爆点清单 [{kind: setup|turn|climax|hook, text, anchor}]
    hooks_json: Mapped[list] = mapped_column(JSON, default=list)
    # 分集来源：head（原文集头切分）| llm（LLM 按剧情切集）
    plan_source: Mapped[str] = mapped_column(String(10), default=PLAN_SOURCE_HEAD)
    # 最近一次该集审核结果 {passed, verdict, issues[], exit_state, at}
    audit_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 制作状态（PRODUCE_* 常量）
    produce_status: Mapped[str] = mapped_column(String(20), default=PRODUCE_PLANNED)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class Shot(Base):
    """分镜：状态由列表接口按 node_versions 动态推导。
    status 列当前存推导值：pending（待首帧）| first_frame（首帧就绪）| done（视频完成）。"""

    __tablename__ = "shots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    episode_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    # 剧本原文摘抄（该分镜对应的源行，1:1 对齐的凭据；台词以外内容已在 description）
    excerpt: Mapped[str] = mapped_column(Text, default="")
    # 导演级视频画面描述（LLM 生成，@角色/@场景 引用素材，生成视频时转换）
    video_prompt: Mapped[str] = mapped_column(Text, default="")
    # 运镜/景别（短文本）
    camera: Mapped[str] = mapped_column(String(100), default="固定")
    duration: Mapped[int] = mapped_column(Integer, default=5)
    # @引用（ID 级）：场景与角色
    scene_ref_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    character_ref_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    # 道具引用（ID 级）：该分镜出现的关键道具（外观一致性锚点）
    prop_ref_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    # 妆造映射：character_id → character_image_id（该分镜用角色的哪个形象，6.6 第 1 条）
    costume_map_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # [{speaker, text, os}]
    dialogue_json: Mapped[list] = mapped_column(JSON, default=list)
    # 转场类型（与上一镜的衔接）：continue（连续动作）| time_skip（同场景时间跳）
    # | scene_cut（切换场景）——拆解 LLM 判定，画面描述据此承接上一镜尾态
    transition_in: Mapped[str] = mapped_column(String(20), default="")
    # 镜头初始/结束状态账本（人物位置朝向、道具状态，跨镜连续性锚点）
    state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 镜头结束状态账本（批次 2c §32.6）：{who: [结束时刻在场角色], position,
    # props, time}——跨镜状态链对账的「出口态」，与 state_json（入口态）成对
    end_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 镜头目的（批次 2b §32.5）：establish | reaction | turn | insert | transition
    # ——稀疏审片的重点核对依据（reaction 占比是「关键反应镜缺失」的观测指标）
    purpose: Mapped[str] = mapped_column(String(20), default="")
    # 已知脆弱点：face | prop | text | motion | crowd（多选）——审片时按
    # 脆弱点定向核对，模型路由建议同步消费
    risks_json: Mapped[list] = mapped_column(JSON, default=list)
    # 多切关键帧（批次 4 §35.2，仅 key_scenes）：[{cut, offset, description,
    # artifact_id}]——段内 2 切每切一张关键帧图（首切复用 first_frame 通道）
    cuts_json: Mapped[list] = mapped_column(JSON, default=list)
    first_frame_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    video_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(20))  # image|video|audio
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class NodeVersion(Base):
    """抽卡记录：节点的每次生成/上传/编辑追加一个版本，selected 版本对外生效。"""

    __tablename__ = "node_versions"
    # 同节点版本号唯一（审计 13 号：max+1 分配无约束，并发落版本会撞号，
    # 「最新/生效版」按 seq desc 取到哪条不确定）——新库由 create_all 建约束，
    # 旧库由 init_db 的 CREATE UNIQUE INDEX 补（重复数据先清理）
    __table_args__ = (
        UniqueConstraint("project_id", "node_id", "seq", name="uq_node_version_seq"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    node_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    # generated（生成）| uploaded（上传）| edited（编辑）
    source: Mapped[str] = mapped_column(String(20), default="generated")
    # 版本命名（用户可改；空 = 前端按「版本N / 基础-版本N」格式展示默认名）
    name: Mapped[str] = mapped_column(String(100), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    refs_json: Mapped[list] = mapped_column(JSON, default=list)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32))
    # 发起人（2026-09-30）：记录是谁点的这一下，用于审计（当前恒为空，见 resource_map
    # 的说明——判权限走 owner_user_id，不依赖它）。
    initiator_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 计费流水关联（33 号方案）：本任务由哪笔消费流水发起（由 core/billing.py 的
    # contextvar 在 submit 时自动带出，业务代码不手填）；任务失败/取消时凭它自动
    # 退款。空 = 无关联（免费端点或内部调用），不做退款判定。
    billing_txn_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                        index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列；由系统自动填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    node_id: Mapped[str] = mapped_column(String(64))
    runner: Mapped[str] = mapped_column(String(50))  # t2v / i2v / t2i / tts / compose ...
    model_id: Mapped[str] = mapped_column(String(100), default="")
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    input_refs_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued|loading_model|running|done|failed|cached|canceled
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    artifact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 任务完成后追加的抽卡版本
    version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 批量任务聚合（整组执行/批量首帧/多集连发）
    batch_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    timings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 解析过程可观测轨迹（持久化）：{stages: [{label, progress, at}], thoughts: [{title, content, at}]}
    trace_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    started_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ModelRecord(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    family: Mapped[str] = mapped_column(String(50))  # minimax-h3 / ltx-2 / wan-2.2 / flux ...
    task_type: Mapped[str] = mapped_column(String(30))  # t2v / i2v / fl2v / ref2v / t2i / edit
    quant: Mapped[str] = mapped_column(String(30), default="")
    revision: Mapped[str] = mapped_column(String(64), default="")
    files_json: Mapped[list] = mapped_column(JSON, default=list)
    vram_mb: Mapped[int] = mapped_column(Integer, default=0)
    installed: Mapped[int] = mapped_column(Integer, default=0)  # 0 未安装 1 已安装
    status: Mapped[str] = mapped_column(String(30), default="not_installed")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Session(Base):
    """Agent 会话（多会话并行，对齐设计文档 5.1/10 章）。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    # active（进行中）| closed（已关闭）
    status: Mapped[str] = mapped_column(String(20), default="active")
    # left（对话面板靠左）| right（靠右）
    pane_side: Mapped[str] = mapped_column(String(20), default="right")
    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now, onupdate=_now)


class Message(Base):
    """会话消息（含 @refs 引用与工具调用记录）。"""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    # user | assistant
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    # [{node_id, version_id?}] @引用（ID 级，见设计文档 4.6）
    refs_json: Mapped[list] = mapped_column(JSON, default=list)
    # [{tool, args, result}] 工具调用记录（见设计文档 5.4）
    tool_calls_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32))
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    role: Mapped[str] = mapped_column(String(20))  # user|assistant|tool
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class ShotReview(Base):
    """镜头裁决记录（批次 3 §34.1）：五级裁决 + 单变量记录 + 镜头日志。

    一条记录 = 一次裁决/重拍动作（take_seq 按镜内递增）——裁决是请求驱动
    的动作，失败的任务不产生记录（不占重试预算，codex 评审口径）；
    observed_json 在 keep 裁决时回写观测态（实际时长/首尾帧 artifact）——
    已接受的观测状态高于计划状态。
    """

    __tablename__ = "shot_reviews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    episode_id: Mapped[str] = mapped_column(String(32), index=True)
    shot_id: Mapped[str] = mapped_column(String(32), index=True)
    take_seq: Mapped[int] = mapped_column(Integer, default=1)
    # keep | post | edit | reroll | rewrite（报告 10.1 五级裁决）
    verdict: Mapped[str] = mapped_column(String(20), default="pending")
    # 单变量记录：seed | prompt | refs | model | params | both（§34.2 单变量锁）
    changed: Mapped[str] = mapped_column(String(10), default="seed")
    # 重摇原因（25 号 2.4）：用户为什么重出这一镜——姿态/人物不一致/构图/文字/
    # 声音/画质/其他（取值见 drama.REROLL_REASONS）。**没有这一列，优化就没有
    # 靶子**：历史 188 条记录的 evidence 全空，等于只知道"又摇了一次"。
    # 基建类失败（infra:*) 是系统自动重试，不填原因（"用户没不满意"）。
    # 2026-10-10 起**多选**：逗号分隔的取值串（如 "identity,pose"），逐项在
    # REROLL_REASONS 里；单值即长度为 1 的串，老数据原样兼容。
    reason: Mapped[str] = mapped_column(String(64), default="")
    # 重出靶子（2026-10-10）：ff=首帧 / video=视频 / all=全部生成；""=本字段
    # 上线前的历史记录（归属不可辨）。**盲摇预算必须按靶子分账**——旧口径把
    # 首帧重摇算进视频预算，实测把「首帧摇过两次」的镜的视频重出整个锁死
    # （且那两次还叠加了种子钳死 bug、是无效重摇，双重伤害）。
    target: Mapped[str] = mapped_column(String(10), default="")
    # 记录来源（25 号复查）：submit=提交动作 / verdict=人工裁决意向 / auto=AI
    # 判级建议 / 空串=本列上线前的历史行。统计口径必须区分三者，否则
    # 「原因填写率」「盲摇次数」都会被模型判级与意向记录污染
    origin: Mapped[str] = mapped_column(String(10), default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    reviewer: Mapped[str] = mapped_column(String(10), default="user")
    observed_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class CharacterAssetLink(Base):
    """角色资产的作用域绑定（借鉴 Jellyfish 的 scoped link，见 services/scoped_link.py）。

    一条记录表达「哪个形象在什么范围内生效」——三级作用域【就近覆盖】：

    - `scope=project`：整剧默认形象（本项目此前的"基础形象"语义）；
    - `scope=episode`：整集生效（如第 3-5 集晚宴装——一条记录顶掉逐镜打标）；
    - `scope=shot`：单镜覆盖（最高优先级）。

    解析时按 shot > episode > project 取第一条命中，取代历史上"逐镜 blob
    映射 + 形象自带 episodes_json 列表"的双账本（跨镜漂移与漏标的根源）。
    """

    __tablename__ = "character_asset_links"
    __table_args__ = (
        UniqueConstraint("project_id", "character_id", "scope", "episode_id",
                         "shot_id", name="uq_char_asset_link_scope"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    character_id: Mapped[str] = mapped_column(String(32), index=True)
    # 绑定的形象记录 ID（CharacterImage.id）——解析后即为该角色的生效参考图
    image_id: Mapped[str] = mapped_column(String(32))
    # project | episode | shot（作用域越具体优先级越高）
    scope: Mapped[str] = mapped_column(String(16), default="project")
    episode_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class ScriptVersion(Base):
    """剧本解析历史版本：重复解析保留的整包快照。

    快照内容 = 剧本原文 + 解析元信息 + 全部产物（角色/场景/分集/分镜），
    可整体恢复到操作面板（恢复即重放 data_json 到主表）。
    """

    __tablename__ = "script_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列。
    # **由系统自动填**（db/session.py 的 before_flush 钩子），业务代码不手填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 项目内版本序号（从 1 递增）
    seq: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class ProviderKey(Base):
    """第三方服务 API Key（key 管理，可被多个模型服务端点复用）。

    本地单机应用，api_key 明文存储（与 .env 兼容层的现状一致）；列表接口脱敏展示。
    """

    __tablename__ = "provider_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100))
    api_key: Mapped[str] = mapped_column(Text, default="")
    remark: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now, onupdate=_now)


class ProviderEndpoint(Base):
    """模型服务端点（模型管理录入项，替代旧 .env 五槽位固定配置）。

    一条端点 = 名称 + 模型类型（kind）+ 调用协议（provider）+ 服务地址 +
    关联 Key + 默认模型。运行时按「任务级编码串 > 每类默认端点 > .env 回退」解析。
    """

    __tablename__ = "provider_endpoints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    # 模型类型枚举（与 PROVIDER_KINDS 对齐）：llm|t2i|t2v|i2v|tts
    kind: Mapped[str] = mapped_column(String(20), index=True)
    # 调用协议：openai|ark|agnes|custom（不含 local，本地推理不走端点）
    provider: Mapped[str] = mapped_column(String(20))
    base_url: Mapped[str] = mapped_column(Text, default="")
    # 关联 ProviderKey.id（不建 FK 约束，对齐全库惯例；解绑后运行时报配置缺失）
    key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 端点默认模型（可空 = 用协议精选默认）
    model: Mapped[str] = mapped_column(String(200), default="")
    # 是否支持多模态（批次 3.5）：勾选 = llm 模型可接收图像消息（视觉审片
    # 判级的准入开关；仅 llm 类端点有意义）
    multimodal: Mapped[bool] = mapped_column(Boolean, default=False)
    # ---------- 计费费率（33 号方案，2026-10-09 用户口径：按模型服务端点配置） ----------
    # 积分计费的配置对象就是**模型服务端点**（本表）：每个服务配自己的单价，
    # 按实际发生的每次调用（调用日志逐行）计费。三档单价哪类有消耗算哪类，
    # 0 = 该类不计费；三类全 0 或 bill_enabled=False = 该服务免费。
    credits_per_1k_tokens: Mapped[int] = mapped_column(Integer, default=0)
    credits_per_image: Mapped[int] = mapped_column(Integer, default=0)
    credits_per_video_second: Mapped[int] = mapped_column(Integer, default=0)
    # 计费开关（关 = 该服务的全部调用不计费）
    bill_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now, onupdate=_now)


class User(Base):
    """用户账号（内置管理员种子创建，其余经账号管理页维护）。

    password_hash 为 PBKDF2 散列串（见 app.core.security），不存明文。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # 禁用标记：禁用后不可登录（已有令牌一并失效）
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    last_login_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AuthToken(Base):
    """登录令牌（Bearer Token → 用户映射，持久化以容忍后端重启）。

    token 为随机 256bit 十六进制串（明文即凭据，不落散列——本地应用 threat
    model 下可接受，换取零额外查询开销）。
    """

    __tablename__ = "auth_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    expires_at: Mapped[int] = mapped_column(Integer)


class ModelCall(Base):
    """**模型调用日志**（25 号 2.3/2.6 的底座）：每一次对外模型调用一行。

    为什么单独建表而不搭在 `tasks` 上：**LLM 调用不是任务**——一个解析任务内部会
    发生几十次 LLM 调用，搭在 task 上就丢掉了"每次调用"的粒度；而「消费信息」
    （token/张数/秒数）本来就属于**调用**而不是任务。

    写入点在 `providers._post_json` / `_poll_get_json` 两处（所有对外调用都经过
    它们，是天然收口）：`what` 是调用点的中文标签（「LLM」「Agnes 文生图」
    「方舟文生视频提交」…），`host`/`model` 由 URL 与**请求 payload** 确定性取出
    ——不做关键词猜测。**写日志失败绝不影响调用本身**（fail-open）。

    用途：抽卡率/成片率看板 = 对这张表聚合（按 kind/模型/端点/项目/日期），
    不必再去 join `trace_json`。
    """

    __tablename__ = "model_calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                    default=_uuid)
    created_at: Mapped[int] = mapped_column(Integer, default=_now, index=True)
    # 调用点标签（中文）+ 由 URL/payload 确定性取出的端点与模型
    what: Mapped[str] = mapped_column(String(64), default="")
    host: Mapped[str] = mapped_column(String(128), default="", index=True)
    model: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 结果侧
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fail_class: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    # **消费信息**（确定性从响应/请求取，取不到留空，不猜）
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 归属（可空：LLM 调用常发生在任务之外）
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 发起这次调用的 HTTP 请求编号（2026-10-07 按用量计费）：BillingGate 扣费时
    # 经 ContextVar 带出（同 billing_txn 机制），同步端点结算时按它汇总用量
    request_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    # 本行调用的积分数（33 号方案 2026-10-09）：记录时按**所用模型服务端点的
    # 费率 × 本次用量**算好存下（ok=False 或未匹配到计费端点 = 0）。
    # 结算 = 按范围 SUM 这一列——比"结算时再算"更权威（扣费时点费率快照）
    # 也更简单（一条 SQL）。计费的"调用情况"就是这张表的行。
    credits: Mapped[int] = mapped_column(Integer, default=0)
    # 权限归属（2026-09-30 两层模型）：判权限只看这一列；由系统自动填。
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 调用留痕（2026-09-30，调用日志页「查看详情」）：完整 URL + 请求/响应正文。
    # 单列而不是塞进 usage_json 的理由：usage 是**确定性的消费口径**（看板聚合
    # 用），留痕是**排查材料**，两者生命周期与体积差一个量级。写入前统一打码
    # 敏感字段并截断超长内容（见 providers._clip_trace）。
    url: Mapped[str] = mapped_column(String(500), default="")
    request_json: Mapped[str] = mapped_column(Text, default="")
    response_json: Mapped[str] = mapped_column(Text, default="")

# ---------- 积分计费体系（33 号方案，2026-10-07 用户拍板）----------
#
# 白话总览：用户充值得到积分（credit_accounts 的 balance），调用收费端点时
# 先原子扣分再放行（BillingGate 中间件，见 core/billing.py），业务失败自动退分；
# 每一次余额变动都写一行流水（credit_transactions）——它是唯一账本，随时可以
# 用「流水累计 = 余额」做对账。价目（billing_rules）与兑换码（redeem_codes）
# 是配套的管理工具。

# 流水类型（CreditTransaction.kind）
TXN_RECHARGE = "recharge"  # 充值（管理员操作或兑换码核销）
TXN_CONSUME = "consume"    # 消费（一次端调用扣的分）
TXN_REFUND = "refund"      # 退款（请求失败 / 任务失败 / 缓存命中零成本）
TXN_ADJUST = "adjust"      # 管理员调整（纠错用，可正可负）
TXN_BONUS = "bonus"        # 初始赠送（新用户建户时发放）

# 流水状态（仅消费/退款类使用；充值/调整/赠送类留空）
TXN_PENDING = "pending"    # 已扣、请求处理中（未结算）
TXN_CONSUMED = "consumed"  # 请求成功，计费成立
TXN_REFUNDED = "refunded"  # 已退款（终态，不会再次退）


class CreditAccount(Base):
    """积分账户（一人一户，33 号方案）。

    **余额永远 ≥ 0**：由 core/billing.py 的原子扣减
    （`UPDATE ... WHERE balance >= :c`）保证，任何代码路径都不允许直接改名减余额。
    账户在首次发生积分往来时幂等创建（不预建，避免与用户 CRUD 耦合）。
    """

    __tablename__ = "credit_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # 账户所有者（唯一，一人一户）
    user_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # 当前余额（整数积分；永不为负）
    balance: Mapped[int] = mapped_column(Integer, default=0)
    # 累计充值 / 累计消费（展示用统计，由记账函数同步维护）
    total_recharged: Mapped[int] = mapped_column(Integer, default=0)
    total_consumed: Mapped[int] = mapped_column(Integer, default=0)
    # 权限归属（两层模型）：用户级表，钩子按「owner = user_id」特判自动填
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now, onupdate=_now)


class CreditTransaction(Base):
    """积分流水（33 号方案：唯一账本，**只增不改金额**）。

    每一行 = 一次余额变动：充值（+）/ 消费（-）/ 退款（+）/ 调整（±）/ 赠送（+）。
    `amount` 是变动量（正入负出），`balance_after` 是变动后的余额快照——列表展示
    与对账（`balance == SUM(amount)`）都靠这两列。

    消费/退款类带状态机：pending（已扣、请求处理中）→ consumed（计费成立）或
    refunded（已退款）；退款动作统一走一条 CAS（pending/consumed → refunded），
    幂等防双退。`ref_txn_id` 指向被退的那笔原消费（唯一约束 = 双退的第二道保险）。
    """

    __tablename__ = "credit_transactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    # TXN_* 常量之一
    kind: Mapped[str] = mapped_column(String(20))
    # 变动量：正 = 入账，负 = 出账
    amount: Mapped[int] = mapped_column(Integer)
    # 变动后余额快照（对账与「这笔交易时余额多少」的审计凭据）
    balance_after: Mapped[int] = mapped_column(Integer, default=0)
    # TXN_PENDING / TXN_CONSUMED / TXN_REFUNDED；充值/调整/赠送类留空
    status: Mapped[str] = mapped_column(String(20), default="")
    # 计费端点键（如 "POST /api/tasks"）；消费/退款行填
    endpoint: Mapped[str] = mapped_column(String(200), default="")
    # 扣费时点的单价快照——价目表以后改价不影响历史审计与退款金额
    price: Mapped[int] = mapped_column(Integer, default=0)
    # 按用量结算所需的价格快照（JSON：mode/单价三类/预扣；2026-10-07 按用量计费）。
    # 结算用**扣费时点**的价目，不用结算时点的（管理员中途改价不影响在途单）。
    rule_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 按用量结算时间戳（0 = 未结算）。结算动作的幂等凭据（CAS：0 → 时间戳）。
    settled_at: Mapped[int] = mapped_column(Integer, default=0)
    # 业务关联（能解析到就记，可空）
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                   index=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 发起这次变动的 HTTP 请求编号（uui4 hex，排查用）
    request_id: Mapped[str] = mapped_column(String(36), default="")
    # 退款行指向的原消费流水 id（唯一约束防同一笔被退两次；非退款行为空）
    ref_txn_id: Mapped[str | None] = mapped_column(String(32), unique=True,
                                                   nullable=True)
    # 人类可读说明（如「管理员充值」「任务失败自动退款」「命中缓存零成本退款」）
    note: Mapped[str] = mapped_column(String(200), default="")
    # 管理员操作（充值/调整）的执行人
    operator_user_id: Mapped[str] = mapped_column(String(32), default="")
    # 权限归属（两层模型）：用户级表，钩子按「owner = user_id」特判自动填
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    created_at: Mapped[int] = mapped_column(Integer, default=_now, index=True)


class RedeemCode(Base):
    """兑换码（33 号方案）：管理员批量生成，用户在自己页面输入兑换充值。

    核销必须是原子操作（防并发同码双花）：
    `UPDATE redeem_codes SET used_by_user_id=..., used_at=...
     WHERE code=:c AND used_by_user_id IS NULL` + rowcount 判定，
    成功才继续充值记账（同一事务）。code 为高熵随机串（防在线枚举），
    兑换接口另有失败限速（见 core/billing.py）。
    """

    __tablename__ = "redeem_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # 兑换码本体（高熵随机串，唯一；大小写不敏感由核销时归一处理）
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    # 面额（兑换后充入的积分数）
    credits: Mapped[int] = mapped_column(Integer, default=0)
    # 批次备注（生成时填写，便于对账「这批码发给谁了」）
    note: Mapped[str] = mapped_column(String(200), default="")
    # 使用者（NULL = 未使用）
    used_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                                        index=True)
    used_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 生成者（管理员）
    created_by: Mapped[str] = mapped_column(String(32), default="")
    # 权限归属（两层模型）：留空 = 谁都不属于 = 仅管理员可见（正确语义）
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    created_at: Mapped[int] = mapped_column(Integer, default=_now)


class BillingRule(Base):
    """【遗留表，勿用】端点价目（33 号方案 v1）。

    **2026-10-09 口径修正后不再用于计费**：计费配置的正式载体是
    `provider_endpoints`（模型服务费率）与 `model_calls.credits`（行级计价）；
    「哪些 HTTP 接口需要发起前预扣」改为 `core/billing.py` 的
    `GATED_ENDPOINTS` 代码清单（预扣额内置默认，无需配置）。本表保留在库
    只为兼容旧数据，代码不再读写；新库由 create_all 建空表（无害）。

    原释义（存档）：每个端点的计价方式与单价。

    **两种计价方式**（mode 列）：
    - `per_usage`（按用量，2026-10-07 用户口径）：按外部调用的**实际消耗**计费
      ——大模型按 token（每千 token 单价）、生图按张数、生视频按秒数，可同时
      配置多类单价（哪类有消耗算哪类）；调用前先按 `precharge` 预扣占位，
      完成后按实际用量**多退少补**（见 core/billing.settle_usage）。
    - `per_call`（按次）：每次调用固定 `credits` 分——留给「批量生成」这类
      一次调用里干很多活、或形态混合（首帧图+视频）不好按单一量纲计的端点。

    免费判定：enabled=False 免费；per_call 时 credits=0 免费；per_usage 时
    三类单价全 0 免费（**且要求 precharge>0 才生效**——保证「调用前验证」
    恒成立）。**默认全部免费**（先上线后开闸）；管理页「一键应用建议价目」
    按 SUGGESTED_PRICES 写入。

    启动时与真实路由清单同步：只补缺行（INSERT OR IGNORE，不覆盖已有配置）、
    只同步变更型端点（POST/PUT/PATCH）——与「归属自动识别」同一思路：
    宁可漏收不可错收，管理员显式定价才收费。

    系统配置表（同 provider_endpoints 惯例），不加 owner_user_id。
    """

    __tablename__ = "billing_rules"
    __table_args__ = (
        UniqueConstraint("method", "path_template",
                         name="uq_billing_rule_endpoint"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    method: Mapped[str] = mapped_column(String(10))
    # 路由模板（与 FastAPI 的 path 模板逐字一致，如 /api/tasks）
    path_template: Mapped[str] = mapped_column(String(200))
    # 计价方式：per_usage（按实际用量，默认）| per_call（每次固定价）
    mode: Mapped[str] = mapped_column(String(16), default="per_call")
    # 每次调用消耗的积分数（per_call 用；0 = 免费）
    credits: Mapped[int] = mapped_column(Integer, default=0)
    # 按用量的三类单价（per_usage 用；0 = 该类消耗不计费，可混合配置）：
    # 每 1000 tokens / 每张图 / 每秒视频 的积分数
    credits_per_1k_tokens: Mapped[int] = mapped_column(Integer, default=0)
    credits_per_image: Mapped[int] = mapped_column(Integer, default=0)
    credits_per_video_second: Mapped[int] = mapped_column(Integer, default=0)
    # 预扣积分（per_usage 的占位押金：发起前先扣这笔、完成后按实际用量多退少补；
    # 必须 > 0 该端点才生效——保证「调用前验证余额」恒成立）
    precharge: Mapped[int] = mapped_column(Integer, default=0)
    # 计费开关（临时关闭某端点收费；关 = 免费）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 中文名（后台展示；建议价目应用时一并写入）
    label: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[int] = mapped_column(Integer, default=_now, onupdate=_now)
