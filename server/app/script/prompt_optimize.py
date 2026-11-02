"""AI 优化提示词（20 号批次 3）：把用户的自然语言修改需求，结合**完整上下文**，
改写为一条可直接使用的生图 / 生视频提示词。

三条设计约束（对应方案 §5）：

1. **纯计算不落库** —— 本模块只返回候选文本与校验结果；写回由用户在弹窗点
   「应用」后走**既有** PATCH 端点（不新增写路径，避免绕过既有校验）。
2. **上下文由服务端组装** —— 前端只传 `target + id + instruction`，避免前端遗漏
   字段、也避免上下文随版本漂移（字段清单见 `_context_of_*`）。
3. **确定性校验** —— 生成结果必须过：① 引用集合不变 ② 引用名与出场资产名都在
   名单内 ③ 不得写画风/质量词 ④ 质量指标（长度/冗余率/配额）。
   校验失败**不静默放行**：`warnings` 随结果一起返回，前端在弹窗里如实展示。

与流水线共用同一份「每字有用」判据（`prompts.PROMPT_CRAFT_RULES`），
保证「一键优化」不会变成「一键制造废话」。
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from app.agent import prompts as _prompts
from app.core.prompt_metrics import prompt_quality_metrics
from app.core.providers import ProviderError, llm_available, llm_chat
from app.db.models import Character, CharacterImage, Episode, Prop, Scene, Shot
from app.script.persist import project_style_context
from app.script.prompt_blocks import (
    strip_character_system_blocks, strip_prop_system_blocks,
    strip_scene_system_blocks,
)

logger = logging.getLogger(__name__)

# —— 优化对象类型（与前端 target 字段一一对应）——
TARGET_CHARACTER = "character"   # 角色定妆图 image_prompt
TARGET_COSTUME = "costume"       # 角色妆造 appearance_desc
TARGET_SCENE = "scene"           # 场景主图 image_prompt
TARGET_PROP = "prop"             # 道具图 image_prompt
TARGET_SHOT = "shot"             # 分镜 video_prompt
OPTIMIZE_TARGETS: tuple[str, ...] = (
    TARGET_CHARACTER, TARGET_COSTUME, TARGET_SCENE, TARGET_PROP, TARGET_SHOT)

# @引用形态（**全链路单一真源**）：角色@顾清寒 / 场景@老周面馆 / 道具@写字板。
# 名称止于分隔符/括号/空白——`api/drama.py` 的未绑定提及扫描与本模块的引用保护
# 校验共用这一份：两套正则会让「什么算一个引用」在两条链路上口径不同。
MENTION_RE = re.compile(r"(角色|场景|道具)@([^，。！？；、\n（()）\s]+)")

# 「不写画风与质量词」检查（约束 3）：正文出现这些词即告警——画风由系统统一注入
STYLE_WORD_MARKERS: tuple[str, ...] = (
    "电影感", "高级质感", "氛围感", "画质", "8K", "4K", "超高清", "高清",
    "写实电影质感", "赛博朋克风格", "水墨风格", "3D 渲染", "二次元",
)

# 优化结果的长度上限（与 t2i 端点预算同量级；超限在 warnings 里点名）
OPTIMIZE_MAX_CHARS = 600
# 「最小改动」保留率红线（**确定性对账**）：优化结果与原文的相似度（difflib 比例）
# 低于该值 = 模型没有按「微调」执行，而是重写了整条（实测反面案例：用户只要求改
# 发型，结果连画风句与一个五官细节都被删掉，相似度掉到 0.66）。命中即**定向重试
# 一次**并把提示写进重试消息；重试仍低只告警、不拦（结果照给用户看，他自己判断）。
MIN_PRESERVE_RATIO = 0.70


def _preserve_ratio(current: str, new_prompt: str) -> float:
    """优化结果与原文的相似度（0-1）。纯字符串计算，判「改了多少」不靠感觉。"""
    import difflib

    a, b = (current or "").strip(), (new_prompt or "").strip()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _deleted_spans(current: str, new_prompt: str, min_len: int = 6,
                   gap: int = 4) -> list[str]:
    """优化结果里**被删掉的原文片段**（逐段列出，≥ min_len 字）。

    为什么不能只靠相似度红线：实测用户报障那次的相似度是 **0.774 > 0.70**——它删掉了
    开头的画风句与「鼻梁挺直」，但整条仍有七成多字符相同，红线抓不住。所以这里做
    **逐段对账**：把原文按差异切成保留/删除两段，凡是被删且不短的片段都列出来交给
    用户判断（他不要求删的地方被删掉，一眼就能看见并点重试）。
    纯字符串计算（difflib 差异块），不做任何语义判断。
    """
    import difflib

    a, b = (current or "").strip(), (new_prompt or "").strip()
    if not a or not b:
        return []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: list[str] = []

    def _flush() -> None:
        _seg = buf.strip("，。、；： ")
        if len(_seg) >= min_len:
            out.append(_seg)

    buf = ""
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag == "equal":
            seg = a[i1:i2]
            if len(seg) >= gap:
                _flush()          # 足够长的相同段 = 真正的边界
                buf = ""
            else:
                buf += seg        # 短相同段（difflib 的对齐碎片）留在同一块里
        elif tag == "insert":
            continue              # 插入不打断删除块（改写 = 删一段 + 插一段）
        else:                      # delete / replace：原文侧被改掉的部分
            buf += a[i1:i2]
    _flush()
    return out


def _leading_style_block(current: str, keywords: str, anchor: str) -> str:
    """原文开头的**画风块**（含句末标点）；判据 = 首句里含画风锚点或关键词。

    用途：模型把用户原文里的画风句删掉时（规则 4 要求原样保留），由代码**原样补回**——
    画风句是系统与用户共同维护的固定块，缺了它用户会以为整条被重写（实测报障）。
    只在首句确实含画风词时才认，避免把普通首句误当画风块。
    """
    head, sep, _tail = (current or "").partition("。")
    if not sep:
        return ""
    if anchor and anchor.strip() and anchor.strip() in head:
        return head + sep
    for kw in [k for k in (keywords or "").split("，") if k.strip()][:3]:
        if kw.strip() in head:
            return head + sep
    return ""


def _refs_of(text: str) -> set[tuple[str, str]]:
    """抽取文本中的 @引用集合（（类型, 名称）元组集）。"""
    return {(m.group(1), m.group(2).strip()) for m in MENTION_RE.finditer(text or "")}


def _asset_names(db: Session, project_id: str) -> dict[str, set[str]]:
    """项目资产名单（引用白名单：引用名必须逐字来自这里）。"""
    chars = db.query(Character).filter(Character.project_id == project_id).all()
    scenes = db.query(Scene).filter(Scene.project_id == project_id).all()
    props = db.query(Prop).filter(Prop.project_id == project_id).all()
    return {
        "角色": {c.name for c in chars},
        "场景": {s.name for s in scenes},
        "道具": {p.name for p in props},
    }


def _clip(text: str, limit: int) -> str:
    """上下文文本裁剪（避免把整篇人设塞进 payload；超长截断并留省略号）。"""
    body = (text or "").strip()
    return body if len(body) <= limit else body[:limit].rstrip() + "…"


def _context_character(db: Session, project_id: str, obj) -> tuple[str, dict, list[str]]:
    """角色定妆：上下文 = 本人人设/外观/音色 + 同剧其他角色外观（差异化约束）。"""
    others = (db.query(Character)
              .filter(Character.project_id == project_id, Character.id != obj.id)
              .all())
    ctx = {
        "persona": _clip(obj.persona, 400),
        "appearance": _clip(obj.appearance, 300),
        "voice_desc": _clip(obj.voice_desc, 120),
        "role": obj.role or "",
        "同剧其他角色（用于拉开差异，防串脸）": [
            {"name": c.name, "appearance": _clip(c.appearance, 120)} for c in others[:6]
        ],
    }
    digest = ["角色人设", "角色外观", "音色描述",
              f"同剧其他角色 ×{min(len(others), 6)}（防串脸差异约束）"]
    return obj.image_prompt or "", ctx, digest


def _context_costume(db: Session, project_id: str, obj) -> tuple[str, dict, list[str]]:
    """角色妆造：上下文 = 基础定妆 + 该角色人设/外观 + 妆造适用集数。

    `obj` 可为 CharacterImage（已有妆造形象）或 Character（**妆造建议草稿**——
    建议尚未落成形象记录，UI 上「微调描述」编辑的是角色 id + 草稿文本）。
    """
    if isinstance(obj, Character):
        char, costume_name, episodes = obj, "（妆造建议草稿）", []
    else:
        char = db.get(Character, obj.character_id)
        costume_name, episodes = obj.name or "", obj.episodes_json or []
    ctx = {
        "角色": {
            "name": char.name if char else "",
            "persona": _clip(char.persona if char else "", 300),
            "appearance": _clip(char.appearance if char else "", 300),
            # 剥系统块（画风前缀/负向词）：2026-09-30 组装写回后字段里含这些，
            # 原样交给 LLM 重写会被改写或复述成两份
            "基础定妆提示词": _clip(
                strip_character_system_blocks(char.image_prompt if char else ""), 400),
        },
        "妆造名称": costume_name,
        "适用集数": episodes,
    }
    digest = ["角色人设", "角色外观", "角色基础定妆提示词", "妆造名称与适用集数"]
    current = "" if isinstance(obj, Character) else (obj.appearance_desc or "")
    return current, ctx, digest


def _context_scene(db: Session, project_id: str, obj) -> tuple[str, dict, list[str]]:
    """场景主图：上下文 = 场景描述 + 空间布局声明。

    current 取**剥净系统块**的文本（2026-09-30：提示词框里存的是组装后的完整
    提示词——画风前缀/空镜约束/空间布局/负向词；把系统块交给 LLM 重写会被改写或
    复述成两份。系统块由生成阶段的组装函数统一补回，这里只优化画面描述本身）。
    """
    ctx = {"description": _clip(obj.description, 400),
           "layout（空间布局声明）": _clip(obj.layout, 300),
           "出现集数": obj.episodes_json or []}
    digest = ["场景描述", "空间布局声明（出入口/门窗/固定陈设方位）", "出现集数"]
    return strip_scene_system_blocks(obj.image_prompt or "", obj.layout or ""), ctx, digest


def _context_prop(db: Session, project_id: str, obj) -> tuple[str, dict, list[str]]:
    """道具图：上下文 = 道具描述 + 出现集数。

    current 取**剥净系统块**的文本（同场景：去掉画风前缀与负向词，只优化描述本体）。
    """
    ctx = {"description": _clip(obj.description, 300),
           "出现集数": obj.episodes_json or []}
    digest = ["道具描述", "出现集数"]
    return strip_prop_system_blocks(obj.image_prompt or ""), ctx, digest


def _context_shot(db: Session, project_id: str, obj) -> tuple[str, dict, list[str]]:
    """分镜视频提示词：上下文 = 本镜全量结构化信息 + 出场资产 + 前序镜衔接。"""
    chars = (db.query(Character).filter(Character.id.in_(obj.character_ref_ids_json)).all()
             if obj.character_ref_ids_json else [])
    props = (db.query(Prop).filter(Prop.id.in_(obj.prop_ref_ids_json)).all()
             if obj.prop_ref_ids_json else [])
    scene = db.get(Scene, obj.scene_ref_id) if obj.scene_ref_id else None
    # 前序镜摘要（衔接：上一镜结尾状态决定本镜起幅）
    prev = (db.query(Shot)
            .filter(Shot.episode_id == obj.episode_id, Shot.seq < obj.seq)
            .order_by(Shot.seq.desc()).limit(2).all())
    ctx = {
        "description": _clip(obj.description, 300),
        "excerpt（剧本原文摘抄）": _clip(obj.excerpt, 300),
        "dialogue（台词，原句不得改动）": obj.dialogue_json or [],
        "camera（运镜）": obj.camera or "",
        "duration（本镜秒数，不得改动）": obj.duration,
        "state（镜头开始状态）": obj.state_json or {},
        "end_state（镜头结束状态）": obj.end_state_json or {},
        "transition_in（与上一镜衔接类型）": obj.transition_in or "",
        "出场角色": [
            {"name": c.name, "persona": _clip(c.persona, 150),
             "appearance": _clip(c.appearance, 150),
             "定妆提示词": _clip(
                 strip_character_system_blocks(c.image_prompt or ""), 200)}
            for c in chars
        ],
        "出场场景": ({"name": scene.name, "description": _clip(scene.description, 200),
                      "layout": _clip(scene.layout, 200)} if scene else {}),
        "出场道具": [{"name": p.name, "description": _clip(p.description, 150)} for p in props],
        "前序镜头（衔接参考）": [
            {"seq": s.seq, "description": _clip(s.description, 120)} for s in reversed(prev)
        ],
    }
    digest = ["本镜画面描述", "剧本原文摘抄", "台词原句（不得改）", "运镜与秒数（不得改）",
              "镜头开始/结束状态", "与上一镜衔接类型",
              f"出场角色 ×{len(chars)}（人设/外观/定妆）",
              "出场场景（描述/空间布局）", f"出场道具 ×{len(props)}",
              f"前序镜头 ×{len(prev)}（衔接）"]
    return obj.video_prompt or "", ctx, digest


_CONTEXT_BUILDERS = {
    TARGET_CHARACTER: _context_character,
    TARGET_COSTUME: _context_costume,
    TARGET_SCENE: _context_scene,
    TARGET_PROP: _context_prop,
    TARGET_SHOT: _context_shot,
}

_TARGET_MODELS = {
    TARGET_CHARACTER: Character,
    TARGET_COSTUME: CharacterImage,
    TARGET_SCENE: Scene,
    TARGET_PROP: Prop,
    TARGET_SHOT: Shot,
}


def _owner_project_id(db: Session, target: str, obj) -> str | None:
    """解析对象所属项目 id（归属校验用）。

    Shot 挂在 episode 下、CharacterImage 挂在 character 下——两者都没有
    `project_id` 列，必须反查，否则归属校验会把合法对象误判为越权。
    Character 本身有 project_id（妆造建议草稿场景直接返回）。
    """
    if target == TARGET_COSTUME and not isinstance(obj, Character):
        char = db.get(Character, obj.character_id)
        return getattr(char, "project_id", None)
    if target == TARGET_SHOT:
        episode = db.get(Episode, obj.episode_id)
        return getattr(episode, "project_id", None)
    return getattr(obj, "project_id", None)


def optimize_prompt(db: Session, project_id: str, target: str, obj_id: str,
                    instruction: str, current_override: str = "") -> dict:
    """执行一次 AI 优化（纯计算，不落库）。

    `current_override`：**编辑中的文本**（可空）。仅用于「编辑对象不是持久字段」的
    场景——典型是妆造建议的「微调描述」草稿（此时 `obj_id` 传角色 id，尚无形象记录）。

    返回 `{prompt, notes, metrics, context_digest, warnings, target}`；
    参数非法 / LLM 不可用 → 抛 `ValueError` / `ProviderError`（API 层转 4xx/5xx）。
    """
    if target not in OPTIMIZE_TARGETS:
        raise ValueError(f"未知优化对象：{target}（可选：{'、'.join(OPTIMIZE_TARGETS)}）")
    if not (instruction or "").strip():
        raise ValueError("请先写明修改需求")
    obj = db.get(_TARGET_MODELS[target], obj_id)
    if obj is None and target == TARGET_COSTUME:
        # 妆造建议草稿：尚无形象记录，id 传的是角色 id（上下文由该角色构建）
        obj = db.get(Character, obj_id)
    if obj is None:
        raise ValueError("优化对象不存在（可能已被删除）")
    # 归属校验：对象必须属于该项目（防越权改到别人的资产）。
    # Shot 没有 project_id（经 episode 反查）、CharacterImage 经 character 反查
    if _owner_project_id(db, target, obj) != project_id:
        raise ValueError("优化对象不属于当前项目")

    current, ctx, digest = _CONTEXT_BUILDERS[target](db, project_id, obj)
    if not (current or "").strip():
        current = (current_override or "").strip()   # 草稿：用编辑中的文本
    if not (current or "").strip():
        raise ValueError("当前提示词为空——请先生成或填写提示词再来优化")

    style_ctx = project_style_context(db, project_id)
    names = _asset_names(db, project_id)
    payload = {
        "target": target,
        "current_prompt": current,
        "instruction": instruction.strip(),
        "context": ctx,
        "style": {
            "anchor": style_ctx.get("anchor_prompt", ""),
            "keywords": _clip(_style_keywords_of(style_ctx), 200),
            "guide": "",
        },
        # 名单只给与本次相关的类型（全量名单会诱导模型写入无关资产）
        "names": {
            "characters": sorted(_names_for(target, "角色", names,
                                            ctx, current)),
            "scenes": sorted(_names_for(target, "场景", names, ctx, current)),
            "props": sorted(_names_for(target, "道具", names, ctx, current)),
        },
    }

    if not llm_available():
        raise ProviderError("LLM 未配置：请在「模型管理-服务配置」中填写 LLM 服务后使用 AI 优化")
    style_block = _leading_style_block(
        current, payload["style"]["keywords"] or "", payload["style"]["anchor"] or "")
    _user_msg = json.dumps(payload, ensure_ascii=False)
    parsed: dict = {}
    new_prompt = ""
    ratio = 0.0
    # 最多两轮：第一轮按契约写；若「改得太多」（保留率低于红线）就带着**定向提示**
    # 再要一次——「哪里不行」而不是笼统「再试一次」（与解析侧重试同款纪律）
    for attempt in (1, 2):
        raw = llm_chat(
            [{"role": "system", "content": _prompts.PROMPT_OPTIMIZE_SYSTEM_PROMPT},
             {"role": "user", "content": _user_msg}],
            json_mode=True, temperature=0.4,
        )
        parsed = _parse_reply(raw)
        cand = str(parsed.get("prompt") or "").strip()
        if not cand:
            continue
        cand_ratio = _preserve_ratio(current, cand)
        if cand_ratio >= MIN_PRESERVE_RATIO or attempt == 2:
            new_prompt, ratio = cand, cand_ratio
            break
        logger.info("AI 优化改得太多（保留率 %.2f < %.2f），定向重试一次",
                    cand_ratio, MIN_PRESERVE_RATIO)
        _user_msg = json.dumps({
            **payload,
            "instruction": (
                instruction.strip()
                + "。（系统提示：上一次改写超出了需求——只改我指出的那一处，"
                  "其余文字逐字保留，包括原文开头的画风句与所有五官/服装细节，"
                  "不要润色、不要重排、不要删减）"),
        }, ensure_ascii=False)
    if not new_prompt:
        raise ProviderError("AI 未返回有效提示词，请重试或换个说法")

    warnings = _validate(current, new_prompt, names)
    if ratio < MIN_PRESERVE_RATIO:
        warnings.append(
            f"这次改动的幅度偏大（与原文相似度 {ratio:.0%}）——只想要微调的话，"
            "把修改需求写得更具体，或直接在结果框里改回你不想动的部分")
    # 逐段对账：被删掉的原文片段（≥6 字）逐条列出来——相似度红线抓不住的情况
    # （实测 0.774 却删了两处）靠这条让用户看见
    for _seg in _deleted_spans(current, new_prompt):
        if _seg and _seg not in (style_block or ""):
            warnings.append(f"删掉了原文里的「{_seg[:24]}」——若这不是你要求的，"
                            "请点重试或直接在结果框里改回")
    if style_block and style_block not in new_prompt:
        # 确定性补回（规则 4：原文的画风句原样保留）
        new_prompt = style_block + new_prompt
        warnings.append("原文开头的画风句被删掉了，已按原文补回（画风句不属于本次修改范围）")
    metrics = prompt_quality_metrics(new_prompt)
    metrics["limit"] = OPTIMIZE_MAX_CHARS
    metrics["over"] = max(0, metrics["length"] - OPTIMIZE_MAX_CHARS)
    logger.info("AI 优化提示词 target=%s obj=%s 原 %d 字 → 新 %d 字，告警 %d 条",
                target, obj_id, len(current), len(new_prompt), len(warnings))
    return {
        "target": target,
        "prompt": new_prompt,
        "notes": str(parsed.get("notes") or "").strip(),
        "metrics": metrics,
        "context_digest": digest,
        "warnings": warnings,
    }


def _style_keywords_of(style_ctx: dict) -> str:
    """从 style_hint 里取画风关键词段（供模型理解项目画风，但它不得写进正文）。"""
    hint = style_ctx.get("style_hint") or ""
    for line in hint.splitlines():
        if "（关键词：" in line:
            return line.split("（关键词：", 1)[1].rstrip("）")
    return ""


def _names_for(target: str, kind: str, names: dict[str, set[str]],
               ctx: dict, current: str) -> set[str]:
    """按目标类型给出相关名单：引用到的 + 上下文里出现的。

    分镜只给**本镜出场资产**（全量名单会诱导模型把无关角色写进画面）；
    其余类型（角色/场景/道具图）给全量名单（它们本就要与同剧资产拉开差异）。
    """
    referenced = {name for k, name in _refs_of(current) if k == kind}
    if target != TARGET_SHOT:
        return set(names.get(kind) or ()) | referenced
    allowed: set[str] = set(referenced)
    if kind == "场景":
        # 分镜上下文里场景是**单个 dict**（不是列表），单独取
        scene = ctx.get("出场场景") or {}
        if isinstance(scene, dict) and scene.get("name"):
            allowed.add(str(scene["name"]))
        return allowed
    for item in ctx.get("出场" + kind) or []:
        if isinstance(item, dict) and item.get("name"):
            allowed.add(str(item["name"]))
    return allowed


def _parse_reply(raw: str) -> dict:
    """解析 LLM 回复（容错：剥 Markdown 围栏；解析失败返回空 dict）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("AI 优化回复非 JSON：%s", text[:200])
        return {}
    return data if isinstance(data, dict) else {}


def _validate(current: str, new_prompt: str, names: dict[str, set[str]]) -> list[str]:
    """确定性校验（§5.4 第 4/5 条 + 约束 3）：返回告警列表（空 = 全部通过）。

    这些校验**不拦截**（结果仍返回给用户看），但必须在弹窗里如实展示——
    「AI 改坏了什么」要让用户看得见，而不是悄悄应用。
    """
    warnings: list[str] = []
    old_refs = _refs_of(current)
    new_refs = _refs_of(new_prompt)
    if old_refs != new_refs:
        missing = old_refs - new_refs
        added = new_refs - old_refs
        parts = []
        if missing:
            parts.append("丢失引用：" + "、".join(f"{k}@{n}" for k, n in sorted(missing)))
        if added:
            parts.append("新增引用：" + "、".join(f"{k}@{n}" for k, n in sorted(added)))
        warnings.append("引用集合与原文不一致（系统靠引用绑定参考图）——" + "；".join(parts))
    for kind, name in sorted(new_refs):
        if name not in (names.get(kind) or set()):
            warnings.append(f"引用了名单外的{kind}「{name}」——可能绑定不到参考图")
    # 画风词：**只对「新增」告警**（2026-09-30 修）。此前只要结果里出现画风词就告警，
    # 而系统提示词同时又要求「正文不写画风词」——两条自相矛盾，模型于是把用户原文里
    # 已有的画风句一并删掉（实测报障「我只是微调，他全部重写」）。现在：原文里本来
    # 就有的画风句属于「原样保留」范围，只有新加进来的才算违规。
    hit = next((w for w in STYLE_WORD_MARKERS
                if w in new_prompt and w not in current), "")
    if hit:
        warnings.append(f"新出现了原文没有的画风/质量词「{hit}」——画风由系统统一注入，"
                        "正文不该新增这类词（原文已有的画风句应原样保留）")
    if len(new_prompt) > OPTIMIZE_MAX_CHARS:
        warnings.append(f"提示词 {len(new_prompt)} 字，超过建议上限 "
                        f"{OPTIMIZE_MAX_CHARS} 字（过长会稀释模型注意力）")
    return warnings
