"""解析前置需求（21 号批次 5）：AI 解析前收集的少量关键参数。

**为什么加这一环**：官方短剧 skill 的 planner 阶段有明确的「澄清清单」——「在剧情设计、
剧本设定和视频生成开始前，建议主动澄清会显著影响结果的关键参数」。我们此前没有这一环，
一切靠 LLM 自己猜，下游（分镜节奏、爽感机制、技能包选择）只能吃猜出来的基调 ——
这是「解析完发现不对味、要重来」的一类返工来源。

**收集原则（用户口径：合理不过度、抓重点）**：每个字段都必须回答「不填会怎样」，
答不上来的不收。项目新建时已收集的（画风/画幅/国家/模型预设）与剧本里已有的
（故事主题/人物/冲突/结局）**一律不重复收集**。

**存储**：`Setting drama_requirements:{project_id}`（**不改 Project 表 → 零迁移**）——
需求是「每次解析的指令」（重解析可能调整），不是项目固有属性。
"""

from __future__ import annotations

import json
from sqlalchemy.orm import Session

from app.db.models import Project, Setting

# 需求存档键前缀（**不得改名**：改名等于丢弃所有存量项目已填需求）
REQUIREMENTS_KEY_PREFIX = "drama_requirements:"

# —— 剧作基调（官方两套剧作法，决定分镜节奏档与剧作层法则）——
TONE_HOOK = "hook"     # 短视频钩子驱动：3 秒钩子、反转密、爽点集中释放
TONE_FILM = "film"     # 微电影三幕式：铺陈→冲突→高潮落点，允许留白与呼吸
TONE_LABELS = {TONE_HOOK: "短视频钩子驱动", TONE_FILM: "微电影三幕式"}
DEFAULT_TONE = TONE_HOOK

# —— 目标受众（官方：国内吃集体主义爽感、海外吃个人主义爽感）——
AUDIENCE_DOMESTIC = "domestic"
AUDIENCE_OVERSEAS = "overseas"
AUDIENCE_LABELS = {AUDIENCE_DOMESTIC: "国内（大众/下沉）", AUDIENCE_OVERSEAS: "海外"}
DEFAULT_AUDIENCE = AUDIENCE_DOMESTIC

# 单集目标时长可选档与默认（官方默认 6 分钟）
EPISODE_MINUTE_OPTIONS: tuple[int, ...] = (5, 6, 8, 10)
DEFAULT_EPISODE_MINUTES = 6

# 需求字段上限（防空值/超长；note 与 banned 是自由文本）
MAX_BANNED_CHARS = 300
MAX_NOTE_CHARS = 500
MAX_GENRE_CHARS = 40


def requirements_key(project_id: str) -> str:
    """需求存档键。"""
    return f"{REQUIREMENTS_KEY_PREFIX}{project_id}"


def normalize_requirements(raw: dict | None) -> dict:
    """归一化用户提交的需求（**白名单 + 档位校验**，非法值回退默认）。

    确定性处理：不认识的键丢弃、非法枚举回默认、超长截断——
    避免把脏数据写进存档并在注入时污染提示词。
    """
    src = raw if isinstance(raw, dict) else {}
    tone = str(src.get("tone") or "").strip()
    if tone not in TONE_LABELS:
        tone = DEFAULT_TONE
    audience = str(src.get("audience") or "").strip()
    if audience not in AUDIENCE_LABELS:
        audience = DEFAULT_AUDIENCE
    try:
        minutes = int(src.get("episode_minutes") or DEFAULT_EPISODE_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_EPISODE_MINUTES
    if minutes not in EPISODE_MINUTE_OPTIONS:
        minutes = DEFAULT_EPISODE_MINUTES
    return {
        "tone": tone,
        "episode_minutes": minutes,
        "genre": str(src.get("genre") or "").strip()[:MAX_GENRE_CHARS],
        "audience": audience,
        "style_anchor": str(src.get("style_anchor") or "").strip()[:40],
        "banned": str(src.get("banned") or "").strip()[:MAX_BANNED_CHARS],
        "note": str(src.get("note") or "").strip()[:MAX_NOTE_CHARS],
    }


def save_requirements(db: Session, project_id: str, raw: dict | None) -> dict:
    """保存需求（归一化后落 Setting），返回归一化结果。"""
    data = normalize_requirements(raw)
    db.merge(Setting(key=requirements_key(project_id),
                     value=json.dumps(data, ensure_ascii=False)))
    db.commit()
    return data


def load_requirements(db: Session, project_id: str) -> dict:
    """读取需求（无存档/损坏 → 返回默认值，**旧项目解析不报错**）。"""
    row = db.get(Setting, requirements_key(project_id))
    if not row or not (row.value or "").strip():
        return normalize_requirements(None)
    try:
        return normalize_requirements(json.loads(row.value))
    except (json.JSONDecodeError, TypeError):
        return normalize_requirements(None)


def review_hint(req: dict, project: Project | None = None) -> str:
    """I1：注入审读（SCRIPT_REVIEW）的需求段落。

    作用：让**技能包自选**与 synopsis/关键场次判断贴合用户口径，而不是模型自己猜基调。
    """
    lines = [f"剧作基调：{TONE_LABELS.get(req.get('tone'), '')}"]
    tone = req.get("tone")
    if tone == TONE_HOOK:
        lines[0] += ("（3 秒强钩子、快速冲突堆叠、爽点集中释放；每章节至少一次反转；"
                     "结尾给明确落点，不用「待续」收尾）")
    elif tone == TONE_FILM:
        lines[0] += ("（单一核心事件、经典三幕式 25%/50%/25%；允许留白与呼吸；"
                     "落点优先情感闭环而非打脸）")
    lines.append(f"单集目标时长：约 {req.get('episode_minutes')} 分钟")
    genre = req.get("genre") or ""
    lines.append(f"题材：{genre}" if genre else "题材：AI 自动判断")
    audience = req.get("audience")
    if audience == AUDIENCE_OVERSEAS:
        lines.append("目标受众：海外（个人主义爽感——宿命拉扯、独立觉醒、荷尔蒙张力）")
    else:
        lines.append("目标受众：国内（集体主义爽感——围观群众与势利眼配角衬托主角）")
    if project is not None:
        # 函数内导入防循环：persist 也要读本模块（meta 里存需求），顶层导入会成环
        from app.script.persist import anchor_prompt_for

        anchor = anchor_prompt_for(project.style_preset,
                                   str(getattr(project, "style_anchor", "") or "")
                                   or req.get("style_anchor") or "")
        if anchor:
            lines.append(f"风格锚点：{anchor}（全局风格限定词，各阶段一字不差沿用）")
    if req.get("banned"):
        lines.append(f"禁用内容：{req['banned']}")
    if req.get("note"):
        lines.append(f"补充说明：{req['note']}")
    return "【用户需求】（本项目定向要求，与剧本冲突时以本需求为准）\n" + "\n".join(
        f"- {line}" for line in lines)


def tone_craft_hint(req: dict) -> str:
    """I3：注入逐集拆解（SCRIPT_PARSE）的节奏档指引。

    官方两套节奏表的**取向**（不是逐字搬运 15 秒镜头数——本链路单镜 5-12 秒，
    与官方的 15 秒片段粒度不同，直接搬镜头数会误导；搬的是**节奏取向与留白纪律**）。
    """
    if req.get("tone") == TONE_FILM:
        return ("本剧为**微电影三幕式**：节奏允许中速与慢速——允许 2-3 秒静态凝视、"
                "无对白空镜、环境音铺陈承担情绪；允许 5-8 秒的呼吸镜头；"
                "不因为担心「划走」而删掉留白（留白是情绪沉淀的必需成本）。")
    return ("本剧为**短视频钩子驱动**：节奏走快档——单镜信息密度高、动作与台词紧凑、"
            "避免长留白与静态凝视；钩子/打脸/高潮段允许更碎的切分；"
            "整集不允许出现长时间没有信息增量或情绪推进的镜头。")


def constraint_line(req: dict) -> str:
    """I4：注入画面描述/合规的禁用内容约束行（无禁用内容时返回空串）。"""
    banned = req.get("banned") or ""
    return f"【画面与台词均不得出现】{banned}" if banned else ""
