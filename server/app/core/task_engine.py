"""任务引擎：队列、状态机、调度、产物落库、WS 推送。

对齐设计文档 6.2 / 4.7：
- 任务 = 节点的一次执行
- 单 GPU 串行调度（模型互斥），LLM/ffmpeg 类任务与 GPU 任务分道
- 状态流转 queued → loading_model → running → done|failed|cached|canceled
"""

import asyncio
import contextvars
import json
import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core import access, billing, cache
from app.core.config import artifact_dir_for, settings
from app.core.provider_store import is_endpoint_model
from app.core.model_manager import KIND_LLM, project_preset
from app.core.providers import (FailClass, provider_enabled, use_llm_model,
                                use_task_ctx)
from app.db.models import Asset, NodeVersion, Project, Task, country_face_hint
from app.db.session import SessionLocal
from app.inference.pool import _dir_size_mb, pool as inference_pool
from app.runners.base import BaseRunner, RunnerContext, RunnerError
from app.runners.generate import RUNNERS, get_runner, _is_local_model
from app.runners.compose import RUNNERS_EXTRA
from app.runners.script_parse import RUNNER_NAME as RUNNER_PARSE_SCRIPT
from app.runners.script_parse import ParseScriptRunner
from app.runners.script_episode import (
    RUNNER_ASSETS, RUNNER_AUDIT, RUNNER_BUILD, RUNNER_OPTIMIZE,
    AuditEpisodeRunner, BuildEpisodeRunner, ExtractEpisodeAssetsRunner,
    OptimizeEpisodeRunner,
)
from app.runners.script_replan import RUNNER_NAME as RUNNER_REPLAN_EPISODES
from app.runners.script_replan import ReplanEpisodesRunner
from app.ws.bus import bus

ALL_RUNNERS: dict[str, BaseRunner] = {
    **RUNNERS, **RUNNERS_EXTRA,
    RUNNER_PARSE_SCRIPT: ParseScriptRunner(),
    RUNNER_REPLAN_EPISODES: ReplanEpisodesRunner(),
    RUNNER_AUDIT: AuditEpisodeRunner(),
    RUNNER_OPTIMIZE: OptimizeEpisodeRunner(),
    RUNNER_ASSETS: ExtractEpisodeAssetsRunner(),
    RUNNER_BUILD: BuildEpisodeRunner(),
}

logger = logging.getLogger(__name__)

# GPU 类 runner（互斥调度）；其余（tts/ffmpeg/llm 拆解）走并行道
GPU_RUNNERS = {"t2i", "t2v", "i2v", "fl2v", "ref2v"}

# runner → provider 任务类别映射（provider 接管判定/预检放行共用）：
# 视频类 runner 共用 t2v provider 配置（ref2v 带参考图同样可走方舟/agnes）；
# i2i（图生图）runner 继承 _provider_t2i 路径，共用 t2i 类别
_RUNNER_PROVIDER_KIND = {
    "t2i": "t2i",
    "i2i": "t2i",
    "t2v": "t2v",
    "i2v": "t2v",
    "fl2v": "t2v",
    "ref2v": "t2v",
}

# 不走产物缓存的 runner：结果须在 runner 内落库/落状态，缓存命中会跳过
# runner 执行导致副作用丢失（如剧本解析的角色/分镜写入、剧本优化的修复稿回写）
NO_CACHE_RUNNERS = {RUNNER_PARSE_SCRIPT, RUNNER_REPLAN_EPISODES,
                    RUNNER_AUDIT, RUNNER_OPTIMIZE, RUNNER_ASSETS, RUNNER_BUILD}

# 任务终态（吸收态：进入后任何 _set_status 不再覆写，取消/失败后进度泵不得复活状态）
TERMINAL_STATUSES = ("done", "failed", "canceled", "cached")

# 僵尸任务判定阈值（秒）：超期非终态任务由 sweep_stale_tasks 兜底终止
_STALE_TASK_SEC = 3 * 3600
# 进度心跳阈值（秒）：任务执行中【超过该时长无任何进度推送】即判定为卡死。
# 为什么用「心跳」而非总超时：视频任务排队+生成可达 15 分钟属正常（会持续推
# 进度），而卡死表现为【零进度】——心跳判活既不会误杀长任务，又能及时救回
# 泳道（实测：provider 调用无超时阻塞时，泳道 worker 被永久占住，取消也救不回，
# 必须重启进程；心跳看门狗是第二道防线）。
_STALL_SEC = 20 * 60
# 心跳检查间隔（秒）
_STALL_CHECK_SEC = 60

# ---------- 可重试失败类的自动重试（25 号批次 1.2） ----------
# **只重试「请求大概率没有落地」的类别**：网络失败（连不上）、限流（429，被拒）、
# 服务端错误（5xx，未开始处理）。**超时不在其列**——请求可能已经落地，原样重提
# 会重复计费（「幂等是重试的前提」，25 号 §7.9）。内容安全/参数/配额同样不重试：
# 前者要改内容、后两者要改请求或充值，原样重试必败。
_AUTO_RETRY_CLASSES = ("network", "rate_limit", "server")
# 同一任务最多自动重试次数（余量给用户手动重摇，不无限占预算）
_AUTO_RETRY_MAX = 2
# 第 1/2 次重试前的等待（秒）——退避而非固定间隔，避免故障期自造 DoS
_AUTO_RETRY_BACKOFF_SEC = (5, 20)
# 重试计数记在 params 的下划线键里：不进 generation trace（trace 剔除下划线键），
# 且**重启后仍在**——1.1 的重启恢复会把 queued 任务重新入队，计数不会归零。
_AUTO_RETRY_KEY = "_auto_retry"
# 熔断阈值（1.7）：窗口内同一 `runner::model` 的基础设施失败达到这个数，
# 就**暂停自动重试**（只停自动的；用户手动重摇不受限）——故障期成批任务各自
# 退避重试会变成「自制的 DoS」，在供应商已经不稳时继续加压。
_BREAKER_WINDOW_SEC = 300
_BREAKER_THRESHOLD = 8

# 加载模型进度泵：采样间隔 / 进度上限（余量留给落位/转档阶段）/
# psutil 缺失时按时间线性推进的兜底速率（每秒）
_LOAD_POLL_SEC = 1.0
_LOAD_PCT_CAP = 0.95
_LOAD_PCT_FLOOR = 0.02
_LOAD_FALLBACK_RATE = 0.03


def _now() -> int:
    return int(time.time())


def _vram_allocated_bytes() -> int:
    """当前进程显存分配字节数（torch 缺失或无 CUDA 返回 0；进度泵增量分子）。"""
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.memory_allocated(0)
    except ImportError:
        pass
    return 0


def _trace_summary(trace: dict) -> dict:
    """轨迹摘要（列表/WS 事件用）：体量与最后阶段，不含条目正文。

    完整轨迹只走任务详情接口（getTask），避免 2s 轮询/高频 WS 事件把
    数百 KB 的 thoughts 反复序列化下发（审计 13 号：WS 队列 500 上限
    被大 payload 挤爆后终态事件会被静默丢弃）。
    """
    stages = trace.get("stages") or []
    thoughts = trace.get("thoughts") or []
    last_stage = stages[-1].get("title", "") if stages else ""
    return {"stage_count": len(stages), "thought_count": len(thoughts),
            "last_stage": last_stage}


class TaskEngine:
    def __init__(self) -> None:
        self._gpu_queue: asyncio.Queue[str] = asyncio.Queue()
        self._fast_queue: asyncio.Queue[str] = asyncio.Queue()
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="runner")
        # 当前驻留 GPU 模型（以资源池 stats 为准，此值仅作记录）
        self._current_gpu_model: str | None = None
        self._workers_started = False
        # 泳道 worker 强引用（create_task 无强引用会被事件循环 GC 中途取消）
        self._worker_tasks: dict[str, asyncio.Task] = {}
        # 僵尸任务清扫节流时间戳（submit 顺带触发）
        self._last_sweep = 0.0
        # 基础设施失败账（`runner::model` → 失败时间戳列表），供熔断判定用（1.7）：
        # 故障期成批任务各自退避重试会变成「自制的 DoS」，故按端点统计并临时停重试
        self._infra_fails: dict[str, list[float]] = {}
        # 主事件循环引用（start_workers 时记录）：runner 线程的跨线程推送依赖它
        self._loop: asyncio.AbstractEventLoop | None = None
        # 任务心跳（task_id → 最后进度推送时间戳，单调时钟）：卡死判活依据
        self._heartbeat: dict[str, float] = {}
        # 已为哪个卡死任务补过泳道 worker（防看门狗每轮重复补；任务清理时复位）
        self._stall_compensated: set[str] = set()
        # 任务 → 泳道（看门狗补 worker 只补**卡死任务所在的那条**：
        # 2026-09-30 复查 R2——旧实现两条泳道都补，健康泳道也会多出一个 worker，
        # GPU 单串行契约被静默破坏、worker 数随每次卡死单调累积）
        self._lane_of: dict[str, str] = {}
        # 提交时算好的缓存键（task_id → key）：执行侧写缓存**直接复用这个键**。
        # 为什么不能在执行侧重算：提交后 params 里已被注入随机 seed（抽卡语义），
        # 重算必然得到另一个键（2026-09-30 修「缓存永不命中」的直接原因）。
        self._cache_keys: dict[str, str] = {}
        # 看门狗协程（start_workers 启动；应用关停时随循环取消）
        self._watchdog_task: asyncio.Task | None = None

    # ---------- 对外接口 ----------
    def submit(self, project_id: str, node_id: str, runner_name: str,
               model_id: str = "", params: dict | None = None,
               input_refs: dict | None = None,
               batch_id: str | None = None,
               inject_country_hint: bool = True,
               reroll: bool = True,
               actor_user_id: str = "") -> dict[str, Any]:
        """提交生成任务。

        reroll（抽卡语义，审计 13 号 B）：True=「再抽一次」——未指定 seed 时注入
        随机种子并**跳过缓存查询**，保证每次真生成出新图（默认，保持既有用户
        预期；显式指定 seed 时按键查缓存，同 seed 同图可复现）；False=「要回旧
        产物」——系统内部重算/重试用，不注入随机种子，让同参数提交命中缓存
        零成本返回（缓存按无 seed 的键写，取到的是该组参数最新一张）。

        actor_user_id：发起人（2026-09-30 多租户）。**由 API 层显式传入**，不用
        contextvar——实测中间件里 set 的 contextvar 到端点线程读不到（Starlette 的
        BaseHTTPMiddleware 在独立任务里跑，同步端点又走线程池，值传不过去），
        显式传参既可靠又不用猜。
        """
        if runner_name not in ALL_RUNNERS:
            raise RunnerError(f"未知 runner：{runner_name}")
        params = dict(params or {})
        # 僵尸任务兜底清扫（节流 1 分钟/次，见 sweep_stale_tasks）
        try:
            self.sweep_stale_tasks()
        except Exception:  # noqa: BLE001 清扫失败不影响提交
            logger.exception("僵尸任务清扫失败")
        # 提交预检：真实推理模式下 GPU 任务要求本地模型已安装（fail-fast，不落库）
        _preflight_model(runner_name, model_id)
        task_id = uuid.uuid4().hex
        with SessionLocal() as db:
            # 项目国家预设 → 人物外貌约束（仅 t2i 注入）。视频类（t2v/i2v/
            # fl2v/ref2v）不注入：video_prompt 的【全局定义】已由 LLM 按地区
            # 规范写入人种设定，前置的人脸词（面孔/五官/肤色）反而会诱导
            # 模型生成面部特写；场景图由调用方显式关闭（同理）
            project = db.get(Project, project_id)
            country = getattr(project, "country", None) if project else None
            hint = country_face_hint(country)
            if inject_country_hint and hint and runner_name == "t2i":
                params["country_hint"] = hint
            # 缓存键统一口径（审计 13 号 B）：读键与写键必须同源——旧实现读侧用
            # 原始 model_id（未指定时是空串）与 artifact id，写侧用解析后的默认
            # 模型与文件路径，两个 sha256 必然不同，短剧链路（恒带参考图）缓存
            # 从不命中、「抽卡切回旧参数零成本」完全失效。此处把提交时的解析
            # 结果固化，读写共用同一键。
            cache_model = model_id or ALL_RUNNERS[runner_name].default_model
            cache_refs = self._resolve_refs(input_refs or {})
            # 缓存键**只算一次**（在随机 seed 注入之前）：读（本次 lookup）与写
            # （_execute_inner 的 cache.store）共用它。
            #
            # 2026-09-30 修「缓存永不命中」：旧实现把 seed 注入写在 lookup 之后，
            # 但代码顺序实际是「先注入 → 再 lookup」，于是 params 里恒有一个随机
            # seed ⇒ 每次提交算出的键都不同 ⇒ 缓存只写不读、命中率恒为 0，
            # 「多级缓存省 70% 成本」对生成链路完全不成立（注释与自检声明都写着
            # 「放在缓存查询之后」，实现与声明相反）。
            # 现在把 key 固化下来、执行侧复用，任何一边都不再重算，杜绝再次漂移。
            cache_key = cache.make_key(runner_name, cache_model, params, cache_refs)
            # 「再抽一次」语义：未指定 seed 时注入随机种子。
            #
            # 2026-09-30 回修（对齐 doc 14 §4.1「reroll=true 必不命中」）：既然是
            # 「抽新图」，这一单就**跳过缓存查询**——旧实现把键算完后注入 seed、
            # 却仍拿「无 seed 的键」去查，于是同参数二次提交必然命中上一次的结果，
            # 用户点「重摇」拿回同一张图（探针实测复现），且照样占一次重摇预算。
            # 缓存仍按**无 seed 的键**写入：reroll=False 的「切回旧参数零成本」
            # 照常命中最新一张，两个语义各归各位。
            fresh_draw = False
            if (reroll and runner_name in GPU_RUNNERS
                    and str(params.get("seed") or "").strip() == ""):
                params["seed"] = random.randrange(2**31)
                fresh_draw = True
            # 缓存命中直接完成（NO_CACHE_RUNNERS 除外：副作用在 runner 内，须真实执行）
            hit = None if (runner_name in NO_CACHE_RUNNERS or fresh_draw) else \
                cache.lookup(runner_name, cache_model, params, cache_refs,
                             key=cache_key)
            status = "cached" if hit else "queued"
            artifact_id = hit or None
            task = Task(
                id=task_id, project_id=project_id, node_id=node_id,
                runner=runner_name, model_id=model_id,
                params_json=params, input_refs_json=input_refs or {},
                status=status, artifact_id=artifact_id,
                batch_id=batch_id,
                timings_json={},
                # 发起人（2026-09-30 多租户）：任务归属依据。API 层显式传入；
                # 内部调用（重算/重试/无请求上下文）留空，判权时退回按项目归属。
                initiator_user_id=actor_user_id or "",
                # 计费流水关联（33 号方案）：由 BillingGate 扣费后经 ContextVar
                # 自动带出（见 core/billing.py 顶部说明）；任务失败/取消时凭它
                # 自动退款。无计费上下文 = None = 不做退款判定（fail-safe）。
                billing_txn_id=billing.current_txn_id() or None,
            )
            if hit:
                task.finished_at = _now()
                task.progress = 1.0
                # 缓存命中同样追加版本（抽卡切回旧参数重生成零成本）
                task.version_id = self._append_version(
                    db, project_id, node_id, params, input_refs or {},
                    "generated", hit, task_id,
                )
            db.add(task)
            db.commit()
        # 计费（33 号方案）注：**不在 submit 内触发结算**——评审抓到的时序缺陷：
        # 批量端点循环提交时，第 1 个提交（如命中缓存）此刻兄弟任务还没建完，
        # 「全终态且无 done」的判定会把整笔预扣误退。cached 与「极快失败」的
        # 结算统一挪到 BillingGate 响应后（全部任务已提交完，见 main.py）；
        # 任务执行中的 done/failed 终态由 _finish_ok / _fail 触发（同样安全）。
        # 缓存键留给执行侧写回用（未命中才会走到执行；命中时不再需要）
        if not hit:
            self._cache_keys[task_id] = cache_key
        if not hit:
            q = self._gpu_queue if runner_name in GPU_RUNNERS else self._fast_queue
            q.put_nowait(task_id)
        self._publish(project_id, "task_created", self._task_dict(task_id))
        return {"task_id": task_id, "status": status, "cached": bool(hit)}

    def find_active_task(self, project_id: str,
                         node_id: str) -> dict[str, Any] | None:
        """查同节点是否已有活跃任务（活动任务复用去重，借鉴 Jellyfish）。

        治「重复提交即重复计费」：HTTP 超时重试 / 多标签页 / 连点 都会对同一
        镜头重复提交，而 GPU 任务未指定 seed 时必然真生成（缓存不命中）——
        用户只看到一份产物、费用照付。返回活跃任务摘要供调用方复用或提示。
        """
        with SessionLocal() as db:
            row = db.scalars(
                select(Task).where(Task.project_id == project_id,
                                   Task.node_id == node_id,
                                   Task.status.in_(("queued", "loading_model",
                                                    "running")))
                .order_by(Task.created_at.desc()).limit(1)
            ).first()
            if row is None:
                return None
            return {"task_id": row.id, "status": row.status,
                    "progress": row.progress, "node_id": row.node_id}

    def cancel(self, task_id: str) -> bool:
        """取消任务：排队任务直接置 canceled；加载/生成中任务置取消事件并立即落
        canceled 态（加载完成或 runner 步级检查到事件后跳过/中止生成，不再覆写状态）。

        计费（33 号）：取消是结算触发点之一——取消后调结算判定（用户没拿到
        结果、且整批无成功产物时自动退款；见 billing.settle_after_terminal）。
        """
        ev = self._cancel_events.get(task_id)
        if ev:
            ev.set()
            txn_id = ""
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                if task and task.status not in TERMINAL_STATUSES:
                    task.status = "canceled"
                    task.finished_at = _now()
                    txn_id = task.billing_txn_id or ""
                    db.commit()
                    self._publish(task.project_id, "task_update", self._task_dict(task_id))
            if txn_id:
                billing.settle_after_terminal(txn_id)
            return True
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if task and task.status in ("queued",):
                task.status = "canceled"
                task.finished_at = _now()
                txn_id = task.billing_txn_id or ""
                db.commit()
                self._publish(task.project_id, "task_update", self._task_dict(task_id))
                if txn_id:
                    billing.settle_after_terminal(txn_id)
                return True
        return False

    def list_tasks(self, project_id: str | None = None,
                   batch_id: str | None = None,
                   project_ids: list[str] | None = None) -> list[dict]:
        """任务摘要列表。

        project_ids：**多项目过滤**（2026-09-30 跨租户收口）——不带单个
        project_id 时，API 层传「当前用户自己的项目」，而不是像旧实现那样
        返回全库任务（任何登录用户可拉别人的任务摘要）。
        """
        with SessionLocal() as db:
            # rowid 兜底同秒创建的任务排序，保证「最新任务」稳定在列表头部
            q = select(Task).order_by(Task.created_at.desc(), text("rowid DESC"))
            if project_id:
                q = q.where(Task.project_id == project_id)
            elif project_ids is not None:
                q = q.where(Task.project_id.in_(project_ids or [""]))
            if batch_id:
                # 批次聚合查询：批量生成可达数十条任务，放宽上限
                q = q.where(Task.batch_id == batch_id).limit(500)
            else:
                q = q.limit(200)
            return [self._to_dict(t) for t in db.scalars(q)]

    def recover(self) -> int:
        """服务启动时处理重启遗留的非终态任务（**三分法**，25 号批次 1.1）。

        原先一刀切判死，而正式库实测 81 条「服务重启导致任务中断」里 **71 条其实
        还没开始跑**（`started_at` 为空）——没跑过、没花钱，重跑就是用户本意，
        判死等于白扔。

        | 中断时状态 | 处理 | 理由 |
        | --- | --- | --- |
        | `queued`（未开始） | **重新入队** | 零成本；`recover()` 在
          `start_workers()` 之前调用（见 main.py），入队后即被消费 |
        | 已开始（loading_model/running） | 判失败，提示重试 | 远端任务号**未落库**
          （只存在于被杀进程的局部变量里）⇒ 无法自动回捞；但已计费，不能假装没发生 |

        返回**判失败**的条数（重新入队数走日志——不能把「救回」混进「清理」）。
        """
        requeued: list[tuple[str, str]] = []      # (task_id, runner)
        failed = 0
        failed_txns: list[str] = []               # 判失败任务的计费流水（待结算）
        with SessionLocal() as db:
            rows = db.scalars(
                select(Task).where(Task.status.in_(("queued", "loading_model", "running")))
            ).all()
            for t in rows:
                if t.status == "queued" and not t.started_at:
                    t.status = "queued"
                    t.error = ""
                    requeued.append((t.id, t.runner))
                else:
                    t.status = "failed"
                    t.error = "服务重启导致任务中断，请重试"
                    t.finished_at = _now()
                    if t.billing_txn_id:
                        failed_txns.append(t.billing_txn_id)
                    failed += 1
            db.commit()
        # 入队必须在 commit 之后：worker 读到的一定是已提交的状态
        for _tid, _runner in requeued:
            _q = self._gpu_queue if _runner in GPU_RUNNERS else self._fast_queue
            _q.put_nowait(_tid)
        if requeued:
            logger.info("重启恢复：%s 条任务重新入队（中断时尚未开始，零成本）",
                        len(requeued))
        # 计费（33 号）：重启判失败是结算触发点——逐个走结算判定（幂等；
        # 一笔流水被多个任务共用时只会结算一次生效，见 billing.settle_after_terminal）
        for _txn in failed_txns:
            try:
                billing.settle_after_terminal(_txn)
            except Exception:  # noqa: BLE001 退款失败不阻塞启动恢复
                logger.exception("重启结算失败（流水 %s，留给启动对账兜底）", _txn)
        return failed

    def sweep_stale_tasks(self) -> int:
        """兜底清理超时僵尸任务（审计 13 号：runner 阻塞在无超时的外部调用时
        泳道会永久卡住，cancel 只改 DB 救不回线程——本方法按 started_at 兜底
        把超期非终态任务落 failed，让前端不再永久「生成中」）。

        由 submit 顺带触发（节流：≥1 次/分钟），不引入新线程；阈值取 3 小时
        （极保守——正常任务：视频轮询 15 分钟上限、LLM 解析分钟级，3 小时
        只可能是真卡死）。
        """
        now = _now()
        if now - self._last_sweep < 60:
            return 0
        self._last_sweep = now
        cutoff = now - _STALE_TASK_SEC
        stale_txns: list[str] = []
        with SessionLocal() as db:
            rows = db.scalars(
                select(Task).where(
                    Task.status.in_(("loading_model", "running")),
                    Task.started_at < cutoff)
            ).all()
            for t in rows:
                t.status = "failed"
                t.error = (f"任务超时未完成（超过 {_STALE_TASK_SEC // 3600} 小时，"
                           "可能卡在外部服务等待），已自动终止，请重试")
                t.finished_at = now
                if t.billing_txn_id:
                    stale_txns.append(t.billing_txn_id)
            if rows:
                db.commit()
                logger.error("清扫僵尸任务 %s 条（超时 %s 秒）", len(rows),
                             _STALE_TASK_SEC)
        # 计费（33 号）：僵尸判死是结算触发点——逐个走结算判定（幂等）
        for _txn in stale_txns:
            try:
                billing.settle_after_terminal(_txn)
            except Exception:  # noqa: BLE001 退款失败不阻塞清扫
                logger.exception("僵尸任务结算失败（流水 %s，留给对账兜底）", _txn)
        return len(rows)

    # ---------- 内部实现 ----------
    async def start_workers(self) -> None:
        """在应用 startup（事件循环内）调用；同步端点线程池里无法启动。"""
        if self._workers_started:
            return
        self._workers_started = True
        self._loop = asyncio.get_running_loop()
        self._spawn_worker("gpu", self._gpu_queue)
        self._spawn_worker("fast", self._fast_queue)
        # 心跳看门狗（卡死任务的第二道防线，见 _STALL_SEC 注释）
        self._watchdog_task = asyncio.create_task(self._stall_watchdog())

    def _spawn_worker(self, lane: str, queue: asyncio.Queue) -> None:
        """启动泳道 worker 并保强引用 + 完成回调（崩溃自愈）。

        裸 create_task 既无强引用（事件循环可能中途 GC 掉）也无完成回调——
        worker 一旦退出，该泳道后续任务永远停在 queued，前端永久「生成中」
        且拒绝重发，只有重启进程能恢复（审计 13 号 A）。
        """
        task = asyncio.create_task(self._worker(lane, queue))
        self._worker_tasks[lane] = task

        def _on_done(t: "asyncio.Task", _lane=lane, _q=queue) -> None:
            if t.cancelled():
                return  # 应用关停的正常取消
            exc = t.exception()
            logger.error("泳道 %s worker 意外退出（%s），即将重启",
                         _lane, exc)
            # 热重启：DB 抖动等瞬时故障恢复后队列继续消费；由 _done 回调
            # 重新拉起，不依赖进程重启
            self._loop.call_later(3.0, self._spawn_worker, _lane, _q)

        task.add_done_callback(_on_done)

    async def _worker(self, lane: str, queue: asyncio.Queue) -> None:
        while True:
            task_id = await queue.get()
            try:
                try:
                    await self._execute(task_id, lane)
                except Exception as e:  # noqa: BLE001
                    logger.exception("任务内部错误 task=%s lane=%s", task_id, lane)
                    try:
                        self._fail(task_id, f"内部错误：{e}")
                    except Exception:  # noqa: BLE001
                        # 失败落库本身再抛（DB 锁/磁盘满）：绝不外抛——
                        # 外抛会让 while True 退出、泳道永久死亡
                        logger.exception("失败落库亦失败 task=%s（任务保持非终态，"
                                         "由 _done 回调重启泳道后继续消费）", task_id)
            finally:
                queue.task_done()

    async def _stall_watchdog(self) -> None:
        """心跳看门狗：执行中任务超过 _STALL_SEC 无进度 → 判卡死并终止。

        实测场景（本轮复现两次）：provider 调用在无网络超时时阻塞，泳道 worker
        被永久占住，后续任务全部 queued——取消只改 DB 状态救不回线程，只能重启
        进程。看门狗是第二道防线：判定卡死后把任务落 failed 并**补齐泳道 worker**
        （卡死的线程无法回收，用新 worker 继续消费队列，坏线程遗留但不再阻塞）。

        判活口径＝心跳（任何进度推送都刷新），因此长时间但持续出进度的视频任务
        不会被误杀。
        """
        while True:
            await asyncio.sleep(_STALL_CHECK_SEC)
            now = time.monotonic()
            stalled = [tid for tid, ts in list(self._heartbeat.items())
                       if now - ts > _STALL_SEC]
            for tid in stalled:
                logger.error("任务 %s 心跳停滞超过 %s 秒，判定卡死并终止",
                             tid, int(_STALL_SEC))
                had_heartbeat = self._heartbeat.pop(tid, None) is not None
                try:
                    self._fail(tid, f"任务心跳停滞超过 {int(_STALL_SEC / 60)} 分钟"
                                    "（疑似外部服务无响应），已自动终止；"
                                    "如为端点拥堵请稍后重试",
                               FailClass.TIMEOUT)
                except Exception:  # noqa: BLE001 看门狗自身绝不因落库失败而退出
                    logger.exception("卡死任务落库失败 task=%s", tid)
                # 泳道补齐：卡死任务占住的是 runner 线程池线程，而泳道 worker
                # 此刻**仍存活**（await 在 executor future 上）——它永远不会再
                # 消费队列，故必须补一个 worker 接管。安全性：旧 worker 卡在
                # await 上，不会再调 queue.get，不会与新 worker 争抢任务
                # （任务在旧线程里已注册取消事件，看门狗已将其落 failed）。
                #
                # **判据修正（2026-09-30）**：原条件是 `task is None or task.done()`
                # ——而卡死的 worker 恰恰是**未完成**（await 挂住）的那个，`done()`
                # 恒为 False，于是这段补偿在它自己描述的场景里**从不触发**：GPU 泳道
                # （单 worker）被一条挂死的云端调用永久占住，后续任务全部排队。
                # 正确语义 = 「worker 还活着（没 done）但它再也消费不了队列」。
                #
                # **只补一次**：卡死任务在心跳表被摘掉前会一直出现在 stalled 里，
                # 每轮都补就会无限堆 worker。用 `had_heartbeat`（本次才摘掉的）
                # 作为「首次判定」标记，任务清理时复位该标记（见 _execute 收尾）。
                if not had_heartbeat or tid in self._stall_compensated:
                    continue
                # 只补**卡死任务所在的那条泳道**（2026-09-30 复查 R2）：旧实现
                # 遍历两条泳道、各自判断「worker 未完成」，而正常时两条 worker
                # 都未完成 → 健康泳道也被加一个 worker，GPU 单串行被破坏。
                lane = self._lane_of.get(tid)
                if lane is None:
                    # 认不出泳道（理论上任务执行中必有记录）：保守不补，
                    # 宁可晚一轮也不能误伤健康泳道
                    continue
                task = self._worker_tasks.get(lane)
                q = self._gpu_queue if lane == "gpu" else self._fast_queue
                if task is not None and not task.done():
                    self._stall_compensated.add(tid)
                    self._spawn_worker(lane, q)
                    logger.warning("泳道 %s 已补齐 worker（原 worker 被卡死"
                                   "任务 %s 占住，其线程不可回收）", lane, tid)

    async def _execute(self, task_id: str, lane: str) -> None:
        # 取消事件【先登记】再查库（审计 13 号 C）：旧实现把登记放在两次查库+
        # 引用解析之后，任务被 worker 弹出后、登记前取消时 cancel 找不到事件
        # （状态已非 queued 也走不到兜底分支）→ 什么都不做，GPU 仍跑满全程并
        # 落产物/写缓存/追加版本。登记提前后，cancel 一定能 set 到事件。
        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event
        self._lane_of[task_id] = lane
        # 心跳起点：任务开始执行即算一次「活」（此后每次进度推送刷新）
        self._heartbeat[task_id] = time.monotonic()
        if cancel_event.is_set():
            self._cancel_events.pop(task_id, None)
            self._heartbeat.pop(task_id, None)
            return  # 登记前已被取消（cancel 走 queued 兜底分支置了态）
        # 缓存键在这里统一解析（与 submit 侧同源：解析后的 model_id + 文件路径）：
        # 提交与执行两侧共用同一键构造，缓存才可能命中（审计 13 号 B）。
        # 引用失效在同一步落成任务失败（13 号 K-中）——不再静默降级为纯文生图。
        cache_model, cache_refs, missing_refs = "", {}, []
        with SessionLocal() as db:
            _t = db.get(Task, task_id)
            if _t is not None:
                cache_model = (_t.model_id
                               or ALL_RUNNERS[_t.runner].default_model)
                cache_refs = self._resolve_refs(dict(_t.input_refs_json or {}),
                                                missing_refs)
        if missing_refs:
            self._fail(task_id,
                       f"任务引用的产物缺失（{len(missing_refs)} 项，可能已被"
                       "清理或路径变更）："
                       + "、".join(m[:12] for m in missing_refs[:5])
                       + "。请重新生成对应产物后重试")
            self._cancel_events.pop(task_id, None)
            self._heartbeat.pop(task_id, None)
            return
        try:
            await self._execute_inner(task_id, lane, cancel_event,
                                      cache_model, cache_refs)
        finally:
            self._cancel_events.pop(task_id, None)
            self._heartbeat.pop(task_id, None)
            self._lane_of.pop(task_id, None)
            # 复位「已补泳道」标记：任务收尾后不再需要它，长期驻留会漏内存
            self._stall_compensated.discard(task_id)
            # 复位缓存键：写缓存成功时已被 pop，这里兜住失败/取消/引用缺失等
            # 未走到写入的路径（否则键会随任务数单调堆积）
            self._cache_keys.pop(task_id, None)

    async def _execute_inner(self, task_id: str, lane: str,
                             cancel_event: asyncio.Event,
                             cache_model: str = "",
                             cache_refs: dict | None = None) -> None:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task or task.status == "canceled":
                return
            project_id, runner_name = task.project_id, task.runner
            node_id = task.node_id
            # 项目级 LLM 端点预设（空串 = 走全局默认）：本次任务的**所有**
            # LLM 调用都用它，经 use_llm_model 注入执行期上下文（见下方提交处）
            llm_preset = project_preset(db.get(Project, project_id), KIND_LLM)
            # raw_model_id 为任务的原始模型选择（空 = 未指定）：provider 分发判定
            # 用它而非解析后的默认值，否则默认本地模型会把第三方路径恒然拦死
            raw_model_id = task.model_id
            model_id = raw_model_id or ALL_RUNNERS[runner_name].default_model
            params, input_refs = dict(task.params_json), dict(task.input_refs_json)
            input_refs = self._resolve_refs(input_refs)

        # provider 接管判定（按 runner → provider 类别映射覆盖全部视频类 runner）：
        # 配置了第三方（或任务模型为端点编码串）且未显式选择本地模型时，由第三方
        # 执行——跳过本地模型预加载（省显存与等待，也避免本地模型加载失败
        # （如显存不足）抢在第三方调用之前把任务打成失败；编码串预热本地会
        # 直接以「模型未安装」失败）
        provider_kind = _RUNNER_PROVIDER_KIND.get(runner_name)
        provider_served = (
            provider_kind is not None
            and not settings.mock_mode
            and (provider_enabled(provider_kind)
                 or is_endpoint_model(raw_model_id))
            and not _is_local_model(raw_model_id)
        )

        # 生成前复核取消态：登记与首次查库之间入队的取消已把状态置为 canceled，
        # 此处补一次读（避免已取消任务继续加载模型/生成）
        with SessionLocal() as db:
            if (db.get(Task, task_id) or None) is None or \
                    (db.get(Task, task_id).status == "canceled"):
                return

        # 模型换载（GPU 道）：以资源池驻留状态判断，未驻留则真实预热加载；
        # provider 接管的任务跳过本地模型加载（第三方执行无需本地权重）
        load_sec: float | None = None
        if lane == "gpu" and not provider_served:
            resident = {r["model_id"] for r in inference_pool.stats()["resident"]}
            if model_id not in resident:
                self._set_status(task_id, "loading_model", progress=0.0)
                self._publish(project_id, "task_update", self._task_dict(task_id))
                if _inference_ok():
                    # 真实预热：同模型命中池缓存零耗时，新模型按 LRU 腾显存加载；
                    # 加载期间启动进度泵（前端显示「加载模型 N%」），结束即取消
                    pump = asyncio.create_task(self._pump_loading_progress(
                        task_id, project_id, model_id,
                    ))
                    try:
                        load_sec = await asyncio.to_thread(
                            inference_pool.preload, model_id, runner_name,
                        )
                    except RunnerError as e:
                        # 模型未安装等确定性错误：任务直接失败，错误信息原样透出
                        self._fail(task_id, str(e))
                        return
                    finally:
                        pump.cancel()
                else:
                    # 推理依赖缺失（mock 环境）：保持模拟换载耗时，状态机正常流转
                    await asyncio.sleep(0.6)
                self._current_gpu_model = model_id
        # 加载期间被取消：不再进入生成（已加载管线驻留资源池，供后续任务复用）
        if cancel_event.is_set():
            self._set_status(task_id, "canceled", finished=True)
            self._publish(project_id, "task_update", self._task_dict(task_id))
            return
        self._set_status(task_id, "running", progress=0.0, started=True)
        self._publish(project_id, "task_update", self._task_dict(task_id))

        def publish_progress(p: float, label: str = "") -> None:
            # 心跳刷新：任何进度推送＝任务仍活着（看门狗据此判卡死，见 _STALL_SEC）
            self._heartbeat[task_id] = time.monotonic()
            self._set_status(task_id, "running", progress=max(0.0, min(1.0, p)))
            self._publish(project_id, "task_progress", {
                "task_id": task_id, "node_id": node_id,
                "progress": max(0.0, min(1.0, p)), "label": label,
            })

        def publish_trace(kind: str, title: str, content: str = "") -> None:
            """过程轨迹：追加持久化到任务行 + WS 实时推送（解析可观测）。"""
            self._append_trace(task_id, project_id, node_id, kind, title, content)

        def publish_generation(payload: dict) -> None:
            """生成参数结构化记录（trace_json.generation 键 + WS 推送）。

            排查生成质量（串脸/构图错/方向反）时核对「模型实际收到什么」：
            最终 prompt、参考图清单（顺序即参考图编号）、模型与截断告警。
            """
            self._set_generation_trace(task_id, project_id, node_id, payload)

        def publish_stream(kind: str, text: str) -> None:
            """流式思考增量：仅 WS 实时推送（高频、不落库）。

            kind: begin（text=活动标签）| delta（text=思考/正文增量）| end。
            推送时映射为前端约定的 stream_begin / stream / stream_end。
            持久化的过程轨迹只存 LLM 回复结论（llm_parser 经 publish_trace
            写入的结论条目），思考原文不做归档。
            """
            event = {"task_id": task_id, "node_id": node_id}
            if kind == "begin":
                event.update({"kind": "stream_begin", "title": (text or "")[:120]})
            elif kind == "delta":
                event.update({"kind": "stream", "text": (text or "")[:400]})
            else:
                event.update({"kind": "stream_end"})
            self._publish(project_id, "task_trace", event)

        ctx = RunnerContext(
            task_id=task_id,
            cancel_event=cancel_event,
            publish_progress=publish_progress,
            artifacts_dir=settings.artifacts_dir,
            models_dir=settings.models_dir,
            # 产物按项目分目录（见 core/config.artifact_dir_for）：runner 不查库，
            # 由这里从 Task.project_id 填入
            project_id=project_id or "",
            mock=settings.mock_mode or not _inference_ok(),
            raw_model_id=raw_model_id,
            publish_trace=publish_trace,
            publish_stream=publish_stream,
            publish_generation=publish_generation,
        )

        runner = ALL_RUNNERS[runner_name]
        t0 = time.time()
        try:
            # **必须先 with 再 copy_context 再提交**：run_in_executor 不传播
            # contextvar（实测），只传函数是拿不到项目预设的
            # 任务上下文一并设置：模型调用日志靠它归属到项目/任务（providers 侧读）
            with use_task_ctx(project_id, task_id), use_llm_model(llm_preset):
                _llm_ctx = contextvars.copy_context()
                result = await asyncio.get_running_loop().run_in_executor(
                    self._pool, _llm_ctx.run, runner.run,
                    ctx, model_id, params, input_refs,
                )
        except RunnerError as e:
            if cancel_event.is_set():
                # runner 内部取消（check_cancel 抛出）：按取消而非失败落态
                self._set_status(task_id, "canceled", finished=True)
                self._publish(project_id, "task_update", self._task_dict(task_id))
            else:
                self._fail_or_retry(task_id, str(e),
                                  getattr(e, "fail_class", ""))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("推理异常 task=%s runner=%s model=%s", task_id, runner_name, model_id)
            # 完整 traceback **只进日志文件**；任务 error 只留一行可读结果
            # （2026-09-30 复查 F3：旧实现把 traceback 原样拼进 task.error 并
            # 在界面上屏——用户看不懂，还泄露服务器路径）。
            self._fail(task_id, f"{type(e).__name__}: {e}")
            return

        if cancel_event.is_set():
            self._set_status(task_id, "canceled", finished=True)
            self._publish(project_id, "task_update", self._task_dict(task_id))
            return

        # 判死复核（2026-09-30 复查 R5）：看门狗判死只改数据库状态，runner 在
        # 线程池里可能稍后才返回——若任务已是终态（failed/canceled），丢弃这个
        # 迟到结果：不落产物、不写缓存、不追加版本。旧实现只查 cancel_event，
        # 于是「任务显示失败，版本条里却冒出一张新图、缓存也存了一份」，
        # 状态与事实对不上，还会误导用户重复付费。
        with SessionLocal() as db:
            _cur = db.get(Task, task_id)
            _cur_status = _cur.status if _cur else ""
        if _cur_status not in ("running", "loading_model"):
            logger.warning("任务 %s 已是终态 %s，丢弃迟到结果（不落产物/缓存/版本）",
                           task_id, _cur_status)
            return

        if result is None:
            # runner 契约：必须返回 RunnerResult。返回 None 时旧实现在下一行直接
            # `result.ok` → AttributeError 逃到泳道层，落一条**没有 traceback** 的
            # 「内部错误：'NoneType' object has no attribute 'ok'」——用户只看到一句
            # 黑话，排查时连哪个 runner 都不知道（实测本项目 5 条同秒失败）。
            logger.error("runner 未返回结果 task=%s runner=%s model=%s",
                         task_id, runner_name, model_id)
            self._fail(task_id, "runner 未返回结果（内部错误：runner.run 返回 None）")
            return

        if not result.ok:
            # 按类分流（25 号 1.2）：runner 透传的 fail_class 决定「可重试类自动
            # 重试」（网络/限流/5xx）；空串或不可重试类直接落终态
            self._fail_or_retry(task_id, result.error or "未知错误",
                                getattr(result, "fail_class", "") or "")
            return

        # 产物落库
        artifact_id = self._save_artifact(task_id, result, project_id)
        if runner_name not in NO_CACHE_RUNNERS:
            # 写键 = 提交时固化好的那个键（不再就地重算：params 已被注入随机
            # seed，重算必然与读键不同——2026-09-30 修「缓存永不命中」）
            cache.store(runner_name, cache_model or model_id, params,
                        cache_refs if cache_refs else input_refs, artifact_id,
                        key=self._cache_keys.pop(task_id, ""))
        elapsed = round(time.time() - t0, 2)
        # 结果侧记录（25 号 2.1）：成功也记——只看失败会漏掉"成了但很慢"的观测
        self._stamp_generation_result(task_id, {
            "ok": True, "elapsed_sec": elapsed, "artifact_id": artifact_id})
        timings = {"elapsed_sec": elapsed}
        if load_sec is not None:
            timings["load_sec"] = round(load_sec, 2)
        # 抽卡：任务完成自动追加版本，前端收到事件后拉取版本列表
        with SessionLocal() as db:
            task_row = db.get(Task, task_id)
            version_id = self._append_version(
                db, project_id, task_row.node_id, task_row.params_json,
                task_row.input_refs_json, "generated", artifact_id, task_id,
            ) if task_row else None
            if task_row and version_id:
                task_row.version_id = version_id
                db.commit()
            # 一次抽 N 张：**其余候选也各落一个版本**（顺序即抽卡顺序，最后一张
            # 默认为"最新"=未挑选时的生效首帧；用户可在分镜卡上「设为首帧」改指）
            _extra_ids = []
            if task_row is not None and result.kind == "image":
                _a0 = db.get(Asset, artifact_id) if artifact_id else None
                _extra_ids = [i for i in
                              ((_a0.meta_json or {}).get("artifact_ids") or [])
                              if i and i != artifact_id]
            for _extra in _extra_ids:
                _vid = self._append_version(
                    db, project_id, task_row.node_id, task_row.params_json,
                    task_row.input_refs_json, "generated", _extra, task_id,
                ) if task_row else None
                if _vid:
                    self._publish(project_id, "version_created", {
                        "node_id": task_row.node_id, "version_id": _vid,
                        "task_id": task_id, "artifact_id": _extra})
            # **必须补提交**：`_append_version` 只 add 不 commit（约定由调用方提交），
            # 上面那次 commit 在循环之前——不补的话候选版本随会话关闭被回滚，
            # 落库的只有第一张（实测：调用 3 次、库里只剩 1 条）
            if _extra_ids:
                db.commit()
        self._set_status(
            task_id, "done", progress=1.0, artifact_id=artifact_id,
            finished=True, timings=timings,
        )
        # 计费（33 号方案按用量升级，2026-10-07）：任务成功是「按用量结算」的
        # 触发点——settle_after_terminal 内部判定：该流水关联任务全部终态且
        # 存在 done 时，per_usage 流水按成功任务的**实际用量**多退少补；
        # per_call 流水计费成立不动。幂等（可被最后一个终态任务重复触发）。
        _txn = task_row.billing_txn_id if task_row else None
        if _txn:
            try:
                billing.settle_after_terminal(_txn)
            except Exception:  # noqa: BLE001 结算异常不影响任务完成
                logger.exception("任务成功结算异常（流水 %s，留给对账兜底）", _txn)
        self._publish(project_id, "task_update", self._task_dict(task_id))
        self._publish(project_id, "version_created", {
            "node_id": task_row.node_id if task_row else "",
            "version_id": version_id, "task_id": task_id,
            "artifact_id": artifact_id,
        })
    # 轨迹条目上限（防异常任务写爆任务行）
    _TRACE_MAX_ENTRIES = 500

    # 轨迹追加互斥锁：并发 LLM 调用（资产分析/分集拆解/画面描述）的归档
    # 条目同时读-改-写同一任务行的 JSON 列，无锁会互相覆盖（丢失更新）
    _trace_lock = threading.Lock()

    def _append_trace(self, task_id: str, project_id: str, node_id: str,
                      kind: str, title: str, content: str = "",
                      progress: float | None = None) -> None:
        """追加过程轨迹：持久化到任务行（JSON 列表，截断保护）+ WS 实时推送。"""
        entry = {"kind": kind, "title": title,
                 "content": (content or "")[:600], "at": _now()}
        # 读-改-写全程持锁：并发归档各自基于旧基线追加会 last-writer-wins 丢条目
        with self._trace_lock:
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                if not task:
                    return
                trace = dict(task.trace_json or {})
                if kind == "stage":
                    stages = list(trace.get("stages") or [])
                    stages.append({"label": title, "progress": progress, "at": entry["at"]})
                    trace["stages"] = stages[-self._TRACE_MAX_ENTRIES:]
                elif kind == "gate":
                    # 门禁事件桶（批次 1 §31.6）：确定性门禁触发的结构化统计
                    # 数据源（哪道门最常响 → 改提示词措辞），与思考轨迹分离
                    gates = list(trace.get("gates") or [])
                    gates.append({"gate": title, "content": entry["content"],
                                  "at": entry["at"]})
                    trace["gates"] = gates[-self._TRACE_MAX_ENTRIES:]
                else:
                    thoughts = list(trace.get("thoughts") or [])
                    thoughts.append({"title": title, "content": entry["content"],
                                     "at": entry["at"]})
                    trace["thoughts"] = thoughts[-self._TRACE_MAX_ENTRIES:]
                task.trace_json = trace
                db.commit()
        self._publish(project_id, "task_trace", {
            "task_id": task_id, "node_id": node_id, **entry,
        })

    def _set_generation_trace(self, task_id: str, project_id: str, node_id: str,
                              payload: dict) -> None:
        """生成参数结构化记录：写 trace_json["generation"] 键 + WS 推送。

        独立键而非 thoughts 条目——首帧/视频 prompt 常超 600 字（thoughts
        条目截断），且需要稳定结构供前端「生成参数」弹窗按字段渲染。
        """
        record = {**payload, "at": _now()}
        with self._trace_lock:
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                if not task:
                    return
                trace = dict(task.trace_json or {})
                trace["generation"] = record
                task.trace_json = trace
                db.commit()
        self._publish(project_id, "task_trace", {
            "task_id": task_id, "node_id": node_id,
            "kind": "generation", "payload": record,
        })

    def _stamp_generation_result(self, task_id: str, payload: dict) -> None:
        """把**结果侧**字段补进 `trace_json["generation"]["result"]`（25 号 2.1）。

        为什么需要：`generation` 记录原先只是**请求快照**（prompt/params/refs），
        没有"这次成没成、花了多久、按什么类别失败"——于是抽卡率只能靠 Task 表
        join 反推，视频侧种子还得翻 params。**请求与结果分开写、合并进同一个键**：
        既有字段一个不动（前端「查看参数」按字段渲染，增量安全）。
        无 generation 记录的 runner（parse/audit 等）静默跳过。
        """
        with self._trace_lock:
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                if not task:
                    return
                trace = dict(task.trace_json or {})
                gen = dict(trace.get("generation") or {})
                if not gen:
                    return
                gen["result"] = {**payload, "at": _now()}
                trace["generation"] = gen
                task.trace_json = trace
                db.commit()

    def _append_version(self, db, project_id: str, node_id: str,
                        params: dict, input_refs: dict, source: str,
                        artifact_id: str | None, task_id: str | None) -> str:
        """追加抽卡版本：生成/上传/编辑共用。返回版本 ID。

        顺带把**产物的权限归属**补齐（2026-09-30 两层模型）：产物表没有项目外键，
        自己推不出归属；而每个产物都必然被某个版本引用，所以在这里按版本的项目
        作为兜底来源最省事——上传/编辑/抽帧/合成那些写入点不必各自处理。
        """
        if artifact_id:
            _asset = db.get(Asset, artifact_id)
            access.set_owner_from_project(db, _asset, project_id)
        refs: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, list):
                for v in value:
                    collect(v)
            elif isinstance(value, str) and len(value) == 32:
                refs.append(value)

        collect(list((input_refs or {}).values()))
        # 版本号分配带唯一约束重试（审计 13 号：并发落版本会撞号，
        # uq_node_version_seq 让撞号从「静默错乱」变成「可重试的冲突」）
        for _attempt in range(3):
            last = db.scalars(
                select(NodeVersion.seq)
                .where(NodeVersion.node_id == node_id,
                       NodeVersion.project_id == project_id)
                .order_by(NodeVersion.seq.desc())
                .limit(1)
            ).first()
            version = NodeVersion(
                node_id=node_id, project_id=project_id, seq=(last or 0) + 1,
                source=source, prompt=str((params or {}).get("prompt", "")),
                refs_json=refs, task_id=task_id, artifact_id=artifact_id,
            )
            db.add(version)
            try:
                db.flush()
                return version.id
            except IntegrityError:
                db.rollback()
                logger.warning("版本号冲突，重试分配 node=%s（第 %s 次）",
                               node_id, _attempt + 1)
        raise RunnerError(f"版本号分配冲突（{node_id}）：请稍后重试")

    def _resolve_refs(self, input_refs: dict,
                      missing_out: list[str] | None = None) -> dict:
        """把 input_refs 中的 artifact_id 解析为文件路径（支持单值与列表）。

        失效引用不再静默丢弃（审计 13 号 K-中）：产物被清理/路径变更时，旧实现
        把该项置 None 而列表形态仍保留、消费端再过滤掉 None——精修成片会静默
        少镜头、首帧/参考图缺失会静默降级为纯文生图，用户只见「一致性变差」。
        传入 missing_out 时收集失效 id 交由调用方报错（不在此抛——submit 侧
        要先把任务落库，失败才可被前端看见）。
        """
        missing: list[str] = []

        def resolve(value: Any) -> Any:
            if isinstance(value, list):
                return [resolve(v) for v in value]
            if isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value):
                with SessionLocal() as db:
                    asset = db.get(Asset, value)
                    if asset and asset.path and Path(asset.path).exists():
                        return asset.path
                missing.append(value)
                return None
            return value

        resolved = {}
        for key, value in input_refs.items():
            v = resolve(value)
            if v is None or (isinstance(v, list) and not v):
                continue
            # 列表内含失效项：丢弃该键（执行侧会据 missing 报错），不下发半截引用
            if isinstance(v, list) and any(x is None for x in v):
                continue
            resolved[key] = v
        if missing_out is not None:
            missing_out.extend(missing)
        return resolved

    def _save_artifact(self, task_id: str, result, project_id: str = "") -> str:
        kind = result.kind
        if kind == "json":
            import json

            path = artifact_dir_for(project_id) / f"{task_id}.json"
            path.write_text(json.dumps(result.meta, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            files = [str(path)]
            file_kind = "json"
        elif kind == "image":
            files = result.meta.get("paths", [])
            file_kind = "image"
        else:
            files = [result.meta.get("path", "")]
            file_kind = kind

        with SessionLocal() as db:
            # 种子与**是否真的生效**一并入产物 meta（25 号 2.2）：种子原先只落在
            # generation 记录的 params 里，产物本身不知道自己是哪个种子出的。更要紧
            # 的是 17 号实测——ark/Seedream 端点**静默忽略 seed 且返回 200**，只记
            # seed 不记"是否生效"会造出**假的可复现性**（用户按固定种子重试，结果
            # 根本不一样）。判据取端点能力表，不猜。
            meta = {"files": files, **result.meta}
            _task = db.get(Task, task_id)
            _params = getattr(_task, "params_json", None) or {}
            if isinstance(_params, dict) and _params.get("seed") is not None:
                from app.core.providers import provider_seed_effective

                meta["seed"] = _params["seed"]
                meta["seed_effective"] = provider_seed_effective(
                    getattr(_task, "runner", "") or "",
                    getattr(_task, "model_id", "") or "")
            # **一次抽 N 张：每张各落一个产物**（原先只落 files[0]，多抽的候选
            # 直接丢掉——用户「一次抽 3 张挑一张」就无处可挑）。返回第一张的 id
            # 保持调用方兼容，其余 id 记在 meta 里供调用方补落版本。
            artifact_ids: list[str] = []
            for _f in (files or [""]):
                asset = Asset(kind=file_kind, path=_f, meta_json=meta)
                # 权限归属（2026-09-30 两层模型）：产物表既没有 project_id 也没有
                # 父级外键，before_flush 钩子推导不出来；这里由落库方直接带出
                # （它的 project_id 就是入参）。不填的话该产物会退化成「仅管理员可见」。
                access.set_owner_from_project(db, asset, project_id)
                db.add(asset)
                db.flush()
                artifact_ids.append(asset.id)
            artifact_id = artifact_ids[0] if artifact_ids else ""
            db.commit()
        if len(artifact_ids) > 1:
            # 其余候选的产物 id 回写进 meta（同一事务外再写一次，避免拿着未提交
            # 的 id 出去）——调用方据此补落版本
            with SessionLocal() as db:
                _a = db.get(Asset, artifact_id)
                if _a is not None:
                    _m = dict(_a.meta_json or {})
                    _m["artifact_ids"] = artifact_ids
                    _a.meta_json = _m
                    db.commit()
        return artifact_id

    async def _pump_loading_progress(self, task_id: str, project_id: str,
                                     model_id: str) -> None:
        """加载模型阶段进度泵：按内存/显存增量 / 权重体积估算加载比例，每秒推送。

        from_pretrained 无原生进度回调。分子取「进程 RSS 增量」与「显存分配
        增量」的较大者：CPU 常驻加载路径 RSS 线性增长；HiDream 流式加载
        （device_map=cuda）权重不驻留 RSS、显存分配线性增长。分母为模型
        目录实况体积，映射到 2%-95%（余量留给转档/落位阶段）。
        psutil 缺失时退化为按时间线性推进的时间估算。
        任务结束时由调用方 cancel，本协程不自行退出。
        """
        weight_mb = _dir_size_mb(settings.models_dir / model_id)
        if weight_mb <= 0:
            return
        weight_bytes = weight_mb * 1024 * 1024
        try:
            import psutil

            proc = psutil.Process()
            base_rss = proc.memory_info().rss
        except ImportError:
            proc = None
        base_vram = _vram_allocated_bytes()
        started = time.time()
        while True:
            await asyncio.sleep(_LOAD_POLL_SEC)
            # 任务取消后提前退出（终态吸收保证状态不被覆写，此处仅省掉无谓轮询）
            ev = self._cancel_events.get(task_id)
            if ev is not None and ev.is_set():
                break
            rss_delta = (proc.memory_info().rss - base_rss) if proc else 0
            vram_delta = _vram_allocated_bytes() - base_vram
            progress_bytes = max(rss_delta, vram_delta, 0)
            if progress_bytes > 0:
                pct = min(_LOAD_PCT_CAP, max(_LOAD_PCT_FLOOR, progress_bytes / weight_bytes))
            else:
                # 两种增量都读不到（无 psutil 等）：时间线性估算兜底
                pct = min(_LOAD_PCT_CAP,
                          _LOAD_FALLBACK_RATE * (time.time() - started) + _LOAD_PCT_FLOOR)
            self._set_status(task_id, "loading_model", progress=round(pct, 3))
            self._publish(project_id, "task_update", self._task_dict(task_id))

    def _set_status(self, task_id: str, status: str, *, progress: float | None = None,
                    artifact_id: str | None = None, finished: bool = False,
                    started: bool = False, error: str | None = None,
                    timings: dict | None = None) -> None:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task:
                return
            # 终态吸收：已终态的任务不再覆写（取消后加载进度泵/runner 收尾不得复活状态）
            if task.status in TERMINAL_STATUSES:
                return
            task.status = status
            if progress is not None:
                task.progress = progress
            if artifact_id:
                task.artifact_id = artifact_id
            if error is not None:
                # 强制 str（SQLite 列类型不严格——非 str 值会让前端/API 消费
                # 方在下标或拼接处炸出难查的 TypeError）
                task.error = error if isinstance(error, str) else json.dumps(
                    error, ensure_ascii=False, default=str)
            if started:
                task.started_at = _now()
            if finished:
                task.finished_at = _now()
            if timings:
                task.timings_json = timings
            db.commit()

    def _breaker_open(self, key: str) -> bool:
        """熔断判定（25 号 1.7）：窗口内同 `runner::model` 基础设施失败达阈值 → 停自动重试。

        为什么必须有：故障期成批任务各自退避重试，会变成「自制的 DoS」——
        在供应商已经不稳的时候继续加压。熔断只停**自动**重试，用户手动重摇不受限。
        """
        now = time.time()
        hits = [t for t in self._infra_fails.get(key, [])
                if now - t < _BREAKER_WINDOW_SEC]
        self._infra_fails[key] = hits
        return len(hits) >= _BREAKER_THRESHOLD

    def _fail_or_retry(self, task_id: str, error: str, fail_class: str = "") -> None:
        """可重试类失败 → **延时重新入队**；否则落终态（25 号批次 1.2 + 1.7）。

        判据是 `fail_class`（在抛出点确定，见 `providers.FailClass`）——不猜错误
        文本。**只重试同一端点、原样参数**：不做端点切换（12 号用户决策：自动切换
        的成本不可控），换端点仍只给中文指引。

        **重试只能有一个 owner**（1.7）：HTTP 层已不再自行重试（原先同步 sleep
        20/40/60s，既占死泳道、又与这里叠乘），LLM 的 3 档阶梯是「换参数降级」、
        轮询 GET 是幂等短重试——原样重试只此一处。

        延时用 `create_task` 而不是就地 sleep（**不占泳道**）；期间任务回到 `queued`，
        若此时进程重启，1.1 的重启恢复会把它重新入队（计数在 params 里，不会归零）。
        """
        error_out = error
        if fail_class in _AUTO_RETRY_CLASSES:
            with SessionLocal() as db:
                t = db.get(Task, task_id)
                if t is None:
                    return
                # 终态吸收（2026-09-30 复查 R5）：任务已被看门狗判死（failed）时，
                # 迟到的「可重试失败」不得把它改回 queued 重新跑一遍——否则
                # 「卡死判死」与「自动重试」叠加成重复付费。
                if t.status in TERMINAL_STATUSES:
                    logger.info("任务 %s 已是终态 %s，自动重试被吸收",
                                task_id, t.status)
                    return
                params = dict(t.params_json or {})
                n = int(params.get(_AUTO_RETRY_KEY) or 0)
                lane_runner, project_id = t.runner, t.project_id
                key = f"{t.runner}::{t.model_id}"
                self._infra_fails.setdefault(key, []).append(time.time())
                tripped = self._breaker_open(key)
                if n < _AUTO_RETRY_MAX and not tripped:
                    params[_AUTO_RETRY_KEY] = n + 1
                    t.params_json = params
                    t.status = "queued"
                    t.error = ""
                    t.started_at = None
                    db.commit()
                    retrying = True
                else:
                    retrying = False
                    if tripped:
                        error_out = (
                            f"{error}\n[熔断]同端点（{key}）基础设施失败过密，"
                            f"已暂停自动重试（{_BREAKER_WINDOW_SEC // 60} 分钟内 "
                            f"{_BREAKER_THRESHOLD} 次）——请稍后手动重试")
            if retrying:
                delay = _AUTO_RETRY_BACKOFF_SEC[min(n, len(_AUTO_RETRY_BACKOFF_SEC) - 1)]
                logger.warning(
                    "可重试失败（%s），%s 秒后自动重试第 %s 次 task=%s：%s",
                    fail_class, delay, n + 1, task_id, error[:160])
                self._publish(project_id, "task_update", self._task_dict(task_id))
                self._spawn_requeue(task_id, lane_runner, float(delay))
                return
        self._fail(task_id, error_out, fail_class)

    async def _delayed_requeue(self, task_id: str, runner_name: str,
                               delay: float) -> None:
        """退避到点后把任务放回泳道（让出泳道等，不阻塞 worker）。"""
        try:
            await asyncio.sleep(delay)
            q = self._gpu_queue if runner_name in GPU_RUNNERS else self._fast_queue
            q.put_nowait(task_id)
        except Exception as e:  # noqa: BLE001 兜底：延迟入队失败不能拖垮泳道
            logger.exception("自动重试入队失败 task=%s：%s", task_id, e)
            self._fail(task_id, f"自动重试入队失败：{e}")

    def _spawn_requeue(self, task_id: str, runner_name: str,
                       delay: float) -> None:
        """调起「延时重新入队」，并**保住强引用**。

        与 WS 推送同一坑（见 `_pending_publish_tasks` 注释）：create_task 返回的
        任务若无强引用，可能被事件循环 GC 掉 —— 那样任务会永远停在 `queued`
        （前端一直「排队中」，批次进度条不结束），而 `sweep_stale_tasks` 只清
        loading_model/running，不会救它（25 号复查 R6）。
        """
        task = asyncio.create_task(self._delayed_requeue(task_id, runner_name,
                                                         delay))
        self._pending_requeue_tasks.add(task)
        task.add_done_callback(self._pending_requeue_tasks.discard)

    def _fail(self, task_id: str, error: str, fail_class: str = "") -> None:
        """任务失败统一出口：落库 + WS 推送 + 文件日志（便于事后定位）。

        fail_class：基础设施/平台策略类失败类别（providers.FailClass）——写入
        错误尾注并对同镜的 pending take 打标，使「故障类失败不占用户重摇预算」
        （网络抖动/限流/内容安全误杀都不是「这一镜拍得不好」）。

        终态吸收（审计 13 号 D）：已取消/已完成的任务不再被改写成 failed——
        用户取消后 runner 若先抛非 RunnerError（provider 超时/JSON 错/管线
        RuntimeError），旧实现会把终态覆盖成「失败 + traceback」，前端还会
        把用户主动取消算作失败并跳过下游节点。
        """
        # 结果侧记录（25 号 2.1）：失败的类别与截断原因一并入 generation 记录
        self._stamp_generation_result(task_id, {
            "ok": False, "fail_class": fail_class or "unknown",
            "error": (error or "")[:300]})
        if fail_class:
            error = f"{error}\n[fail_class={fail_class}]"
        logger.error("任务失败 task=%s: %s", task_id, error)
        txn_id = ""
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if not task:
                return
            if task.status in TERMINAL_STATUSES and task.status != "failed":
                logger.info("任务 %s 已是终态 %s，失败写入被吸收（不覆盖）",
                            task_id, task.status)
                return
            task.status = "failed"
            task.error = error if isinstance(error, str) else str(error)
            task.finished_at = _now()
            txn_id = task.billing_txn_id or ""
            # 基础设施类失败：给该镜最近的 pending take 打标（预算判定据此豁免）
            if fail_class:
                self._mark_infra_take(db, task, fail_class)
            db.commit()
            self._publish(task.project_id, "task_update", self._task_dict(task_id))
        # 计费（33 号）：失败是结算触发点（**放在终态吸收早退之后**——被吸收的
        # 不重复结算）。结算判定：整批无成功产物才退；单任务「远端已计费」不退。
        if txn_id:
            try:
                billing.settle_after_terminal(txn_id, fail_class=fail_class)
            except Exception:  # noqa: BLE001 退款失败不影响失败落库（对账兜底）
                logger.exception("任务失败结算异常（流水 %s）", txn_id)

    @staticmethod
    def _mark_infra_take(db, task, fail_class: str) -> None:  # noqa: ANN001
        """把「基础设施类失败」标到同镜最近一条 pending take 上（不新增 schema）。

        take 在提交成功时创建（verdict=pending/reroll），若该任务随后因故障
        失败，则把 `changed` 改写为 `infra:<类别>`——重摇预算查询只统计
        非 infra 的 reroll，故障因此不消耗用户预算。
        """
        from app.db.models import ShotReview

        node_id = str(task.node_id or "")
        if not node_id.startswith("shot_"):
            return  # 非镜头任务（compose/episode_*）无 take
        shot_id = node_id.split("_", 2)[1]
        row = db.scalars(
            select(ShotReview)
            .where(ShotReview.shot_id == shot_id,
                   ShotReview.verdict.in_(("pending", "reroll")))
            .order_by(ShotReview.created_at.desc())
            .limit(1)
        ).first()
        if row is not None and not str(row.changed or "").startswith("infra:"):
            row.changed = f"infra:{fail_class}"
            logger.info("镜 %s 的 take 标记为基础设施失败（%s），不占重摇预算",
                        shot_id[:8], fail_class)

    def _task_dict(self, task_id: str, include_trace: bool = False) -> dict:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            return (self._to_dict(task, include_trace) if task
                    else {"id": task_id})

    @staticmethod
    def _to_dict(t: Task, include_trace: bool = False) -> dict:
        return {
            "id": t.id, "project_id": t.project_id, "node_id": t.node_id,
            "runner": t.runner, "model_id": t.model_id,
            "status": t.status, "progress": t.progress,
            "artifact_id": t.artifact_id, "version_id": t.version_id,
            "batch_id": t.batch_id,
            "error": t.error,
            # params 不下发（前端 TaskInfo 无此字段、无消费端）：解析任务的
            # params 里带着整篇剧本（实测单条 186KB），它会随每个 task_update
            # WS 事件与每次 list_tasks（200 条）一起发出去——用户实测浏览器
            # 标签在长解析中崩溃，这类"没人读却反复传的大字段"是首要清理对象。
            # 需要参数请走 /shots/{id}/generation-params（按需查库，单镜粒度）。
            "timings": t.timings_json,
            # 原始引用（artifact id 列表，顺序即参考图编号）——排查生成质量
            # 时与提示词「参考图N」标注逐一对账
            "input_refs": t.input_refs_json or {},
            # 完整轨迹按需返回（审计 13 号：列表接口 2s 轮询最长 30 分钟，
            # 单任务 trace 可达 500 条×600 字 → 单次响应数十 MB；WS 事件同理
            # 会用大 payload 挤爆订阅队列导致终态丢失）。列表/事件给摘要，
            # 任务详情接口 get_task 给完整轨迹。
            **({"trace": t.trace_json or {}} if include_trace else
               {"trace_summary": _trace_summary(t.trace_json or {})}),
            "created_at": t.created_at, "started_at": t.started_at,
            "finished_at": t.finished_at,
        }

    # WS 推送任务的强引用集：create_task 无强引用会被事件循环 GC 中途取消（丢消息）
    _pending_publish_tasks: set = set()
    # 延时重新入队任务的强引用集（同上：被 GC 掉的话任务永远停在 queued）
    _pending_requeue_tasks: set = set()

    def _publish(self, project_id: str, event: str, data: dict) -> None:
        """WS 推送入口（线程安全）：事件循环内直接调度，runner 线程跨线程投递。

        此前两个静默丢消息的坑：① executor 线程里 get_running_loop() 抛
        RuntimeError 被吞（推理步进进度全部丢失）；② create_task 返回的
        任务无强引用，可能被事件循环 GC 中途取消。均在此修复。
        """
        coro = bus.publish(project_id, event, data)
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            task = running.create_task(coro)
        elif loop is not None and loop.is_running():
            # runner 线程：跨线程投递到主事件循环
            task = asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            return  # 事件循环尚未启动（启动早期），消息无从投递
        self._pending_publish_tasks.add(task)
        task.add_done_callback(self._pending_publish_tasks.discard)


def _inference_ok() -> bool:
    from app.runners.base import inference_available

    return inference_available()


def _preflight_model(runner_name: str, model_id: str) -> None:
    """提交预检：真实推理模式下 GPU 任务要求本地模型已安装，未安装直接报错不进队列。"""
    if runner_name not in GPU_RUNNERS:
        return
    if settings.mock_mode or not _inference_ok():
        return
    # 端点编码串任务：走第三方路径，不要求本地模型
    if is_endpoint_model(model_id):
        return
    # 对应 provider 类别已启用时不强制本地模型（ref2v 等视频 runner 共用 t2v provider）
    provider_kind = _RUNNER_PROVIDER_KIND.get(runner_name)
    if provider_kind and provider_enabled(provider_kind):
        return
    mid = model_id or ALL_RUNNERS[runner_name].default_model
    if not (settings.models_dir / mid).exists():
        raise RunnerError(f"模型未安装：{mid}，请先在模型管理中下载")
    # 管线就绪预检（25 号 1.4）：模型装了、但当前 diffusers 里没有对应的管线类时，
    # 原先要等任务**跑到加载阶段**才炸——实测 16 条「H3 diffusers 集成未就绪」，
    # 用户白等一轮才知道该换 provider/换模型。这里提前到**提交时**报同样的指引。
    # 复用 `pipeline_class` 这一处探针（不另写判据，免得两处口径漂移）。
    from app.inference.pool import pipeline_class  # 延迟导入：避免与推理层循环依赖

    pipeline_class(mid, runner_name)


engine = TaskEngine()
