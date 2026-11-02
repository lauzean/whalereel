"""剧本建档长任务 runner：审读 → 分集规划 → 资产分析 → 落库（18 号 §50 阶段 A）。

建档只产出「分集计划 + 资产名单 + 分集要点表」，**不含终审与拆解/画面描述**：
- 整部终审已由逐集审核替换（`audit_episode` runner）；
- 拆解与画面描述迁到逐集制作（`build_episode` runner）。

为什么拆：旧链路把 10 集 593 镜绑成一个任务、全有或全无——实测跑满 88 分钟、
LLM 全部完成后在落库前被心跳看门狗杀掉，593 镜全部丢失。建档拆出后单次耗时
降到分钟级，失败代价极小；每集独立制作，失败只重跑那一集。

失败语义：LLM 阶段失败 → 任务 failed（原因直达前端），旧建档数据保留；
取消 → canceled；落库异常 → failed（回滚）。
"""

import json
import logging

from app.db.session import SessionLocal
from app.db.models import Project, Setting, Task
from app.runners.base import BaseRunner, RunnerContext, RunnerResult
from app.script.llm_parser import QA_PLAIN, ScriptParseError, llm_parse
from app.script.requirements import load_requirements, review_hint
from app.script.persist import (
    build_global_lessons_text, build_lessons_text,
    bump_script_version,
    load_global_lessons,
    load_lessons, merge_lessons,
    persist_parse_result, project_style_context, reattach_character_assets,
    reset_drama, save_parse_artifacts, save_script_trace,
)

logger = logging.getLogger(__name__)

# runner 名称（drama.py 提交与前端 node_id 分支共用）
RUNNER_NAME = "parse_script"

# 阶段进度区间：llm_parse 内部 0-1 映射到 0.05-0.95
_FRAC_PLAN = (0.0, 0.05)
_FRAC_PIPELINE = (0.05, 0.95)
_FRAC_PERSIST = (0.95, 1.0)


def _parse_cache_hooks() -> tuple:
    """建档阶段结果缓存读写钩子：Setting 持久化，独立容错。

    服务资产提取缓存（同文本+同画风+同模型复用——资产是全链路最贵的一段）
    与逐集审核缓存。读写故障都只记日志降级为无缓存，不阻断建档。
    """
    def load(key: str) -> str | None:
        try:
            with SessionLocal() as db:
                row = db.get(Setting, key)
                return row.value if row else None
        except Exception:  # noqa: BLE001
            logger.exception("建档缓存读取失败 key=%s", key[:40])
            return None

    def save(key: str, value: str) -> None:
        try:
            with SessionLocal() as db:
                db.merge(Setting(key=key, value=value))
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("建档缓存写入失败 key=%s", key[:40])

    return (load, save)


class ParseScriptRunner(BaseRunner):
    """剧本建档任务：文本入参，审读/分集/资产三段 LLM，产物 JSON 存档供排查。"""

    name = RUNNER_NAME
    default_model = ""
    allowed_models = [""]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        project_id = str(params.get("project_id") or "")
        text = str(params.get("text") or "")
        if not project_id or not text.strip():
            return RunnerResult(ok=False, error="建档参数缺失（project_id/text）")

        # 项目属性（新建项目时设定）：视频风格 = 画风预设（含画风规范）；
        # 画幅 = 项目画幅比例；国家 = 人物外貌约束 + 地区规范。全链路生效
        # （提示词注入 + 文生视频参数），不从剧本提取
        with SessionLocal() as db:
            ctx_style = project_style_context(db, project_id)
            # 解析前置需求（21 号批次 5）：审读时注入，让技能包自选与关键场次判断
            # 贴合用户口径（剧作基调/受众/题材/禁用内容）
            _project = db.get(Project, project_id)
            _req = load_requirements(db, project_id)
            requirements_text = review_hint(_req, _project)
        preset = ctx_style["style_hint"]
        preset_name = ctx_style["preset_name"]
        aspect_ratio = ctx_style["aspect_ratio"]
        model_presets = ctx_style["model_presets"]

        def on_progress(frac: float, label: str) -> None:
            lo, hi = _FRAC_PIPELINE
            ctx.publish_progress(lo + (hi - lo) * frac, label)

        # 自学习（教训账本）：建档前读账本构建【历史教训】段落注入拆镜提示词
        lessons_text = ""
        try:
            with SessionLocal() as db:
                _ledger = load_lessons(db, project_id)
                _global_ledger = load_global_lessons(db)
            _plain = {r: (v[0] if isinstance(v, tuple) and v else r)
                      for r, v in QA_PLAIN.items()}
            lessons_text = (build_lessons_text(_ledger, "storyboard", _plain)
                            + build_lessons_text(_ledger, "script")
                            + build_global_lessons_text(
                                _global_ledger, "storyboard", _plain)
                            + build_global_lessons_text(
                                _global_ledger, "script"))
        except Exception:  # noqa: BLE001 账本读取失败不阻断建档
            logger.exception("教训账本读取失败 task=%s", ctx.task_id)

        try:
            ctx.publish_progress(_FRAC_PLAN[1], "建档计划已生成，AI 通读剧本中")
            result = llm_parse(text, progress_cb=on_progress,
                               cancel_check=lambda: ctx.cancel_event.is_set(),
                               style_hint=preset,
                               model_presets=model_presets,
                               trace_cb=ctx.publish_trace,
                               stream_cb=ctx.publish_stream,
                               parse_cache=_parse_cache_hooks(),
                               lessons_text=lessons_text,
                               requirements_text=requirements_text,
                               project_id=project_id)
            # 产出物元信息回填：视频风格/画幅来自项目设定（非剧本提取）
            result["video_style"] = preset_name
            result["aspect_ratio"] = aspect_ratio
        except ScriptParseError as e:
            if ctx.cancel_event.is_set():
                return RunnerResult(ok=False, error="任务已取消",
                                    fail_class=getattr(e, 'fail_class', ''))
            logger.warning("剧本建档任务失败 task=%s：%s", ctx.task_id, e)
            return RunnerResult(ok=False, error=str(e),
                                fail_class=getattr(e, 'fail_class', ''))

        ctx.publish_progress(_FRAC_PERSIST[0], "建档完成，正在写入数据库")
        # 事务内**不得**调 ctx.publish_*（2026-10-10 实测死锁修复）：publish 走
        # 任务引擎的另一条数据库连接写 tasks 行，而本事务从 reset_drama 起就
        # 持有 SQLite 写锁且未提交——两连接互等 15 秒后抛 "database is locked"，
        # 整个重建回滚（实测：重拆已有产出的项目必炸；旧分镜保住纯属事务回滚
        # 的运气）。提示句攒进 _deferred_notes、提交后统一外发。
        _deferred_notes: list[tuple[str, str, str]] = []   # (kind, title, msg)
        try:
            with SessionLocal() as db:
                reset_info = reset_drama(db, project_id)
                if reset_info.get("produced_discarded"):
                    # **明确告知**（25 号 2.5）：重建会弃用旧产出——产物文件保留在
                    # assets（可回溯、没删），但已不再挂在任何分镜上。静默丢弃
                    # 付过钱的产出比抽卡更贵，所以这句话必须让用户看见。
                    # 走 **trace**（进任务轨迹、可回看）而不只是进度文案：进度
                    # 会被下一条覆盖，用户那一刻没盯着就看不到了（25 号复查）
                    _msg = (f"本次重建弃用了 {reset_info['produced_discarded']} 个"
                            "已产出分镜的版本引用（产物文件保留在 assets，可回溯）")
                    _deferred_notes.append(("discard", "重建弃用了已有产出", _msg))
                counts = persist_parse_result(db, project_id, result)
                # 形象资产迁移：按角色名挂接回新角色（重新建档不丢已生成的
                # 定妆图/四视图/妆造，音色参考同步回填）
                counts["images_moved"] = reattach_character_assets(
                    db, project_id, reset_info)
                # 资产名单未命中告警（写过程轨迹，告警不阻断）
                unmatched = counts.get("unmatched_refs") or {}
                if any(unmatched.values()):
                    parts = [f"{label}：{'、'.join(names)}"
                             for label, names in (
                                 ("角色", unmatched.get("characters") or []),
                                 ("场景", unmatched.get("scenes") or []),
                                 ("道具", unmatched.get("props") or [])) if names]
                    _deferred_notes.append(
                        ("unmatched", "资产引用告警",
                         "部分分镜引用的资产名不在解析名单，这些引用生成时将缺"
                         "参考图，建议在故事板分镜卡修正引用：" + "；".join(parts)))
                save_parse_artifacts(db, project_id, result)
        except Exception as e:  # noqa: BLE001 落库异常原样透出
            logger.exception("剧本建档落库失败 task=%s", ctx.task_id)
            return RunnerResult(ok=False, error=f"建档结果落库失败：{e}")

        # 提交后统一外发延迟提示（事务已提交，publish 的连接不再与写锁互等）
        for _kind, _title, _msg in _deferred_notes:
            if _kind == "discard":
                ctx.publish_progress(_FRAC_PERSIST[0], "注意：" + _msg)
            if ctx.publish_trace is not None:
                ctx.publish_trace("thought", _title, _msg)

        # 归档过程轨迹 + 分配新版本号（独立容错：主数据已落库成功，此处异常
        # 不得把任务打成 failed，否则前端数据已更新但任务显示失败）
        try:
            with SessionLocal() as db:
                task = db.get(Task, ctx.task_id)
                save_script_trace(
                    db, project_id,
                    dict(task.trace_json or {}) if task else None)
                bump_script_version(db, project_id)
        except Exception:  # noqa: BLE001 归档失败仅记日志
            logger.exception("建档轨迹归档/版本号分配失败 task=%s", ctx.task_id)
        ctx.publish_progress(
            _FRAC_PERSIST[1],
            f"建档完成：角色 {counts['characters']}、场景 {counts['scenes']}、"
            f"分集 {counts['episodes']}（分镜在逐集制作时生成）")
        return RunnerResult(ok=True, kind="json",
                            meta={"script": result, **counts})
