# -*- coding: utf-8 -*-
"""提示词「系统块」的剥离（2026-09-30 用户规则：组装结果写回提示词框后，下游取用先剥干净）。

背景：场景/道具的提示词框现在存的是**组装后的完整提示词**——画风前缀 → 空镜约束
→ 空间布局 → 画面描述 → 风格负向词（界面所见即所发，见 `drama.compose_scene_prompt`
与 `drama.compose_prop_prompt`）。但有三类下游要的是**干净描述**，系统块混进去会出问题：

1. **分镜首帧取场景文本**——「没有任何生物出现」这类空镜约束会压住本镜出场人物；
2. **场景补充视角的 LLM 重写输入**——不该看到系统样板句；
3. **「AI 优化提示词」的当前值**——把系统块交给 LLM 重写，会被改写或复述成两份。

剥离依据全部是**我们自己写过的已知文本**（精确字符串 / 固定前缀），不做语义猜测；
用户自己写的「避免某物」等措辞不在已知集合里，不会被误删。
"""

from __future__ import annotations

import re

from app.agent import prompts as prompts_mod
from app.script.persist import load_style_presets

# 「空间布局：…；」整句（我们自己的注入格式）。主剥离通道是按当前 layout 精确整串
# 替换；正则是兜底——布局值改过、旧句残留在文本里时也能剥掉。
_SCENE_LAYOUT_RE = re.compile(r"[，,]?\s*空间布局：[^；;]*[；;]?")


def _strip_segment(text: str, segment: str) -> str:
    """精确删段：连同分隔符与**拼接去尾标点后的变体**一起匹配。

    为什么要变体：拼装走 `_join_parts`（会去掉每段尾部的「。，；」等标点），
    常量原文「…。」进文本后变成「…」——只按原文匹配会残留（实测：场景二次
    组装时空镜约束没剥掉，系统块顺序漂移，幂等断言当场抓到）。
    """
    if not segment:
        return text
    out = text
    for seg in {segment, segment.rstrip("。，,、！!？?；;")}:
        if not seg:
            continue
        out = out.replace(f"，{seg}", "").replace(f"、{seg}", "")
        out = out.replace(seg, "")
    return out


def _tidy(text: str) -> str:
    """清理剥离留下的重复分隔符与首尾标点。"""
    out = re.sub(r"[，,]{2,}", "，", text or "")
    out = re.sub(r"[，,]([。；;、])", r"\1", out)
    return out.strip().strip("，,；;、").strip()


def strip_known_style_negative(text: str) -> str:
    """删掉文本里**系统写过的**「避免：…」段（四种画风 × 三类的已知文本，精确匹配）。

    精确匹配的理由：用户可能在文本里自己写「避免…」措辞，正则截断会把用户内容删掉。
    """
    out = text or ""
    for preset in (load_style_presets() or {}).values():
        for seg in (preset.get("negatives") or {}).values():
            if seg:
                out = _strip_segment(out, f"避免：{seg}")
    return out


def strip_scene_system_blocks(text: str, layout: str = "",
                              layout_alt: str = "") -> str:
    """剥掉场景提示词里的系统块（空镜约束 / 空间布局 / 风格负向词）→ 干净描述。

    `layout_alt`（2026-10-02）：布局清洗（人物走位剔除）落地后，注入版本与库里的
    原值可能不同（老文本注入的是清洗前旧句、新文本注入清洗后）——两版当针**都在
    通用正则之前**试完整剥离；顺序不能反：先跑通用正则 `_SCENE_LAYOUT_RE` 会在
    旧针没中时把「空间布局：<到第一个分号>」**半段吃掉**，尾巴残留进底文，
    另一版针就再也没得剥了（门禁当场抓到过这条迁移不幂等）。
    """
    out = strip_known_style_negative(text or "")
    out = _strip_segment(out, prompts_mod.SCENE_IMAGE_CONSTRAINTS)
    for needle in (layout, layout_alt):
        if needle:
            out = _strip_segment(out, f"空间布局：{needle}")
    out = _SCENE_LAYOUT_RE.sub("", out)
    return _tidy(out)


def strip_prop_system_blocks(text: str) -> str:
    """剥掉道具提示词里的系统块（目前只有风格负向词）→ 干净描述。"""
    return _tidy(strip_known_style_negative(text or ""))


def strip_leading_style_keywords(text: str, keywords: str) -> str:
    """剥掉段首画风前缀（写回后的字段以它开头；关键词为空时原样返回）。"""
    t = (text or "").lstrip()
    if keywords and t.startswith(keywords):
        t = t[len(keywords):].lstrip("，,；;、 ")
    return t


def strip_character_system_blocks(text: str, keywords: str = "") -> str:
    """剥掉角色「生图提示词」里的系统块（画风前缀 / 风格负向词）→ 干净描述。"""
    out = strip_known_style_negative(text or "")
    out = strip_leading_style_keywords(out, keywords)
    return _tidy(out)


# ---------- 场景组（32 号）的提示词片段 ----------
# 组环境设定的标签（注入与剥离共用同一前缀——幂等靠它，不猜语义）
GROUP_ENV_LABEL = "同空间环境设定："
# 参考图继承说明句（固定模板，无槽位；只继承环境、不继承构图与陈设）
GROUP_ENV_REF_NOTE = (
    "本图与参考图属于同一真实空间的不同房间或机位：只继承建筑结构、装修材质、"
    "色调、门窗样式与照明基调；本房间的陈设与构图以本文为准，"
    "不得沿用参考图的构图与陈设"
)
# 组环境设定以**我们注入的句号**为界（注入侧会把 env 里的句号换成逗号再补一个句号，
# 见 drama.compose_scene_prompt）：剥离必须精确到这一句，否则 [^。]* 会把后面的
# 空镜约束甚至画面描述一起吃掉（实测幂等断言当场抓到）
_GROUP_ENV_RE = re.compile(re.escape(GROUP_ENV_LABEL) + r"[^。]*。")


def strip_group_blocks(text: str) -> str:
    """剥掉场景组注入的片段（组环境设定 / 参考图继承说明句）→ 干净文本。

    注入是幂等的：每次组装先剥旧的、再按当前组状态重拼（组改了/母版有无变了
    都能对齐）；剥离按**我们自己的固定标签/模板**精确匹配，用户自己写的内容不动。
    """
    out = _GROUP_ENV_RE.sub("", text or "")
    out = _strip_segment(out, GROUP_ENV_REF_NOTE)
    return _tidy(out)
