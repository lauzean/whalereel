"""Agent 编排器（对齐设计文档 5.2/5.4/5.5/8.4）。

mock 编排：llama.cpp server 不可用时按关键词规则规划（_rule_plan）；
LLM 接入点预留（_llm_plan，参考 runners/generate.py 的 ScriptRunner._llm 模式，
端点未配置或不可达时自动回退规则规划）。

编排规则（M2 规则版）：
- 消息含「图片/画/张图」→ 建图片节点 + 提交 t2i 任务
- 消息含「视频/片段」→ 建视频节点 + 提交 t2v 任务
- 含数量词「N 张/N 个」且 N>1 → 批量，先出计划确认卡（5.5）
- 其余 → 文本建议回复

工具集（对齐 5.4）：create_node（写画布 graph_json，位置在现有节点右侧偏移）、
run_node（engine.submit）、get_canvas_state（读画布）。
"""

import json
import re
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.agent import prompts as _prompts
from app.core.model_manager import KIND_T2I, KIND_T2V, project_preset
from app.core.providers import llm_available, llm_chat
from app.core.task_engine import engine
from app.db.models import Canvas, Message, Project
from app.db.models import Session as SessionModel

# 节点类型 → 生成 runner 与默认模型（对齐前端 specs.ts；runner 取值即预设类别）
NODE_RUNNERS = {"image": KIND_T2I, "video": KIND_T2V}
DEFAULT_MODELS = {"image": "hidream-o1/image", "video": "minimax-h3/fl2va-int8"}
NODE_LABELS = {"image": "图片", "video": "视频"}

# 单个产物的预估耗时（秒），用于计划确认卡的时间预估（设计文档 5.5）；
# 图片按 HiDream-O1 full 档官方分辨率（2048²/50 步）保守估算
ESTIMATE_PER_UNIT = {"image": 180, "video": 120}
# 预计总耗时超过该值需要确认（设计文档 5.5：约 5 分钟）
CONFIRM_ESTIMATE_SEC = 300

# 新节点放在现有节点右侧的偏移量，批量节点按纵向间距排开
NODE_OFFSET_X = 360
NODE_GAP_Y = 300

# 数量词识别「N 张/N 个/N 幅」（排除「第 N 个」序数用法），额外支持一/两
_COUNT_RE = re.compile(r"(?<!第)(\d+|[一两])\s*(?:张|个|幅)")

_IMAGE_HINTS = ("图", "画")
_VIDEO_HINTS = ("视频", "片段")


def _now() -> int:
    return int(time.time())


@dataclass
class IntentPlan:
    """规划结果：kind 为 image/video 时执行生成动作，reply 时仅文本回复。"""

    kind: str  # image|video|reply
    count: int = 1
    prompt: str = ""
    reply: str = ""
    model: str = ""
    estimated: int = 0

    @property
    def needs_confirm(self) -> bool:
        """批量（N>1）或预计总耗时超阈值时需要计划确认（设计文档 5.5）。"""
        return self.kind in NODE_RUNNERS and (
            self.count > 1 or self.estimated > CONFIRM_ESTIMATE_SEC
        )


# ---------- 规划 ----------

def _detect_count(text: str) -> int:
    m = _COUNT_RE.search(text)
    if not m:
        return 1
    raw = m.group(1)
    if raw == "一":
        return 1
    if raw == "两":
        return 2
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _suggest_reply(text: str) -> str:
    return (
        "收到。当前为规则编排模式，可以这样对我说：\n"
        "- 画一张……（生成图片）\n"
        "- 生成一段……的视频（生成视频）\n"
        "- 生成 3 张……（批量生成，我会先给出创作计划供确认）"
    )


def _rule_plan(text: str) -> IntentPlan:
    """规则规划：关键词判生成意图，数量词判批量。"""
    if any(k in text for k in _VIDEO_HINTS):
        kind = "video"
    elif any(k in text for k in _IMAGE_HINTS):
        kind = "image"
    else:
        return IntentPlan(kind="reply", reply=_suggest_reply(text))
    count = _detect_count(text)
    return IntentPlan(
        kind=kind, count=count, prompt=text,
        model=DEFAULT_MODELS[kind],
        estimated=count * ESTIMATE_PER_UNIT[kind],
    )


def _llm_plan(text: str, canvas_state: dict) -> IntentPlan | None:
    """LLM 规划接入点：第三方 LLM（OpenAI 兼容，providers 层）可用时优先走 LLM。

    未配置或不可达时返回 None，回退规则规划（mock 编排）。
    """
    if not llm_available():
        return None
    try:
        content = llm_chat(
            [
                {"role": "system", "content": _prompts.GENERAL_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    "画布现状：" + json.dumps(canvas_state, ensure_ascii=False)
                    + "\n用户消息：" + text
                )},
            ],
            temperature=0.3,
            max_tokens=1024,
        )
        data = json.loads(content)
        kind = data.get("kind", "reply")
        if kind not in NODE_RUNNERS:
            return IntentPlan(kind="reply",
                              reply=str(data.get("reply", "")) or _suggest_reply(text))
        count = max(1, int(data.get("count", 1)))
        return IntentPlan(
            kind=kind, count=count,
            prompt=str(data.get("prompt", "")).strip() or text,
            model=DEFAULT_MODELS[kind],
            estimated=count * ESTIMATE_PER_UNIT[kind],
        )
    except Exception:  # noqa: BLE001 LLM 不可用即回退规则规划
        return None


# ---------- 工具（对齐设计文档 5.4）----------

def get_canvas_state(db: OrmSession, project_id: str) -> dict:
    """get_canvas_state：画布快照（节点/连线）。"""
    canvas = db.get(Canvas, project_id)
    if not canvas:
        return {"nodes": [], "edges": []}
    graph = json.loads(canvas.graph_json or "{}")
    return {"nodes": graph.get("nodes", []), "edges": graph.get("edges", [])}


def _base_position(db: OrmSession, project_id: str) -> dict:
    """新节点锚点位置：现有节点右侧偏移（画布为空时从原点开始）。"""
    canvas = db.get(Canvas, project_id)
    graph = json.loads(canvas.graph_json or "{}") if canvas else {}
    nodes = graph.get("nodes", [])
    max_x = max((n.get("position", {}).get("x", 0) for n in nodes),
                default=-NODE_OFFSET_X)
    return {"x": max_x + NODE_OFFSET_X, "y": 0}


def create_node(db: OrmSession, project_id: str, node_type: str,
                prompt: str, position: dict) -> dict:
    """create_node：写画布 graph_json（位置由编排器计算后传入，
    对齐 5.4 工具签名 create_node(type, position, params)）。

    节点结构对齐前端 React Flow 约定（type=studio + data.specType）。
    节点模型取项目预设（未预设回退系统默认，与计划确认卡展示一致）。
    """
    canvas = db.get(Canvas, project_id)
    graph = json.loads(canvas.graph_json or "{}") if canvas else {}
    nodes: list = graph.setdefault("nodes", [])
    graph.setdefault("edges", [])

    project = db.get(Project, project_id)
    params: dict = {"prompt": prompt,
                    "model": project_preset(project, NODE_RUNNERS[node_type])
                    or DEFAULT_MODELS[node_type],
                    "aspect": "16:9"}
    if node_type == "video":
        params["duration"] = 5
        params["camera_move"] = ""
    node = {
        "id": f"ag_{uuid.uuid4().hex[:10]}",
        "type": "studio",
        "position": {"x": int(position.get("x", 0)), "y": int(position.get("y", 0))},
        "data": {"specType": node_type, "label": node_type,
                 "params": params, "refs": []},
    }
    nodes.append(node)

    graph_json = json.dumps(graph, ensure_ascii=False)
    now = _now()
    if canvas:
        canvas.graph_json = graph_json
        canvas.updated_at = now
    else:
        db.add(Canvas(project_id=project_id, graph_json=graph_json))
    if project:
        project.updated_at = now
    return node


def run_node(project_id: str, node: dict, batch_id: str | None = None) -> dict:
    """run_node：提交节点生成任务（engine.submit，完成后自动追加抽卡版本）。

    与前端 run.ts 一致：model 作为 model_id 单独传，不进任务 params。
    """
    spec_type = node["data"]["specType"]
    params = {k: v for k, v in node["data"]["params"].items() if k != "model"}
    return engine.submit(
        project_id=project_id, node_id=node["id"],
        runner_name=NODE_RUNNERS[spec_type],
        model_id=node["data"]["params"].get("model", ""),
        params=params, batch_id=batch_id,
    )


# ---------- 编排入口 ----------

def _save_assistant(db: OrmSession, session: SessionModel, content: str,
                    tool_calls: list) -> Message:
    """落 assistant 消息并提交事务（连同此前的 user 消息一起生效）。"""
    msg = Message(session_id=session.id, role="assistant", content=content,
                  refs_json=[], tool_calls_json=tool_calls)
    db.add(msg)
    db.flush()
    session.updated_at = _now()
    db.commit()
    return msg


def _last_pending_plan_text(db: OrmSession, session_id: str) -> str | None:
    """找回最近一条计划确认卡的触发原文（用于「确认执行」重放，5.5）。"""
    msgs = db.scalars(
        select(Message)
        .where(Message.session_id == session_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .limit(5)
    ).all()
    for m in msgs:
        for call in (m.tool_calls_json or []):
            if isinstance(call, dict) and call.get("tool") == "plan":
                pending = (call.get("args") or {}).get("text")
                if pending:
                    return str(pending)
    return None


def orchestrate_chat(db: OrmSession, session: SessionModel, text: str,
                     refs: list[dict], confirmed: bool) -> dict:
    """会话编排入口：规划 →（批量/长耗时先出计划确认卡）→ 工具执行 → 消息落库。

    返回 {message_id, reply, actions, plan}；plan 非 null 表示待确认（未执行）。
    """
    canvas_state = get_canvas_state(db, session.project_id)

    # 落用户消息（含 @refs）
    user_msg = Message(session_id=session.id, role="user", content=text,
                       refs_json=refs, tool_calls_json=[])
    db.add(user_msg)
    db.flush()

    # 规划：LLM 优先（接入点预留），不可用时规则规划
    plan = _llm_plan(text, canvas_state) or _rule_plan(text)

    # 「确认执行」本身无生成意图：confirmed 时找回待确认计划重放（5.5）
    if plan.kind == "reply" and confirmed:
        pending_text = _last_pending_plan_text(db, session.id)
        if pending_text:
            text = pending_text
            plan = _llm_plan(text, canvas_state) or _rule_plan(text)

    # 项目预设模型覆盖计划默认模型：保证确认卡展示与 create_node 实际写入一致
    if plan.kind in NODE_RUNNERS:
        plan.model = (project_preset(db.get(Project, session.project_id),
                                     NODE_RUNNERS[plan.kind])
                      or plan.model)

    # 纯文本建议回复（无生成意图）
    if plan.kind == "reply":
        reply = plan.reply or _suggest_reply(text)
        assistant = _save_assistant(db, session, reply, tool_calls=[])
        return {"message_id": assistant.id, "reply": reply,
                "actions": [], "plan": None}

    # 计划确认卡（5.5）：批量/长耗时且未确认 → 只返回计划不执行
    if plan.needs_confirm and not confirmed:
        label = NODE_LABELS[plan.kind]
        plan_card = {
            "node_count": plan.count,
            "model": plan.model,
            "estimated_seconds": plan.estimated,
            "summary": f"批量创建 {plan.count} 个{label}节点并提交生成任务：{plan.prompt}",
        }
        reply = (f"本次为批量生成：{plan.count} 个{label}（模型 {plan.model}），"
                 f"预计总耗时约 {plan.estimated} 秒。确认后我将开始执行。")
        assistant = _save_assistant(db, session, reply, tool_calls=[{
            "tool": "plan", "args": {"text": text}, "result": plan_card,
        }])
        return {"message_id": assistant.id, "reply": reply,
                "actions": [], "plan": plan_card}

    # 执行：create_node + run_node；批量任务共用 batch_id（同模型聚合排队）。
    # 先建节点并提交事务（与 drama API 同约定：避免本会话未提交写锁
    # 与 engine.submit 的独立会话冲突），再逐个提交任务。
    batch_id = uuid.uuid4().hex if plan.count > 1 else None
    actions: list[dict] = []
    tool_calls: list[dict] = []
    nodes: list[dict] = []
    base = _base_position(db, session.project_id)
    for i in range(plan.count):
        position = {"x": base["x"], "y": base["y"] + i * NODE_GAP_Y}
        node = create_node(db, session.project_id, plan.kind, plan.prompt,
                           position=position)
        nodes.append(node)
        tool_calls.append({"tool": "create_node",
                           "args": {"type": plan.kind, "prompt": plan.prompt,
                                    "position": position},
                           "result": {"node_id": node["id"]}})
        actions.append({"type": "create_node", "node_id": node["id"], "node": node})
    db.commit()

    for node in nodes:
        submitted = run_node(session.project_id, node, batch_id=batch_id)
        tool_calls.append({"tool": "run_node", "args": {"node_id": node["id"]},
                           "result": submitted})
        actions.append({"type": "run_node", "node_id": node["id"], "node": node,
                        "task_id": submitted["task_id"]})

    label = NODE_LABELS[plan.kind]
    noun = f"{plan.count} 个{label}" if plan.count > 1 else f"1 个{label}"
    reply = f"已在画布创建 {noun}节点并提交生成任务，完成后可在节点抽卡记录中查看结果。"
    assistant = _save_assistant(db, session, reply, tool_calls=tool_calls)
    return {"message_id": assistant.id, "reply": reply,
            "actions": actions, "plan": None}
