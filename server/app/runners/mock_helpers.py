"""mock 产物生成：SVG 图片 / WAV 音频 / ffmpeg lavfi 视频。

依赖就绪时尽量走真实实现；mock 仅用于无 GPU 环境跑通产品链路。
"""

import math
import struct
import wave
from pathlib import Path

from app.core.config import settings


def _ffmpeg_available() -> bool:
    import shutil

    return shutil.which(settings.ffmpeg_bin) is not None


def mock_image(path: Path, text: str, width: int = 1280, height: int = 720) -> Path:
    """生成占位 SVG 图（首帧/定妆图 mock）。"""
    safe = (text or "未命名画面")[:120].replace("&", "&amp;").replace("<", "&lt;")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#1a1d2e"/>
      <stop offset="100%" stop-color="#2d3561"/>
    </linearGradient>
  </defs>
  <rect width="100%" height="100%" fill="url(#bg)"/>
  <rect x="24" y="24" width="{width - 48}" height="{height - 48}" fill="none"
        stroke="#5b8cff" stroke-width="2" stroke-dasharray="12 6" rx="12"/>
  <text x="50%" y="46%" fill="#8fa3d0" font-size="28" text-anchor="middle"
        font-family="system-ui, sans-serif">[MOCK 图像]</text>
  <foreignObject x="80" y="{height * 0.55}" width="{width - 160}" height="{height * 0.35}">
    <div xmlns="http://www.w3.org/1999/xhtml" style="color:#dbe4ff;font-size:20px;
      font-family:system-ui,sans-serif;word-wrap:break-word;">{safe}</div>
  </foreignObject>
</svg>"""
    path = path.with_suffix(".svg")
    path.write_text(svg, encoding="utf-8")
    return path


def mock_audio(path: Path, text: str) -> Path:
    """生成占位 WAV（配音 mock）：时长随文本长度 2-10s 的提示音。"""
    duration = min(10.0, max(2.0, len(text or "") * 0.12))
    sample_rate = 16000
    n_samples = int(duration * sample_rate)
    path = path.with_suffix(".wav")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # 三连提示音，间距随文本长度
        for i in range(n_samples):
            t = i / sample_rate
            beep = math.sin(2 * math.pi * 660 * t) * 0.4 if (t % 1.0) < 0.25 else 0.0
            fade = min(1.0, t * 4, (duration - t) * 4)
            value = int(beep * fade * 32767)
            wf.writeframes(struct.pack("<h", value))
    return path


def mock_video(path: Path, text: str, duration: int = 5, width: int = 1280,
               height: int = 720, fps: int = 24) -> Path:
    """ffmpeg lavfi 生成占位视频（彩条测试源）。"""
    if not _ffmpeg_available():
        raise RuntimeError("mock 视频需要 ffmpeg，请安装或将 AVS_FFMPEG 指向可执行文件")
    import subprocess

    path = path.with_suffix(".mp4")
    cmd = [
        settings.ffmpeg_bin, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i",
        f"testsrc2=duration={duration}:size={width}x{height}:rate={fps}",
        "-pix_fmt", "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    return path
