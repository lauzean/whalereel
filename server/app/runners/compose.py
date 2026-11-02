"""合成 runner：拼接 / 转场 / 导出（ffmpeg）。"""

import json
import re
import subprocess
from pathlib import Path

from app.core.config import settings
from app.runners.base import BaseRunner, RunnerContext, RunnerResult

# 导出封装格式白名单（2026-09-30 安全整改）：`format` 来自请求参数并被直接拼进
# 输出文件名 `f"{task_id}.{fmt}"`，旧实现不做任何校验——传
# `"mp4/../../../../app/main.py"` 就能借 ffmpeg 的 `-y` 覆盖任意文件（只要
# 目标目录存在）。现在只认这几个字面量，其余一律拒绝。
EXPORT_FORMATS: tuple[str, ...] = ("mp4", "mov", "webm", "mkv")
# 分辨率写法校验：只接受 `宽x高`（正整数），避免 split 抛 ValueError 变成
# 「内部错误 + traceback」这种用户看不懂的失败（顺带堵住注入 ffmpeg 参数）
_RESOLUTION_RE = re.compile(r"^\d{2,5}x\d{2,5}$")


def _ffmpeg_available() -> bool:
    import shutil

    return shutil.which(settings.ffmpeg_bin) is not None


def _as_list(value) -> list[str]:
    """把单值/None/列表统一为字符串列表。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


def _within_artifacts(path) -> bool:
    """输入文件是否落在产物目录内（2026-09-30 安全整改）。

    `clips` / `audio_tracks` / `source` 都是请求体直传的绝对路径，旧实现只判
    「文件存在」——任何登录用户都能把本机任意媒体文件（系统里的图片/音频/视频）
    交给 ffmpeg 转码后落进自己的产物目录再下载，等于一个任意文件读取原语。
    这里与媒体读取侧（`media_sign.is_within`）同一口径：只认产物目录内的文件。
    """
    from app.core import media_sign

    try:
        return media_sign.is_within(settings.artifacts_dir, Path(str(path)))
    except OSError:
        return False


class ComposeRunner(BaseRunner):
    """拼接时间线 / 转场 / 导出节点共用 runner。

    params:
      mode: concat(默认) | transition | export
      clips: [artifact_path, ...]           # concat/transition
      transition: fade|none                  # transition 模式
      audio_tracks: [artifact_path, ...]     # 可选音轨（配音/BGM）
      export: {format, resolution, bitrate}  # export 模式
    """

    name = "compose"
    default_model = "ffmpeg"
    allowed_models = ["ffmpeg"]

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        if not _ffmpeg_available():
            return RunnerResult(ok=False, error="未找到 ffmpeg，无法合成")
        mode = params.get("mode", "concat")
        # 端口输入注入：clips/videos（视频，可多）/ audio（音轨，可多）/ video（导出源）
        if not params.get("clips"):
            merged = (_as_list(input_refs.get("clips"))
                      + _as_list(input_refs.get("videos"))
                      + _as_list(input_refs.get("video")))
            if merged:
                params = {**params, "clips": merged}
        if not params.get("audio_tracks"):
            audio = _as_list(input_refs.get("audio"))
            if audio:
                params = {**params, "audio_tracks": audio}
        # 输入路径收口（2026-09-30）：三个入口都只认产物目录内的文件，越界即失败
        # （不静默丢弃：用户拿到明确原因，而不是「合成出来少了一段」）
        for key in ("clips", "audio_tracks", "source"):
            for p in _as_list(params.get(key)):
                if not _within_artifacts(p):
                    return RunnerResult(
                        ok=False,
                        error=f"合成输入不在产物目录内，拒绝执行：{str(p)[:120]}")
        if mode == "concat":
            return self._concat(ctx, params)
        if mode == "transition":
            return self._transition(ctx, params)
        if mode == "export":
            return self._export(ctx, params, input_refs)
        return RunnerResult(ok=False, error=f"未知合成模式：{mode}")

    # -- 拼接 --
    def _concat(self, ctx: RunnerContext, params: dict) -> RunnerResult:
        clips = _as_list(params.get("clips"))
        if not clips:
            return RunnerResult(ok=False, error="时间线上没有片段")
        missing = [c for c in clips if not Path(c).exists()]
        if missing:
            return RunnerResult(ok=False, error=f"片段文件缺失：{missing[:2]}")

        # 逐个转统一编码再 concat demuxer（避免异构流拼接失败）
        normalized: list[str] = []
        for i, clip in enumerate(clips):
            ctx.publish_progress(i / max(1, len(clips)) * 0.8, f"规范化片段 {i + 1}/{len(clips)}")
            norm = self.artifact_dir(ctx) / f"{ctx.task_id}_norm{i}.mp4"
            self._run_ffmpeg([
                "-y", "-loglevel", "error", "-i", clip,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-r", "24",
                "-video_track_timescale", "24000",
                str(norm),
            ])
            normalized.append(str(norm))

        list_file = self.artifact_dir(ctx) / f"{ctx.task_id}_list.txt"
        list_file.write_text(
            "\n".join(f"file '{p}'" for p in normalized), encoding="utf-8"
        )
        out = self.artifact_dir(ctx) / f"{ctx.task_id}.mp4"
        ctx.publish_progress(0.85, "拼接片段")
        self._run_ffmpeg([
            "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(out),
        ])

        # 可选混音（BGM/配音）
        audio_tracks = [t for t in (params.get("audio_tracks") or []) if Path(t).exists()]
        final_out = out
        if audio_tracks:
            ctx.publish_progress(0.92, "混入音轨")
            mixed = self.artifact_dir(ctx) / f"{ctx.task_id}_mixed.mp4"
            args = ["-y", "-loglevel", "error", "-i", str(out)]
            for t in audio_tracks:
                args += ["-stream_loop", "-1", "-i", t]
            n_in = 1 + len(audio_tracks)
            args += [
                "-filter_complex",
                f"amix=inputs={n_in}:duration=first:dropout_transition=2[a]",
                "-map", "0:v", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-shortest",
                str(mixed),
            ]
            self._run_ffmpeg(args)
            final_out = mixed

        ctx.publish_progress(1.0, "完成")
        return RunnerResult(ok=True, kind="video", meta={"path": str(final_out)})

    # -- 转场（两片段 + 效果）--
    def _transition(self, ctx: RunnerContext, params: dict) -> RunnerResult:
        clips = params.get("clips") or []
        if len(clips) != 2:
            return RunnerResult(ok=False, error="转场需要恰好两段视频")
        effect = params.get("transition", "fade")
        if effect == "none":
            return self._concat(ctx, {"clips": clips})
        a, b = clips
        if not (Path(a).exists() and Path(b).exists()):
            return RunnerResult(ok=False, error="片段文件缺失")
        out = self.artifact_dir(ctx) / f"{ctx.task_id}.mp4"
        # 简化实现：0.5s 交叉叠化
        self._run_ffmpeg([
            "-y", "-loglevel", "error",
            "-i", a, "-i", b,
            "-filter_complex",
            "[0:v][1:v]xfade=transition=fade:duration=0.5:offset=3[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            str(out),
        ])
        return RunnerResult(ok=True, kind="video", meta={"path": str(out)})

    # -- 导出 --
    def _export(self, ctx: RunnerContext, params: dict, input_refs: dict) -> RunnerResult:
        src = params.get("source") or input_refs.get("video") or ""
        if not src or not Path(src).exists():
            return RunnerResult(ok=False, error="导出源不存在")
        export = params.get("export", {})
        fmt = str(export.get("format", "mp4") or "mp4").strip().lower().lstrip(".")
        if fmt not in EXPORT_FORMATS:
            # 不静默降级为 mp4：用户明确选了别的封装，悄悄换掉比报错更糟
            return RunnerResult(
                ok=False,
                error=f"不支持的导出格式：{fmt}（可选 "
                      + "/".join(EXPORT_FORMATS) + "）")
        resolution = str(export.get("resolution", "source") or "source")
        out = self.artifact_dir(ctx) / f"{ctx.task_id}.{fmt}"
        args = ["-y", "-loglevel", "error", "-i", src]
        if resolution != "source":
            if not _RESOLUTION_RE.match(resolution):
                return RunnerResult(
                    ok=False,
                    error=f"分辨率写法无效：{resolution}（应为 宽x高，如 1080x1920）")
            w, h = resolution.split("x")
            args += ["-vf", f"scale={w}:{h}"]
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", str(out)]
        ctx.publish_progress(0.5, "导出成片")
        self._run_ffmpeg(args)
        ctx.publish_progress(1.0, "导出完成")
        return RunnerResult(ok=True, kind="video", meta={"path": str(out), "exported": True})

    @staticmethod
    def _run_ffmpeg(args: list[str], timeout: int = 600) -> None:
        cmd = [settings.ffmpeg_bin] + args
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 失败：{result.stderr[-500:]}")


class StoryboardRunner(BaseRunner):
    """分镜板：批量首帧/批量转视频的编排由前端拆为多个 t2i/i2v 任务，
    此 runner 仅负责 shots 数据透传（M1 由任务引擎批量派发替代）。"""

    name = "storyboard"
    default_model = ""
    allowed_models = []

    def run(self, ctx: RunnerContext, model_id: str, params: dict,
            input_refs: dict) -> RunnerResult:
        shots = params.get("shots") or input_refs.get("shots") or []
        return RunnerResult(ok=True, kind="json", meta={"shots": shots})


RUNNERS_EXTRA: dict[str, BaseRunner] = {
    r.name: r for r in (ComposeRunner(), StoryboardRunner())
}
