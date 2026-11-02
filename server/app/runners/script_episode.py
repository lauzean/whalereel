"""逐集流水线 runner（18 号 §52/§53/§54）：审核 → 修复 → 制作，每集独立成任务。

三个 runner 都在本模块（同一集的三个阶段，共享上下文装配与落库逻辑）：

- `audit_episode`：带上一集/下一集上下文的单集剧情审核，结果写 Episode.audit_json；
- `optimize_episode`：按勾选的问题清单做补丁式定点修复，**不跑复审**，修复稿写回
  Episode.body 并同步回写剧本原文对应区间；
- `build_episode`：拆解 → **立即落库分镜** → 画面描述 → UPDATE 回填。

为什么每集独立成任务：旧链路把整部绑成一个任务（全有或全无）——10 集 593 镜
跑满 88 分钟，LLM 全部完成后在落库前被看门狗杀掉，593 镜全丢。逐集后失败只
损失一集（重跑一集 ≈ 2 次 LLM 调用），且每集进度/成本独立可见。
"""

import json
import logging
import time

from app.db.models import (
    PRODUCE_AUDITED, PRODUCE_BUILDING, PRODUCE_BUILT, PRODUCE_FAILED,
    PRODUCE_NEEDS_FIX, Character, Episode, Prop, Scene, Setting, Shot,
)
from app.db.session import SessionLocal
from app.runners.base import BaseRunner, RunnerContext, RunnerResult
from app.script.requirements import (
    constraint_line, load_requirements, normalize_requirements, tone_craft_hint,
)
from app.script.llm_parser import (
    QA_PLAIN, ScriptParseError,
    audit_episode, backfill_image_prompts, decompose_episode_full,
    enrich_episode, episode_audit_contexts, extract_costume_suggestions,
    extract_episode_assets,
    optimize_episode, prompt_layer_lessons, qa_repair_episode,
)
from app.script.persist import (
    build_global_lessons_text, build_lessons_text, ep_assets_key,
    load_global_lessons,
    load_lessons, merge_costume_suggestions, merge_lessons,
    persist_episode_shots, project_style_context,
    save_script_text, script_meta_key, script_text_key,
    shot_skill_key, skill_text_key, storyboard_qa_key,
    t2v_skill_key, update_shot_video_prompts, upsert_episode_assets,
)

logger = logging.getLogger(__name__)

RUNNER_AUDIT = "audit_episode"
RUNNER_OPTIMIZE = "optimize_episode"
RUNNER_ASSETS = "extract_episode_assets"
RUNNER_BUILD = "build_episode"


# ---------- 共享：分集上下文装配 ----------

def _load_episodes(db, project_id: str) -> list[dict]:
    """按集号读全部分集，装配成审核/制作所需的上下文字典列表。

    相邻集一律取 Episode.body（分集切分产物，始终存在），不依赖相邻集是否
    已制作——逐集推进时上一集可能还没拆解。
    """
    rows = db.query(Episode).filter(
        Episode.project_id == project_id).order_by(Episode.seq).all()
    return [{"id": e.id, "num": e.seq, "title": e.title or "",
             "body": e.body or "", "synopsis": e.synopsis or "",
             "beats": list(e.beats_json or []),
             "hooks": list(e.hooks_json or []),
             "exit_hint": "", "produce_status": e.produce_status or "",
             "audit": e.audit_json if isinstance(e.audit_json, dict) else {}}
            for e in rows]


def _exit_states(sections: list[dict]) -> dict:
    """已审核分集的出口状态快照（按集号索引）——比要点表的 exit_hint 更准。"""
    out: dict[int, str] = {}
    for s in sections:
        st = (s.get("audit") or {}).get("exit_state") or ""
        if st:
            out[s["num"]] = st
    return out


def _load_assets(db, project_id: str) -> dict:
    """项目资产名单（逐集拆解与画面描述的锚定依据）。

    场景/道具的 image_prompt 取**剥净系统块**的本体（2026-09-30）：生成阶段
    组装写回后，字段里含空镜约束/画风前缀/负向词——原样喂给拆解 LLM 会把
    「没有任何生物出现」这类空镜措辞带进有人物的镜头描述。
    """
    from app.script.prompt_blocks import (
        strip_character_system_blocks, strip_prop_system_blocks,
        strip_scene_system_blocks,
    )

    def _rows(model):
        return db.query(model).filter(model.project_id == project_id).all()
    return {
        "characters": [{"name": c.name, "persona": c.persona or "",
                        "appearance": c.appearance or "",
                        "image_prompt": strip_character_system_blocks(
                            c.image_prompt or "")}
                       for c in _rows(Character)],
        "scenes": [{"name": s.name, "description": s.description or "",
                    "layout": s.layout or "",
                    "image_prompt": strip_scene_system_blocks(
                        s.image_prompt or "", s.layout or "")}
                   for s in _rows(Scene)],
        "props": [{"name": p.name, "description": p.description or "",
                   "image_prompt": strip_prop_system_blocks(p.image_prompt or "")}
                  for p in _rows(Prop)],
    }


def _lessons(db, project_id: str) -> str:
    """自学习【历史教训】段落（读取失败返回空串，不阻断）。"""
    try:
        ledger = load_lessons(db, project_id)
        global_ledger = load_global_lessons(db)
        plain = {r: (v[0] if isinstance(v, tuple) and v else r)
                 for r, v in QA_PLAIN.items()}
        return (build_lessons_text(ledger, "storyboard", plain)
                + build_lessons_text(ledger, "script")
                + build_global_lessons_text(global_ledger, "storyboard", plain)
                + build_global_lessons_text(global_ledger, "script"))
    except Exception:  # noqa: BLE001
        logger.exception("教训账本读取失败")
        return ""


def _lessons_for_prompt(db, project_id: str) -> str:
    """**写提示词**那一步的【历史教训】（prompt 段；读取失败返回空串，不阻断）。

    与 `_lessons`（喂拆镜）分开取：教训要**注入到它该起作用的那一步**——
    拆镜的坑喂拆镜、提示词的坑喂写提示词；混在一起模型会收到无关约束。
    prompt 段由 `prompt_layer_lessons` 从体检结果归档（unbound_figure /
    crowd_word / overload 这类「提示词写坏了」的信号）。
    """
    try:
        ledger = load_lessons(db, project_id)
        global_ledger = load_global_lessons(db)
        plain = {r: (v[0] if isinstance(v, tuple) and v else r)
                 for r, v in QA_PLAIN.items()}
        return (build_lessons_text(ledger, "prompt", plain)
                + build_global_lessons_text(global_ledger, "prompt", plain))
    except Exception:  # noqa: BLE001
        logger.exception("提示词层教训读取失败")
        return ""


def _key_scene_note(db, project_id: str, seq: int) -> str:
    """本集的关键场次强化指令（稀疏导演决策层，建档时随 meta 持久化）。

    只有高潮/重大反转/打斗/多人大场面才配得上更强镜头设计，普通场次不列——
    宁缺毋滥。读取失败返回空串（退化为常规拆解，不阻断）。
    """
    try:
        row = db.get(Setting, script_meta_key(project_id))
        if not row or not row.value:
            return ""
        meta = json.loads(row.value) or {}
        return "\n".join(
            f"· {k.get('note')}" for k in (meta.get("key_scenes") or [])
            if isinstance(k, dict) and k.get("episode") == seq and k.get("note"))
    except (ValueError, TypeError):
        logger.warning("关键场次读取失败 project=%s", project_id)
        return ""


def _requirements_of(db, project_id: str) -> dict:
    """本项目的解析前置需求（21 号批次 5）：优先读建档 meta，缺失时读存档。

    meta 优先的理由：需求随**这一次解析**生效——重解析会用新需求覆盖存档，
    而旧解析产出的分镜应当继续按当时的基调/禁用内容制作（避免中途换档）。
    两处都读不到 → 默认值（旧项目行为不变）。
    """
    try:
        row = db.get(Setting, script_meta_key(project_id))
        if row and row.value:
            meta = json.loads(row.value) or {}
            req = meta.get("requirements")
            if isinstance(req, dict) and req:
                return normalize_requirements(req)
    except (ValueError, TypeError):
        logger.warning("解析需求读取失败 project=%s", project_id)
    return load_requirements(db, project_id)


def _load_skill_texts(db, project_id: str) -> dict:
    """建档期存档的技能包正文（按层取用，逐集制作复用同一上下文）。

    shot＝分镜层（拆解用）；model_adapt＝模型适配层（画面描述用）；
    t2v＝文生视频模型包。各阶段只注入自己需要的那一层。
    """
    def _get(key: str) -> str:
        row = db.get(Setting, key)
        return (row.value or "") if row else ""
    return {"shot": _get(shot_skill_key(project_id)),
            "model_adapt": _get(skill_text_key(project_id)),
            "t2v": _get(t2v_skill_key(project_id))}


# ---------- 逐集审核 ----------

class AuditEpisodeRunner(BaseRunner):
    """单集剧情审核：带上一集/下一集上下文，只拦 critical。"""

    name = RUNNER_AUDIT
    default_model = ""
    allowed_models = [""]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        episode_id = str(params.get("episode_id") or "")
        if not episode_id:
            return RunnerResult(ok=False, error="审核参数缺失（episode_id）")
        with SessionLocal() as db:
            episode = db.get(Episode, episode_id)
            if episode is None:
                return RunnerResult(ok=False, error="分集不存在")
            project_id = episode.project_id
            sections = _load_episodes(db, project_id)
        idx = next((i for i, s in enumerate(sections) if s["id"] == episode_id), -1)
        if idx < 0:
            return RunnerResult(ok=False, error="分集不在该项目中")
        ep = sections[idx]
        prev_ctx, next_ctx = episode_audit_contexts(
            sections, idx, exit_states=_exit_states(sections))

        if ctx.publish_trace is not None:
            ctx.publish_trace("stage", "剧情审核中")
            ctx.publish_trace(
                "thought", "逐集审核开始",
                f"第 {ep['num']} 集《{ep['title'] or '未命名'}》"
                f"（{len(ep['body'])} 字）：注入"
                + ("上一集要点+结尾状态+尾锚；" if prev_ctx else "无上一集；")
                + ("下一集要点+头锚" if next_ctx else "无下一集"))
        try:
            audit = audit_episode(
                ep, prev_ctx, next_ctx,
                cancel_check=lambda: ctx.cancel_event.is_set(),
                stream_cb=ctx.publish_stream,
                cache=_audit_cache_hooks())
        except ScriptParseError as e:
            if ctx.cancel_event.is_set():
                return RunnerResult(ok=False, error="任务已取消",
                                    fail_class=getattr(e, 'fail_class', ''))
            return RunnerResult(ok=False, error=f"剧情审核失败：{e}",
                                fail_class=getattr(e, 'fail_class', ''))

        crit = [i for i in audit["issues"] if i["severity"] == "critical"]
        status = PRODUCE_AUDITED if audit["passed"] else PRODUCE_NEEDS_FIX
        try:
            with SessionLocal() as db:
                row = db.get(Episode, episode_id)
                if row is not None:
                    row.audit_json = {**audit, "at": int(__import__("time").time())}
                    # 用户已放行（忽略问题）过的集：审核只更新清单，不回退状态
                    if row.produce_status not in (PRODUCE_BUILT, PRODUCE_BUILDING):
                        row.produce_status = status
                db.commit()
        except Exception as e:  # noqa: BLE001 落库失败即结果丢失，按失败处理
            logger.exception("审核结果落库失败 task=%s", ctx.task_id)
            return RunnerResult(ok=False, error=f"审核结果落库失败：{e}")

        if ctx.publish_trace is not None:
            ctx.publish_trace(
                "thought", "逐集审核完成",
                f"第 {ep['num']} 集：{'通过' if audit['passed'] else '未通过'}"
                f"（critical {len(crit)}，建议 {len(audit['issues']) - len(crit)}）"
                f"｜{audit['verdict'] or '无总评'}"
                + ("（缓存命中，零调用）" if audit.get("cached") else ""))
            if audit["exit_state"]:
                ctx.publish_trace("thought", "本集出口状态", audit["exit_state"])
            for it in audit["issues"][:10]:
                ctx.publish_trace(
                    "thought", f"[{it['severity']}] {it['location']}{it['type']}",
                    f"{it['description']}｜定位锚：{it['quote'][:40]}")
            ctx.publish_trace("stage", "剧情审核完成", "")
        return RunnerResult(ok=True, kind="json",
                            meta={"episode_id": episode_id, "seq": ep["num"],
                                  "passed": audit["passed"],
                                  "critical": len(crit),
                                  "issues": len(audit["issues"])})


def _audit_cache_hooks() -> tuple:
    """逐集审核缓存读写钩子（Setting 持久化，独立容错）。"""
    def load(key: str) -> str | None:
        try:
            with SessionLocal() as db:
                row = db.get(Setting, key)
                return row.value if row else None
        except Exception:  # noqa: BLE001
            logger.exception("审核缓存读取失败")
            return None

    def save(key: str, value: str) -> None:
        try:
            with SessionLocal() as db:
                db.merge(Setting(key=key, value=value))
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("审核缓存写入失败")

    return (load, save)


# ---------- 逐集修复 ----------

class OptimizeEpisodeRunner(BaseRunner):
    """单集一键修复：补丁协议定点修复，不跑复审，修复稿写回当集正文与原文。"""

    name = RUNNER_OPTIMIZE
    default_model = ""
    allowed_models = [""]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        episode_id = str(params.get("episode_id") or "")
        if not episode_id:
            return RunnerResult(ok=False, error="修复参数缺失（episode_id）")
        with SessionLocal() as db:
            episode = db.get(Episode, episode_id)
            if episode is None:
                return RunnerResult(ok=False, error="分集不存在")
            project_id = episode.project_id
            body = episode.body or ""
            archived = episode.audit_json if isinstance(episode.audit_json, dict) else {}
            lessons_text = _lessons(db, project_id)
        if not body.strip():
            return RunnerResult(ok=False, error="本集正文为空，无法修复")

        # 勾选的问题清单：前端只传勾选项；未传时取存档里的全部 critical
        picked = [it for it in (params.get("issues") or []) if isinstance(it, dict)]
        if not picked:
            picked = [it for it in (archived.get("issues") or [])
                      if isinstance(it, dict) and it.get("severity") == "critical"]
        if not picked:
            return RunnerResult(
                ok=False, error="没有可修复的问题清单：请先执行一次剧情审核")

        if ctx.publish_trace is not None:
            ctx.publish_trace("stage", "逐集修复中")
            ctx.publish_trace(
                "thought", "逐集定点修复开始",
                f"按 {len(picked)} 条问题定点修复（本集 {len(body)} 字）："
                "审核提供逐字定位锚（quote），模型产出编辑补丁，代码逐字应用，"
                "**修复后不复审**（用户口径），改由代码逐条对账命中情况")

        def on_progress(done: int, total: int) -> None:
            ctx.publish_progress(0.05 + 0.85 * (done / max(total, 1)),
                                 f"剧本修复 {done}/{total} 步")

        try:
            outcome = optimize_episode(
                body, picked,
                cancel_check=lambda: ctx.cancel_event.is_set(),
                stream_cb=ctx.publish_stream, lessons_text=lessons_text,
                progress_cb=on_progress)
        except ScriptParseError as e:
            if ctx.cancel_event.is_set():
                return RunnerResult(ok=False, error="任务已取消",
                                    fail_class=getattr(e, 'fail_class', ''))
            logger.warning("逐集修复失败 task=%s：%s", ctx.task_id, e)
            return RunnerResult(ok=False, error=f"剧本修复失败：{e}",
                                fail_class=getattr(e, 'fail_class', ''))

        fixed = outcome["text"]
        stats = outcome["stats"]
        patches = outcome["patches"]
        missed = [p for p in patches if not p.get("applied")]

        # 修复稿写回当集正文 + 剧本原文对应区间（确定性替换，从后往前规避偏移）
        try:
            with SessionLocal() as db:
                row = db.get(Episode, episode_id)
                if row is None:
                    return RunnerResult(ok=False, error="分集不存在（已被删除）")
                row.body = fixed
                # 命中/未命中逐条对账写回审核存档（面板按条标出 ✓/✗）
                doc = dict(row.audit_json or {})
                issues = list(doc.get("issues") or [])
                for p in patches:
                    i = p.get("index")
                    if isinstance(i, int) and 0 <= i < len(issues):
                        issues[i] = {**issues[i], "patch": {
                            "applied": bool(p.get("applied")),
                            "reason": p.get("reason", "")}}
                doc["issues"] = issues
                doc["patched"] = True
                row.audit_json = doc
                if row.produce_status in (PRODUCE_NEEDS_FIX, PRODUCE_FAILED):
                    row.produce_status = PRODUCE_AUDITED
                _sync_episode_back_to_script(db, project_id, row)
                db.commit()
        except Exception as e:  # noqa: BLE001 落库失败即用户拿不到成果，按失败处理
            logger.exception("逐集修复结果落库失败 task=%s", ctx.task_id)
            return RunnerResult(ok=False, error=f"修复结果落库失败：{e}")

        if ctx.publish_trace is not None:
            ctx.publish_trace(
                "thought", "逐集定点修复完成",
                f"补丁 {stats['patches']} 条：命中 {stats['applied']}，"
                f"未命中 {stats['missed']}（未命中会在问题面板标出，可再点一次"
                "或手动改）；修复稿已写回本集正文与剧本原文")
            for p in missed[:8]:
                ctx.publish_trace(
                    "thought", f"未命中补丁 #{p.get('index')}",
                    f"{p.get('reason', '')}｜原句：{str(p.get('quote') or '')[:40]}")
        ctx.publish_progress(0.95, f"定点修复完成（{len(fixed)} 字）")
        return RunnerResult(ok=True, kind="json",
                            meta={"episode_id": episode_id, **stats})


def _sync_episode_back_to_script(db, project_id: str, episode: Episode) -> None:
    """把修复后的当集正文回写剧本原文对应区间（确定性替换）。

    每集记录自己的 [char_start, char_end)——用它把该区间原文替换为新的
    Episode.body，并同步更新 char_end（长度可能变）。区间之外的原文一字不动，
    其他集的区间因此**不受影响**（每集各自持有绝对区间，替换当集不会让别集
    的偏移漂移）。定位不到区间（旧数据无 char_end）即跳过并记日志，
    **不做任何语义近似**（红线：代码只做确定性对账）。
    """
    row = db.get(Setting, script_text_key(project_id))
    text = (row.value or "") if row else ""
    if not text:
        return
    if episode.char_end <= episode.char_start:
        logger.info("分集无原文区间，跳过回写 episode=%s", episode.id)
        return
    new_text = (text[:episode.char_start] + episode.body
                + text[episode.char_end:])
    episode.char_end = episode.char_start + len(episode.body)
    save_script_text(db, project_id, new_text)


# ---------- 按集资产解析 ----------

class ExtractEpisodeAssetsRunner(BaseRunner):
    """按集资产解析（18 号 §61）：本集正文 + 已有名单 → **只补新增**。

    用户口径：「资产在每集中单独解析，但要把已分析的资产告诉 llm，不要重复解析」。
    已有名单随请求注入，返回后代码再按名字逐字过滤一遍（双保险）；同物异名由
    LLM 输出 aliases 映射、代码逐字应用（语义归模型、对账归代码）。
    """

    name = RUNNER_ASSETS
    default_model = ""
    allowed_models = [""]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        episode_id = str(params.get("episode_id") or "")
        if not episode_id:
            return RunnerResult(ok=False, error="资产解析参数缺失（episode_id）")
        with SessionLocal() as db:
            episode = db.get(Episode, episode_id)
            if episode is None:
                return RunnerResult(ok=False, error="分集不存在")
            project_id = episode.project_id
            seq = episode.seq
            body = episode.body or ""
            known = _load_assets(db, project_id)
            skills = _load_skill_texts(db, project_id)
            front_text = _front_text(db, project_id) if seq == 1 else ""
        if not body.strip():
            return RunnerResult(ok=False, error="本集正文为空，无法解析资产")

        if ctx.publish_trace is not None:
            ctx.publish_trace("stage", "本集资产解析中")
            ctx.publish_trace(
                "thought", "按集资产解析开始",
                f"第 {seq} 集（{len(body)} 字）：已把项目现有资产名单"
                f"（角色 {len(known['characters'])}、场景 {len(known['scenes'])}、"
                f"道具 {len(known['props'])}）交给 AI，只补本集新增，不重复解析"
                + ("；本集为第 1 集，附带前置章节（总纲/人物设定）" if front_text else ""))

        style = _style_hint(db, project_id)
        try:
            result = extract_episode_assets(
                body, seq=seq, front_text=front_text, known=known,
                style_hint=style,
                model_skill_text=skills.get("t2i", ""),
                cancel_check=lambda: ctx.cancel_event.is_set(),
                stream_cb=ctx.publish_stream, trace_cb=ctx.publish_trace)
        except ScriptParseError as e:
            if ctx.cancel_event.is_set():
                return RunnerResult(ok=False, error="任务已取消",
                                    fail_class=getattr(e, 'fail_class', ''))
            logger.warning("按集资产解析失败 task=%s：%s", ctx.task_id, e)
            return RunnerResult(ok=False, error=f"本集资产解析失败：{e}",
                                fail_class=getattr(e, 'fail_class', ''))

        # 生图提示词兜底（缺 image_prompt 的补写）——放在落库前，资产入库即带
        # 提示词，用户去资产页就能直接生成定妆图；失败不阻断（图可后补）
        try:
            backfill_image_prompts(result, style,
                                   lambda: ctx.cancel_event.is_set(),
                                   model_skill_text=skills.get("t2i", ""),
                                   stream_cb=ctx.publish_stream)
        except Exception:  # noqa: BLE001
            logger.exception("生图提示词兜底失败 task=%s", ctx.task_id)

        try:
            with SessionLocal() as db:
                added = upsert_episode_assets(db, project_id, seq, result)
                _save_episode_assets(db, episode_id, {
                    **added, "reused": result.get("reused") or {},
                    "aliases": result.get("aliases") or [],
                    # 补充视角建议（只提示、不生成）：随本集资产存档透出，
                    # 前端在资产页显示「建议生成反打/交互区视角」（用户决定是否生成）
                    "view_hints": result.get("view_hints") or [],
                    "at": int(time.time())})
        except Exception as e:  # noqa: BLE001 落库失败即结果丢失，按失败处理
            logger.exception("本集资产落库失败 task=%s", ctx.task_id)
            return RunnerResult(ok=False, error=f"本集资产落库失败：{e}")

        # 妆造建议（接线回归的修复）：资产已落库 ⇒ 角色名单是全的，此时提取
        # 「本集的换装/妆造事件」并并入项目清单。fail-open，失败不影响资产解析
        costume_added = _extract_costume_hints(ctx, project_id, seq, body, style)

        if ctx.publish_trace is not None:
            ctx.publish_trace(
                "thought", "本集资产解析完成",
                f"新增角色 {added['characters']}、场景 {added['scenes']}、"
                f"道具 {added['props']}；别名映射 {len(result.get('aliases') or [])} 条；"
                "缺定妆图的资产可在「资产」页生成")
            ctx.publish_trace("stage", "本集资产解析完成", "")
        ctx.publish_progress(0.97, f"第 {seq} 集资产解析完成")
        return RunnerResult(ok=True, kind="json",
                            meta={"episode_id": episode_id, "seq": seq,
                                  "costume_suggestions": costume_added, **added})


def _load_known_assets(db, project_id: str) -> dict:
    """已有资产名单（名字 + 一句话提示）——注入提示词要求 LLM 不重复提取。"""
    return {
        "characters": [{"name": c.name, "persona": c.persona or ""}
                       for c in db.query(Character).filter(
                           Character.project_id == project_id).all()],
        "scenes": [{"name": s.name, "description": s.description or ""}
                   for s in db.query(Scene).filter(
                       Scene.project_id == project_id).all()],
        "props": [{"name": p.name, "description": p.description or ""}
                  for p in db.query(Prop).filter(
                      Prop.project_id == project_id).all()],
    }


def _front_text(db, project_id: str) -> str:
    """建档时存档的前置章节正文（首集之前的文本，通常是总纲/人物设定）。"""
    try:
        row = db.get(Setting, script_meta_key(project_id))
        if not row or not row.value:
            return ""
        return str((json.loads(row.value) or {}).get("front_text") or "")
    except (ValueError, TypeError):
        logger.warning("前置章节读取失败 project=%s", project_id)
        return ""


def _episode_assets_doc(db, episode_id: str) -> dict:
    """本集资产解析存档（未解析返回空 dict——用于区分「没解析」与「解析了没新增」）。"""
    row = db.get(Setting, ep_assets_key(episode_id))
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) else {}
    except ValueError:
        logger.warning("本集资产存档损坏 episode=%s", episode_id)
        return {}


def _save_episode_assets(db, episode_id: str, doc: dict) -> None:
    """逐集资产解析结果存档（分集列表接口据此显示「已解析 · 新增 N 个」）。

    与审核清单同样的通道选择：结构化结果落存档 + 随分集列表透出，不走任务
    error 字符串（历史教训：清单塞 error 会退化成把原始 JSON 糊到用户脸上）。
    """
    db.merge(Setting(key=ep_assets_key(episode_id),
                     value=json.dumps(doc, ensure_ascii=False)))
    db.commit()   # 自带提交：调用方（runner）不再额外 commit，Session 关闭即回滚


def _style_hint(db, project_id: str) -> str:
    """项目画风上下文（含画风规范与地区规范），注入资产解析提示词。"""
    try:
        return project_style_context(db, project_id)["style_hint"]
    except Exception:  # noqa: BLE001 画风读不到不阻断（退化为基础提取）
        logger.exception("画风上下文读取失败 project=%s", project_id)
        return ""


def _extract_costume_hints(ctx, project_id: str, seq: int, body: str,
                           style: str) -> int:
    """按集提取妆造建议并**并入**项目清单（妆造链路接线：25 号 §8 待办）。

    这条链路的下游早就建好了（Setting 落库 → 角色卡的读取/接受/拒绝端点 →
    「接受一键生成」UI），唯独生产端 `extract_costume_suggestions` **零调用方**
    （历史日志里还留着它旧调用点的 import 行 ⇒ 是按集化改造时掉线的），
    结果是界面上永远空着一块「妆造建议」。

    放在**资产解析之后**：模型从项目角色名单里逐字选人（代码再把名单外的丢弃），
    此时本集新增角色已落库 ⇒ 名单是全的。一次 LLM 调用/集。
    **fail-open**：提取失败只记日志与轨迹，绝不阻断资产解析（资产是主产出）。
    返回本次新增条数（已有条目按「角色·妆造名」并集集数，见 persist 侧规则）。
    """
    try:
        with SessionLocal() as db:
            chars = [{"name": c.name, "persona": c.persona or "",
                      "appearance": c.appearance or ""}
                     for c in db.query(Character).filter(
                         Character.project_id == project_id).all()]
            total_eps = db.query(Episode).filter(
                Episode.project_id == project_id).count()
        if not chars or total_eps <= 0:
            return 0
        suggestions = extract_costume_suggestions(
            chars, [{"num": seq, "body": body}], style, total_eps,
            cancel_check=lambda: ctx.cancel_event.is_set(),
            trace_cb=ctx.publish_trace)
        with SessionLocal() as db:
            added = merge_costume_suggestions(db, project_id, suggestions)
        if added and ctx.publish_trace is not None:
            ctx.publish_trace(
                "thought", "妆造建议已入清单",
                f"第 {seq} 集新增 {added} 条换装/妆造事件（角色卡上可「接受一键"
                "生成」或「拒绝」；接受后该角色在对应集数用这套妆造造型）")
        return added
    except Exception:  # noqa: BLE001 建议是增值项，失败不影响资产解析
        logger.exception("妆造建议提取失败（跳过）task=%s", ctx.task_id)
        return 0


# ---------- 逐集制作 ----------

class BuildEpisodeRunner(BaseRunner):
    """单集制作：拆解 → **立即落库分镜** → 画面描述 → UPDATE 回填。"""

    name = RUNNER_BUILD
    default_model = ""
    allowed_models = [""]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        episode_id = str(params.get("episode_id") or "")
        if not episode_id:
            return RunnerResult(ok=False, error="制作参数缺失（episode_id）")
        with SessionLocal() as db:
            episode = db.get(Episode, episode_id)
            if episode is None:
                return RunnerResult(ok=False, error="分集不存在")
            project_id = episode.project_id
            sections = _load_episodes(db, project_id)
            assets = _load_assets(db, project_id)
            skills = _load_skill_texts(db, project_id)
            key_note = _key_scene_note(db, project_id, episode.seq)
            # 解析前置需求（21 号批次 5）：基调 → 拆解节奏档；禁用内容 → 画面描述
            _req = _requirements_of(db, project_id)
            tone_hint = tone_craft_hint(_req)
            banned_line = constraint_line(_req)
            # 本集资产解析产出的别名映射（本集叫法 → 已有名单名）：拆解后代码
            # 逐字应用到分镜引用，避免同物异名被随后的名单对账当「名单外」剔除
            ep_assets = _episode_assets_doc(db, episode_id)
            aliases = [a for a in (ep_assets.get("aliases") or [])
                       if isinstance(a, dict)]
            lessons_text = _lessons(db, project_id)
            # 提示词层的教训单独取（喂给**写提示词**那一步，见 _lessons_for_prompt）
            lessons_prompt = _lessons_for_prompt(db, project_id)
            ctx_style = project_style_context(db, project_id)
        idx = next((i for i, s in enumerate(sections) if s["id"] == episode_id), -1)
        if idx < 0:
            return RunnerResult(ok=False, error="分集不在该项目中")
        ep = sections[idx]
        if not (ep.get("body") or "").strip():
            return RunnerResult(ok=False, error="本集正文为空，无法制作")
        style = ctx_style["style_hint"]
        # 跨集衔接：上一集出口状态（已审核产出，更准）+ 正文尾部锚
        prev_ctx, _ = episode_audit_contexts(
            sections, idx, exit_states=_exit_states(sections))

        def on_progress(frac: float, label: str) -> None:
            ctx.publish_progress(0.05 + 0.9 * frac, label)

        if ctx.publish_trace is not None:
            ctx.publish_trace("stage", "分集制作中")
            ctx.publish_trace(
                "thought", "逐集制作开始",
                f"第 {ep['num']} 集《{ep['title'] or '未命名'}》"
                f"（{len(ep['body'])} 字）：拆解 → 立即落库 → 画面描述")
            if not ep_assets:
                ctx.publish_trace(
                    "thought", "本集资产尚未解析",
                    "建议先在上一步「本集资产」解析——未解析时拆解出的角色/场景/道具"
                    "可能没有对应的资产记录，生成时将缺参考图（人物与陈设容易漂移）")

        # 阶段 1：拆解 + 确定性整合（失败重试一次在函数内）
        try:
            on_progress(0.10, "分镜拆解中")
            shots = decompose_episode_full(
                ep, assets, style,
                prev_exit_state=prev_ctx.get("exit_state", ""),
                prev_tail=prev_ctx.get("tail", ""),
                key_scene_note=key_note, aliases=aliases,
                shot_skill=skills["shot"], lessons_text=lessons_text,
                tone_hint=tone_hint,
                cancel_check=lambda: ctx.cancel_event.is_set(),
                stream_cb=ctx.publish_stream, trace_cb=ctx.publish_trace)
        except ScriptParseError as e:
            if ctx.cancel_event.is_set():
                return RunnerResult(ok=False, error="任务已取消",
                                    fail_class=getattr(e, 'fail_class', ''))
            _mark_failed(episode_id)
            return RunnerResult(ok=False, error=f"分镜拆解失败：{e}",
                                fail_class=getattr(e, 'fail_class', ''))

        # 阶段 2：**立即落库**（保命：即使画面描述阶段失败/被杀，分镜已在库里）
        try:
            with SessionLocal() as db:
                counts = persist_episode_shots(db, project_id, episode_id, shots)
            if ctx.publish_trace is not None:
                ctx.publish_trace(
                    "thought", "分镜已落库",
                    f"{counts['shots']} 个分镜已写入数据库（不等画面描述——"
                    "避免整集成果在最后一步丢失）")
        except Exception as e:  # noqa: BLE001
            logger.exception("分镜落库失败 task=%s", ctx.task_id)
            _mark_failed(episode_id)
            return RunnerResult(ok=False, error=f"分镜落库失败：{e}")

        # 阶段 3：画面描述（就地写入 shots 的 video_prompt）
        try:
            on_progress(0.55, "画面描述生成中")
            enrich_episode(
                {**ep, "shots": shots}, assets, style,
                model_skill_text=skills["model_adapt"], t2v_skill=skills["t2v"],
                cancel_check=lambda: ctx.cancel_event.is_set(),
                stream_cb=ctx.publish_stream, trace_cb=ctx.publish_trace,
                progress_cb=lambda frac, label: on_progress(
                    0.55 + 0.35 * frac, label),
                banned_line=banned_line,
                lessons_text=lessons_prompt)
        except ScriptParseError as e:
            if ctx.cancel_event.is_set():
                return RunnerResult(ok=False, error="任务已取消",
                                    fail_class=getattr(e, 'fail_class', ''))
            # 画面描述失败不废掉已落库的分镜：如实告知可重跑补描述
            _mark_failed(episode_id)
            return RunnerResult(
                ok=False, error=f"画面描述失败（分镜已落库，可重跑本集补齐）：{e}")

        # 阶段 4：画面描述回填（UPDATE 已落库分镜）
        try:
            with SessionLocal() as db:
                n = update_shot_video_prompts(db, episode_id, shots)
        except Exception as e:  # noqa: BLE001
            logger.exception("画面描述回填失败 task=%s", ctx.task_id)
            _mark_failed(episode_id)
            return RunnerResult(ok=False, error=f"画面描述回填失败：{e}")

        # 画面描述是"单镜失败留空"的容错设计（不因个别镜失败废掉整集），所以
        # 这里要主动对账：一镜都没产出说明该阶段整体失败，必须如实报错而不是
        # 报成功——否则用户看到"制作完成"却拿到一堆空提示词
        missing_vp = [s for s in shots if not (s.get("video_prompt") or "").strip()]
        if shots and len(missing_vp) == len(shots):
            _mark_failed(episode_id)
            return RunnerResult(
                ok=False,
                error=f"画面描述全部失败（{len(shots)} 镜均无提示词）——"
                      "分镜已落库，可重跑本集补齐")
        if missing_vp and ctx.publish_trace is not None:
            ctx.publish_trace(
                "thought", "画面描述部分缺失",
                f"{len(missing_vp)}/{len(shots)} 镜未产出提示词（单镜失败留空），"
                "可重跑本集补齐；已产出的提示词不受影响")

        # 阶段 5：分镜体检 + 修复循环（确定性 + LLM 定向修复）
        try:
            issues, repair = qa_repair_episode(
                {**ep, "shots": shots}, assets, style,
                skill_text=skills["shot"], t2v_skill=skills["t2v"],
                cancel_check=lambda: ctx.cancel_event.is_set(),
                stream_cb=ctx.publish_stream, trace_cb=ctx.publish_trace)
        except Exception:  # noqa: BLE001 体检失败不废掉已落库分镜
            logger.exception("分镜体检失败 task=%s", ctx.task_id)
            issues, repair = [], {}
        # 阶段 5.5：体检修复**回写**——先落库设计下，阶段 4 之后的每一次内存
        # 改动都不会自动进库。体检的「悬空修复 N 镜 / 补镜 N 个」正是在阶段 4
        # 之后发生的：旧实现算完只写日志，库里描述依旧为空（实测霜刃辞第1集
        # 日志报「悬空修复 7 镜」而 7 镜 description 全空，成片因此出现两段
        # 完全相同的视频——画面描述塌缩的直接上游）。
        # 覆盖语义与阶段 2 同源（清空该集重建），故直接复用 persist_episode_shots。
        if (repair or {}).get("changed"):
            try:
                with SessionLocal() as db:
                    counts = persist_episode_shots(db, project_id, episode_id, shots)
                if ctx.publish_trace is not None:
                    ctx.publish_trace(
                        "thought", "体检修复已回写",
                        f"{int((repair or {}).get('changed_count') or 0)} 处修复"
                        f"写入数据库（{counts['shots']} 个分镜）——"
                        "先落库之后的内存改动必须显式回写，否则只留在日志里")
            except Exception as e:  # noqa: BLE001 回写失败不废掉已落库分镜
                logger.exception("体检修复回写失败 task=%s", ctx.task_id)
                if ctx.publish_trace is not None:
                    ctx.publish_trace(
                        "thought", "体检修复回写失败",
                        f"{e}（分镜与画面描述已在库，体检修复结果未生效）")
        # 体检报告存档（项目级 Setting，按集覆盖合并）
        try:
            with SessionLocal() as db:
                _merge_storyboard_qa(db, project_id, ep["num"], issues)
                detail = (repair or {}).get("detail") or {}
                if detail:
                    merge_lessons(db, project_id, "storyboard", detail)
                # 提示词层教训（独立一段）：`issues` 此时已含 unbound_figure /
                # crowd_word / overload 这类**写提示词写坏了**的信号，而它们
                # 此前一条都进不了账本——模型每次都从零开始犯同样的错。
                # 归档后经 enrich_video_prompts 的 lessons_text 注入到**写提示词**
                # 那一步（与 storyboard 段注入拆镜、script 段注入审核同构）。
                pdetail = prompt_layer_lessons(issues)
                if pdetail:
                    merge_lessons(db, project_id, "prompt", pdetail)
                row = db.get(Episode, episode_id)
                if row is not None:
                    row.produce_status = PRODUCE_BUILT
                db.commit()
        except Exception:  # noqa: BLE001 存档失败不影响已落库分镜
            logger.exception("体检报告存档失败 task=%s", ctx.task_id)

        if ctx.publish_trace is not None:
            ctx.publish_trace(
                "thought", "逐集制作完成",
                f"第 {ep['num']} 集《{ep['title'] or '未命名'}》："
                f"{counts['shots']} 个分镜，{n} 镜画面描述已回填；"
                f"体检残留 {len(issues)} 项")
            ctx.publish_trace("stage", "分集制作完成", "")
        ctx.publish_progress(0.98, f"第 {ep['num']} 集制作完成")
        return RunnerResult(ok=True, kind="json",
                            meta={"episode_id": episode_id, "seq": ep["num"],
                                  "shots": counts["shots"],
                                  "video_prompts": n,
                                  "qa_issues": len(issues)})


def _mark_failed(episode_id: str) -> None:
    """标记该集制作失败（仅影响本集，其余集数据完好）。"""
    try:
        with SessionLocal() as db:
            row = db.get(Episode, episode_id)
            if row is not None:
                row.produce_status = PRODUCE_FAILED
                db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("标记制作失败状态异常 episode=%s", episode_id)


def _merge_storyboard_qa(db, project_id: str, seq: int,
                         issues: list[dict]) -> None:
    """把本集体检项并入项目级体检报告（按集号替换，不丢其他集的项）。

    存档结构保持**扁平 Issue 列表**（每条自带 episode 字段）——与旧整部解析
    时代一致，提交前硬门 / 体检报告端点 / 前端报告面板的消费方式都不用改；
    逐集制作只是按集替换自己那部分。
    """
    key = storyboard_qa_key(project_id)
    row = db.get(Setting, key)
    kept: list[dict] = []
    if row and row.value:
        try:
            loaded = json.loads(row.value)
            if isinstance(loaded, list):
                kept = [i for i in loaded
                        if isinstance(i, dict) and i.get("episode") != seq]
        except ValueError:
            logger.warning("体检报告存档损坏，按空重建 project=%s", project_id)
    db.merge(Setting(key=key, value=json.dumps(kept + list(issues),
                                               ensure_ascii=False)))
