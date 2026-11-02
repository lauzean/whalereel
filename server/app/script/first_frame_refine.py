"""首帧提示词精炼（23 号批次 3）——在 runner 内调用，**不依赖 db**。

**为什么在 runner 内**：精炼是每镜一次 LLM 调用（秒级）。若放在同步 API 端点里，
「生成首帧」批量提交会阻塞 N×秒（30 镜可达十分钟，HTTP 请求超时）；runner 是
异步任务，天然承载，并复用任务的进度 / 取消 / 错误通道。

**为什么不能查库**：`RunnerContext` 的注释明确「runner 自己不该去查库」——精炼
所需的上下文由 API 侧 `build_refine_context` 打包进任务 params 随任务传。

**失败一律 fail-open**：异常 / 超时 / 护栏不过 → 原样返回初稿，绝不阻断出图。
精炼是增益，不是门禁。

调用链：
    组装初稿 → build_refine_context（API 侧，有 db）→ 任务 params
    → refine_first_frame（runner 侧，无 db）→ 成品文本 + edits → prompt_notes
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, NamedTuple

from app.agent import prompts as _prompts
from app.core.prompt_metrics import LEAK_SEGMENT_RE
from app.core.providers import llm_available, llm_chat
from app.script.llm_parser import _extract_json

logger = logging.getLogger(__name__)

# 任务 params 里的精炼上下文键。**下划线前缀**：generate 侧记录生成参数时会
# 跳过下划线键（见 runners/generate.py 的 publish_generation 注释），所以它
# 不会混进「最终发给模型的参数」记录。
REFINE_PARAM_KEY = "_first_frame_refine"

# 三段段名——与 System Prompt 的输出契约、组装侧的分段度量键**逐字一致**
# （runtime 会用它重算 `prompt_segments`，键不同会让分段度量静默归零）
REFINE_SECTIONS: tuple[str, ...] = ("画面内容", "素材与参考", "约束")

# 参考图标注（组装侧 `_resolve_video_prompt` 的产物）。
# **draft 就是最终 prompt**（含画风前缀 + 已解析的标注）——runner 拿到的本就是
# 它，所以护栏按**编号**比对，而不是按「角色@X」。踩过的坑：一度按「未解析的
# 角色@X」设计护栏，但 runner 里根本没有那个形态，于是「初稿零引用 / LLM 多出
# 一堆引用」，5 镜全部误判为不通过。
REF_LABEL_RE = re.compile(r"（参考图(\d+)）")

# LLM 参数：改写类任务给少量创造性（与 PROMPT_OPTIMIZE 同档）
REFINE_TEMPERATURE = 0.4
# 输出上限：**给足**。agnes-llm 是推理模型，思考与正文共用这个额度——实测
# 4000 时有 2/5 镜因「推理耗时超出输出上限」被截断（正文一个字没产出）。
# 改写类任务的输入输出都在千字量级，8000 留了充裕余量。
REFINE_MAX_TOKENS = 8000
# 单镜精炼的超时上限（秒）。精炼慢于出图本身就没有意义了。
REFINE_TIMEOUT = 120

# 上下文各字段的截断上限：30 镜 × 全长会把任务 payload 撑大（WS 性能红线）
CTX_VIDEO_PROMPT_MAX = 2000
CTX_FIELD_MAX = 300

# 初稿短于此值不精炼（没有可精炼的内容）
MIN_DRAFT_CHARS = 30

# 画面内容段的收缩下限（23 号 §4-D 反向断言的**运行时落地**）：本方案的全部
# 价值在「把画面写足」，若精炼反而把它砍小就白做了。
#
# **但阈值不能卡太死**：职责②「删噪音」本来就会缩短这一段——首帧初稿没有段
# 标记，时间标记 / 元说明 / 运镜词就落在画面内容段里，删掉它们是对的。实测
# 一镜 230 → 197（-14%），按 0.9 拦下等于整次精炼白做。0.8 的意思是
# 「**别成规模地砍**」，不是「一个字都不许少」；实际砍了多少由 trace 逐镜记录
# （见 `refine_first_frame` 的 trace 条目），攒够样本再校准这个值。
PICTURE_FLOOR_RATIO = 0.8

# prompt_notes 里最多列几条改动明细（超出的只报总数，避免刷屏）
EDIT_NOTE_MAX = 6


class RefineOutcome(NamedTuple):
    """精炼结果。`applied=False` 时 `text` 就是原初稿（fail-open 的统一形态）。"""

    text: str
    sections: dict[str, str]
    edits: list[dict]
    applied: bool


def _clip(text: str, limit: int) -> str:
    """超长截断（保留头部：镜头原意集中在开场）。"""
    body = (text or "").strip()
    return body if len(body) <= limit else body[:limit] + "…"


def build_refine_context(db, project_id: str, shot, draft: str,
                         picture_chars: int = 0) -> dict:
    """API 侧：打包精炼上下文。**返回空 dict 表示本镜不精炼**。

    `draft` 传的是**最终 prompt**（含画风前缀与已解析的参考图标注）——runner
    里拿到的就是它，两侧必须是同一形态（护栏按此形态比对）。

    四种「不精炼」的情形都在这一处判（runner 侧只管「有上下文就精炼」），
    免得判据散落到两条链路上：
    1. 项目关了这个开关（`Project.refine_first_frame`，默认开）；
    2. 本镜没有 `video_prompt`——精炼的输入是它，旧分镜没有可理解的源；
    3. 初稿过短；
    4. LLM 端点不可用（挂上去也只会失败，白占一次任务超时）。

    `picture_chars`：初稿「画面内容」段的字数，供 runner 侧护栏比对
    （见 `PICTURE_FLOOR_RATIO`）——成品文本反推不出分段，只能在这里记录。
    """
    text = (draft or "").strip()
    if len(text) < MIN_DRAFT_CHARS:
        return {}
    video_prompt = (getattr(shot, "video_prompt", "") or "").strip()
    if not video_prompt:
        return {}
    from app.db.models import Project  # 延迟导入：避免模块级循环依赖

    project = db.get(Project, project_id)
    if project is not None and not getattr(project, "refine_first_frame", True):
        return {}
    if not llm_available():
        return {}
    state = getattr(shot, "state_json", None) or {}
    return {
        "shot_id": getattr(shot, "id", ""),
        "seq": getattr(shot, "seq", 0),
        "video_prompt": _clip(video_prompt, CTX_VIDEO_PROMPT_MAX),
        "state": {
            "position": _clip(str(state.get("position") or ""), CTX_FIELD_MAX),
            "props": _clip(str(state.get("props") or ""), CTX_FIELD_MAX),
            "time": _clip(str(state.get("time") or ""), CTX_FIELD_MAX),
        },
        "camera": _clip(getattr(shot, "camera", "") or "", CTX_FIELD_MAX),
        "duration": getattr(shot, "duration", 0),
        "style_anchor": _clip(getattr(project, "style_anchor", "") or "", CTX_FIELD_MAX),
        # 初稿里已有的参考图编号：LLM 只能原样保留这些，不增不减
        "ref_labels": sorted(ref_labels_of(text), key=int),
        "draft_picture_chars": int(picture_chars or 0),
        # 本镜出场角色名单（**精炼提示词的两条自检依赖它**：「开头声明的人物 vs
        # 实际出现」与「每个出场人物都在名单内」）。此前从不送 → 两条自检全部落空
        # （契约对账，25 号 0.3）。
        "characters": _character_names(db, shot),
    }


def _character_names(db, shot) -> list[str]:
    """本镜出场角色名——取自 `shot.character_ref_ids_json` 绑定的角色。"""
    from app.db.models import Character  # 延迟导入：避免模块级循环依赖

    out: list[str] = []
    for cid in (getattr(shot, "character_ref_ids_json", None) or []):
        c = db.get(Character, cid)
        name = str(getattr(c, "name", "") or "").strip() if c is not None else ""
        if name and name not in out:
            out.append(name)
    return out


def ref_labels_of(text: str) -> set[str]:
    """抽取文本里的参考图编号集合（（参考图1）（参考图3）→ {"1","3"}）。"""
    return {m.group(1) for m in REF_LABEL_RE.finditer(text or "")}


def _payload(draft: str, ctx: dict) -> dict:
    """构造发给 LLM 的 user 消息体（System Prompt 的输入契约）。"""
    return {
        "draft": draft,
        "video_prompt": ctx.get("video_prompt", ""),
        "state": ctx.get("state") or {},
        "camera": ctx.get("camera", ""),
        "duration": ctx.get("duration", 0),
        "style_anchor": ctx.get("style_anchor", ""),
        "ref_labels": ctx.get("ref_labels") or [],
        "characters": ctx.get("characters") or [],
    }


def _join_sections(sections: dict) -> str:
    """按固定顺序拼接三段（段间空行分隔，与组装侧的四段结构同构）。"""
    parts = [str(sections.get(name) or "").strip() for name in REFINE_SECTIONS]
    return "\n\n".join(p for p in parts if p)


def _guard(draft: str, sections: dict, ctx: dict) -> str:
    """确定性护栏：通过返回空串，否则返回拒绝原因。

    只拦「精炼把东西弄坏了」的情形，**不判断写得够不够好**（那是 LLM 的活）。
    四条：画面内容不得被砍大截（`PICTURE_FLOOR_RATIO`）、参考图标注集合必须与
    初稿一致、不得把组装用的段名标记写回正文、画面内容段不得为空。
    """
    picture = str(sections.get("画面内容") or "").strip()
    if not picture:
        return "画面内容段为空"
    floor = int(ctx.get("draft_picture_chars") or 0)
    if floor and len(picture) < floor * PICTURE_FLOOR_RATIO:
        return (f"画面内容段被砍小：{len(picture)} 字 < 初稿 {floor} 字 × "
                f"{PICTURE_FLOOR_RATIO}")
    joined = _join_sections(sections)
    draft_refs, new_refs = ref_labels_of(draft), ref_labels_of(joined)
    if draft_refs != new_refs:
        lost = sorted(draft_refs - new_refs, key=int)
        added = sorted(new_refs - draft_refs, key=int)
        return (f"参考图标注与初稿不一致（丢失 {lost or '无'}，"
                f"多出 {added or '无'}）——标注是系统绑定参考图的依据，"
                "只能原样保留")
    if LEAK_SEGMENT_RE.search(joined):
        return "把组装用的段名标记写进了正文"
    return ""


def _edits_digest(edits: list[dict]) -> str:
    """改动清单压成一行（供 trace 与 prompt_notes 概览）。"""
    if not edits:
        return "无改动"
    by_kind: dict[str, int] = {}
    for e in edits:
        kind = str(e.get("kind") or "?")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return "、".join(f"{k} {v} 处" for k, v in sorted(by_kind.items()))


def edit_notes(outcome: RefineOutcome) -> list[str]:
    """把 edits 渲染为 prompt_notes 条目——**系统改过的地方必须外露**。"""
    if not outcome.applied:
        return []
    if not outcome.edits:
        return ["首帧已精炼（无逐条改动记录）"]
    notes = [f"首帧精炼：{_edits_digest(outcome.edits)}"]
    for e in outcome.edits[:EDIT_NOTE_MAX]:
        notes.append(f"· {e.get('kind', '')} {e.get('what', '')}"
                     f"（{e.get('why', '')}）")
    if len(outcome.edits) > EDIT_NOTE_MAX:
        notes.append(f"· …另有 {len(outcome.edits) - EDIT_NOTE_MAX} 处改动")
    return notes


def refine_first_frame(draft: str, ctx: dict,
                       trace: Callable[[str, str, str], None] | None = None
                       ) -> RefineOutcome:
    """runner 侧：调 LLM 精炼首帧提示词。**任何失败都返回初稿**。

    `trace`：`RunnerContext.publish_trace` 形态的回调（kind, title, content），
    用于把「精炼做了什么 / 为什么没用」写进任务轨迹——静默的失败不可接受。
    """
    text = (draft or "").strip()
    if not ctx or not text:
        return RefineOutcome(text=text, sections={}, edits=[], applied=False)
    try:
        raw = llm_chat(
            [{"role": "system", "content": _prompts.FIRST_FRAME_REFINE_SYSTEM_PROMPT},
             {"role": "user",
              "content": json.dumps(_payload(text, ctx), ensure_ascii=False)}],
            json_mode=True, temperature=REFINE_TEMPERATURE,
            max_tokens=REFINE_MAX_TOKENS, timeout=REFINE_TIMEOUT)
        parsed = _extract_json(raw)
        sections = {str(k): str(v).strip()
                    for k, v in (parsed.get("sections") or {}).items()}
        reason = _guard(text, sections, ctx)
        # 逐镜记录「画面内容段」的前后字数——护栏阈值（PICTURE_FLOOR_RATIO）与
        # 效果验收都靠它，只看「采用/未采用」等于没有数据可校准
        pic_before = int(ctx.get("draft_picture_chars") or 0)
        pic_after = len(str(sections.get("画面内容") or "").strip())
        if reason:
            logger.warning("首帧精炼未采用（%s）shot=%s", reason, ctx.get("shot_id"))
            if trace:
                trace("first_frame_refine", "首帧精炼未采用",
                      f"按初稿出图：{reason}（画面段 {pic_before} → {pic_after}）")
            return RefineOutcome(text=text, sections={}, edits=[], applied=False)
        refined = _join_sections(sections)
        edits = [e for e in (parsed.get("edits") or []) if isinstance(e, dict)]
        if trace:
            trace("first_frame_refine", "首帧精炼",
                  f"{len(text)} 字 → {len(refined)} 字；画面段 {pic_before} → "
                  f"{pic_after}；{_edits_digest(edits)}")
        return RefineOutcome(text=refined, sections=sections, edits=edits,
                             applied=True)
    except Exception as e:  # noqa: BLE001 fail-open：精炼是增益，不是门禁
        logger.warning("首帧精炼失败（%s）shot=%s，按初稿出图", e, ctx.get("shot_id"))
        if trace:
            trace("first_frame_refine", "首帧精炼失败", f"按初稿出图：{e}")
        return RefineOutcome(text=text, sections={}, edits=[], applied=False)
