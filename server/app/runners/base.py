"""Runner 基础设施。

每个 runner 对应一类节点的执行逻辑。统一接口：
  run(ctx, model_id, params, input_refs) -> 产物文件路径 + 元数据

支持两种执行模式：
- real: 调用 diffusers / ffmpeg / edge-tts（依赖就绪时）
- mock: 生成占位产物，跑通全链路（依赖缺失时自动回退，可在设置强制）
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.core.config import artifact_dir_for


@dataclass
class RunnerContext:
    task_id: str
    cancel_event: asyncio.Event
    publish_progress: Callable[[float, str], None]
    artifacts_dir: Path
    models_dir: Path
    mock: bool
    # 所属项目（产物落 `artifacts/{project_id}/` 用；空 = 落 _unassigned/）。
    # 由 task_engine 从 Task.project_id 填入——runner 自己不该去查库。
    project_id: str = ""
    # 任务的原始模型选择（未解析默认值，空 = 未指定）：
    # provider 分发判定以此为准（空/第三方模型 → 第三方优先；本地模型 → 本地）
    raw_model_id: str = ""
    # 过程轨迹回调（kind, title, content）：持久化到任务 + WS 实时推送（可观测）
    publish_trace: Callable[[str, str, str], None] | None = None
    # 流式思考回调（kind: begin|delta|end, text）：仅 WS 实时推送（不落库）
    publish_stream: Callable[[str, str], None] | None = None
    # 生成参数回调（payload dict）：结构化记录最终发给模型的参数
    # （trace_json.generation 键，不受 thoughts 条目 600 字截断）——排查生成
    # 质量时核对「模型实际收到什么」的唯一权威记录
    publish_generation: Callable[[dict], None] | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class RunnerResult:
    ok: bool
    file_path: str = ""
    kind: str = ""  # image|video|audio|json
    meta: dict = field(default_factory=dict)
    error: str = ""
    # 失败类别（见 providers.FailClass）：由 runner 在**捕获到分类过的异常**时
    # 透传（如 ScriptParseError.fail_class）。引擎据它决定「可重试类自动重试」——
    # 缺了它，解析/审核/资产这些任务撞上瞬时限流只会硬失败（25 号复查 R2：
    # HTTP 层的同步退避删掉后，任务级重试必须真的接上）
    fail_class: str = ""


class RunnerError(RuntimeError):
    """runner 执行失败。

    fail_class：基础设施/平台策略类失败类别（见 providers.FailClass）——透传到
    任务与镜头日志，用于「故障类失败不占用户重摇预算」的判定。空串 = 未知/质量类。
    """

    def __init__(self, message: str, fail_class: str = "") -> None:
        super().__init__(message)
        self.fail_class = fail_class


class BaseRunner:
    name: str = ""
    default_model: str = ""
    allowed_models: list[str] = []

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        raise NotImplementedError

    # -- 公共工具 --
    @staticmethod
    def check_cancel(ctx: RunnerContext) -> None:
        if ctx.cancel_event.is_set():
            raise RunnerError("任务已取消")

    @staticmethod
    def artifact_dir(ctx: RunnerContext) -> Path:
        """本项目产物目录（不存在则建）：`artifacts/{project_id}/`。"""
        return artifact_dir_for(ctx.project_id)

    @staticmethod
    def artifact_path(ctx: RunnerContext, ext: str) -> Path:
        """产物路径：`artifacts/{project_id}/{task_id}.{ext}`。

        文件名规则不变（仍是任务 UUID + 后缀），只是从根目录平铺改为**按项目
        分目录**——见 `core/config.artifact_dir_for` 的说明。
        """
        return BaseRunner.artifact_dir(ctx) / f"{ctx.task_id}.{ext}"

    @staticmethod
    def attach_step_progress(ctx: RunnerContext, pipe, kwargs: dict,
                             base: float = 0.0, frac: float = 1.0) -> None:
        """diffusers 管线步级进度接入（按调度器总步数折算，base/frac 定位分段区间）。

        同时接入取消检查：回调内抛 RunnerError 会中止管线（diffusers 不吞
        回调异常），任务在 _execute 统一按取消落态。管线不支持
        callback_on_step_end 时静默跳过（仅失去步级进度，不影响生成）。
        """
        import inspect

        try:
            sig_params = inspect.signature(pipe.__call__).parameters
        except (TypeError, ValueError):
            return
        if "callback_on_step_end" not in sig_params:
            return

        def _on_step(_pipe, step: int, _t, cb_kwargs: dict) -> dict:
            if ctx.cancel_event.is_set():
                raise RunnerError("任务已取消")
            timesteps = getattr(_pipe.scheduler, "timesteps", None)
            total = len(timesteps) if timesteps is not None else 1
            ctx.publish_progress(
                min(base + frac, base + frac * (step + 1) / max(1, total)),
                f"推理 {step + 1}/{total} 步",
            )
            return cb_kwargs

        kwargs["callback_on_step_end"] = _on_step

    def resolve_model(self, model_id: str) -> str:
        return model_id or self.default_model


def inference_available() -> bool:
    try:
        import diffusers  # noqa: F401
        import torch  # noqa: F401

        return torch.cuda.is_available()
    except ImportError:
        return False


def sleep_with_progress(ctx: RunnerContext, seconds: float, label: str = "") -> None:
    """mock 模式下模拟推理耗时，同时保持可取消。"""
    steps = max(1, int(seconds * 2))
    for i in range(steps):
        BaseRunner.check_cancel(ctx)
        time.sleep(min(0.5, seconds / steps))
        ctx.publish_progress((i + 1) / steps * 0.95, label)
