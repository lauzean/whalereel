# -*- coding: utf-8 -*-
"""上传读取收口：带上限的分块读取（2026-09-30 安全整改）。

**为什么单独有这一步**：全项目有四个文件上传入口（画布节点上传、剧本文件、
音色参考音频、角色形象图），此前只有画布节点做了上限，其余三处都是
`await file.read()`——**先整份读进来、再判断大小**。上限在读完之前拦不住任何
东西：一个请求就能把内存/磁盘吃满（云端多用户下等于免费的 DoS）。

这里给出一份共用实现，**新上传入口一律走它**；已有的三处也统一改过来，
避免「一处判定散落多处」的老毛病（审计 14 号：6 条严重缺陷都是这个形态）。
"""

import os

from fastapi import HTTPException

# 分块读取粒度：边读边累计，超限立刻返回，不把超限文件整份读进内存
_UPLOAD_PROBE_CHUNK = 1024 * 1024


def env_max_bytes() -> int:
    """默认单次上传上限（`AVS_UPLOAD_MAX_MB`，默认 512MB）。

    512MB 覆盖短剧素材（4K 短视频片段也在内）；专项入口（音色参考 20MB、
    角色形象图 20MB）在调用处传更小的值。
    """
    return int(os.environ.get("AVS_UPLOAD_MAX_MB", "512")) * 1024 * 1024


async def read_upload_capped(file, max_bytes: int | None = None, *,
                             too_large_message: str = "") -> bytes:
    """分块读上传内容；超过上限立即 413（不把超限内容整份读进内存）。

    `too_large_message` 非空时用调用方的话术（专项入口的限额提示要写清
    「音频/图片」与各自的 MB 数，不能给用户看一个笼统的数字）。
    """
    limit = max_bytes or env_max_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_PROBE_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            detail = too_large_message or (
                f"文件超过上限 {limit // (1024 * 1024)}MB"
                "（可用环境变量 AVS_UPLOAD_MAX_MB 调整）")
            raise HTTPException(413, detail)
        chunks.append(chunk)
    return b"".join(chunks)
