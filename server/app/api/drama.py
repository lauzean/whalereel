"""短剧&漫剧 Agent API（M3 卡片流主视图，对齐设计文档第 6/9/10 章）。

- 剧本解析落库（仅 LLM 智能拆解，失败直接报错；重复解析 = 清空旧数据重建）
- 全局角色/场景管理，分集与分镜故事板
- 批量首帧（t2i）/ 批量视频（ref2v，H3 Ref2VA 路径）/ 单集合成（compose）

任务一律走 task_engine.submit（自动追加 node_versions + WS 推送），不直接写产物。
node_id 约定：char_{cid}_{imgId} / scene_{sid} / shot_{sid}_ff / shot_{sid}_v / episode_{eid}。
"""

import hashlib
import json
import logging
import re
import time
import uuid
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from app.api.auth import require_admin
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

import app.agent.prompts as prompts_mod
from app.agent.prompts_loader import list_prompts, load_prompt, reload_prompts, save_prompt
from app.api.frames import (
    extract_frame_version, has_frame_version_for, latest_video_artifact_id,
)
from app.core.config import artifact_dir_for, settings
from app.core.model_manager import KIND_LLM, KIND_T2I, KIND_T2V, project_preset
from app.core.providers import video_ref_budget
from app.core.task_engine import engine
from app.core import access
from app.core.access import project_access_guard
from app.core.upload import read_upload_capped
from app.db.models import (
    PRODUCE_AUDITED, PRODUCE_BUILDING, PRODUCE_BUILT,
    Asset, Character, CharacterAssetLink, CharacterImage, Episode,
    NodeVersion, Project, Prop, Scene, SceneGroup, ScriptVersion, Setting, Shot,
    ShotReview, Task,
)
from app.db.session import SessionLocal, get_db
from app.core.prompt_metrics import (
    DIALOGUE_RATE_MAX,
    PICTURE_SEGMENT_NAME,
    SEGMENT_REQUIREMENT,
    clip_at_sentence,
    dialogue_rate_by_beat,
    normalize_picture_head_newline,
    rebalance_beat_marks,
    rescale_beat_marks,
    shot_dialogue_rate,
)
from app.core.providers import (
    ProviderError, llm_available, llm_chat, use_llm_model,
)
from app.script.contracts import AutoReviewOut, contract_violations
from app.script.expander import expand
from app.script.first_frame_refine import REFINE_PARAM_KEY, build_refine_context
from app.script.prompt_optimize import MENTION_RE, optimize_prompt
from app.script.prompt_blocks import (
    GROUP_ENV_LABEL, GROUP_ENV_REF_NOTE, strip_character_system_blocks,
    strip_group_blocks, strip_prop_system_blocks, strip_scene_system_blocks,
)
from app.script.requirements import (  # noqa: F401
    AUDIENCE_LABELS, EPISODE_MINUTE_OPTIONS, TONE_LABELS,
    load_requirements, save_requirements,
)
from app.script.extract import DocExtractError, extract_text as extract_doc_text
from app.script.llm_parser import (
    QA_PLAIN, SHOT_DURATION_MAX, SHOT_DURATION_MIN, ScriptParseError,
    regenerate_shot_prompt,
    scale_of, scale_rhythm_flags, timing_error,
    vp_dialogue_order_error,
    _as_str, _declares_empty, _extract_json, _BEAT_SPAN_RE, _VP_VIDEO_SECTION_RE,
)
from app.services import readiness
from app.services.readiness import (
    shot_info_status, shot_video_readiness,
)
from app.script.persist import (
    build_lessons_suggestions,
    costume_suggestions_key, ep_assets_key, mark_costume_handled,
    get_script_cur_seq, load_global_lessons,
    anchor_prompt_for, load_script_trace, load_style_presets, merge_lessons,
    project_style_context, prompt_snapshot_key,
    save_script_text, script_meta_key, script_text_key,
    shot_skill_key, skill_text_key, storyboard_qa_key, t2v_skill_key,
)

router = APIRouter(prefix="/api", tags=["drama"], dependencies=[Depends(project_access_guard)])

# 视频生成 runner（H3 Ref2VA 路径）
RUNNER_REF2V = "ref2v"

# 剧本解析 runner（长任务：审读 + 分集规划 + 资产分析 + 落库，进度经 WS 推送）
RUNNER_PARSE_SCRIPT = "parse_script"

# 重新分集 runner（18 号 §51：LLM 按剧情/爆点/钩子重切分集边界）
RUNNER_REPLAN_EPISODES = "replan_episodes"
NODE_ID_REPLAN = "script_replan"

# 逐集流水线 runner 与 node_id 前缀（18 号 §52/§53/§54）：node_id 形如
# `ep_audit_{episode_id}`——前端按前缀把进度/轨迹归到当集（治「10 集进度
# 交错在一起看不懂」）
RUNNER_EPISODE_AUDIT = "audit_episode"
RUNNER_EPISODE_OPTIMIZE = "optimize_episode"
RUNNER_EPISODE_ASSETS = "extract_episode_assets"
RUNNER_EPISODE_BUILD = "build_episode"
NODE_ID_EP_AUDIT = "ep_audit"
NODE_ID_EP_OPTIMIZE = "ep_optimize"
NODE_ID_EP_ASSETS = "ep_assets"
NODE_ID_EP_BUILD = "ep_build"


def _episode_node_id(prefix: str, episode_id: str) -> str:
    """逐集任务 node_id：`{阶段}_{集 id}`（前端按前缀归属到当集）。"""
    return f"{prefix}_{episode_id}"

# 视为“生成中”的任务状态
_ACTIVE_STATUSES = ("queued", "loading_model", "running")

logger = logging.getLogger(__name__)

# 角色形象类型合法值：**只剩妆造**（2026-09-30 用户定稿：基础图=角色设定图，
# 由 /characters/{id}/images/fourview 出；历史 basic/triple 记录已迁移成
# fourview，界面页签也只有「角色设定图 / 妆造」——旧类型不再可创建，免得两套口径并存）
CHARACTER_IMAGE_KINDS = ("costume",)

# 角色定妆图画幅：基础/妆造竖版（正面直立全身照，横画幅易把站立人物生成
# 横躺）；三视图一行四列并排，用横版大图
# 角色定妆图画幅：2:3（比 3:4 更竖）——实测 3:4 画全身像纵向空间不足，
# 瘦削人像约半数被模型截成半身（"全身照"指令被构图挤压忽略），2:3 给
# 头顶到鞋底留足空间（各 provider 档位表已同步补 2:3 档）
# 视频分辨率档 → 目标高度（宽由画幅比例推导）。档位文案与位深随产品定义：
# 480p=8bit / 720p=8bit / 1080p=10bit；默认 720p（端点通用档，成本可控）。
from app.core.contracts import (  # noqa: E402,F401
    ASPECT_ADAPTIVE, ASPECT_WH, DEFAULT_RESOLUTION, RESOLUTION_SHORT_EDGE,
)

# 档位表与默认值统一取自契约层（单一来源）——此前本文件与 core/contracts 各持
# 一份同样的表，正属审计反复抓到的「同一语义两处定义」漂移形态。此处保留别名
# 以兼容既有引用（projects API 与校验逻辑都用这个名字）。
VIDEO_RESOLUTION_HEIGHTS = RESOLUTION_SHORT_EDGE
DEFAULT_VIDEO_RESOLUTION = DEFAULT_RESOLUTION
# 长边上限护栏（端点普遍限制长边 ≤1920，如 ark 1080p 档）
VIDEO_LONG_EDGE_MAX = 1920


def _video_size(aspect: str, resolution: str) -> tuple[int, int]:
    """画幅 + 分辨率档 → 目标宽高（供端点按自身能力映射）。

    **档位语义 = 短边**（480p/720p/1080p 指短边像素，业界通行口径）：横屏短边
    是高、竖屏短边是宽——若按「高度=档位」算，竖屏 720p 会得出 720×1280，
    端点的按高度分档逻辑会误判为 1080p 档。长边按画幅比例等比推导并取偶。

    adaptive：不强制比例，给方形基准（端点按首帧/参考图自适应）。
    """
    tier = VIDEO_RESOLUTION_HEIGHTS.get(resolution, 720)
    if aspect == ASPECT_ADAPTIVE or aspect not in ASPECT_WH:
        return tier, tier
    base_w, base_h = ASPECT_WH[aspect]
    if base_w >= base_h:          # 横屏/方形：短边是高
        h = tier
        w = max(2, int(round(base_w * tier / base_h / 2)) * 2)
    else:                          # 竖屏：短边是宽
        w = tier
        h = max(2, int(round(base_h * tier / base_w / 2)) * 2)
    # 长边上限护栏：端点普遍限制长边 ≤1920（如 ark 1080p 档），超宽画幅
    # （21:9 @1080p → 2520 宽）按短边推导会越界——超护栏时按长边反推两轴
    if max(w, h) > VIDEO_LONG_EDGE_MAX:
        scale = VIDEO_LONG_EDGE_MAX / max(w, h)
        w = max(2, int(round(w * scale / 2)) * 2)
        h = max(2, int(round(h * scale / 2)) * 2)
    return w, h


CHARACTER_IMAGE_ASPECT = "2:3"
CHARACTER_FOURVIEW_ASPECT = "16:9"

# 首帧 t2i 参考图预算（与 runner 层 _T2I_MAX_REFS 对齐）：占号顺序已按
# 「角色→场景→道具」优先（_resolve_video_prompt 两遍法），超预算时先裁
# 尾部的道具/场景——角色参考图必在预算内，杜绝「提示词悬空标注 → 串脸」
def _t2i_ref_budget(db: Session, project_id: str) -> int:
    """本项目 t2i 端点的参考图预算（编号分配与上送共用同一上限）。"""
    from app.core.providers import t2i_ref_budget

    return t2i_ref_budget(_resolve_model(db, project_id, KIND_T2I, ""))

# 角色音色参考音频：允许扩展名与大小上限（短音频参考，远小于该值）
VOICE_REF_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
VOICE_REF_MAX_BYTES = 20 * 1024 * 1024

# 角色资产图上传：仅收图片扩展名，大小上限（对齐音色参考的 20MB）
IMAGE_UPLOAD_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
IMAGE_UPLOAD_MAX_BYTES = 20 * 1024 * 1024

# 展示用 MB（错误提示换算）
_BYTES_PER_MB = 1024 * 1024


# ---------- 请求体 ----------
class ScriptParseIn(BaseModel):
    text: str
    # 重复解析时保留当前结果为历史版本（前端确认后传 true）
    keep_version: bool = False
    # AI 解析前置需求（21 号批次 5，可空 = 沿用上次/默认值）：剧作基调、单集时长、
    # 题材、受众、风格锚点、禁用内容、补充说明。归一化后落 Setting 并注入审读与逐集阶段
    requirements: dict | None = None


class EpisodePatchIn(BaseModel):
    """分集计划编辑（18 号 §51.5）：改集名/梗概/当集正文，均为确定性写入。

    正文改动只影响该集（制作真源在 Episode.body），不触发重新分集；
    需要重切边界时走 replan 端点。
    """

    title: str | None = None
    synopsis: str | None = None
    body: str | None = None


class EpisodeReplanIn(BaseModel):
    """重新分集（LLM 按剧情/爆点/钩子重切）：两段式——confirm=false 先预检
    （将重建几集、丢弃几个分镜），confirm=true 才提交任务。"""

    confirm: bool = False


class CharacterIn(BaseModel):
    name: str
    role: str = "配角"  # 主角|配角
    persona: str = ""
    appearance: str = ""
    voice_desc: str = ""
    image_prompt: str = ""


class CharacterPatchIn(BaseModel):
    name: str | None = None
    role: str | None = None  # 主角|配角
    persona: str | None = None
    appearance: str | None = None
    voice_desc: str | None = None
    image_prompt: str | None = None


class CharacterImageIn(BaseModel):
    kind: str = "costume"  # 只剩妆造；基础图走 /images/fourview（角色设定图）
    name: str = ""
    appearance_desc: str = ""
    # 生图模型（空 = t2i 默认模型）
    model_id: str = ""
    # 适用集数（seq 列表，空 = 未设置）
    episodes: list[int] | None = None
    # 抽卡目标形象记录（非空 = 不新建记录，在该形象下追加一个抽卡版本，
    # 与场景卡同款 NodeVersion 版本流）
    regenerate_image_id: str = ""


class ApplyImageIn(BaseModel):
    """将画布图片产物应用为角色形象/场景主图（设计文档 4.9 P0）。"""
    artifact_id: str


class CostumeApplyIn(BaseModel):
    """妆造→分镜批量映射入参（6.4 第 2 条）。"""
    image_id: str
    # 目标分集（Episode.id）；空 = 全部引用该角色的分集
    episode_ids: list[str] | None = None


class ScenePatchIn(BaseModel):
    name: str | None = None
    description: str | None = None
    image_prompt: str | None = None
    # 场景组（32 号）：""=移出组；非空=按名并入或新建（同名并入，确定性）
    group_name: str | None = None


class SceneIn(BaseModel):
    """手工新建场景入参（名称必填，描述/生图提示词可选）。"""
    name: str
    description: str = ""
    image_prompt: str = ""


class SceneGenerateIn(BaseModel):
    """场景主图生成入参（count = 一次抽几张，1-4，见 T2I_MAX_CANDIDATES）。"""
    model_id: str = ""
    count: int = 1


class ShotIn(BaseModel):
    description: str
    scene_ref_id: str | None = None
    duration: int = 5


class ShotPatchIn(BaseModel):
    description: str | None = None
    # 导演级视频画面描述（@角色/@场景 引用素材，生成视频时转换为图N 格式）
    video_prompt: str | None = None
    duration: int | None = None
    scene_ref_id: str | None = None
    character_ref_ids: list[str] | None = None
    camera: str | None = None
    # 与上一镜的衔接类型（continue/time_skip/scene_cut）——体检报告
    # 「衔接方式与剧情对不上」的一键修正通道
    transition_in: str | None = None
    # 妆造映射：character_id → character_image_id
    costume_map: dict[str, str] | None = None


class ShotsGenerateIn(BaseModel):
    mode: str = "first_frames"  # first_frames|videos|all
    # 视频生成模型（本地家族或方舟模型 id；空 = 默认模型）
    model_id: str = ""
    # 逃生口：跳过资产生成门禁（默认拦截缺基础形象的出场角色——人物一致性
    # 依赖参考图，裸提示词生成是角色漂移的根源）
    ignore_missing_assets: bool = False
    # 定向生成子集（审片重拍：只重生成指定分镜；空 = 全部分镜）
    shot_ids: list[str] | None = None
    # 单变量记录（批次 3 §34.2 单变量锁）：seed|prompt|refs|model|params|both
    # ——重拍时改了什么；同时改多项记 both（前端警告后强提）
    changed: str = "seed"
    # 重拍备注（一句话依据，进镜头日志 evidence）
    note: str = ""
    # 重摇原因（25 号 2.4）：**含重出时必填**（该镜已有产出还要再抽一次）。
    # 取值见 REROLL_REASONS；这是数据飞轮的靶子——没有它，「重摇 70% 只换种子」
    # 这类结论永远只能靠猜
    reason: str = ""
    # 重试预算逃生口（§34.2）：同缺陷第二次盲摇被阻止后强提
    force: bool = False
    # 逃生口：分镜体检 error 残留拦截的豁免（批次 3 §33.2）
    ignore_storyboard_qa: bool = False
    # 首帧一次抽几张（1-4；2026-09-29 用户要求）：首帧比视频便宜得多，而「背对
    # 镜头」「两人同框」这类构图**模型本身不稳定**（实测同一提示词两次抽卡一正一
    # 反）——多抽几张、在卡片上挑一张，比反复改措辞务实。
    count: int = 1


# 角色定位合法值（LLM 定义 + 手动编辑共用）
CHARACTER_ROLES = ("主角", "配角")

# ---------- 重摇原因（25 号 2.4：抽卡数据的靶子） ----------
# 用户重出这一镜，到底是不满意什么——**必填**才谈得上「质量类重摇的原因分布
# 可查」（历史 188 条 ShotReview 的 evidence 全空，优化只能靠猜）。取值与前端
# `apps/web/src/drama/rollReasons.ts` 的常量表逐字一致（改动必须两侧同步）。
REROLL_REASON_POSE = "pose"                # 姿态/动作不对（站位、朝向、调度）
REROLL_REASON_IDENTITY = "identity"        # 人物不一致（串脸/双胞胎/换人）
REROLL_REASON_COMPOSITION = "composition"  # 构图/景别/运镜不对
REROLL_REASON_TEXT = "text"                # 画面文字穿帮（招牌/字幕/乱码）
REROLL_REASON_AUDIO = "audio"              # 声音问题（口型/语速/音效）
REROLL_REASON_QUALITY = "quality"          # 画质缺陷（模糊/畸变/闪烁/糊脸）
REROLL_REASON_OTHER = "other"              # 其他（**必须**写备注，否则无从分析）
REROLL_REASONS = (REROLL_REASON_POSE, REROLL_REASON_IDENTITY,
                  REROLL_REASON_COMPOSITION, REROLL_REASON_TEXT,
                  REROLL_REASON_AUDIO, REROLL_REASON_QUALITY,
                  REROLL_REASON_OTHER)

# 裁决记录的**来源**（25 号复查：三处都在往 shot_reviews 写字，但含义完全不同
# ——提交动作 / 人工裁决意向 / AI 判级建议。统计「重摇原因填写率」和「盲摇次数」
# 时只有前者算数；不区分就会把模型判级和建议当成用户的行动，得出永远到不了
# 100% 的假指标）
REVIEW_ORIGIN_SUBMIT = "submit"     # 提交端点写的 take（一次真实的生成动作）
REVIEW_ORIGIN_VERDICT = "verdict"   # 人工五级裁决按钮（意向记录，未提交）
REVIEW_ORIGIN_AUTO = "auto"         # AI 审片（多模态判级建议）

# 原因码 → 白话名（**服务端文案用**：400 提示必须自解释「能选什么」；
# 前端另有一份带 hint 的展示表——两边都是文案，契约是上面的**取值**）
REROLL_REASON_LABELS = {
    REROLL_REASON_POSE: "姿态", REROLL_REASON_IDENTITY: "人物不一致",
    REROLL_REASON_COMPOSITION: "构图", REROLL_REASON_TEXT: "文字",
    REROLL_REASON_AUDIO: "声音", REROLL_REASON_QUALITY: "画质",
    REROLL_REASON_OTHER: "其他",
}


def _norm_roll_reasons(raw: str) -> str:
    """重摇原因归一（**多选**，逗号分隔；2026-10-10 用户要求）：逐项校验去重保序。

    返回存储串（如 "identity,pose"；空串 = 没选）。非法取值直接 400——错误
    要在提交时响亮地暴露，落库的每个原因码都必须是清单内的（分布统计才可信）。
    单值（无逗号）与旧数据完全兼容。
    """
    parts = [p.strip().lower() for p in (raw or "").split(",") if p.strip()]
    seen: list[str] = []
    for p in parts:
        if p not in REROLL_REASONS:
            raise HTTPException(
                400, f"无效重摇原因：{p}（可选：" + "、".join(REROLL_REASONS) + "）")
        if p not in seen:
            seen.append(p)
    return ",".join(seen)


# 端点不可用（25 号 1.5）在 400 文案里的**稳定标记**：前端据此弹「强制提交」
# 逃生口（与「体检存在」同一套机制）——不靠对整句文案的匹配（改文案即静默失效）
ENDPOINT_UNAVAILABLE_TAG = "[端点不可用]"

# 盲摇止损（§34.2）在 400 文案里的**稳定标记**：同款机制——前端据此弹
# 「放弃学习价值、再盲摇一次」的强提确认（此前文案让用户"勾选强提"，而
# 单镜入口根本没有这个开关，用户被堵死且不知为何；2026-10-10 实测）。
REROLL_BUDGET_TAG = "[重摇预算]"


# ---------- 公共工具 ----------
def _style_keywords(db: Session, project_id: str | None) -> str:
    """画风前缀：**风格锚点全称（若有）在最前，随后是画风关键词**。

    顺序即语义（20 号 §2.3）：官方要求全局风格限定词是「某某风格的电影」这类**具体**
    锚点、且置于 Prompt 最前、一字不差沿用；画风关键词（质感/镜头/画质）作为**追加的
    美学词组**跟在后面（官方明确「允许追加、主锚点不得替换」）。
    """
    project = db.get(Project, project_id) if project_id else None
    preset = project.style_preset if project else ""
    keywords = load_style_presets().get(preset, {}).get("keywords", "")
    anchor = anchor_prompt_for(preset, str(getattr(project, "style_anchor", "") or ""))
    if anchor:
        return f"{anchor}，{keywords}" if keywords else anchor
    return keywords


def _styled_prompt(keywords: str, content: str) -> str:
    """prompt 拼接：前缀画风关键词（无关键词时原样返回）。

    画风去重：解析阶段规范要求 image_prompt「以项目画风统一质感开头」，
    此类 content 再拼一遍前缀会出现连续两段画风质量词（实测场景图 prompt
    画风重复）。以画风关键词的首短语判定：content 以其开头才视为已带画风
    （codex review：子串包含判定会把恰含画风词的内容误判为已去重）。
    """
    if keywords:
        key_head = keywords.split("，", 1)[0].strip()
        if key_head and content.strip().startswith(key_head):
            return content
        return f"{keywords}，{content}"
    return content


def _join_parts(*parts: str) -> str:
    """拼接 prompt 片段：去掉各片段尾部标点后用中文逗号连接。"""
    cleaned = [p.strip().rstrip("。，,、！!？?；;") for p in parts if p and p.strip()]
    return "，".join(cleaned)


# 性别/年龄锚提取（首帧人物限定句防性别跑偏）：appearance 实测形态多样——
# 「中年男性，微胖，约五十岁」「青年男性，三十岁左右」「女，32岁」「34岁女性，
# 短发」「男主，28岁」。两种语序都要覆盖（审计 13 号 #4）：性别词在前（后 12
# 字内找年龄）与年龄在前（前 6 字内找性别词）；「男主/女主」类代称并入性别词。
_IDENTITY_RE = re.compile(
    r"([一二三四五六七八九十百\d]{1,4})岁[^。；\n]{0,6}?"
    r"(男主|女主|男性|女性|男人|女人|男|女)"
    r"|(男主|女主|男性|女性|男人|女人|男|女)[^。；\n]{0,12}?(?:约|大约)?"
    r"([一二三四五六七八九十百\d]{1,4})岁")
# 性别词归一（「男主/女主」→ 男/女）
_IDENTITY_SEX_NORM = {"男主": "男", "女主": "女"}


def _identity_anchor(char: Character) -> str:
    """从角色设定提取性别/年龄锚（如「男，50岁」）；无法提取返回空串。

    兼容两种语序：「男性，34岁」（性别在前）与「34岁女性」（年龄在前）；
    「男主/女主」类代称归一为 男/女。
    """
    from app.script.llm_parser import _cn_to_int

    text = f"{char.appearance or ''}，{char.persona or ''}"
    m = _IDENTITY_RE.search(text)
    if not m:
        return ""
    # 分支一（年龄在前）：组1=年龄、组2=性别词；分支二（性别在前）：组3=性别词、组4=年龄
    if m.group(1):
        age_raw, sex_raw = m.group(1), m.group(2)
    else:
        age_raw, sex_raw = m.group(4), m.group(3)
    sex = _IDENTITY_SEX_NORM.get(sex_raw or "", sex_raw or "")
    sex = sex.replace("性", "").replace("人", "") or sex_raw
    age = _cn_to_int(age_raw)
    return f"{sex}，{age}岁" if (age and sex) else ""


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    return project


def _resolve_model(db: Session, project_id: str, kind: str, explicit: str) -> str:
    """任务模型解析：显式指定 > 项目预设 > 空串（runner default_model 兜底）。

    在提交时解析并固化进 Task.model_id（缓存键依赖 model_id），改预设只影响之后的任务。
    """
    mid = (explicit or "").strip()
    if mid:
        return mid
    return project_preset(db.get(Project, project_id) if project_id else None, kind)


def _latest_artifact(db: Session, project_id: str, node_id: str) -> str | None:
    """节点最新抽卡版本的 artifact（无版本返回 None）。"""
    return db.scalar(
        select(NodeVersion.artifact_id)
        .where(NodeVersion.node_id == node_id,
               NodeVersion.project_id == project_id)
        .order_by(NodeVersion.seq.desc())
        .limit(1)
    ) or None


# ---------- 背对/侧对镜头的多角度参考图（2026-09-29） ----------
# 有人背对镜头时模型最容易把朝向画反（实测同一提示词两次抽卡一正一反）。角色若有
# **四视图**资产，可从里面裁出「背面/侧面」单图作为额外参考图送进去——**仅对认多
# 角度的端点**（agnes 系）；方舟/Seedance 官方明确「多视图会被识别为多个主体、
# 反而加剧 ID 漂移」，故按端点分叉（25 号 §7.7）。
_BACK_FACING_WORDS: tuple[str, ...] = ("背对", "背朝", "背影", "后脑")
_SIDE_FACING_REF_WORDS: tuple[str, ...] = ("侧对", "侧脸", "侧身", "半侧")


def _material_views(video_prompt: str, name: str) -> tuple[bool, bool]:
    """素材枚举里该角色声明了「背对」还是「侧对」→ (要背面图, 要侧面图)。

    确定性解析：只在【全局定义】的素材行里找 `角色@名（…）` 这一小段（**名字是
    闭集**、词表固定），不做全文语义猜测。找不到该角色的括号说明时两者都 False
    （宁可不补，也不送错角度的图）。
    """
    m = re.search(rf"角色@{re.escape(name)}[（(]([^）)]*)[）)]",
                  video_prompt or "")
    if not m:
        return False, False
    seg = m.group(1)
    return (any(w in seg for w in _BACK_FACING_WORDS),
            any(w in seg for w in _SIDE_FACING_REF_WORDS))


def _is_placeholder_artifact(db: Session, artifact_id: str | None) -> bool:
    """占位产物判定（mock 图/彩条视频/抽帧失败留下的 fallback 帧）。

    只看产物 meta 里的显式标记（`mock` / `fallback`，2026-09-30 复查 R6 起
    mock 产物与抽帧失败都打标），不去猜文件内容或体积。
    """
    if not artifact_id:
        return False
    asset = db.get(Asset, artifact_id)
    meta = (asset.meta_json or {}) if asset is not None else {}
    return bool(meta.get("mock") or meta.get("fallback"))


def _first_frame_artifact(db: Session, project_id: str,
                          shot: Shot) -> str | None:
    """该镜**生效的首帧**产物：优先用户在候选里「设为首帧」的那一版，否则最新版。

    为什么要有挑选（2026-09-29）：一次抽 N 张会落成 N 个版本，而下游（视频生成
    以首帧起像、卡片展示、就绪度检查）此前一律取「最新版本」——抽完 3 张等于只有
    最后一张算数，前两张白抽。`shots.first_frame_version_id`（建库时就有、一直是
    废字段）现在承担这个指针；解析不出（版本被删/跨项目）时**保守回退到最新版**。

    占位避让（2026-09-30 复查 R6）：自动回退选「最新版」时跳过占位版本
    （mock 图/抽帧失败的 fallback 帧）——一次抽帧失败不该让占位帧自动成为
    生效首帧被送去付费视频。用户手选的那一版不拦（尊重选择），只留日志。
    """
    pref = str(getattr(shot, "first_frame_version_id", "") or "")
    if pref:
        row = db.get(NodeVersion, pref)
        if row is not None and row.project_id == project_id                 and row.node_id == f"shot_{shot.id}_ff" and row.artifact_id:
            if _is_placeholder_artifact(db, row.artifact_id):
                logger.warning("镜 %s 手选的首帧是占位产物（mock/fallback），"
                               "按用户选择使用", str(shot.id)[:8])
            return row.artifact_id
    rows = db.scalars(
        select(NodeVersion)
        .where(NodeVersion.project_id == project_id,
               NodeVersion.node_id == f"shot_{shot.id}_ff")
        .order_by(NodeVersion.seq.desc())
    ).all()
    latest: str | None = None
    for row in rows:
        if not row.artifact_id:
            continue
        if latest is None:
            latest = row.artifact_id          # 全是占位时的兜底（保持 mock 链路可用）
        if not _is_placeholder_artifact(db, row.artifact_id):
            return row.artifact_id
    return latest


def _scene_artifact(db: Session, scene: Scene) -> str | None:
    """场景当前生效产物：优先用户「设为生效」的版本，否则最新版本。

    生效版本指针无效（版本被删/不属于该场景）时自动回退最新。
    """
    pref = getattr(scene, "preferred_version_id", None)
    if pref:
        v = db.get(NodeVersion, pref)
        if (v and v.node_id == f"scene_{scene.id}"
                and v.project_id == scene.project_id and v.artifact_id):
            return v.artifact_id
    return _latest_artifact(db, scene.project_id, f"scene_{scene.id}")


def _prop_artifact(db: Session, prop) -> str | None:
    """道具当前生效产物（同场景机制：优先生效指针，回退最新版本）。"""
    pref = getattr(prop, "preferred_version_id", None)
    if pref:
        v = db.get(NodeVersion, pref)
        if (v and v.node_id == f"prop_{prop.id}"
                and v.project_id == prop.project_id and v.artifact_id):
            return v.artifact_id
    return _latest_artifact(db, prop.project_id, f"prop_{prop.id}")


def _character_image_artifact(db: Session, project_id: str,
                              img: CharacterImage) -> str | None:
    """形象记录当前生效产物：优先用户「设为生效」的版本，否则最新版本
    （与场景/道具同机制；指针无效——版本被删/不属于该形象——自动回退）。"""
    pref = getattr(img, "preferred_version_id", None)
    node_id = f"char_{img.character_id}_{img.id}"
    if pref:
        v = db.get(NodeVersion, pref)
        if v and v.node_id == node_id and v.project_id == project_id \
                and v.artifact_id:
            return v.artifact_id
    return _latest_artifact(db, project_id, node_id)


def _node_generating(db: Session, project_id: str, prefix: str) -> bool:
    """node_id 前缀关联任务是否有 queued/running 中的最新任务。"""
    row = db.scalar(
        select(Task.id)
        .where(Task.project_id == project_id,
               Task.node_id.like(f"{prefix}%"),
               Task.status.in_(_ACTIVE_STATUSES))
        .limit(1)
    )
    return row is not None


def _character_dict(db: Session, project_id: str, c: Character) -> dict:
    rows = db.scalars(
        select(CharacterImage)
        .where(CharacterImage.character_id == c.id)
        .order_by(CharacterImage.created_at, CharacterImage.id)
    ).all()
    images = []
    for img in rows:
        artifact_id = _character_image_artifact(db, project_id, img)
        # 过滤取消残留的占位记录：无产物且已无进行中任务（避免空白缩略图常驻列表）
        if artifact_id is None and not _node_generating(db, project_id, f"char_{c.id}_{img.id}"):
            continue
        images.append({"id": img.id, "name": img.name, "kind": img.kind,
                       "episodes": img.episodes_json or [],
                       "artifact_id": artifact_id,
                       # 用户「设为生效」的版本 id（空 = 跟随最新版本）
                       "preferred_version_id": getattr(img, "preferred_version_id", None)})
    return {
        "id": c.id, "name": c.name, "role": getattr(c, "role", None) or "配角",
        "persona": c.persona,
        "appearance": c.appearance, "voice_desc": c.voice_desc,
        "voice_ref_asset_id": getattr(c, "voice_ref_asset_id", None),
        "image_prompt": getattr(c, "image_prompt", None) or "",
        "images": images,
        # 首见集号（逐集资产解析写入）：前端据此数「本集新增资产」与标注
        # 「第 N 集解析」——**此前未序列化**，导致资产面板恒显「未解析」
        "origin_seq": getattr(c, "origin_seq", 0) or 0,
        # 出场顺序（该集解析时它在剧本里的先后；0 = 未记录，见 _apply_order_seq）
        "order_seq": getattr(c, "order_seq", 0) or 0,
    }


def _character_sort_key(db: Session, project_id: str, created_key):
    """角色「老排序」的键函数：主角在前 → 出场分镜数降序 → 创建时间。

    出场分镜数 = 项目内引用该角色 id 的分镜数量（一次查询后内存计数）。
    只在**没有出场顺序**的资产上生效（老项目 / 手工新建），见 _apply_order_seq。
    """
    ep_ids = select(Episode.id).where(Episode.project_id == project_id)
    counts: dict[str, int] = {}
    for (ref_ids,) in db.execute(
        select(Shot.character_ref_ids_json).where(Shot.episode_id.in_(ep_ids))
    ):
        for cid in ref_ids or []:
            counts[cid] = counts.get(cid, 0) + 1
    return lambda c: (
        0 if (getattr(c, "role", "") or "配角") == "主角" else 1,
        -counts.get(c.id, 0),
        created_key(c),
    )


def _apply_order_seq(rows: list, legacy_key) -> list:
    """资产列表排序：**有出场顺序的按「来源集号 + 出场顺序」排在前，缺值的按原规则排在后**。

    出场顺序由逐集资产解析写入（LLM 输出数组的下标 = 该资产在该集正文里的出场先后，
    见 persist.upsert_episode_assets）；来源集号 = 它首次出现在哪一集。两者合起来就是
    「按剧本出场先后」的顺序——也是资产页/故事板要的展示次序。

    `order_seq == 0` 表示未记录（手工新建，或 18 号逐集解析之前解析的老项目）：
    这类**整体回退原有排序**（角色：主角优先；场景/道具：创建时间），
    因此老项目的列表顺序与改动前逐字一致。
    """
    legacy = sorted(rows, key=legacy_key)
    ordered = sorted(
        (r for r in legacy if getattr(r, "order_seq", 0)),
        key=lambda r: (getattr(r, "origin_seq", 0) or 0, r.order_seq),
    )
    return ordered + [r for r in legacy if not getattr(r, "order_seq", 0)]


def _scene_dict(db: Session, project_id: str, s: Scene) -> dict:
    _grp = db.get(SceneGroup, s.group_id) if getattr(s, "group_id", "") else None
    return {
        "id": s.id, "name": s.name, "description": s.description,
        # 场景组（32 号）：只带界面展示用的最小信息；详情走 /scenes/{id}/scene-group
        "group": ({"id": _grp.id, "name": _grp.name,
                   "env_setting": _grp.env_setting or ""} if _grp else None),
        "image_prompt": getattr(s, "image_prompt", None) or "",
        "artifact_id": _scene_artifact(db, s),
        # 用户「设为生效」的版本 id（空 = 跟随最新版本）
        "preferred_version_id": getattr(s, "preferred_version_id", None),
        "freq": s.freq, "episodes": s.episodes_json,
        "origin_seq": getattr(s, "origin_seq", 0) or 0,
        "order_seq": getattr(s, "order_seq", 0) or 0,
    }


def _prop_dict(db: Session, project_id: str, p: Prop) -> dict:
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "image_prompt": getattr(p, "image_prompt", None) or "",
        "artifact_id": _prop_artifact(db, p),
        "preferred_version_id": getattr(p, "preferred_version_id", None),
        "freq": p.freq, "episodes": p.episodes_json,
        "origin_seq": getattr(p, "origin_seq", 0) or 0,
        "order_seq": getattr(p, "order_seq", 0) or 0,
    }


def _episode_assets(db: Session, episode_id: str) -> dict:
    """本集资产解析结果（Setting 存档；未解析返回空 {})。

    与审核清单同一条通道：结构化结果落存档 + 随分集列表透出，不走任务 error。
    """
    row = db.get(Setting, ep_assets_key(episode_id))
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) else {}
    except ValueError:
        logger.warning("本集资产存档损坏 episode=%s", episode_id)
        return {}


def _episode_dict(db: Session, e: Episode) -> dict:
    count = db.scalar(
        select(func.count()).select_from(Shot).where(Shot.episode_id == e.id)
    )
    # 已合成的成片产物：episode_{eid} 节点最新 done 任务的 artifact
    # （合成入口此前只在前端临时态展示，刷新即失——持久化供随时回看）
    compose_artifact = db.scalar(
        select(Task.artifact_id).where(
            Task.node_id == f"episode_{e.id}", Task.status == "done",
            Task.artifact_id.isnot(None))
        .order_by(Task.created_at.desc()).limit(1)
    )
    # 分集计划字段（18 号 §55）：正文 body 是大字段（单集数千字），按性能红线
    # 不进列表响应，只给字数；正文走 GET /episodes/{id}/script 按需取
    return {"id": e.id, "seq": e.seq, "title": e.title, "status": e.status,
            "shot_count": count or 0,
            "compose_artifact_id": compose_artifact,
            "body_chars": len(e.body or ""),
            "synopsis": e.synopsis or "",
            "beats": list(e.beats_json or []),
            "hooks": list(e.hooks_json or []),
            "plan_source": e.plan_source or "",
            "produce_status": e.produce_status or "",
            "audit": (e.audit_json or {}) if isinstance(e.audit_json, dict) else {},
            "assets": _episode_assets(db, e.id),
            }


def _shot_dict(db: Session, project_id: str, shot: Shot) -> dict:
    # 首帧/视频 artifact 与状态按 node_versions 最新版本动态推导
    ff = _first_frame_artifact(db, project_id, shot)
    video = _latest_artifact(db, project_id, f"shot_{shot.id}_v")
    status = "done" if video else ("first_frame" if ff else "pending")
    return {
        "id": shot.id, "seq": shot.seq, "description": shot.description,
        "excerpt": shot.excerpt or "",
        "video_prompt": shot.video_prompt or "",
        # 时序对账（D3）：画面描述时间小节与当前时长不符 = 时长被编辑过或
        # 生成时失配——前端展示 stale 徽标并可一键重算画面描述
        "timing_ok": not timing_error({"duration": shot.duration,
                                       "video_prompt": shot.video_prompt or ""}),
        "camera": shot.camera, "duration": shot.duration,
        "scene_ref_id": shot.scene_ref_id,
        "character_ref_ids": shot.character_ref_ids_json,
        # 道具引用（资产栏定位/提及映射数据源；生成链路 prop_ref_ids_json
        # 早已消费，此前未暴露给前端）
        "prop_ref_ids": shot.prop_ref_ids_json or [],
        "costume_map": shot.costume_map_json or {},
        # 镜头目的与已知脆弱点（批次 2b §32.5）：审片重点核对与前端徽标数据源
        "purpose": shot.purpose or "",
        "risks": shot.risks_json or [],
        # 结束状态账本（批次 2c §32.6）：状态链对账出口态，前端可查
        "end_state": shot.end_state_json or {},
        # 多切关键帧（批次 4 §35.2）：key_scenes 段内 2 切
        "cuts": shot.cuts_json or [],
        "dialogue": shot.dialogue_json,
        "first_frame_artifact_id": ff,
        "video_artifact_id": video,
        # ---- 三条状态语义分离（借鉴 Jellyfish，见 services/readiness.py）----
        # status/generating = 运行时状态（由任务系统与产物推导）
        "status": status,
        "generating": _node_generating(db, project_id, f"shot_{shot.id}"),
        # info_status = 信息确认态（资产引用是否都已确认可用，确定性推导）
        "info_status": shot_info_status(db, project_id, shot),
        # video_readiness = 视频就绪度（结构化检查项，提交前硬门的同一来源）
        "video_readiness": shot_video_readiness(db, project_id, shot),
    }


def _cancel_active_tasks(db: Session, project_id: str, node_prefix: str) -> int:
    """取消节点前缀匹配的进行中任务（排队/加载模型/生成中），返回取消数量。

    排队任务立即置 canceled；生成中任务由 cancel 事件协作中止（runner 在
    步级回调处检查，模型加载完成后不再进入生成）。
    """
    task_ids = db.scalars(select(Task.id).where(
        Task.project_id == project_id,
        Task.node_id.like(f"{node_prefix}%"),
        Task.status.in_(_ACTIVE_STATUSES),
    )).all()
    for task_id in task_ids:
        engine.cancel(task_id)
    return len(task_ids)


def _asset_shot_refs(db: Session, project_id: str) -> tuple[list[Shot], dict[str, int]]:
    """项目全部分镜 + episode_id→集 seq 映射（资产删除预检的引用统计共用）。"""
    episodes = db.scalars(
        select(Episode).where(Episode.project_id == project_id)).all()
    shots = db.scalars(select(Shot).where(
        Shot.episode_id.in_([e.id for e in episodes]))).all()
    return shots, {e.id: e.seq for e in episodes}


def _ref_episode_seqs(ref_shots: list[Shot], ep_seqs: dict[str, int]) -> list[int]:
    """引用分镜去重后的集 seq 升序列表（确认弹窗展示「第几集」用）。"""
    return sorted({ep_seqs[s.episode_id] for s in ref_shots
                   if s.episode_id in ep_seqs})


def _costume_map_to_names(db: Session, costume_map: dict) -> dict:
    """妆造映射转名字存档：character_id→image_id 转为 角色名→形象名。

    版本恢复后角色/形象 id 全新，只有名字可跨版本对应；查不到的条目跳过。
    """
    out: dict[str, str] = {}
    for cid, img_id in (costume_map or {}).items():
        char = db.get(Character, cid)
        img = db.get(CharacterImage, img_id)
        if char and img:
            out[char.name] = img.name
    return out


# ---------- 剧本解析 ----------
# 解析任务 node_id（前端按此前缀识别解析任务并展示进度/刷新数据）
NODE_ID_PARSE = "script_parse"


@router.get("/projects/{project_id}/script/requirements")
def get_script_requirements(project_id: str, db: Session = Depends(get_db)):
    """读取项目的 AI 解析前置需求（重解析对话框回填用）。

    无存档时返回默认值（**旧项目不报错**）：这是「新功能不破坏老项目」的契约。
    附 `options` 供前端渲染选项（单一真源在后端，前端不硬编码档位）。
    """
    _project(db, project_id)
    return {
        "requirements": load_requirements(db, project_id),
        "options": {
            "tones": [{"value": k, "label": v} for k, v in TONE_LABELS.items()],
            "episode_minutes": list(EPISODE_MINUTE_OPTIONS),
            "audiences": [{"value": k, "label": v} for k, v in AUDIENCE_LABELS.items()],
        },
    }


@router.post("/projects/{project_id}/script/parse")
def parse_script(project_id: str, body: ScriptParseIn,
                 db: Session = Depends(get_db)):
    """提交剧本解析长任务（立即返回 task_id，拆解与落库在任务内异步执行）。

    keep_version=true 且已有解析结果时：先把当前生效结果整包快照为历史版本
    （可随时恢复），再执行新解析。进度/思考经 WS task_trace 实时推送；
    失败任务 failed（原因透出，旧解析数据保留），取消任务 canceled。
    """
    _project(db, project_id)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "剧本内容为空")
    # 配置类错误即时反馈（不空转任务）
    if not llm_available():
        raise HTTPException(400,
                            "LLM 未配置：请在「模型管理-服务配置」中填写 LLM 服务"
                            "（AVS_LLM_PROVIDER=openai、AVS_LLM_BASE_URL、"
                            "AVS_LLM_API_KEY）后重试")

    # 保留旧版本（v1.1 降级为「剧本原文版本」）：新解析会覆盖旧解析产物，
    # 而分镜级生成物本来就不随版本走（分镜 id 全变即变孤儿），整包快照恢复出来
    # 也只是「产物壳子」——所以只留真正值钱且可回滚的东西：**剧本原文**。
    # 回滚 = 把该版原文填回编辑器重新解析（有资产缓存，重解析很便宜）。
    if body.keep_version:
        prev_text = db.get(Setting, script_text_key(project_id))
        prev_value = (prev_text.value or "").strip() if prev_text else ""
        last = db.scalars(
            select(ScriptVersion).where(ScriptVersion.project_id == project_id)
            .order_by(ScriptVersion.seq.desc()).limit(1)).first()
        # 与上一版原文相同则不重复入档（避免连点解析刷出一串同文版本）
        if prev_value and (last is None or (last.text or "") != prev_value):
            meta_setting = db.get(Setting, script_meta_key(project_id))
            note = ""
            if meta_setting and meta_setting.value:
                try:
                    note = str((json.loads(meta_setting.value) or {}).get("title", ""))
                except ValueError:
                    note = ""
            seq = (last.seq if last else 0) + 1
            db.add(ScriptVersion(project_id=project_id, seq=seq,
                                 note=note[:200], text=prev_value,
                                 data_json=""))
            db.commit()

    # 解析原文先落库（即使后续拆解失败也保留）：刷新/重进后回填编辑器可直接重新解析
    db.merge(Setting(key=script_text_key(project_id), value=text))
    # 前置需求落库（21 号批次 5）：**不随任务参数传递**——任务 payload 禁带大参数，
    # 且 runner 侧读 Setting 更省事（同 project_style_context 的做法）。
    # 传空 = 沿用存档（重解析对话框会回填上次值，用户不改就不覆盖）
    if body.requirements is not None:
        save_requirements(db, project_id, body.requirements)
    db.commit()

    try:
        submitted = engine.submit(
            project_id=project_id, node_id=NODE_ID_PARSE,
            runner_name=RUNNER_PARSE_SCRIPT,
            params={"project_id": project_id, "text": text,
                    },
        )
    except Exception as e:  # noqa: BLE001 提交异常（未知 runner 等）转 HTTP 错误
        raise HTTPException(400, str(e)) from e
    return {"task_id": submitted["task_id"], "status": submitted["status"]}


@router.get("/projects/{project_id}/script")
def get_script(project_id: str, db: Session = Depends(get_db)):
    setting = db.get(Setting, script_meta_key(project_id))
    meta = json.loads(setting.value) if setting and setting.value else {}
    # 上次解析的原文（可能不存在：旧版本解析的项目），回填编辑器供重新解析
    text_setting = db.get(Setting, script_text_key(project_id))
    characters = db.scalars(
        select(Character).where(Character.project_id == project_id)
        .order_by(Character.created_at, Character.id)
    ).all()
    scenes = db.scalars(
        select(Scene).where(Scene.project_id == project_id)
        .order_by(Scene.created_at, Scene.id)
    ).all()
    props_rows = db.scalars(
        select(Prop).where(Prop.project_id == project_id)
        .order_by(Prop.created_at, Prop.id)
    ).all()
    episodes = db.scalars(
        select(Episode).where(Episode.project_id == project_id)
        .order_by(Episode.seq)
    ).all()
    # 当前生效解析版本号：Setting 缺失时按旧数据兜底（已解析项目 = 历史
    # 版本下一空号，与 bump 公式一致；从未解析为 0，前端不显示标签）
    cur_seq = get_script_cur_seq(db, project_id)
    if cur_seq <= 0 and meta:
        max_seq = db.scalar(
            select(func.max(ScriptVersion.seq))
            .where(ScriptVersion.project_id == project_id)) or 0
        cur_seq = max_seq + 1
    return {
        "analyzed": bool(meta),
        "title": meta.get("title", ""),
        "logline": meta.get("logline", ""),
        "style": meta.get("style", ""),
        "video_style": meta.get("video_style", ""),
        "aspect_ratio": meta.get("aspect_ratio", ""),
        "text": (text_setting.value or "") if text_setting else "",
        # 当前生效解析版本号（header 标签）与过程轨迹（观测窗恢复）
        "version": cur_seq,
        "trace": load_script_trace(db, project_id),
        "characters": [
            _character_dict(db, project_id, c)
            for c in _apply_order_seq(characters, _character_sort_key(
                db, project_id, lambda c: (c.created_at, c.id)))
        ],
        "scenes": [
            _scene_dict(db, project_id, s)
            for s in _apply_order_seq(scenes, lambda x: (x.created_at, x.id))
        ],
        "props": [
            _prop_dict(db, project_id, p)
            for p in _apply_order_seq(props_rows, lambda x: (x.created_at, x.id))
        ],
        "episodes": [_episode_dict(db, e) for e in episodes],
    }


@router.get("/projects/{project_id}/script/versions")
def list_script_versions(project_id: str, db: Session = Depends(get_db)):
    """剧本原文版本列表（v1.1：只存原文，不再有整包产物统计）。

    历史行（v1.0 及更早写入的整包快照）一律按只读文本对待：列表与恢复只看
    text 字段，data_json 不再消费。
    """
    _project(db, project_id)
    rows = db.scalars(select(ScriptVersion).where(ScriptVersion.project_id == project_id)
                      .order_by(ScriptVersion.seq)).all()
    return [{
        "id": v.id, "seq": v.seq, "note": v.note, "created_at": v.created_at,
        "text_chars": len(v.text or ""),
    } for v in rows]


@router.post("/script/versions/{version_id}/restore")
def restore_script_version(version_id: str, db: Session = Depends(get_db)):
    """恢复历史剧本原文：把该版原文写回编辑器原文存档（不重放解析产物）。

    v1.1 降级语义：版本 = 剧本原文，恢复 = 换回那一版稿子。用户确认内容后重新
    解析即可（资产提取有缓存，重解析成本主要是拆解与画面描述）。解析产物与
    版本号不受影响——恢复文本不改动当前生效结果。
    """
    v = db.get(ScriptVersion, version_id)
    if not v:
        raise HTTPException(404, "历史版本不存在")
    if not (v.text or "").strip():
        raise HTTPException(400, "该版本没有可恢复的剧本原文")
    save_script_text(db, v.project_id, v.text)
    return {"ok": True, "project_id": v.project_id, "text": v.text,
            "chars": len(v.text)}


@router.post("/projects/{project_id}/script/extract")
async def extract_script_doc(project_id: str, file: UploadFile = File(...),
                             db: Session = Depends(get_db)):
    """上传剧本文档（txt/md/docx/pdf）提取纯文本。

    不直接落库解析：文本返回前端填回编辑器，用户确认后走 script/parse
    （解析会覆盖旧的角色/场景/分集数据，需用户显式确认）。
    """
    _project(db, project_id)
    filename = file.filename or ""
    try:
        # 带上限的分块读取（2026-09-30 安全整改）：旧实现 `await file.read()`
        # 先整份读进内存再判内容——上限拦不住内存占用。
        data = await read_upload_capped(file)
        try:
            text = extract_doc_text(filename, data)
        except DocExtractError as e:
            raise HTTPException(400, str(e)) from e
        if not text.strip():
            raise HTTPException(400, "未从文档中提取到文本内容")
        return {"filename": filename, "chars": len(text), "text": text}
    finally:
        await file.close()


class ScriptExpandIn(BaseModel):
    text: str


class PromptOptimizeIn(BaseModel):
    """AI 优化提示词入参（20 号批次 3）：只传「改哪里 + 想怎么改」，上下文由服务端组装。"""
    # character（角色定妆）| costume（角色妆造）| scene（场景主图）| prop（道具图）| shot（分镜视频）
    target: str
    # 对象 id（Character.id / CharacterImage.id / Scene.id / Prop.id / Shot.id）
    id: str
    # 用户的修改需求（自然语言）
    instruction: str
    # 编辑中的文本（可空）：仅用于「对象尚无持久提示词」的场景（妆造建议草稿），
    # 服务端优先用对象自身的提示词，取不到时才用此值
    current_prompt: str = ""


@router.post("/projects/{project_id}/prompts/optimize")
def optimize_prompt_endpoint(project_id: str, body: PromptOptimizeIn,
                             db: Session = Depends(get_db)):
    """AI 优化提示词（20 号批次 3）：按用户的修改需求改写一条生图/生视频提示词。

    **纯计算不落库** —— 返回候选文本与校验告警；写回由用户在弹窗点「应用」后
    走既有 PATCH 端点（不新增写路径）。失败语义：参数错 400、LLM 不可用 502。
    """
    _project(db, project_id)
    try:
        return optimize_prompt(db, project_id, body.target, body.id,
                               body.instruction, body.current_prompt)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except ProviderError as e:
        raise HTTPException(502, f"AI 优化失败：{e}") from e


@router.post("/projects/{project_id}/script/expand")
def expand_script(project_id: str, body: ScriptExpandIn,
                  db: Session = Depends(get_db)):
    """一句话梗概扩写为标准剧本（仅 LLM，失败直接报错），返回文本供确认（6.2 第 3 条）。"""
    _project(db, project_id)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "剧本内容为空")
    try:
        return expand(text)
    except ProviderError as e:
        raise HTTPException(502, f"AI 扩写失败：{e}") from e


class EpisodeOptimizeIn(BaseModel):
    """逐集一键修复入参（18 号 §53）：前端只传勾选的问题条目。

    issues 为空时 runner 取该集存档里全部 critical——用户点「一键修复」而
    不勾选时，默认修全部 critical（与旧行为一致）。
    """

    issues: list[dict] = []


@router.post("/episodes/{episode_id}/audit")
def audit_episode_endpoint(episode_id: str, db: Session = Depends(get_db)):
    """提交单集剧情审核长任务（18 号 §52）：带上一集/下一集上下文。

    审核结果写 `Episode.audit_json`，随分集列表接口返回（结构化数据走端点，
    不进任务 error 字符串——历史教训：清单塞 error 会退化成「把原始 JSON
    糊到用户脸上」）。审核只拦 critical，major/minor 仅提示。
    """
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    if not llm_available():
        raise HTTPException(400, "LLM 未配置：请在「模型管理-服务配置」中填写 LLM 服务后重试")
    try:
        submitted = engine.submit(
            project_id=episode.project_id, node_id=_episode_node_id(
                NODE_ID_EP_AUDIT, episode_id),
            runner_name=RUNNER_EPISODE_AUDIT,
            params={"episode_id": episode_id},
        )
    except Exception as e:  # noqa: BLE001 提交异常（未知 runner 等）转 HTTP 错误
        raise HTTPException(400, str(e)) from e
    return {"task_id": submitted["task_id"], "status": submitted["status"]}


@router.post("/episodes/{episode_id}/assets")
def extract_episode_assets_endpoint(episode_id: str,
                                    db: Session = Depends(get_db)):
    """提交本集资产解析长任务（18 号 §61）：只补新增，不重复解析已有资产。

    输入=本集正文 + 已有资产名单（第 1 集额外带前置章节/总纲）；输出=新增资产
    + 别名映射。代码先按名字过滤掉名单内的（提示词也已要求只输出新增），
    再落库并记录来源集号——资产页据此标注「第 N 集解析」。
    """
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    if not (episode.body or "").strip():
        raise HTTPException(400, "本集正文为空，无法解析资产（请先解析或重新分集）")
    if not llm_available():
        raise HTTPException(400, "LLM 未配置：请在「模型管理-服务配置」中填写 LLM 服务后重试")
    try:
        submitted = engine.submit(
            project_id=episode.project_id, node_id=_episode_node_id(
                NODE_ID_EP_ASSETS, episode_id),
            runner_name=RUNNER_EPISODE_ASSETS,
            params={"episode_id": episode_id},
        )
    except Exception as e:  # noqa: BLE001 提交异常（未知 runner 等）转 HTTP 错误
        raise HTTPException(400, str(e)) from e
    return {"task_id": submitted["task_id"], "status": submitted["status"]}


@router.post("/episodes/{episode_id}/optimize")
def optimize_episode_endpoint(episode_id: str, body: EpisodeOptimizeIn,
                              db: Session = Depends(get_db)):
    """提交单集一键修复长任务（18 号 §53）：补丁协议定点修复，**不复审**。

    body.issues 为空时取存档里该集的全部 critical。修复稿写回当集正文与剧本
    原文对应区间；命中/未命中由代码逐条对账写回 audit_json（面板按条标出）。
    """
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    if not llm_available():
        raise HTTPException(400, "LLM 未配置：请在「模型管理-服务配置」中填写 LLM 服务后重试")
    try:
        submitted = engine.submit(
            project_id=episode.project_id, node_id=_episode_node_id(
                NODE_ID_EP_OPTIMIZE, episode_id),
            runner_name=RUNNER_EPISODE_OPTIMIZE,
            params={"episode_id": episode_id, "issues": body.issues or []},
        )
    except Exception as e:  # noqa: BLE001 提交异常（未知 runner 等）转 HTTP 错误
        raise HTTPException(400, str(e)) from e
    return {"task_id": submitted["task_id"], "status": submitted["status"]}


@router.post("/episodes/{episode_id}/build")
def build_episode_endpoint(episode_id: str, db: Session = Depends(get_db)):
    """提交单集制作长任务（18 号 §54）：拆解 → 立即落库 → 画面描述 → 回填。

    做成独立任务的关键理由：旧链路整部一次解析，10 集 593 镜跑满 88 分钟后
    在落库前被看门狗杀掉、593 镜全丢；逐集制作后失败只损失一集。
    """
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    if not (episode.body or "").strip():
        raise HTTPException(400, "本集正文为空，无法制作（请先解析或重新分集）")
    if not llm_available():
        raise HTTPException(400, "LLM 未配置：请在「模型管理-服务配置」中填写 LLM 服务后重试")
    try:
        submitted = engine.submit(
            project_id=episode.project_id, node_id=_episode_node_id(
                NODE_ID_EP_BUILD, episode_id),
            runner_name=RUNNER_EPISODE_BUILD,
            params={"episode_id": episode_id},
        )
    except Exception as e:  # noqa: BLE001 提交异常（未知 runner 等）转 HTTP 错误
        raise HTTPException(400, str(e)) from e
    return {"task_id": submitted["task_id"], "status": submitted["status"]}


# ---------- 角色 ----------
@router.post("/projects/{project_id}/characters", status_code=201)
def create_character(project_id: str, body: CharacterIn,
                     db: Session = Depends(get_db)):
    _project(db, project_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "角色名不能为空")
    if body.role not in CHARACTER_ROLES:
        raise HTTPException(400, f"无效角色定位：{body.role}")
    row = Character(project_id=project_id, name=name, role=body.role,
                    persona=body.persona,
                    image_prompt=body.image_prompt,
                    appearance=body.appearance, voice_desc=body.voice_desc)
    db.add(row)
    db.commit()
    out = _character_dict(db, project_id, row)
    out["generating"] = False
    return out


@router.get("/projects/{project_id}/characters")
def list_characters(project_id: str, db: Session = Depends(get_db)):
    _project(db, project_id)
    rows = db.scalars(
        select(Character).where(Character.project_id == project_id)
        .order_by(Character.created_at, Character.id)
    ).all()
    return [
        {**_character_dict(db, project_id, c),
         "generating": _node_generating(db, project_id, f"char_{c.id}")}
        for c in _apply_order_seq(rows, _character_sort_key(
            db, project_id, lambda c: (c.created_at, c.id)))
    ]


@router.patch("/characters/{character_id}")
def patch_character(character_id: str, body: CharacterPatchIn, db: Session = Depends(get_db)):
    row = db.get(Character, character_id)
    if not row:
        raise HTTPException(404, "角色不存在")
    # 归属校验（2026-09-30）：地址里没有 project_id，必须按资源的 project_id 判
    if body.role is not None and body.role not in CHARACTER_ROLES:
        raise HTTPException(400, f"无效角色定位：{body.role}")
    for field in ("name", "role", "persona", "appearance", "voice_desc",
                  "image_prompt"):
        value = getattr(body, field)
        if value is not None:
            setattr(row, field, value)
    if body.image_prompt is not None:
        # 保存即重组装（2026-09-30 用户规则）：框里始终是完整提示词
        # （画风前缀/负向词按当前画风补回），点生成只是发送框里已有的内容。
        compose_character_prompt(db, row)
    db.commit()
    return {"ok": True}


@router.post("/characters/{character_id}/images", status_code=201)
def create_character_image(character_id: str, body: CharacterImageIn,
                           db: Session = Depends(get_db)):
    """生成定妆图（支持抽卡）：任务完成后由 task_engine 按
    node_id=char_{cid}_{imgId} 自动追加 node_versions。

    - 新建形象（默认）：落 CharacterImage 记录（无 artifact），基础形象一次
      生成一张；每次点「重新生成」即对该形象抽卡（同 node 追加版本）。
    - 抽卡（regenerate_image_id 非空）：不新建记录，在该形象下追加一个抽卡
      版本（与场景卡同款版本流）；basic 按角色当前设定重新组装提示词，
      妆造按原形象提示词原样重生成（妆造描述无独立存储，同参数保长相）。

    提示词组合（只喂视觉信息 + 固定构图限定）：
    - basic：外貌摘要 + 生图提示词（LLM 按剧本生成或手动编辑）+ 基础图
      固定约束（白底/单人/全身正面/平视/双手下垂等）+ 肢体尾锚；
    - costume：同上 + 妆造描述（不含默认服装的生图提示词部分，避免与
      妆造描述冲突），并以基础形象为参考图保持长相一致。
    persona（身份/剧情/性格等叙事设定）不进生图 prompt：生图模型无法理解
    叙事信息，只会被剧情词带偏（「察觉真凶归来」→ 沧桑老态、年龄失真、
    表情过度），外貌视觉特征已由 appearance + image_prompt 完整覆盖。
    """
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    if not char.project_id:
        raise HTTPException(400, "全局角色不支持生成定妆图")
    image: CharacterImage | None = None
    kind = body.kind
    if body.regenerate_image_id:
        # 抽卡模式：定位目标形象记录，按其类型续抽（body.kind 忽略）
        image = db.get(CharacterImage, body.regenerate_image_id)
        if not image or image.character_id != character_id:
            raise HTTPException(404, "形象不存在或不属于该角色")
        if image.kind != "costume":
            raise HTTPException(400, f"该形象类型不支持重新生成：{image.kind}")
        kind = image.kind
    elif body.kind not in CHARACTER_IMAGE_KINDS:
        raise HTTPException(400, f"无效形象类型：{body.kind}")

    if image is not None and kind == "costume":
        # 妆造抽卡：妆造描述无独立存储（appearance_desc 存的是组装后的完整
        # 提示词），按原文原样重生成（同参数抽卡，长相稳定）
        prompt_text = image.appearance_desc
    else:
        desc = (body.appearance_desc or "").strip()
        # 生图提示词取**剥净系统块**（画风前缀/负向词，2026-09-30 组装写回后字段
        # 里含这些）+ 姿态词清洗（历史 image_prompt 可能含「人物略微侧身」等与
        # 正面直立约束冲突的描述——侧身时模型会补画正面手臂，是三条胳膊畸形的
        # 头号诱因；剥离结果在框架里可见）
        image_prompt = _character_image_base(db, char)
        # 进 prompt 的视觉信息：外貌摘要 + 生图提示词（persona 不进，见 docstring）
        visual = _join_parts(char.appearance, image_prompt)
        # 风格负面词按画风整块取（批次 1 §31.8）：插在构图限定之前——肢体尾锚
        # 必须收尾（首尾双锚纪律），负面词提前不破坏锚位
        style_neg = _style_negative(db, char.project_id, "character")
        if not desc:
            raise HTTPException(400, "形象外观描述不能为空")
        # 妆造：服装以妆造描述为准，长相靠外貌摘要与参考图锁定。
        # 不拼空手姿势约束——妆造描述可含持械/持物造型（如持枪姿势）。
        # 肢体尾锚在此不拼：参考图一致性句（下方追加）必须位于尾锚之前，
        # 保证肢体尾锚始终收尾
        prompt_text = _join_parts(
            visual, desc, style_neg, prompts_mod.CHARACTER_IMAGE_BASE_CONSTRAINTS)

    # 妆造带基础参考图（有则传，跨形象长相一致；抽卡同享）——参考图就是该角色的
    # **角色设定图**（基础图，见 `_character_artifact`）
    input_refs: dict = {}
    ref_artifact = _character_artifact(db, char.project_id, character_id, None)
    if ref_artifact:
        input_refs = {"image": ref_artifact}
        # 一致性句仅在重新组装时追加（抽卡原文已含，避免重复堆叠）
        if image is None:
            prompt_text = _join_parts(
                prompt_text,
                "人物长相、发型与体型必须与参考图保持完全一致，服装妆面以文字描述为准")
    # 防回归兜底：肢体数量约束缺失则强制补（见 _ensure_limb_guarantee；
    # 幂等——抽卡原文已含锚词时不会重复追加）
    prompt_text = _ensure_limb_guarantee(
        prompt_text, prompts_mod.CHARACTER_COSTUME_LIMB_REINFORCE)

    if image is None:
        image = CharacterImage(character_id=character_id,
                               name=body.name or f"{char.name}形象",
                               kind=kind, appearance_desc=prompt_text,
                               episodes_json=sorted(set(body.episodes or [])))
        db.add(image)
    else:
        # 抽卡：记录描述同步为本次提示词（basic 已按最新设定重组）
        image.appearance_desc = prompt_text
    db.commit()  # 先落记录再提交任务，任务完成后列表接口可查

    node_id = f"char_{character_id}_{image.id}"
    keywords = _style_keywords(db, char.project_id)
    # 负向词按形象类型区分：妆造允许持械持物，不套用基础图的"禁手持"约束
    negative = prompts_mod.CHARACTER_COSTUME_NEGATIVE_PROMPT
    try:
        submitted = engine.submit(
            project_id=char.project_id, node_id=node_id, runner_name="t2i",
            params={"prompt": _styled_prompt(keywords, prompt_text),
                    "aspect": CHARACTER_IMAGE_ASPECT,
                    "negative_prompt": negative},
            model_id=_resolve_model(db, char.project_id, KIND_T2I, body.model_id),
            input_refs=input_refs,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    task = db.get(Task, submitted["task_id"])
    return {"task_id": submitted["task_id"],
            "version_id": task.version_id if task else None,
            "image_id": image.id, "node_id": node_id}


class FourViewIn(BaseModel):
    """四视图生成入参（生图模型可指定，空 = 项目预设/默认）。"""
    model_id: str = ""
    # 抽卡目标四视图记录（非空 = 不新建记录，在该记录下追加一个抽卡版本）
    regenerate_image_id: str = ""


def fourview_artifact(db: Session, project_id: str, cid: str) -> str | None:
    """该角色**三视图(四视图)大图**的最新产物（没有则 None）。

    「三视图」在本项目的落地形态就是这张一行四列的大图（见
    `CHARACTER_FOURVIEW_PROMPT`）——它一次覆盖四个角度，是**成本与参考图名额**
    都最省的做法；挂图时由 `_fourview_angle_artifact` 裁成单角度图再送。
    """
    images = db.scalars(select(CharacterImage).where(
        CharacterImage.character_id == cid,
        CharacterImage.kind == "fourview")).all()
    for img in reversed(images):
        art = _character_image_artifact(db, project_id, img)
        if art:
            return art
    return None


def submit_character_fourview(db: Session, char: Character, model_id: str = "",
                              regenerate_image_id: str = "",
) -> dict:
    """提交**角色设定图**（一张横版图：左侧头肩特写 + 右侧三格全身 正/侧/背）。

    这是角色形象的**唯一入口**（用户 2026-09-30 定稿）：新角色直接出设定图，
    **这张图就是它的基础形象**——整张作为后续所有镜头的参考图（不再裁切，
    见 `_character_artifact`）；图内的正/侧/背三格供模型理解该角色各角度。

    **一个角色只有一条记录**：重复生成一律往这条记录上追加版本，界面的版本条
    负责切换与「设为生效」；每点一次新建一条会让历史图变成看不见的孤儿记录。
    没有任何形象时也能出（纯文生图，第一张就是它）。
    """
    image: CharacterImage | None = None
    _created = False
    if regenerate_image_id:
        image = db.get(CharacterImage, regenerate_image_id)
        if not image or image.character_id != char.id:
            raise HTTPException(404, "四视图记录不存在或不属于该角色")
        if image.kind != "fourview":
            raise HTTPException(400, "目标形象不是四视图记录")
    else:
        # **一个角色只有一条设定图记录**（2026-09-30）：重复生成一律往这条记录上
        # 追加版本，而不是每点一次新建一条——否则界面上只有"最新那条"看得到，
        # 历史设定图全成了看不见的孤儿记录，版本条也就形同虚设（用户实测报障
        # 「多版本好像没显示出来」）。带 regenerate_image_id 时按指定的那条走。
        image = db.scalars(
            select(CharacterImage)
            .where(CharacterImage.character_id == char.id,
                   CharacterImage.kind == "fourview")
            .order_by(CharacterImage.created_at, CharacterImage.id)).all()[-1:]
        image = image[0] if image else None
    # 参考图：有基础形象就带上（保持跨次长相一致）；**没有也能出**——用户
    # 2026-09-30 定稿「基础形象直接生成三视图」：新角色第一张图就是三视图，
    # **这张图就是该角色的基础形象**（基础图=角色设定图，2026-09-30 用户定稿）
    ref_artifact = _character_artifact(db, char.project_id, char.id, None)

    keywords = _style_keywords(db, char.project_id)
    # **每次按当前设定重新组装**（2026-09-30 修报障）：旧实现在已有记录时按
    # 「库里存的原文」重生成——于是用户改完「生图提示词/外貌摘要」再点生成，
    # 改动永远不生效（以为改了其实没改）。抽卡语义不靠复用文本实现：设定没改时
    # 组装结果与上次一致（同一份文本换种子重摇），效果等同、改动生效。
    #
    # 外貌摘要 + 生图提示词（外貌与默认服装）+ 四视图固定提示词一并喂给生图
    # 模型（persona 不进 prompt——叙事设定会带偏画面，见 create_character_image）；
    # 四视图规格自含（一行四列横版与基础约束的竖版单人冲突，不拼后者），
    # 末尾复锚每格人物的肢体数量约束
    visual = _join_parts(char.appearance, _character_image_base(db, char))
    core = visual or char.name
    # 风格负面词插在四视图固定提示词之前（肢体尾锚由 _ensure_limb_guarantee
    # 收尾，负面词不得破坏锚位——批次 1 §31.8）
    prompt_text = _ensure_limb_guarantee(
        _join_parts(core, _style_negative(db, char.project_id, "character"),
                    prompts_mod.CHARACTER_FOURVIEW_PROMPT),
        prompts_mod.CHARACTER_FOURVIEW_LIMB_REINFORCE)

    if image is None:
        image = CharacterImage(character_id=char.id,
                               name=f"{char.name}·角色设定图",
                               kind="fourview", appearance_desc=prompt_text)
        db.add(image)
        _created = True
    else:
        # 记录里的原文同步为本次组装结果（它是这张记录、以及后续抽卡的提示词快照）
        image.appearance_desc = prompt_text
    db.commit()  # 先落记录再提交任务，任务完成后列表接口可查

    node_id = f"char_{char.id}_{image.id}"
    try:
        submitted = engine.submit(
            project_id=char.project_id, node_id=node_id, runner_name="t2i",
            params={"prompt": _styled_prompt(keywords, prompt_text),
                    "aspect": CHARACTER_FOURVIEW_ASPECT,
                    # 四视图一行四列含四个人物，负向词不可带"多个人物"
                    "negative_prompt": prompts_mod.CHARACTER_FOURVIEW_NEGATIVE_PROMPT},
            model_id=_resolve_model(db, char.project_id, KIND_T2I, model_id),
            input_refs={"image": ref_artifact} if ref_artifact else {},
        )
    except Exception as e:  # noqa: BLE001
        # 只回滚**本次新建**的空记录（复用已有记录时绝不能删——那会连带删掉
        # 它的历史版本，把用户的设定图历史一起毁掉）
        if _created:
            db.delete(image)
            db.commit()
        raise HTTPException(400, str(e)) from e
    task = db.get(Task, submitted["task_id"])
    return {"task_id": submitted["task_id"],
            "version_id": task.version_id if task else None,
            "image_id": image.id, "node_id": node_id}


@router.post("/characters/{character_id}/images/fourview", status_code=201)
def create_fourview_images(character_id: str, body: FourViewIn,
                           db: Session = Depends(get_db)):
    """生成角色三视图（四视图）：单张**横版**图内**一行四列**（正面/左45°/背面/右45°）。

    以角色基础形象为参考图（无基础形象时拒绝，保证四格与基础形象为同一人物）。
    它同时是**该角色的基础形象**：后续镜头参考图、就绪度检查都认它
    （用户 2026-09-30 决策：不再裁切成单角度图——裁剪 + 角度映射那套太复杂、
    容易出错，整张作为参考图使用）。
    实现在 `submit_character_fourview`（与自动补齐共用）。
    """
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    if not char.project_id:
        raise HTTPException(400, "全局角色不支持生成三视图")
    return submit_character_fourview(db, char, body.model_id,
                                     body.regenerate_image_id)


@router.post("/characters/{character_id}/images/cancel", status_code=200)
def cancel_character_images(character_id: str, db: Session = Depends(get_db)):
    """取消该角色全部进行中的定妆图生成任务。"""
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    canceled = _cancel_active_tasks(db, char.project_id, f"char_{character_id}_")
    return {"ok": True, "canceled": canceled}


@router.delete("/characters/{character_id}/images/{image_id}")
def delete_character_image(character_id: str, image_id: str,
                           db: Session = Depends(get_db)):
    """删除角色形象（定妆图/妆造/四视图）：删除形象记录与其节点版本。

    生成中的任务先取消；分镜妆造映射对该形象的引用一并清理（回退基础形象）；
    图片产物 Asset 保留（资产库三方联动，可从资产库找回）。
    """
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    image = db.get(CharacterImage, image_id)
    if not image or image.character_id != character_id:
        raise HTTPException(404, "形象不存在或不属于该角色")
    # 取消该形象的进行中任务（避免删除后任务完成回写孤儿版本）
    _cancel_active_tasks(db, char.project_id, f"char_{character_id}_{image_id}")
    # 清理分镜妆造映射：映射指向被删形象时移除（生成时回退基础形象）
    shots = db.scalars(select(Shot).where(
        Shot.episode_id.in_(
            select(Episode.id).where(Episode.project_id == char.project_id))
    )).all()
    for shot in shots:
        mapping = dict(shot.costume_map_json or {})
        if mapping.get(character_id) == image_id:
            del mapping[character_id]
            shot.costume_map_json = mapping
    # 删除形象节点的版本记录（产物 Asset 保留，可从资产库找回）
    db.execute(delete(NodeVersion).where(
        NodeVersion.node_id == f"char_{character_id}_{image_id}",
        NodeVersion.project_id == char.project_id))
    db.delete(image)
    db.commit()
    return {"ok": True}


@router.delete("/characters/{character_id}")
def delete_character(character_id: str, confirm: bool = False,
                     db: Session = Depends(get_db)):
    """删除角色资产（两段式：先预检映射情况，确认后删除并清除全部映射）。

    - confirm=false（默认）：仅统计映射情况不删除——分镜引用数/涉及集数/
      妆造映射数/形象作用域绑定数，供前端确认弹窗展示；
    - confirm=true：取消该角色全部进行中生成任务，删除角色、形象记录、
      节点版本与形象作用域绑定，清除分镜的角色引用与妆造映射；
      图片产物 Asset 保留（资产库可找回）。
    """
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    shots, ep_seqs = _asset_shot_refs(db, char.project_id)
    ref_shots = [s for s in shots
                 if character_id in (s.character_ref_ids_json or [])]
    map_shots = [s for s in shots
                 if character_id in (s.costume_map_json or {})]
    link_count = db.scalar(
        select(func.count()).select_from(CharacterAssetLink).where(
            CharacterAssetLink.project_id == char.project_id,
            CharacterAssetLink.character_id == character_id)) or 0
    references = {
        "shots": len(ref_shots),
        "episodes": _ref_episode_seqs(ref_shots, ep_seqs),
        "costume_maps": len(map_shots),
        "asset_links": int(link_count),
    }
    if not confirm:
        return {"ok": True, "deleted": False, "name": char.name,
                "references": references}

    # 确认删除：取消任务（形象节点）→ 清分镜映射 → 删记录（产物 Asset 保留）
    _cancel_active_tasks(db, char.project_id, f"char_{character_id}_")
    for s in ref_shots:
        s.character_ref_ids_json = [c for c in (s.character_ref_ids_json or [])
                                    if c != character_id]
    for s in map_shots:
        mapping = dict(s.costume_map_json or {})
        mapping.pop(character_id, None)
        s.costume_map_json = mapping
    db.execute(delete(NodeVersion).where(
        NodeVersion.node_id.like(f"char_{character_id}_%"),
        NodeVersion.project_id == char.project_id))
    db.execute(delete(CharacterImage).where(
        CharacterImage.character_id == character_id))
    db.execute(delete(CharacterAssetLink).where(
        CharacterAssetLink.project_id == char.project_id,
        CharacterAssetLink.character_id == character_id))
    db.delete(char)
    db.commit()
    return {"ok": True, "deleted": True, "cleared": references}


@router.post("/characters/{character_id}/voice-ref", status_code=201)
async def upload_voice_ref(character_id: str, file: UploadFile = File(...),
                           db: Session = Depends(get_db)):
    """上传角色音色参考音频（mp3/wav/m4a/aac/ogg/flac，≤20MB）。

    文件落 artifacts 目录并登记 Asset(kind=audio)，id 记到角色
    voice_ref_asset_id，后续生成视频时作为该角色配音音色参考；
    重复上传覆盖旧参考。
    """
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in VOICE_REF_EXTENSIONS:
        supported = " / ".join(VOICE_REF_EXTENSIONS)
        raise HTTPException(400, f"不支持的音频格式：{ext or '未知'}（支持：{supported}）")
    # 带上限的分块读取（2026-09-30 安全整改）：上限在读取过程中就生效
    data = await read_upload_capped(
        file, VOICE_REF_MAX_BYTES,
        too_large_message=("音频文件过大（上限 "
                           f"{VOICE_REF_MAX_BYTES // _BYTES_PER_MB} MB）"))
    if not data:
        raise HTTPException(400, "音频文件为空")
    try:
        settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir_for(char.project_id) / \
            f"voice_ref_{character_id}_{uuid.uuid4().hex[:8]}{ext}"
        path.write_bytes(data)
    except OSError as e:
        raise HTTPException(500, f"音频文件保存失败：{e}") from e
    finally:
        await file.close()
    asset = Asset(kind="audio", path=str(path),
                  meta_json={"kind": "voice_ref", "character_id": character_id,
                             "filename": filename})
    db.add(asset)
    char.voice_ref_asset_id = asset.id
    db.commit()
    return {"ok": True, "asset_id": asset.id}


@router.delete("/characters/{character_id}/voice-ref")
def delete_voice_ref(character_id: str, db: Session = Depends(get_db)):
    """移除角色音色参考音频（仅清除引用，已上传的音频文件保留）。"""
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    char.voice_ref_asset_id = None
    db.commit()
    return {"ok": True}


def _next_version_seq(db: Session, node_id: str, project_id: str) -> int:
    """节点当前最大版本号 +1（与 nodes.upload_version 的 seq 规则一致）。"""
    last = db.scalars(
        select(NodeVersion.seq)
        .where(NodeVersion.node_id == node_id, NodeVersion.project_id == project_id)
        .order_by(NodeVersion.seq.desc())
        .limit(1)
    ).first()
    return (last or 0) + 1


def _save_upload_image_asset(db: Session, character_id: str,
                             filename: str, data: bytes) -> Asset:
    """校验并保存上传图片为产物 Asset（仅图片格式，落 artifacts 目录）。"""
    ext = Path(filename).suffix.lower()
    if ext not in IMAGE_UPLOAD_EXTENSIONS:
        supported = " / ".join(IMAGE_UPLOAD_EXTENSIONS)
        raise HTTPException(400, f"不支持的图片格式：{ext or '未知'}（支持：{supported}）")
    if len(data) > IMAGE_UPLOAD_MAX_BYTES:
        raise HTTPException(
            400, f"图片文件过大（上限 {IMAGE_UPLOAD_MAX_BYTES // _BYTES_PER_MB} MB）")
    try:
        _up_char = db.get(Character, character_id)
        path = artifact_dir_for(_up_char.project_id if _up_char else "") / \
            f"char_img_{character_id}_{uuid.uuid4().hex[:8]}{ext}"
        path.write_bytes(data)
    except OSError as e:
        raise HTTPException(500, f"图片文件保存失败：{e}") from e
    return Asset(kind="image", path=str(path),
                 meta_json={"kind": "character_image", "character_id": character_id,
                            "filename": filename, "size": len(data)})


@router.post("/characters/{character_id}/images/upload", status_code=201)
async def upload_character_image(character_id: str, file: UploadFile = File(...),
                                 db: Session = Depends(get_db)):
    """上传本地图片作为角色基础形象（新建角色/无形象角色的补图入口）。

    与生成定妆图同语义但不提交任务：落 CharacterImage(kind=basic) 记录 +
    uploaded 版本（node_id=char_{cid}_{imgId}），卡片展示与妆造/四视图的
    参考图解析自动跟随「生效版本优先，否则最新」。
    """
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    if not char.project_id:
        raise HTTPException(400, "全局角色不支持上传形象图")
    filename = file.filename or ""
    # 带上限的分块读取（2026-09-30 安全整改）：上限在读取过程中就生效
    data = await read_upload_capped(
        file, IMAGE_UPLOAD_MAX_BYTES,
        too_large_message=("图片文件过大（上限 "
                           f"{IMAGE_UPLOAD_MAX_BYTES // _BYTES_PER_MB} MB）"))
    if not data:
        raise HTTPException(400, "图片文件为空")
    asset = _save_upload_image_asset(db, character_id, filename, data)
    db.add(asset)
    # 上传的形象图 = 该角色的基础图（角色设定图这一种），与生成路径同一种记录
    image = CharacterImage(character_id=character_id, name="角色设定图",
                           kind="fourview", appearance_desc="")
    db.add(image)
    db.flush()  # 先取形象 id 再挂版本
    node_id = f"char_{character_id}_{image.id}"
    version = NodeVersion(
        node_id=node_id, project_id=char.project_id,
        seq=_next_version_seq(db, node_id, char.project_id),
        source="uploaded", prompt="", refs_json=[], artifact_id=asset.id,
    )
    db.add(version)
    db.commit()
    return {"ok": True, "image_id": image.id, "artifact_id": asset.id,
            "version_id": version.id}


# ---------- 场景 ----------
@router.get("/projects/{project_id}/scenes")
def list_scenes(project_id: str, db: Session = Depends(get_db)):
    _project(db, project_id)
    rows = db.scalars(
        select(Scene).where(Scene.project_id == project_id)
        .order_by(Scene.created_at, Scene.id)
    ).all()
    return [
        {**_scene_dict(db, project_id, s),
         "generating": _node_generating(db, project_id, f"scene_{s.id}")}
        for s in _apply_order_seq(rows, lambda x: (x.created_at, x.id))
    ]


@router.get("/projects/{project_id}/props")
def list_props(project_id: str, db: Session = Depends(get_db)):
    """道具列表（故事板资产栏/提及候选数据源，与场景列表同模式）。"""
    _project(db, project_id)
    rows = db.scalars(
        select(Prop).where(Prop.project_id == project_id)
        .order_by(Prop.created_at, Prop.id)
    ).all()
    return [
        {**_prop_dict(db, project_id, p),
         "generating": _node_generating(db, project_id, f"prop_{p.id}")}
        for p in _apply_order_seq(rows, lambda x: (x.created_at, x.id))
    ]


@router.post("/projects/{project_id}/scenes", status_code=201)
def create_scene(project_id: str, body: SceneIn,
                 db: Session = Depends(get_db)):
    """手工新建场景（解析之外补充资产，与角色手工新建同模式）。"""
    _project(db, project_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "场景名不能为空")
    row = Scene(project_id=project_id, name=name,
                description=body.description.strip(),
                image_prompt=body.image_prompt.strip())
    db.add(row)
    db.commit()
    out = _scene_dict(db, project_id, row)
    out["generating"] = False
    return out


@router.patch("/scenes/{scene_id}")
def patch_scene(scene_id: str, body: ScenePatchIn, db: Session = Depends(get_db)):
    row = db.get(Scene, scene_id)
    if not row:
        raise HTTPException(404, "场景不存在")
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.image_prompt is not None:
        row.image_prompt = body.image_prompt
        # 保存即重组装（2026-09-30 用户规则）：框里始终是完整提示词
        compose_scene_prompt(db, row)
    if body.group_name is not None:
        # 场景组（32 号）：并入/新建/移出后重组装（组环境设定与说明句要跟上）
        _set_scene_group(db, row, body.group_name)
        compose_scene_prompt(db, row)
    db.commit()
    return {"ok": True}


def _set_scene_group(db: Session, scene, group_name: str) -> None:
    """设置/移出场景组（界面入口）：`""` = 移出；非空 = **同名并入或新建**（确定性）。"""
    name = (group_name or "").strip()
    if not name:
        scene.group_id = None
        return
    group = db.scalars(select(SceneGroup).where(
        SceneGroup.project_id == scene.project_id,
        SceneGroup.name == name)).first()
    if group is None:
        group = SceneGroup(project_id=scene.project_id, name=name)
        db.add(group)
        db.flush()
    scene.group_id = group.id


@router.post("/scenes/{scene_id}/generate")
def generate_scene(scene_id: str, body: SceneGenerateIn,
                   db: Session = Depends(get_db)):
    """生成场景主图：优先 LLM 生成的 image_prompt，其次场景描述。

    按项目画幅比例出图（9:16 项目的场景图若按 16:9 生成，构图与成片不一致）。
    model_id 可指定 t2i 模型（空 = 默认模型）。
    """
    scene = db.get(Scene, scene_id)
    if not scene:
        raise HTTPException(404, "场景不存在")
    project = db.get(Project, scene.project_id)
    aspect = (getattr(project, "aspect_ratio", None) or "16:9") if project else "16:9"
    # **生成阶段组装 + 写回提示词框**（2026-09-30 用户规则：界面所见即所发）：
    # 画风前缀 → 空镜约束 → 空间布局（哪个方向有门/窗的空间法典）→ 画面描述 →
    # 风格负向词（场景专用那套）。调用接口时不再暗地追加任何文本。
    prompt = compose_scene_prompt(db, scene)
    db.commit()   # 写回先落库：前端提示词框随即显示本次实际发送的完整文本
    # 环境母版参考图（32 号场景组）：同组已有产物时带上作参考图——「只继承环境、
    # 不继承构图与陈设」的说明句已由 compose 按母版有无写进提示词（所见即所发）
    _anchor = _scene_group_anchor(db, scene)
    _refs = {"extra_images": [_anchor]} if _anchor else {}
    try:
        submitted = engine.submit(
            project_id=scene.project_id, node_id=f"scene_{scene_id}",
            runner_name="t2i",
            params={"prompt": prompt, "aspect": aspect,
                    # 一次抽 N 张（同首帧语义）：每张各落一个版本，用户在版本条上
                    # 挑一张「设为生效」，省掉「点一次看一张」的往返
                    **({"count": _n} if (_n := _candidate_count(body.count)) > 1 else {})},
            input_refs=_refs,
            model_id=_resolve_model(db, scene.project_id, KIND_T2I, body.model_id),
            # 场景图无人物语义：关闭国家外貌约束注入（「东亚面孔/五官」拼在
            # prompt 开头会诱导生成模型输出人脸特写——场景人脸事故的混杂源）
            inject_country_hint=False,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    # 回传组装结果：前端把提示词框同步成这条（写回 + 立即可见）
    return {"task_id": submitted["task_id"], "prompt": prompt,
            "group_anchor_artifact": _anchor}


class SceneGroupPatchIn(BaseModel):
    """场景组编辑入参（32 号）：缺省 = 不改；anchor_asset_id 空串 = 回「自动取首张」。"""
    name: str | None = None
    env_setting: str | None = None
    anchor_asset_id: str | None = None


@router.get("/scenes/{scene_id}/scene-group")
def get_scene_group(scene_id: str, db: Session = Depends(get_db)):
    """场景组详情（界面「场景组」弹窗）：当前组 + 同组成员（各带生效产物，供选母版）。"""
    scene = db.get(Scene, scene_id)
    if not scene:
        raise HTTPException(404, "场景不存在")
    group = db.get(SceneGroup, scene.group_id) if getattr(scene, "group_id", "") else None
    if group is None:
        return {"ok": True, "group": None, "members": []}
    rows = db.scalars(select(Scene).where(Scene.group_id == group.id).order_by(
        Scene.created_at, Scene.id)).all()
    members = [{"scene_id": m.id, "name": m.name,
                "artifact_id": _scene_artifact(db, m) or ""} for m in rows]
    return {"ok": True,
            "group": {"id": group.id, "name": group.name,
                      "env_setting": group.env_setting or "",
                      "anchor_asset_id": group.anchor_asset_id or "",
                      # 当前生效母版（显式指定 ?? 组内首个有产物的成员；空串 = 自动）
                      "anchor_artifact": _scene_group_anchor(db, scene)},
            "members": members}


@router.patch("/scene-groups/{group_id}")
def patch_scene_group(group_id: str, body: SceneGroupPatchIn,
                      db: Session = Depends(get_db)):
    """编辑场景组：改名 / 组环境设定 / 指定环境母版图（32 号）。

    改完对**组内全部场景**重组装提示词（组环境设定与「只继承环境」说明句都要跟上，
    「所见即所发」不破）；母版必须来自本组成员自己的产物（防挂外部/别人的图）。
    """
    group = db.get(SceneGroup, group_id)
    if group is None:
        raise HTTPException(404, "场景组不存在")
    if body.name is not None and body.name.strip():
        group.name = body.name.strip()[:200]
    if body.env_setting is not None:
        group.env_setting = body.env_setting.strip()
    if body.anchor_asset_id is not None:
        aid = body.anchor_asset_id.strip()
        if aid:
            member_ids = db.scalars(select(Scene.id).where(
                Scene.group_id == group.id)).all()
            belongs = bool(member_ids) and bool(db.scalar(
                select(func.count()).select_from(NodeVersion).where(
                    NodeVersion.node_id.in_(
                        [f"scene_{i}" for i in member_ids]),
                    NodeVersion.artifact_id == aid)))
            if not belongs:
                raise HTTPException(400, "环境母版必须取自本组成员已有的场景图")
        group.anchor_asset_id = aid or None
    for member in db.scalars(select(Scene).where(
            Scene.group_id == group.id)).all():
        compose_scene_prompt(db, member)
    db.commit()
    return {"ok": True}


class SceneGroupApplyIn(BaseModel):
    """多选式编组入参（32 号交互优化）：一次提交「这批场景的组归属」期望状态。"""

    project_id: str
    scene_ids: list[str] = []
    name: str = ""                       # "" = 把勾选场景移出各自所在的组
    env_setting: str = ""
    anchor_asset_id: str | None = None   # None = 不改；"" = 回「自动」；id = 指定母版


def _delete_empty_scene_groups(db: Session, project_id: str) -> None:
    """清掉项目下已无成员的场景组（组靠成员场景卡承载、没有独立入口，空组 = 垃圾）。

    必须先 flush：会话是 autoflush=False，库里查不到本事务里**还没落库**的成员
    关系——不 flush 会把「刚挂好成员的新组」误判为空组删掉（门禁当场抓到过：
    场景 group_id 指向了已删除的组）。
    """
    db.flush()
    for grp in db.scalars(select(SceneGroup).where(
            SceneGroup.project_id == project_id)).all():
        has_member = db.scalar(select(func.count()).select_from(Scene).where(
            Scene.group_id == grp.id))
        if not has_member:
            db.delete(grp)


@router.post("/scene-groups/apply")
def apply_scene_group(body: SceneGroupApplyIn, request: Request,
                      db: Session = Depends(get_db)):
    """多选式编组（32 号交互优化）：勾一批场景、一次保存（原子，提交期望状态）。

    为什么要有它：旧交互是「逐张场景卡开弹窗、输同名组名」，5 个场景要点 5 次；
    这里一次把「谁和谁一组」提交完。

    - `name` 非空：`scene_ids` 全部归入名为 name 的组（不存在则新建；同名并入），
      组里原成员凡不在 `scene_ids` 的移出；`env_setting` 非空才覆盖（空串保留旧值，
      防「盲输已有组名」把设定抹掉）；`anchor_asset_id` 三态（None 不改 / 空串回
      自动 / id 指定，须为本组成员已有产物）。
    - `name` 为空：`scene_ids` 移出各自所在的组。
    - 事后清掉空组，并对全部受影响场景重组装提示词（组环境设定与「只继承环境」
      说明句跟上，「所见即所发」不破）。
    """
    access.ensure_project_access(db, body.project_id, request, need="write")
    # 请求体里的编号中间件看不到（三层守卫只认路径/查询参数），逐个手查
    # 归属：必须属于本项目且归调用者，越权一律 404
    scenes: list[Scene] = []
    seen: set[str] = set()
    for sid in body.scene_ids:
        if sid in seen:
            continue
        seen.add(sid)
        sc = db.get(Scene, sid)
        if sc is None or sc.project_id != body.project_id:
            raise HTTPException(404, "场景不存在")
        access.ensure_resource_project_access(db, sc, request, what="场景")
        scenes.append(sc)

    name = (body.name or "").strip()
    if not name and not scenes:
        return {"ok": True, "removed": 0}   # 没勾场景也没给组名：无事发生
    touched: list[Scene] = []
    if not name:
        for sc in scenes:
            if sc.group_id:
                sc.group_id = None
                touched.append(sc)
    else:
        if not scenes:
            raise HTTPException(400, "请至少勾选一个场景")
        group = db.scalars(select(SceneGroup).where(
            SceneGroup.project_id == body.project_id,
            SceneGroup.name == name)).first()
        if group is None:
            group = SceneGroup(project_id=body.project_id, name=name)
            db.add(group)
            db.flush()
        env = (body.env_setting or "").strip()
        if env:
            group.env_setting = env[:500]   # 覆盖为用户框里所见（空串保留旧值）
        # 母版三态；指定时必须是**结果成员集**里某场景的已有产物（防挂外部图）
        if body.anchor_asset_id is not None:
            aid = body.anchor_asset_id.strip()
            if aid:
                belongs = bool(db.scalar(
                    select(func.count()).select_from(NodeVersion).where(
                        NodeVersion.node_id.in_(
                            [f"scene_{sc.id}" for sc in scenes]),
                        NodeVersion.artifact_id == aid)))
                if not belongs:
                    raise HTTPException(400, "环境母版必须取自本组成员已有的场景图")
            group.anchor_asset_id = aid or None
        want_ids = {sc.id for sc in scenes}
        for member in db.scalars(select(Scene).where(
                Scene.group_id == group.id)).all():
            if member.id not in want_ids:   # 原成员不在勾选里 = 移出
                member.group_id = None
                touched.append(member)
        for sc in scenes:
            sc.group_id = group.id
            touched.append(sc)
    # flush 在 compose 之前：组装里的母版解析要按**改动后**的成员关系查库
    # （autoflush=False，不 flush 会读旧成员表——同上方空组清理同一个坑）
    db.flush()
    for sc in touched:
        compose_scene_prompt(db, sc)
    _delete_empty_scene_groups(db, body.project_id)
    db.commit()
    return {"ok": True, "removed": sum(1 for sc in touched if not sc.group_id)}


# 一次抽卡张数上限（**唯一来源**：首帧、场景主图共用；runner 侧同值截断见
# runners/generate.py 的 count 解析，前端 CANDIDATE_COUNTS 与之同源）。
# 为什么要有抽卡：资产图/首帧这类"好不好看"没有确定答案的产物，一次生成多张、
# 用户挑一张，比反复点「重新生成」省时省钱（每次点击都是一次完整调用）。
T2I_MAX_CANDIDATES = 4


def _candidate_count(raw) -> int:
    """抽卡张数归一：钳到 [1, T2I_MAX_CANDIDATES]（纯算术，不许越界下单）。"""
    try:
        return max(1, min(T2I_MAX_CANDIDATES, int(raw or 1)))
    except (TypeError, ValueError):
        return 1


# 台词密集镜阈值（20 号 §4.4 音频解耦）：本镜台词达到该句数即提示「可音频先行」
DENSE_DIALOGUE_SHOTS = 2

# 场景补充视角（20 号批次 4 · B4）：反向视角 / 关键交互区特写——反打镜头与
# 关键动作镜的空间一致性靠这两张图锚定。**不做满官方 6 视图**：成本高，
# 左/右视角与中景可由主图 + 镜头机位声明覆盖，实际有需求的就是这两种。
SCENE_EXTRA_VIEWS: tuple[str, ...] = ("reverse", "interaction")
SCENE_EXTRA_VIEW_LABELS = {"reverse": "反向视角", "interaction": "关键交互区特写"}
# 视角提示（正向描述该视角要什么，不写「不要什么」）
SCENE_EXTRA_VIEW_HINTS = {
    "reverse": ("反向视角：从场景主图机位的正对面回看整个空间，"
                "空间结构、出入口位置、固定陈设方位与光源方向与主图完全一致；"),
    "interaction": ("关键交互区特写：聚焦场景中最常发生剧情交互的区域与核心陈设，"
                    "材质、色温、光线方向与主图一致；"),
}


# 视角重写的**逐字重合度**红线：重写结果与主图描述的最长公共子串 ÷ 较短一侧长度
# 达到该值 = 模型把主图描述照抄了回来（旧实现是整段照抄，重合度≈1.0）。这是纯字符串
# 计算，属**确定性对账**——判「有没有真的重写」不靠感觉、也不靠关键词猜。
SCENE_VIEW_COPY_MAX = 0.5
# 视角提示词字数下限：预算被固定段吃满时按此值给重写留额度（避免压成一句话）
SCENE_VIEW_MIN_CHARS = 120
# 「整句照抄」判据：重写里任何一句（按。；；\n 切）与主图描述里的某句**逐字相同**
# 且不短于该字数 → 判为照抄主图那一侧的构图句。这条比上面的整体重合度更准：
# 实测（2026-09-29 看图）重写整体重合度只有 0.19（因为总长短），却整句搬走了
# 「城墙横向展开，占据画面下方三分之二」——出图仍是主图那个机位。
SCENE_VIEW_COPY_SENTENCE_MIN = 12


def _copied_sentences(view_text: str, main_text: str,
                      min_len: int = SCENE_VIEW_COPY_SENTENCE_MIN) -> list[str]:
    """重写里**整句照抄**了主图描述的句子（逐字相同且 ≥ min_len 字），返回这些句子。

    确定性对账：按句读切分后做集合比对，不做任何近似匹配（近义改写**不**算照抄，
    那是允许的；逐字搬运主图那一侧的构图句才是要拦的）。
    """
    main_sents = {s.strip() for s in re.split(r"[。；;\n]", main_text or "")
                  if len(s.strip()) >= min_len}
    if not main_sents:
        return []
    out: list[str] = []
    for s in re.split(r"[。；;\n]", view_text or ""):
        t = s.strip()
        if len(t) >= min_len and t in main_sents:
            out.append(t)
    return out


class SceneExtraViewIn(BaseModel):
    """场景补充视角入参：view = reverse（反向视角）| interaction（关键交互区特写）。"""
    view: str
    model_id: str = ""


def _view_copy_ratio(view_text: str, main_text: str) -> float:
    """视角提示词与主图描述的逐字重合度（最长公共子串 ÷ 较短一侧长度，0-1）。

    确定性计算（difflib）：整段照抄时≈1.0，真重写时只剩零散共用短语。
    只回答「这两段文字有多少字逐字一样」，不做任何语义判断。
    """
    import difflib
    a = (view_text or "").strip()
    b = (main_text or "").strip()
    if not a or not b:
        return 0.0
    match = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b))
    return match.size / min(len(a), len(b))


def _copied_view_sentences(view_text: str, main_text: str,
                           own_text: str = "") -> list[str]:
    """整句照抄主图描述、**且不是我们自己注入的样板**的句子。

    own_text = 画风 + 空镜约束 + 负向词（系统注入的那几段）：这些句子本来就会
    出现在主图描述里（主题词块在 image_prompt 开头），模型复述它们无害，不该判成
    「照抄主图那一侧的构图句」——实测模型确实会把画风句原样带回来，不豁免的话
    用户会收到一个莫名其妙的「视角重写没生效」。
    """
    return [s for s in _copied_sentences(view_text, main_text)
            if s not in (own_text or "")]


def _rewrite_scene_view_prompt(db: Session, scene: Scene, view: str,
                               main_text: str, layout: str) -> str:
    """为补充视角**重写**该视角的文字（LLM），失败直接报错、**不降级出图**。

    为什么不复用主图描述：见 `prompts.SCENE_VIEW_REWRITE_SYSTEM_PROMPT` 注释——
    主图那一侧的构图句被逐字带入时，模型只会画出与主图同视角的图（实测连抽 3 版
    都不对）。为什么失败要拦：降级就等于把已知会画错的提示词再送一次，白花一次
    生图钱与用户时间。

    字数预算按端点取（agnes ≤600 / 方舟 Seedream ≤300），先扣掉固定段（画风前缀
    另计、空镜约束、视角提示、空间布局、负向词）再给重写留额度——纯算术。
    """
    from app.agent import prompts as _p
    from app.core.providers import prompt_budget as _prompt_budget
    from app.script.llm_parser import ScriptParseError, _llm_call

    model_id = _resolve_model(db, scene.project_id, KIND_T2I, "")
    limit, _hint = _prompt_budget("t2i", model_id)
    layout_part = f"空间布局：{layout}；" if layout else ""
    # 我们自己注入的样板（画风 / 空镜约束 / 负向词）：重写里出现这些句子**不算
    # 照抄主图**（实测：模型会把主图开头的画风句原样带回来，若按照抄判死，
    # 用户会收到一个「视角重写没生效」的报错——把无害的复述当成缺陷拦下来）
    own_text = _join_parts(_style_keywords(db, scene.project_id),
                           prompts_mod.SCENE_IMAGE_CONSTRAINTS,
                           _style_negative(db, scene.project_id, "scene"))
    fixed = len(_join_parts(prompts_mod.SCENE_IMAGE_CONSTRAINTS,
                            SCENE_EXTRA_VIEW_HINTS[view], layout_part,
                            _style_negative(db, scene.project_id, "scene")))
    budget = max(SCENE_VIEW_MIN_CHARS, limit - fixed) if limit else 0
    payload = json.dumps({
        "scene_name": scene.name,
        "view_name": SCENE_EXTRA_VIEW_LABELS.get(view, view),
        "view_requirement": SCENE_EXTRA_VIEW_HINTS[view],
        "layout": layout,
        "main_image_prompt": main_text,
        "budget_chars": budget,
    }, ensure_ascii=False)
    last_ratio = 0.0
    _last_copied: list[str] = []
    for attempt in (1, 2):
        try:
            # 与「重算」同样挂项目 LLM 预设：不挂会悄悄用全局 LLM（风格不一致）
            with use_llm_model(project_preset(
                    db.get(Project, scene.project_id), KIND_LLM)):
                data = _llm_call(_p.SCENE_VIEW_REWRITE_SYSTEM_PROMPT, payload)
        except ScriptParseError as e:
            raise HTTPException(502, f"视角重写失败（LLM 调用出错）：{e}") from e
        text = str(data.get("view_prompt") or "").strip()
        if not text:
            continue
        last_ratio = _view_copy_ratio(text, main_text)
        # 逐句比对时**剔除我们自己注入的样板句**（画风/空镜约束/负向词）
        _copied = _copied_view_sentences(text, main_text, own_text)
        if last_ratio < SCENE_VIEW_COPY_MAX and not _copied:
            logger.info("场景补充视角重写：%s/%s 与主图描述重合度 %.2f、无整句照抄",
                        scene.name, view, last_ratio)
            return text
        if _copied:
            logger.warning("场景补充视角重写第%s次整句照抄了主图描述：%s",
                           attempt, "；".join(_copied[:3]))
        else:
            logger.warning("场景补充视角重写第%s次仍在照抄主图描述（重合度 %.2f）：%s",
                           attempt, last_ratio, text[:80])
        _last_copied = _copied
    raise HTTPException(
        400, "视角重写没生效：模型连续两次把主图描述照抄了回来，本次**没有生成**"
             + (f"（整句照抄：「{_last_copied[0]}」）" if _last_copied else
                f"（重合度 {last_ratio:.2f}）")
             + "——生成出来还是主图那个视角，白花钱。请重试；若反复如此，"
               "先把该场景的主图提示词写得更具体（资产页场景卡「编辑」）再试。")


@router.post("/scenes/{scene_id}/extra-view")
def generate_scene_extra_view(scene_id: str, body: SceneExtraViewIn,
                              db: Session = Depends(get_db)):
    """生成场景补充视角：**以场景主图为 I2I 参考**（官方口径：全景氛围图是该场景的
    空间母版，后续视角优先用母版补全——同源补全才能保证空间逻辑不漂移）。

    无主图时**明确报错**，不降级为 T2I（丢空间约束等于换一个场景）。
    产物挂在独立节点 `scene_{id}_view_{view}`，不覆盖主图版本。

    **本视角的文字由 LLM 重写**（2026-09-29）：此前把主图描述原文整段抄进本视角
    提示词，主图那一侧的构图句（「固定机位/城墙横向展开/前景为城垛特写」）与
    「换到对面」直接打架，实测连抽 3 版都还是主图那个视角。现在只把主图描述作为
    **重写输入**，进提示词的是「该视角可见什么」；重写失败（照抄/LLM 报错）**直接
    报错不出图**，不做静默降级。
    """
    scene = db.get(Scene, scene_id)
    if not scene:
        raise HTTPException(404, "场景不存在")
    view = (body.view or "").strip()
    if view not in SCENE_EXTRA_VIEWS:
        raise HTTPException(
            400, f"未知视角：{view}（可选：{'、'.join(SCENE_EXTRA_VIEWS)}）")
    main = _latest_artifact(db, scene.project_id, f"scene_{scene_id}")
    if not main:
        raise HTTPException(400, "请先生成场景主图——补充视角以主图为参考图，"
                                 "无主图时生成的空间与主图不一致")
    project = db.get(Project, scene.project_id)
    aspect = (getattr(project, "aspect_ratio", None) or "16:9") if project else "16:9"
    keywords = _style_keywords(db, scene.project_id)
    # 主图描述取**剥净系统块**的本体（写回后 image_prompt 里含空镜约束/负向词，
    # 直接喂给重写 LLM 会把系统样板句带进来）
    main_text = prompts_mod.strip_scene_figures(
        _scene_base_text(db, scene)
        or (scene.description or "").strip() or scene.name)
    # 布局取**清洗版**（剔人物走位句）：原样拼原始布局会把「妈妈可站在布帘后
    # 探头看向沙发」直接送进本视角提示词（2026-10-02 实测泄漏），补充视角
    # 和主图一样必须是纯环境
    layout = _clean_scene_layout(db, scene)
    layout_part = f"空间布局：{layout}；" if layout else ""
    # 本视角的文字**由 LLM 重写**（只写这一侧看得见什么）：主图描述原文不再进本
    # 视角提示词（主图仍作为参考图提供，负责空间一致性），否则主图那一侧的构图句
    # 会把模型按回同一个视角——见 `_rewrite_scene_view_prompt` 注释与实测。
    view_prompt = _rewrite_scene_view_prompt(db, scene, view, main_text, layout)
    prompt = _ensure_scene_guarantee(_styled_prompt(keywords, _join_parts(
        prompts_mod.SCENE_IMAGE_CONSTRAINTS, SCENE_EXTRA_VIEW_HINTS[view], layout_part,
        view_prompt, _style_negative(db, scene.project_id, "scene"))))
    try:
        submitted = engine.submit(
            project_id=scene.project_id,
            node_id=f"scene_{scene_id}_view_{view}",
            runner_name="t2i",
            # view_prompt 单列一份：它是 LLM 重写出来的**本视角画面内容**，用户
            # 在「查看参数」里能直接看到系统这次到底让它画什么（系统改写必须外露）
            params={"prompt": prompt, "aspect": aspect, "view_prompt": view_prompt},
            input_refs={"extra_images": [main]},
            model_id=_resolve_model(db, scene.project_id, KIND_T2I, body.model_id),
            inject_country_hint=False,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    return {"task_id": submitted["task_id"], "view": view}


@router.post("/props/{prop_id}/generate")
def generate_prop(prop_id: str, body: SceneGenerateIn,
                  db: Session = Depends(get_db)):
    """生成道具定妆图：白底物件图（外观一致性锚点，跨镜头参考）。

    model_id 可指定 t2i 模型（空 = 默认模型）。
    """
    prop = db.get(Prop, prop_id)
    if not prop:
        raise HTTPException(404, "道具不存在")
    # 生成阶段组装 + 写回提示词框（2026-09-30 用户规则）：画风前缀 → 画面描述 →
    # 风格负向词（道具专用那套，不含皮肤/五官类人物词）。
    prompt = compose_prop_prompt(db, prop)
    db.commit()
    try:
        submitted = engine.submit(
            project_id=prop.project_id, node_id=f"prop_{prop_id}",
            runner_name="t2i", params={"prompt": prompt, "aspect": "1:1"},
            model_id=_resolve_model(db, prop.project_id, KIND_T2I, body.model_id),
            # 道具图无人物语义：关闭国家外貌约束注入
            inject_country_hint=False,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    return {"task_id": submitted["task_id"], "prompt": prompt}


class PropPatchIn(BaseModel):
    """道具信息编辑入参（与场景 PATCH 同模式）。"""
    name: str | None = None
    description: str | None = None
    image_prompt: str | None = None


@router.patch("/props/{prop_id}")
def patch_prop(prop_id: str, body: PropPatchIn, db: Session = Depends(get_db)):
    """编辑道具名称/描述/生图提示词（解析后可手动修正，重新生成时生效）。"""
    row = db.get(Prop, prop_id)
    if not row:
        raise HTTPException(404, "道具不存在")
    if body.name is not None and body.name.strip():
        row.name = body.name.strip()
    if body.description is not None:
        row.description = body.description
    if body.image_prompt is not None:
        row.image_prompt = body.image_prompt
        # 保存即重组装（2026-09-30 用户规则）：框里始终是完整提示词
        compose_prop_prompt(db, row)
    db.commit()
    return {"ok": True}


class PropIn(BaseModel):
    """手工新建道具入参（名称必填，描述/生图提示词可选）。"""
    name: str
    description: str = ""
    image_prompt: str = ""


@router.post("/projects/{project_id}/props", status_code=201)
def create_prop(project_id: str, body: PropIn,
                db: Session = Depends(get_db)):
    """手工新建道具（解析之外补充资产，与场景/角色手工新建同模式）。"""
    _project(db, project_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "道具名不能为空")
    row = Prop(project_id=project_id, name=name,
               description=body.description.strip(),
               image_prompt=body.image_prompt.strip())
    db.add(row)
    db.commit()
    out = _prop_dict(db, project_id, row)
    out["generating"] = False
    return out


class PropPreferIn(BaseModel):
    """道具「设为生效」入参：version_id 为空 = 回退跟随最新版本。"""
    version_id: str | None = None


@router.post("/props/{prop_id}/image/prefer")
def prefer_prop_image(prop_id: str, body: PropPreferIn,
                      db: Session = Depends(get_db)):
    """把指定版本设为道具的生效图（分镜参考图与卡片展示随之切换）。

    version_id 为空表示回退跟随最新版本。版本必须属于该道具节点（prop_{id}）。
    """
    prop = db.get(Prop, prop_id)
    if not prop:
        raise HTTPException(404, "道具不存在")
    vid = (body.version_id or "").strip() or None
    if vid:
        v = db.get(NodeVersion, vid)
        if not v or v.node_id != f"prop_{prop_id}" \
                or v.project_id != prop.project_id:
            raise HTTPException(400, "版本不属于该道具")
        if not v.artifact_id:
            raise HTTPException(400, "该版本没有产物，无法设为生效")
    prop.preferred_version_id = vid
    db.commit()
    return {"ok": True}


@router.post("/props/{prop_id}/cancel")
def cancel_prop(prop_id: str, db: Session = Depends(get_db)):
    """取消该道具进行中的定妆图生成任务（与场景取消同模式）。"""
    prop = db.get(Prop, prop_id)
    if not prop:
        raise HTTPException(404, "道具不存在")
    canceled = _cancel_active_tasks(db, prop.project_id, f"prop_{prop_id}")
    return {"ok": True, "canceled": canceled}


@router.delete("/props/{prop_id}")
def delete_prop(prop_id: str, confirm: bool = False,
                db: Session = Depends(get_db)):
    """删除道具资产（两段式：先预检映射情况，确认后删除并清除全部映射）。

    - confirm=false（默认）：仅统计映射情况不删除——引用该道具的分镜数
      与涉及集数，供前端确认弹窗展示；
    - confirm=true：取消进行中生成任务，删除道具与其节点版本，把引用
      分镜的 prop_refs 中的该道具移除；图片产物 Asset 保留（资产库可找回）。
    """
    prop = db.get(Prop, prop_id)
    if not prop:
        raise HTTPException(404, "道具不存在")
    shots, ep_seqs = _asset_shot_refs(db, prop.project_id)
    ref_shots = [s for s in shots
                 if prop_id in (s.prop_ref_ids_json or [])]
    references = {
        "shots": len(ref_shots),
        "episodes": _ref_episode_seqs(ref_shots, ep_seqs),
    }
    if not confirm:
        return {"ok": True, "deleted": False, "name": prop.name,
                "references": references}

    # 确认删除：取消任务 → 清分镜道具引用 → 删记录（产物 Asset 保留）
    _cancel_active_tasks(db, prop.project_id, f"prop_{prop_id}")
    for s in ref_shots:
        s.prop_ref_ids_json = [p for p in (s.prop_ref_ids_json or [])
                               if p != prop_id]
    db.execute(delete(NodeVersion).where(
        NodeVersion.node_id == f"prop_{prop_id}",
        NodeVersion.project_id == prop.project_id))
    db.delete(prop)
    db.commit()
    return {"ok": True, "deleted": True, "cleared": references}


@router.post("/scenes/{scene_id}/cancel")
def cancel_scene(scene_id: str, db: Session = Depends(get_db)):
    """取消该场景进行中的场景图生成任务。"""
    scene = db.get(Scene, scene_id)
    if not scene:
        raise HTTPException(404, "场景不存在")
    canceled = _cancel_active_tasks(db, scene.project_id, f"scene_{scene_id}")
    return {"ok": True, "canceled": canceled}


@router.delete("/scenes/{scene_id}/image")
def delete_scene_image(scene_id: str, db: Session = Depends(get_db)):
    """删除场景主图：清空场景节点的全部版本产物，场景回到未生成态。

    生成中的任务先取消；图片产物 Asset 保留（资产库可找回）；
    分镜对场景的引用不受影响（引用场景本身而非图片）。
    """
    scene = db.get(Scene, scene_id)
    if not scene:
        raise HTTPException(404, "场景不存在")
    _cancel_active_tasks(db, scene.project_id, f"scene_{scene_id}")
    db.execute(delete(NodeVersion).where(
        NodeVersion.node_id == f"scene_{scene_id}",
        NodeVersion.project_id == scene.project_id))
    scene.artifact_id = None
    scene.preferred_version_id = None
    db.commit()
    return {"ok": True}


@router.delete("/scenes/{scene_id}")
def delete_scene(scene_id: str, confirm: bool = False,
                 db: Session = Depends(get_db)):
    """删除场景资产（两段式：先预检映射情况，确认后删除并清除全部映射）。

    - confirm=false（默认）：仅统计映射情况不删除——引用该场景的分镜数
      与涉及集数，供前端确认弹窗展示；
    - confirm=true：取消进行中生成任务，删除场景与其节点版本，把引用
      分镜的 scene_ref_id 置空；图片产物 Asset 保留（资产库可找回）。
    """
    scene = db.get(Scene, scene_id)
    if not scene:
        raise HTTPException(404, "场景不存在")
    shots, ep_seqs = _asset_shot_refs(db, scene.project_id)
    ref_shots = [s for s in shots if s.scene_ref_id == scene_id]
    references = {
        "shots": len(ref_shots),
        "episodes": _ref_episode_seqs(ref_shots, ep_seqs),
    }
    if not confirm:
        return {"ok": True, "deleted": False, "name": scene.name,
                "references": references}

    # 确认删除：取消任务 → 清分镜引用 → 删记录（产物 Asset 保留）
    _cancel_active_tasks(db, scene.project_id, f"scene_{scene_id}")
    for s in ref_shots:
        s.scene_ref_id = None
    db.execute(delete(NodeVersion).where(
        NodeVersion.node_id == f"scene_{scene_id}",
        NodeVersion.project_id == scene.project_id))
    db.delete(scene)
    db.commit()
    return {"ok": True, "deleted": True, "cleared": references}


# ---------- 妆造建议（解析提取 → 角色卡确认 → 一键生成） ----------
def _load_costume_suggestions(db: Session, project_id: str) -> list[dict]:
    """读取妆造建议清单（Setting 存储；解析后由 runner 写入）。"""
    row = db.get(Setting, costume_suggestions_key(project_id))
    if not row or not row.value:
        return []
    try:
        return json.loads(row.value) or []
    except ValueError:
        return []


def _replace_suggestion(db: Session, project_id: str,
                        suggestion_id: str) -> None:
    """从建议清单移除指定建议（accept / reject 共用）并**记入已处理键**。

    必须显式 commit（审计 13 号：accept/reject 端点的 Setting 写入原先无提交，
    依赖 get_db 只在 finally 里 close()——拒绝后建议仍在（刷新复现）、采纳后
    建议仍在可再次采纳并重复生成妆造形象）。

    记已处理键的原因（接线后新增）：逐集资产解析会**跨集增量合并**建议，
    处理过的条若不留痕，重跑某一集的资产解析就会把它**复活**——用户要么重复
    生成一张同样的妆造图，要么再拒一次。
    """
    sug_list = _load_costume_suggestions(db, project_id)
    hit = next((s for s in sug_list if s.get("id") == suggestion_id), None)
    if hit is None:
        raise HTTPException(404, "建议不存在或已处理")
    left = [s for s in sug_list if s.get("id") != suggestion_id]
    db.merge(Setting(key=costume_suggestions_key(project_id),
                     value=json.dumps(left, ensure_ascii=False)))
    mark_costume_handled(db, project_id, str(hit.get("character") or ""),
                         str(hit.get("name") or ""))
    # 兜底提交：mark_costume_handled 在键为空时会提前返回，移除必须照样落库
    db.commit()


@router.get("/projects/{project_id}/costume-suggestions")
def list_costume_suggestions(project_id: str, db: Session = Depends(get_db)):
    """妆造建议清单（解析提取的换装事件，按角色分组供角色卡确认）。"""
    _project(db, project_id)
    return _load_costume_suggestions(db, project_id)


class CostumeSuggestionAcceptIn(BaseModel):
    """接受妆造建议入参：字段缺省时使用建议原文（允许微调后接受）。"""
    character_id: str
    name: str | None = None
    appearance_desc: str | None = None
    episodes: list[int] | None = None
    model_id: str = ""


@router.post("/projects/{project_id}/costume-suggestions/{suggestion_id}/accept",
             status_code=201)
def accept_costume_suggestion(project_id: str, suggestion_id: str,
                              body: CostumeSuggestionAcceptIn,
                              db: Session = Depends(get_db)):
    """接受妆造建议：按建议描述生成妆造形象（复用妆造生成全链路——基础形象
    参考图 + 妆造负向词 + 适用集数写入），并从建议清单移除该条。"""
    _project(db, project_id)
    sug = next((s for s in _load_costume_suggestions(db, project_id)
                if s.get("id") == suggestion_id), None)
    if not sug:
        raise HTTPException(404, "建议不存在或已处理")
    char = db.get(Character, body.character_id)
    if not char or char.project_id != project_id:
        raise HTTPException(404, "角色不存在或不属于该项目")
    if char.name != sug.get("character"):
        raise HTTPException(400, "建议与角色不匹配")
    desc = (body.appearance_desc or "").strip()         or (sug.get("appearance_desc") or "").strip()
    if not desc:
        raise HTTPException(400, "建议缺少妆造描述，无法生成")
    name = (body.name or "").strip() or sug.get("name") or f"{char.name}妆造"
    episodes = body.episodes if body.episodes is not None         else [e for e in (sug.get("episodes") or []) if isinstance(e, int)]
    # 注：这里必须传已校验过的 char.id（同一个路由函数被当作普通函数直接调用，
    # 位置参数即它的 character_id）。历史 bug：写成不存在的局部名 character_id，
    # 导致「接受妆造建议」接口恒 500（实测 2026-09-22 用户报障）
    res = create_character_image(char.id, CharacterImageIn(
        kind="costume", name=name, appearance_desc=desc,
        episodes=episodes or None, model_id=body.model_id), db)
    _replace_suggestion(db, project_id, suggestion_id)
    return {**res, "episodes": episodes or []}


@router.post("/projects/{project_id}/costume-suggestions/{suggestion_id}/reject")
def reject_costume_suggestion(project_id: str, suggestion_id: str,
                              db: Session = Depends(get_db)):
    """拒绝妆造建议（从建议清单移除，不生成）。"""
    _project(db, project_id)
    _replace_suggestion(db, project_id, suggestion_id)
    return {"ok": True}


class ScenePreferIn(BaseModel):
    """「设为生效」入参：version_id 为空 = 回退跟随最新版本。"""
    version_id: str | None = None


@router.post("/scenes/{scene_id}/image/prefer")
def prefer_scene_image(scene_id: str, body: ScenePreferIn,
                       db: Session = Depends(get_db)):
    """把指定版本设为场景的生效主图（分镜参考图与卡片展示随之切换）。

    version_id 为空表示回退跟随最新版本。版本必须属于该场景节点。
    """
    scene = db.get(Scene, scene_id)
    if not scene:
        raise HTTPException(404, "场景不存在")
    vid = (body.version_id or "").strip() or None
    if vid:
        v = db.get(NodeVersion, vid)
        if not v or v.node_id != f"scene_{scene_id}" \
                or v.project_id != scene.project_id:
            raise HTTPException(400, "版本不属于该场景")
        if not v.artifact_id:
            raise HTTPException(400, "该版本没有产物，无法设为生效")
    scene.preferred_version_id = vid
    db.commit()
    return {"ok": True}


class CharacterImagePreferIn(BaseModel):
    """形象「设为生效」入参：version_id 为空 = 回退跟随最新版本。"""
    version_id: str | None = None


@router.post("/characters/{character_id}/images/{image_id}/prefer")
def prefer_character_image(character_id: str, image_id: str,
                           body: CharacterImagePreferIn,
                           db: Session = Depends(get_db)):
    """把指定版本设为形象记录的生效图（分镜参考图与卡片展示随之切换）。

    version_id 为空表示回退跟随最新版本。版本必须属于该形象的抽卡节点
    （char_{cid}_{imgId}）。
    """
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    image = db.get(CharacterImage, image_id)
    if not image or image.character_id != character_id:
        raise HTTPException(404, "形象不存在或不属于该角色")
    vid = (body.version_id or "").strip() or None
    if vid:
        v = db.get(NodeVersion, vid)
        if not v or v.node_id != f"char_{character_id}_{image_id}" \
                or v.project_id != char.project_id:
            raise HTTPException(400, "版本不属于该形象")
        if not v.artifact_id:
            raise HTTPException(400, "该版本没有产物，无法设为生效")
    image.preferred_version_id = vid
    db.commit()
    return {"ok": True}


class ComposeBatchIn(BaseModel):
    project_id: str
    episode_ids: list[str]


@router.post("/episodes/compose-batch")
def compose_batch(body: ComposeBatchIn, request: Request,
                  db: Session = Depends(get_db)):
    """多集连发合成：按传入顺序逐集排队（6.6 第 6 条 / 6.7 第 2 条），无视频的分集跳过。

    2026-09-30 补：**请求体里的 project_id 要判归属**——中间件看不到请求体，
    此前任何登录用户都能按别人的 project_id 触发付费合成。
    """
    if not body.episode_ids:
        raise HTTPException(400, "未选择分集")
    access.ensure_project_access(db, body.project_id, request, need="write")
    _actor = access.current_user_id(request)
    task_ids: list[str] = []
    skipped = 0
    failed: list[dict] = []
    for eid in body.episode_ids:
        episode = db.get(Episode, eid)
        if not episode or episode.project_id != body.project_id:
            skipped += 1
            continue
        shots = db.scalars(
            select(Shot).where(Shot.episode_id == eid).order_by(Shot.seq)
        ).all()
        videos: list[str] = []
        for shot in shots:
            artifact = _latest_artifact(db, body.project_id, f"shot_{shot.id}_v")
            if artifact:
                videos.append(artifact)
        if not videos:
            skipped += 1
            continue
        try:
            submitted = engine.submit(
                project_id=body.project_id, node_id=f"episode_{eid}",
                runner_name="compose", params={"mode": "concat"},
                input_refs={"videos": videos},
                actor_user_id=_actor,
            )
        except Exception as e:  # noqa: BLE001
            # 提交异常与「无视频可合成」分开计数（审计 13 号：旧实现两者都记
            # skipped，用户只看到一个数字无从定位；失败原因随响应透出）
            skipped += 1
            failed.append({"episode_id": eid,
                           "reason": str(e)[:120] or "提交失败"})
            continue
        task_ids.append(submitted["task_id"])
    return {"task_ids": task_ids, "skipped": skipped, "failed": failed}


# ---------- 画布产物应用（资产三方联动，设计文档 4.9 P0） ----------
def _image_artifact(db: Session, artifact_id: str) -> Asset:
    asset = db.get(Asset, artifact_id)
    if not asset:
        raise HTTPException(404, "产物不存在")
    if asset.kind != "image":
        raise HTTPException(400, "仅图片产物可应用为角色形象/场景主图")
    return asset


@router.post("/characters/{character_id}/apply-image")
def apply_character_image(character_id: str, body: ApplyImageIn,
                          db: Session = Depends(get_db)):
    """将图片产物设为角色形象（追加一条**角色设定图**记录——基础图只有这一种）。"""
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    _image_artifact(db, body.artifact_id)
    image = CharacterImage(character_id=character_id,
                           name=f"{char.name}·角色设定图", kind="fourview",
                           appearance_desc="", artifact_id=body.artifact_id)
    db.add(image)
    db.commit()
    return {"ok": True, "image_id": image.id}


@router.post("/scenes/{scene_id}/apply-image")
def apply_scene_image(scene_id: str, body: ApplyImageIn,
                      db: Session = Depends(get_db)):
    """将图片产物设为场景主图。"""
    scene = db.get(Scene, scene_id)
    if not scene:
        raise HTTPException(404, "场景不存在")
    _image_artifact(db, body.artifact_id)
    scene.artifact_id = body.artifact_id
    # 画布应用的图追加为场景节点的 uploaded 版本——进入版本流后可被
    # 「设为生效」选中，与生成版本统一管理
    seq = (db.scalar(
        select(NodeVersion.seq).where(
            NodeVersion.node_id == f"scene_{scene_id}",
            NodeVersion.project_id == scene.project_id)
        .order_by(NodeVersion.seq.desc()).limit(1)) or 0) + 1
    db.add(NodeVersion(node_id=f"scene_{scene_id}", project_id=scene.project_id,
                       seq=seq, source="uploaded", artifact_id=body.artifact_id))
    db.commit()
    return {"ok": True}


@router.post("/characters/{character_id}/costume/apply")
def apply_costume(character_id: str, body: CostumeApplyIn,
                  db: Session = Depends(get_db)):
    """妆造→分镜批量映射（6.4 第 2 条）：把指定形象批量写入引用该角色的分镜
    costume_map，同时回写形象适用集数；episode_ids 为空 = 全部含该角色的分集。"""
    char = db.get(Character, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    image = db.get(CharacterImage, body.image_id)
    if not image or image.character_id != character_id:
        raise HTTPException(400, "形象不存在或不属于该角色")
    if not char.project_id:
        raise HTTPException(400, "全局角色不支持映射分镜")

    if body.episode_ids:
        episodes = [
            e for e in
            db.scalars(select(Episode).where(Episode.id.in_(body.episode_ids))).all()
            if e.project_id == char.project_id
        ]
    else:
        episodes = db.scalars(
            select(Episode).where(Episode.project_id == char.project_id)
            .order_by(Episode.seq)
        ).all()

    updated = 0
    if episodes:
        ep_ids = [e.id for e in episodes]
        shots = db.scalars(select(Shot).where(Shot.episode_id.in_(ep_ids))).all()
        for shot in shots:
            if character_id not in (shot.character_ref_ids_json or []):
                continue
            mapping = dict(shot.costume_map_json or {})
            mapping[character_id] = image.id
            shot.costume_map_json = mapping
            updated += 1

    image.episodes_json = sorted({e.seq for e in episodes})
    db.commit()
    return {"ok": True, "updated": updated, "episodes": image.episodes_json}


# ---------- 分集与分镜 ----------
@router.get("/projects/{project_id}/episodes")
def list_episodes(project_id: str, db: Session = Depends(get_db)):
    _project(db, project_id)
    rows = db.scalars(
        select(Episode).where(Episode.project_id == project_id)
        .order_by(Episode.seq)
    ).all()
    return [_episode_dict(db, e) for e in rows]


@router.get("/episodes/{episode_id}/script")
def get_episode_script(episode_id: str, db: Session = Depends(get_db)):
    """取该集正文（制作真源）。

    正文是大字段（单集数千字），不进分集列表响应（性能红线：大字段按需查端点），
    编辑/查看时单独取。
    """
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    return {"id": episode.id, "seq": episode.seq, "title": episode.title,
            "body": episode.body or "",
            "char_start": episode.char_start, "char_end": episode.char_end}


@router.patch("/episodes/{episode_id}")
def patch_episode(episode_id: str, body: EpisodePatchIn, db: Session = Depends(get_db)):
    """编辑分集计划（集名/梗概/当集正文）——确定性写入，不触发重新分集。"""
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    if body.title is not None:
        episode.title = body.title.strip()[:200]
    if body.synopsis is not None:
        episode.synopsis = body.synopsis.strip()
    if body.body is not None:
        # 正文改动使已拆解的分镜与正文不再对应 → 标记该集需重新制作
        # （不自动删分镜：产物是用户资产，由用户决定是否重跑）
        episode.body = body.body
        if episode.produce_status in (PRODUCE_BUILT, PRODUCE_BUILDING):
            episode.produce_status = PRODUCE_AUDITED
    db.commit()
    return _episode_dict(db, episode)


@router.post("/projects/{project_id}/episodes/replan")
def replan_episodes(project_id: str, body: EpisodeReplanIn,
                    db: Session = Depends(get_db)):
    """重新分集：LLM 按剧情/爆点/钩子重切分集边界。

    两段式（沿用资产两段式删除的既有约定）：confirm=false 返回预检（将重建
    几集、丢弃多少分镜），confirm=true 才提交长任务——重切边界会让旧分镜不再
    对应，属破坏性操作，必须先让用户看到代价。
    """
    _project(db, project_id)
    if not llm_available():
        raise HTTPException(400,
                            "LLM 未配置：请在「模型管理-服务配置」中填写 LLM 服务后重试")
    setting = db.get(Setting, script_text_key(project_id))
    text = (setting.value or "").strip() if setting and setting.value else ""
    if not text:
        raise HTTPException(400, "剧本原文为空，请先解析或粘贴剧本")
    ep_ids = db.scalars(
        select(Episode.id).where(Episode.project_id == project_id)).all()
    shots = 0
    if ep_ids:
        shots = db.scalar(
            select(func.count()).select_from(Shot)
            .where(Shot.episode_id.in_(ep_ids))) or 0
    if not body.confirm:
        return {"episodes": len(ep_ids), "shots": shots, "chars": len(text)}
    try:
        submitted = engine.submit(
            project_id=project_id, node_id=NODE_ID_REPLAN,
            runner_name=RUNNER_REPLAN_EPISODES,
            params={"project_id": project_id},
        )
    except Exception as e:  # noqa: BLE001 提交异常（未知 runner 等）转 HTTP 错误
        raise HTTPException(400, str(e)) from e
    return {"task_id": submitted["task_id"], "status": submitted["status"]}


@router.get("/episodes/{episode_id}/shots")
def list_shots(episode_id: str, db: Session = Depends(get_db)):
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    shots = db.scalars(
        select(Shot).where(Shot.episode_id == episode_id)
        .order_by(Shot.seq)
    ).all()
    return [_shot_dict(db, episode.project_id, s) for s in shots]


@router.post("/episodes/{episode_id}/shots", status_code=201)
def create_shot(episode_id: str, body: ShotIn, db: Session = Depends(get_db)):
    """新增分镜（追加末尾）。"""
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    desc = (body.description or "").strip()
    if not desc:
        raise HTTPException(400, "分镜描述不能为空")
    if body.scene_ref_id and not db.get(Scene, body.scene_ref_id):
        raise HTTPException(400, "引用的场景不存在")
    last_seq = db.scalar(
        select(Shot.seq).where(Shot.episode_id == episode_id)
        .order_by(Shot.seq.desc()).limit(1)
    ) or 0
    shot = Shot(episode_id=episode_id, seq=last_seq + 1, description=desc,
                camera="固定",
                # 时长钳位到通用安全区间（与解析侧同口径，防 provider 静默拉长）
                duration=max(SHOT_DURATION_MIN,
                             min(SHOT_DURATION_MAX, int(body.duration))),
                scene_ref_id=body.scene_ref_id,
                character_ref_ids_json=[], dialogue_json=[])
    db.add(shot)
    db.commit()
    return _shot_dict(db, episode.project_id, shot)


@router.patch("/shots/{shot_id}")
def patch_shot(shot_id: str, body: ShotPatchIn, db: Session = Depends(get_db)):
    row = db.get(Shot, shot_id)
    if not row:
        raise HTTPException(404, "分镜不存在")
    # 分镜经 episode_id 关联项目（任务书：跨租户不得改别人的分镜）
    if body.description is not None:
        row.description = body.description
    if body.video_prompt is not None:
        # 保存侧归一：标题与首个时间标记之间必须有换行（幂等，只动接缝）
        row.video_prompt = normalize_picture_head_newline(body.video_prompt)
    duration_note = ""
    if body.duration is not None:
        # 时长钳位到通用安全区间（与解析侧同口径，防 provider 静默拉长/截断；
        # 钳位后值随响应回传，前端据此同步显示）
        requested = max(SHOT_DURATION_MIN,
                        min(SHOT_DURATION_MAX, int(body.duration)))
        # 语速硬门复核（审计 13 号 #3）：解析期 apply_duration_floor 保证台词
        # 念得完，但手工编辑只做钳位——48 字台词可被设成 5 秒，而时序对账只验
        # 「小节与 duration 自洽」，对账通过、precheck 全绿、成片台词被截断。
        # 含台词的镜低于语速下限时自动抬升并明示原因（要短节奏请删台词，
        # 但不该得到一个念不完的镜）。
        from app.script.llm_parser import _dialogue_duration_floor
        floor = _dialogue_duration_floor(
            {"dialogue": row.dialogue_json or [], "duration": requested})
        if floor > requested:
            # 抬升不超过全局上限（时长钳位口径不破）；台词超过上限仍念不完时
            # 明示建议拆镜（解析期由 apply_duration_floor 自动拆链，手工编辑
            # 场景只提示不代改，避免静默改变用户的镜头结构）
            requested = min(floor, SHOT_DURATION_MAX)
            duration_note = (
                f"台词需 {floor} 秒才念得完（按语速与对话轮转折算），已自动调整为 "
                f"{requested} 秒" + ("；仍不足，建议拆分为链式镜头"
                                     if floor > SHOT_DURATION_MAX else ""))
        row.duration = requested
        if row.video_prompt:
            # **改时长即刻把时间小节重排到新时长**（2026-09-30 用户质疑「时长改了
            # 不就与画面描述不一致了」）：与出片提交路径同一份确定性对账
            # （rescale 等比缩放 + rebalance 台词小节扩容，都是算术、不动画面文本）
            # ——框里所见即实际发送的内容；此前只有出片时才重排，框里留着旧秒数。
            from app.core.prompt_metrics import (
                rebalance_beat_marks, rescale_beat_marks,
            )
            _vp = rescale_beat_marks(row.video_prompt, requested)
            _vp = rebalance_beat_marks(
                _vp, [d.get("text") or "" for d in (row.dialogue_json or [])])
            row.video_prompt = normalize_picture_head_newline(_vp)
    if body.scene_ref_id is not None:
        row.scene_ref_id = body.scene_ref_id
    if body.character_ref_ids is not None:
        row.character_ref_ids_json = body.character_ref_ids
    if body.costume_map is not None:
        row.costume_map_json = body.costume_map
    if body.camera is not None:
        row.camera = body.camera
    if body.transition_in is not None:
        # 衔接类型合法值校验（与解析侧枚举一致）
        if body.transition_in not in ("continue", "time_skip", "scene_cut"):
            raise HTTPException(400, f"无效衔接类型：{body.transition_in}")
        row.transition_in = body.transition_in
    db.commit()
    # duration 回传：手工编辑被钳位/抬升时前端以服务端值为准同步显示；
    # duration_note 为语速复核的调整原因（空串=未调整），前端提示给用户
    return {"ok": True, "duration": row.duration,
            "duration_note": duration_note,
            # 改时长会确定性地重排画面描述里的时间小节（见上）：回传新文本与
            # 对账结果，前端就地把框更新成「实际会发送的那份」
            "video_prompt": row.video_prompt or "",
            "timing_ok": not timing_error({"duration": row.duration,
                                           "video_prompt": row.video_prompt or ""})}


class RegeneratePromptIn(BaseModel):
    """单镜画面描述重算入参（两段式契约）。

    - 都为空：生成并落库（旧行为，体检项一键修复仍走这条）；
    - `dry_run=true`：**只生成不落库**，返回结果供预览；
    - `prompt` 非空：**直接应用这段文本**（预览结果，用户可能改过），不再调 LLM。

    为什么要有第三种：预览与实际落库必须是**同一段文本**——若「应用」时再生成
    一次，用户确认过的内容会变（那还是抽盲盒）。而落库又必须走本端点才能带上
    「清除该镜内容类体检项」的联动，故不能改走通用的 updateShot。
    """
    dry_run: bool = False
    prompt: str = ""
    # 姿态改写（与 prompt 同源的两段式）：dry_run 预览返回它，用户点「应用」时
    # 原样回传——**应用路径不调 LLM**，不回传就等于改写丢了（只改了 video_prompt）
    state_position: str = ""


@router.post("/shots/{shot_id}/regenerate-video-prompt")
def regenerate_shot_video_prompt(shot_id: str,
                                 body: RegeneratePromptIn | None = None,
                                 db: Session = Depends(get_db)):
    """单镜画面描述重生成（时长编辑后时序失配重算 / 手动重抽）。

    复用解析期同款画风与技能包上下文（Setting 存档）、资产锚定摘要与
    前序镜头衔接锚点——与批量画面描述同语义，LLM 同步调用秒级返回。

    UI 的「重算」走**两段式**：先 dry_run 预览 → 用户看过（可改）→ 回传应用。
    此前是点一下直接全量覆盖用户手改过的描述（用户反馈「像抽盲盒」）。
    预演**同样不触发体检项清除**——副作用与落库保持一致，否则「看看但没应用」
    也会把体检问题抹掉。
    """
    row = db.get(Shot, shot_id)
    if not row:
        raise HTTPException(404, "分镜不存在")
    ep = db.get(Episode, row.episode_id)
    if not ep:
        raise HTTPException(404, "分集不存在")
    if not llm_available():
        raise HTTPException(400, "LLM 未配置：请在「模型管理-服务配置」中"
                                 "填写 LLM 服务后重试")
    # 分镜 payload 形态（引用 id → 名称，与解析期结构一致）
    scene = db.get(Scene, row.scene_ref_id) if row.scene_ref_id else None
    char_names = [c.name for c in db.query(Character).filter(
        Character.id.in_(row.character_ref_ids_json or [])).all()] \
        if row.character_ref_ids_json else []
    prop_names = [p.name for p in db.query(Prop).filter(
        Prop.id.in_(row.prop_ref_ids_json or [])).all()] \
        if row.prop_ref_ids_json else []
    shot_payload = {"seq": row.seq, "duration": row.duration,
                    "description": row.description or "",
                    "excerpt": row.excerpt or "",
                    "dialogue": row.dialogue_json or [],
                    "scene_name": scene.name if scene else "",
                    "character_names": char_names,
                    "prop_names": prop_names,
                    "transition_in": row.transition_in or "",
                    "state": row.state_json or {}}
    # 前序 2 镜摘要（衔接锚点，与 enrich 批间衔接同语义）
    prev_rows = db.scalars(select(Shot).where(
        Shot.episode_id == row.episode_id, Shot.seq < row.seq
    ).order_by(Shot.seq.desc()).limit(2)).all()
    prev_shots = [{"seq": s.seq, "description": (s.description or "")[:60],
                   "camera": s.camera or "", "state": s.state_json or {}}
                  for s in reversed(prev_rows)]
    ctx_style = project_style_context(db, ep.project_id)
    characters = [{"name": c.name,
                   # image_prompt 取剥净系统块的本体（2026-09-30：组装写回后字段含
                   # 画风前缀/负向词，原样喂拆解 LLM 会把系统样板句带进画面描述）
                   "image_prompt": strip_character_system_blocks(
                       c.image_prompt or "",
                       _style_keywords(db, ep.project_id)),
                   # appearance 必须随载荷下去：画面描述里的「人物一致性」段要求
                   # 逐字用它，不给就只能凭印象编（实测把「墨发」写成「麦色长发」、
                   # 人脸特征整段丢——2026-09-29 报障「脸变了」）
                   "appearance": c.appearance or "",
                   "persona": c.persona or ""} for c in db.scalars(
        select(Character).where(Character.project_id == ep.project_id))]
    scenes = [{"name": s.name,
               # image_prompt 取**剥净系统块**的本体（2026-09-30：写回后字段里含
               # 空镜约束/负向词，原样喂 LLM 会把「没有任何生物出现」写进镜头描述）
               "image_prompt": strip_scene_system_blocks(
                   s.image_prompt or "", getattr(s, "layout", "") or ""),
               # layout（空间法典）必须随 scenes 一起传入：asset_briefs 会把
               # 「空间布局：…」前置进 scene_briefs，漏传则单镜重算描述悄悄
               # 丢掉「人物只能经由门进出」约束（批量生成有、重算后没有——
               # 审计 13 号 #2 的越修越退步）
               "layout": getattr(s, "layout", "") or "",
               "description": s.description or ""} for s in db.scalars(
        select(Scene).where(Scene.project_id == ep.project_id))]
    props = [{"name": p.name,
              "image_prompt": strip_prop_system_blocks(p.image_prompt or ""),
              "description": p.description or ""} for p in db.scalars(
        select(Prop).where(Prop.project_id == ep.project_id))]

    def _skill(key: str) -> str:
        s = db.get(Setting, key)
        return s.value if s and s.value else ""

    given = (body.prompt if body else "").strip()
    dry_run = bool(body and body.dry_run)
    if given:
        # 应用预览结果（用户确认/改过的文本）：不再调 LLM，保证应用的就是看到的。
        # 姿态改写随预览一起回传（应用路径不调 LLM，不从入参取就丢了）
        vp = given
        rewritten_pos = (body.state_position or "").strip()
    else:
        try:
            # 本端点是**唯一绕过任务引擎直调 LLM 的地方**（其余 LLM 调用都经
            # runner，预设由 task_engine 注入）——这里必须自己挂项目预设，
            # 否则同一项目在本端点会悄悄用全局 LLM（「重算」结果风格不一致）
            with use_llm_model(project_preset(
                    db.get(Project, ep.project_id), KIND_LLM)):
                vp, rewritten_pos = regenerate_shot_prompt(
                    shot_payload, prev_shots, ctx_style["style_hint"],
                    characters, scenes, props,
                    skill_text=_skill(skill_text_key(ep.project_id)),
                    model_skill_text=_skill(t2v_skill_key(ep.project_id)),
                    # 确定性问题清单 → 让这次重算成为**定向修复**而不是再抽一次
                    issues=_shot_issue_lines(db, ep.project_id, row),
                    )
        except ScriptParseError as e:
            raise HTTPException(502, f"画面描述重生成失败：{e}") from e
        # 姿态改写（模型返回的取景语言版 position）：**只在该镜被判「姿态用了
        # 指令语言」时才会非空**（见 _shot_issue_lines 的姿态那条）。落库后，
        # 首帧组装读到的就是取景语言——「背对」这类写法会被正面直立定妆图压过，
        # 成图始终正面；「只见背影与后脑」模型才照做（实测镜2 一次到位）。
        if rewritten_pos.strip():
            _state_new = dict(row.state_json or {})
            _state_new["position"] = rewritten_pos.strip()
            row.state_json = _state_new
        # 时序对账 + 台词密度重平衡（**确定性，LLM 之后跑**）：LLM 写的小节常与
        # duration 不符，而 j 规则它并不总遵守——实测 13 镜小节对不上（末节止于
        # 5 秒而 duration 是 6、或一直排到 10 秒），用户点「重算」标签仍在。
        # 重算 = LLM 重写 + 代码对账，这样标签才会真的消失。
        vp = rescale_beat_marks(vp, row.duration)
        vp = rebalance_beat_marks(
            vp, [d.get("text") or "" for d in (row.dialogue_json or [])])
        if dry_run:
            # 预演：不落库、不清体检项（副作用与落库一致，见 docstring）
            return {"ok": True, "video_prompt": vp, "qa_cleared": 0,
                    "dry_run": True,
                    # 改写后的 position（空串 = 本镜无需改写）：预览必须能看到，
                    # 否则用户在「应用」前不知道姿态会被改成什么样
                    "state_position": rewritten_pos.strip(),
                    "timing_ok": not timing_error({"duration": row.duration,
                                                   "video_prompt": vp})}
    # 写入侧归一（2026-09-30）：LLM 或历史重排可能把「【视频画面】」与首个时间
    # 标记挤在一行——这里统一补回换行（幂等，只动这一处接缝）
    row.video_prompt = normalize_picture_head_newline(vp)
    # 重算即修复：清除该镜在体检存档中的「内容类」问题（suspended/state_chain/
    # overload——描述重写后旧问题不再成立；transition/line_dup 等结构性项
    # 保留，需用户另行处理）。报告面板的「重算描述」一键修复依赖此联动
    qa_setting = db.get(Setting, storyboard_qa_key(ep.project_id))
    qa_cleared = 0
    if qa_setting and qa_setting.value:
        try:
            old_items: list = json.loads(qa_setting.value)
        except ValueError:
            old_items = []
        kept: list = []
        for it in old_items:
            if (it.get("episode") in (None, ep.seq) and it.get("seq") == row.seq
                    and it.get("rule") in CLEARABLE_QA_RULES):
                qa_cleared += 1
                continue
            kept.append(it)
        if qa_cleared:
            qa_setting.value = json.dumps(kept, ensure_ascii=False)
    db.commit()
    return {"ok": True, "video_prompt": vp, "qa_cleared": qa_cleared,
            "timing_ok": not timing_error({"duration": row.duration,
                                           "video_prompt": vp})}


# 重算画面描述可自动消除的体检项（描述重写后问题不再成立的「内容类」规则）
CLEARABLE_QA_RULES = {"suspended", "state_chain", "overload"}

# 人工重拍**归因到提示词**的判据（决定这条裁决要不要进教训账本）。
# 只有「重拍/重写」+「单变量指向 prompt」才说明**提示词写错了**：
# keep/post 是「这条能过」；changed=seed/refs/model/params 是别的变量不对
# （换种子/换参考图/换模型），归档进来只会污染【历史教训】。
PROMPT_BLAME_VERDICTS: tuple[str, ...] = ("reroll", "rewrite")
PROMPT_BLAME_CHANGED: tuple[str, ...] = ("prompt", "both")


@router.get("/episodes/{episode_id}/storyboard-qa")
def storyboard_qa_report(episode_id: str, db: Session = Depends(get_db)):
    """分镜体检报告（解析期存档全量 + 白话说明）——前端报告面板数据源。

    每项 {rule, level, episode, seq, message, suggest, title, plain}：
    message/suggest 面向懂行的人，title/plain 用生活语言解释问题与影响。
    """
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    qa_setting = db.get(Setting, storyboard_qa_key(episode.project_id))
    items: list[dict] = []
    if qa_setting and qa_setting.value:
        try:
            raw = json.loads(qa_setting.value)
        except ValueError:
            raw = []
        # 存档按项目聚合（一次解析全部分集），按本集 seq 过滤
        items = [i for i in raw if i.get("episode") in (None, episode.seq)]
    items.sort(key=lambda i: (0 if i.get("level") == "error" else 1,
                              i.get("seq") or 0))
    return {"ok": True, "items": items}


# 四视图切分的角度顺序（与 CHARACTER_FOURVIEW_VIEWS 的生成顺序一一对应：
# 正面 → 左侧45° → 背面 → 右侧45°，一行四列等宽排布）
# 设定图格位顺序 = 裁切顺序（与 prompts.CHARACTER_SHEET_* 的版式同源）：
# 左 1/3 正脸特写，右 2/3 依次为 正面 / 侧面 / 背面 全身像
_CAMERA_MOTION_RE = re.compile(
    r"手持跟拍|手持稳定器|手持|跟拍|推轨|摇镜|甩镜|环绕运镜|环绕|运镜|"
    r"移动镜头|升降镜头|长镜头|一镜到底")

# 场景主图提示词中的「空镜」语义（场景卡 image_prompt 为无人空镜撰写）——
# 原样进首帧提示词会弱化/抹除人物（实测镜2 人脸消失的成因之一）
_SCENE_EMPTYSHOT_RE = re.compile(r"无人空镜头?|空镜头?")


def _strip_camera_motion(text: str) -> str:
    """剔除 camera 文本中的运镜术语（保留景别/角度词），清理残留标点。

    首帧是静止画面：运镜动词对生图无意义且有害（「手持跟拍」→ 模型画
    第一人称的手）；运镜词移除后清理空括号与连续标点。

    注意：这是**黑名单**，只对词表内的运镜词有效。LLM 的 camera 是开放
    生成（实测「中近景下摇到手部特写（弯腰—递回）」——「下摇」不在表内
    就漏网，首帧被画成手部特写，与本镜 video_prompt 的「低位后拉」起点
    相矛盾 → 空间断裂）。首帧侧应优先用 `_shot_size_first` 走白名单。
    """
    out = _CAMERA_MOTION_RE.sub("", text or "")
    out = re.sub(r"（\s*）|\(\s*\)", "", out)
    out = re.sub(r"、\s*、|，\s*，|。\s*。|，\s*。", "，", out)
    return out.strip(" ，,、。")


# 景别词表（**长的在前**：正则 alternation 取首个匹配，防「中景」吞掉「中近景」）。
# 首帧侧走白名单提取而非黑名单删除：LLM 的 camera 是开放文本，黑名单永远补不全。
_SHOT_SIZE_RE = re.compile(
    r"超远景|大全景|中全景|远景|全景|中近景|中景|近景|大特写|微距|"
    r"手部特写|面部特写|局部特写|特写")


# 「中景及更宽」的景别：空间布局声明对首帧有指导意义的最小景别（更紧的
# 近景/特写取景放不下方位信息，注入了也是废字）。与 `_SHOT_SIZE_RE` 的词表
# 对齐——「中近景」偏紧，不计入。
_WIDE_SHOT_SIZES: tuple[str, ...] = (
    "超远景", "大全景", "中全景", "远景", "全景", "中景")


def _shot_size_first(camera: str) -> str:
    """取 camera 里的**起点景别**（首帧专用）；无景别词返回空串。

    为什么取第一个而不是全部：`state.position` 描述的是**镜头开始时**的人物
    状态，首帧必须与它同框——「中近景下摇到手部特写」的起点是「中近景」
    （能看到人物弯腰），把它整串带进首帧会让模型直接画手部特写，人物被裁出
    画面、空间关系随之断裂（实测镜3 的成因）。
    """
    m = _SHOT_SIZE_RE.search(camera or "")
    return m.group(0) if m else ""


# 【全局定义】中与首帧其它段落重复的句子标记（首帧已有画风/时刻光线/一致性
# 声明/人物与道具清单，整段带入=同一信息写两遍，实测首帧提示词 918 字 vs
# Seedream 建议 ≤300，注意力被稀释是空间与人物漂移的成因之一）
#
# **「光源为」不在此列**（曾误列，已移出）：state.time 只有「夜·大雪」这类
# 时刻，说的是**什么时候**，不是**光从哪来、什么质感**；「本镜光源为火把侧光
# ＋月光冷光」是画面信息，删掉首帧就只剩时间没有光（实测镜4 首帧无任何光源
# 声明，而同一镜的 video_prompt 有）。光源句另经 `_split_global_light` 提到
# 画面内容段——它属于画面，不属于「素材与参考」。
# 注：标记写「人物一致性」即可——模型实际写的是「人物一致性：…」而旧标记是
# 「人物一致性声明」，**匹配不上**，于是那段被转写的外观（还带着幻觉）就这样
# 进了首帧提示词（2026-09-29 用户报障「脸变了」）。
_GD_DUP_MARKERS = ("国家/地区", "人物一致性")
_GD_LIGHT_MARKER = "光源为"


def _split_global_light(gd: str) -> tuple[str, str]:
    """拆出【全局定义】里的光源句。返回 (光源句, 其余部分)。

    光源句归**画面内容**（与 state.time 合并成完整的「时刻与光线」），其余
    仍走 `_trim_global_block` 后进「素材与参考」段。
    """
    if not gd:
        return "", ""
    light: list[str] = []
    rest: list[str] = []
    for sent in re.split(r"(?<=。)", gd):
        if not sent.strip():
            continue
        (light if _GD_LIGHT_MARKER in sent else rest).append(sent)
    return "".join(light).strip().rstrip("。"), "".join(rest)

# 首帧里「定格于时间线起点」片段的字数上限（句边界裁剪用，见 clip_at_sentence）
OPENING_CLIP_LIMIT = 60
# 切点关键帧（cuts[1]）的对应片段上限（信息量高于首帧，给更宽额度）
CUT_OPENING_CLIP_LIMIT = 80

# 多切（cuts）产物措辞：**只属于切点关键帧**，不得进入单张首帧提示词——
# 实测单张首帧 prompt 里出现「同构图双镜对比：镜A…镜B…两帧构图完全一致，
# 只换人物与手的状态」，对单帧生成毫无意义且诱导模型去画两张（20 号 §1.2 整改 4）
_MULTICUT_MARKERS = ("同构图双镜", "双镜对比", "两帧构图完全一致", "只换人物",
                     "镜A", "镜B", "镜Ａ", "镜Ｂ")


def _strip_multicut_phrases(text: str) -> str:
    """剔除多切（cuts）产物的措辞短语（按逗号/分号级切分，逐条丢弃）。

    只丢含多切标记的**短语**而非整句：同句里可能有该镜真实画面内容
    （如「镜A——顾清寒按在剑柄上的手…」里的手部动作对首帧有效但语义错位），
    保守起见整条短语丢弃，宁可少写也不写错。
    """
    body = (text or "").strip()
    if not body or not any(m in body for m in _MULTICUT_MARKERS):
        return body
    kept = [p for p in re.split(r"(?<=[，,；;、])", body)
            if p.strip() and not any(m in p for m in _MULTICUT_MARKERS)]
    return "".join(kept).strip().rstrip("，,；;、").strip()


def _trim_global_block(gd: str) -> str:
    """剔除【全局定义】里与首帧已有段落重复的句子，只留首帧独有的信息。

    保留的是「镜头位于…朝向…」（首帧构图的空间锚）与「素材：…」（参考图编号
    绑定源）；删掉的是画风句（与画风注入重复）、国家句、光源句（与 state.time
    的「时刻与光线」重复）、一致性声明（与参考图用途说明重复）。
    """
    if not gd:
        return ""
    kept: list[str] = []
    for idx, sent in enumerate(re.split(r"(?<=。)", gd)):
        if not sent.strip():
            continue
        # 首个短句通常是画风（与 _styled_prompt 注入的画风重复）
        if idx == 0 and "风格" in sent and len(sent) <= 60:
            continue
        if any(mark in sent for mark in _GD_DUP_MARKERS):
            continue
        kept.append(sent)
    return "".join(kept).strip()


# 文字禁令（**排除式**，两个不变量都要保住）。
#
# 旧措辞是「不生成字幕、文字、水印。」——它和内容需求**直接矛盾**：场景
# image_prompt 写着「碑上'铸锋'二字覆霜」，同一段提示词又要求「不生成文字」，
# 模型被两种信号撕扯，输出「像字但不是字」的鬼画符（实测铸锋城外·雪原界碑，
# 用户反馈「总是生成乱码」）。这不是纯模型能力问题，提示词自己打架。
#
# 新措辞同时守住两件事：
# ① 防幽灵文字——「画面描述之外的多余文字」仍然禁止（防的是模型自己乱加字，
#    如把人物锚句里的「（男，22岁）」渲染成地砖上的文字）；
# ② 不与内容需求冲突——画面描述里**明确写出的**文字（碑刻/牌匾）是被允许的。
# 无需任何检测/推断：禁令本身就是安全的，写没写文字都不产生矛盾。
_NO_TEXT_CONSTRAINT = ("不生成字幕、水印；除画面描述中明确写出的文字外，"
                       "画面内不得出现任何其他文字。")


# 字幕类措辞（生图提示词的干扰源）：首帧/关键帧不承担字幕，模型看到「字幕/
# 片头字幕/字幕模板」会按影视海报套路构图（人像特写），与分镜画面无关。
# 否定前缀必须一并吞掉：场景 image_prompt 常以「无人物、无字幕。」结尾（LLM
# 为无人空镜撰写），只剔「字幕」会留下悬空的「无人物、无」（实测 5/5 分镜
# 提示词全中，与「画面人物仅限…」正面冲突）。
_CAPTION_TERM_RE = re.compile(
    r"[，,；;、]?\s*(?:无|不含|不出现|不要|没有|不生成|不添加)?\s*"
    r"(?:片头|片尾|开场|结尾)?字幕(?:模板|条|行|样式|镜头)?[^，,。；;]{0,12}")

# 「无人物」类空镜声明：对场景图正确，注入分镜提示词即与人物名单冲突。
# 空角色镜另有更强的显式声明（见首帧组装处的「画面中没有任何人物…」），
# 故此声明在此一律整条剔除——**不依赖本镜是否真有角色**，两种情形都成立。
_SCENE_NOFIGURE_RE = re.compile(
    r"[，,；;、]?\s*(?:无|没有|不出现|不含)\s*(?:任何)?\s*人物\s*(?=[，,。；;、]|$)")

# 剔除条款后残留的连续标点（「…点色。。」类）收敛为单个句号
_DUP_PUNCT_RE = re.compile(r"[。，,；;、]{2,}")

# 素材行里紧跟在「（参考图N）」之后的括号说明——**首帧一律剥掉**。两条理由：
#   ① **冗余**：这些说明（角色外形/场景陈设/道具形态）本就由 character_briefs /
#      scene_briefs / prop_briefs 单独提供，那才是单一真源；
#   ② **有害**：它紧贴「（参考图N）」，模型会读成「**这张参考图的描述**」，而
#      参考图实际内容可能相反 → 直接矛盾。实测镜3 的素材行写着
#      「沈铁衣（参考图2）（**背对镜头**，俯身伏在垛口）」，与画面内容段的
#      「只见他的背影与后脑」打架，而成图**是正面直立**——姿态指令正是从这里
#      漏回来的（`state.position` 那边已改写成取景语言，但素材行没跟着清）。
_REF_NOTE_RE = re.compile(r"(（参考图\s*\d+）)[（(][^）)]*[）)]")


def _strip_ref_notes(text: str) -> str:
    """剥掉素材行里「（参考图N）」后的括号说明，只留引用本身。"""
    return _REF_NOTE_RE.sub(r"\1", text or "")


def _strip_caption_terms(text: str) -> str:
    """剔除生图提示词里的字幕类措辞（首帧/关键帧不做字幕，详见常量注释）。"""
    out = _CAPTION_TERM_RE.sub("", text or "")
    out = _SCENE_NOFIGURE_RE.sub("", out)
    out = _DUP_PUNCT_RE.sub("。", out)
    return out.strip().rstrip("，,；;、").strip()


def _strip_leading_style(text: str, anchor_prompt: str) -> str:
    """剔除段落开头**重复的画风词块**。

    成因：`project_style_context` 告知模型「全局风格限定词 Prompt 首句原样沿用」
    （官方「一字不差」口径），模型遂把它写进 scene.image_prompt 落库；而首帧
    组装又用 `_style_keywords()` 前置一次 → 实测画风词块出现 **2 次**、两套
    质量词（20 号批次 1 目标为 1 次）。
    处理：只在本段开头确实以锚点全称起首时，剥掉首个句子（画风由组装侧统一
    注入，此处剥掉零损失）；锚点为空或段落不以它起首时原样返回。
    """
    t = (text or "").lstrip()
    if not anchor_prompt or not t.startswith(anchor_prompt):
        return t
    head, sep, tail = t.partition("。")
    # 无句号（整段就是画风）时直接清空，避免把内容当风格留下
    return (tail.lstrip("，,；;、 ") if sep else "")


def _scene_text_for_frame(text: str, anchor_prompt: str = "") -> str:
    """场景主图提示词转首帧用：剔除「空镜」语义与重复画风词块。

    场景卡的 image_prompt 为无人空镜撰写（SCENE_IMAGE_CONSTRAINTS 语境），
    「空镜」二字原样进首帧提示词与「画面中出现的人物仅限…」冲突，弱模型
    会压低人物存在感甚至去人脸。画风词块同理（见 `_strip_leading_style`）。
    """
    return _strip_leading_style(_SCENE_EMPTYSHOT_RE.sub("环境", text or ""),
                                anchor_prompt)

# 镜头朝向判定词（camera 为主信号、description 兜底；判定不到默认正面）。
# 只认「朝向动词/角度术语」——裸「左侧/右侧」是站位词（「站在门左侧」≠
# 侧脸镜），曾误判导致硬换四视图侧脸切图、生成人物方向与分镜相反。
# 「画面左/画面右」是 STATE_ANCHOR_FIX 提示词**要求模型写**的锚点措辞
# （「朝画面左 / 画面右 / 画面深处 / 朝镜头」），属契约词表对账、不是猜同义词。
_ANGLE_BACK_RE = re.compile(r"背面|背影|背对|背身|背向|后脑")
_ANGLE_LEFT_RE = re.compile(r"左45|朝左|向左|面朝左|面向左|画面左")
_ANGLE_RIGHT_RE = re.compile(r"右45|朝右|向右|面朝右|面向右|画面右")


def _view_angle_of(angle: str) -> str:
    """镜头朝向（front/left/right/back）→ 设定图里该取哪一格。

    左右两个 45°/90° 侧向在设定图里共用一格「侧面」（版式只有三个全身格）；
    正脸特写不参与镜头取景（它是身份大头照）。
    """
    return {"back": "back", "left": "side", "right": "side"}.get(angle, "front")


def _character_artifact(db: Session, project_id: str, cid: str,
                        mapping: dict | None = None,
                        angle: str = "front",
                        episode_seq: int | None = None) -> str | None:
    """单个角色的参考图（最新版本 artifact）。

    选图优先级（高→低）：
    0. **作用域绑定**（CharacterAssetLink：shot > episode > project 就近覆盖，
       见 services/scoped_link.py——「整集统一换装」一条记录生效，替代逐镜打标）；
    1. 分镜 costume_map 指定的形象（6.4 第 2 条「不同时空妆造精准映射」，
       显式用户意图最高）；
    2. **按集自动妆造**：分镜所属分集落在某条妆造形象的适用集数内
       （episodes_json 升级为规则——「第 3-5 集晚宴装」设一次，新增分镜
       自动正确，多条匹配时最新创建优先）；
    3. 非正面镜头 + 角色有四视图切分产物 → 对应角度的单图（侧面/背面镜头
       的一致性锚点——正面定妆图对背面/侧面戏零参考信息）；
    4. 基础形象（正面，默认）→ 无基础形象回退该角色全部形象（兼容旧数据）。

    同类型多条形象记录时最新创建优先（与卡片预览一致）；同一条形象记录内
    多个抽卡版本时「设为生效」版本优先（用户指定，见 _character_image_artifact）。
    """
    images = db.scalars(
        select(CharacterImage)
        .where(CharacterImage.character_id == cid)
        .order_by(CharacterImage.created_at, CharacterImage.id)
    ).all()
    picked: list[CharacterImage] = []
    # 0. 作用域绑定（最高优先级：用户显式指定的生效范围，就近覆盖——
    #    shot > episode > project，见 services/scoped_link.py）。历史上换装
    #    靠逐镜 blob 映射 + 形象自带集数列表双账本，漏标即漂移；绑定机制
    #    让「整集统一换装」一条记录生效，且优先级高于自动妆造推导。
    from app.services.scoped_link import resolve_bound_image_id

    bound_id = resolve_bound_image_id(
        db, project_id=project_id, character_id=cid, episode_seq=episode_seq)
    if bound_id:
        picked = [img for img in images if img.id == bound_id]
    mapped_id = (mapping or {}).get(cid)
    if not picked and mapped_id:
        picked = [img for img in images if img.id == mapped_id]
    if not picked and episode_seq:
        # 按集自动妆造：适用集数覆盖分镜所属分集的妆造形象（最新创建优先）
        costume = [im for im in images if im.kind == "costume"
                   and im.episodes_json and episode_seq in im.episodes_json]
        picked = costume
    if not picked:
        # 基础形象（2026-09-30 用户定稿：**基础图 = 角色设定图**）：
        # ① 角色设定图（kind=fourview：一张图含头肩特写 + 正/侧/背全身，整张作参考）；
        # ② 历史数据里的基础形象（kind=basic，改造前的正面定妆图）；
        # ③ 其它形象兜底。
        # 注释史：改造前这里**排除**四视图（多格拼版会诱发串脸）；现在设定图就是
        # 基础图本身，且提示词里已明确「多格是同一角色、严禁当多个人画进画面」。
        # **必须筛掉还没出图的记录**（任务在跑时记录已建、产物还没有）：否则
        # 会把「设定图生成中」判成「已就绪」→ 拿到 None 参考图或误判缺图
        _sheet = [img for img in images if img.kind == "fourview"
                  and _character_image_artifact(db, project_id, img)]
        # 历史 basic/triple 记录已迁移成 fourview（2026-09-30）；这里只剩兜底：
        # 既没有设定图、也没有妆造可用的角色返回 None，由就绪度提示用户去生成
        picked = _sheet or [img for img in images if img.kind != "fourview"]
    # 同类型多条形象时**最新创建优先**：与卡片预览一致（看到哪版就用哪版），
    # 修复「抽卡 N 次后分镜仍引用最早一版」的错位
    picked = list(reversed(picked))
    for img in picked:
        # 形象内取图：生效版本优先（用户「设为生效」），否则最新抽卡版本
        artifact = _character_image_artifact(db, project_id, img)
        if artifact:
            return artifact
    return None


def _extra_ref_artifacts(db: Session, project_id: str, shot: Shot) -> list[str]:
    """垫图参考：角色图按镜头朝向选角度 + 场景图 + 关键道具图（去重保序）。

    顺序即参考图优先级：角色 > 场景 > 道具（参考图上限截断时道具先被裁）。
    背对/侧对镜头的角色，其主参考图由 `_character_artifact` 换成**三视图裁出的
    对应角度单图**（1 个名额就够）——这就是「一张拼图覆盖四个角度、挂图只挂需要的
    那一张」（用户 2026-09-29 拍板：省图钱 + 省参考图名额）。
    """
    mapping = shot.costume_map_json or {}
    ep = db.get(Episode, shot.episode_id)
    ep_seq = ep.seq if ep else None
    ids: list[str] = []
    for cid in shot.character_ref_ids_json or []:
        # 角色的参考图 = 它的生效形象（妆造绑定 > 妆造映射 > 按集妆造 > 角色设定图）。
        # 2026-09-30 起**不再按镜头朝向裁切换图**：那张设定图本身就是「头肩特写 +
        # 正/侧/背全身」，整张作参考（用户决策：裁剪那套太复杂、容易出错）
        artifact = _character_artifact(db, project_id, cid, mapping, "front", ep_seq)
        if artifact and artifact not in ids:
            ids.append(artifact)
    if shot.scene_ref_id:
        scene = db.get(Scene, shot.scene_ref_id)
        artifact = _scene_artifact(db, scene) if scene else None
        if artifact and artifact not in ids:
            ids.append(artifact)
        # 补充视角（SHOT_PROMPT k5 声明 + 产物存在才挂）：反打镜/交互区特写需要
        # 主图看不到的那一面，只给主图模型会凭空编另一侧的空间。放在场景主图
        # 之后、道具之前——属场景一族，优先级高于道具。
        view_ref = str((shot.state_json or {}).get("view_ref") or "").strip()
        if scene and view_ref in SCENE_EXTRA_VIEWS:
            view_art = _latest_artifact(
                db, project_id, f"scene_{scene.id}_view_{view_ref}")
            if view_art and view_art not in ids:
                ids.append(view_art)
    for pid_ in shot.prop_ref_ids_json or []:
        prop = db.get(Prop, pid_)
        artifact = _prop_artifact(db, prop) if prop else None
        if artifact and artifact not in ids:
            ids.append(artifact)
    return ids


# 画面描述素材引用标号（统一"参考图N"，N 为传给视频模型的图片序号）
_CHAR_REF_LABEL = "参考图"


def _video_endpoint(db: Session, project_id: str,
                    explicit_model: str = "") -> tuple[str, str]:
    """解析视频链路的 (端点名, 实际模型)——**提交与预算共用同一解析**。

    与 `submit_video` 里那段「端点解析一次、两处共用」同源：预算必须按**本次
    实际提交的模型**算（agnes flash 5 张 / 2.5 8 张），按端点默认档算会把 pro
    当成 flash（25 号 1.6）。
    """
    from app.core.providers import resolve_runtime_config

    submit_model = _resolve_model(db, project_id, KIND_T2V, explicit_model)
    try:
        cfg, model = resolve_runtime_config("t2v", submit_model or "")
        return (getattr(cfg, "name", "") or "custom",
                model or (getattr(cfg, "model", "") or ""))
    except Exception:  # noqa: BLE001 配置解析失败交提交层兜底
        return "custom", ""


def _video_provider_drops_refs(provider: str) -> bool:
    """该视频端点在**有首帧时**是否整段丢弃参考图（端点能力画像）。

    ref2v 视频链路恒有首帧：方舟 seedance 2.0 的「首帧」与「参考图」是互斥
    生成模式（混传 400），t2v_ark_submit 只发 first_frame、整段丢弃 ref_paths，
    而提示词里已写入「（参考图N）」标注与切点对齐句——模型面对不存在的图号
    （审计 13 号 F）。agnes 则把首帧并入 images，不丢参考图。

    **必须传入本次实际提交的端点**：此前实现读全局默认端点（`_load_config`），
    而任务可由编码串指定端点，两个方向都会错——默认 ark + 任务选 agnes ⇒ 参考图
    被无谓清空；默认 agnes + 任务选 ark ⇒ 标注悬空。判定本身走能力表（端点名
    是端点自己的标识，非模型 ID 猜测）。
    """
    try:
        from app.core.providers import video_capability

        return bool(video_capability(provider).first_frame_excludes_refs)
    except Exception:  # noqa: BLE001 能力表异常按「不丢」处理（保守）
        return False


# 参考图标注——两种形态都要脱：带名「名称（参考图N）」与裸「参考图N」
# （多切注入句用的是裸形态）。只脱编号保留名称，不触碰其余文字。
_REF_LABEL_RE = re.compile(r"（参考图\d+）|参考图\d+")


def _strip_ref_labels(prompt: str) -> str:
    """脱去「（参考图N）」编号标注（端点丢弃参考图时用：标注指向不存在的图）。"""
    return _REF_LABEL_RE.sub("", prompt or "").replace("（）", "")


# 未绑定引用的类型前缀（「角色@李默」「场景@…」「道具@…」）：本仓**内部标记语**，
# 只对 _resolve_video_prompt 的占号器有意义，绝不该原样进端点提示词
_MENTION_PREFIX_RE = re.compile(r"(?:角色|场景|道具)@")


def _strip_mention_prefixes(prompt: str) -> tuple[str, int]:
    """脱去未绑定引用的**类型前缀**（「角色@李默」→「李默」），返回（文本, 处数）。

    端点在**零参考图预算**（只收首帧）时，所有「角色@/场景@/道具@」引用都进不了
    占号绑定（预算闸门保留原文），内部标记语会原样进模型提示词——模型把它当
    噪声消费，而名字（唯一有信息量的部分）被掩盖。只脱前缀、名字逐字保留。
    确定性变换：前缀是我们的组装约定，不是对自然语言的猜测。
    """
    if not prompt:
        return prompt or "", 0
    return _MENTION_PREFIX_RE.subn("", prompt)


def _retarget_ref_to_frame(prompt: str) -> str:
    """端点上没有参考图可依时，把「与参考图一致」类措辞改指到首帧画面。

    方舟首帧模式整段丢弃参考图，而提示词里留着「人物妆发、服装、随身物全程与
    参考图一致」——模型找不到那张参考图，一致性约束就落空了（实测：视频中途给
    角色加上首帧没有的眼镜、道具形态变形）。改写为「与首帧画面一致」让约束重新
    落在实处；agnes 端保留参考图，不做改写。
    """
    out = (prompt or "").replace("参考图", "首帧画面")
    # 改写后可能出现「与首帧画面N一致」这类编号残留（编号已在上一步剥除，
    # 此处兜底清理，避免又造出悬空指代）
    return re.sub(r"首帧画面\d+", "首帧画面", out)


def _video_provider_timestamps(provider: str, model_id: str = "") -> bool:
    """该视频端点/模型是否响应提示词里的**精确秒数锚点**（能力画像）。

    官方《Seedance 2.0 系列提示词指南》明确「模型对精确时间（如 0–3 秒）的
    支持不稳定，强行限制时长可能导致生成结果异常」，**2.5 起才响应整数秒
    时间戳**；agnes 未实测，按「不动」处理（默认 True 不改提示词）。

    `model_id` 必须传**本次实际提交的模型**：同一端点下代际不同能力不同
    （ark 上 2.0 剥、2.5 不剥），只按端点判会在换代后错误地剥掉 2.5 支持的
    秒数。判定走能力表（端点档 + 模型家族覆盖），与 `_video_provider_drops_refs`
    同款。
    """
    try:
        from app.core.providers import video_capability

        return bool(video_capability(provider, model_id).prompt_timestamps)
    except Exception:  # noqa: BLE001 能力表异常按「支持」处理（不动提示词）
        return True


# 行首时间小节锚点（「00—02秒：」，容忍 0-3 秒 / 00－02 秒 等各种连字符与冒号
# 变体）——只剥前缀，保留该小节的动作描述与换行（段落顺序即表演顺序）
_TIME_ANCHOR_RE = re.compile(
    r"^[ \t]*\d{1,3}[ \t]*[—–~～\-－][ \t]*\d{1,3}[ \t]*秒[ \t]*[:：][ \t]*",
    re.M)


def _strip_time_anchors(prompt: str) -> tuple[str, int]:
    """剥离行首精确秒数锚点，返回（处理后提示词, 剥离处数）。

    秒数在系统内部承担时序对账（时长覆盖校验）与首帧派生，但作为**给模型的
    输入**只对支持时间戳的端点有效——在不支持的端点上它是官方点名的不稳定
    输入。剥离后各小节仍逐行独立、顺序不变，模型按描述自然分配节奏。
    """
    if not prompt:
        return prompt or "", 0
    stripped, count = _TIME_ANCHOR_RE.subn("", prompt)
    if count:
        # 前缀被删后行尾可能残留空白，收敛掉避免出现空小节行
        stripped = re.sub(r"[ \t]+$", "", stripped, flags=re.M)
    return stripped, count

# 导演级画面描述的段落提取（首帧生成用其【全局定义】与首个时间小节）
# 【全局定义】正文**可能同行紧接**（实测 LLM 两种写法都出：`【全局定义】\n正文`
# 与 `【全局定义】正文`）。旧正则写死 \s*\n，遇到同行写法整段提取为空——首帧
# 提示词因此丢掉画风/素材枚举/光源/人物一致性声明（空镜连「本镜无出场人物」
# 一起丢，模型遂把空镜画成人像特写）。此处容错两种写法，并补一条契约断言。
_VP_GLOBAL_RE = re.compile(r"【全局定义】[ \t]*\n?(.+?)(?=\n【|$)", re.S)
_VP_FIRST_BEAT_RE = re.compile(r"【视频画面】[ \t]*\n?(.+?)(?:\n|$)", re.S)

# 大场面降噪（远景/全景模型的通病：在画不清的远景处补细碎噪点假装丰富）——
# 给模型"允许虚"的出口：焦点主体+浅景深+空气透视衰减
_WIDE_SHOT_RE = re.compile(r"远景|全景|航拍|俯瞰|大全景")
_WIDE_SHOT_SUFFIX = (
    "画面具有明确的焦点主体，浅景深，背景轻微虚化；"
    "利用空气透视，远处景物对比度与饱和度自然衰减；"
    "避免全画面均匀清晰与细碎噪点纹理"
)


def _anti_noise_suffix(camera: str, prompt: str) -> str:
    """远景/全景类镜头自动追加降噪与景深约束（提示词已含时跳过）。"""
    if not (camera and _WIDE_SHOT_RE.search(camera)):
        return prompt
    key = "空气透视"
    return prompt if key in prompt else f"{prompt}。{_WIDE_SHOT_SUFFIX}"


def _ensure_limb_guarantee(prompt: str, reinforce: str) -> str:
    """防回归兜底：角色定妆类 prompt 必须含肢体数量约束，缺失则强制补齐。

    多余肢体（三条胳膊）是角色图最高频的生成事故，任何入口、任何未来的
    组装改动都不允许出现无肢体约束的定妆 prompt。
    """
    return prompt if "两条手臂" in prompt else f"{prompt}。{reinforce}"


def _ensure_scene_guarantee(prompt: str) -> str:
    """防回归兜底：场景主图 prompt 必须是正向定义的空镜头（含「空镜头」锚词）。

    场景图带人物会破坏空镜锚点语义（人物由分镜生成时单独注入参考图）；
    约束正文不得含「人」字（否定式人物措辞会在弱模型上反向触发）。
    """
    if "空镜头" in prompt:
        return prompt
    return f"{prompt}。{prompts_mod.SCENE_IMAGE_CONSTRAINTS}"


def _first_frame_parts(video_prompt: str,
                       offset: int | None = None) -> tuple[str, str]:
    """从导演级画面描述提取（全局定义, 时间小节）作为关键帧生成提示词。

    offset=None 取首个时间小节（首帧）；offset 给定（批次 4 §35.2 多切）
    取覆盖该秒的时间小节——「每格构图由自己的关键帧控制」。全局定义携带
    画风/素材枚举/一致性与光源声明，关键帧由此派生可与后续视频共享同一套
    一致性描述。结构不符或为空返回 ("", "")，调用方回退旧组合。
    """
    if not (video_prompt or "").strip():
        return "", ""
    m = _VP_GLOBAL_RE.search(video_prompt)
    gd = m.group(1).strip() if m else ""
    m2 = _VP_FIRST_BEAT_RE.search(video_prompt)
    opening = m2.group(1).strip() if m2 else ""
    if offset is not None and offset > 0:
        section = _VP_VIDEO_SECTION_RE.search(video_prompt)
        if section:
            for line in section.group(1).splitlines():
                span = _BEAT_SPAN_RE.search(line)
                if span and int(span.group(1)) <= offset < int(span.group(2)):
                    opening = line.strip()
                    break
    return gd, opening


# 台词覆盖比对用的标点/空白归一（容忍 LLM 对台词的小幅排版差异）
_CN_PUNCT_RE = re.compile(
    r"[\s，。！？；：、“”‘’「」『』（）()【】\[\],.!?;:~～…·\"]")


def _dialogue_in_prompt(text: str, prompt: str) -> bool:
    """单条台词是否已被画面描述覆盖：台词原句归一化后出现在提示词中即算。

    只按台词文本判定（说话人名不算——同一角色多句台词只写一句时仍需补拼）；
    空台词视为已覆盖。
    """
    t = _CN_PUNCT_RE.sub("", text or "")
    return (not t) or t in _CN_PUNCT_RE.sub("", prompt)


# 漏写台词补拼时的说明行：告诉模型这批台词属于**本镜**、按出现顺序排列。
# 「按顺序」是代码唯一能确定的信息——每句对应第几秒要 LLM 判断，兜底不做这个猜测。
_DIALOGUE_INJECT_HINT = (
    "本镜台词（按出现顺序，请在对应人物的动作时点逐字念出，不得改写、不得增删）：")


# 漏写的素材引用补拼时的说明行（声明这批素材属于本镜画面）
_MATERIAL_INJECT_HINT = "本镜画面素材："


def _missing_material_refs(db: Session, project_id: str, shot: Shot,
                           vp: str) -> list[str]:
    """本镜**该出场却没引用**的素材（确定性对账）——体检与提交前补拼**共用这一份**。

    判据是「该出场」而不是「已绑定」：**绑定不等于出场**——实测镜1 是仰拍大远景
    空镜，绑定了食盒但 state 账本写着「无具体道具」，LLM 不引用它是对的（按
    「绑定即该引用」判会给这类镜误报）。出场与否看 **state 账本**（「第 0 秒静止
    画面」的权威描述）；账本自己声明为空时不再要求。角色/道具还要求**名字出现在
    账本文本里**（账本没提到的绑定素材，本镜未必出场；宁可漏检不误报）。
    场景是镜头的容器——任何镜都在某个场景里，恒该引用。

    **只在文本已用引用体系时检查**（出现任一 @引用）：旧格式提示词的参考图由
    `_extra_ref_artifacts` 按名单补齐，不走引用体系。
    """
    if not vp or not MENTION_RE.search(vp):
        return []
    st = shot.state_json if isinstance(shot.state_json, dict) else {}
    pos = str(st.get("position") or "")
    props = str(st.get("props") or "")
    out: list[str] = []
    if not _declares_empty(pos):
        for cid in shot.character_ref_ids_json or []:
            ch = db.get(Character, cid)
            if ch and ch.name in pos and f"角色@{ch.name}" not in vp:
                out.append(f"角色@{ch.name}")
    if not _declares_empty(props):
        for pid in shot.prop_ref_ids_json or []:
            pr = db.get(Prop, pid)
            if pr and pr.name in props and f"道具@{pr.name}" not in vp:
                out.append(f"道具@{pr.name}")
    if shot.scene_ref_id:
        sc = db.get(Scene, shot.scene_ref_id)
        if sc and f"场景@{sc.name}" not in vp:
            out.append(f"场景@{sc.name}")
    return out


# **朝向类**姿态指令词（要求模型「把参考图里的人转过去」）→ 正面直立的定妆图
# 做不到，模型会保守地跟参考图走，成图仍是正面。**取景语言**（「只见侧脸与肩线」
# 「只见背影与后脑」）只描述画面里看到什么，模型按描述摆画面即可。
#
# **只收朝向类**：词表一度含「伏在/俯身」，但改写后的正确文本恰恰是「**俯身伏在**
# 垛口上、上半身探出」——那是取景语言的一部分，留着会让同一镜被反复判为待改
# （实测）。体位幅度类描述（伏在/俯身）配合身体细节本就能被模型执行，不算问题。
_POSE_DIRECTIVE_WORDS: tuple[str, ...] = (
    "背对", "背朝", "转身", "侧身", "半侧", "回眸",
)


def _pose_directive_words(position: str) -> list[str]:
    """position 里命中的姿态指令词（确定性词表判定，命中即提示改写）。"""
    return [w for w in _POSE_DIRECTIVE_WORDS if w in (position or "")]


# ---------- 人物朝向锚点：首帧生成前的自动补全（2026-09-29 用户报障） ----------
# 事故：霜刃辞3.0 镜3 要求她与沈铁衣**同向**望向关外，成片却背对沈铁衣。
# 根因：首帧提示词的「人物位置与姿态」是 `state.position` 的**原文**，而
# state 里写的是「只见侧脸与肩线」——**没写朝画面哪一侧**；定妆参考图是正面照，
# 生图模型便给了个 3/4 正脸。视频模型只是忠实延续首帧，不该背锅。
#
# 判据分两层，都是**确定性**的（不是语义理解，是「必需信息缺没缺」的检查）：
# - 半侧面描写（侧脸/半侧身/侧对）**才**需要锚——背影/正面一眼看死，不需要；
# - 锚有两类：① 画面侧向锚（朝画面哪一侧）；② 同框关系锚（≥2 人时与谁同向/反向）。
# 只认**相机参照**的侧向词：实测「面朝垛口方向」这种**场景相对方向**模型换算不动
# （那一镜就是这样翻车的），所以它不算锚。
_SIDE_FACING_WORDS: tuple[str, ...] = ("侧脸", "半侧", "侧身", "侧对", "侧背")
_CAMERA_ANCHOR_WORDS: tuple[str, ...] = (
    "画面左", "画面右", "画面深处", "画面外侧", "画面内侧", "画面中轴",
    "朝镜头", "面向镜头", "背向镜头", "背对镜头",
)
_RELATION_ANCHOR_WORDS: tuple[str, ...] = (
    "同向", "反向", "面对面", "相对而立",
)


def _state_anchor_missing(position: str, character_count: int) -> bool:
    """这段 state.position 是否需要补朝向锚（确定性判定，见上方说明）。"""
    pos = position or ""
    if not any(w in pos for w in _SIDE_FACING_WORDS):
        return False          # 背影/正面写法不需要锚
    if not any(w in pos for w in _CAMERA_ANCHOR_WORDS):
        return True           # 没写朝画面哪一侧
    if character_count >= 2 and not any(w in pos
                                        for w in _RELATION_ANCHOR_WORDS):
        return True           # 同框 ≥2 人却没写同向/反向
    return False


def _repair_state_anchors(db: Session, project_id: str,
                          shots: list[Shot]) -> list[int]:
    """出首帧前**自动补人物朝向锚点**——点一下「重新生成首帧」就该修好。

    只动 `state.position`（其余字段一律不碰），**检测到可疑才调 LLM**（一次调用处理
    全部可疑镜），失败 fail-open（照旧出图，不让一次改写挡住用户的生成）。
    返回被修正的镜号，供前端提示「已自动修正人物朝向」。
    """
    from app.agent import prompts as _p
    from app.script.llm_parser import _llm_call

    suspects: list[tuple[Shot, str]] = []
    for s in shots:
        _st = s.state_json if isinstance(s.state_json, dict) else {}
        pos = str(_st.get("position") or "")
        if _state_anchor_missing(pos, len(s.character_ref_ids_json or [])):
            suspects.append((s, pos))
    if not suspects:
        return []
    _ids = {i for s, _ in suspects for i in (s.character_ref_ids_json or [])}
    _names = {c.id: c.name for c in db.query(Character).filter(
        Character.id.in_(_ids)).all()} if _ids else {}
    payload = {"shots": [{
        "seq": s.seq, "position": pos,
        "characters": [_names.get(i, "") for i in (s.character_ref_ids_json or [])],
        "description": (s.description or "")[:200],
    } for s, pos in suspects]}
    try:
        data = _llm_call(_p.STATE_ANCHOR_FIX_SYSTEM_PROMPT,
                         json.dumps(payload, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 改不了就照旧出图（fail-open）
        logger.warning("人物朝向锚点自动补全失败（照旧生成首帧）：%s", e)
        return []
    by_seq = {int(it.get("seq") or 0): str(it.get("position") or "").strip()
              for it in (data.get("shots") or []) if isinstance(it, dict)}
    fixed: list[int] = []
    for s, pos in suspects:
        new_pos = by_seq.get(s.seq) or ""
        if not new_pos or new_pos == pos:
            continue
        _new = dict(s.state_json or {})
        _new["position"] = new_pos
        s.state_json = _new
        fixed.append(s.seq)
        logger.info("镜%s 人物朝向锚点已自动补全：%s → %s",
                    s.seq, pos[:60], new_pos[:60])
    if fixed:
        db.commit()
    return fixed


def _shot_issue_lines(db: Session, project_id: str, shot: Shot) -> list[str]:
    """本镜的**确定性问题清单**（供重算做定向修复，见 `regenerate_shot_prompt(issues=)`）。

    只收确定性检查判得出的问题（算术 + 名单对账），语义类交给 LLM 自己在创作时
    处理——「凡是能算的绝不给模型」。两类：
    - **台词密度**：某时间小节的台词字数 ÷ 秒数超上限（原生配音只能加速念完）；
    - **素材引用**：该出场的角色/道具/场景没在画面描述里引用（没有参考图锚点）。

    为什么不在重算后补做：这两件事**代码自己就能修**（补引用）或**必须让模型
    重排**（台词分摊），但重算本身不带这些信息就只是「再抽一次」——实测 6 镜
    重算后同类漏写仍有 2 条、还新冒出 2 条密度超载。
    """
    out: list[str] = []
    for beat in dialogue_rate_by_beat(
            shot.video_prompt or "",
            [d.get("text") or "" for d in (shot.dialogue_json or [])]):
        if beat["rate"] > DIALOGUE_RATE_MAX:
            out.append(
                f"{beat['start']:02d}—{beat['end']:02d}秒（{beat['secs']}s）塞了 "
                f"{beat['chars']} 字台词 = {beat['rate']} 字/秒（正常 4-5，上限 "
                f"{DIALOGUE_RATE_MAX:g}）——请把台词分摊到相邻时间小节，"
                "或减少该小节的台词量")
    missing = _missing_material_refs(db, project_id, shot, shot.video_prompt or "")
    if missing:
        out.append(f"画面描述漏了这些素材引用：{'、'.join(missing)}"
                   "——请补进【视频画面】段（引用是绑定参考图的依据）")
    # 姿态指令 → 取景语言（见 `_POSE_DIRECTIVE_WORDS` 的说明）：定妆参考图是
    # 正面直立照，「背对/侧身/伏在」等于要求模型把它转过去，它跟参考图走，
    # 成图仍是正面——必须改写成「画面里只见背影与后脑」这类取景描述。
    _st = shot.state_json if isinstance(shot.state_json, dict) else {}
    _pos = str(_st.get("position") or "")
    # 朝向锚点缺失（2026-09-29 用户报障 + 拍板走"让模型复核"这条路）：
    # 半侧面取景语言（「只见侧脸与肩线」）**没说朝画面哪一侧**时，定妆参考图是
    # 正面照，生图模型会随手给 3/4 正脸——实测两人同框时朝向会反、背对同框的人。
    # 触发用确定性检测（只对可疑镜发起，不逐镜烧调用），改写交给模型：提示词里
    # 明确「已经写清楚的必须原样返回」，误报只多花一次调用、不会改坏数据。
    if _state_anchor_missing(_pos, len(shot.character_ref_ids_json or [])):
        out.append(
            "state.position 是半侧面描写（侧脸/半侧身）但**没给方向锚**——"
            "定妆参考图是正面直立照，只写「侧脸」模型会随手给一个 3/4 正脸"
            "（实测两人同框时朝向会反、背对了同框的人）。请**补上两个锚**："
            "① 朝画面哪一侧（如「面朝画面深处」「面朝画面右侧」——必须是**画面参照**，"
            "「面朝垛口方向」这类场景相对方向不算）；② 同框 ≥2 人时与谁**同向还是反向**"
            "（如「与沈铁衣同向，都朝关外」）。其余文字一律原样保留，"
            "并在输出里用 **state_position** 字段给出改写后的完整 position 原文")
    _poses = _pose_directive_words(_pos)
    if _poses:
        out.append(
            f"state.position 用了姿态指令（{'、'.join(_poses)}）——定妆参考图是"
            "正面直立照，这类写法等于要求模型把参考图里的人转过去，而它会保守地"
            "跟参考图走（成图仍是正面）。请**改写成取景语言**（描述画面里看到什么）："
            "「背对」→「画面里只见他的背影与后脑、看不见正脸」；「侧身/半侧」→"
            "「只见侧脸与肩线」；「伏在」→「俯身伏在…上、上半身探出」；"
            "并在输出里用 **state_position** 字段给出改写后的完整 position 原文")
    return out


def _ensure_material_refs(db: Session, project_id: str, shot: Shot,
                          prompt: str) -> str:
    """把该出场却没引用的素材**补进【视频画面】段**（提交前兜底，确定性）。

    为什么不靠重算：漏没漏是**算得出来的**，而重算是「再抽一次」——不带「上次
    哪里错了」的信息（实测 6 镜重算后仍有 2 条同类漏写，还新引入 2 条台词密度
    超载）。引用是绑定参考图的依据，这种确定性事实该由代码保证。

    补进**段内**而非全文末尾：引用经 `_resolve_video_prompt` 转成（参考图N）时，
    补在【全局定义】的素材行同样能绑上参考图，但那样模型读不到「它在画面里」
    这一层（与台词补拼同一个理由，见 `_inject_missing_dialogue`）。
    """
    vp = (prompt or "").strip()
    missing = _missing_material_refs(db, project_id, shot, vp)
    if not missing:
        return prompt
    line = f"{_MATERIAL_INJECT_HINT}{'、'.join(missing)}"
    idx = vp.find(SEGMENT_REQUIREMENT)
    if idx < 0:
        return _join_parts(vp, line)
    return f"{vp[:idx].rstrip()}\n{line}\n\n{vp[idx:]}"


def _inject_missing_dialogue(prompt: str, lines: list[str]) -> str:
    """把漏写的台词补进【视频画面】段末尾（**段内**，而非全文末尾）。

    **为什么不能补在全文末尾**：那里是【安全合规】，实测补出来的形态是
    「…不生成字幕、标题、水印、拼贴、分屏或额外人物，沈霜辞：爹——吃饭。菜都
    冻成冰了」—— 台词**紧跟在禁令串之后**，模型会把它读成要禁止的内容；且与
    时间小节相隔数百字，模型不知道它对应第几秒，**口型与配音对不上**
    （实测 14/54 条台词落在这里）。

    段内补拼让台词与画面描述同段、并显式声明它属于本镜。**根因治在提示词侧**
    （SHOT_PROMPT 要求把台词原句逐字写入对应时间小节），这里只是漏网兜底——
    所以不追求「精确到某一秒」，只消除「它是什么 / 属不属于这一镜」的歧义。
    """
    block = "\n".join(line for line in lines if line.strip())
    if not block:
        return prompt
    idx = prompt.find(SEGMENT_REQUIREMENT)
    if idx < 0:
        # 无五段式结构（旧数据 / 非分镜提示词）：退回末尾拼，但保留同样的说明行，
        # 至少不让台词与禁令串直接相邻
        return _join_parts(prompt, _DIALOGUE_INJECT_HINT, block)
    return (f"{prompt[:idx].rstrip()}\n{_DIALOGUE_INJECT_HINT}\n{block}"
            f"\n\n{prompt[idx:]}")


def _resolve_video_prompt(db: Session, project_id: str, shot: Shot,
                          prompt: str, *, max_refs: int) -> tuple[str, list[str]]:
    """画面描述中的资产引用转为视频模型可理解的格式。

    【前缀精准映射（类型隔离）】只认带类型前缀的提及——「场景@名称」只绑
    场景资产、「角色@名称」只绑角色资产，前缀天然隔离互不干扰（未来可扩展
    「音色@」等新资产类型）；无前缀的裸名一律不绑定（旧数据的历史提示词由
    提交时的 _extra_ref_artifacts 补齐参考图）。

    - 「场景@名称」→「名称（参考图N）」，场景主图加入参考图列表；
    - 「角色@名称」→「名称（参考图N）」，角色定妆图（按妆造优先级）加入；
    - 别名容错：文本写的名称是资产名前缀（「场景@老周面馆 夜」对资产全名
      「老周面馆 夜 内」）时扩展为资产全名；
    - **两遍法（占号顺序 ≠ 匹配顺序）**：第一遍按名称长度降序做匹配与冲突
      判定（长名优先防短名误吞长名——跨类型重叠保护依赖此顺序），收集命中
      不占号；第二遍按 **角色 → 场景 → 道具** 优先级占号并替换。参考图编号
      顺序即 extra_images 数组顺序，下游按上限截断时先裁道具/场景——短名
      角色不再被挤出预算（历史事故：按长度占号使「张总」排第 5，被 runner
      4 张上限静默裁掉，提示词悬空标注致角色串脸）。
    - 预算闸门（max_refs）：第二遍占号时 refs 满之后的提及保留「角色@名称」
      原文不绑定——编号只在确定保留的集合上分配（LLM 提示词语义零改写）。
    - 返回（转换后提示词, 有序 artifact id 列表）。
    """
    known: list[tuple[str, str, str]] = []  # (名称, 类型, 实体 id)
    for c in db.scalars(select(Character).where(Character.project_id == project_id)):
        known.append((c.name, "char", c.id))
    for s in db.scalars(select(Scene).where(Scene.project_id == project_id)):
        known.append((s.name, "scene", s.id))
    for p in db.scalars(select(Prop).where(Prop.project_id == project_id)):
        known.append((p.name, "prop", p.id))
    known.sort(key=lambda item: len(item[0]), reverse=True)

    mapping = shot.costume_map_json or {}
    ep = db.get(Episode, shot.episode_id)
    ep_seq = ep.seq if ep else None
    final = prompt
    # ---------- 第一遍：匹配与冲突判定（不占号，收集命中） ----------
    # 命中项：(名称, 类型, 完整提及串, artifact, 别名串|None)；别名串非空 =
    # 以「前缀@名称前缀」形式命中（第二遍替换时重新定位——其他替换会移动
    # 第一遍记录的位置）
    hits: list[tuple[str, str, str, str, str | None]] = []
    for name, kind, eid in known:
        prefix = {"scene": "场景@", "char": "角色@", "prop": "道具@"}[kind]
        if prefix not in final:
            continue
        if kind == "char":
            artifact = _character_artifact(db, project_id, eid, mapping,
                                           "front", ep_seq)
        elif kind == "scene":
            scene = db.get(Scene, eid)
            artifact = _scene_artifact(db, scene) if scene else None
        else:
            prop = db.get(Prop, eid)
            artifact = _prop_artifact(db, prop) if prop else None
        if not artifact:
            continue  # 无产物的提及保留原文
        typed = f"{prefix}{name}"
        pos = final.find(typed)
        alias_needle: str | None = None
        compact_name: str | None = None
        if pos < 0:
            # 别名扩展：从全名递减找「前缀@资产名前缀」（后随中文字符说明
            # 名称还在延续，等待更长匹配）。同物异名的根治在解析期：
            # 【资产名单】注入分镜 LLM + _align_asset_names 白名单强制收敛
            # （名单外名称不靠生成期模糊匹配兜底）
            for L in range(len(name) - 1, 1, -1):
                needle = f"{prefix}{name[:L]}"
                idx = final.find(needle)
                if idx < 0:
                    continue
                after = final[idx + len(needle): idx + len(needle) + 1]
                if after and "一" <= after <= "鿿":
                    continue
                alias_needle = needle
                break
        if pos < 0 and alias_needle is None:
            # **空格归一匹配**（格式归一，不是模糊猜测）：资产名含空格而模型写
            # 引用时省掉空格（实测场景名「城头战场 夜」被写成「场景@城头战场夜」），
            # 精确匹配与别名扩展会双双落空——提及原样留在提示词里成了悬空标注
            # （参考图明明挂了，模型却读不到编号）。压缩比对只去掉空白字符，
            # 不改任何字，命中后按压缩下标表**回填原文**替换。
            _cname = name.replace(" ", "")
            if _cname:
                _ctext, _ = _compact_text(final)
                if f"{prefix}{_cname}" in _ctext:
                    compact_name = f"{prefix}{_cname}"
        # 先判定命中、再进入编号分配——历史上是先 append 后 find，未在文本中
        # 引用的资产（如未出场的角色）也顺带占号进入参考图列表，配合笼统的
        # 「都是角色参考」注入句，视频模型把没出场的角色画进画面（张冠李戴）
        if pos < 0 and alias_needle is None and compact_name is None:
            continue
        if pos >= 0:
            after = final[pos + len(typed): pos + len(typed) + 1]
            if after and "一" <= after <= "鿿":
                # 名称后仍有中文延续：检查延续出的片段是否近似构成「异类型资产名」
                # （名称跨类型重叠，如角色「老周」+延续「面馆 夜」≈场景「老周面馆
                #  夜 内」——最长公共前缀达资产名长度-1 即判定）——重叠时保留原文
                # 不绑定（跨类型误绑比不绑危险；正常引用如「角色@老周在灶台前」
                # 的「在」不构成资产名，正常绑定）
                name_start = pos + len(prefix)
                seg = re.split(r"[，。！？；、\n]", final[name_start:])[0]
                conflict = False
                for other, okind, _e2 in known:
                    if okind == kind or not other:
                        continue
                    lcp = 0
                    for a, b in zip(other, seg):
                        if a != b:
                            break
                        lcp += 1
                    if lcp >= max(2, len(other) - 1):
                        conflict = True
                        break
                if conflict:
                    continue  # 误绑保护：不进命中列表，提及保留原文
        hits.append((name, kind, typed, artifact, alias_needle, compact_name))

    # ---------- 第二遍：角色→场景→道具 优先占号 + 全量替换 ----------
    # 稳定排序保持第一遍（长度降序）内的相对顺序；带类型前缀的完整提及串
    # 互不重叠，替换执行顺序不影响结果
    kind_priority = {"char": 0, "scene": 1, "prop": 2}
    hits.sort(key=lambda h: kind_priority[h[1]])
    refs: list[str] = []
    for name, kind, typed, artifact, alias_needle, compact_name in hits:
        if artifact in refs:
            n = refs.index(artifact) + 1  # 同产物复用已占编号（不新增占位）
        else:
            if len(refs) >= max_refs:
                continue  # 预算闸门：满后的提及保留原文不绑定（无悬空标注）
            refs.append(artifact)
            n = len(refs)
        label = f"{name}（{_CHAR_REF_LABEL}{n}）"
        if alias_needle is not None:
            # 别名命中【全量替换】：与精确命中分支同款——只换首处会让尾部
            # （人物限定句/时间小节）残留裸「角色@名称」，参考图绑定对模型失效
            # （审计 13 号：刚修过的串脸成因在别名分支复活）
            idx = final.find(alias_needle)
            while idx >= 0:
                final = (final[:idx] + label
                         + final[idx + len(alias_needle):])
                idx = final.find(alias_needle, idx + len(label))
        elif compact_name is not None:
            # 空格命中同样【全量替换】，且**每次都在当前文本上重定位**（替换会
            # 改变长度，先前算好的下标会失效——与别名分支同款的处理）
            _ctext, _cmap = _compact_text(final)
            _ci = _ctext.find(compact_name)
            while _ci >= 0:
                _s, _e = _cmap[_ci], _cmap[_ci + len(compact_name) - 1] + 1
                final = final[:_s] + label + final[_e:]
                _ctext, _cmap = _compact_text(final)
                _ci = _ctext.find(compact_name)
        else:
            # 全量替换——同名提及在 prompt 里出现多次（【全局定义】枚举 +
            # 尾部人物限定句），只换首处会让尾行残留裸「角色@名称」，参考图
            # 绑定对模型失效（实测镜6 人物错乱的直接根因）
            final = final.replace(typed, label)
    # 未绑定的残留引用**必须留痕**：此前静默保留原文，模型把「角色@沈铁衣」当
    # 普通文字消费，而用户从提示词里根本看不出参考图没挂上（实测 6.3% 的提及
    # 悬空）。这里只告警不改写——名字对不上是解析期的问题（资产名单约束），
    # 生成期没有依据替它猜。
    _leftover = re.findall(r"(?:角色|场景|道具)@[^\s，。；、）)]{1,12}", final)
    if _leftover:
        logger.warning("提示词里有未绑定的资产引用（参考图未挂/名称对不上）：%s",
                       "、".join(_leftover[:6]))
    return final, refs


def _compact_text(text: str) -> tuple[str, list[int]]:
    """去掉**空白字符**后的文本 + 每个压缩字符对应的原文字符下标。

    只服务于「空格归一匹配」（见 `_resolve_video_prompt`）：资产名里有空格而复述
    时省掉的那种引用，压缩比对才能对上；命中后用下标表把位置映射回原文。
    纯格式归一——不改、不增、不删任何非空白字符，也不做任何近似。
    """
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text or ""):
        if ch.isspace():
            continue
        chars.append(ch)
        idx.append(i)
    return "".join(chars), idx


def _produced_shot_ids(db: Session, project_id: str, shots: list[Shot],
                       mode: str) -> set[str]:
    """本次模式下**已有产出**的镜 id 集合——「重出」判定的**唯一来源**。

    含重出时才要求原因（`_reroll_guard`）、才把 take 记成 reroll 类（
    `_record_takes`）——两处必须同口径，否则会出现「守卫要求填原因、记录却不收」
    这类自相矛盾（自检 PASS 138 抓的正是它）。

    判据是确定性的「该镜是否已有产物」（不猜意图）：
    - `first_frames` → 已有**首帧**（`shot_{id}_ff`）；
    - `videos` / `all` → 已有**视频**（`shot_{id}_v`）。

    `all` 也只看视频（25 号复查后端 #4）：它一次提交两样，但判「算不算重出」看
    的是**成品**——止损预算（no_video_ids）本来就是这个口径，两处不一致时
    「只有首帧、视频一次没生成过」的镜会被记成 reroll，两次 all 提交就把用户
    锁死在「已盲摇 2 次」上（而它的视频一次都没出过）。
    """
    suffixes = {"first_frames": ("ff",)}.get(mode, ("v",))
    return {
        s.id for s in shots
        if any(_latest_artifact(db, project_id, f"shot_{s.id}_{sfx}")
               for sfx in suffixes)}


def _reroll_guard(shots: list[Shot], produced_ids: set[str],
                  reason: str, note: str) -> str:
    """重摇原因必填（25 号 2.4）：本批**含重出**时必须有原因；返回归一后的原因。

    全批都是**首拍**（没有任何产出会被覆盖）时不要求：首拍不是重摇，逼用户为它
    编一个原因只会往飞轮里灌噪声。基建类失败的系统自动重试也不走这里（它不经
    提交端点，`changed` 记 `infra:*`）。
    """
    reroll_seqs = [s.seq for s in shots if s.id in produced_ids]
    if not reroll_seqs:
        return ""
    if not (reason or "").strip():
        head = "、".join(str(n) for n in reroll_seqs[:8])
        more = "…" if len(reroll_seqs) > 8 else ""
        _opts = "、".join(REROLL_REASON_LABELS[r] for r in REROLL_REASONS)
        raise HTTPException(
            400, f"重摇原因必填：本次提交会重出 {len(reroll_seqs)} 个已有产出的"
                 f"镜头（镜{head}{more}）——请先选择原因（{_opts}，可多选）再提交。"
                 "这条记录是后续优化的唯一依据：不知道为什么重摇，就只能继续盲摇")
    # 多选（2026-10-10）：逗号分隔串逐项校验去重；选了「其他」仍必须带备注
    reason = _norm_roll_reasons(reason)
    if REROLL_REASON_OTHER in reason.split(",") and not (note or "").strip():
        raise HTTPException(
            400, "重摇原因选了「其他」时必须写一句话说明（备注）——"
                 "否则这条记录无从分析，等于没选")
    return reason


@router.post("/episodes/{episode_id}/shots/generate")
def generate_shots(episode_id: str, body: ShotsGenerateIn,
                   db: Session = Depends(get_db)):
    """批量生成：first_frames（全部 t2i 首帧）/ videos（ref2v 视频需已有首帧）/ all。"""
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    if body.mode not in ("first_frames", "videos", "all"):
        raise HTTPException(400, f"无效生成模式：{body.mode}")
    all_shots = db.scalars(
        select(Shot).where(Shot.episode_id == episode_id)
        .order_by(Shot.seq)
    ).all()
    if not all_shots:
        raise HTTPException(400, "本集暂无分镜")
    # 定向生成子集（审片重拍只重生成指定分镜）；prev_map 始终用全量列表——
    # 机会式链式衔接需要真实的上一镜，与是否在生成子集内无关
    prev_map = {cur.id: prv for prv, cur in zip(all_shots, all_shots[1:])}
    shots = all_shots
    if body.shot_ids:
        idset = set(body.shot_ids)
        shots = [s for s in all_shots if s.id in idset]
        if not shots:
            raise HTTPException(400, "指定的分镜均不存在")

    # ------------------------------------------------------------------
    # 提交前硬门（单一来源：app.services.readiness）
    # 历史上资产生成门禁 / 分镜体检硬门 / 未绑定 @引用检查是三段各自实现的
    # 校验，同一份保证散落在多条路径上——审计 14 号报告里 6 条严重缺陷的共同
    # 形态（主路径有、旁路没有）。现统一走就绪度聚合服务：一处定义、多处消费
    # （本门 + 成片预检 + 前端向导 + /readiness 端点）。
    # 逃生口保持独立：ignore_missing_assets（资产）/ ignore_storyboard_qa（体检）
    # ------------------------------------------------------------------
    if body.mode in ("videos", "all"):
        from app.services.readiness import (
            CHECK_CHARACTER_ASSET, CHECK_PROVIDER_HEALTH, CHECK_REFERENCES,
            CHECK_SCENE_ASSET, CHECK_STORYBOARD, shot_video_readiness,
        )

        blocked: list[str] = []
        blocked_keys: set[str] = set()
        for shot in shots:
            result = shot_video_readiness(db, episode.project_id, shot, episode)
            for check in result["checks"]:
                if check["ok"] or check.get("advisory"):
                    continue  # advisory = 非阻断提示（缺首帧会被跳过/缺图仅降一致性）
                # 端点健康是**端点级**判定（与哪一镜无关）：整批只报一次，
                # 放到逐镜循环外（见下方），否则 42 镜会重复 42 遍同一句话
                if check["key"] == CHECK_PROVIDER_HEALTH:
                    continue
                # 逃生口按检查项精确豁免（不整表跳过——未绑引用等 error 级
                # 问题没有逃生口，必须修）
                if (check["key"] == CHECK_STORYBOARD
                        and body.ignore_storyboard_qa):
                    continue
                if (check["key"] in (CHECK_CHARACTER_ASSET, CHECK_SCENE_ASSET)
                        and body.ignore_missing_assets):
                    continue
                blocked_keys.add(check["key"])
                blocked.append(f"镜{shot.seq}：{check['message']}")
        # 端点健康（25 号 1.5）：逃生口 = 强提——用户刚充值/平台已恢复时账本
        # 还没被新调用刷新，不该把用户锁在门外（与止损强提同一个开关）
        if not body.force:
            from app.services.readiness import provider_health_check

            # 按**本次实际提交的模型**查（body.model_id 可能指向与项目预设不同的
            # 端点，25 号复查后端 #8）
            _t2v_health = provider_health_check(db, episode.project_id, KIND_T2V,
                                                body.model_id)
            if not _t2v_health["ok"]:
                blocked_keys.add(CHECK_PROVIDER_HEALTH)
                blocked.append(_t2v_health["message"])
        if blocked:
            _tag = ""
            if CHECK_PROVIDER_HEALTH in blocked_keys:
                _tag = ENDPOINT_UNAVAILABLE_TAG
                _hint = "。端点问题：充值或稍后重试即自动解除；确认端点已恢复" \
                        "（如刚充值成功）可勾选「强提」强制提交"
            elif any("定妆" in b or "体检" in b for b in blocked):
                _hint = "。请先在资产页补齐参考图，或修复分镜后重试"
            else:
                _hint = "。请修正后重试"
            raise HTTPException(
                400, f"{_tag}生成前置检查未通过（{len(blocked)} 项）："
                     f"{'；'.join(blocked[:6])}" + _hint)

    # 端点健康：首帧链路（t2i 端点）也要前置拦（25 号 1.5）。首帧不在上面那段
    # 就绪度里（那是**视频**就绪度），但欠费一样会让每个首帧提交白跑一次——
    # 尤其批量提交时是 N 次。逃生口同样是 force。
    if body.mode in ("first_frames", "all") and not body.force:
        from app.services.readiness import (
            CHECK_PROVIDER_HEALTH, provider_health_check,
        )

        _t2i_health = provider_health_check(db, episode.project_id, KIND_T2I)
        if not _t2i_health["ok"]:
            raise HTTPException(
                400, ENDPOINT_UNAVAILABLE_TAG + "生成前置检查未通过（1 项）："
                     + _t2i_health["message"]
                     + "。充值或稍后重试即自动解除；确认端点已恢复可勾选"
                       "「强提」强制提交")

    # 首帧抽卡张数（1-4；仅 first_frames/all 有意义）
    _ff_count = _candidate_count(body.count)

    # 出首帧前：自动补人物朝向锚点（首帧提示词的「人物位置与姿态」是 state 原文，
    # 半侧面没写朝画面哪一侧时，生图模型会随手给 3/4 正脸——2026-09-29 报障）
    anchor_fixed: list[int] = []
    if body.mode in ("first_frames", "all"):
        anchor_fixed = _repair_state_anchors(db, episode.project_id, shots)

    # 重摇原因必填（25 号 2.4）：**含重出的批次必须有原因**——先于任何提交发生，
    # 所以被拦下时零成本（首拍不要求，见 _reroll_guard）。同一份「已产出」判定
    # 既供守卫、也供 take 记录（一处口径）
    produced_ids = _produced_shot_ids(db, episode.project_id, shots, body.mode)
    reroll_reason = _reroll_guard(shots, produced_ids, body.reason, body.note)

    # 单变量锁与预算止损（批次 3 §34.2）：重拍动作落镜头日志（take 记录）；
    # 纯重摇（seed/params）同缺陷两次后强制 rewrite——第三次盲摇读不出任何
    # 信息（报告 10.3 半程止损；force 逃生口记录在案）
    changed = (body.changed or "seed").lower()
    if changed not in ("seed", "prompt", "refs", "model", "params", "both"):
        changed = "seed"

    # take 记录【延迟到提交成功后写】：旧实现在提交前无条件落库，导致「提交期
    # 故障」（端点超时/网络断连）也记一条 reroll，两次故障即把该镜的重摇预算
    # 耗尽 → 后续正常重试被「同缺陷两次盲摇」规则锁死（实测《柿子红了》镜1/
    # 镜3 因此无法重拍）。take 的语义是「一次生成动作」，提交没成功就不构成
    # 一次生成。
    def _record_takes(submitted_ids: list[str]) -> None:
        """为【确实提交成功】的镜写 take 记录（首拍记 pending，重出记 reroll 类）。

        **三种模式都写**（25 号复查：首帧重出此前被 `mode in ("videos","all")`
        挡在门外——守卫要求用户选了原因、这里却把记录丢掉，等于白问；而首帧
        恰恰是「姿态/构图不对」的高发入口）。
        """
        if not submitted_ids:
            return
        with SessionLocal() as _db:
            for _shot in shots:
                if _shot.id not in submitted_ids:
                    continue
                _take = (_db.scalar(
                    select(func.count()).select_from(ShotReview)
                    .where(ShotReview.shot_id == _shot.id)) or 0) + 1
                # 「重出」与「首拍」按**该镜是否已有产出**区分（与提交前的
                # 重摇守卫同一份判定，25 号 2.4）：旧实现把「全量提交」一律当
                # 首拍记 pending——可全量提交在已产出的镜上就是重摇，原因会被
                # 守卫要求填、却在这里被丢掉（自检 PASS 138 抓到的自相矛盾）。
                # 基建失败的 take 标记接受 pending/reroll 两种，故不影响
                # 「故障不占重摇预算」。
                _first_take = _shot.id not in produced_ids
                # 本次**自动补过朝向锚点**的镜：state 变了 ⇒ 这不再是「同参数重摇」。
                # 若不改口径会自相矛盾（25 号复查抓到的同类问题）：记录写「只换种子」
                # 却实际改了人物朝向，既污染单变量统计，又让这次重出**白占盲摇预算**
                # （明明改了东西）。按「提示词侧变了」记：changed=prompt → verdict
                # 落到 rewrite（不占盲摇预算），备注里写明改了什么。
                _anchored = _shot.seq in anchor_fixed
                _changed_eff = "prompt" if _anchored else changed
                _note_eff = body.note + (
                    "（本次同时自动补全了人物朝向锚点：state.position 已修正）"
                    if _anchored else "")
                _db.add(ShotReview(
                    project_id=episode.project_id, episode_id=episode_id,
                    shot_id=_shot.id, take_seq=_take,
                    verdict=("pending" if _first_take
                             else "reroll" if _changed_eff in ("seed", "params")
                             else "rewrite" if _changed_eff == "prompt"
                             else "edit"),
                    # 原因只记在**重出**上：首拍记 pending，用户没有"不满意"
                    # 可言（为它采原因只会稀释飞轮）
                    reason=("" if _first_take else reroll_reason),
                    # 重出靶子（2026-10-10）：盲摇预算按它分账（首帧重摇不许
                    # 吃视频预算——实测把镜的视频重出整个锁死；反之同理）
                    target=("ff" if body.mode == "first_frames"
                            else "video" if body.mode == "videos" else "all"),
                    changed=_changed_eff, evidence=_note_eff, reviewer="user",
                    origin=REVIEW_ORIGIN_SUBMIT))
            _db.commit()

    if body.mode in ("videos", "all"):
        # 预算止损只约束「定向重拍」（shot_ids 非空）——全量首拍不是盲摇，
        # 基础设施故障（进程崩溃/服务重启）的失败重试同样不占预算
        targeted = bool(body.shot_ids)
        # 「首拍」判定：该镜还没有任何视频产物。定向提交里既有「重拍」也有
        # 「给单个分镜补拍第一版」——后者不是盲摇，既不该被止损拦、也不该
        # 记成 reroll（实测：单镜补拍两次即被「已盲摇 2 次」锁死，而用户
        # 只是想把缺的镜头补上）
        no_video_ids = {
            s.id for s in shots
            if not _latest_artifact(db, episode.project_id, f"shot_{s.id}_v")}
        if targeted and changed in ("seed", "params") and not body.force:
            for shot in shots:
                if shot.id in no_video_ids:
                    continue  # 首拍不计预算
                # 只统计【真实质量重摇】：
                # ① 基础设施类失败（限流/超时/网络/内容安全误杀/配额/已计费——
                #    task_engine 在失败时把 take.changed 标为 `infra:<类别>`）
                #    不是「这一镜拍得不好」，不占用户的重摇预算；
                # ② **只数提交产生的 take**（origin=submit）：人工五级裁决
                #    （verdic 那一栏点「重摇」）是"打算重拍"的意向记录、AI 审片
                #    是模型判级，都不是用户摇过——把它们算进止损会误锁
                #    （空 origin = 本列上线前的历史记录，照旧计入，不改变既有判罚）；
                # ③ **按靶子分账**（2026-10-10）：首帧重摇（target=ff）不许吃
                #    视频预算——旧口径不区分产物，用户摇过两次首帧后视频重出
                #    被整个锁死（且那两次还是种子钳死 bug 下的无效重摇，双重
                #    伤害）；空 target = 字段上线前的历史记录，归属不可辨，
                #    按「宁可不锁」处理（本预算刚因跨产物误锁伤过用户一次，
                #    假阳性代价 > 假阴性代价）
                rerolls = db.scalar(
                    select(func.count()).select_from(ShotReview)
                    .where(ShotReview.shot_id == shot.id,
                           ShotReview.verdict == "reroll",
                           ShotReview.target.in_(("video", "all")),
                           ~ShotReview.changed.like("infra:%"),
                           ShotReview.origin.notin_((REVIEW_ORIGIN_VERDICT,
                                                     REVIEW_ORIGIN_AUTO)))) or 0
                if rerolls >= 2:
                    raise HTTPException(
                        400, REROLL_BUDGET_TAG + f"镜{shot.seq} 已盲摇 {rerolls} 次"
                             "——同缺陷第三次重摇无论结果如何都读不出信息，请修改"
                             "提示词后以「重写」方式重拍；确认放弃学习价值可强提"
                             "（确认后本次重摇照发）")

    keywords = _style_keywords(db, episode.project_id)
    # 审读产出的画幅比例透传给视频生成（16:9/9:16 等）
    meta_setting = db.get(Setting, script_meta_key(episode.project_id))
    try:
        aspect = (json.loads(meta_setting.value).get("aspect_ratio") if meta_setting else "") or "16:9"
    except (ValueError, AttributeError):
        aspect = "16:9"
    batch_id = uuid.uuid4().hex
    task_ids: list[str] = []
    skipped: list[dict] = []

    # 在途任务守卫：引擎级原语 `engine.find_active_task(project, node)` 已就位
    # （借鉴 Jellyfish 的活动任务复用）。此处**不做端点级硬拦**——批量生成与
    # 重拍本就是有意的重复提交，硬拦会误伤；防误重复的职责留给前端提交前
    # 自查（调用该原语）与任务级去重键（后续批次）。

    def submit_first_frame(shot: Shot) -> list[str]:
        # 首帧优先由导演级画面描述派生（【全局定义】含画风/素材枚举/一致性，
        # 首个时间小节即开场画面），并携带角色/场景定妆参考图——首帧的人物
        # 长相不再凭空想象；旧分镜无 video_prompt 时回退 description 组合。
        # 组装顺序（实测首帧与分镜偏离的修复）：景别/运镜紧跟全局定义前置
        # （弱信号放尾部会被场景描写淹没，特写被画成全景），场景陈设锚定用
        # 与场景主图同源的 image_prompt（description 是剧本视角环境描写，
        # 陈设细节漂移——首帧「店内」被画成店外即此因），并限定画面人物
        # 仅出场角色（首帧是分镜起始构图，出现剧本外人物即穿帮）
        gd, opening = _first_frame_parts(shot.video_prompt or "")
        # 光源句从【全局定义】拆出（归画面内容，不归素材段）：state.time 只给
        # 「什么时候」，光源句给「光从哪来、什么质感」，两者互补不可互替
        gd_light, gd_rest = _split_global_light(gd)
        scene = db.get(Scene, shot.scene_ref_id) if shot.scene_ref_id else None
        # 场景 image_prompt 落库时已按「首句原样沿用」写入了风格锚点，组装侧
        # 又要前置一次画风 → 需剥掉段首重复块（identity map 命中，无额外查询）
        _proj = db.get(Project, episode.project_id)
        _scene_anchor = anchor_prompt_for(
            _proj.style_preset,
            str(getattr(_proj, "style_anchor", "") or "")) if _proj else ""
        # 场景文本取**剥净系统块**的本体（2026-09-30：写回后 image_prompt 含空镜
        # 约束与负向词——「没有任何生物出现」进首帧会压本镜出场人物）
        _scene_body = strip_scene_system_blocks(
            (scene.image_prompt or "").strip(),
            str(getattr(scene, "layout", "") or "")) if scene else ""
        scene_text = _scene_text_for_frame(
            _scene_body
            or (scene.description or "").strip()
            or (scene.name if scene else ""), _scene_anchor) if scene else ""
        # 空间法典注入（批次 4 补充 §35.1）：场景 layout 声明出入口/门窗/陈设
        # 方位——首帧模型据此画门与通道，杜绝「从窗户进出」类空间穿帮
        scene_layout = str((scene.layout if scene else "") or "").strip()
        # 状态账本承接（批次 2c「入口态」设计）：镜头开始时的人物位置/朝向、
        # 关键道具状态、时刻光线——这三件套就是「第 0 秒静止画面」的权威描述，
        # 是首帧提示词的信息主体（state.time 光线锚——同场景连续镜的首帧
        # 色调/光线连续性凭据）
        state_pos = str((shot.state_json or {}).get("position") or "").strip()
        state_time = str((shot.state_json or {}).get("time") or "").strip()
        state_props = str((shot.state_json or {}).get("props") or "").strip()
        # ---- 首帧 prompt 组装：**固定四段 + 单真源**（20 号 §1.2 整改 1/3）----
        # 结构固定为：画风前缀（_styled_prompt 注入）→ 画面内容 → 素材与参考 →
        # 约束（全部后置）。教训两条：
        # ① 旧版按 append 顺序混排，约束块插在画面描述中间，且画风被三处重复
        #    注入（前缀 + 全局定义 + 场景文本）——实测单条首帧里画风词块出现
        #    4 次、文字禁令 3 次，约束占比中位 0.49、最高 0.99（画面内容被挤没）；
        # ② 约束与负面词散落在中段会与内容争夺注意力，统一后置到末尾。
        content: list[str] = []       # 画面内容（主体，注意力优先）
        ref_sec: list[str] = []       # 素材与参考（技术规格 + 机位 + 参考图用途）
        constraint: list[str] = []    # 约束（统一后置）
        chars = (db.query(Character).filter(
            Character.id.in_(shot.character_ref_ids_json)).all()
            if shot.character_ref_ids_json else [])
        if chars:
            # 人物锚（性别/年龄文本锚——实测弱参考模型对参考图性别遵循不稳定）
            anchors = "、".join(
                f"角色@{c.name}（{_identity_anchor(c)}）"
                if _identity_anchor(c) else f"角色@{c.name}"
                for c in chars)
            # **第三人防线**（实测形态）：模型会凭空补一个「侍女/随从/路人」来
            # 承担画面里的道具动作——镜2 明写「沈霜辞手中端着食盒」，成图却多出
            # 一个**绿衣女子端着食盒**（白衣的沈霜辞空着手），按影视套路补了个
            # 丫鬟。它与主角并**不相像**，所以「禁止同款分身/双胞胎」那句**拦不住
            # 它**——必须显式点名「不得出现名单外的人物」并列举套路角色。
            content.append(
                f"画面人物仅限：{anchors}（每人只出现一次）——**画面里只有上面"
                "这些人，不得出现任何其他人物**（侍女、随从、丫鬟、侍卫、路人、"
                "背景人影一律不出现）；**画面中的道具由列出的人物持有**，"
                "不得交给未列出的人")
            # **人物外貌逐字取自资产**（2026-09-29 用户报障「脸变了」的根因）：
            # 首帧提示词此前**没有任何人脸描述**——外观只出现在【全局定义】的
            # 「人物一致性」段，而那段是 LLM 转写的：实测把资产里的「墨发」写成
            # 「麦色长发」，并把「瓜子脸/剑眉入鬓/眼尾微挑」整段丢掉 ⇒ 模型只能
            # 靠参考图还原脸，两人同框时脸必漂。外观是**数据库里的事实**，
            # 按「凡是能算的绝不给模型」直接注入原文（上面那条去重标记保证
            # 【全局定义】里那份转写版不会同时出现——同一信息两处打架更糟）。
            _look = "；".join(
                f"{c.name}：{(c.appearance or '').strip()}"
                for c in chars if (c.appearance or "").strip())
            if _look:
                content.append(
                    f"人物外貌（**与定妆参考图一致，五官/发型/服装按此还原**）：{_look}")
            # 多人同框的**双胞胎防线**（官方指南列明的固定约束句，此前只写进了
            # 视频侧 SHOT_PROMPT）。生图模型在「两张人物参考图 + 同场景两人」时
            # 会画出同款分身——实测镜2 出过双胞胎，而首帧提示词三个版本的描述
            # 完全一致（不是提示词漂移，是模型随机性），故这里是**降低概率**而非
            # 根治：命中时重抽即可。只对 ≥2 人的镜注入（单人镜无此风险，白占配额）
            if len(chars) >= 2:
                content.append(
                    "禁止出现外形、着装、配饰完全一致的人物，禁止生成同款分身、"
                    "双胞胎效果，同一画面中仅保留单个对应人物")
            # 幽灵文字防线：年龄/国籍锚紧邻人名，生图模型会把「（男，22岁）」
            # 类标注渲染成画面内地面文字（实测「22岁中国农村青年」被画进地砖
            # 倒影）——必须紧贴锚句子以豁免声明，放约束段尾部压不住
            # 「除画面描述明确写出的文字外」不可省：本句与 _NO_TEXT_CONSTRAINT
            # 同属**排除式**——否则需要识字的镜头（碑刻/牌匾）会再次自相矛盾
            constraint.append("以上括号内为人物设定说明而非画面内容，除画面描述中"
                              "明确写出的文字外，画面中不得出现任何其他文字、数字、"
                              "年龄或姓名标注、地面文字、水印")
        else:
            # 无角色镜：强否定声明（实测只写「纯环境镜头」不足以压制；且
            # scene_text 里的「看向镜头」类措辞会让模型画出无关人像特写）。
            # **判定依据只能是「本镜有没有角色」，不能挂在 scene_text 上**：
            # scene_ref 未绑定（名单名与拆解输出不一致时被剔除）→ scene_text
            # 为空 → 旧 elif 分支整条不触发，空镜提示词里一个「不出现人物」都
            # 没有，叠加国家外貌前缀遂被画成人像特写（实测霜刃辞场01 成因）。
            content.append("画面中没有任何人物（不出现人脸/人影/背影/局部肢体，"
                           "含远处模糊人群）")
        if state_pos:
            # 措辞必须是「状态定格」而非「动作尚未发生」：state 里常含已完成
            # 的持有/姿态描述（如「右手已把手机举到胸前」）——「动作尚未发生」
            # 与之自相矛盾，模型会信字面把手中物清掉（实测镜9 手机消失）
            content.append(f"人物位置与姿态：{state_pos}"
                           "（首帧为该状态的静止定格；后续动作尚未开始）")
        if scene_text:
            content.append(_strip_caption_terms(scene_text))
        if state_props:
            content.append(f"在场道具：{state_props}")
        if state_time or gd_light:
            # 时刻（state.time）与光源（全局定义）合并成一条完整的「时刻与光线」：
            # 前者答「什么时候」，后者答「光从哪来、什么质感」。此前光源句被
            # `_GD_DUP_MARKERS` 当重复删掉，首帧只剩时间没有光（实测镜4）。
            _tl: list[str] = []
            if state_time:
                _tl.append(state_time)
            if gd_light:
                _tl.append(re.sub(r"^本镜", "", gd_light))  # 「本镜光源为…」→「光源为…」
            content.append("时刻与光线：" + "；".join(_tl))
        if not state_pos and not state_props and shot.description:
            content.append(shot.description)
        if opening and not (state_pos or state_props):
            # 句边界裁剪（20 号 §1.2 整改 2）：旧版 opening[:60] 字符切片把
            # 「…3D渲染、动画电影质感、柔和全局光照」切成「…柔）」——破碎
            # token 污染生成，且该片段还复述了一遍画风
            _opening = _strip_multicut_phrases(opening)
            _opening = clip_at_sentence(_opening, OPENING_CLIP_LIMIT)
            if _opening:
                content.append(f"（首帧定格于该时间线的起点：{_opening}）")
        # 技术规格：景别（**白名单取起点景别**——首帧与 state.position 的「镜头
        # 开始时」同框）。黑名单删运镜词会漏网「下摇」这类表外词，首帧被画成
        # 运镜终点的手部特写、人物被裁出画面、空间随之断裂（实测镜3 成因）
        if shot.camera:
            _cam = _shot_size_first(shot.camera) or _strip_camera_motion(shot.camera)
            if _cam:
                ref_sec.append(_cam + "，固定视角")
        if gd_rest:
            # 【全局定义】去重后入首帧：画风/国家/一致性声明在前文已有，整段
            # 带入等于同一信息写两遍（超长稀释注意力）。**光源句不在此**——它已
            # 由 `_split_global_light` 提出、并入画面内容的「时刻与光线」。
            # 素材行的括号说明**不在这里剥**——此刻引用尚未解析，素材行还是
            # 「角色@沈铁衣（背对镜头…）」形态、没有「（参考图N）」，正则匹配不到。
            # 真正的剥离在下方 _resolve_video_prompt 之后（见那里的注释）
            _gd_trim = _strip_multicut_phrases(_trim_global_block(gd_rest))
            if _gd_trim:
                ref_sec.append(_gd_trim)
        if chars:
            # 参考图用途说明（Agnes 官方推荐写法 + 批次 1 §31.8 保留级别枚举）：
            # 图生图会把参考图内容融进画面——实测不说明用途时模型把每张角色
            # 参考图人物都画出来（首帧多人物事故），显式限定「仅取外貌、以
            # 文字为准」压制；用途声明细化为「控制什么/不控制什么」两栏——
            # 参考图只控制身份（长相/发型/服装），不控制姿态、构图与光线，
            # 缓解参考图偶然构图污染生成
            # **把参考图的已知形态说出来**（实测的关键）：定妆图按规范是「全身
            # 正面直立、双手垂在身侧」的证件式构图，它同时携带了身份信息（要保留）
            # 与姿态构图信息（要丢掉），而模型分不开这两者——实测镜2 要求沈铁衣
            # 「伏在垛口、背对」，成图却是**正面直立**（跟参考图走了）。
            # 只说「不控制姿态」不够：模型不知道参考图**长什么样**、要改掉什么。
            # 讲明「它是什么样 → 不要照搬 → 只取什么」，模型才知道该改哪一部分。
            ref_sec.append(
                "角色参考图是**全身正面直立的定妆照**——只从中取该角色的"
                "长相、发型与服装，**不要照搬它的站姿、朝向与构图**；"
                "画面内容、人物站位、姿态与出场人物一律以本镜头文字描述为准")
            # 说明参考图的**已知版式**（2026-09-30）：基础图=角色设定图，一张图里
            # 是「头肩特写 + 正/侧/背全身」多格拼版——必须讲明它是一张设定图、
            # 只作身份与角度参照，**不得把多格里的人物当成多个人**画进画面
            ref_sec.append(
                "角色参考图是该角色的**设定图**（一张图内含头肩特写与正/侧/背全身多格）"
                "——只取该角色的长相、发型、服装与体态特征；**严禁把同一角色的多格"
                "当成多个人物**画进画面，也不要照搬其站姿与构图")
        # 约束条款合成一段（原为 4 条独立长句；合并后仍覆盖同一组事故防线，
        # 且与上方「参考图用途说明」不重复——后者已覆盖身份锚定）
        # 负面约束档位（20 号 §4.3 A/B 开关）：positive_only 下**画面类禁止句不写**
        # （官方：Seedance 对画面内容不支持负向控制，否定词反成注意力标签），
        # 只保留字幕/音频类负面词与「空间布局」的正向部分
        _positive_only = settings.prompt_negative_mode == "positive_only"
        # 空间布局只在**中景及更宽**的景别注入：近景/特写取景太紧，方位声明
        # 既不可执行又挤占画面内容配额（实测近景双人镜里塞进「城门位于城墙
        # 中部，朝向外敌方向…城墙占据画面下方三分之二」近百字）。**结构禁令
        # 与景别无关**（任何景别都不许穿墙/悬空），始终保留。
        _layout_wide = _shot_size_first(shot.camera) in _WIDE_SHOT_SIZES
        _layout_part = (f"空间布局：{scene_layout}；"
                        if (scene_layout and _layout_wide) else "")
        _struct_part = ("人物只能经由门/通道进出，不得穿越门窗玻璃与墙面，双脚踩地；"
                        if scene_layout else "")
        if _positive_only:
            _cons = "【约束】" + _layout_part + _struct_part
        else:
            _cons = ("【约束】单一机位固定视角，不出现别处场景或拼贴；"
                     + _layout_part + _struct_part)
        if chars and not _positive_only:
            # 反双胞胎（官方指南列明的已知弱点）：此前只进了 video_prompt，
            # 首帧缺失导致背景复制人物（实测镜2 柜台边多出第二女性、镜3 门边
            # 双胞胎）——「每人只出现一次」不足以压制，固定句逐字写入
            _cons += ("禁止出现外形、着装、配饰完全一致的人物，"
                      "禁止生成同款分身、双胞胎效果；")
            # 背景点名清空：agnes 对空间定位的遵循好于抽象人数约束——把复制
            # 人物的高发区逐一点名为空，比「只出现三人」更可执行（实测闸机后/
            # 桌后复制人物多轮重摇不清）。措辞保持**场景无关**：原先写死了
            # 「玻璃幕墙与镜面反光」等现代室内物件，对雪夜城头这类场景是纯废字
            # （一个都不在场），且诱导模型往室内陈设上想。
            _cons += ("背景（门后、陈设后方、反光面中）为空旷环境，"
                      "不得出现任何人影；")
        # 文字禁令：官方明确「可负向控制字幕与音频」，两个档位都保留。
        # **排除式**措辞——不与「画面描述要求出现某文字」冲突（见常量注释）
        _cons += _NO_TEXT_CONSTRAINT
        constraint.append(_cons)
        # 四段结构的分段字数（供 prompt_quality 做分段度量）：首帧没有五段式
        # 那类段名标记，成品文本反推不出分段，只能在这里显式记录。**约束占比
        # 偏高的根因排查全靠它**——能直接看出「画面内容被挤掉多少」。
        _seg_report = {
            "画面内容": len(_join_parts(*content)) if content else 0,
            "素材与参考": len(_join_parts(*ref_sec)) if ref_sec else 0,
            "约束": len(_join_parts(*constraint)) if constraint else 0,
        }
        core = _join_parts(*(content + ref_sec + constraint))
        # 舞台指示符「△」（导演级描述的动作标记）对生图模型是纯噪音，
        # 弱模型可能画成三角符号——移除（含紧随空白）
        core = re.sub(r"△\s*", "", core)
        refs: list[str] = []
        # @引用解析：只要文本里有「角色@/场景@/道具@」前缀就解析（首帧旧数据
        # 无 video_prompt 时人物锚句同样带「角色@」——旧实现要求 gd 非空，
        # 这类首帧的参考图编号不会绑定，标注悬空）
        if gd or "@" in core:
            core, mentioned = _resolve_video_prompt(
                db, episode.project_id, shot, core, max_refs=_t2i_ref_budget(db, episode.project_id))
            refs.extend(mentioned)
            # **素材行的括号说明在这里剥**（必须晚于引用解析）：解析前素材行是
            # 「角色@沈铁衣（背对镜头，俯身伏在垛口）」，还没有「（参考图N）」，
            # 正则无从匹配；解析后成了「沈铁衣（参考图2）（背对镜头…）」才可剥。
            # 这些说明是冗余（外形/陈设/形态由 character_briefs 等单独提供），
            # 且**有害**：紧贴「（参考图N）」会被读成「这张参考图的描述」，与画面
            # 内容段的取景语言直接矛盾——实测镜3 就因此变回正面直立。
            core = _strip_ref_notes(core)
        prompt = _styled_prompt(keywords, core)
        # 负向词按本镜有没有角色取套（2026-09-30）：无角色空镜用场景套，
        # 否则「避免皮肤毛孔纹理」这类人物词是纯噪音
        style_neg = _style_negative(db, episode.project_id,
                                    "character" if chars else "scene")
        if style_neg:
            prompt = f"{prompt}，{style_neg}"
        # 远景/全景首帧同样追加降噪与景深约束
        prompt = _anti_noise_suffix(shot.camera, prompt)
        # 未在描述中提及的出场角色/场景/道具：按优先级补齐参考图（去重保序，
        # 补齐同样尊重预算——超出部分静默丢弃，不产生悬空标注）
        for artifact in _extra_ref_artifacts(db, episode.project_id, shot):
            if artifact not in refs and len(refs) < _t2i_ref_budget(db, episode.project_id):
                refs.append(artifact)
        # 补充视角用途说明（k5）：该参考图是**同场景的另一视角**而非本镜机位的
        # 画面——不说明用途时模型可能把两个视角的画面拼在一起（同「角色参考图
        # 仅作身份参考」的教训）。未挂图时不写，避免悬空说明。
        _vref = str((shot.state_json or {}).get("view_ref") or "").strip()
        if _vref in SCENE_EXTRA_VIEWS and scene is not None:
            _vart = _latest_artifact(
                db, episode.project_id, f"scene_{scene.id}_view_{_vref}")
            if _vart and _vart in refs:
                ref_sec.append(
                    f"其中{SCENE_EXTRA_VIEW_LABELS[_vref]}图仅用于核对本场景的"
                    "空间结构与陈设方位（另一侧/局部视角），**不作为画面构图**；"
                    "本镜取景与画面内容一律以文字描述与场景主图为准")
        # 首帧精炼上下文（23 号批次 3）：draft 就是**最终 prompt**（含画风前缀与
        # 已解析的参考图标注）——runner 里拿到的正是它，两侧必须同形态，护栏才
        # 比得准（踩过：一度按未解析的「角色@X」设计护栏，而 runner 里根本没有
        # 那个形态 → 初稿零引用 / 模型多出一堆 → 5 镜全部误判为不通过）。
        # 返回空 dict = 本镜不精炼（开关关 / 无 video_prompt / 初稿过短 /
        # LLM 不可用，四种情形都在 build_refine_context 里判）。
        # 实际精炼在 runner 内执行（每镜一次 LLM 调用，放这里会阻塞批量提交）。
        _refine_ctx = build_refine_context(
            db, episode.project_id, shot, prompt,
            picture_chars=_seg_report["画面内容"])
        refs = list(refs)
        input_refs: dict = {"extra_images": refs} if refs else {}
        # 提交摘要落日志（backend.log）：完整参数以 runner 侧 generation 记录为准
        logger.info("首帧提交 shot=%s seq=%s refs=%d model=%s",
                    shot.id, shot.seq, len(refs),
                    _resolve_model(db, episode.project_id, KIND_T2I, ""))
        submitted = engine.submit(
            project_id=episode.project_id, node_id=f"shot_{shot.id}_ff",
            runner_name="t2i",
            params={"prompt": prompt, "aspect": aspect,
                    "prompt_segments": _seg_report,
                    # 一次抽 N 张（仅首帧；视频侧不支持多候选）：每张各落一个版本，
                    # 用户在分镜卡上「设为首帧」挑定
                    **({"count": _ff_count} if _ff_count > 1 else {}),
                    # 精炼上下文（runner 侧消费；空 dict 表示不精炼）。
                    # 下划线前缀键不进「最终发给模型的参数」记录，见 runner 侧注释
                    **({REFINE_PARAM_KEY: _refine_ctx} if _refine_ctx else {})},
            input_refs=input_refs, batch_id=batch_id,
            # 未显式选模型：走项目预设（t2i），再兜底 runner default_model
            model_id=_resolve_model(db, episode.project_id, KIND_T2I, ""),
            # 国家外貌约束只对**有角色的镜**注入：其措辞是「中国人，东亚面孔，
            # 黑发深棕色眼睛，亚洲人的五官与肤色特征」，本质是在描述一张脸——
            # 前置到空镜提示词开头会诱导模型画出人像特写（任务引擎侧对视频类
            # 与场景图/道具图已同款关闭，分镜首帧此前漏掉，实测霜刃辞场01 空镜
            # 被画成男子面部特写的直接成因）
            inject_country_hint=bool(chars),
        )
        ids = [submitted["task_id"]]
        # 多切第二关键帧（批次 4 §35.2）：cuts[1] 的 offset 对应时间小节为
        # 起点投影——切点图独立 node（shot_{id}_ff_c2），视频提交按对齐句注入
        cuts = shot.cuts_json or []
        if len(cuts) >= 2 and (cuts[1].get("offset") or 0) > 0:
            gd2, opening2 = _first_frame_parts(
                shot.video_prompt or "", offset=int(cuts[1]["offset"]))
            if gd2 or opening2:
                # 与主首帧同构：画面内容前置 + 约束合块（见主首帧组装注释）
                parts2: list[str] = []
                if opening2:
                    parts2.append(f"切点画面（第 {cuts[1]['offset']} 秒起）："
                                  f"{cuts[1].get('description') or ''}")
                    # 句边界裁剪（同主首帧整改 2）：原为 opening2[:80] 字符切片
                    parts2.append("（该时刻的镜头状态："
                                  f"{clip_at_sentence(opening2, CUT_OPENING_CLIP_LIMIT)}）")
                else:
                    parts2.append(f"切点画面（第 {cuts[1]['offset']} 秒起）："
                                  f"{cuts[1].get('description') or ''}")
                parts2.append("【约束】单一机位固定视角，不出现别处场景或拼贴；"
                              "人物只能经由门/通道进出，不得穿越门窗玻璃与墙面；"
                              + _NO_TEXT_CONSTRAINT)
                if gd2:
                    parts2.append(gd2)
                core2 = _join_parts(*parts2)
                core2, refs2 = _resolve_video_prompt(
                    db, episode.project_id, shot, core2, max_refs=_t2i_ref_budget(db, episode.project_id))
                prompt2 = _styled_prompt(keywords, core2)
                if style_neg:
                    prompt2 = f"{prompt2}，{style_neg}"
                prompt2 = _anti_noise_suffix(shot.camera, prompt2)
                # 主帧参考图并入同样尊重预算（超出部分丢弃，无悬空标注）
                refs2.extend(a for a in refs
                             if a not in refs2 and len(refs2) < _t2i_ref_budget(db, episode.project_id))
                input_refs2 = {"extra_images": refs2} if refs2 else {}
                sub2 = engine.submit(
                    project_id=episode.project_id,
                    node_id=f"shot_{shot.id}_ff_c2",
                    runner_name="t2i",
                    params={"prompt": prompt2, "aspect": aspect},
                    input_refs=input_refs2, batch_id=batch_id,
                    model_id=_resolve_model(db, episode.project_id, KIND_T2I, ""),
                )
                ids.append(sub2["task_id"])
        return ids

    def submit_video(shot: Shot) -> str | None:
        ff_artifact = _first_frame_artifact(db, episode.project_id, shot)
        if not ff_artifact:
            skipped.append({"shot_id": shot.id, "seq": shot.seq,
                            "reason": "无首帧"})
            return None
        # 占位首帧不进付费视频（2026-09-30 复查 R6）：抽帧失败留下的 fallback 帧
        # 或 mock 图当首帧送去云端视频模型 = 花钱拿垃圾。按「跳过 + 说明原因」
        # 处理（与「无首帧」同一出口，不中断整批）；mock 模式（开发联调）不拦。
        if not settings.mock_mode and _is_placeholder_artifact(db, ff_artifact):
            skipped.append({"shot_id": shot.id, "seq": shot.seq,
                            "reason": "首帧是占位图（抽帧失败或 mock 产物），"
                                      "先重出首帧再生成视频"})
            return None
        # 机会式链式衔接（continue 转场）：上一镜视频已完成且本镜首帧尚未
        # 对齐到它时，抽上一镜尾帧追加为本镜首帧新版本并以其起像（首尾帧
        # 连续转场；幂等同 link_frames，fallback 占位帧不采用）。全量批量
        # 时上一镜视频未出、此处自然跳过——主要惠及逐镜重生成/续生成流。
        if shot.transition_in == "continue":
            prev = prev_map.get(shot.id)
            if prev is not None:
                src_node = f"shot_{prev.id}_v"
                src_artifact = latest_video_artifact_id(
                    db, episode.project_id, src_node)
                if src_artifact and not has_frame_version_for(
                        db, episode.project_id, f"shot_{shot.id}_ff",
                        src_artifact):
                    result = extract_frame_version(
                        db, episode.project_id, src_node,
                        f"shot_{shot.id}_ff", position="last",
                        duration_hint=float(prev.duration))
                    if result and not (result[1].meta_json or {}).get("fallback"):
                        ff_artifact = result[1].id
        # 画面描述优先用导演级 video_prompt（@提及 → 图N 引用 + 有序参考图）；
        # video_prompt 的时间小节里已含台词原句，不再重复拼接 dialogue（否则
        # 台词在提示词中出现两次，视频模型可能让角色把台词念两遍）——但 LLM
        # 漏写台词时不能跟着丢，逐条校验关键台词缺失则回退补拼一次
        if (shot.video_prompt or "").strip():
            # 该出场却没引用的素材：**提交前确定性补上**（引用是绑定参考图的
            # 依据，而「漏没漏」是算得出来的——不靠重算：实测 6 镜重算后仍有
            # 同类漏写，还新引入台词密度超载）
            _vp_used = _ensure_material_refs(
                db, episode.project_id, shot, shot.video_prompt)
            # 台词密度超载：**确定性重平衡小节边界**（往邻居借时间让台词节达标）。
            # 实测带清单的重算会调整划分但不给台词节扩容（镜11 从 3 节切成 4 节，
            # 台词节仍是 3 秒 20 字），密度是算术、该由代码保证。
            _vp_used = rescale_beat_marks(_vp_used, int(shot.duration or 0))
            _vp_used = rebalance_beat_marks(
                _vp_used,
                [d.get("text") or "" for d in (shot.dialogue_json or [])])
            # 参考图预算取**端点能力**（agnes flash 5 / 2.5 pro 8，首帧另占 1 位）
            # ——此前硬编码 `< 4`：那个 4 是按 flash 手算的，于是 pro 档实际能收
            # 8 张却只送 4 张，**白扔一半角色/场景锚定**（25 号 1.6）。
            # **必须在占号解析之前算**：@引用→（参考图N）的编号分配也吃这个预算，
            # 算晚了会出现「占号只认前 4 个、补齐却按 7 个塞」的两套口径。
            _prov_ref, _model_ref = _video_endpoint(
                db, episode.project_id, body.model_id)
            _vref_budget = video_ref_budget(_prov_ref, _model_ref)
            prompt, mentioned = _resolve_video_prompt(
                db, episode.project_id, shot, _vp_used, max_refs=_vref_budget)
            extra = list(mentioned)
            # 未在描述中提及的出场角色/场景：按妆造优先级补齐参考图（去重保序）。
            # 台词覆盖校验：台词原句完全不在描述中时补拼（归一化比对，
            # 容忍 LLM 对台词的小幅排版差异——只拦整条丢失）
            missing_lines = [
                f"{d.get('speaker', '')}：{d.get('text', '')}"
                for d in (shot.dialogue_json or [])
                if d.get("text") and not _dialogue_in_prompt(
                    str(d["text"]), prompt)
            ]
            if missing_lines:
                # 补进【视频画面】段内而非全文末尾——末尾是【安全合规】，
                # 台词贴禁令串的形态见 `_inject_missing_dialogue` 的注释
                prompt = _inject_missing_dialogue(prompt, missing_lines)
            # 多切注入（批次 4 §35.2）：二切关键帧追加为末位参考图（agnes
            # 首帧 append 到 images 末尾，追加位编号 = len(extra)+1 无冲突）
            # + 切点对齐句——「第 N 秒起画面与参考图 M 一致」
            cuts = shot.cuts_json or []
            if len(cuts) >= 2 and (cuts[1].get("offset") or 0) > 0:
                c2_art = _latest_artifact(db, episode.project_id,
                                          f"shot_{shot.id}_ff_c2")
                if c2_art and c2_art not in extra and len(extra) < _vref_budget:
                    extra.append(c2_art)
                    prompt += (f"\n第 {cuts[1]['offset']} 秒起画面切换为与"
                               f"参考图{len(extra)}完全一致的人物与构图"
                               "（切点目标画面，衔接前后两段）")
        else:
            # 旧分镜无 video_prompt：回退 description + 台词的组合
            dialogue = _join_parts(*[
                f"{d.get('speaker', '')}：{d.get('text', '')}"
                for d in (shot.dialogue_json or [])
            ])
            prompt = _join_parts(shot.description, dialogue)
            extra = _extra_ref_artifacts(db, episode.project_id, shot)
        # 端点解析**一次、两处共用**（脱标注判定 + 生成前校验），且必须用
        # 「实际提交的那个模型」而非全局默认端点：此前脱标注读全局默认、
        # 校验读 body.model_id，与实际提交的 model_id 都可能不是同一端点。
        submit_model = _resolve_model(db, episode.project_id, KIND_T2V,
                                      body.model_id)
        # 端点与**实际模型**一起解析（第二返回值此前被丢弃）：同一端点下代际
        # 不同的模型能力不同（ark 上 2.0 不响应精确时间戳、2.5 响应）
        _provider, _ep_model = "custom", ""
        try:
            from app.core.providers import resolve_runtime_config as _rrc

            _ep_cfg, _ep_model = _rrc("t2v", submit_model or "")
            _provider = getattr(_ep_cfg, "name", "") or "custom"
            # 非编码串时第二返回值为空——模型名在端点配置里
            _ep_model = _ep_model or (getattr(_ep_cfg, "model", "") or "")
        except Exception:  # noqa: BLE001 配置解析失败交提交层兜底
            pass
        # 端点能力对齐（审计 13 号 F）：方舟有首帧时整段丢弃参考图，而提示词里
        # 已写入「（参考图N）」标注——脱去编号（保留名称）并说明以首帧为准，
        # 避免模型面对不存在的图号；丢弃数量随 generation 记录供「查看参数」核对
        dropped_refs = 0
        mention_stripped = 0
        if _video_provider_drops_refs(_provider) and ff_artifact:
            dropped_refs = len(extra)
            prompt = _strip_ref_labels(prompt)
            # 参考图不存在了：把「与参考图一致」改指到首帧，否则一致性约束悬空
            # （实测视频中途给角色加上首帧没有的眼镜）
            prompt = _retarget_ref_to_frame(prompt)
            # 未绑定的「角色@名称」同样要脱前缀：预算为 0 的端点（如只收首帧的
            # Vidu）没有任何引用能进占号，内部标记语会原样漏进模型提示词
            prompt, mention_stripped = _strip_mention_prefixes(prompt)
            prompt = _join_parts(
                prompt,
                "人物长相、服装与场景陈设一律以本镜首帧画面为准"
                "（本端点以首帧保真模式生成，不额外接收参考图）")
            extra = []
        # 时间锚点对齐：端点不响应精确时间戳时剥离行首秒数（保留动作描述与
        # 段落顺序）——秒数只服务内部时序对账与首帧派生，对这类模型是官方
        # 点名的不稳定输入（见 _video_provider_timestamps）
        stripped_beats = 0
        if not _video_provider_timestamps(_provider, _ep_model):
            prompt, stripped_beats = _strip_time_anchors(prompt)
        # ComfyUI/H3 端点：参考图占位改写（34 号，2026-10-10 用户实测纠正）——
        # 「（参考图N）」改成**字面** <Picture N>，N = 给 ComfyUI 的图片参数
        # 下标（0 起）+ 1（挂载顺序：参考图在前、首帧挂末位，故与「参考图N」
        # 同号）；有首帧时前置起始画面声明。**必须改在组装侧而不是 provider 层**：
        # 参数弹窗（generation 记录）存的就是这份组装完成的提示词，在 provider
        # 里补说明句会让弹窗与调用日志不一致（用户实测发现的问题形态）。
        picture_note = ""
        if _provider == "comfyui":
            from app.core import comfyui as _comfyui_mod

            _pic_total = len(extra) + (1 if ff_artifact else 0)
            if _pic_total:
                _before_prompt = prompt
                prompt = _comfyui_mod.finalize_prompt(
                    prompt, picture_total=_pic_total,
                    has_first_frame=bool(ff_artifact))
                if prompt != _before_prompt:
                    picture_note = ("参考图占位已改为 <Picture N>"
                                    f"（N = 图片序号，本镜共 {_pic_total} 张"
                                    + ("；首帧挂末位并声明为起始画面）"
                                       if ff_artifact else "）"))
        # 系统改动过提示词的地方逐条外露（随 generation.params 进「查看参数」）
        # ——「参数被改了而用户不知道」是本项目反复出现的缺陷形态
        prompt_notes: list[str] = []
        if dropped_refs:
            prompt_notes.append(
                f"已脱去 {dropped_refs} 处「参考图N」标注"
                "（本端点有首帧时不接收参考图）")
        if mention_stripped:
            prompt_notes.append(
                f"已脱去 {mention_stripped} 处「角色@/场景@/道具@」类型前缀"
                "（本端点不接收参考图；名称逐字保留）")
        if stripped_beats:
            prompt_notes.append(
                f"已剥离 {stripped_beats} 处秒数锚点"
                "（本端点不响应精确时间戳）")
        if picture_note:
            prompt_notes.append(picture_note)
        # 画风前缀（含风格锚点全称）：**视频侧同样由代码注入**（20 号 §1.2 整改 1）。
        # 此前画风由 LLM 写在【全局定义】里，现已从该段移除（消除重复）——若不在此
        # 补注入，视频提示词会整体丢风格（首帧有、视频没有，跨阶段风格漂移）。
        # 与 t2i 共用 `_styled_prompt`，保证两阶段风格限定词同源同形。
        prompt = _styled_prompt(keywords, prompt)
        # 远景/全景镜头追加降噪与景深约束（防远景补噪点）
        prompt = _anti_noise_suffix(shot.camera, prompt)
        input_refs: dict = {"image": ff_artifact}
        if extra:
            input_refs["extra_images"] = extra
        if dropped_refs:
            logger.info("视频端点不回传参考图，已脱去 %s 处编号标注 shot=%s",
                        dropped_refs, shot.id)
        # 生成前能力校验（借鉴 Jellyfish 的 fail-loud，见 core/providers.py）：
        # 时长/画幅/参考图张数超出端点能力时**明确报错**，不再静默钳位——
        # 用户改过的参数被悄悄改掉比直接报错更糟（审计 14 号的多条缺陷同源）
        from app.core.providers import validate_video_request

        _problems = validate_video_request(
            _provider, duration=int(shot.duration or 0), aspect=aspect,
            ref_count=len(extra),
            has_first_frame=bool(ff_artifact))
        if _problems:
            raise HTTPException(
                400, f"镜{shot.seq} 生成参数超出端点能力：{'；'.join(_problems)}"
                     "。请调整分镜时长/画幅或改用支持该参数的模型")
        # 分辨率档（项目设置）：随 params 传给 runner，由端点按自身能力映射
        _proj = db.get(Project, episode.project_id)
        _res = (getattr(_proj, "video_resolution", "") or
                DEFAULT_VIDEO_RESOLUTION)
        _w, _h = _video_size(aspect, _res)
        video_params = {"prompt": prompt, "duration": shot.duration,
                        "aspect": aspect, "width": _w, "height": _h,
                        "resolution": _res}
        if prompt_notes:
            # 下划线前缀 = 记录元数据：runner 侧单列，不混进生成参数
            video_params["_prompt_notes"] = prompt_notes
        submitted = engine.submit(
            project_id=episode.project_id, node_id=f"shot_{shot.id}_v",
            runner_name=RUNNER_REF2V,
            # 视频预设键统一 t2v（ref2v 等视频类 runner 共用该类别）
            model_id=submit_model,
            params=video_params,
            input_refs=input_refs, batch_id=batch_id,
        )
        return submitted["task_id"]

    # 成功提交的镜集合（take 记录只认它——提交失败的镜不消耗重摇预算）
    _submitted_shot_ids: set[str] = set()

    def _safe_submit(shot: Shot, submit_fn) -> list[str]:
        """逐镜容错提交：单镜失败不中断整批（失败镜进 skipped 并记原因）。

        注意：submit_first_frame 返回 list[str]，submit_video 返回 str | None——
        此处统一收敛为 list（直接把字符串 extend 进 task_ids 会被拆成单字符）。
        """
        try:
            raw = submit_fn(shot)
        except HTTPException as e:
            skipped.append({"shot_id": shot.id, "seq": shot.seq,
                            "reason": str(e.detail)[:160]})
            return []
        except Exception as e:  # noqa: BLE001
            skipped.append({"shot_id": shot.id, "seq": shot.seq,
                            "reason": str(e)[:160]})
            return []
        if not raw:
            return []
        ids = [raw] if isinstance(raw, str) else list(raw)
        _submitted_shot_ids.add(shot.id)
        return ids

    if body.mode in ("first_frames", "all"):
        for shot in shots:
            task_ids.extend(_safe_submit(shot, submit_first_frame))
    if body.mode in ("videos", "all"):
        for shot in shots:
            task_ids.extend(_safe_submit(shot, submit_video))

    # 只给「确实提交成功」的镜写 take；一个都没提交成功则不落任何记录。
    # **三种模式都写**（含 first_frames）：首帧重出同样是重摇，原因必须落库
    _record_takes(_submitted_shot_ids)

    return {"batch_id": batch_id, "task_ids": task_ids, "skipped": skipped,
            # 出首帧前自动补过朝向锚点的镜号（前端据此提示用户「已自动修正」）
            "anchor_fixed": anchor_fixed}


# ---------- 预检与审片（确定性检查，只产出报告不改数据；设计 6.10.2 / E2） ----------

# 运镜方向词（轴线抽查：同场景相邻镜头左右方向冲突即越轴观感）
def _camera_direction(camera: str) -> str | None:
    """从 camera 提取左右方向（左/右）；无方向词返回 None。"""
    text = camera or ""
    if "左" in text:
        return "左"
    if "右" in text:
        return "右"
    return None


# 冻结关键帧卫生门（批次 1 §31.4）：首小节命中进行/突变时体词 → 首帧画成
# 动作进行中（弱启发告警，允许人工豁免——台词原句含这些词属合法）
_ASPECT_MARK_RE = re.compile(r"正在|忽然|突然|已经|随后|顿时")


def _style_negative(db: Session, project_id: str | None,
                    kind: str = "character") -> str:
    """风格负面词（**按「画风 × 资产类型」取**——角色/场景/道具各一套）。

    2026-09-30 用户规则：一套词打天下会出现「场景图禁皮肤毛孔」这类无效约束
    （场景空镜里没有人，写「避免皮肤毛孔纹理」纯属噪音，还会按「否定词=注意力
    标签」的官方口径反向诱导）。现在拆成三套：
      - `character`：人物类（角色图/设定图/分镜首帧）——含皮肤/五官/脸型词；
      - `scene`：场景空镜——只留画风通用词（如「真人照片质感、实拍摄影」）；
      - `prop`：道具物件——同场景，另含「塑料感」等物件质感词。
    `kind` 取不到时回退 `character`（老数据/新类型兼容）。

    用于 t2i 组装尾部的「避免：」句——端点无负向词字段时下沉进正文仍生效；
    本地管线的 negative_prompt 参数继续承载肢体类负向词，两者正交。

    **A/B 开关**（20 号 §4.3）：`AVS_PROMPT_NEGATIVE_MODE=positive_only` 时返回空串
    ——风格负面词是典型的「画面类否定」，官方口径下会成为注意力标签；对照组据此
    彻底去掉画面类负向词（字幕/音频类负面词另在约束块保留）。
    """
    if settings.prompt_negative_mode == "positive_only":
        return ""
    preset = ""
    if project_id:
        project = db.get(Project, project_id)
        preset = (project.style_preset if project else "") or ""
    info = load_style_presets().get(preset, {})
    negatives = info.get("negatives") or {}
    neg = negatives.get(kind) or negatives.get("character") \
        or info.get("negative", "")          # 最后一项兼容旧格式预设文件
    return f"避免：{neg}" if neg else ""


# ---------- 场景/道具：组装写回提示词框（2026-09-30 用户规则） ----------
#
# 规则：发给模型的提示词必须在**生成阶段组装好、写回提示词框**（界面所见即所发），
# 不许在调用接口时暗地追加。三个必须堵的副作用（改这里前先读）：
#   ① 场景框里的系统块会被分镜首帧取用 → `strip_scene_system_blocks` 剥干净再取；
#   ② 「AI 优化提示词」把框里整段当素材重写 → `prompt_optimize` 侧同样先剥后送；
#   ③ 重新解析会重建 image_prompt（干净）→ 下次生成时组装函数再补回，写回可见。


def _project_character_names(db: Session, project_id: str | None) -> list[str]:
    """项目角色名单（只返回 **≥2 字** 的名字，给「按名单剔句」用）。

    单字名做子串匹配会误伤普通词（「林」出现在「树林」里），宁缺毋滥——
    单字名角色由解析侧提示词规则约束（见 `prompts.strip_figure_clauses`）。
    """
    if not project_id:
        return []
    return [n for n in db.scalars(select(Character.name).where(
        Character.project_id == project_id)).all()
        if n and len(n.strip()) >= 2]


def _clean_scene_layout(db: Session, scene) -> str:
    """场景「空间布局」的**可注入版本**：剔掉混入的人物走位句（名单对账）。

    实测泄漏（2026-10-02 用户反馈，场景图里出现了妈妈）：解析把人物调度写进了
    layout（「…厨房门洞与布帘在左后方，妈妈可站在布帘后探头看向沙发」「小杰坐
    对面背朝房门」），而 layout 是原样拼进生图提示词的。名字取自项目角色表
    （解析落库的语义结果）——剔句是确定性对账，不猜同义词、不做模糊匹配。

    注入（compose）与剥离（`_scene_base_text`）共用本函数，新老数据的
    「所见即所发」都靠它成立。
    """
    layout = str(getattr(scene, "layout", "") or "").strip()
    if not layout:
        return ""
    layout = prompts_mod.strip_figure_clauses(
        layout, _project_character_names(db, getattr(scene, "project_id", None)))
    return prompts_mod.strip_scene_figures(layout)


def _scene_base_text(db: Session, scene) -> str:
    """场景提示词里的**画面描述本体**（剥系统块 + 段首画风前缀）→ 给下游用。

    下游要干净描述的三处：分镜首帧取场景文本、补充视角的 LLM 重写输入、
    AI 优化的当前值（后两者在别处调用 `prompt_blocks` 的同名剥离）。
    """
    layout = str(getattr(scene, "layout", "") or "").strip()
    # 布局注入的是**清洗版**（人物走位句已剔）：老文本里注入的是清洗前旧句——
    # 两版当针同一次调用内都试完整剥离（`layout_alt`），迁移期幂等靠它
    clean_layout = _clean_scene_layout(db, scene)
    text = strip_scene_system_blocks(
        str(getattr(scene, "image_prompt", "") or ""), layout,
        clean_layout if clean_layout != layout else "")
    # 场景组注入的两段（同空间环境设定 / 参考图继承说明句）也要剥掉——下游
    # （首帧、补视角重写、AI 优化）要的是干净画面描述，不背组的约束
    text = strip_group_blocks(text)
    # 段首画风前缀（写回后才会有；模型自己写的画风句由 _strip_leading_style 兜）
    kw = _style_keywords(db, getattr(scene, "project_id", None))
    if kw:
        stripped = text.lstrip()
        if stripped.startswith(kw):
            text = stripped[len(kw):].lstrip("，,；;、 ")
    # 人物名整句剔除（角色名单对账）：老数据里混进「小杰坐在桌前…」这类走位句
    # 时，下游（首帧场景文本/补视角重写/AI 优化）仍拿到纯环境描述
    text = prompts_mod.strip_figure_clauses(
        text, _project_character_names(db, getattr(scene, "project_id", None)))
    return text.strip().strip("，,；;、").strip()


def _scene_group_lock(db: Session, project_id: str | None) -> bool:
    """项目开关「组内环境锁定（用母版图）」：关掉 = 只用组环境设定文本打底。"""
    if not project_id:
        return True
    project = db.get(Project, project_id)
    return bool(getattr(project, "scene_group_lock", True)) if project else True


def _scene_has_artifact(db: Session, scene, asset_id: str) -> bool:
    """该场景的抽卡版本里是否有这张产物（用于判断「母版是不是它自己」）。"""
    return bool(db.scalar(select(func.count()).select_from(NodeVersion).where(
        NodeVersion.node_id == f"scene_{scene.id}",
        NodeVersion.artifact_id == asset_id)))


def _scene_group_anchor(db: Session, scene) -> str:
    """该场景所属组的**环境母版产物 id**（"" = 无母版可用 / 自己就是母版 / 开关关）。

    取法（确定性，不落状态；32 号方案）：
    1. 组显式指定的 `anchor_asset_id` 优先（界面「换母版」写它）；
    2. 未指定时取**组内首个已有产物的成员**（按创建顺序）——「组内首张生成好的
       场景图自动当母版」因此天然成立，不需要任何写回钩子；
    3. 母版就是本场景自己时返回空（拿自己的旧图当参考会让重出趋向旧图）；
    4. 项目开关关掉时返回空（只保留文本打底）。
    """
    gid = str(getattr(scene, "group_id", "") or "")
    if not gid:
        return ""
    group = db.get(SceneGroup, gid)
    if group is None or not _scene_group_lock(db, getattr(scene, "project_id", None)):
        return ""
    explicit = str(group.anchor_asset_id or "").strip()
    if explicit:
        return "" if _scene_has_artifact(db, scene, explicit) else explicit
    members = db.scalars(select(Scene).where(Scene.group_id == gid).order_by(
        Scene.created_at, Scene.id)).all()
    for member in members:
        art = _latest_artifact(db, getattr(scene, "project_id", ""),
                               f"scene_{member.id}")
        if art:
            return "" if member.id == scene.id else art
    return ""


def compose_scene_prompt(db: Session, scene) -> str:
    """组装场景主图的**完整提示词**并写回 `scene.image_prompt`（幂等）。

    顺序：画风前缀 → 组环境设定（32 号，组内共享）→ 空镜约束 → 空间布局 →
    画面描述 → 参考图继承说明句（有母版可参考时）→ 风格负向词（场景专用那套）。
    幂等依据：画风看段首、组块/布局/负向词先剥后拼、空镜看「空镜头」锚词——
    重复生成不叠加；写回由调用方 commit。
    """
    base = _scene_base_text(db, scene)
    if not base:
        base = str(getattr(scene, "description", "") or "").strip() or scene.name
    base = prompts_mod.strip_scene_figures(base)
    # 人物名整句剔除兜底：description 回退来的底文也可能含走位句（「小杰坐在
    # 桌前拿着笔…」），场景主图是纯环境空镜，一个人名都不许留
    base = prompts_mod.strip_figure_clauses(
        base, _project_character_names(db, getattr(scene, "project_id", None)))
    layout = _clean_scene_layout(db, scene)
    group = db.get(SceneGroup, scene.group_id) if getattr(scene, "group_id", "") else None
    env = (group.env_setting or "").strip() if group is not None else ""
    anchor = _scene_group_anchor(db, scene)
    parts: list[str] = []
    if "空镜头" not in base:
        parts.append(prompts_mod.SCENE_IMAGE_CONSTRAINTS)
    if layout:
        parts.append(f"空间布局：{layout}；")
    parts.append(base)
    if anchor:
        parts.append(GROUP_ENV_REF_NOTE)
    neg = _style_negative(db, getattr(scene, "project_id", None), "scene")
    if neg:
        parts.append(neg)
    # 组环境设定单独成句（末尾补句号当**剥离边界**——env 文本里的句号换成逗号，
    # 保证剥离时精确到这一句，不吞后面内容）
    head = ""
    if env:
        head = f"{GROUP_ENV_LABEL}{env.replace('。', '，').strip('，')}。"
    prompt = _ensure_scene_guarantee(_styled_prompt(
        _style_keywords(db, getattr(scene, "project_id", None)),
        head + _join_parts(*parts)))
    if (getattr(scene, "image_prompt", "") or "") != prompt:
        scene.image_prompt = prompt
    return prompt


def compose_prop_prompt(db: Session, prop) -> str:
    """组装道具图的**完整提示词**并写回 `prop.image_prompt`（幂等；道具无固定约束块）。

    顺序：画风前缀 → 画面描述 → 风格负向词（道具专用那套）。
    """
    text = strip_prop_system_blocks(str(getattr(prop, "image_prompt", "") or ""))
    kw = _style_keywords(db, getattr(prop, "project_id", None))
    if kw:
        stripped = text.lstrip()
        if stripped.startswith(kw):
            text = stripped[len(kw):].lstrip("，,；;、 ")
    text = prompts_mod.strip_scene_figures(text)
    base = text or str(getattr(prop, "description", "") or "").strip() or prop.name
    parts = [base]
    neg = _style_negative(db, getattr(prop, "project_id", None), "prop")
    if neg:
        parts.append(neg)
    prompt = _styled_prompt(kw, _join_parts(*parts))
    if (getattr(prop, "image_prompt", "") or "") != prompt:
        prop.image_prompt = prompt
    return prompt


def _character_image_base(db: Session, char) -> str:
    """角色图的**画面描述本体**：从「生图提示词」剥掉系统块（画风前缀/负向词）+ 姿态词。

    姿态词剥离的理由见 prompts.strip_pose_words（定妆图带姿态会被参考图机制
    放大成后续镜头的固定姿态）；剥离结果在**框里可见**（组装写回的就是剥完的文本）。
    """
    kw = _style_keywords(db, getattr(char, "project_id", None))
    return prompts_mod.strip_pose_words(strip_character_system_blocks(
        str(getattr(char, "image_prompt", "") or ""), kw)).strip()


def backfill_asset_prompts() -> int:
    """把全库资产的提示词框回填成**完整提示词**（幂等；启动时跑一次）。

    背景：组装写回是 2026-09-30 才有的——旧数据的提示词框里没有系统块，
    只有点一次生成才会补上（用户明确否掉了这种「事后诸葛亮」）。启动时统一
    回填：打开任何项目的资产卡，框里立即是完整提示词（画风前缀/约束/布局/
    负向词），点生成只是发送框里已有的内容。
    只写 image_prompt 字段；compose 本身幂等（按锚词判在不在），重复启动零改动。
    返回被改动的行数。
    """
    from app.db.session import SessionLocal

    changed = 0
    with SessionLocal() as db:
        for char in db.scalars(select(Character)).all():
            before = char.image_prompt or ""
            compose_character_prompt(db, char)
            if (char.image_prompt or "") != before:
                changed += 1
        for scene in db.scalars(select(Scene)).all():
            before = scene.image_prompt or ""
            compose_scene_prompt(db, scene)
            if (scene.image_prompt or "") != before:
                changed += 1
        for prop in db.scalars(select(Prop)).all():
            before = prop.image_prompt or ""
            compose_prop_prompt(db, prop)
            if (prop.image_prompt or "") != before:
                changed += 1
        if changed:
            db.commit()
    return changed


def repair_video_prompt_newlines() -> int:
    """历史数据修补：把「【视频画面】」后紧贴时间标记的画面描述补回换行（幂等）。

    成因：时间小节重排（rescale/rebalance）重建该段时吃掉了标题与首节之间的
    换行（重建逻辑 2026-09-30 已修，写入侧也已归一）；此前经重算/重排写回库的
    文本要一次性补回。只动我们自己的「标题 + 时间标记」接缝，不碰内容。
    返回被修补的行数。
    """
    from app.db.session import SessionLocal

    fixed = 0
    with SessionLocal() as db:
        rows = db.scalars(select(Shot).where(
            Shot.video_prompt.like(f"%{PICTURE_SEGMENT_NAME}%"))).all()
        for row in rows:
            new = normalize_picture_head_newline(row.video_prompt or "")
            if new != (row.video_prompt or ""):
                row.video_prompt = new
                fixed += 1
        if fixed:
            db.commit()
    return fixed


def repair_scene_layout_figures() -> int:
    """历史数据修补：把场景「空间布局」里混入的人物走位句洗干净（幂等，启动时跑）。

    成因（2026-10-02 用户反馈，生成的场景图里出现人物）：解析把人物调度写进了
    layout，layout 原样拼进生图提示词。解析提示词已加规则（场景级字段只写静态
    空间），本函数负责存量数据——清洗后由 backfill_asset_prompts 重组装，
    提示词框与生成链路一起换新。返回被清洗的场景数。
    """
    from app.db.session import SessionLocal

    fixed = 0
    with SessionLocal() as db:
        for scene in db.scalars(select(Scene)).all():
            before = str(getattr(scene, "layout", "") or "")
            if not before.strip():
                continue
            clean = _clean_scene_layout(db, scene)
            if clean != before.strip():
                scene.layout = clean
                fixed += 1
        if fixed:
            db.commit()
    return fixed


def repair_storyboard_qa_scene_headers() -> int:
    """历史数据修补：清掉体检存档里**场次头误报**的「源行未认领」条目（幂等）。

    成因（2026-10-10 用户报障《妈妈的计时器》）：场次时间头（**【19:00】**）
    曾被当作内容行要求镜头认领——它只是时间/地点的结构标记（已由场景名与
    state.time 承接），永远认领不上，于是存档里躺着 error 级 line_gap、
    还会触发视频生成硬门。规则侧已把场次头归 decl 豁免（llm_parser.
    `_is_scene_header_line`），本函数把**已存档的报告**按同一判据清理
    （谓词同源——只删新规则不会再产生的条目，绝不误删真缺口）。返回清理条数。
    """
    from app.db.session import SessionLocal
    from app.script.llm_parser import _is_structure_line
    from app.script.persist import STORYBOARD_QA_KEY_PREFIX

    def _is_header_item(item: dict) -> bool:
        """存档条目是否为「结构行误报」：line_gap 且未认领的原文是结构行。

        结构行谓词与解析侧同源（元信息头含 **加粗** 变体 / 场次时间头 /
        头部定义列表项）——只删新规则不会再产生的条目，绝不误删真缺口。
        """
        if item.get("rule") != "line_gap":
            return False
        msg = str(item.get("message") or "")
        raw = msg.split("源行未认领：", 1)[-1] if "源行未认领：" in msg else ""
        return _is_structure_line(raw)

    fixed = 0
    with SessionLocal() as db:
        rows = db.scalars(select(Setting).where(
            Setting.key.like(f"{STORYBOARD_QA_KEY_PREFIX}%"))).all()
        for row in rows:
            try:
                items = json.loads(row.value or "[]")
            except ValueError:
                continue
            if not isinstance(items, list):
                continue
            kept = [it for it in items
                    if not (isinstance(it, dict) and _is_header_item(it))]
            if len(kept) != len(items):
                row.value = json.dumps(kept, ensure_ascii=False)
                fixed += len(items) - len(kept)
        if fixed:
            db.commit()
    return fixed


def compose_character_prompt(db: Session, char) -> str:
    """组装角色「生图提示词」并写回 `character.image_prompt`（幂等，2026-09-30 用户规则）。

    顺序：画风前缀 → 外貌生图描述（已剔姿态词）→ 风格负向词（人物套）。
    角色图的**版式类约束**（设定图四格版式 / 妆造白底全身、肢体尾锚）按生成方式
    不同，仍由各自生成路径追加——同一份字段无法同时服务两种版式；框里所见＝
    两种生成方式共用的那部分（画风/描述/负向词）。
    """
    kw = _style_keywords(db, getattr(char, "project_id", None))
    base = _character_image_base(db, char) or char.name
    parts = [base]
    neg = _style_negative(db, getattr(char, "project_id", None), "character")
    if neg:
        parts.append(neg)
    prompt = _styled_prompt(kw, _join_parts(*parts))
    if (getattr(char, "image_prompt", "") or "") != prompt:
        char.image_prompt = prompt
    return prompt


def _episode_precheck(db: Session, project_id: str,
                      shots: list[Shot]) -> dict:
    """成片预检（确定性检查，全部规则脚本判定不调模型）。

    检查项（设计 6.10.2）：资产完备 / 段间衔接 / 景别层次 / 台词覆盖 /
    轴线抽查 / 时序对账 + **剧作层门**（解析期体检存档回显）+ 时长分布统计 +
    模型路由建议（6.10.3）。

    剧作层门的口径说明：行号级对账与剧作量化门（单句台词过长/整集无动作节拍/
    同框人数）在解析期已算并存档，这里**回显存档**而不是重算——同一份判定只算
    一次（审计 14 号：同一保证散落多条路径是缺陷之源）。
    """
    issues: list[dict] = []
    # 1. 资产完备：缺形象角色（error：人物一致性依赖参考图）/ 缺主图场景与
    # 缺定妆图道具（warn：文字描述约束足够，只损失陈设/道具一致性）
    missing_chars: list[str] = []
    for shot in shots:
        for cid in shot.character_ref_ids_json or []:
            if _character_artifact(db, project_id, cid, None):
                continue
            char = db.get(Character, cid)
            if char and char.name not in missing_chars:
                missing_chars.append(char.name)
    if missing_chars:
        issues.append({"level": "error", "category": "资产完备", "seq": None,
                       "message": f"出场角色缺基础形象：{'、'.join(missing_chars)}"
                                  "——请先在资产页生成定妆图（无参考图人物逐镜漂移）"})
    missing_scenes: list[str] = []
    missing_props: list[str] = []
    for shot in shots:
        if shot.scene_ref_id:
            scene = db.get(Scene, shot.scene_ref_id)
            if scene and scene.name not in missing_scenes \
                    and not _scene_artifact(db, scene):
                missing_scenes.append(scene.name)
        for pid_ in shot.prop_ref_ids_json or []:
            prop = db.get(Prop, pid_)
            if prop and prop.name not in missing_props \
                    and not _prop_artifact(db, prop):
                missing_props.append(prop.name)
    if missing_scenes:
        issues.append({"level": "warn", "category": "资产完备", "seq": None,
                       "message": f"场景缺主图：{'、'.join(missing_scenes)}"
                                  "（陈设一致性下降，可继续生成）"})
    if missing_props:
        issues.append({"level": "warn", "category": "资产完备", "seq": None,
                       "message": f"道具缺定妆图：{'、'.join(missing_props)}"
                                  "（道具外观可能跨镜漂移）"})
    # 2. 景别层次：首镜交代环境 + 单一景别占比（与解析期体检消费同一实现，
    # 消重复拦截——批次 2a §32.2）
    for rule, msg in scale_rhythm_flags([s.camera or "" for s in shots]):
        issues.append({"level": "warn", "category": "景别层次",
                       "seq": shots[0].seq if rule == "first_shot" else None,
                       "message": msg + ("（观感近似会动的 PPT）"
                                         if rule == "scale_mono" else "")})
    # 3. 段间衔接：相邻同场景同景别直连；4. 轴线抽查：方向词冲突
    for a, b in zip(shots, shots[1:]):
        if not (a.scene_ref_id and a.scene_ref_id == b.scene_ref_id):
            continue
        sa, sb = scale_of(a.camera or ""), scale_of(b.camera or "")
        if sa and sa == sb:
            issues.append({"level": "warn", "category": "段间衔接", "seq": b.seq,
                           "message": f"镜{a.seq}→{b.seq} 同场景同景别（{sa}）"
                                      "直连，建议相邻镜头景别跳档衔接"})
        da, db_ = _camera_direction(a.camera or ""), _camera_direction(b.camera or "")
        if da and db_ and da != db_:
            issues.append({"level": "warn", "category": "轴线抽查", "seq": b.seq,
                           "message": f"镜{a.seq}→{b.seq} 同场景运镜方向冲突"
                                      f"（{da}→{db_}，越轴观感）"})
    # 5. 时序对账 + 6. 台词覆盖 + 冻结关键帧卫生门（逐镜）
    for shot in shots:
        err = timing_error({"duration": shot.duration,
                            "video_prompt": shot.video_prompt or ""})
        if err:
            issues.append({"level": "warn", "category": "时序对账",
                           "seq": shot.seq,
                           "message": err + "（时长可能被编辑过，建议重算画面描述）"})
        # 冻结关键帧卫生门（批次 1 §31.4，弱启发 warn 不阻断）：首帧图由
        # 【全局定义】+首小节派生，首小节含进行/突变时体词 → 首帧画成动作
        # 进行中（AI 视频最常见缺陷「起始帧提前出现动作结果」）
        opening = _first_frame_parts(shot.video_prompt or "")[1]
        if opening and _ASPECT_MARK_RE.search(opening):
            issues.append({"level": "warn", "category": "冻结关键帧",
                           "seq": shot.seq,
                           "message": "首个时间小节含进行/突变时体词（正在/突然/"
                                      "已经/随后…），首帧可能画成动作进行中——"
                                      "建议首小节只写起点静态状态，动作推进移到"
                                      "后续小节"})
        if (shot.video_prompt or "").strip():
            missing_lines = [
                d for d in (shot.dialogue_json or [])
                if d.get("text") and not _dialogue_in_prompt(
                    str(d["text"]), shot.video_prompt or "")]
            if missing_lines:
                issues.append({"level": "error", "category": "台词覆盖",
                               "seq": shot.seq,
                               "message": f"{len(missing_lines)} 句台词未进画面"
                                          "描述（成片会丢台词），建议重算画面描述"})
            # 台词顺序校验（批次 2a §32.1 顺序规则）：VP 中台词出现顺序与
            # 源行顺序不一致 = 因果倒置嫌疑（warn）
            order_err = vp_dialogue_order_error(
                {"video_prompt": shot.video_prompt or "",
                 "dialogue": shot.dialogue_json or []})
            if order_err:
                issues.append({"level": "warn", "category": "台词顺序",
                               "seq": shot.seq, "message": order_err})
        elif shot.dialogue_json:
            issues.append({"level": "warn", "category": "台词覆盖", "seq": shot.seq,
                           "message": "有台词但无画面描述（生成走旧回退组合）"})
        # 台词密集镜提示（20 号 §4.4 音频解耦·方案 A 的轻量落地）：本镜台词 ≥2 句
        # 时，视频模型原生配音更容易念错/串声线，而重拍整镜的代价与画面无关——
        # 提示可走「音频先行」（对白轨单独生成、视频只负责画面与口型）。
        # **只提示不拦截**：口型同步是新变量，未实测前不改变默认链路。
        _dlg_count = len([d for d in (shot.dialogue_json or []) if d.get("text")])
        if _dlg_count >= DENSE_DIALOGUE_SHOTS:
            issues.append({"level": "warn", "category": "台词密集", "seq": shot.seq,
                           "message": f"本镜 {_dlg_count} 句台词（密集镜）——原生配音"
                                      "念错/串声线时需重拍整镜；如反复失败，可考虑"
                                      "对白轨单独生成（音频先行）后与画面合流"})
        # 素材引用覆盖对账（确定性）：本镜**该出场的**角色/道具/场景，必须都在
        # video_prompt 里以「角色@名」形式出现——引用是系统绑定参考图的依据，
        # 漏一个就等于该要素**没有参考图锚点**，模型只能自由发挥（道具漏引用 →
        # 形态跨镜漂移，实测编织袋变网兜；人物漏引用 → 串脸）。
        #
        # **判据是「该出场」而不是「已绑定」**——绑定不等于出场：实测镜1 是仰拍
        # 大远景空镜，绑定了食盒但 state 账本明确写着「无具体道具」，LLM 不引用
        # 它是对的（按「绑定即该引用」判会给这种镜误报）。出场与否看 **state
        # 账本**（它是「第 0 秒静止画面」的权威描述）；账本自己声明为空时不再要求。
        # **只在文本已用引用体系时检查**（出现任一 @引用），否则旧格式提示词
        # 会被整片误判——那时参考图由 `_extra_ref_artifacts` 按名单补齐。
        _absent = _missing_material_refs(
            db, project_id, shot, shot.video_prompt or "")
        if _absent:
            if _absent:
                issues.append({
                    "level": "warn", "category": "素材引用", "seq": shot.seq,
                    "message": f"本镜 {len(_absent)} 个出场素材未在画面描述里引用："
                               f"{'、'.join(_absent[:4])}——引用是绑定参考图的依据，"
                               "漏写时该素材没有锚点（道具跨镜变形、人物串脸）"})
        # 台词密度对账（确定性）：**单个时间小节**的台词字数 ÷ 秒数超上限。
        # 与「台词密集」（按句数）互补：那条管句数多，这条管**装不下**——实测
        # 一镜整镜是合格的（10s / 3 句 39 字），但 08—10秒 那 2 秒塞了 2 句
        # 26 字 = 13 字/秒（正常 4-5、上限 6）；原生配音只能加速念完，成片
        # 语速快得不像话。拆镜期的整镜折算管不到这个粒度。
        _rate_beats = dialogue_rate_by_beat(
            shot.video_prompt or "",
            [d.get("text") or "" for d in (shot.dialogue_json or [])])
        _overloaded = [b for b in _rate_beats if b["rate"] > DIALOGUE_RATE_MAX]
        if _overloaded:
            _beat = _overloaded[0]
            issues.append({
                "level": "warn", "category": "台词密度", "seq": shot.seq,
                "message": f"{_beat['start']:02d}—{_beat['end']:02d}秒"
                           f"（{_beat['secs']}s）有 {_beat['chars']} 字台词 = "
                           f"{_beat['rate']} 字/秒（正常 4-5，上限 "
                           f"{DIALOGUE_RATE_MAX:g}）——原生配音会加速念完、听感快得"
                           "不像话；请把台词分摊到相邻小节或加长本镜"})
        # 整镜语速对账（确定性，25 号 3.2）：**总时长装不装得下全部台词**。
        # 与小节级是两个维度——小节级重排能改善，**整镜级重排改不了**（台词总量与
        # 镜头总时长都没变，怎么挪小节语速都一样）。实测 30% 的镜偏快、18 镜
        # >8 字/秒（物理上念不完）。这条报 **error**：不是"可以凑合"的观感问题。
        _shot_rate = shot_dialogue_rate(
            shot.duration,
            [d.get("text") or "" for d in (shot.dialogue_json or [])])
        if _shot_rate["rate"] > DIALOGUE_RATE_MAX:
            issues.append({
                "level": "error", "category": "整镜语速", "seq": shot.seq,
                "message": f"本镜 {_shot_rate['secs']}s 塞了 {_shot_rate['chars']} 字台词"
                           f" = {_shot_rate['rate']} 字/秒（正常 4-5，上限 "
                           f"{DIALOGUE_RATE_MAX:g}）——**整镜级**超限，重排时间小节"
                           "改不了它（台词总量与镜头时长都没变）：请**加长本镜时长**"
                           "或**删减台词**"})
    # 7. 时长分布统计（含 reaction 占比——关键反应镜缺失的观测指标） +
    # 8. 模型路由建议（6.10.3：建议不自动改模型；risks 为解析期声明，与
    # camera/desc 启发合并）
    durations = [s.duration for s in shots]
    summary = {
        "shots": len(shots),
        "total_duration": sum(durations),
        "avg_duration": round(sum(durations) / len(durations), 1) if durations else 0,
        "short_shots": sum(1 for d in durations if d < SHOT_DURATION_MIN),
        "reaction_shots": sum(1 for s in shots if (s.purpose or "") == "reaction"),
    }
    hints: list[dict] = []
    for shot in shots:
        cam = shot.camera or ""
        desc = shot.description or ""
        risks = shot.risks_json or []
        hint = ""
        if "特写" in cam or "近景" in cam or "face" in risks:
            hint = "人物特写/近景：首帧建议写实系图模型（细节保真）"
        elif any(w in cam for w in ("远景", "全景", "航拍", "俯瞰")):
            hint = "大场面镜头：已自动追加降噪与景深约束，抽卡时优先看主体清晰度"
        if "text" in risks or any(
                w in desc for w in ("写字板", "信纸", "招牌", "字条", "字迹", "横幅")):
            hint = (hint + "；" if hint else "") + \
                "含文字画面：建议选支持文字渲染的模型（精确文字仍需后期）"
        if "crowd" in risks:
            hint = (hint + "；" if hint else "") + \
                "多人同框：重点核对人数与身份（拆镜可降低同框人数）"
        # 高风险动作镜（批次 4 §35.2 白名单新增类别）：解析期已按 9.7 条要求
        # 写降级拍法，此处再把「模型档位」这个用户可操作项摆出来（审计 13 号 #6：
        # 白名单加了新类别但消费端只认 face/text/crowd，高风险镜零提示）
        _hard_action = [r for r in ("fluid", "mirror", "collision", "transform",
                                    "hands") if r in risks]
        if _hard_action:
            hint = (hint + "；" if hint else "") + (
                f"高风险动作镜（{'/'.join(_hard_action)}）：已按降级拍法写结果态，"
                "建议选动作遵循强的高档视频模型，必要时拆为「撞前/撞后」两镜")
        if hint:
            hints.append({"seq": shot.seq, "hint": hint})
    # 9. 剧作层门回显（解析期体检存档）：单句台词过长 / 整集无动作节拍 /
    #    同框人数超限 + 行号对账残留——预检面板据此把「剧本层问题」一并摆出来，
    #    用户不必切到体检报告页；原样引用存档（不重算，保持单一来源）
    episode = db.get(Episode, shots[0].episode_id) if shots else None
    qa_setting = db.get(Setting, storyboard_qa_key(project_id))
    if qa_setting and qa_setting.value:
        try:
            qa_all = json.loads(qa_setting.value)
        except ValueError:
            qa_all = []
        _ep_seq = episode.seq if episode is not None else None
        for it in qa_all:
            if it.get("episode") not in (None, _ep_seq):
                continue
            issues.append({
                "level": it.get("level") or "warn",
                "category": "剧本层",
                "seq": it.get("seq"),
                "message": f"[{it.get('title') or it.get('rule')}] "
                           f"{it.get('message') or ''}",
            })
    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: (order.get(i["level"], 3), i["seq"] or 0))
    return {"summary": summary, "issues": issues, "model_hints": hints}


class AssetLinkIn(BaseModel):
    """作用域绑定入参（scope=project|episode|shot）。"""
    character_id: str
    image_id: str
    scope: str = "project"
    episode_id: str | None = None
    shot_id: str | None = None
    note: str = ""


@router.get("/projects/{project_id}/asset-links")
def list_asset_links(project_id: str, character_id: str | None = None,
                     db: Session = Depends(get_db)):
    """角色形象的作用域绑定列表（按具体度排序：shot > episode > project）。"""
    _project(db, project_id)
    from app.services.scoped_link import list_bindings
    return {"ok": True, "links": list_bindings(db, project_id, character_id)}


@router.put("/projects/{project_id}/asset-links")
def put_asset_link(project_id: str, body: AssetLinkIn,
                   db: Session = Depends(get_db)):
    """建立/更新作用域绑定（同作用域幂等覆盖）。

    「第 3-5 集换晚宴装」= 建 3 条 episode 级绑定（或 1 条 + 逐集），
    分镜解析参考图时按 shot > episode > project 自动取最近生效者——
    不再逐镜打标，漏标即漂移的问题从结构上消除。
    """
    _project(db, project_id)
    from app.services.scoped_link import bind_character_image
    try:
        row = bind_character_image(
            db, project_id=project_id, character_id=body.character_id,
            image_id=body.image_id, scope=body.scope,
            episode_id=body.episode_id, shot_id=body.shot_id,
            note=body.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return {"ok": True, "link_id": row.id, "scope": row.scope}


@router.delete("/projects/{project_id}/asset-links")
def delete_asset_link(project_id: str, character_id: str, scope: str,
                      episode_id: str | None = None,
                      shot_id: str | None = None,
                      db: Session = Depends(get_db)):
    """解除一条作用域绑定（解绑后自动回退到更宽作用域或默认选图链）。"""
    _project(db, project_id)
    from app.services.scoped_link import unbind
    removed = unbind(db, project_id=project_id, character_id=character_id,
                     scope=scope, episode_id=episode_id, shot_id=shot_id)
    db.commit()
    return {"ok": True, "removed": removed}


@router.get("/episodes/{episode_id}/video-readiness")
def episode_video_readiness_endpoint(episode_id: str,
                                     db: Session = Depends(get_db)):
    """整集视频就绪度（单一来源服务，前端生成向导消费）。

    返回逐镜的结构化检查项：哪些镜可以立刻生成、哪些被什么条件阻塞。
    与提交前硬门、成片预检共用同一判定（app/services/readiness.py）。
    """
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    shots = db.scalars(select(Shot).where(Shot.episode_id == episode_id)
                       .order_by(Shot.seq)).all()
    return {"ok": True,
            **readiness.episode_video_readiness(db, episode.project_id,
                                                episode, list(shots))}


@router.get("/episodes/{episode_id}/precheck")
def precheck_episode(episode_id: str, db: Session = Depends(get_db)):
    """成片预检报告（确定性检查不改数据）：生成前一键体检。"""
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    shots = db.scalars(select(Shot).where(Shot.episode_id == episode_id)
                       .order_by(Shot.seq)).all()
    report = _episode_precheck(db, episode.project_id, list(shots))
    return {"ok": True, **report}


def _parse_day(ts: str) -> int | None:
    """`2026-09-01` → 本地时区当天 0 点的时间戳（解析不出返回 None）。"""
    try:
        return int(time.mktime(time.strptime((ts or "").strip(), "%Y-%m-%d")))
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _model_call_window(days: int, start: str = "",
                       end: str = "") -> tuple[int, int, str]:
    """调用日志的时间窗 → (起始时间戳, 截止时间戳（不含）, 白话描述)。

    自定义区间（`start`/`end`，**含结束当天**）优先于「近 N 天」。日期格式不对
    或开始晚于结束 → 400：**静默忽略用户填的日期比报错更糟**（他会以为筛过了）。
    """
    now = int(time.time())
    if (start or "").strip() or (end or "").strip():
        _s, _e = _parse_day(start), _parse_day(end)
        if _s is None or _e is None:
            raise HTTPException(
                400, "自定义日期格式不对：开始与结束都要填完整日期（如 2026-09-01）")
        until = _e + 86400          # 含结束当天
        if _s >= until:
            raise HTTPException(400, "开始日期不能晚于结束日期")
        return _s, until, f"{start.strip()} 至 {end.strip()}"
    if days <= 0:
        # 「全部」档（2026-09-30 调用日志分页）：不限下限，配合分页翻历史
        return 0, now + 1, "全部时间"
    d = max(1, min(days, 90))
    return now - d * 86400, now + 1, f"近 {d} 天"


@router.get("/model-calls", dependencies=[Depends(require_admin)])
def model_calls_log(days: int = 7, limit: int = 200, offset: int = 0,
                    only_failed: bool = False,
                    q: str = "", start: str = "", end: str = "",
                    db: Session = Depends(get_db)):
    """**模型调用日志**（系统管理·调用日志页）：逐条调用，倒序，**支持分页**。

    看板（`/model-calls/stats`）回答「哪个端点最差、一共花了多少」，这里回答
    「**具体哪一次**失败了、为什么失败」——两者是同一张表的两个视角。排查生成
    质量时先看这里挑出可疑调用，再用任务号去分镜卡的「查看参数」看那一次调用
    实际发出去的完整提示词与参考图；单条详情（含请求/响应留痕）在
    `GET /model-calls/{id}`。

    过滤：`only_failed`（只看失败）、`q`（在 调用点/域名/模型/错误信息 里搜）、
    时间窗（`days` 近 N 天，**`days<=0` = 不限（全部）**，或 `start`/`end`
    自定义日期区间——后者优先）。`offset`/`limit` 分页，同时返回 `total`
    供前端显示「已显示 X / 共 Y 条」。
    """
    from app.db.models import ModelCall

    since, until, window_label = _model_call_window(days, start, end)
    base = (select(ModelCall)
            .where(ModelCall.created_at >= since, ModelCall.created_at < until))
    if only_failed:
        base = base.where(ModelCall.ok.is_(False))
    if q.strip():
        like = f"%{q.strip()}%"
        base = base.where(or_(ModelCall.what.ilike(like),
                              ModelCall.host.ilike(like),
                              ModelCall.model.ilike(like),
                              ModelCall.error.ilike(like)))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    page_limit = max(1, min(limit, 500))
    offset = max(0, offset)
    stmt = (base.order_by(ModelCall.created_at.desc(), ModelCall.id.desc())
            .offset(offset).limit(page_limit))
    rows = db.scalars(stmt).all()
    return {"ok": True,
            "window": {"since": since, "until": until, "label": window_label},
            "total": total, "offset": offset, "limit": page_limit,
            "items": [{
        "id": r.id, "at": r.created_at,
        "what": r.what, "host": r.host, "model": r.model,
        "ok": bool(r.ok), "http_status": r.http_status,
        "fail_class": r.fail_class, "error": r.error,
        "elapsed_ms": r.elapsed_ms, "usage": r.usage_json or {},
        "project_id": r.project_id or "", "task_id": r.task_id or "",
    } for r in rows]}


@router.get("/model-calls/stats", dependencies=[Depends(require_admin)])
def model_call_stats(days: int = 7, start: str = "", end: str = "",
                     db: Session = Depends(get_db)):
    """**模型调用看板**（25 号 2.3/2.6）：对 `model_calls` 聚合，不碰 `tasks` 表。

    回答三个问题：
    ① **哪些端点/模型在失败** —— 抽卡率的**分母**在这（LLM 调用不是任务，搭在
       tasks 上会丢粒度）；
    ② **消费了多少** —— token / 出图张数 / 视频秒数（从调用的 usage 里求和）；
    ③ 按天趋势 —— 调用量与失败量。

    时间窗：`days`（近 N 天）或 `start`/`end`（自定义日期，含结束当天，优先）。
    """
    from app.db.models import ModelCall

    since, until, window_label = _model_call_window(days, start, end)
    rows = db.scalars(select(ModelCall).where(
        ModelCall.created_at >= since, ModelCall.created_at < until)).all()

    def _num(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    per_model: dict[tuple[str, str, str], dict] = {}
    per_day: dict[str, dict] = {}
    total = ok = 0
    images = video_secs = tokens = 0
    for r in rows:
        total += 1
        if r.ok:
            ok += 1
        u = r.usage_json or {}
        _img = _num(u.get("images"))
        _tok = _num((u.get("tokens") or {}).get("total_tokens")
                    if isinstance(u.get("tokens"), dict) else 0)
        # 视频的计费量纲是秒数：只在视频类调用上累加（请求侧 duration）
        # 视频的计费量纲是秒数：请求侧时长（agnes 载荷键为 seconds、
        # 自建端点用 duration——两者都取，缺一个会让看板恒为 0）
        _sec = (_num(u.get("req_duration") or u.get("req_seconds"))
                if "视频" in (r.what or "") else 0)
        images += _img
        tokens += _tok
        video_secs += _sec
        key = (r.what or "", r.host or "", r.model or "")
        m = per_model.setdefault(key, {"calls": 0, "failed": 0, "ms": 0,
                                       "images": 0, "tokens": 0, "video_secs": 0})
        m["calls"] += 1
        m["failed"] += 0 if r.ok else 1
        m["ms"] += _num(r.elapsed_ms)
        m["images"] += _img
        m["tokens"] += _tok
        m["video_secs"] += _sec
        day = time.strftime("%Y-%m-%d", time.localtime(r.created_at or 0))
        d = per_day.setdefault(day, {"calls": 0, "failed": 0})
        d["calls"] += 1
        d["failed"] += 0 if r.ok else 1

    return {
        "window_days": max(1, min(days, 90)),
        "window": {"since": since, "until": until, "label": window_label},
        "total": {"calls": total, "ok": ok, "failed": total - ok},
        "consumption": {"images": images, "video_seconds": video_secs,
                        "tokens": tokens},
        "by_model": sorted(
            ({"what": k[0], "host": k[1], "model": k[2],
              "calls": v["calls"], "failed": v["failed"],
              "avg_ms": round(v["ms"] / v["calls"]) if v["calls"] else 0,
              "images": v["images"], "tokens": v["tokens"],
              "video_seconds": v["video_secs"]} for k, v in per_model.items()),
            key=lambda x: -x["calls"]),
        "by_day": [{"date": d, **v} for d, v in sorted(per_day.items())],
    }


@router.get("/model-calls/{call_id}", dependencies=[Depends(require_admin)])
def model_call_detail(call_id: str, db: Session = Depends(get_db)):
    """单次调用详情（调用日志「查看详情」弹窗）：完整地址 + 请求/响应留痕。

    留痕在**写入时**就已打码敏感字段、截断超长内容（`providers._clip_trace`——
    十万字符的解析提示词也只留前两万），这里原样回读，不再加工。
    （声明在 `/model-calls/stats` 之后：路由按声明顺序匹配，stats 不会被当成 id。）
    """
    from app.db.models import ModelCall

    row = db.get(ModelCall, call_id)
    if row is None:
        raise HTTPException(404, "调用记录不存在")
    return {"ok": True, "call": {
        "id": row.id, "at": row.created_at,
        "what": row.what, "host": row.host, "model": row.model,
        "url": row.url or "", "ok": bool(row.ok),
        "http_status": row.http_status,
        "fail_class": row.fail_class, "error": row.error,
        "elapsed_ms": row.elapsed_ms, "usage": row.usage_json or {},
        "project_id": row.project_id or "", "task_id": row.task_id or "",
        "request_json": row.request_json or "",
        "response_json": row.response_json or "",
    }}


@router.get("/projects/{project_id}/gate-stats")
def gate_stats(project_id: str, db: Session = Depends(get_db)):
    """门禁触发统计（批次 2a §32.2，只读不改数据）。

    聚合最近归档解析轨迹的 gates 事件（31.6 埋点）：哪道门最常响 → 该改
    那条提示词的措辞，而不是再骂一遍模型；从未响过的门要么规则已被内化、
    要么是死门（需自查）。数据源是最近一次成功解析，跨批次趋势待批次 3
    的 ShotReview 落库后扩展。
    """
    trace = load_script_trace(db, project_id) or {}
    counter: dict[str, int] = {}
    samples: dict[str, str] = {}
    for g in trace.get("gates") or []:
        name = str(g.get("gate") or "")
        if not name:
            continue
        counter[name] = counter.get(name, 0) + 1
        samples.setdefault(name, str(g.get("content") or ""))
    gates = [{"gate": name, "count": n, "last": samples.get(name, "")}
             for name, n in sorted(counter.items(), key=lambda kv: -kv[1])]
    # 解析期体检报告的 error/warn 概要一并透出（提交前硬门的数据预览）
    qa_setting = db.get(Setting, storyboard_qa_key(project_id))
    qa_summary: dict = {"error": 0, "warn": 0}
    if qa_setting and qa_setting.value:
        try:
            for it in json.loads(qa_setting.value):
                level = it.get("level")
                if level in qa_summary:
                    qa_summary[level] += 1
        except ValueError:
            pass
    return {"ok": True, "gates": gates, "storyboard_qa": qa_summary}


def _probe_video(path: Path) -> dict | None:
    """视频确定性探针（cv2）：时长/帧率 + 抽 3 帧纯色检测。

    帧内标准差 < 8 判定近纯色（黑帧/白帧；彩条帧内 std 高不会误判）；
    读取失败返回 None（调用方按「探针失败」报告，不臆断）。
    """
    import cv2  # 函数内导入：cv2 加载重，未用到审片不付启动成本

    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0 or frames <= 0:
            return None
        solids = 0
        for fi in {0, frames // 2, max(0, frames - 1)}:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            if float(frame.std()) < 8.0:
                solids += 1
        return {"duration": frames / fps, "fps": fps, "frames": frames,
                "solid_frames": solids}
    finally:
        cap.release()


@router.get("/episodes/{episode_id}/review")
def review_episode(episode_id: str, db: Session = Depends(get_db)):
    """确定性审片（E2 第一档）：视频时长偏差 / 纯色黑帧 / 首帧缺失。

    只产出报告与失败镜头清单；重拍走 generate_shots 的 shot_ids 定向
    生成（单变量重拍：同参数重新生成，换参数请手动改分镜后重拍）。
    """
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    shots = db.scalars(select(Shot).where(Shot.episode_id == episode_id)
                       .order_by(Shot.seq)).all()
    results: list[dict] = []
    for shot in shots:
        ff = _latest_artifact(db, episode.project_id, f"shot_{shot.id}_ff")
        video = _latest_artifact(db, episode.project_id, f"shot_{shot.id}_v")
        item: dict = {"shot_id": shot.id, "seq": shot.seq,
                      "has_video": bool(video), "has_first_frame": bool(ff)}
        if video:
            asset = db.get(Asset, video)
            probe = (_probe_video(Path(asset.path))
                     if asset and asset.path and Path(asset.path).exists()
                     else None)
            if probe:
                item["video_duration"] = round(probe["duration"], 1)
                item["expected_duration"] = shot.duration
                item["duration_ok"] = abs(probe["duration"] - shot.duration) <= 1.0
                item["solid_frames"] = probe["solid_frames"]
                item["frame_ok"] = probe["solid_frames"] == 0
            else:
                item["probe_failed"] = True
        results.append(item)
    failed_ids = [
        r["shot_id"] for r in results
        if not r["has_video"]
        or not r.get("duration_ok", True) or not r.get("frame_ok", True)
        or r.get("probe_failed")]
    return {"ok": True, "results": results, "failed_shot_ids": failed_ids}


@router.post("/episodes/{episode_id}/link-frames")
def link_frames(episode_id: str, db: Session = Depends(get_db)):
    """全链首尾帧对齐（设计文档 6.6 第 4 条）：对每对相邻分镜 (n, n+1)，
    取分镜 n 视频的尾帧，追加为分镜 n+1 首帧节点的新版本（source=edited），
    实现相邻镜头尾帧到首帧的丝滑转场。

    幂等：源视频未变（分镜 n+1 首帧节点已有引用同一视频产物的抽帧版本）
    时跳过，重复执行不再膨胀版本。返回：
    - duplicates：幂等跳过数；skipped：前一分镜无视频的跳过数
    - fallback_seqs：抽到 mock 占位帧的分镜 seq（ffmpeg 不可用/抽帧失败，
      该帧不可作为真实转场衔接，需检查环境后重试）
    - regen_seqs：首帧已被对齐更新但视频仍是旧首帧生成的分镜 seq，
      需重新生成视频后对齐才生效
    """
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    shots = db.scalars(
        select(Shot).where(Shot.episode_id == episode_id)
        .order_by(Shot.seq)
    ).all()
    linked = duplicates = skipped = 0
    fallback_seqs: list[int] = []
    regen_seqs: list[int] = []
    for cur, nxt in zip(shots, shots[1:]):
        src_node = f"shot_{cur.id}_v"
        dest_node = f"shot_{nxt.id}_ff"
        src_artifact = latest_video_artifact_id(db, episode.project_id, src_node)
        if not src_artifact:
            skipped += 1
            continue
        # 幂等：源视频未变时对齐版本已存在，跳过（不重复追版本）
        if has_frame_version_for(db, episode.project_id, dest_node, src_artifact):
            duplicates += 1
            continue
        result = extract_frame_version(
            db, episode.project_id, src_node, dest_node,
            position="last", duration_hint=float(cur.duration),
        )
        if not result:
            skipped += 1
            continue
        _version, asset = result
        linked += 1
        if (asset.meta_json or {}).get("fallback"):
            fallback_seqs.append(nxt.seq)
        # 对齐只更新首帧引用：下一镜已有视频时旧视频仍是旧首帧，需重新生成
        if _latest_artifact(db, episode.project_id, f"shot_{nxt.id}_v"):
            regen_seqs.append(nxt.seq)
    return {"ok": True, "linked": linked, "duplicates": duplicates,
            "skipped": skipped, "fallback_seqs": fallback_seqs,
            "regen_seqs": regen_seqs}


@router.post("/episodes/{episode_id}/compose")
def compose_episode(episode_id: str, db: Session = Depends(get_db)):
    """单集合成：分镜视频按序拼接（无视频的分镜跳过）。"""
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    shots = db.scalars(
        select(Shot).where(Shot.episode_id == episode_id)
        .order_by(Shot.seq)
    ).all()
    videos: list[str] = []
    for shot in shots:
        artifact = _latest_artifact(db, episode.project_id, f"shot_{shot.id}_v")
        if artifact:
            videos.append(artifact)
    if not videos:
        raise HTTPException(400, "本集暂无已完成的分镜视频，无法合成")
    try:
        submitted = engine.submit(
            project_id=episode.project_id, node_id=f"episode_{episode_id}",
            runner_name="compose", params={"mode": "concat"},
            input_refs={"videos": videos},
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    return {"task_id": submitted["task_id"]}


# ---------- 提示词在线编辑（批次 2b §32.4，D-4 决策：外置 + UI 在线编辑） ----------


@router.get("/episodes/{episode_id}/shots/download")
def download_shot_videos(episode_id: str, db: Session = Depends(get_db)):
    """打包下载本集全部分镜视频（按镜号排序命名；无视频 400）。

    与合成同一份产物解析（_latest_artifact × node=shot_{id}_v）——分镜页展示
    的视频与包内一致。ZIP_STORED：mp4 本身已压缩，二次压缩慢且无收益。
    """
    import io
    import zipfile
    from urllib.parse import quote

    from fastapi.responses import StreamingResponse

    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    shots = db.scalars(
        select(Shot).where(Shot.episode_id == episode_id)
        .order_by(Shot.seq)
    ).all()
    entries: list[tuple[str, str]] = []  # (zip 内文件名, 磁盘路径)
    for shot in shots:
        art_id = _latest_artifact(db, episode.project_id, f"shot_{shot.id}_v")
        asset = db.get(Asset, art_id) if art_id else None
        if not asset or not asset.path:
            continue
        path = Path(asset.path)
        if not path.exists():
            continue
        entries.append((f"镜{shot.seq:02d}_{int(shot.duration or 0)}s.mp4",
                        str(path)))
    if not entries:
        raise HTTPException(400, "本集暂无已完成的分镜视频，无法打包")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, path in entries:
            zf.write(path, name)
    buf.seek(0)
    # 文件名：项目名_第n集_分集标题（Windows 非法字符消毒）
    project = db.get(Project, episode.project_id)
    parts = [getattr(project, "name", ""), f"第{episode.seq}集",
             (episode.title or "").strip()]
    safe = re.sub(r'[\\/:*?"<>|]', "_",
                  "_".join(p for p in parts if p and p.strip()) or "分镜视频")
    filename = quote(f"{safe}.zip")
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


class PromptSaveIn(BaseModel):
    """提示词保存入参（全文替换）。"""

    content: str


@router.get("/prompts", dependencies=[Depends(require_admin)])
def list_system_prompts():
    """外置提示词清单（名称/描述/注入阶段/可编辑标记；仅管理员）。"""
    return {"ok": True, "prompts": list_prompts()}


@router.get("/prompts/{name}", dependencies=[Depends(require_admin)])
def get_system_prompt(name: str, db: Session = Depends(get_db)):
    """读取提示词正文：外置文件优先，缺失回退内置常量（双保险；仅管理员）。"""
    meta = {it["name"]: it for it in list_prompts()}
    body = load_prompt(name)
    if body is None:
        body = getattr(prompts_mod, name, None)
        if not (isinstance(body, str) and name.endswith("_SYSTEM_PROMPT")):
            raise HTTPException(404, "提示词不存在")
    return {"ok": True, "name": name, "content": body,
            "description": meta.get(name, {}).get("description", ""),
            "stage": meta.get(name, {}).get("stage", ""),
            "editable": meta.get(name, {}).get("editable", True),
            "has_snapshot": db.get(Setting, prompt_snapshot_key(name))
            is not None}


@router.put("/prompts/{name}", dependencies=[Depends(require_admin)])
def save_system_prompt(name: str, body: PromptSaveIn,
                       db: Session = Depends(get_db)):
    """保存提示词编辑：快照上一版 → 完整性自检写盘 → 模块全局热生效。

    热生效 = setattr 回 prompts 模块（已导入方下次 LLM 调用即读新内容，
    无需重启）；locked 与过短正文拒绝保存（仅管理员）。
    """
    meta = {it["name"]: it for it in list_prompts()}
    if name not in meta:
        raise HTTPException(404, "提示词不存在")
    if not meta[name]["editable"]:
        raise HTTPException(400, f"{name} 为锁定提示词，不允许在线编辑")
    try:
        old = load_prompt(name) or ""
        # 先写盘自检（过短/IO 失败即拒绝，快照不被触碰——被拒的保存不得
        # 覆盖上一版快照），成功后才落快照并提交
        save_prompt(name, body.content)
        db.merge(Setting(key=prompt_snapshot_key(name), value=old))
        db.commit()
        reload_prompts()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        db.rollback()
        raise HTTPException(500, f"提示词写盘失败：{e}") from e
    return {"ok": True}


@router.post("/prompts/{name}/restore", dependencies=[Depends(require_admin)])
def restore_system_prompt(name: str, db: Session = Depends(get_db)):
    """从快照恢复上一版提示词（恢复后快照清除，再次保存会重新快照；仅管理员）。"""
    snap = db.get(Setting, prompt_snapshot_key(name))
    if not snap or not (snap.value or "").strip():
        raise HTTPException(404, "无可恢复的快照")
    try:
        save_prompt(name, snap.value)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    reload_prompts()
    db.delete(snap)
    db.commit()
    return {"ok": True}


@router.get("/lessons-insights", dependencies=[Depends(require_admin)])
def lessons_insights(db: Session = Depends(get_db)):
    """自学习全局洞察：跨项目系统性弱项 + 提示词改进建议（应用前用户确认）。

    suggestions 的 applied 由目标提示词正文是否已含同规则标记判定（幂等）；
    前端提示词页展示并支持一键应用（应用走快照+热生效，可恢复）。

    2026-09-30 补管理员门禁：这是**跨项目聚合**的统计，且 apply 会改写全局
    提示词（影响所有人的生成质量基线）——普通用户既不该读也不该写。
    与 /prompts 系列同一道门（那是管理员专属）。
    """
    ledger = load_global_lessons(db)
    plain = {r: (v[0] if isinstance(v, tuple) and v else r)
             for r, v in QA_PLAIN.items()}
    # 提示词正文快照（判断建议是否已应用）
    meta = {it["name"]: it for it in list_prompts()}
    texts = {name: (load_prompt(name) or "") for name in meta}
    patterns = []
    for section in ("storyboard", "script"):
        bucket = ledger.get(section) or {}
        for rule, entry in bucket.items():
            if not isinstance(entry, dict):
                continue
            patterns.append({
                "rule": rule, "title": plain.get(rule, rule),
                "count": int(entry.get("count") or 0),
                "projects": len(entry.get("projects") or {}),
                "section": section,
            })
    patterns.sort(key=lambda p: (-p["projects"], -p["count"]))
    return {"ok": True, "patterns": patterns,
            "suggestions": build_lessons_suggestions(ledger, plain, texts)}


class LessonApplyIn(BaseModel):
    """自学习改进建议应用入参（按规则名定位建议）。"""
    rule: str


@router.post("/lessons-insights/apply", dependencies=[Depends(require_admin)])
def apply_lessons_suggestion(body: LessonApplyIn,
                             db: Session = Depends(get_db)):
    """应用改进建议：把建议正文追加到目标提示词（快照兜底 + 热生效）。

    幂等：目标提示词已含同规则标记时直接返回（不重复追加）；写盘失败
    回滚快照（与 /prompts PUT 同语义）。

    2026-09-30 补管理员门禁：写入的是**全局提示词**（所有用户共用），
    必须与 /prompts PUT 同权限级别。
    """
    ledger = load_global_lessons(db)
    plain = {r: (v[0] if isinstance(v, tuple) and v else r)
             for r, v in QA_PLAIN.items()}
    meta = {it["name"]: it for it in list_prompts()}
    texts = {name: (load_prompt(name) or "") for name in meta}
    suggestion = next(
        (s for s in build_lessons_suggestions(ledger, plain, texts)
         if s["rule"] == body.rule), None)
    if not suggestion:
        raise HTTPException(404, "该规则暂无可用改进建议（未达系统性阈值或未映射）")
    if suggestion["applied"]:
        return {"ok": True, "already_applied": True}
    target = suggestion["target"]
    if target not in meta:
        raise HTTPException(404, f"目标提示词不存在：{target}")
    if not meta[target]["editable"]:
        raise HTTPException(400, f"{target} 为锁定提示词，不允许在线编辑")
    try:
        old = load_prompt(target) or ""
        save_prompt(target, old + suggestion["append_text"])
        db.merge(Setting(key=prompt_snapshot_key(target), value=old))
        db.commit()
        reload_prompts()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        db.rollback()
        raise HTTPException(500, f"提示词写盘失败：{e}") from e
    return {"ok": True, "target": target,
            "append_text": suggestion["append_text"]}


# ---------- 审片-重拍闭环·确定性档（批次 3 §33/§34：裁决/日志/止损/预览） ----------

# 未绑定 @提及扫描（前缀 + 名称，名称止于分隔符/括号/空白）——
# **正则单一真源在 script/prompt_optimize.py**（AI 优化的引用保护校验用同一份），
# 此处仅别名导入：两套正则会让「什么算一个引用」在两条链路上口径不同。
_MENTION_RE = MENTION_RE
_PFX_KIND = {"角色": "char", "场景": "scene", "道具": "prop"}


def _known_asset_names(db: Session, project_id: str) -> list[tuple[str, str]]:
    """项目资产名单 [(名称, kind)]（名称长度降序——长名优先命中）。"""
    known: list[tuple[str, str]] = []
    for c in db.scalars(select(Character).where(Character.project_id == project_id)):
        known.append((c.name, "char"))
    for s in db.scalars(select(Scene).where(Scene.project_id == project_id)):
        known.append((s.name, "scene"))
    for p in db.scalars(select(Prop).where(Prop.project_id == project_id)):
        known.append((p.name, "prop"))
    known.sort(key=lambda it: len(it[0]), reverse=True)
    return known


def _asset_id_by_name(db: Session, project_id: str, kind: str,
                      name: str) -> str | None:
    """按名称查资产实体 id（kind: char|scene|prop）。"""
    model = {"char": Character, "scene": Scene, "prop": Prop}[kind]
    return db.scalar(select(model.id).where(
        model.project_id == project_id, model.name == name))


def _asset_artifact_by_kind(db: Session, project_id: str, kind: str,
                            entity_id: str) -> str | None:
    """按资产类别解析当前生效参考图 artifact（与生成链路同口径）。"""
    if kind == "char":
        return _character_artifact(db, project_id, entity_id, None)
    if kind == "scene":
        scene = db.get(Scene, entity_id)
        return _scene_artifact(db, scene) if scene else None
    prop = db.get(Prop, entity_id)
    return _prop_artifact(db, prop) if prop else None


def _unresolved_mentions(db: Session, project_id: str, shot: Shot,
                         *, max_refs: int | None = None) -> list[dict]:
    """扫描视频提示词中未解析的 @提及（批次 3 §33.2 第二道闸）。

    _resolve_video_prompt 把命中的「前缀@名称」替换为「名称（参考图N）」、
    预算满/无产物的保留原文——对解析结果再扫一遍 @ 前缀即可枚举未绑定项，
    按成因四分：资产不存在（error）/ 资产缺图（error）/ 预算满（warn）。

    `max_refs` 缺省时**按本项目端点能力自行解析**（`video_ref_budget`，唯一来源）
    ——调用方不必各自算，也就不会再出现「这里写死 4、那里写 7」的两套口径。
    """
    if max_refs is None:
        max_refs = video_ref_budget(*_video_endpoint(db, project_id))
    prompt = shot.video_prompt or ""
    if not prompt.strip():
        return []
    final, refs = _resolve_video_prompt(db, project_id, shot, prompt,
                                        max_refs=max_refs)
    known = _known_asset_names(db, project_id)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in _MENTION_RE.finditer(final):
        kind = _PFX_KIND[m.group(1)]
        nm = m.group(2).strip()
        key = (kind, nm)
        if key in seen:
            continue
        seen.add(key)
        # 渐进最长前缀匹配（与主解析器同语义）：贪婪捕获的提及可能后随
        # 叙述文字（「角色@小美拿起第19版方案」）——known 按长度降序取
        # 第一个命中（提及==资产名 / 资产名是提及前缀），否则误报
        # 「资产不存在」拦截整批视频（终验实测误报）
        hit = next(((n, k) for (n, k) in known
                    if k == kind and (nm == n or n.startswith(nm)
                                      or nm.startswith(n))), None)
        if hit is None:
            out.append({"kind": m.group(1), "name": nm, "level": "error",
                        "reason": "资产不存在（名单外名称）"})
            continue
        entity_id = _asset_id_by_name(db, project_id, kind, hit[0])
        artifact = (_asset_artifact_by_kind(db, project_id, kind, entity_id)
                    if entity_id else None)
        if artifact is None:
            out.append({"kind": m.group(1), "name": nm, "level": "error",
                        "reason": "资产缺图（先在资产页生成）"})
        elif len(refs) >= max_refs:
            out.append({"kind": m.group(1), "name": nm, "level": "warn",
                        "reason": "参考图预算已满（可明示豁免）"})
    return out


def _observed_state(db: Session, project_id: str, shot: Shot) -> dict:
    """keep 裁决的观测态回写（§34.2 第 5 条）：实际时长 + 首尾帧 artifact。

    观测值来自真实产物（cv2 探针实测时长），continue 链与预检消费——
    「已接受的观测状态高于计划状态」。
    """
    observed: dict = {"observed_at": int(time.time())}
    video_art = _latest_artifact(db, project_id, f"shot_{shot.id}_v")
    ff_art = _latest_artifact(db, project_id, f"shot_{shot.id}_ff")
    observed["first_frame_artifact"] = ff_art
    observed["video_artifact"] = video_art
    if video_art:
        asset = db.get(Asset, video_art)
        if asset and asset.path and Path(asset.path).exists():
            probe = _probe_video(Path(asset.path))
            if probe:
                observed["duration_sec"] = round(probe.get("duration", 0), 2)
    return observed


class ShotVerdictIn(BaseModel):
    """镜头裁决入参（五级裁决按钮组）。"""

    verdict: str  # keep | post | edit | reroll | rewrite
    evidence: str = ""
    changed: str = ""
    # 重摇原因（25 号 2.4）：裁决是**记录**不是提交，故此处非必填；带了就校验
    # 取值合法后入日志（提交侧 generate_shots 才是必填闸门）
    reason: str = ""


@router.post("/shots/{shot_id}/verdict")
def review_shot(shot_id: str, body: ShotVerdictIn,
                db: Session = Depends(get_db)):
    """人工五级裁决落镜头日志；keep 回写观测态（实际时长/首尾帧）。"""
    shot = db.get(Shot, shot_id)
    if not shot:
        raise HTTPException(404, "分镜不存在")
    episode = db.get(Episode, shot.episode_id)
    if body.verdict not in ("keep", "post", "edit", "reroll", "rewrite"):
        raise HTTPException(400, f"无效裁决：{body.verdict}")
    # 多选原因同样归一（2026-10-10）：记录型入口可选填，带了就逐项校验
    reason = _norm_roll_reasons(body.reason)
    take_seq = (db.scalar(select(func.count()).select_from(ShotReview)
                          .where(ShotReview.shot_id == shot_id)) or 0) + 1
    observed = (_observed_state(db, episode.project_id, shot)
                if body.verdict == "keep" and episode else {})
    db.add(ShotReview(
        project_id=episode.project_id, episode_id=shot.episode_id,
        shot_id=shot_id, take_seq=take_seq, verdict=body.verdict,
        reason=reason, origin=REVIEW_ORIGIN_VERDICT,
        changed=(body.changed or "-")[:10], evidence=body.evidence,
        reviewer="user", observed_json=observed))
    # 人工反馈进教训账本（**全链路最准的信号**：人看过成片说「不对」）。
    # 只收「提示词写错了」这一类：裁决是重拍/重写、且单变量指向 prompt。
    #   · keep/post 是「这条能过」，不是教训；
    #   · changed=seed/refs/model/params 是别的变量不对（换种子/换参考图/换模型），
    #     不是提示词的锅——归档进来只会污染【历史教训】。
    # 备注（evidence）是用户自己写的白话，直接作为反面案例，不加工不改写。
    _blame_prompt = (
        body.verdict in PROMPT_BLAME_VERDICTS
        and (body.changed or "").strip().lower() in PROMPT_BLAME_CHANGED
        and (body.evidence or "").strip())
    if _blame_prompt and episode is not None:
        merge_lessons(db, episode.project_id, "prompt",
                      {"human_prompt_fix": [
                          f"镜{shot.seq}（用户重拍时改了提示词）："
                          f"{body.evidence.strip()}"]})
    db.commit()
    return {"ok": True, "take_seq": take_seq, "observed": observed}


@router.get("/shots/{shot_id}/reviews")
def shot_reviews(shot_id: str, db: Session = Depends(get_db)):
    """单镜裁决日志（Take N · changed · verdict · evidence——重读日志胜过
    重新经历；同缺陷两次的重摇被止损规则阻止）。"""
    rows = db.scalars(select(ShotReview).where(ShotReview.shot_id == shot_id)
                      .order_by(ShotReview.take_seq)).all()
    return {"ok": True, "reviews": [{
        "take_seq": r.take_seq, "verdict": r.verdict, "changed": r.changed,
        "reason": r.reason, "evidence": r.evidence, "reviewer": r.reviewer,
        "observed": r.observed_json or {}, "created_at": r.created_at,
    } for r in rows]}


class FirstFramePreferIn(BaseModel):
    """「设为首帧」入参：version_id 为空 = 回退跟随最新版本。"""

    version_id: str | None = None


@router.post("/shots/{shot_id}/prefer-first-frame")
def prefer_first_frame(shot_id: str, body: FirstFramePreferIn,
                       db: Session = Depends(get_db)):
    """在候选里挑定这一镜的首帧（2026-09-29：配合「一次抽 3 张」）。

    只接受属于本镜 `shot_{id}_ff` 节点的版本；后续视频生成、卡片展示、就绪度
    检查都按这个指针取首帧。**只改指针、不删任何产物**（挑错可随时改回）。
    """
    shot = db.get(Shot, shot_id)
    if not shot:
        raise HTTPException(404, "分镜不存在")
    episode = db.get(Episode, shot.episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    vid = (body.version_id or "").strip() or None
    if vid is not None:
        row = db.get(NodeVersion, vid)
        if (row is None or row.project_id != episode.project_id
                or row.node_id != f"shot_{shot.id}_ff"):
            raise HTTPException(400, "该版本不属于本镜的首帧候选")
    shot.first_frame_version_id = vid
    db.commit()
    return {"ok": True, "version_id": vid}


@router.get("/shots/{shot_id}/generation-params")
def shot_generation_params(shot_id: str, kind: str = "ff",
                           db: Session = Depends(get_db)):
    """单镜生成参数排查（GET /generation-params?kind=ff|v）。

    返回该镜首帧（shot_{id}_ff）或视频（shot_{id}_v）最新任务的完整参数：
    最终 prompt、input_refs（artifact id 有序——顺序即提示词「参考图N」
    编号）、模型与 runner 侧的 generation 结构化记录（截断告警等）。生成
    质量问题（串脸/构图错/方向反）先看这里对账「模型实际收到了什么」。
    """
    shot = db.get(Shot, shot_id)
    if not shot:
        raise HTTPException(404, "分镜不存在")
    if kind not in ("ff", "v"):
        raise HTTPException(400, f"无效类型：{kind}（ff=首帧 / v=视频）")
    task = db.scalar(select(Task).where(
        Task.node_id == f"shot_{shot_id}_{'ff' if kind == 'ff' else 'v'}"
    ).order_by(Task.created_at.desc(), Task.id.desc()).limit(1))
    if not task:
        return {"ok": True, "task": None}
    return {"ok": True, "task": {
        "id": task.id, "node_id": task.node_id, "runner": task.runner,
        "model_id": task.model_id, "status": task.status,
        "prompt": str((task.params_json or {}).get("prompt", "")),
        "params": {k: v for k, v in (task.params_json or {}).items()
                   if k != "prompt"},
        "input_refs": task.input_refs_json or {},
        "generation": (task.trace_json or {}).get("generation"),
        "created_at": task.created_at,
    }}


@router.get("/episodes/{episode_id}/reviews")
def episode_reviews(episode_id: str, db: Session = Depends(get_db)):
    """整集裁决日志（镜头日志时间线）。"""
    rows = db.scalars(select(ShotReview)
                      .where(ShotReview.episode_id == episode_id)
                      .order_by(ShotReview.shot_id, ShotReview.take_seq)).all()
    return {"ok": True, "reviews": [{
        "shot_id": r.shot_id, "take_seq": r.take_seq, "verdict": r.verdict,
        "changed": r.changed, "reason": r.reason, "evidence": r.evidence,
        "reviewer": r.reviewer,
        "observed": r.observed_json or {}, "created_at": r.created_at,
    } for r in rows]}


@router.get("/projects/{project_id}/reroll-reasons")
def reroll_reasons(project_id: str, db: Session = Depends(get_db)):
    """重摇原因分布（25 号 2.4 的数据飞轮：先有靶子，再谈优化）。

    **只统计用户真的提交过生成动作的记录**（`origin=submit`），并按 `changed`
    排除基建类失败（系统自动重试，用户没有"不满意"可言）。25 号复查抓到的口径
    错误：原先把**人工裁决意向**（在裁决面板点「重摇」，没提交）和 **AI 审片
    判级**也算进来——那两类的 verdict 同样是 reroll/edit/rewrite、且没有原因，
    于是「必填率」被永久拖低，还被当成了"有旁路绕过闸门"的证据。

    返回四件：
    - `by_reason` / `by_changed`：质量类重摇按原因、按单变量聚合；
    - `label_rate`：**提交侧原因填写率**（必填闸门的直接读数，应为 100%）；
    - `unlabeled`：没有原因的提交记录数（不为 0 = 真有旁路或旧版本提交）；
    - `other_origins`：裁决意向 / AI 判级 / 历史行的条数（**只作参照**，
      不参与填写率）。
    """
    _project(db, project_id)
    rows = db.scalars(
        select(ShotReview).where(ShotReview.project_id == project_id)
        .order_by(ShotReview.created_at.desc())).all()
    by_reason: dict[str, int] = {}
    by_changed: dict[str, int] = {}
    unlabeled = 0
    samples: list[dict] = []
    quality_total = 0
    other_origins = {"verdict": 0, "auto": 0, "legacy": 0}
    for r in rows:
        if (r.changed or "").startswith("infra:"):
            continue
        if r.verdict not in ("reroll", "rewrite", "edit"):
            continue          # keep/post 是「这条能过」，不是重摇
        origin = r.origin or ""
        if origin != REVIEW_ORIGIN_SUBMIT:
            other_origins[origin or "legacy"] = (
                other_origins.get(origin or "legacy", 0) + 1)
            continue
        quality_total += 1
        by_changed[r.changed or "-"] = by_changed.get(r.changed or "-", 0) + 1
        if r.reason:
            # 多选后按**单个原因**分别计数（2026-10-10）：一条 "identity,pose"
            # 记给 identity 和 pose 各一次——分布表要回答的是"哪个问题出现得
            # 最多"，一组原因整串计数会让两个问题都数不到
            for _part in r.reason.split(","):
                _part = _part.strip()
                if _part:
                    by_reason[_part] = by_reason.get(_part, 0) + 1
        else:
            unlabeled += 1
        if len(samples) < 20:
            samples.append({"shot_id": r.shot_id, "take_seq": r.take_seq,
                            "verdict": r.verdict, "changed": r.changed,
                            "reason": r.reason, "evidence": r.evidence[:120],
                            "created_at": r.created_at})
    return {
        "ok": True,
        "quality_rerolls": quality_total,
        "unlabeled": unlabeled,
        "label_rate": (round(1 - unlabeled / quality_total, 3)
                       if quality_total else 1.0),
        "other_origins": other_origins,
        "by_reason": [{"reason": k, "count": v}
                      for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])],
        "by_changed": [{"changed": k, "count": v}
                       for k, v in sorted(by_changed.items(), key=lambda kv: -kv[1])],
        "samples": samples,
    }


@router.get("/episodes/{episode_id}/generate-preview")
def generate_preview(episode_id: str, db: Session = Depends(get_db)):
    """生成清单预览（报告铁律 2 轻量版）：数量/时长/模型/风险清单——确认
    后再调 generate（信息性预览，不改变生成语义）。"""
    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    shots = db.scalars(select(Shot).where(Shot.episode_id == episode_id)
                       .order_by(Shot.seq)).all()
    missing_chars: list[str] = []
    unbound_error = 0
    unbound_sample: list[str] = []
    # 参考图预算与提交侧同源（agnes flash 5 / 2.5 pro 8）：写死 4 会把「第 5~7 张
    # 本该绑上的引用」误判成「预算满」而只报 warn（25 号 1.6）
    _pv_budget = video_ref_budget(*_video_endpoint(db, episode.project_id))
    for shot in shots:
        for cid in shot.character_ref_ids_json or []:
            if _character_artifact(db, episode.project_id, cid, None):
                continue
            char = db.get(Character, cid)
            if char and char.name not in missing_chars:
                missing_chars.append(char.name)
        if (shot.video_prompt or "").strip():
            for it in _unresolved_mentions(db, episode.project_id, shot,
                                           max_refs=_pv_budget):
                if it["level"] != "error":
                    continue
                unbound_error += 1
                if len(unbound_sample) < 5:
                    unbound_sample.append(
                        f"镜{shot.seq}:{it['kind']}@{it['name']}（{it['reason']}）")
    qa_setting = db.get(Setting, storyboard_qa_key(episode.project_id))
    qa_errors: list[dict] = []
    if qa_setting and qa_setting.value:
        try:
            qa_all = json.loads(qa_setting.value)
        except ValueError:
            qa_all = []
        # 与提交前硬门同口径：只统计【本集】的 error（漏 episode 维度会把
        # 别集错误算进本集预览，用户看到与实际拦截不一致的清单）
        qa_errors = [i for i in qa_all if i.get("level") == "error"
                     and i.get("episode") in (None, episode.seq)]
    # 端点健康（25 号 1.5）：欠费/队列满在**确认框里**就说清楚——用户点「确认
    # 生成」之前知道端点接不了单，而不是等每条任务各失败一次（批量 42 镜 = 42 次）
    from app.services.readiness import provider_health_check

    _health_notes = []
    for _kind, _label in ((KIND_T2I, "生图"), (KIND_T2V, "视频")):
        _h = provider_health_check(db, episode.project_id, _kind)
        if not _h["ok"]:
            _health_notes.append(_h["message"])
    return {"ok": True,
            "shots": len(shots),
            "total_duration": sum(s.duration for s in shots),
            "model": _resolve_model(db, episode.project_id, KIND_T2V, ""),
            "missing_characters": missing_chars,
            "unresolved_error_count": unbound_error,
            "unresolved_sample": unbound_sample,
            "qa_errors": len(qa_errors),
            "provider_warnings": _health_notes}


# ---------- 视觉自动判级（批次 3.5 §34.3：抽帧拼图 + 多模态 LLM 判级） ----------

_VERDICTS_VALID = ("keep", "post", "edit", "reroll", "rewrite")


def _grab_frame_grid(path: Path, picks: tuple[float, ...] = (0.08, 0.5, 0.92)) -> str | None:
    """视频抽三帧横向拼图 → jpeg base64（视觉判级输入；失败返回 None）。

    picks 为归一化取样时刻（首/中/尾避开片头片尾黑帧）。cv2 函数内导入
    （加载重，未用到审片不付启动成本——与 _probe_video 同口径）。
    """
    try:
        import base64

        import cv2
        import numpy as np
    except ImportError:  # noqa: BLE001
        return None
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    try:
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frames <= 0:
            return None
        tiles: list = []
        for t in picks:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(frames - 1, int(frames * t))))
            ok, frame = cap.read()
            if ok:
                tiles.append(frame)
        if not tiles:
            return None
        # 拼图缩放到统一高度（原图高度不一可能来自不同分辨率，取最小高）
        h = min(f.shape[0] for f in tiles)
        resized = [cv2.resize(f, (int(f.shape[1] * h / f.shape[0]), h))
                   for f in tiles]
        grid = np.hstack(resized)
        ok, buf = cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            return None
        return base64.b64encode(buf.tobytes()).decode("ascii")
    finally:
        cap.release()


@router.post("/episodes/{episode_id}/auto-review")
def auto_review_episode(episode_id: str, db: Session = Depends(get_db)):
    """视觉自动判级（批次 3.5）：逐镜抽帧拼图 → 多模态 LLM 五级裁决。

    前置：默认 llm 端点勾选「支持多模态」（模型管理），未勾选 400 指引；
    结果以 reviewer=auto 落 ShotReview（人工可覆判——auto 是建议不是终裁）；
    单镜判级失败跳过不阻断（fail-open，与确定性审片同语义）。
    """
    from app.core.providers import llm_endpoint_multimodal as _llm_mm

    episode = db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(404, "分集不存在")
    # 项目级 LLM 预设（空串 = 全局默认）。本函数**直接调 llm_chat**（不经
    # 任务引擎），故显式传参即可——比上下文更直白，也保证准入判定与
    # 实际调用是同一个端点
    _llm_preset = project_preset(db.get(Project, episode.project_id), KIND_LLM)
    if not _llm_mm(_llm_preset):
        raise HTTPException(
            400, "当前默认 LLM 端点未勾选「支持多模态」：请在「系统配置-模型"
                 "管理」编辑 LLM 端点勾选后重试（判级需要把镜头抽帧拼图发给"
                 "多模态模型）")
    shots = db.scalars(select(Shot).where(Shot.episode_id == episode_id)
                       .order_by(Shot.seq)).all()
    results: list[dict] = []
    for shot in shots:
        video_art = _latest_artifact(db, episode.project_id, f"shot_{shot.id}_v")
        if not video_art:
            continue
        asset = db.get(Asset, video_art)
        if not asset or not asset.path or not Path(asset.path).exists():
            continue
        b64 = _grab_frame_grid(Path(asset.path))
        if not b64:
            continue
        user_payload = json.dumps({
            "seq": shot.seq, "duration": shot.duration,
            "description": shot.description, "dialogue": shot.dialogue_json,
            "camera": shot.camera, "scene_name":
                (db.get(Scene, shot.scene_ref_id).name
                 if shot.scene_ref_id and db.get(Scene, shot.scene_ref_id) else ""),
            "character_names": [
                (db.get(Character, cid).name if db.get(Character, cid) else "?")
                for cid in (shot.character_ref_ids_json or [])],
        }, ensure_ascii=False)
        messages = [
            {"role": "system", "content": prompts_mod.AUTO_REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"分镜设定：\n{user_payload}"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ]
        try:
            data = _extract_json(llm_chat(
                messages, temperature=0.2, max_tokens=1024, timeout=120.0,
                model_id=_llm_preset))
            violations = contract_violations(data, AutoReviewOut)
            if violations:
                raise ValueError(violations[0])
            verdict = _as_str(data.get("verdict")).lower()
            if verdict not in _VERDICTS_VALID:
                raise ValueError(f"判级越界：{verdict}")
            evidence = _as_str(data.get("evidence"))[:300]
        except (ProviderError, ValueError, json.JSONDecodeError) as e:
            results.append({"shot_id": shot.id, "seq": shot.seq,
                            "verdict": "", "error": str(e)[:120]})
            continue
        take_seq = (db.scalar(select(func.count()).select_from(ShotReview)
                              .where(ShotReview.shot_id == shot.id)) or 0) + 1
        db.add(ShotReview(
            project_id=episode.project_id, episode_id=episode_id,
            shot_id=shot.id, take_seq=take_seq, verdict=verdict,
            origin=REVIEW_ORIGIN_AUTO,
            changed="-", evidence=evidence, reviewer="auto",
            observed_json={"frames": "first/mid/last"}))
        results.append({"shot_id": shot.id, "seq": shot.seq,
                        "verdict": verdict, "evidence": evidence})
    db.commit()
    return {"ok": True, "results": results,
            "auto_count": sum(1 for r in results if r.get("verdict"))}
