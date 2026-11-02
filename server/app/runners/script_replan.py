"""重新分集长任务 runner：LLM 按剧情/爆点/钩子重切分集边界（18 号 §51.5）。

为什么是任务而不是同步 HTTP：分集规划是分钟级调用（分块提节拍 + 汇总切集，
长剧本 3-8 次 LLM 调用），挂请求上则进度不可见、关页面即丢、无法取消。做成
任务后走统一的 trace/stream/progress 通道，与解析共用同一个观测窗。

**破坏性语义（必须显式）**：重切边界后旧分镜不再与集对应，本 runner 会删除
该项目全部 Episode 与 Shot 再按新分集重建。API 侧已做两段式预检
（confirm=false 先返回将丢弃的分镜数），runner 内不再二次确认。
资产（角色/场景/道具）不删——它们按剧本全量提取，与分集边界无关。
"""

import logging

from app.db.models import (
    PLAN_SOURCE_LLM, PRODUCE_PLANNED, Episode, Setting, Shot,
)
from app.db.session import SessionLocal
from app.runners.base import BaseRunner, RunnerContext, RunnerResult
from app.script.llm_parser import ScriptParseError, plan_episodes
from app.script.persist import script_text_key

logger = logging.getLogger(__name__)

# runner 名称（drama.py 提交与前端 node_id 分支共用）
RUNNER_NAME = "replan_episodes"

# 进度区间：LLM 调用阶段（留头尾给任务引擎的状态切换）
_FRAC_LO, _FRAC_HI = 0.05, 0.95


class ReplanEpisodesRunner(BaseRunner):
    """重新分集任务：重切分集边界并重建 Episode（分镜随之清空重建）。"""

    name = RUNNER_NAME
    default_model = ""
    allowed_models = [""]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        project_id = str(params.get("project_id") or "")
        if not project_id:
            return RunnerResult(ok=False, error="重新分集参数缺失（project_id）")

        with SessionLocal() as db:
            row = db.get(Setting, script_text_key(project_id))
            text = (row.value or "").strip() if row and row.value else ""
        if not text:
            return RunnerResult(ok=False, error="剧本原文为空，无法重新分集")

        def on_progress(frac: float, label: str) -> None:
            ctx.publish_progress(_FRAC_LO + (_FRAC_HI - _FRAC_LO) * frac, label)

        if ctx.publish_trace is not None:
            ctx.publish_trace("stage", "重新分集中")
            ctx.publish_trace(
                "thought", "重新分集开始",
                f"按剧情走向/爆点/钩子重切 {len(text)} 字剧本的分集边界"
                "（分块提节拍 → 汇总切集）；重切后原有分镜将清空重建")

        try:
            sections = plan_episodes(
                text,
                cancel_check=lambda: ctx.cancel_event.is_set(),
                stream_cb=ctx.publish_stream,
                trace_cb=ctx.publish_trace,
                progress_cb=on_progress)
        except ScriptParseError as e:
            if ctx.cancel_event.is_set():
                return RunnerResult(ok=False, error="任务已取消",
                                    fail_class=getattr(e, 'fail_class', ''))
            logger.warning("重新分集失败 task=%s：%s", ctx.task_id, e)
            return RunnerResult(ok=False, error=f"重新分集失败：{e}",
                                fail_class=getattr(e, 'fail_class', ''))

        # 重建分集：旧 Episode 与 Shot 一并删除（边界已变，旧分镜不再对应）。
        # 资产不动——角色/场景/道具按整部剧本提取，与分集边界无关。
        try:
            with SessionLocal() as db:
                old_ids = db.query(Episode.id).filter(
                    Episode.project_id == project_id).all()
                old_shot_n = 0
                if old_ids:
                    ids = [r[0] for r in old_ids]
                    old_shot_n = db.query(Shot).filter(
                        Shot.episode_id.in_(ids)).delete(
                            synchronize_session=False)
                    db.query(Episode).filter(
                        Episode.project_id == project_id).delete(
                            synchronize_session=False)
                for sec in sections:
                    db.add(Episode(
                        project_id=project_id, seq=sec["num"],
                        title=sec.get("title", "") or "",
                        body=sec.get("body", "") or "",
                        char_start=int(sec.get("char_start") or 0),
                        char_end=int(sec.get("char_end") or 0),
                        synopsis=sec.get("synopsis", "") or "",
                        beats_json=[b for b in (sec.get("beats") or []) if b],
                        hooks_json=[h for h in (sec.get("hooks") or [])
                                    if isinstance(h, dict)],
                        plan_source=PLAN_SOURCE_LLM,
                        produce_status=PRODUCE_PLANNED))
                db.commit()
        except Exception as e:  # noqa: BLE001 落库异常原样透出（事务已回滚）
            logger.exception("重新分集落库失败 task=%s", ctx.task_id)
            return RunnerResult(ok=False, error=f"重新分集落库失败：{e}")

        preview = "；".join(
            f"第{s['num']}集《{s.get('title') or '未命名'}》{len(s['body'])}字"
            for s in sections[:8])
        if ctx.publish_trace is not None:
            ctx.publish_trace(
                "thought", "重新分集完成",
                f"共切出 {len(sections)} 集：{preview}"
                + ("……" if len(sections) > 8 else "")
                + f"（原 {len(old_ids)} 集 / {old_shot_n} 个分镜已清空重建，"
                "资产保留）")
            ctx.publish_trace("stage", "重新分集完成", "")
        ctx.publish_progress(_FRAC_HI, f"重新分集完成：{len(sections)} 集")
        return RunnerResult(ok=True, kind="json",
                            meta={"project_id": project_id,
                                  "episodes": len(sections),
                                  "shots_dropped": old_shot_n})
