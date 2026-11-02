"""LLM 剧本解析管线（无规则兜底，失败直接报错；确定性对齐校验兜底剧情）。

流水线（v1.1 只收成品剧本；18 号起分集规划与要点表前置）：
0. 审读：通读全文，判定剧本是否完整；提取产出物元信息（剧名/题材/简介/
   真实分集目录/关键场次）。
1. 分集规划：定位分集正文（本地：LLM 集头 + 正则交叉校验）；**无集头且超
   阈值**时由 LLM 按剧情/爆点/钩子切集（plan_episodes，两段式：分块提节拍
   → 汇总切集，代码按锚句逐字定位切分）。有集头时只提炼分集要点表
   （summarize_episodes），不重切。
2. 终审 ∥ 资产分析：终审只拦 critical（18 号批次 3 后由逐集审核替换）；
   资产分析从全文档提取角色/场景/道具 + 生图提示词。
3. 分集分镜：逐集拆解（「第X幕/场」只作集内边界不升集、目录行不算集头），
   每镜带 excerpt 原文摘抄；随后执行确定性 1:1 对齐校验——原文台词/动作行
   未被覆盖时由代码直接补镜（不依赖 LLM 承诺，剧情零丢失）。
4. 画面描述：逐集生成导演级 video_prompt（精准还原、细节拉满）。
任何 LLM 环节失败抛 ScriptParseError（由任务 runner 转 failed，旧数据保留）。
"""

import hashlib
import json
import logging
import math
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app.agent import prompts as _prompts
from app.agent.skills import (
    LAYER_MODEL, LAYER_SHOT, LAYER_STORY,
    get_skill_packs, load_skill_packs_with_files,
    render_skill_directory, render_skill_text,
)
from app.core.config import settings
from app.core.prompt_metrics import clip_at_sentence
from app.core.providers import (
    ProviderError, llm_available, llm_chat, resolve_runtime_config,
)
from app.script.contracts import (
    AuditOut, AssetsOut, CostumeOut, EpisodeBeatsOut, EpisodeTableOut,
    GroupsOut, PatchOut,
    ReviewOut, ShotsOut, VpOut, contract_retry_suffix, contract_violations,
)
# 教训样本字数上限与账本同源（persist 不 import llm_parser，无环）
from app.script.persist import LESSON_SAMPLE_MAX_CHARS
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ScriptParseError(RuntimeError):
    """LLM 剧本解析失败（信息直接面向用户展示）。

    `fail_class`：基础设施类失败（限流/网络/超时/服务端）**在此透传**，供任务级
    自动重试按类分流（25 号 1.2/1.7）。此前 wrap 时把类别丢了——于是解析类任务
    遇到瞬时 429 只能硬失败，而 HTTP 层又自己同步重试（既叠乘又占死泳道）。
    """

    def __init__(self, message: str, fail_class: str = "") -> None:
        super().__init__(message)
        self.fail_class = fail_class



ProgressCallback = Callable[[float, str], None]
CancelCheck = Callable[[], bool]

# 终审缓存版本（批次 1 §31.7）：与提示词全文、生效 LLM 模型共同构成指纹——
# 任一变更自动失效；审核维度有代码级结构变化时 bump 版本号
_AUDIT_CACHE_VERSION = "v1"


def _audit_cache_key(payload: str) -> str:
    """逐集审核缓存键：sha256(提示词全文 + 审核实现 + 缓存版本 + 生效模型) + 载荷哈希。

    载荷＝「本集正文 + 上一集上下文 + 下一集上下文」——审核结论只对这组输入
    成立，任一集改动即失效。提示词措辞或 user 消息模板变动 → 指纹即变（缓存
    自动失效），杜绝「改了规则还吃到旧结论」（审计 13 号教训：原指纹只覆盖
    system prompt，改 user 模板不失效）。
    """
    try:
        # 取**实际生效**的模型名（端点自带 model），不是 resolve 的第二返回值——
        # 后者在非编码串入参下恒为空串，会让指纹漏掉模型维度：换默认端点后
        # 仍可能吃到另一个模型留下的审核结论（实测默认端点 agnes ↔ deepseek
        # 并存，两者的判定风格明显不同）
        model = resolve_runtime_config("llm", "")[0].model or ""
    except Exception:  # noqa: BLE001 模型解析失败不阻断审核
        model = ""
    try:
        import inspect

        impl = (inspect.getsource(episode_audit_contexts)
                + inspect.getsource(_audit_user_message))
    except (OSError, TypeError):  # 源码不可得（冻结打包）时退化为仅提示词指纹
        impl = ""
    fp = hashlib.sha256(
        (_prompts.SCRIPT_AUDIT_SYSTEM_PROMPT + impl + _AUDIT_CACHE_VERSION + model)
        .encode("utf-8")).hexdigest()[:16]
    text_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"drama_audit_cache:{fp}:{text_hash}"


# 审读输入上限（字符）：方舟/DeepSeek 128K 上下文足够容纳全篇
_REVIEW_MAX_CHARS = 60000

# 通用工艺包文件名：无题材归属、**每个项目都需要**的分镜工艺知识——由代码
# 无条件并入技能包集合（见审读段的路由注释），不依赖 LLM 自选、也不复制进 12 个
# 题材包（避免 12 份重复维护）。新增同类通用包只在此登记文件名。
UNIVERSAL_SKILL_FILES: tuple[str, ...] = ("shot_craft.md",)


def with_universal_packs(genre_packs: list) -> list:
    """并入通用工艺包（**无条件**，与 LLM 自选无关）。

    为什么由代码并入而不靠 LLM 选：它承载与题材无关的分镜工艺（景别/视角与 POV/
    运镜/连续性/单镜一运镜/方向性物体/情绪外化/高危动作禁用），每个项目都需要，
    漏选即静默丢知识；而复制进 12 个题材包会造成 12 份重复维护。同名去重，
    LLM 若也选中同一个包只保留一份。
    """
    names = {p.name for p in genre_packs}
    out = list(genre_packs)
    for filename, pack in load_skill_packs_with_files():
        if filename in UNIVERSAL_SKILL_FILES and pack.name not in names:
            out.append(pack)
            names.add(pack.name)
    return out

# 强集头：第X集（容忍「第 01 集」空格、《剧名》前缀、Markdown/编号前缀；幕不是分集）。
# 集名两种写法都取：集号之后（「第1集 归途」）与书名号之内（「第01集《垣上孤烟》」）；
# 仅有剧名前缀（「《柿子红了》第1集」）时集名为空——由 with_title_mark 回退到集号。
# 旧正则要求集号在行尾，导致「《柿子红了》第1集」不匹配 → Episode.title 为空
# → 过程轨迹里渲染出空「《》」残句（实测事故）。
_EPISODE_HEAD_RE = re.compile(
    r"^\s*#{0,6}\s*(?:\*\*)?\s*(?:《[^》]{1,40}》\s*)?(?:\d+\s*[·•]?\s*)?"
    r"第\s*([0-9一二三四五六七八九十百千零两]+)\s*集\s*[·•:：\-—]?\s*"
    r"(?:《\s*([^》\n]{1,40}?)\s*》|([^\n]{0,40}?))\s*$"
)


def episode_head_title(line: str) -> str:
    """从集头行提取集名（无集名返回空串）：集号后的正文 或 书名号内文字。"""
    m = _EPISODE_HEAD_RE.match((line or "").strip())
    if not m:
        return ""
    return (m.group(2) or m.group(3) or "").strip("《》 ·:：-—")


def with_title_mark(title: str, fallback_seq: int | None = None) -> str:
    """集名的显示标签：有名用《名》，无名回退「第 N 集」——**不留空《》**。

    空集名此前会在过程轨迹里渲染成「《》拆解完成」这类残句（实测《柿子红了》）。
    """
    name = (title or "").strip()
    if name:
        return f"《{name}》"
    if fallback_seq is not None:
        return f"第 {fallback_seq} 集"
    return "本集"
# 集号归一（中文数字 → int）
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10}
# 弱边界（集内切块用）：幕 / 场次头（【分镜】等逐镜标记不是边界，切太碎会
# 丢失上下文且调用次数爆炸）
_WEAK_HEAD_RE = re.compile(
    r"^\s*#{0,6}\s*(?:\*\*)?\s*(第\s*[0-9一二三四五六七八九十百千零两]+\s*幕"
    r"|场\s*\d+\s*[|｜]|场景\s*[:：])"
)

# 单集拆解预算：超过则在幕/场边界分段（每段独立 1:1 拆解）
_EPISODE_BUDGET = 3500
# 对齐循环轮数上限（LLM 检查→补镜为一轮；有界防卡死烧钱）
_MAX_ALIGN_ROUNDS = 3
# LLM 调用参数（质量优先）：思考开启、json_mode 优先，三档尝试阶梯见 _llm_call
_LLM_TEMPERATURE = 0.2
_LLM_MAX_TOKENS = 32768
_LLM_TIMEOUT = 600.0
# 分镜时长区间（秒）：通用安全区间 [5,12]——方舟 Seedance [5,12]、Agnes [4,12]、
# 本地 Wan/LTX [4,15]，取全路径可用交集（下界 5/上界 12），保证任何执行路径
# 不被 provider 静默拉长或截断；时长推导规范见拆解提示词 5.5 条。
# 公开常量：drama API（patch_shot/create_shot 钳位）与解析侧共用同一口径
SHOT_DURATION_MIN = 5
SHOT_DURATION_MAX = 12
SHOT_DURATION_DEFAULT = 8
# 场景补充视角白名单（本镜 state.view_ref 可取值）。**与 `api/drama.py` 的
# `SCENE_EXTRA_VIEWS` 必须一致**——那边是产物的节点名来源，这边是解析侧的
# 校验白名单；各写一个会漂移，由 _verify_pipeline 断言对账（同 prompt_budget
# 的镜像模式）。解析侧不能直接 import api（会成环）。
STATE_VIEW_REFS: tuple[str, ...] = ("reverse", "interaction")
# 台词行（speaker：text）
_DIALOGUE_LINE_RE = re.compile(r"^([^：\n【#|]{1,16})：(.+)$")
# 声明行关键词（「场景：/人物：/角色：」开头是结构声明不是台词——speaker 恰好
# 命中这些词时归动作行，避免对齐检查把它们误当台词行要求进 dialogue）
_DECLARATION_SPEAKERS = {"场景", "人物", "角色", "地点", "时间"}
# 场景头带编号形态（「场景一：老周面馆 夜 内」——编号场景头是常见剧本格式，
# 精确集合覆盖不了，正则兜底；实测 LLM 会把它当说话人结构化成伪台词）
_DECLARATION_SPEAKER_RE = re.compile(
    r"^(?:场景|场次|幕|地点|时间)\s*[一二三四五六七八九十百千零\d]*$")


def _is_declaration_speaker(speaker: str) -> bool:
    """说话人是否为结构声明头（场景/场次/地点/时间，可带编号）。"""
    return speaker in _DECLARATION_SPEAKERS or bool(
        _DECLARATION_SPEAKER_RE.match(speaker))


# 集级元信息头（剧本模板自带的头部行，如「时长：约20分钟 | 本集主题：失去 |
# 情绪曲线：暖→骤变→悲壮→冷钩」）。这类行**不是剧情内容**，不该要求被某个
# 镜头认领——旧实现把它归成动作行，于是永远停在「源行未认领」，体检报 error、
# 还硬拦整集视频生成（实测霜刃辞第1集：一条元信息头把出片门禁卡住）。
# 2026-10-10 扩展：允许 Markdown 加粗变体（「**时长：** 约5分钟」「**人物：**」
# 「**场景：** …」——实测《妈妈的计时器》模板全部带星号，原正则整批漏网、
# 一次解析打出 5 条 error）；关键词表补 人物/场景/人物简介/场景说明。
_EPISODE_META_RE = re.compile(
    r"^\*{0,2}\s*(?:时长|本集主题|主题|情绪曲线|本集钩子|钩子|集数|总时长|"
    r"人物简介|人物设定|人物|场景说明|场景)\s*[:：]")


def _is_definition_bullet(line: str) -> bool:
    """「- 小杰：10岁，聪明但写作业极度拖拉。」式**定义列表项**（头部人物/场景块）。

    判定：Markdown 列表符 + 冒号前是短名字（≤12 字、不含冒号）——是"资产定义"
    而不是动作行；资产信息由资产解析承接，不该要求镜头认领。
    **只在首个场次标记之前生效**（见 `_source_lines`）：正文里以列表书写的
    动作/台词行仍照常参与认领对账，豁免不外溢。
    """
    return bool(re.match(r"^[-*]\s*\**\s*[^：:\s]{1,12}\**\s*[:：]", line or ""))


def _is_structure_line(line: str) -> bool:
    """结构行总谓词（认领豁免类）：元信息头 / 场次时间头 / 定义列表项。

    体检存档的「源行未认领」误报清理（drama.repair_storyboard_qa_*）与解析
    侧共用本谓词——同源判定，清理不会误删真缺口。
    """
    return (_is_episode_meta_line(line) or _is_scene_header_line(line)
            or _is_definition_bullet(line))


def _is_episode_meta_line(line: str) -> bool:
    """是否为集级元信息头（结构声明，不参与镜头认领）。"""
    return bool(_EPISODE_META_RE.match(line.strip()))


# 场次时间头（「**【19:00】**」「【19:30 饭桌】」）：整行只有方括号段（可带
# Markdown 加粗）——是**结构信息**（时间/地点已由场景名与 state.time 承接），
# 不是剧情内容。与集级元信息头同理，要求镜头认领只会得到永远清不掉的
# 「源行未认领」error，还会触发视频生成硬门（实测《妈妈的计时器》第 1 集：
# 三行场次头打出 3 条 error 级 line_gap）。
_SCENE_HEADER_RE = re.compile(r"^\*{0,2}\s*【[^】]{1,40}】\s*\*{0,2}$")


def _is_scene_header_line(line: str) -> bool:
    """整行为方括号场次头（可选加粗）→ 镜头认领豁免（decl 类）。

    只匹配**整行**都是方括号段的行；行内出现「【闪回】她推开门」这类混排
    内容仍按动作行对账（不该豁免）。
    """
    return bool(_SCENE_HEADER_RE.match((line or "").strip()))


def _report(progress_cb: ProgressCallback | None, frac: float, label: str) -> None:
    """进度回调安全封装（回调异常不影响解析）。"""
    if progress_cb is not None:
        try:
            progress_cb(max(0.0, min(1.0, frac)), label)
        except Exception:  # noqa: BLE001
            logger.exception("解析进度回调异常")


def _check_cancel(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise ScriptParseError("任务已取消")


def _gate(trace_cb: Callable[[str, str, str], None] | None, gate: str,
          detail: str) -> None:
    """确定性门禁事件落账（批次 1 §31.6）：kind="gate" 写入 trace 的 gates 桶。

    结构化统计数据源——回答「哪道门最常响 → 改那条提示词措辞，而不是再骂
    一遍模型」；批次 2a 提供 gate-stats 聚合端点。trace_cb 为空或异常不影响
    解析主流程。
    """
    if trace_cb is None:
        return
    try:
        trace_cb("gate", gate, detail)
    except Exception:  # noqa: BLE001
        logger.exception("门禁事件落账失败：%s", gate)


def _llm_call(system: str, user: str, cancel_check: CancelCheck | None = None,
              json_mode_first: bool = True,
              stream_cb: Callable[[str, str], None] | None = None,
              stream_label: str = "",
              contract: "type[BaseModel] | None" = None) -> dict:
    """带尝试阶梯的 LLM 结构化调用：思考优先，全部失败抛 ScriptParseError。

    contract（批次 2b §32.3）：输出契约模型——JSON 提取成功后先过 schema，
    违规时把具体违规项拼进重试消息**定向重试一次**（同档参数），仍违规才
    降入下一档阶梯——重试有据（「哪里错了」而非笼统「又失败了」）。

    stream_cb 提供时走 SSE 流式（思考/正文增量实时外送，观测窗流式输出）。
    """
    last_error: Exception | None = None
    modes = ((json_mode_first, False), (False, False), (False, True))
    for mode_index, (json_mode, think_off) in enumerate(modes):
        _check_cancel(cancel_check)
        if stream_cb is not None:
            stream_cb("begin", stream_label or "AI 思考中")
        try:
            content = llm_chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                json_mode=json_mode, temperature=_LLM_TEMPERATURE,
                max_tokens=_LLM_MAX_TOKENS, timeout=_LLM_TIMEOUT,
                thinking_disabled=think_off,
                on_delta=(lambda t: stream_cb("delta", t)) if stream_cb else None,
                cancel_check=cancel_check)
            if stream_cb is not None:
                stream_cb("end", "")
            parsed = _extract_json(content)
        except (ProviderError, ValueError, json.JSONDecodeError) as e:
            # 取消属终态：立即上抛中止整条流水线，不进重试阶梯
            if isinstance(e, ProviderError) and "已取消" in str(e):
                raise ScriptParseError(str(e)) from e
            last_error = e
            if stream_cb is not None:
                stream_cb("end", "")
            logger.warning("LLM 调用失败（json_mode=%s 思考关=%s）：%s",
                           json_mode, think_off, e)
            continue
        if contract is None:
            return parsed
        violations = contract_violations(parsed, contract)
        if not violations:
            return parsed
        # 契约定向重试：只在【首档】做一次（违规项拼进 user 消息点名修复）——
        # 旧实现在每一档都重试，三档累计最多 6 次调用（长剧本分片 ×N，成本
        # 放大；审计 13 号）。首档已用最好参数（json_mode + 思考开启），
        # 后续档位本身是降级尝试，重复定向重试收益低。
        if mode_index == 0:
            try:
                if stream_cb is not None:
                    stream_cb("begin", stream_label or "AI 思考中")
                content2 = llm_chat(
                    [{"role": "system", "content": system},
                     {"role": "user",
                      "content": user + contract_retry_suffix(violations)}],
                    json_mode=json_mode, temperature=_LLM_TEMPERATURE,
                    max_tokens=_LLM_MAX_TOKENS, timeout=_LLM_TIMEOUT,
                    thinking_disabled=think_off,
                    on_delta=(lambda t: stream_cb("delta", t)) if stream_cb else None,
                    cancel_check=cancel_check)
                if stream_cb is not None:
                    stream_cb("end", "")
                parsed2 = _extract_json(content2)
                violations2 = contract_violations(parsed2, contract)
                if not violations2:
                    logger.warning("LLM 输出契约违规已定向重试修复：%s",
                                   violations[:2])
                    return parsed2
                last_error = ValueError("输出契约违规：" + "；".join(violations2[:3]))
                logger.warning("LLM 输出契约违规（定向重试后仍失败）：%s", violations2)
            except (ProviderError, ValueError, json.JSONDecodeError) as e:
                if isinstance(e, ProviderError) and "已取消" in str(e):
                    raise ScriptParseError(str(e)) from e
                last_error = e
                if stream_cb is not None:
                    stream_cb("end", "")
                logger.warning("LLM 契约定向重试调用失败：%s", e)
        else:
            last_error = ValueError("输出契约违规：" + "；".join(violations[:3]))
    # fail_class 透传（25 号 1.7）：基础设施类失败要能被任务级按类分流重试，
    # 否则瞬时 429 在解析任务上就是硬失败（而 HTTP 层已不再自行重试）。
    raise ScriptParseError(f"LLM 调用失败：{last_error}",
                           getattr(last_error, "fail_class", ""))


def _first_json_object(text: str, start: int) -> str | None:
    """从 start（'{' 位置）截取第一个【花括号配平】的对象串。

    配平扫描尊重字符串状态（含转义）。回复尾部带散文时，散文里的花括号
    （如「若 xxx 则 {} 结构…」）不会再被 rfind("{") 吞进片段——旧实现
    因此解析必失败（审计 13 号：低概率但代价是整轮阶梯重试）。
    未配平时返回 None（截断场景交给 _repair_json 补全）。
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _extract_json(content: str) -> dict:
    """从 LLM 回复中提取 JSON：剥围栏、配平截取、截断修复。"""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    start = content.find("{")
    if start < 0:
        raise ValueError(f"回复中未找到 JSON 对象：{content[:120]}")
    # ① 优先取花括号配平的首个对象（散文含花括号时不被吞）
    fragment = _first_json_object(content, start)
    if fragment is not None:
        try:
            return json.loads(fragment)
        except json.JSONDecodeError:
            pass
    # ② 截断修复（回复被 max_tokens 截断时括号未闭合，交给修复器补齐）
    repaired = _repair_json(content[start:])
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError as e:
        raise ValueError(f"回复 JSON 非法且无法修复：{content[:120]}") from e
    logger.warning("LLM 回复被截断，已修复 JSON（补齐 %s 字）",
                   len(repaired) - len(fragment or content))
    return data


def _repair_json(fragment: str) -> str:
    """截断 JSON 修复：扫描字符串/括号状态，补齐缺失的引号与收尾括号。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    repaired = fragment.rstrip()
    while repaired.endswith(","):
        repaired = repaired[:-1].rstrip()
    if in_string:
        repaired += '"'
    for bracket in reversed(stack):
        repaired += "}" if bracket == "{" else "]"
    return repaired


def _as_str(value, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _as_int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _norm_role(value) -> str:
    return "主角" if "主角" in _as_str(value) else "配角"


def _cn_to_int(num: str) -> int | None:
    """集号归一：「01」/「一」/「十二」→ int（无法识别返回 None）。"""
    num = num.strip()
    if num.isdigit():
        return int(num)
    if num in _CN_NUM:
        return _CN_NUM[num]
    if num.startswith("十"):
        rest = num[1:]
        return 10 + (_CN_NUM.get(rest, 0) if rest else 0)
    if "十" in num:
        tens, _, ones = num.partition("十")
        return _CN_NUM.get(tens, 1) * 10 + _CN_NUM.get(ones, 0)
    return None


# ---------- 阶段 0：审读 ----------

def _display_model_id(raw: str) -> str:
    """预设模型 ID 展示清洗：剥离 provider 前缀（uuid::model → model），便于对照技能目录。"""
    cleaned = (raw or "").split("::", 1)[-1].strip()
    return cleaned or (raw or "").strip()


def review_script(text: str, cancel_check: CancelCheck | None = None,
                  stream_cb: Callable[[str, str], None] | None = None,
                  model_presets: dict[str, str] | None = None,
                  requirements_text: str = "") -> dict:
    """通读全文：完整性判定 + 产出物元信息 + 自选技能包（题材/模型，渐进式加载）。

    requirements_text：解析前置需求段落（21 号批次 5）——注入后技能包自选与
    synopsis/关键场次判断贴合用户口径，而不是模型自己猜基调。
    """
    _check_cancel(cancel_check)
    # 渐进式加载第一级：技能目录（仅摘要行）随审读注入，由 LLM 自行选择
    directory = render_skill_directory()
    available = (f"\n\n【可用技能包目录（据此选择本剧本需要的制作技能包）】\n{directory}"
                 if directory else "")
    # 项目预设生成模型随目录展示：供 LLM 对照模型技能包的「适用模型」声明自选
    presets = model_presets or {}
    preset_lines = [f"{label}：{_display_model_id(presets[key])}"
                    for key, label in (("t2i", "文生图（生图提示词）"),
                                       ("t2v", "文生视频（画面描述）"))
                    if presets.get(key)]
    preset_hint = (f"\n\n【项目生成模型预设（据此选择适用的模型技能包）】\n"
                   + "\n".join(preset_lines) if preset_lines else "")
    req_hint = f"\n\n{requirements_text}" if requirements_text.strip() else ""
    data = _llm_call(
        _prompts.SCRIPT_REVIEW_SYSTEM_PROMPT,
        f"审读以下剧本文档：\n\n{text[:_REVIEW_MAX_CHARS]}{req_hint}"
        f"{available}{preset_hint}",
        cancel_check, stream_cb=stream_cb,
        stream_label="审读剧本（通读全文）", contract=ReviewOut)
    heads = [_as_str(t) for t in data.get("episode_head_lines") or [] if _as_str(t)]
    tags = [_as_str(t) for t in data.get("genre_tags") or [] if _as_str(t)]
    skills = [_as_str(t) for t in data.get("skills") or [] if _as_str(t)]
    # 关键场次（稀疏导演决策层）：episode 必须为正 int、note 非空，宁缺毋滥
    key_scenes = [
        {"episode": _as_int(k.get("episode"), 0, 1, 10 ** 4),
         "note": _as_str(k.get("note"))}
        for k in data.get("key_scenes") or []
        if isinstance(k, dict) and _as_str(k.get("note"))
        and _as_int(k.get("episode"), 0, 1, 10 ** 4) > 0]
    return {
        "title": _as_str(data.get("title")),
        "is_complete": bool(data.get("is_complete_script")),
        "genre_tags": tags,
        "skills": skills,
        "synopsis": _as_str(data.get("synopsis")),
        "episode_head_lines": heads,
        "key_scenes": key_scenes,
    }


# ---------- 逐集剧情审核（18 号 §52：带上一集/下一集上下文，只拦 critical） ----------

def episode_audit_contexts(sections: list[dict], idx: int,
                           exit_states: dict | None = None
                           ) -> tuple[dict, dict]:
    """构造第 idx 集审核的相邻集上下文（纯本地切片，确定性，不调 LLM）。

    上一集：剧情要点 + 出口状态（优先用**已审核产出的最新 exit_state**，更准；
    未审核时退回分集要点表的 exit_hint）+ 正文尾部锚——判断「位置/道具连续性」
    这类硬伤时纯要点不够，留一小段原文兜底；
    下一集：剧情要点 + 钩子 + 正文头部锚。

    相邻集一律取**原文 body**（分集切分产物，始终存在），不依赖相邻集是否已
    制作——逐集推进时上一集可能还没拆解。
    """
    states = exit_states or {}
    prev_ctx: dict = {}
    next_ctx: dict = {}
    if idx > 0:
        p = sections[idx - 1]
        body = p.get("body") or ""
        seq = p.get("num")
        # 集号键容错：内存里是 int 键，从 JSON 读回时可能是字符串键
        st = states.get(seq) or states.get(str(seq)) or ""
        prev_ctx = {
            "seq": seq,
            "title": p.get("title") or "",
            "beats": [b for b in (p.get("beats") or []) if b],
            "exit_state": st or p.get("exit_hint") or "",
            "tail": body[-EPISODE_CTX_TAIL_CHARS:] if EPISODE_CTX_TAIL_CHARS else "",
        }
    if idx + 1 < len(sections):
        n = sections[idx + 1]
        body = n.get("body") or ""
        next_ctx = {
            "seq": n.get("num"),
            "title": n.get("title") or "",
            "beats": [b for b in (n.get("beats") or []) if b],
            "hooks": [h for h in (n.get("hooks") or []) if isinstance(h, dict)],
            "head": body[:EPISODE_CTX_HEAD_CHARS] if EPISODE_CTX_HEAD_CHARS else "",
        }
    return prev_ctx, next_ctx


def _render_ctx_block(label: str, ctx: dict, anchor_key: str,
                      anchor_label: str) -> str:
    """渲染相邻集上下文块（空字段不渲染，避免提示词里出现空标题行）。"""
    if not ctx:
        return f"【{label}】无（本集是全集的第一集或最后一集，无需衔接核对）\n"
    lines = [f"【{label}：第 {ctx.get('seq')} 集《{ctx.get('title') or '未命名'}》】"]
    beats = ctx.get("beats") or []
    if beats:
        lines.append("剧情要点：" + "；".join(str(b) for b in beats))
    if ctx.get("exit_state"):
        lines.append("结尾状态：" + str(ctx["exit_state"]))
    hooks = ctx.get("hooks") or []
    hook_texts = [str(h.get("text") or "") for h in hooks if h.get("text")]
    if hook_texts:
        lines.append("钩子/爆点：" + "；".join(hook_texts))
    anchor = ctx.get(anchor_key) or ""
    if anchor:
        lines.append(f"{anchor_label}（原文摘录）：\n{anchor}")
    return "\n".join(lines) + "\n"


def _audit_user_message(ep: dict, prev_ctx: dict, next_ctx: dict) -> str:
    """逐集审核 user 消息模板（缓存指纹对它的源码取哈希）。"""
    return (
        "以下是三份材料，请审核**本集**（第二份）。\n\n"
        + _render_ctx_block("上一集", prev_ctx, "tail", "上一集正文尾部")
        + f"\n【本集（第 {ep.get('num')} 集《{ep.get('title') or '未命名'}》）"
        + "正文（审核对象）】\n" + (ep.get("body") or "") + "\n\n"
        + _render_ctx_block("下一集", next_ctx, "head", "下一集正文开头")
    )


def _norm_audit(data: dict, cached: bool) -> dict:
    """归一审核结果：issues 逐条规整 + 裁决。

    passed **不采信 LLM 自报**：存在 critical 即不通过（v1.1 用户口径——审核
    只拦会毁片的硬伤，major/minor 仅提示不阻断）。缺 quote（无定位锚）或无
    description 的问题丢弃——它无法被定点修复，留在清单里只会让用户点了
    「一键修复」却修不动。
    """
    issues: list[dict] = []
    for it in data.get("issues") or []:
        if not isinstance(it, dict):
            continue
        quote = _as_str(it.get("quote"))
        desc = _as_str(it.get("description"))
        if not quote or not desc:
            continue
        sev = _as_str(it.get("severity")).lower()
        if sev not in ("critical", "major", "minor"):
            sev = "major"
        issues.append({
            "severity": sev,
            "location": _as_str(it.get("location")),
            "type": _as_str(it.get("type")),
            "quote": quote,
            "description": desc,
            "suggestion": _as_str(it.get("suggestion")),
        })
    return {"passed": not any(i["severity"] == "critical" for i in issues),
            "verdict": _as_str(data.get("verdict")),
            "issues": issues,
            "exit_state": _as_str(data.get("exit_state")),
            "cached": cached}


def audit_episode(ep: dict, prev_ctx: dict, next_ctx: dict,
                  cancel_check: CancelCheck | None = None,
                  stream_cb: Callable[[str, str], None] | None = None,
                  cache: tuple[Callable[[str], str | None],
                               Callable[[str, str], None]] | None = None) -> dict:
    """逐集剧情审核（18 号 §52）：带上一集/下一集上下文审本集。

    返回 {"passed", "verdict", "issues", "exit_state", "cached"}。
    同输入（本集正文 + 相邻集上下文 + 提示词 + 模型）命中缓存即零调用复用。
    """
    body = (ep.get("body") or "").strip()
    if not body:
        raise ScriptParseError("本集正文为空，无法审核")
    user = _audit_user_message(ep, prev_ctx, next_ctx)
    cache_key = ""
    if cache is not None:
        try:
            cache_key = _audit_cache_key(user)
            hit = cache[0](cache_key)
            if hit:
                data = json.loads(hit)
                if isinstance(data, dict):
                    return _norm_audit(data, cached=True)
        except Exception:  # noqa: BLE001 缓存故障降级为正常审核
            logger.exception("逐集审核缓存读取失败，走正常审核")
            cache_key = ""
    data = _llm_call(_prompts.SCRIPT_AUDIT_SYSTEM_PROMPT, user, cancel_check,
                     stream_cb=stream_cb,
                     stream_label=f"第 {ep.get('num')} 集剧情审核",
                     contract=AuditOut)
    if cache is not None and cache_key:
        try:
            cache[1](cache_key, json.dumps(data, ensure_ascii=False))
        except Exception:  # noqa: BLE001 缓存写入失败不影响审核结果
            logger.exception("逐集审核缓存写入失败")
    return _norm_audit(data, cached=False)


# ---------- 逐集一键修复（18 号 §53：单轮补丁 + 确定性命中对账，不跑复审） ----------


def _apply_patches(text: str, patches: list[dict]) -> tuple[str, list[dict]]:
    """逐字应用补丁（确定性执行）：quote 字符串匹配定位 → replace 一次。

    每个补丁独立应用、独立成败：quote 未命中（模型转述了原句）或 new 与
    quote 相同（未做修改）或 new 超长（借修复之名扩写）→ 该补丁记失败并
    附原因，不影响其他补丁。应用后由调用方复核（LLM 语义判断）。
    """
    new_text = text
    results: list[dict] = []
    for pt in patches:
        idx = pt.get("index")
        quote = str(pt.get("quote") or "").strip()
        new = str(pt.get("new") or "").strip()
        if not quote:
            results.append({**pt, "applied": False, "reason": "补丁缺少 quote"})
            continue
        # 借修复之名扩写的拦截：new 只允许是 quote 的修正版（≤2 倍），
        # 超长说明模型在借位扩写——拒收（实测旧版"修复"把 1422 行改成 2653 行）
        if new != quote and len(new) > max(240, len(quote) * 2):
            results.append({**pt, "applied": False,
                            "reason": f"new 超长（{len(new)} 字 > quote {len(quote)} 字的 2 倍），疑似扩写"})
            continue
        if quote not in new_text:
            results.append({**pt, "applied": False,
                            "reason": "quote 未在当前稿中找到（须逐字摘抄）"})
            continue
        if new == quote:
            results.append({**pt, "applied": False, "reason": "new 与 quote 相同（未做修改）"})
            continue
        new_text = new_text.replace(quote, new, 1)
        results.append({**pt, "applied": True})
    return new_text, results


def optimize_episode(text: str, issues: list[dict],
                     cancel_check: CancelCheck | None = None,
                     stream_cb: Callable[[str, str], None] | None = None,
                     lessons_text: str = "",
                     progress_cb: Callable[[int, int], None] | None = None) -> dict:
    """逐集一键修复（18 号 §53）：LLM 出补丁 → 代码逐字应用 → **不跑复审**。

    与旧整部优化的区别（用户明确「修复后不再复审」）：
    - **单轮补丁**，不跑「修复→复核→再修」收敛循环——旧版每轮都要一次 LLM
      定向复核，用户体感是"修完还要等一轮"，且修完重审仍会挑出新问题；
    - 改为**补丁命中对账**（确定性、零额外 LLM 调用）：逐条记 applied /
      quote 未命中 / new 超长拒收，未命中如实告警，面板按条标出；
    - 残留问题用户可再点一次「一键修复」（只修勾选项），或直接手改当集正文。

    保留一次**纠偏重试**：quote 未命中的补丁把「被拒收的原句」回喂给模型，
    要求重新逐字摘抄——这不是复审（不重新判断问题是否存在），只是补丁协议
    自身的可靠性环节，实测能救回大部分转述型 quote。

    返回 {"text": 修复后正文, "patches": [...对账...], "stats": {...}}。
    """
    _check_cancel(cancel_check)
    issues = [it for it in (issues or []) if isinstance(it, dict)]
    if not issues:
        raise ScriptParseError("没有可修复的问题清单")
    lessons_part = f"\n{lessons_text}" if lessons_text else ""

    # 待修清单带上定位锚（quote）与建议改法：模型只写"改成什么"，不重发正文
    pissues = [{**it, "index": i} for i, it in enumerate(issues)]
    user = (f"【本集正文】\n{text}\n\n"
            f"【待修复问题清单（逐条产出补丁，一条不落）】\n"
            f"{json.dumps(pissues, ensure_ascii=False)}{lessons_part}")
    if progress_cb is not None:
        progress_cb(1, 3)
    data = _llm_call(_prompts.SCRIPT_PATCH_SYSTEM_PROMPT, user, cancel_check,
                     stream_cb=stream_cb,
                     stream_label="生成修复补丁（逐条定点）",
                     contract=PatchOut)
    patches = [pt for pt in (data.get("patches") or []) if isinstance(pt, dict)]
    current, results = _apply_patches(text, patches)
    patches_log = list(results)

    # 纠偏重试（一次）：只针对「quote 未在原文中找到」的补丁——模型转述了原句
    missed = [r for r in results if not r.get("applied")
              and "未在当前稿中找到" in str(r.get("reason", ""))]
    if missed:
        retry_hint = json.dumps(
            [{"index": r.get("index"), "bad_quote": r.get("quote", ""),
              "reason": r.get("reason")} for r in missed], ensure_ascii=False)
        retry_user = (f"【本集正文】\n{current}\n\n"
                      f"【待修复问题清单】\n{json.dumps(pissues, ensure_ascii=False)}\n\n"
                      f"【上一轮补丁被拒收（quote 未在原文中找到——你转述了原句）】\n"
                      f"{retry_hint}\n请重新摘抄原文（一字不差，含标点与空白）后给出补丁。")
        if progress_cb is not None:
            progress_cb(2, 3)
        retry_data = _llm_call(_prompts.SCRIPT_PATCH_SYSTEM_PROMPT, retry_user, cancel_check,
                               stream_cb=stream_cb,
                               stream_label="补丁纠偏重试（逐字摘抄）",
                               contract=PatchOut)
        retry_patches = [pt for pt in (retry_data.get("patches") or [])
                         if isinstance(pt, dict)]
        current, retry_results = _apply_patches(current, retry_patches)
        # 重试结果覆盖同 index 的首次结果（以最终稿为准），其余追加
        by_index = {r.get("index"): r for r in patches_log}
        for r in retry_results:
            by_index[r.get("index")] = r
        patches_log = list(by_index.values())
    if progress_cb is not None:
        progress_cb(3, 3)

    applied = sum(1 for r in patches_log if r.get("applied"))
    stats = {"issues": len(issues), "patches": len(patches_log),
             "applied": applied, "missed": len(patches_log) - applied}
    logger.info("逐集定点修复完成：%s", stats)
    return {"text": current, "patches": patches_log, "stats": stats}

# ---------- 集切分 ----------

def split_episodes(text: str) -> tuple[str, list[dict]]:
    """按强集头切分正文（正则兜底路径）：返回（首部非正片文本, [{num, title, body}]）。

    - 集头容忍 Markdown/编号/空格/书名号（「# 03 · 第01集《垣上孤烟》」）；
    - 同一集号取最后一次出现（正文标题在后，目录条目在前被覆盖）；
    - 「第X幕/场」不是分集，留作集内边界。
    """
    heads: dict[int, tuple[int, str]] = {}
    lines = text.splitlines()
    for idx, ln in enumerate(lines):
        m = _EPISODE_HEAD_RE.match(ln)
        if not m:
            continue
        num = _cn_to_int(m.group(1))
        if num is None:
            continue
        heads[num] = (idx, ln.strip())  # 同集号后者覆盖（正文在后）
    if not heads:
        return text, []
    return _sections_from_indices(text, [idx for _, (idx, _) in sorted(heads.items())])


def _norm_line(s: str) -> str:
    """行归一化（定位 LLM 抄录的标题行）：去空白/Markdown 符号/星号。"""
    return re.sub(r"[\s#*>`【】\[\]()（）]", "", s)


def split_episodes_by_heads(text: str, head_lines: list[str]) -> tuple[str, list[dict]]:
    """按 LLM 抄录的分集标题行原文定位切分（格式无关的主路径）。

    逐行归一化比对（LLM 可能漏抄 Markdown 符号），每个标题行取**最后一次**
    出现：正文标题行在后、目录条目在前，同文时取后者（与 split_episodes 的
    同集号去重语义一致，避免把整集正文误切进目录区）。定位失败的标题行跳过，
    全部失败时由调用方回退正则切分。
    """
    normalized = {hl: _norm_line(hl) for hl in head_lines if _norm_line(hl)}
    lines = text.splitlines()
    line_norms = [_norm_line(ln) for ln in lines]
    indices: list[int] = []
    for norm in normalized.values():
        hit = -1
        for i, ln_norm in enumerate(line_norms):
            if ln_norm and ln_norm == norm:
                hit = i  # 持续覆盖：同文行取最后一次出现（正文标题在后）
        if hit >= 0:
            indices.append(hit)
    indices = sorted(set(indices))
    if len(indices) < 2:  # 少于 2 个定位点：无法构成多集，交回退路径
        return text, []
    return _sections_from_indices(text, indices)


def _sections_from_indices(text: str, indices: list[int]) -> tuple[str, list[dict]]:
    """按行号切分集（相邻集头之间为正文），标题从集头行提取。"""
    indices = sorted(set(indices))
    lines = text.splitlines()
    front = "\n".join(lines[:indices[0]]).strip()
    episodes = []
    for i, idx in enumerate(indices):
        end = indices[i + 1] if i + 1 < len(indices) else len(lines)
        body = "\n".join(lines[idx + 1:end]).strip()
        head_line = lines[idx].strip()
        # 集名提取统一走 episode_head_title（与集头正则同源）：它只认「集号之后」
        # 与「集号紧跟的书名号」两种位置——旧的宽松 `《(.+?)》` 会把**剧名前缀**
        # 当成集名（「《柿子红了》第1集」→ 集名误取「柿子红了」）。
        title = episode_head_title(head_line)
        num = None
        m = _EPISODE_HEAD_RE.match(head_line)
        if m:
            num = _cn_to_int(m.group(1))
        episodes.append({"num": num or (i + 1), "title": title, "body": body})
    return front, episodes


def _split_segments(text: str, budget: int, min_seg: int = 2000) -> list[str]:
    """按弱边界（幕/场）凑段切块：达到最小段长后在最近的弱边界切，
    硬上限 budget 时强制切——保证段间既有边界对齐又不至于过碎。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for ln in lines:
        if current and length >= budget:
            chunks.append("\n".join(current))
            current, length = [], 0
        elif current and length >= min_seg and _WEAK_HEAD_RE.match(ln.strip()):
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(ln)
        length += len(ln) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or ([text] if text.strip() else [])


# ---------- 分集规划（18 号 §51：LLM 按剧情/爆点/钩子切集 + 分集要点表） ----------

# 单集目标区间（字）：实测库内 39 集「中位 2197 / 平均 2136」，真实长篇
# 《霜刃辞》10 集「2293-6813，均值 3113」——据此定 2000-4000 作为分集提示词
# 的切集粒度约束（不是硬门禁：越界只写告警，最终由用户在界面上调整）
EPISODE_TARGET_MIN_CHARS = 2000
EPISODE_TARGET_MAX_CHARS = 4000
# 分块提节拍的字数预算：节拍表输出很小（每 1000 字 2-4 条），可给到接近审读
# 上限——39k 字剧本 2 段、100k 字 4 段，3 并发下一两轮跑完
EPISODE_BEATS_CHUNK_BUDGET = 30000
# 分集容量告警线（倍率）：平均集长超过 目标上限×该倍率 时写告警（提示词
# 未遵守切集粒度，建议用户手动重分集），不阻断
EPISODE_OVERSIZE_RATIO = 1.5
# 逐集审核注入相邻集的原文锚长度（字）：以剧情要点为主，原文锚兜底「位置/
# 道具连续性」这类看要点判不出的硬伤；置 0 = 纯要点模式（出口状态快照仍注入）
EPISODE_CTX_TAIL_CHARS = 800
EPISODE_CTX_HEAD_CHARS = 800


def _find_anchor(text: str, anchor: str, start: int) -> int:
    """逐字定位锚句：返回原文下标，未命中返回 -1。

    先严格逐字匹配（红线：代码只做确定性对账，不猜语义）；未命中时退一步做
    **去空白后匹配**——LLM 抄长句时偶有换行/空格差异，这仍属逐字对账（只容忍
    空白），不是语义近似。仍不命中即交调用方告警回退。
    """
    a = (anchor or "").strip()
    if not a or start >= len(text):
        return -1
    hit = text.find(a, start)
    if hit >= 0:
        return hit
    target = re.sub(r"\s+", "", a)
    if not target:
        return -1
    # 在「去空白视图」上定位，再把该下标映射回原文下标
    pos = re.sub(r"\s+", "", text[start:]).find(target)
    if pos < 0:
        return -1
    seen = 0
    for i in range(start, len(text)):
        if text[i].isspace():
            continue
        if seen == pos:
            return i
        seen += 1
    return -1


def _annotate_offsets(text: str, sections: list[dict]) -> None:
    """为分集补原文字符区间（逐字定位 body，按顺序推进游标）。

    body 是原文行的原样拼接（仅首尾 strip），逐字定位必然命中；定位失败
    （旧数据/异常文本）置 0，由消费方容忍——不做任何语义近似。
    """
    cursor = 0
    for sec in sections:
        body = (sec.get("body") or "").strip()
        pos = text.find(body, cursor) if body else -1
        if pos < 0:
            sec["char_start"], sec["char_end"] = 0, 0
            continue
        sec["char_start"] = pos
        sec["char_end"] = pos + len(body)
        cursor = pos + len(body)


def _beats_from_chunks(text: str, cancel_check: CancelCheck | None,
                       stream_cb: Callable[[str, str], None] | None,
                       progress_cb: ProgressCallback | None = None) -> list[dict]:
    """第一段：分块并发提取节拍表（长剧本单次调用看不完全文）。

    个别段失败跳过（保底可用性）；全部失败则报错——静默产出空节拍表会让
    第二段无从切集，失败必须显式。
    """
    chunks = _split_segments(text, EPISODE_BEATS_CHUNK_BUDGET)
    total = len(chunks)
    results: list[list[dict] | None] = [None] * total
    done = {"n": 0}
    lock = threading.Lock()

    def _one(i: int, chunk: str) -> None:
        _check_cancel(cancel_check)
        try:
            data = _llm_call(
                _prompts.SCRIPT_EPISODE_BEATS_SYSTEM_PROMPT,
                f"提取以下剧本片段的节拍：\n\n{chunk}",
                cancel_check, stream_cb=stream_cb,
                stream_label=f"分集节拍 第 {i + 1}/{total} 段（思考中）",
                contract=EpisodeBeatsOut)
            results[i] = [b for b in (data.get("beats") or [])
                          if isinstance(b, dict)]
        except ScriptParseError as e:
            logger.warning("分集节拍第 %s/%s 段失败（跳过）：%s", i + 1, total, e)
        with lock:
            done["n"] += 1
            if progress_cb is not None:
                progress_cb(done["n"] / total, f"分集节拍 {done['n']}/{total} 段")

    workers = min(3, total)
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="ep-beats") as pool:
            list(pool.map(lambda a: _one(*a), list(enumerate(chunks))))
    else:
        for i, chunk in enumerate(chunks):
            _one(i, chunk)
    if results and all(r is None for r in results):
        raise ScriptParseError("分集节拍提取全部分段失败，请重试或检查 LLM 服务")
    beats: list[dict] = []
    for r in results:
        beats.extend(r or [])
    return beats


def _render_beats_table(beats: list[dict]) -> str:
    """节拍表渲染（第二段输入）：锚句逐字 + 类型 + 说明，按原文顺序。"""
    lines = []
    for i, b in enumerate(beats, 1):
        anchor = _as_str(b.get("anchor"))
        if not anchor:
            continue
        kind = _as_str(b.get("kind")) or "setup"
        note = _as_str(b.get("note"))
        lines.append(f"{i}. [{kind}] {anchor}" + (f"（{note}）" if note else ""))
    return "\n".join(lines)


def _split_by_anchors(text: str, plans: list[dict]) -> list[dict]:
    """按 start_anchor 逐字定位切分分集正文（确定性，不调 LLM）。

    红线：语义判断（哪里该切）归模型，代码只做逐字对账与切片。锚句未命中时
    按规则回退——首集退回全文起点（否则前置正文整段丢失），后续集作废该边界
    （内容并入前一集），**绝不臆造切点**。
    """
    anchors: list[tuple[int, dict]] = []
    cursor = 0
    for idx, p in enumerate(plans):
        anchor = _as_str(p.get("start_anchor"))
        pos = _find_anchor(text, anchor, cursor)
        if pos < 0:
            if idx == 0:
                logger.warning("分集规划：首集起始锚句未命中，退回全文起点")
                pos = 0
            else:
                logger.warning("分集规划：第 %s 集起始锚句未命中，该边界作废"
                               "（内容并入前一集）：%s", idx + 1, anchor[:40])
                continue
        anchors.append((pos, p))
        cursor = pos + max(len(anchor), 1)
    if len(anchors) < 2:
        raise ScriptParseError(
            "分集规划失败：起始锚句在原文中定位不到（模型抄录与原文不一致），"
            "请重试或手动分集")
    sections: list[dict] = []
    for i, (pos, p) in enumerate(anchors):
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(text)
        body = text[pos:end].strip()
        if not body:
            continue
        sections.append({
            "num": len(sections) + 1,
            "title": _as_str(p.get("title")),
            "body": body,
            "char_start": pos,
            "char_end": end,
            "synopsis": _as_str(p.get("synopsis")),
            "beats": [_as_str(b) for b in (p.get("beats") or []) if _as_str(b)],
            "hooks": [h for h in (p.get("hooks") or []) if isinstance(h, dict)],
            "exit_hint": _as_str(p.get("exit_hint")),
            "plan_source": "llm",
        })
    if not sections:
        raise ScriptParseError("分集规划失败：切分后无有效分集正文，请重试")
    return sections


def plan_episodes(text: str, cancel_check: CancelCheck | None = None,
                  stream_cb: Callable[[str, str], None] | None = None,
                  trace_cb: Callable[[str, str, str], None] | None = None,
                  progress_cb: ProgressCallback | None = None) -> list[dict]:
    """LLM 分集规划（无集头且超阈值时触发）：按剧情/爆点/钩子切集。

    两段式：分块并发提节拍表 → 汇总节拍表切集 + 产出分集要点表。
    切分本身由 `_split_by_anchors` 确定性执行（见其文档）。

    返回 [{num, title, body, char_start, char_end, synopsis, beats, hooks,
    exit_hint, plan_source}]，body 为原文切片。
    """
    _check_cancel(cancel_check)
    beats = _beats_from_chunks(text, cancel_check, stream_cb, progress_cb)
    if not beats:
        raise ScriptParseError(
            "分集规划失败：未能从剧本中提取到任何节拍，请重试或手动分集")
    table = _render_beats_table(beats)
    _check_cancel(cancel_check)
    data = _llm_call(
        _prompts.SCRIPT_EPISODE_PLAN_SYSTEM_PROMPT,
        f"以下是全剧节拍表（按原文顺序），请据此切分分集：\n\n{table}",
        cancel_check, stream_cb=stream_cb,
        stream_label="分集规划（按剧情/爆点/钩子切集）", contract=EpisodeTableOut)
    plans = [p for p in (data.get("episodes") or []) if isinstance(p, dict)]
    if not plans:
        raise ScriptParseError("分集规划失败：模型未产出任何分集，请重试")
    sections = _split_by_anchors(text, plans)

    # 容量告警（确定性）：平均集长超上限 1.5 倍 → 提示词未遵守切集粒度，
    # 由用户在界面上手动重分集（不阻断，分集计划本身是可编辑产物）
    avg = sum(len(s["body"]) for s in sections) / len(sections)
    if avg > EPISODE_TARGET_MAX_CHARS * EPISODE_OVERSIZE_RATIO and trace_cb is not None:
        trace_cb("thought", "分集容量告警",
                 f"平均每集 {int(avg)} 字，超过目标上限"
                 f"{EPISODE_TARGET_MAX_CHARS} 字的 {EPISODE_OVERSIZE_RATIO} 倍"
                 f"（共 {len(sections)} 集）——单集制作会偏重，可在分集列表"
                 "手动合并/拆分后重做")
    return sections


def _summary_chunks(sections: list[dict]) -> list[list[dict]]:
    """要点提炼分块（确定性）：按集顺序凑到审读上限，不拆散单集。"""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for sec in sections:
        ln = len(sec.get("body") or "")
        if cur and size + ln > _REVIEW_MAX_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(sec)
        size += ln
    if cur:
        chunks.append(cur)
    return chunks


def summarize_episodes(text: str, sections: list[dict],
                       cancel_check: CancelCheck | None = None,
                       stream_cb: Callable[[str, str], None] | None = None,
                       trace_cb: Callable[[str, str, str], None] | None = None
                       ) -> list[dict]:
    """剧本已有集头时的分集要点提炼（18 号 §51.4）。

    一次调用（超长时按集分块并发）产出各集 synopsis/beats/hooks/exit_hint，
    按集号回填到 sections。**失败降级为空要点**（不阻断建档）：要点缺失时
    逐集审核退化为「上一集出口状态 + 原文锚」，仍可用。
    """
    if not sections:
        return sections
    chunks = _summary_chunks(sections)
    total = len(chunks)
    results: list[dict[int, dict] | None] = [None] * total

    def _one(i: int, group: list[dict]) -> None:
        _check_cancel(cancel_check)
        # 每块带上本块的集号清单：模型据此只输出这些集，避免跨块重复
        seqs = "、".join(str(s["num"]) for s in group)
        body = "\n\n".join(
            (text[s["char_start"]:s["char_end"]]
             if s.get("char_end") else s.get("body") or "")
            for s in group)
        try:
            data = _llm_call(
                _prompts.SCRIPT_EPISODE_SUMMARY_SYSTEM_PROMPT,
                f"本片段包含第 {seqs} 集，请只对这 {len(group)} 集提炼要点：\n\n{body}",
                cancel_check, stream_cb=stream_cb,
                stream_label=f"分集要点 第 {i + 1}/{total} 段（思考中）",
                contract=EpisodeTableOut)
            out: dict[int, dict] = {}
            for p in data.get("episodes") or []:
                if isinstance(p, dict):
                    seq = _as_int(p.get("seq"), 0, 0, 10 ** 4)
                    if seq > 0:
                        out[seq] = p
            results[i] = out
        except ScriptParseError as e:
            logger.warning("分集要点第 %s/%s 段失败（跳过）：%s", i + 1, total, e)

    workers = min(3, total)
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="ep-summary") as pool:
            list(pool.map(lambda a: _one(*a), list(enumerate(chunks))))
    else:
        for i, group in enumerate(chunks):
            _one(i, group)

    merged: dict[int, dict] = {}
    for r in results:
        for seq, p in (r or {}).items():
            merged.setdefault(seq, p)
    if not merged:
        logger.warning("分集要点提炼全部失败，审核退化为原文锚模式")
        if trace_cb is not None:
            trace_cb("thought", "分集要点提炼失败",
                     "未能产出分集要点表，逐集审核将退化为「上一集出口状态 + "
                     "原文锚」模式（不影响制作）")
        return sections
    for sec in sections:
        p = merged.get(sec["num"])
        if not p:
            continue
        sec["synopsis"] = sec.get("synopsis") or _as_str(p.get("synopsis"))
        sec["beats"] = [_as_str(b) for b in (p.get("beats") or []) if _as_str(b)]
        sec["hooks"] = [h for h in (p.get("hooks") or []) if isinstance(h, dict)]
        sec["exit_hint"] = _as_str(p.get("exit_hint"))
        # 集名：原文集头为空时用模型概括的集名补位（确定性回填，不覆盖原文）
        if not (sec.get("title") or "").strip():
            sec["title"] = _as_str(p.get("title"))
    return sections


# ---------- 阶段 2：按集资产提取（18 号 §61） ----------
# 建档不碰资产（审读 → 分集规划 → 落库即结束），资产在**每集制作时增量解析**：
# 输入本集正文 + 已有资产名单，只补新增。为什么不再整部分块提取：建档时全量跑
# 一遍资产（32k 剧本实测 ~16 分钟、11 次调用）会让用户在建档阶段干等，违背
# 「一集一集做」；且后续每集还要再提一次，等于重复付费。

def _known_roster_text(known: dict) -> str:
    """已有资产名单渲染（给 LLM 看，要求不重复提取）。

    只给「名字 + 一句话提示」：给全量 persona 既浪费上下文，也会诱导模型
    重新输出这些资产的完整设定（我们要的恰恰是「别再输出它们」）。
    """
    lines: list[str] = []
    for key, label, hint_key in (("characters", "角色", "persona"),
                                 ("scenes", "场景", "description"),
                                 ("props", "道具", "description")):
        parts = []
        for it in known.get(key) or []:
            if not isinstance(it, dict):
                continue
            name = _as_str(it.get("name"))
            if not name:
                continue
            hint = _as_str(it.get(hint_key)).replace("\n", " ")[:24]
            parts.append(f"{name}（{hint}）" if hint else name)
        if parts:
            lines.append(f"{label}：" + "；".join(parts))
    return "\n".join(lines)


def _norm_asset_items(items: object, known_names: set,
                      skipped: list) -> list[dict]:
    """归一新提取的资产条目：名字去重、**名单内的一律丢弃**（确定性过滤）。

    LLM 即使把已有资产又列了一遍（提示词已明确禁止），这里也按名字逐字丢弃——
    语义判断（哪些是新的）归模型，但「名字是否已在名单里」是确定性对账，由代码
    执行，不让重复条目落库。
    """
    out: list[dict] = []
    seen: set = set()
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue
        name = _as_str(raw.get("name"))
        if not name or name in seen:
            continue
        if name in known_names:
            skipped.append(name)   # 名单内：丢弃并留痕（提示词未遵守）
            continue
        seen.add(name)
        out.append(raw)
    return out


def extract_episode_assets(ep_body: str, *, seq: int = 0, front_text: str = "",
                           known: dict | None = None, style_hint: str = "",
                           model_skill_text: str = "",
                           cancel_check: CancelCheck | None = None,
                           stream_cb: Callable[[str, str], None] | None = None,
                           trace_cb: Callable[[str, str, str], None] | None = None
                           ) -> dict:
    """按集提取资产（18 号 §61）：本集正文 + 已有名单 → **只补新增** + 别名映射。

    known：项目已有资产 `{"characters": [{"name", "persona"}], "scenes": [...],
    "props": [...]}`——摆给 LLM 要求不重复提取，返回后再由代码确定性过滤。
    front_text：前置章节（总纲/人物设定），仅第 1 集传入（这部分不属于任何一集，
    但里面的角色设定是全剧锚点，丢掉会让第 1 集的人物设定缺失）。

    返回 `{"characters", "scenes", "props", "aliases", "reused", "skipped"}`
    ——reused/skipped 只用于轨迹留痕与面板展示（确定性统计，不影响落库）。
    """
    body = (ep_body or "").strip()
    if not body:
        raise ScriptParseError("本集正文为空，无法提取资产")
    known = known or {}
    known_names = {
        key: {_as_str(it.get("name")) for it in (known.get(key) or [])
              if isinstance(it, dict) and _as_str(it.get("name"))}
        for key in ("characters", "scenes", "props")}
    roster = _known_roster_text(known)
    roster_block = (f"\n\n【已有资产名单——不要重复提取，本集若用了别的叫法请写进 aliases】\n"
                    f"{roster}" if roster else "")
    front_block = (f"\n\n【前置章节（总纲/人物设定，不属于任何一集）】\n{front_text}"
                   if (front_text or "").strip() else "")
    skill_block = f"\n\n【技能包】\n{model_skill_text}" if model_skill_text else ""
    user = (f"项目画风：{style_hint}{skill_block}{roster_block}{front_block}"
            f"\n\n提取以下剧本片段的资产（这一集）：\n\n{body}")
    _check_cancel(cancel_check)
    data = _llm_call(_prompts.ASSET_PARSE_SYSTEM_PROMPT, user, cancel_check,
                     stream_cb=stream_cb,
                     stream_label=(f"第 {seq or '?'} 集资产解析"
                                   f"（已有名单 {len(known_names['characters'])} 角色）"),
                     contract=AssetsOut)

    skipped: list = []
    chars = _norm_asset_items(data.get("characters"), known_names["characters"], skipped)
    scenes = _norm_asset_items(data.get("scenes"), known_names["scenes"], skipped)
    props = _norm_asset_items(data.get("props"), known_names["props"], skipped)

    # 场景组（32 号，2026-09-30）：组名逐字归一（strip；非串/空 → 空串 = 不入组）；
    # scene_groups 只收 name 非空的条目（env_setting 可空——落库端"只在组里为空时填"，
    # 不覆盖用户改过的设定）。语义分组归模型，这里只做确定性的形状归一。
    scene_groups = _norm_scene_groups(data.get("scene_groups"))
    for _s in scenes:
        _g = _as_str(_s.get("group")).strip()
        _s["group"] = _g if _g else ""

    # 本集新增场景内部归并（词干 → 相似度门禁 → LLM 分组，每级过内外景屏障）：
    # 单次调用里模型仍可能对同一物理地点给出两个叫法（「面馆 夜 内」/「老陈面馆
    # 夜 内」），归并只作用于**本集新增**，不碰已有场景（避免改动已有分镜的引用）
    if len(scenes) > 1:
        scene_map = {_as_str(s.get("name")): s for s in scenes
                     if _as_str(s.get("name"))}
        if len(scene_map) > 1:
            merged = _merge_scene_variants(scene_map, cancel_check,
                                           trace_cb=trace_cb)
            scenes = list(merged.values())

    # 别名映射白名单校验：to 必须在已有名单里（否则代码替换会指向不存在的资产），
    # from/to 逐字非空、kind 合法——不满足即丢弃并留痕（不猜语义）
    aliases: list = []
    bad_alias: list = []
    for raw in data.get("aliases") or []:
        if not isinstance(raw, dict):
            continue
        kind = _as_str(raw.get("kind")).strip()
        src = _as_str(raw.get("from")).strip()
        dst = _as_str(raw.get("to")).strip()
        key = {"character": "characters", "scene": "scenes",
               "prop": "props"}.get(kind)
        if not key or not src or not dst or src == dst:
            bad_alias.append(f"{kind}:{src}→{dst}")
            continue
        if dst not in known_names[key]:
            bad_alias.append(f"{kind}:{src}→{dst}（目标不在名单）")
            continue
        aliases.append({"kind": kind, "from": src, "to": dst})

    # 复用统计（确定性逐字包含）：本集正文里出现了哪些已有资产名——供面板
    # 显示「复用 3 个」，让用户看得见「没有重复解析」
    reused = {key: [n for n in sorted(known_names[key]) if n and n in body]
              for key in ("characters", "scenes", "props")}
    if trace_cb is not None:
        trace_cb("thought", f"第 {seq or '?'} 集资产解析完成",
                 f"新增角色 {len(chars)}、场景 {len(scenes)}、道具 {len(props)}；"
                 f"复用已有：角色 {len(reused['characters'])}、场景 {len(reused['scenes'])}、"
                 f"道具 {len(reused['props'])}；别名映射 {len(aliases)} 条")
        if skipped:
            trace_cb("thought", "已有资产被重复输出（已丢弃）",
                     "；".join(skipped[:10])
                     + "——提示词要求只输出新增，这些名字已在名单里，未落库")
        if bad_alias:
            trace_cb("thought", "别名映射被丢弃",
                     "；".join(bad_alias[:10])
                     + "——from/to 必须逐字且 to 须在已有名单内（代码不做语义近似）")
    # 补充视角建议（2026-09-29 用户口径：资产解析阶段**只提示、不生成**，
    # 是否生成由用户决定）：取值与分镜侧 state.view_ref、挂载侧共用同一白名单
    # （app/drama.SCENE_EXTRA_VIEWS），场景名逐字对账——三段对齐才谈得上可用
    view_hints = _norm_view_hints(
        data.get("view_hints"), known_names.get("scenes") or [],
        [str(x.get("name") or "") for x in scenes])
    if view_hints and trace_cb is not None:
        trace_cb("thought", "补充视角建议",
                 "；".join(f"{h['scene']} → "
                           f"{'反打视角' if h['view'] == 'reverse' else '交互区特写'}"
                           f"（{h['reason']}）" for h in view_hints[:5])
                 + "——**只是建议**：到「资产 → 场景」里点生成即可（系统不会自动生成）")
    return {"characters": chars, "scenes": scenes, "props": props,
            "aliases": aliases, "reused": reused, "skipped": skipped,
            "view_hints": view_hints, "scene_groups": scene_groups}


def _norm_scene_groups(raw: object) -> list[dict]:
    """归一场景组清单（32 号）：name 去重、env_setting 字符串化；脏条目丢弃。"""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = _as_str(item.get("name")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name,
                    "env_setting": _as_str(item.get("env_setting")).strip()})
    return out

# 场景名尾段的状态词（时间/内外景/天气）——「地点+时间」命名策略下状态词
# 落在名尾，按整段匹配剔除得地点词干（不做任意位置子串剔除：「夜上海」
# 「风铃巷」这类含状态字的地名不能误伤）
_SCENE_STATE_TOKENS = (
    "夜", "日", "晨", "昏", "清晨", "凌晨", "清晨", "早晨", "上午", "中午",
    "午后", "下午", "傍晚", "黄昏", "深夜", "午夜", "白天", "夜晚", "夜里",
    "内", "外", "内景", "外景", "室内", "室外", "雨", "雪", "雾", "大风",
    "夜雨", "雨夜", "雨夜", "暴雨", "大雨", "小雨", "中雨", "雪夜", "风雪", "雨雪",
)


def _scene_stem(name: str) -> str:
    """场景名地点词干：剔除尾段的时间/内外景/天气状态词与分隔符。"""
    segments = [s for s in re.split(
        r"[\s（）()【】\[\]·、，,：:—\-]+", (name or "").strip()) if s]
    while segments and segments[-1] in _SCENE_STATE_TOKENS:
        segments.pop()
    return "".join(segments)


def _llm_scene_groups(scenes: dict[str, dict],
                      cancel_check: CancelCheck | None) -> list[list[str]]:
    """LLM 场景分组（同地点别名/简称识别，词干归一覆盖不了的变体）；
    失败返回空列表（保持不归并，不阻断解析）。

    payload 携带 layout（空间布局声明）：归并判定能看到空间信息——布局矛盾
    的候选在分组阶段即可跳过（审计 13 号 #1 补充）。
    """
    payload = {"scenes": [
        {"name": n, "description": (v.get("description") or "")[:100],
         **({"layout": (v.get("layout") or "")[:80]}
            if (v.get("layout") or "").strip() else {})}
        for n, v in scenes.items()]}
    try:
        data = _llm_call(_prompts.SCENE_MERGE_SYSTEM_PROMPT,
                         json.dumps(payload, ensure_ascii=False), cancel_check,
                         contract=GroupsOut)
    except ScriptParseError as e:
        logger.warning("场景分组归并失败（保持不归并）：%s", e)
        return []
    groups: list[list[str]] = []
    for g in data.get("groups") or []:
        if isinstance(g, list):
            names = [x for x in g if isinstance(x, str) and x in scenes]
            if len(names) > 1:
                groups.append(names)
    return groups


# 场景名中的内外景标记（「… 内」/「… 外」）——内外景是不同拍摄场景，
# 参考图语义不同（店内陈设 vs 门口街景），跨内外景合并是过度归并
_SCENE_INT_EXT_RE = re.compile(r"(?:^|[\s（）()·、，,])(内|室内|外|室外)(?:$|[\s（）()·、，,])")


def _scene_int_ext(name: str) -> str:
    """场景内外景类别：in / out / ""（无标记）。"""
    m = _SCENE_INT_EXT_RE.search(f" {name.strip()} ")
    if not m:
        return ""
    return "in" if m.group(1) in ("内", "室内") else "out"


def _partition_by_int_ext(names: list[str]) -> list[list[str]]:
    """按内外景分组（归并屏障）：内/外不同组；无标记的并入首个有标记组。

    实测事故：LLM 分组把「老陈面馆 夜 小雨 内」与「面馆门口 夜 小雨 外」
    判为同地点——内外景切换是转场不是同场景，确定性屏障兜底。
    """
    by_class: dict[str, list[str]] = {}
    unmarked: list[str] = []
    for n in names:
        cls = _scene_int_ext(n)
        if cls:
            by_class.setdefault(cls, []).append(n)
        else:
            unmarked.append(n)
    groups = list(by_class.values())
    if not groups:
        return [unmarked] if unmarked else []
    groups[0].extend(unmarked)
    return groups


def _merge_scene_group(scenes: dict[str, dict], names: list[str],
                       trace_cb: Callable[[str, str, str], None] | None = None) -> None:
    """把一组同地点变体名就地合并为一条场景卡（保留最短名作锚点）。

    layout（空间法典）保留首个非空值——早期实现整个字段被丢弃，导致「人物进出
    只能经由门」的空间约束在归并后的场景上全部失效（审计 13 号 #1）。各变体
    layout 不一致时记 gate 告警（不静默取首个：矛盾属语义判断，交人工确认）。
    """
    canonical = min(names, key=len)
    descs = list(dict.fromkeys(
        (scenes[n].get("description") or "").strip() for n in names
        if (scenes[n].get("description") or "").strip()))
    prompt = next(((scenes[n].get("image_prompt") or "").strip()
                   for n in names
                   if (scenes[n].get("image_prompt") or "").strip()), "")
    layouts = list(dict.fromkeys(
        (scenes[n].get("layout") or "").strip() for n in names
        if (scenes[n].get("layout") or "").strip()))
    if len(layouts) > 1:
        _gate(trace_cb, "场景归并布局冲突",
              f"{canonical}：{'、'.join(names)} 的布局声明不一致，已保留首条，"
              f"请人工确认（{' ｜ '.join(x[:40] for x in layouts[:2])}）")
    layout = layouts[0] if layouts else ""
    # 场景组（32 号）：合并的变体是同一地点，必然同属一个真实空间——把组名带过来
    # （首个非空；冲突时留痕交人工确认，与 layout 的处理同规格，不静默取首个）
    groups = list(dict.fromkeys(
        (scenes[n].get("group") or "").strip() for n in names
        if (scenes[n].get("group") or "").strip()))
    if len(groups) > 1:
        _gate(trace_cb, "场景归并组冲突",
              f"{canonical}：{'、'.join(names)} 的场景组不一致"
              f"（{'、'.join(groups)}），已保留首条，请人工确认")
    # 状态差异标注：被合并的变体名列进描述，时间/内外景信息供分镜取用
    variants = [n for n in names if n != canonical]
    if variants:
        descs.append("状态变体：" + "、".join(variants))
    row = {"name": canonical,
           "description": "；".join(descs),
           "image_prompt": prompt,
           "layout": layout,
           "group": groups[0] if groups else ""}
    for n in names:
        scenes.pop(n)
    scenes[canonical] = row


def _has_similar_scene_pair(names: list[str]) -> bool:
    """名单中任意两个场景名共享 ≥2 连续汉字才值得 LLM 分组。

    小剧本解析时长的大头是 LLM 调用次数——完全不相干的地名（「面馆 夜」/
    「雨巷 日」无公共片段）调 LLM 分组是纯浪费；有公共片段（「老周面馆」/
    「面馆」）才可能是同地点简称变体。
    """
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for k in range(len(a) - 1):
                if a[k:k + 2] in b:
                    return True
    return False


def _merge_scene_variants(scenes: dict[str, dict],
                          cancel_check: CancelCheck | None,
                          trace_cb: Callable[[str, str, str], None] | None = None) -> dict[str, dict]:
    """场景地点锚点归并（B1/B2）：同一物理地点的变体名合并为一条场景卡。

    「地点+时间」命名策略 + 分块提取下，同一地点产出多条场景卡（多张主图
    → 跨镜陈设漂移）。两级归并（先词干后 LLM——LLM 在词干归并后的名单上
    分组，避免引用将被词干组消化的名字被误跳过）：①确定性问题态词词干
    （「老周面馆 夜」/「老周面馆（夜）」/「老周面馆 夜 内」同词干）；
    ②LLM 分组（简称/别名，仅在存在公共片段的名对时才调用——无相似对
    跳过省一次 LLM 调用）。合并保留最短名作锚点（最接近纯地点名），
    状态差异并入 description（shuohao novel-art「地点锚点 + 光照状态变体」
    方法论），image_prompt 取首个非空。
    """
    if len(scenes) < 2:
        return scenes
    merged = 0
    by_stem: dict[str, list[str]] = {}
    for name in scenes:
        by_stem.setdefault(_scene_stem(name), []).append(name)
    for names in by_stem.values():
        if len(names) > 1:
            # 内外景屏障：同词干但内/外不同的不合并（不同拍摄场景）
            for sub in _partition_by_int_ext(names):
                if len(sub) > 1:
                    _merge_scene_group(scenes, sub, trace_cb)
                    merged += 1
    # LLM 分组兜底：词干归一覆盖不了的变体（简称/别名/不同措辞）——
    # 相似度门禁（无公共片段跳过省调用）+ 内外景屏障（同上）
    if _has_similar_scene_pair(list(scenes)):
        for g in _llm_scene_groups(scenes, cancel_check):
            valid = [n for n in g if n in scenes]
            for sub in _partition_by_int_ext(valid):
                if len(sub) > 1:
                    _merge_scene_group(scenes, sub, trace_cb)
                    merged += 1
    if merged:
        logger.info("场景地点归并：%s 组变体合并为地点锚点", merged)
    return scenes


def _norm_view_hints(raw_hints, known_scenes: list[str],
                     new_scenes: list[str]) -> list[dict]:
    """补充视角建议归一（只提示，不生成）：白名单取值 + 场景名逐字对账。

    - 取值只认 `drama.SCENE_EXTRA_VIEWS`（reverse/interaction）——与分镜侧的
      `state.view_ref`、挂载侧共用同一套取值（**三段对齐**，改了这里要三处一起改）；
    - 场景名必须逐字落在「本次新提取的场景 ∪ 已有场景名单」里（对账，非语义近似），
      名单外一律丢弃并告警；
    - 同一场景同一视角去重。宁缺毋滥：空表完全正常。
    """
    from app.api.drama import SCENE_EXTRA_VIEWS

    valid = {str(n).strip() for n in (list(new_scenes or []) + list(known_scenes or []))
             if str(n).strip()}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_hints or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("scene") or "").strip()
        view = str(item.get("view") or "").strip().lower()
        if not name or view not in SCENE_EXTRA_VIEWS:
            logger.warning("补充视角建议丢弃（字段缺失/取值非法）：%s", str(item)[:80])
            continue
        if name not in valid:
            logger.warning("补充视角建议丢弃（场景不在名单）：%s", name)
            continue
        if (name, view) in seen:
            continue
        seen.add((name, view))
        out.append({"scene": name, "view": view,
                    "reason": str(item.get("reason") or "")[:120]})
    return out


def extract_costume_suggestions(known_characters: list[dict] | list[str],
                                episode_sections: list[dict],
                                style_hint: str,
                                total_eps: int,
                                cancel_check: CancelCheck | None = None,
                                trace_cb: Callable[[str, str, str], None] | None = None,
                                ) -> list[dict]:
    """妆造建议提取（解析管线新增环节）：LLM 从剧本提取换装/造型事件。

    稳定性校验层层把关（被解析出来的妆造有问题就没有意义）：
    - 角色名必须与资产名单逐字一致（名单外丢弃）；
    - episodes 必须为 int 且落在总集数内（越界整条丢弃）；
    - appearance_desc 经姿态词清洗、清洗后为空丢弃；
    - evidence（原文依据）为空丢弃；
    - 同角色同妆造名去重。
    无命中返回空列表（宁缺毋滥）。
    """
    payload = {
        "characters": [
            # 提示词要求「appearance_desc 继承角色 appearance」——原先 payload 只送
            # name/persona，模型拿不到它要继承的东西（契约对账，25 号 0.3）。
            # 兼容 dict（含 persona/appearance）与纯名字两种形态：原注解写 list[str]
            # 而这里却解包 `for n, p in ...`，真接上去会 ValueError，且**名字恰好
            # 2 字时会被静默拆成两个字符**（潜在崩溃，同批修）。
            {"name": str(c.get("name") or "").strip(),
             "persona": str(c.get("persona") or ""),
             "appearance": str(c.get("appearance") or "")}
            if isinstance(c, dict) else
            {"name": str(c or "").strip(), "persona": "", "appearance": ""}
            for c in (known_characters or [])
            if (c.get("name") if isinstance(c, dict) else c)],
        # 分集结构兼容：主链路 episode_sections 元素为 {num, title, body}
        #（无 seq 字段），按 num 取集号并以序号兜底
        "episodes": [{"seq": ep.get("seq") or ep.get("num") or (i + 1),
                      "content": ep["body"][:6000]}
                     for i, ep in enumerate(episode_sections)],
        "style": style_hint,
    }
    try:
        data = _llm_call(_prompts.COSTUME_SUGGEST_SYSTEM_PROMPT,
                         json.dumps(payload, ensure_ascii=False), cancel_check,
                         contract=CostumeOut)
    except ScriptParseError as e:
        logger.warning("妆造建议提取失败（跳过，不影响解析）：%s", e)
        return []
    # 名单校验：与上方 payload 同款兼容 dict / 纯名字两种形态。历史实现写的是
    # `for n, _p in known_characters`——传 dict 名单时**直接 ValueError**
    # （3 个键解不出 2 个变量），传纯名字时又会把两字名字拆成两个字符。
    # 接线（25 号 §8 待办）之前先把这半个坑填上。
    valid_names = {
        str((c.get("name") if isinstance(c, dict) else c) or "").strip()
        for c in (known_characters or [])
        if (c.get("name") if isinstance(c, dict) else c)}
    suggestions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in data.get("suggestions") or []:
        if not isinstance(item, dict):
            continue
        name = _as_str(item.get("character"))
        desc = _prompts.strip_pose_words(_as_str(item.get("appearance_desc")))
        evidence = _as_str(item.get("evidence"))
        cname = _as_str(item.get("name"))[:12]
        if name not in valid_names or not desc or not evidence or not cname:
            logger.warning("妆造建议丢弃（字段缺失/角色不在名单）：%s", str(item)[:80])
            continue
        episodes = [e for e in (item.get("episodes") or [])
                    if isinstance(e, int) and 1 <= e <= total_eps]
        if not episodes:
            logger.warning("妆造建议丢弃（集数越界）：%s", str(item)[:80])
            continue
        key = (name, cname)
        if key in seen:
            continue
        seen.add(key)
        suggestions.append({"id": f"s{len(suggestions) + 1}", "character": name,
                            "name": cname, "episodes": sorted(set(episodes)),
                            "appearance_desc": desc, "evidence": evidence})
    if suggestions and trace_cb is not None:
        trace_cb("thought", "妆造建议提取完成",
                 f"提取 {len(suggestions)} 条妆造建议："
                 + "；".join(f"{s['character']}·{s['name']}（第{s['episodes']}集）"
                             for s in suggestions))
    return suggestions


# ---------- 阶段 3：分集分镜 + 确定性 1:1 对齐 ----------

# 景别五层（远景交代环境→全景交代动作→中景建立关系→近景传递情绪→特写放大重点）
SCALE_ORDER = ("远景", "全景", "中景", "近景", "特写")


def scale_of(camera: str) -> str | None:
    """从 camera 自由文本中识别景别层级（远>全>中>近>特，取首个命中）。"""
    for s in SCALE_ORDER:
        if s in (camera or ""):
            return s
    return None


def _check_scale_rhythm(episodes: list[dict],
                        trace_cb: Callable[[str, str, str], None] | None) -> None:
    """景别五层序列质检（确定性检查，写 trace 提示、不阻断解析）：
    首镜是否先交代环境（远景/全景开场），景别分布是否单调（单一景别直连
    会显得像"会动的 PPT"）。"""
    if trace_cb is None:
        return
    for ep in episodes:
        scales = [scale_of(s.get("camera", "")) for s in ep["shots"]]
        known = [s for s in scales if s]
        if not known:
            continue
        issues: list[str] = []
        if scales[0] is not None and known[0] in ("近景", "特写"):
            issues.append("首镜直接近景/特写，建议先以远景或全景交代环境与人物关系")
        dist = Counter(known)
        top_scale, top_n = dist.most_common(1)[0]
        if top_n / len(known) > 0.6:
            issues.append(f"{top_scale}占比 {top_n}/{len(known)}，景别偏单调")
        dist_text = "、".join(f"{s}×{n}" for s, n in dist.most_common())
        verdict = "；".join(issues) if issues else "景别节奏合格"
        trace_cb("thought",
                 f"{with_title_mark(ep['title'], ep.get('seq'))}景别节奏检查",
                 f"景别分布：{dist_text}。{verdict}")
        _gate(trace_cb, "景别节奏",
              f"{with_title_mark(ep['title'], ep.get('seq'))}{verdict}")

def decompose_episode(ep_title: str, ep_body: str, style_hint: str,
                      cancel_check: CancelCheck | None = None,
                      progress_cb: ProgressCallback | None = None,
                      skill_text: str = "",
                      known_characters: list[str] | None = None,
                      known_scenes: list[str] | None = None,
                      known_props: list[str] | None = None,
                      trace_cb: Callable[[str, str, str], None] | None = None,
                      stream_cb: Callable[[str, str], None] | None = None,
                      prev_ep_tail: str = "",
                      prev_exit_state: str = "",
                      key_scene_note: str = "",
                      tone_hint: str = "",
                      lessons_text: str = "",
                      ) -> list[dict]:
    """单集拆解：分段 1:1 拆解 → LLM 自检-自补对齐循环 → 未对齐即报错。

    known_characters/known_scenes/known_props：资产名单注入——分镜的
    character_names/scene_name/prop_names 必须从名单逐字选取（资产库锚点
    1:1 对齐，名称对不上=画面无法映射参考图）。
    prev_ep_tail：上一集剧本结尾摘要（原文尾部）——本集开头须与其时间/空间/
    情绪自然衔接（分集并发拆解互不可见，跨集连续性靠原文锚定）。
    prev_exit_state：上一集出口状态快照（批次 2c §32.8，终审顺带产出的
    结构化锚点）——比散文尾部更精确的衔接凭据。
    key_scene_note：本集关键场次清单（稀疏导演决策层——审读标记的高潮/
    反转/打斗/大场面，这些场次拆解前先比较多个镜头方案再择优）。
    lessons_text：自学习【历史教训】段落（本项目历次解析修复的问题模式与
    反面案例）——本次拆解主动规避同类问题。
    tone_hint：剧作基调对应的**节奏档指引**（21 号批次 5，源自解析前置需求）
    ——短视频钩子驱动走快档（信息密度高、避免长留白），微电影三幕式允许
    中慢档与呼吸镜头。缺省（无需求存档的旧项目）不注入，行为不变。
    """
    segments = _split_segments(ep_body, _EPISODE_BUDGET)
    # 行号编号注入（批次 2a §32.1）：集内全局行号前缀，供 LLM 输出
    # source_line_ids 做精确对账（摘抄仍须剥掉前缀保持原文逐字）
    src_lines = _source_lines(ep_body)
    numbered = _number_segments(segments, src_lines) if src_lines else segments
    skills = f"\n\n【行业技能包·分镜遵循】\n{skill_text}" if skill_text else ""
    roster = ""
    if known_scenes or known_characters or known_props:
        roster = (f"\n【资产名单（scene_name、character_names 与 prop_names 必须严格从中"
                  f"逐字选取，一字不差，禁止自创/简写/改写）】\n"
                  f"场景：{'、'.join(known_scenes) or '（无）'}\n"
                  f"角色：{'、'.join(known_characters) or '（无）'}\n"
                  f"道具：{'、'.join(known_props) or '（无）'}")
    # 跨集衔接注入（批次 2c §32.8）：出口状态快照（结构化）+ 400 字正文锚
    # （快照未产出时回退纯散文锚——灰度兼容）
    prev_note = ""
    if prev_exit_state.strip():
        prev_note += (f"\n【上一集结尾状态快照（本集开头的人物位置/持物/时间"
                      f"必须与其一致，不得凭空跳变）】：\n{prev_exit_state}")
    if prev_ep_tail.strip():
        prev_note += (f"\n【上一集结尾（本集开头须自然衔接其时间/空间/情绪，"
                      f"不得凭空跳变）】：\n{prev_ep_tail[-400:]}")
    # 稀疏导演决策层：关键场次注入更强的镜头设计要求（普通场次跳过不注入，
    # 避免全量提质成本爆炸）
    key_note = (f"\n【本集关键场次——镜头语言要求更高（先比较后落笔：动笔前对"
                f"这些场次比较两个以上的机位/景别/信息时机方案，择优落笔；打斗"
                f"与多人大场面优先空间交代清晰的机位）】\n{key_scene_note}"
                if key_scene_note.strip() else "")
    # 剧作基调 → 节奏档（21 号批次 5）：来自解析前置需求，决定本集节奏取向
    # （快档=信息密度高/避免长留白；中慢档=允许呼吸镜头与静态凝视）
    tone_note = (f"\n【本剧节奏档（剧作基调决定）】\n{tone_hint}" if tone_hint.strip() else "")
    # 自学习【历史教训】注入（每段都带——全局行为约束，跨段不失效）
    lessons_note = f"\n{lessons_text}" if lessons_text else ""
    shots: list[dict] = []
    tail_note = ""
    for k, seg in enumerate(segments):
        _check_cancel(cancel_check)
        # 跨集衔接只注入首段：中后段离上一集结尾已远，强行回扣反而误导
        prev_part = prev_note if k == 0 else ""
        user = (f"项目画风：{style_hint}\n分集：{ep_title}{skills}{roster}"
                f"{prev_part}{key_note}{tone_note}{lessons_note}{tail_note}\n\n"
                f"逐节拍拆解以下剧本正文（行首 [行号] 是对账编号，excerpt 摘抄"
                f"原文时剥掉前缀；1:1 对齐，每行都要覆盖）：\n\n{numbered[k]}")
        data = _llm_call(_prompts.SCRIPT_PARSE_SYSTEM_PROMPT, user, cancel_check,
                         stream_cb=stream_cb,
                         stream_label=(f"拆解{with_title_mark(ep_title)}"
                                       f"第 {k + 1}/{len(segments)} 段（思考中）"),
                         contract=ShotsOut)
        seg_shots: list[dict] = []
        for raw in data.get("shots") or []:
            if not isinstance(raw, dict):
                continue
            shot = _norm_shot(raw)
            if shot["description"] or shot["dialogue"]:
                # 关键场次打标（批次 4 §35.2）：本集含关键场次时放行多切
                # （集级粒度——场次级匹配脆弱，误放行由多切占比统计兜底）
                shot["_in_key_scene"] = bool(key_scene_note.strip())
                seg_shots.append(shot)
        shots.extend(seg_shots)
        # 段间衔接：长集分多段拆解时，后续段落携带前段末尾 2 镜摘要（含
        # state 站位/道具状态——只带描述不带状态时段界两侧站位无凭据）——
        # 分段拆解互不可见，不注入则段界两侧的景别/站位/动作衔接断裂
        # （与画面描述批间衔接同语义）
        if seg_shots:
            tail = [{"description": s["description"][:60],
                     "camera": s.get("camera", ""),
                     "state": s.get("state") or {}} for s in seg_shots[-2:]]
            tail_note = (f"\n【本集前序分镜（上一拆分段落末尾 2 镜，state 为镜"
                         f"头开始时的人物站位/朝向与道具状态；本段开头的景别、"
                         f"人物站位与动作须与其自然衔接，不得凭空跳变）】\n"
                         + json.dumps(tail, ensure_ascii=False))
    return _align_loop(ep_title, ep_body, shots, style_hint,
                       cancel_check, progress_cb, skill_text,
                       known_characters or [], known_scenes or [],
                       known_props or [], trace_cb, stream_cb)


def _shot_brief(s: dict, with_state: bool = False) -> dict:
    """分镜摘要（对齐检查/补镜定位用；对齐检查仍用精简版）。

    scene_name/character_names 必须携带：对齐提示词按这两个字段判定场景/
    人物声明行的覆盖，缺失会让声明行被误判 missing 触发无谓补镜。

    with_state=True 时额外带 state/end_state：**补镜必须续上一镜的位置**，而补出来
    的镜此前根本没有 state 账本（输出契约里没这个字段）——不把相邻镜的位置给它，
    它只能凭空编一个位置，与上一镜接不上（2026-09-29）。行号对账/别名映射的载荷
    不带（逐镜全量送检，带 state 会把载荷撑大）。
    """
    out = {"seq": s.get("seq"), "excerpt": s.get("excerpt", ""),
           "description": s.get("description", ""),
           "dialogue": s.get("dialogue", []),
           "scene_name": s.get("scene_name", ""),
           "character_names": s.get("character_names", [])}
    if with_state:
        out["state"] = s.get("state") or {}
        out["end_state"] = s.get("end_state") or {}
    return out


def _unmatched_asset_names(shots: list[dict],
                           known_characters: list[str] | None,
                           known_scenes: list[str] | None,
                           known_props: list[str] | None) -> dict[str, list[str]]:
    """确定性算出分镜里「写了但不在名单中」的资产名（逐字集合差，不做语义近似）。

    结果作为 unmatched_* 喂给对齐检查 LLM，要求它**逐条**给出处理结论（能指向
    名单内同一资产的出 *_aliases 映射）。旧实现只在提示词里泛泛要求「发现名单外
    称呼就映射」，实测模型整段忽略：场景引用随后被白名单剔除，scene_ref_id 全空
    ——实测霜刃辞第1集 35 个场景引用被剔除，全部镜头既没有场景参考图、也没有
    「空间布局以参考图为准」的锚定（首帧陈设与空镜否定句随之失效）。

    语义判断仍归 LLM：这里只做「字符串是否逐字出现在名单里」的确定性对账。
    """
    char_names = {c for c in (known_characters or []) if c}
    scene_names = {s for s in (known_scenes or []) if s}
    prop_names = {p for p in (known_props or []) if p}
    out: dict[str, list[str]] = {"scenes": [], "characters": [], "props": []}
    for s in shots:
        sn = _as_str(s.get("scene_name"))
        if sn and sn not in scene_names and sn not in out["scenes"]:
            out["scenes"].append(sn)
        for c in s.get("character_names") or []:
            c = _as_str(c)
            if c and c not in char_names and c not in out["characters"]:
                out["characters"].append(c)
        for p in s.get("prop_names") or []:
            p = _as_str(p)
            if p and p not in prop_names and p not in out["props"]:
                out["props"].append(p)
    return out


def _align_loop(ep_title: str, ep_body: str, shots: list[dict], style_hint: str,
                cancel_check: CancelCheck | None = None,
                progress_cb: ProgressCallback | None = None,
                skill_text: str = "",
                known_characters: list[str] | None = None,
                known_scenes: list[str] | None = None,
                known_props: list[str] | None = None,
                trace_cb: Callable[[str, str, str], None] | None = None,
                stream_cb: Callable[[str, str], None] | None = None,
                ) -> list[dict]:
    """LLM 自检-自补对齐循环（语义判定覆盖，代码只做缓存与执行）。

    每轮：LLM 检查未对齐行是否已被分镜覆盖 → 漏行交 LLM 补镜（含插入位置）
    → 补镜自报覆盖行加入缓存（aligned）→ 下一轮只复核剩余行。
    有界性：最多 _MAX_ALIGN_ROUNDS 轮；missing 不再减少提前退出；
    循环结束后仍有漏行 → 直接抛错交用户重试（质量优先，不做机械插入）。
    """
    src = _source_lines(ep_body)
    for i, s in enumerate(shots, start=1):
        s["seq"] = i
    if not src or not shots:
        return shots

    aligned: set[int] = set()
    # 非画面行（字幕/音效/音乐/画面黑场等演出指示）：LLM 判定、代码按行号豁免
    # 认领。行覆盖对账要求「每条源行都要有镜认领」，对画面行成立，对演出指示
    # 行不成立——不豁免就会把每条字幕/音效逼成一个独立镜头（实测霜刃辞第1集
    # 42 镜里 6 个是废镜）
    non_visual: set[int] = set()
    aliases: dict[str, str] = {}
    prop_aliases: list[dict] = []   # LLM 语义判断的道具别名映射（逐字应用）
    scene_aliases: list[dict] = []  # LLM 语义判断的场景别名映射
    prev_missing: int | None = None
    filled_total = 0   # 对齐循环累计补镜数（门禁统计用）
    gap_total = 0      # 台词确定性兜底累计数
    by_idx = {i: raw for (i, _k, _sp, raw) in src}

    # 结构化行号对账前置（批次 2a §32.1）：全部分镜都带 source_line_ids 键时
    # （_norm_shot 恒产出；手工构造的旧格式无此键 → 灰度回退字符串对账），
    # 先做重复仲裁 + 行号缺口精确补镜 + 台词悬空修复；随后既有 LLM 对齐
    # 循环降级为语义防御层（结构化认领预置进 aligned，不重复送检——防对
    # 同一行补出冗余镜）
    structured = bool(shots) and all("source_line_ids" in s for s in shots)
    qa_dup = qa_filled = qa_repaired = 0
    if structured:
        qa_dup = arbitrate_line_claims(src, shots)
        for _ in range(2):
            qa = storyboard_line_qa(src, shots)
            if not qa["structured"] or not qa["missing"]:
                break
            missing_idx = [i for (i, _r) in qa["missing"]]
            first, last = missing_idx[0], missing_idx[-1]
            ctx_before = next((raw for (i, _k, _sp, raw) in reversed(src)
                               if i < first), "")
            ctx_after = next((raw for (i, _k, _sp, raw) in src if i > last), "")
            fill_payload = {"style": style_hint, "skills": skill_text,
                            "context_before": ctx_before,
                            "context_after": ctx_after,
                            "lines": [{"i": i, "text": by_idx[i]}
                                      for i in missing_idx],
                            "shots": [_shot_brief(s, with_state=True)
                                      for s in shots],
                            "characters": list(known_characters or []),
                            "scenes": list(known_scenes or []),
                            "props": list(known_props or [])}
            try:
                fill = _llm_call(_prompts.SHOT_FILL_SYSTEM_PROMPT,
                                 json.dumps(fill_payload, ensure_ascii=False),
                                 cancel_check, stream_cb=stream_cb,
                                 stream_label=(f"行号缺口补镜"
                                               f"{with_title_mark(ep_title)}（思考中）"),
                                 contract=ShotsOut)
            except ScriptParseError as e:
                logger.warning("《%s》行号缺口补镜失败：%s", ep_title, e)
                break
            # 非画面行豁免（语义判断归 LLM、豁免执行归代码）：LLM 在补镜输出里
            # 标注哪些漏行根本不是画面内容（字幕/音效/音乐/画面黑场/旁白提示等
            # 演出指示），代码只接受**本次送检行号**内的声明并逐号放行——未送检
            # 的幻觉行号一律忽略（与 covered 同一道防线）。被豁免的行不再要求有
            # 镜认领，也不会被逼成一个只有字幕文本的空壳镜头。
            missing_set = set(missing_idx)
            for x in fill.get("non_visual") or []:
                i = _as_int(x, -1, 0, 10 ** 7)
                if i in missing_set:
                    non_visual.add(i)
                    aligned.add(i)
            for item in fill.get("shots") or []:
                if not isinstance(item, dict):
                    continue
                shot = _norm_shot(item)
                if not (shot["description"] or shot["dialogue"]
                        or shot.get("excerpt")):
                    continue
                after = _as_int(item.get("after_seq"), len(shots), 0, len(shots))
                shots.insert(min(after, len(shots)), shot)
                qa_filled += 1
                # 补镜认领回填：文本命中行号写入 source_line_ids（预置对齐）
                s_norm = _norm_text((shot.get("excerpt") or "")
                                    + shot["description"])
                dlg_norm = _norm_text("".join(
                    f"{d.get('speaker', '')}：{d.get('text', '')}"
                    for d in shot["dialogue"]))
                matched: list[int] = []
                for (i, k, _sp, raw) in src:
                    n = _norm_text(raw)
                    if not n:
                        continue
                    if k == "dlg":
                        if n in dlg_norm or n[:20] in dlg_norm:
                            matched.append(i)
                    elif n in s_norm or n[:20] in s_norm:
                        matched.append(i)
                shot["source_line_ids"] = sorted(set(matched))
        for i, s in enumerate(shots, start=1):
            s["seq"] = i
        # 台词悬空修复（§33.1 检查项 1 修复轮）：只许改 description，
        # dialogue/speaker/scene_name/duration 冻结（防修复越权改剧情）
        for _ in range(2):
            qa = storyboard_line_qa(src, shots)
            if not qa["structured"] or not qa["suspended"]:
                break
            suspended = set(qa["suspended"])
            repair_shots = [{"seq": s["seq"], "dialogue": s["dialogue"],
                             "character_names": s.get("character_names") or [],
                             "scene_name": s.get("scene_name", ""),
                             "excerpt": s.get("excerpt", ""),
                             "state": s.get("state") or {}}
                            for s in shots if s.get("seq") in suspended]
            first_seq = repair_shots[0]["seq"] if repair_shots else 1
            payload = {"shots": repair_shots,
                       "context_before": next(
                           (s.get("excerpt", "") for s in shots
                            if s["seq"] == first_seq - 1), ""),
                       "context_after": next(
                           (s.get("excerpt", "") for s in shots
                            if s["seq"] == first_seq + 1), "")}
            try:
                data = _llm_call(_prompts.SHOT_DESC_REPAIR_SYSTEM_PROMPT,
                                 json.dumps(payload, ensure_ascii=False),
                                 cancel_check, stream_cb=stream_cb,
                                 stream_label=(f"台词悬空修复"
                                               f"{with_title_mark(ep_title)}（思考中）"))
            except ScriptParseError as e:
                logger.warning("《%s》台词悬空修复失败：%s", ep_title, e)
                break
            desc_by_seq = {_as_int(it.get("seq"), -1, 1, 10 ** 6):
                           _as_str(it.get("description"))
                           for it in (data.get("shots") or [])
                           if isinstance(it, dict)}
            for s in shots:
                new_desc = desc_by_seq.get(s.get("seq"))
                if s.get("seq") in suspended and new_desc:
                    s["description"] = new_desc
                    qa_repaired += 1
        _gate(trace_cb, "行号对账",
              f"重复仲裁 {qa_dup} 处，精确补镜 {qa_filled} 个，"
              f"悬空修复 {qa_repaired} 镜")
        if trace_cb is not None and (qa_dup or qa_filled or qa_repaired):
            trace_cb("thought", "行号对账（结构化）",
                     f"重复认领仲裁 {qa_dup} 处（后镜复演让位），"
                     f"按行号缺口精确补镜 {qa_filled} 个，"
                     f"台词悬空镜修复 {qa_repaired} 镜（只补画面描述，"
                     "其余字段冻结）")
        # 结构化认领预置进对齐缓存：LLM 对齐循环只复核剩余行（防御层）。
        # 认领缺口按**文本命中**回填——镜实际覆盖了该行、只是 LLM 漏报
        # source_line_ids，不回填就会被误判缺口、补出冗余镜。
        #
        # 旧实现只对**零认领镜**回填（`if s.get("source_line_ids"): continue`）：
        # 已经认领了部分行的镜，它实际覆盖的**其它**行不会被补上，那些行就永久
        # 停在「未认领」（实测霜刃辞第1集：4 条台词明明在分镜 dialogue 里，体检
        # 仍报 line_gap，并硬拦整集出片）。
        #
        # 只补**没有任何镜认领**的行，且认领给**第一个**文本命中的镜——与
        # arbitrate_line_claims 的「后镜让位、前镜优先」同一语义，不会把仲裁
        # 结果重新变成重复认领。
        claimed = {i for s in shots for i in (s.get("source_line_ids") or [])}
        _norms = [
            (s,
             _norm_text((s.get("excerpt") or "") + (s.get("description") or "")),
             _norm_text("".join(
                 f"{d.get('speaker', '')}：{d.get('text', '')}"
                 for d in s.get("dialogue") or [])))
            for s in shots]
        _backfilled = 0
        for (i, k, _sp, raw) in src:
            if i in claimed:
                continue
            n = _norm_text(raw)
            if not n:
                continue
            for s, s_norm0, dlg_norm0 in _norms:
                hit = (n in dlg_norm0 or n[:20] in dlg_norm0) if k == "dlg" \
                    else (n in s_norm0 or n[:20] in s_norm0)
                if hit:
                    s.setdefault("source_line_ids", []).append(i)
                    s["source_line_ids"] = sorted(set(s["source_line_ids"]))
                    claimed.add(i)
                    _backfilled += 1
                    break
        for s in shots:
            aligned.update(s.get("source_line_ids") or [])
        if _backfilled and trace_cb is not None:
            trace_cb("thought", "认领回填",
                     f"按文本命中补认领 {_backfilled} 行（镜覆盖了该行但 LLM 漏报行号）")

    for round_no in range(_MAX_ALIGN_ROUNDS):
        _check_cancel(cancel_check)
        _report(progress_cb, round_no / _MAX_ALIGN_ROUNDS,
                f"对齐检查 第 {round_no + 1}/{_MAX_ALIGN_ROUNDS} 轮")
        pending = [(i, raw) for (i, _k, _sp, raw) in src if i not in aligned]
        if not pending:
            break
        # 仅剩声明行未核（批次 2a 三分类）：由 scene_name/character_names
        # 承接、不参与硬裁决，豁免一轮 LLM 检查调用
        _kind_of = {i: k for (i, k, _sp, _raw) in src}
        if all(_kind_of.get(i) == "decl" for (i, _raw) in pending):
            aligned.update(i for (i, _raw) in pending)
            break
        if not shots:
            # 有剧本内容却零分镜：LLM 返回空 shots 时不得静默生成空集
            raise ScriptParseError(
                f"{with_title_mark(ep_title)}分镜拆解结果为空：剧本有 {len(src)} 行内容"
                f"却未拆出任何分镜，请重新解析重试")
        # 1. LLM 语义检查（只送未对齐行，分镜全量供判定）；名单同步供
        # 道具/场景别名映射（语义判断归 LLM，代码只逐字应用映射）
        unmatched = _unmatched_asset_names(
            shots, known_characters, known_scenes, known_props)
        payload = {"lines": [{"i": i, "text": raw} for i, raw in pending],
                   "shots": [_shot_brief(s) for s in shots],
                   "characters": list(known_characters or []),
                   "scenes": list(known_scenes or []),
                   "props": list(known_props or []),
                   # 代码逐字比对算出的「写了但不在名单里」清单：要求 LLM 逐条给
                   # 结论（进 aliases 或明确不动），而不是泛泛地「自行发现」
                   "unmatched_scene_names": unmatched["scenes"],
                   "unmatched_character_names": unmatched["characters"],
                   "unmatched_prop_names": unmatched["props"]}
        try:
            check = _llm_call(_prompts.SHOT_ALIGN_CHECK_SYSTEM_PROMPT,
                              json.dumps(payload, ensure_ascii=False),
                              cancel_check, stream_cb=stream_cb,
                              stream_label=(f"对齐检查{with_title_mark(ep_title)}"
                                            f"第 {round_no + 1} 轮（思考中）"))
        except ScriptParseError as e:
            logger.warning("《%s》对齐检查第 %s 轮失败：%s", ep_title, round_no + 1, e)
            break
        pending_ids = {i for (i, _raw) in pending}
        # 只接受本轮送检行号的覆盖判定——LLM 幻觉行号不得误标未送检行为已对齐
        aligned |= {i for i in ({_as_int(x, -1, 0, 10 ** 7)
                                 for x in check.get("covered") or []})
                    if i in pending_ids}
        # 非画面行豁免（同补镜通道：只认本轮送检行号内的声明）
        for x in check.get("non_visual") or []:
            i = _as_int(x, -1, 0, 10 ** 7)
            if i in pending_ids:
                non_visual.add(i)
                aligned.add(i)
        aliases.update({
            _as_str(a.get("from")): _as_str(a.get("to"))
            for a in check.get("character_aliases") or []
            if isinstance(a, dict) and _as_str(a.get("from")) and _as_str(a.get("to"))
        })
        # 道具/场景语义别名（批次 4 补充）：LLM 判断「不锈钢保温桶」≈「保温桶」
        # 这类同物异名，代码只逐字应用映射（不猜语义）
        for a in check.get("prop_aliases") or []:
            if isinstance(a, dict) and _as_str(a.get("from")) and _as_str(a.get("to")):
                prop_aliases.append({"from": _as_str(a.get("from")),
                                     "to": _as_str(a.get("to"))})
        for a in check.get("scene_aliases") or []:
            if isinstance(a, dict) and _as_str(a.get("from")) and _as_str(a.get("to")):
                scene_aliases.append({"from": _as_str(a.get("from")),
                                      "to": _as_str(a.get("to"))})
        missing_idx = [i for (i, _k, _sp, _raw) in src if i not in aligned]
        if trace_cb is not None:
            preview = "；".join(by_idx[i][:20] for i in missing_idx[:4]) \
                if missing_idx else "无"
            trace_cb("thought",
                     f"{with_title_mark(ep_title)}对齐检查 第 {round_no + 1} 轮",
                     f"已覆盖 {len(aligned)} 行，未覆盖 {len(missing_idx)} 行（{preview}）；"
                     f"角色别名映射 {len(aliases)} 组")
        if not missing_idx:
            break  # 全部对齐
        if prev_missing is not None and len(missing_idx) >= prev_missing:
            logger.warning("《%s》对齐补镜无进展（%s 行），提前收尾",
                           ep_title, len(missing_idx))
            break
        prev_missing = len(missing_idx)

        # 2. LLM 补镜：漏行 + 前后源行上下文 + 现有分镜（定位）+ 资产名单
        # （scene_name/character_names 必须逐字取自名单，防止补镜产出编外锚点）
        first, last = missing_idx[0], missing_idx[-1]
        ctx_before = next((raw for (i, _k, _sp, raw) in reversed(src) if i < first), "")
        ctx_after = next((raw for (i, _k, _sp, raw) in src if i > last), "")
        fill_payload = {"style": style_hint, "skills": skill_text,
                        "context_before": ctx_before, "context_after": ctx_after,
                        "lines": [{"i": i, "text": by_idx[i]} for i in missing_idx],
                        "shots": [_shot_brief(s, with_state=True) for s in shots],
                        "characters": list(known_characters or []),
                        "scenes": list(known_scenes or []),
                        "props": list(known_props or [])}
        try:
            fill = _llm_call(_prompts.SHOT_FILL_SYSTEM_PROMPT,
                             json.dumps(fill_payload, ensure_ascii=False),
                             cancel_check, stream_cb=stream_cb,
                             stream_label=f"补镜《{ep_title[:14]}》第 {round_no + 1} 轮（思考中）",
                             contract=ShotsOut)
        except ScriptParseError as e:
            logger.warning("《%s》漏行补镜第 %s 轮失败：%s", ep_title, round_no + 1, e)
            break
        inserted = 0
        for item in fill.get("shots") or []:
            if not isinstance(item, dict):
                continue
            shot = _norm_shot(item)
            if not (shot["description"] or shot["dialogue"]
                    or shot.get("excerpt")):
                continue
            after = _as_int(item.get("after_seq"), len(shots), 0, len(shots))
            shots.insert(min(after, len(shots)), shot)
            inserted += 1
            filled_total += 1
            # 补镜自报覆盖（写缓存）：动作行 excerpt/description 命中即对齐；
            # 台词行必须在 dialogue 中命中——台词只进摘抄不进 dialogue 时
            # 成片里没有这句台词，不得视为已覆盖
            s_norm = _norm_text((shot.get("excerpt") or "") + shot["description"])
            dlg_norm = _norm_text("".join(
                f"{d.get('speaker', '')}：{d.get('text', '')}"
                for d in shot["dialogue"]))
            matched_ids: list[int] = []
            for (i, k, _sp, raw) in src:
                n = _norm_text(raw)
                if not n:
                    continue
                if k == "dlg":
                    if n in dlg_norm or n[:20] in dlg_norm:
                        aligned.add(i)
                        matched_ids.append(i)
                elif n in s_norm or n[:20] in s_norm:
                    aligned.add(i)
                    matched_ids.append(i)
            # 补镜行号回填（批次 2a）：自报覆盖的行号同步写入 source_line_ids，
            # 后续体检/precheck 的结构化判定可见
            if matched_ids:
                shot["source_line_ids"] = sorted(set(
                    (shot.get("source_line_ids") or []) + matched_ids))
        for i, s in enumerate(shots, start=1):
            s["seq"] = i
        logger.info("《%s》对齐第 %s 轮：补镜 %s 个，剩余未对齐 %s 行",
                    ep_title, round_no + 1, inserted, len(missing_idx))
        if trace_cb is not None:
            trace_cb("thought", f"《{ep_title}》对齐补镜 第 {round_no + 1} 轮",
                     f"为 {len(missing_idx)} 行未覆盖剧本补写 {inserted} 个分镜"
                     f"（已插入到原文对应位置）")

    # 角色对齐：应用 LLM 判定的别名映射（剧本称呼 → 角色名，如 少年 → 小满）。
    # 仅执行映射（改写 character_names 与 dialogue.speaker），excerpt/description 保持原文
    for from_name, to_name in aliases.items():
        for s in shots:
            s["character_names"] = [to_name if n == from_name else n
                                    for n in s["character_names"]]
            for d in s["dialogue"]:
                if d.get("speaker") == from_name:
                    d["speaker"] = to_name
    if aliases:
        logger.info("《%s》角色别名对齐：%s", ep_title, aliases)

    # ---- 别名补跑（不与「有无漏行」耦合）----
    # 对齐检查此前只在「还有未对齐行」时才调用，于是**拆解覆盖完整时整条链路
    # 不触发**，而场景/道具/人物的别名映射正是在那次调用里产出的——名单外引用
    # 一路无人处理，被白名单静默剔除（实测霜刃辞2.0 第2集：非画面行豁免后
    # pending 归空 → 对齐检查未跑 → 52 个场景引用全被剔除、scene_ref_id 全空，
    # 镜头失去场景参考图与陈设锚定）。这里按**确定性算出的名单外清单**兜一次：
    # 只要存在名单外引用，就单独跑一次别名映射（lines 传空，覆盖判定无输入）。
    # **位置约束**：必须排在下方「别名应用」之前——应用段只认此刻已收集到
    # 的映射，放它后面等于收集了却从不写进分镜（实测：7 组场景别名收集成功、
    # 场景引用仍被全数剔除）。
    _left_aliases = _unmatched_asset_names(
        shots, known_characters, known_scenes, known_props)
    if _left_aliases["scenes"] or _left_aliases["props"] or _left_aliases["characters"]:
        try:
            _alias_payload = {
                "lines": [],
                "shots": [_shot_brief(s) for s in shots],
                "characters": list(known_characters or []),
                "scenes": list(known_scenes or []),
                "props": list(known_props or []),
                "unmatched_scene_names": _left_aliases["scenes"],
                "unmatched_character_names": _left_aliases["characters"],
                "unmatched_prop_names": _left_aliases["props"]}
            _alias_check = _llm_call(
                _prompts.SHOT_ALIGN_CHECK_SYSTEM_PROMPT,
                json.dumps(_alias_payload, ensure_ascii=False),
                cancel_check, stream_cb=stream_cb,
                stream_label=(f"别名映射{with_title_mark(ep_title)}（思考中）"))
            for a in _alias_check.get("character_aliases") or []:
                if isinstance(a, dict) and _as_str(a.get("from")) and _as_str(a.get("to")):
                    aliases.setdefault(_as_str(a.get("from")), _as_str(a.get("to")))
            for a in _alias_check.get("prop_aliases") or []:
                if isinstance(a, dict) and _as_str(a.get("from")) and _as_str(a.get("to")):
                    prop_aliases.append({"from": _as_str(a.get("from")),
                                         "to": _as_str(a.get("to"))})
            for a in _alias_check.get("scene_aliases") or []:
                if isinstance(a, dict) and _as_str(a.get("from")) and _as_str(a.get("to")):
                    scene_aliases.append({"from": _as_str(a.get("from")),
                                          "to": _as_str(a.get("to"))})
            if trace_cb is not None:
                trace_cb("thought", f"{with_title_mark(ep_title)}别名映射",
                         f"名单外引用兜底映射：角色 {len(aliases)} 组、"
                         f"道具 {len(prop_aliases)} 组、场景 {len(scene_aliases)} 组")
        except ScriptParseError as e:
            logger.warning("《%s》别名映射补跑失败：%s", ep_title, e)

    # 道具/场景别名映射（批次 4 补充：语义判断归 LLM，代码逐字应用——
    # 「不锈钢保温桶」→「保温桶」「老陈面馆后厨」→「面馆 夜 内」）
    prop_alias_n = scene_alias_n = 0
    if known_props or known_scenes:
        prop_set, scene_set = set(known_props or []), set(known_scenes or [])
        for s in shots:
            new_props = []
            for pn in s.get("prop_names") or []:
                fixed = next((a["to"] for a in prop_aliases
                              if a["from"] == pn), None)
                if fixed and fixed in prop_set:
                    new_props.append(fixed)
                    prop_alias_n += 1
                else:
                    new_props.append(pn)
            s["prop_names"] = new_props
            sn = _as_str(s.get("scene_name"))
            fixed_sn = next((a["to"] for a in scene_aliases
                             if a["from"] == sn), None)
            if fixed_sn and fixed_sn in scene_set:
                s["scene_name"] = fixed_sn
                scene_alias_n += 1

    # 确定性台词兜底（模块契约「剧情零丢失，不依赖 LLM 承诺」）：对齐检查
    # LLM 误判覆盖时（台词只进摘抄未结构化进 dialogue），由代码把漏行直接
    # 补进包含该行原文的分镜——台词进 dialogue 才会出现在成片；原文无命中
    # （真正丢失）才走报错路径交用户重试
    for _gap_i, gap_raw in _dialogue_coverage_gap(src, shots):
        m = _DIALOGUE_LINE_RE.match(gap_raw.strip())
        if not m:
            continue
        speaker_raw = m.group(1).strip()
        speaker = re.sub(r"（OS）|\(OS\)", "", speaker_raw).strip()
        text = m.group(2).strip()
        n_text = _norm_text(_strip_paren(text))
        target = next((s for s in shots
                       if n_text and n_text in _norm_text(
                           (s.get("excerpt") or "") + (s.get("description") or ""))),
                      None)
        if target is None:
            continue  # 原文无命中：保持报错语义（真正丢内容不机械插入）
        target.setdefault("dialogue", []).append({
            "speaker": speaker, "text": text, "os": "OS" in speaker_raw})
        aligned.add(_gap_i)
        gap_total += 1
        logger.info("台词确定性兜底：第 %s 行补进分镜 seq=%s", _gap_i,
                    target.get("seq"))

    # 说话人确定性清洗（角色名单锚点，LLM 别名映射的最后防线）：
    # - 场景头混入 dialogue（「场景一：老周面馆 夜 内」被当说话人）→ 删除
    #   伪台词（声明不是话，原文已在 excerpt/scene_name）；
    # - 说话人串含角色名（「林晚递过写字板：…」）→ 取角色名；
    # - 无法映射的陌生说话人 → 旁白化（os=true + 空说话人：保台词不绑错
    #   音色——比错绑角色音色或丢台词都安全）
    if known_characters:
        roster = set(known_characters)
        for s in shots:
            cleaned: list[dict] = []
            for d in s.get("dialogue") or []:
                sp = _as_str(d.get("speaker"))
                if sp and sp not in roster:
                    if _is_declaration_speaker(sp):
                        continue  # 场景头声明：伪台词删除
                    hit = next((c for c in roster if c in sp), None)
                    d = {**d, "speaker": hit} if hit else {**d, "speaker": "",
                                                             "os": True}
                cleaned.append(d)
            s["dialogue"] = cleaned

    # 质量裁决：仍有漏行 → 直接报错交用户重试（不做机械插入，宁缺毋滥）。
    # decl 声明行不参与硬裁决（批次 2a 三分类：由 scene_name/character_names
    # 承接，语义覆盖由对齐检查判定，不满足裁决口径）
    final_missing = [i for (i, k, _sp, _raw) in src
                     if k in ("dlg", "act") and i not in aligned]
    _gate(trace_cb, "对齐补镜",
          f"补镜 {filled_total} 个，别名映射 {len(aliases)} 组，"
          f"非画面行豁免 {len(non_visual)} 行")
    # 仍未对上的资产名：别名映射没能覆盖的，会在后续白名单环节被**静默剔除**
    # （镜头丢参考图、丢场景锚定）。这里必须报出来——实测事故的形态正是「剔了
    # 35 个场景引用，日志只有一句计数，没人知道是哪 35 个、为什么剔」。
    left = _unmatched_asset_names(shots, known_characters, known_scenes,
                                  known_props)
    mapped = {a.get("from") for a in scene_aliases}
    mapped |= {a.get("from") for a in prop_aliases}
    left_scenes = [n for n in left["scenes"] if n not in mapped]
    left_props = [n for n in left["props"] if n not in mapped]
    left_chars = [c for c in left["characters"] if c not in aliases]
    if left_scenes or left_props or left_chars:
        detail = (f"场景 {left_scenes[:6]}（{len(left_scenes)} 个）"
                  if left_scenes else "")
        if left_props:
            detail += f"；道具 {left_props[:6]}（{len(left_props)} 个）"
        if left_chars:
            detail += f"；角色 {left_chars[:6]}（{len(left_chars)} 个）"
        _gate(trace_cb, "名单外引用", detail)
        if trace_cb is not None:
            trace_cb(
                "thought", "名单外引用将被剔除",
                f"{detail}——这些引用无法绑定参考图，白名单环节会剔除它们"
                "（该镜失去对应参考图与空间锚定）。若属同物异名，请在对齐"
                "检查中给出别名映射；若属剧本新场景，请先在资产库补录")
    if gap_total:
        _gate(trace_cb, "台词兜底", f"代码直补台词 {gap_total} 行进分镜 dialogue")
    if final_missing:
        preview = "\n".join(f"· {by_idx[i][:60]}" for i in final_missing[:5])
        raise ScriptParseError(
            f"《{ep_title}》分镜对齐校验未通过：{len(final_missing)} 行剧本未被分镜覆盖"
            f"（已自动检查补写 {_MAX_ALIGN_ROUNDS} 轮）。未覆盖片段：\n{preview}\n"
            f"请重新解析重试；若多次失败，请检查剧本格式或更换 LLM 服务")
    return shots


def _norm_shot(raw: dict) -> dict:
    """分镜归一化：字段容错、时长夹取、台词结构修正。"""
    dialogue: list[dict] = []
    for d in raw.get("dialogue") or []:
        if not isinstance(d, dict):
            continue
        speaker, text = _as_str(d.get("speaker")), _as_str(d.get("text"))
        if not text:
            continue
        dialogue.append({"speaker": speaker, "text": text,
                         "os": bool(d.get("os"))})
    # character_names 容错：LLM 偶发返回单个字符串（逐字符展开成错误名单）
    names_raw = raw.get("character_names")
    if isinstance(names_raw, str):
        names_raw = [names_raw]
    props_raw = raw.get("prop_names")
    if isinstance(props_raw, str):
        props_raw = [props_raw]
    state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
    transition = _as_str(raw.get("transition_in")).lower()
    if transition not in ("continue", "time_skip", "scene_cut"):
        transition = ""  # 非法值留空，由画面描述阶段按场景变化推断
    # 镜头目的与脆弱点（批次 2b §32.5）：枚举白名单收敛，非法值静默置空
    purpose = _as_str(raw.get("purpose")).lower()
    if purpose not in ("establish", "reaction", "turn", "insert", "transition"):
        purpose = ""
    risks_raw = raw.get("risks")
    if isinstance(risks_raw, str):
        risks_raw = [risks_raw]
    # 风险白名单（批次 4 补充 §35.2 扩展）：face/prop/text/motion/crowd 之外
    # 新增流体、镜面反射、手部交互、碰撞、物体变形——高风险动作降级拍法的
    # 标注依据（SCRIPT_PARSE 9.7 条款）
    risks = sorted({str(r).strip().lower() for r in risks_raw or []
                    if str(r).strip().lower() in
                    ("face", "prop", "text", "motion", "crowd",
                     "fluid", "mirror", "hands", "collision", "transform")})
    # **事实前提对账**（2026-09-29，用户报「空镜却挂多人同框」）：
    # 风险是模型的**自评**，而角色/道具名单是同一份输出里**经名单逐字校验过**的
    # 结构化字段——两者矛盾时以名单为准（这不是猜语义，是对账）。
    # 实测全库：crowd 43 处里 19 处角色不足 2 个（44% 是错的）、prop 46 处里 7 处
    # 根本没引用道具；face/hands 零错误。错误的标签比没有标签更糟——用户会
    # 照着它去核对一个根本不存在的风险，进而不再相信整排标签。
    _names = [_as_str(x) for x in names_raw or [] if _as_str(x)]
    _props = [_as_str(x) for x in props_raw or [] if _as_str(x)]
    for _risk, _need, _have, _what in (
        ("crowd", 2, len(_names), "登记角色"),
        ("face", 1, len(_names), "登记角色"),
        ("hands", 1, len(_names), "登记角色"),
        ("prop", 1, len(_props), "道具引用"),
    ):
        if _risk in risks and _have < _need:
            risks = [r for r in risks if r != _risk]
            logger.info("风险标签对账剔除：%s 需要至少 %d 个%s，本镜只有 %d 个"
                        "（描述：%s）", _risk, _need, _what, _have,
                        _as_str(raw.get("description"))[:40])

    def _norm_state_entry(value) -> dict:
        """状态条目归一（批次 2c §32.6）：{position, props, time[, who][, view_ref]}。

        who 仅 end_state 使用（出口在场人物集合，状态链对账判据）。
        view_ref 仅 state 使用（本镜需要的场景补充视角，见 SHOT_PROMPT k5）——
        **白名单外一律判空**，防模型自造词透传到组装侧当节点名用。
        """
        d = value if isinstance(value, dict) else {}
        out = {"position": _as_str(d.get("position"))[:300],
               "props": _as_str(d.get("props"))[:300],
               "time": _as_str(d.get("time"))[:80]}
        who_raw = d.get("who")
        if isinstance(who_raw, list):
            out["who"] = [_as_str(w) for w in who_raw if _as_str(w)]
        view_ref = _as_str(d.get("view_ref")).strip().lower()
        if view_ref in STATE_VIEW_REFS:
            out["view_ref"] = view_ref
        return out

    end_state = _norm_state_entry(raw.get("end_state"))
    # 多切关键帧（批次 4 §35.2）：[{cut, offset, description}] 确定性收敛——
    # 首切 offset=0、offset 单调递增且 < duration、切数上限 2（最小闭环）
    cuts_raw = raw.get("cuts")
    cuts: list[dict] = []
    if isinstance(cuts_raw, list):
        max_dur = _as_int(raw.get("duration"), SHOT_DURATION_MAX, 1, 10 ** 3)
        for ci, c in enumerate(cuts_raw[:2], start=1):
            if not isinstance(c, dict):
                continue
            offset = _as_int(c.get("offset"), 0, 0, max_dur - 1)
            if cuts and offset <= cuts[-1]["offset"]:
                offset = cuts[-1]["offset"] + 1
            if offset >= max_dur:
                break
            cuts.append({"cut": ci, "offset": offset,
                         "description": _as_str(c.get("description"))[:200],
                         "artifact_id": None})
        if len(cuts) < 2:
            cuts = []  # 单切/畸形态不启用多切（保持单关键帧语义）
    # source_line_ids 容错（批次 2a §32.1）：LLM 可能给 list/int/区间 dict，
    # 统一收敛为行号 int 列表；无法解析时留空 = 该镜未结构化认领（回退
    # 字符串对账的灰度开关语义）
    ids_raw = raw.get("source_line_ids")
    if isinstance(ids_raw, dict):
        ids_raw = [ids_raw.get("from"), ids_raw.get("to")]
    if isinstance(ids_raw, int):
        ids_raw = [ids_raw]
    line_ids = sorted({_as_int(x, -1, 0, 10 ** 7)
                       for x in (ids_raw or []) if isinstance(x, (int, str))
                       and _as_int(x, -1, 0, 10 ** 7) >= 0})
    return {
        "excerpt": _as_str(raw.get("excerpt")),
        "description": _as_str(raw.get("description")),
        "camera": _as_str(raw.get("camera")) or "固定",
        "duration": _as_int(raw.get("duration"), SHOT_DURATION_DEFAULT,
                            SHOT_DURATION_MIN, SHOT_DURATION_MAX),
        "scene_name": _as_str(raw.get("scene_name")),
        "character_names": [_as_str(n) for n in names_raw or []
                            if _as_str(n)],
        "prop_names": [_as_str(n) for n in props_raw or [] if _as_str(n)],
        "transition_in": transition,
        "state": {k: v for k, v in _norm_state_entry(state).items()
                  if k != "who"},
        "end_state": end_state,
        "cuts": cuts,
        "source_line_ids": line_ids,
        "purpose": purpose,
        "risks": risks,
        "dialogue": dialogue,
    }


def _dialogue_coverage_gap(src: list[tuple[int, str, str, str]],
                           shots: list[dict]) -> list[tuple[int, str]]:
    """确定性台词覆盖兜底检查：源台词行未出现在任何分镜 dialogue 中即漏行。

    对齐检查 LLM 可能把「台词只进摘抄、未结构化进 dialogue」误判为已覆盖
    （实测事故：台词行整句进了 excerpt/description 而 dialogue 为空——
    台词不进 dialogue 成片就没有这句）。判定：台词原文（去括号归一）既不
    完整出现在全部分镜 dialogue 拼接文本中、其前缀（前 12 归一字）也无
    命中（容忍 LLM 把复合句合法拆分到相邻分镜）。
    """
    dlg_blob = _norm_text("".join(
        _strip_paren(_as_str(d.get("text")))
        for s in shots for d in s.get("dialogue") or []))
    gaps: list[tuple[int, str]] = []
    for (i, kind, _sp, raw) in src:
        if kind != "dlg":
            continue
        m = _DIALOGUE_LINE_RE.match(raw.strip())
        if not m:
            continue
        text = _norm_text(_strip_paren(m.group(2)))
        if not text:
            continue
        needle = text if len(text) <= 12 else text[:12]
        if text not in dlg_blob and needle not in dlg_blob:
            gaps.append((i, raw))
    return gaps


def _norm_text(s: str) -> str:
    """归一化：去空白/括号/标点差异，用于覆盖比对。"""
    return re.sub(r'[\s（）()【】\[\]「」『』·：:，,。.！!？?；;—\-～~…"“”*]', "", s)


def _strip_paren(text: str) -> str:
    """剥离台词中的括号表演提示（允许 LLM 把（动作）移入画面描述）。"""
    return re.sub(r"[（(][^）)]*[）)]", "", text)


# ---------- 语速硬门（确定性时长下限，借鉴 shuohao「逐集时长按语速确定性折算」） ----------
# 中文配音语速 ≈ 3.5 字/秒 + 句间停顿 0.5s + 起始反应 1s——台词镜时长低于该
# 折算下限时台词必然念不完（视频被赶节奏/台词被吞），由代码强制抬升兜底
_SPEECH_RATE = 3.5            # 台词语速（字/秒）
_DIALOGUE_PAUSE_SEC = 0.5     # 句间停顿（秒/句）
_DIALOGUE_REACTION_SEC = 1.0  # 起始反应节拍（秒）
# 对话轮转系数：多句台词镜的实际表演比匀速朗读慢——换人节拍、表情与手势不占用
# 字数但占用时间（实测两句台词镜按匀速折算 9s，端点实演拖到 10.1s 且口型比
# 节拍表后移约 1.4s）。纯算术余量，不加语义判断。
_DIALOGUE_ROUND_FACTOR = 1.2


def _dialogue_duration_floor(shot: dict) -> int:
    """台词镜时长下限（秒）：台词字数按语速折算 + 停顿/反应余量；纯动作镜返回 0。

    括号表演提示不计入字数（不占用口播时间）。多句台词（对话/轮转）语速放慢
    _DIALOGUE_ROUND_FACTOR——见该常量注释。
    """
    texts = [t for t in (d.get("text") or "" for d in shot.get("dialogue") or [])
             if t.strip()]
    chars = sum(len(_strip_paren(t)) for t in texts)
    if chars <= 0:
        return 0
    pauses = (_DIALOGUE_PAUSE_SEC * max(0, len(texts) - 1)
              + _DIALOGUE_REACTION_SEC)
    rate = (_SPEECH_RATE / _DIALOGUE_ROUND_FACTOR
            if len(texts) >= 2 else _SPEECH_RATE)
    return math.ceil(chars / rate + pauses)


def _split_long_shot(shot: dict,
                     src: list[tuple[int, str, str, str]] | None = None) -> list[dict]:
    """超上限台词镜确定性拆分为链式镜头（同一叙事节拍的分段生成）。

    按台词句读贪心装段：段字符预算 =（上限时长 - 反应节拍）× 语速 - 停顿
    余量，保证每段语速折算不超上限；后续段 transition_in=continue 承接
    前段（生成链路对 continue 镜自动做尾帧→首帧衔接）。单句独白切不开时
    返回单元素列表（保持夹取语义）。段间衔接由 enrich 的 previous_shots
    状态承接（首段带 description/state，后续段以台词为主）。

    src：源行清单（`_source_lines` 产物，可空）——用于把父镜的 source_line_ids
    按台词文本归位到各子镜（认领账本守恒，审计 13 号 K）。
    """
    lines = [d for d in shot.get("dialogue") or []
             if _as_str(d.get("text"))]
    if len(lines) < 2:
        return [shot]
    # 段字符预算：单段 floor ≤ 上限（floor = chars/3.5 + 停顿 + 1s 反应）
    budget = max(8, int((SHOT_DURATION_MAX - _DIALOGUE_REACTION_SEC)
                        * _SPEECH_RATE) - 4)
    groups: list[list[dict]] = [[]]
    acc = 0
    for d in lines:
        n = len(_strip_paren(_as_str(d.get("text"))))
        if groups[-1] and acc + n > budget:
            groups.append([])
            acc = 0
        groups[-1].append(d)
        acc += n
    groups = [g for g in groups if g]
    if len(groups) < 2:
        return [shot]
    # 认领账本分配（审计 13 号 K）：父镜行号按各段台词文本归位到对应子镜——
    # 浅拷贝会让所有子镜共享同一个 source_line_ids 列表，下游仲裁原地 remove
    # 时把兄弟镜的认领一起删光（行变「无人认领」→ 补出重复分镜）
    parent_ids = list(shot.get("source_line_ids") or [])
    dlg_norm = {}
    for i, kind, _sp, raw in (src or []):  # 无源行时归位不到 → 并入首段
        if kind == "dlg":
            m = _DIALOGUE_LINE_RE.match(raw.strip())
            if m:
                dlg_norm[i] = m.group(2).strip()
    child_ids: list[list[int]] = [[] for _ in groups]
    for i in parent_ids:
        body = dlg_norm.get(i)
        if body:
            for gi, group in enumerate(groups):
                if any(_as_str(d.get("text")).strip() == body for d in group):
                    child_ids[gi].append(i)
                    break
    if not any(child_ids):
        child_ids[0] = parent_ids  # 归位不到时认领全部并入首段，保证并集守恒

    children: list[dict] = []
    for gi, group in enumerate(groups):
        # 共享的跑位/场景/角色可复用，但【会被原地改写的字段必须独立】——
        # source_line_ids/cuts/character_names/prop_names/risks 是 list，
        # state/end_state 是 dict，浅拷贝会让兄弟镜互相污染
        child = {
            **shot,
            "dialogue": [dict(d) for d in group],
            "source_line_ids": sorted(child_ids[gi]),
            "cuts": [],  # 父镜切点按父镜时长计，对分段子镜非法（曾致二切图错位）
            "character_names": list(shot.get("character_names") or []),
            "prop_names": list(shot.get("prop_names") or []),
            "risks": list(shot.get("risks") or []),
            "state": dict(shot.get("state") or {}),
            "end_state": dict(shot.get("end_state") or {}),
        }
        child["duration"] = min(SHOT_DURATION_MAX,
                                max(SHOT_DURATION_MIN,
                                    _dialogue_duration_floor(child)))
        if gi > 0:
            child["transition_in"] = "continue"
            child["description"] = ""
            child["excerpt"] = "\n".join(
                f"{_as_str(d.get('speaker'))}：{_as_str(d.get('text'))}"
                for d in group)
        children.append(child)
    return children


def _split_sentences(text: str) -> list[str]:
    """句子级分段（保留句末标点）：LLM 常用句号连写而非换行，按行分段会
    失效——整镜归一后无法与后镜比对子句。"""
    sents: list[str] = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        sents.extend(s for s in re.split(r"(?<=[。！？!?])", line) if s.strip())
    return sents


def _join_unique_lines(*texts: str) -> str:
    """多段摘抄按行归一去重后保序拼接。"""
    seen: set[str] = set()
    lines: list[str] = []
    for text in texts:
        for line in (text or "").split("\n"):
            n = _norm_text(line)
            if not n or n in seen:
                continue
            seen.add(n)
            lines.append(line.strip())
    return "\n".join(lines)


def consolidate_beats(episodes: list[dict]) -> dict:
    """节拍整合（确定性门禁）：动作与台词拆裂的镜头合并、复演句剔除。

    实测事故形态（《道具验证-深夜面馆》用户报障）：LLM 把一个叙事节拍拆成
    「无台词动作镜 + 台词镜」——端面动作单独成镜，其台词在下一镜，两镜各演
    半截（用户观感：台词本来就在动作镜里，割裂）；复演句可相邻或隔 1 镜。
    句子级分段（LLM 常用句号连写而非换行，按行分段会失效），两阶段：
    阶段一【合并】：无台词镜 A 与窗口 2 镜内有台词镜 B 共享动作句（B 的
      description 包含 A 的某句）→ **B 并入 A**（台词/摘抄/独有描述归位，
      动作与台词同镜、叙事节拍完整），合并后语速折算 ≤ 12s 才允许，超限
      保持拆分；
    阶段二【剔除】：剩余复演句（无台词镜 desc/excerpt 的句子出现在窗口内
      有台词镜中）剔除——同景别正反打/特写覆盖（有台词镜在前）不动。
    返回 {"merged": 合并镜数, "removed": 剔除句数}。
    """
    merged = removed = 0
    for ep in episodes:
        shots = ep.get("shots") or []
        # ---- 阶段一：节拍合并（相邻两镜共享同一动作句 → 并到靠前那镜） ----
        # 方向无关：**台词镜在前、动作镜在后**是同样常见的拆裂形态（实测
        # 《深夜面馆》镜2 台词+动作 desc、镜3 同一条动作 desc 无台词，旧版只
        # 向前找台词镜 → 漏合，用户看到同一动作演两遍）。两镜**都**有台词是
        # 合法正反打，不合并。
        idx = 0
        while idx < len(shots):
            a = shots[idx]
            a_sents = [(s, _norm_text(s)) for s in _split_sentences(a["description"])]
            for j in range(idx + 1, min(idx + 3, len(shots))):
                b = shots[j]
                # 只在「一有一无台词」时合并（都无台词是纯动作续接、都有台词是正反打）
                if bool(a.get("dialogue")) == bool(b.get("dialogue")):
                    continue
                # 共享动作句的判定以**无台词镜的句子 ⊆ 有台词镜 desc** 为准
                # （方向无关：无台词镜那一侧才是"被拆出来的半截"）
                plain, talk = (a, b) if not a.get("dialogue") else (b, a)
                plain_sents = (a_sents if plain is a
                               else [(s, _norm_text(s))
                                     for s in _split_sentences(plain["description"])])
                talk_norm = _norm_text(talk.get("description") or "")
                shared = [n for _s, n in plain_sents if n and n in talk_norm]
                if not shared:
                    continue
                # 反向形态（台词镜在前）判为拆裂需要更强证据：该无台词镜必须
                # **认领的就是这条源行**（excerpt 逐字含该动作句）——否则
                # 「有台词镜 + 同动作的无台词后镜」也可能是合法的正反打/特写
                # 覆盖（后镜认领自己的源行），误合会吞掉真实镜头
                if a.get("dialogue"):
                    plain_ex = _norm_text(plain.get("excerpt") or "")
                    if not any(n in plain_ex for n in shared):
                        continue
                # 预演合并后的台词时长：超 12s 上限不合并（保持拆分）
                merged_dlg = [*(a.get("dialogue") or []), *(b.get("dialogue") or [])]
                floor = _dialogue_duration_floor({**a, "dialogue": merged_dlg})
                if floor > SHOT_DURATION_MAX:
                    continue
                # 台词/摘抄/独有描述归位到靠前那镜（同一叙事节拍）
                a["dialogue"] = merged_dlg
                a["excerpt"] = _join_unique_lines(a.get("excerpt"), b.get("excerpt"))
                a_desc = _norm_text(a.get("description") or "")
                b_extra = [s for s in _split_sentences(b.get("description") or "")
                           if _norm_text(s) and _norm_text(s) not in a_desc]
                if b_extra:
                    own = [s for s, _n in a_sents]
                    a["description"] = "。".join(own + b_extra).strip("。") + "。"
                a["duration"] = max(int(a.get("duration") or 0),
                                    int(b.get("duration") or 0), floor)
                a["character_names"] = list(dict.fromkeys(
                    [*(a.get("character_names") or []), *(b.get("character_names") or [])]))
                a["prop_names"] = list(dict.fromkeys(
                    [*(a.get("prop_names") or []), *(b.get("prop_names") or [])]))
                # 行号认领账本守恒（审计 13 号 K）：B 被物理删除，其认领的源行
                # 必须并入 A——否则这些行变成「无人认领」，体检报 line_gap
                # error 并让修复轮补出重复演出的分镜（正是本函数要消灭的形态）
                a["source_line_ids"] = sorted(
                    {*(a.get("source_line_ids") or []),
                     *(b.get("source_line_ids") or [])})
                # 出口态账本取 B（节拍末段）——A 的 end_state 是被并镜的中途状态，
                # 用于下一镜入口态会让状态链断裂并漏报人物凭空消失
                if b.get("end_state"):
                    a["end_state"] = b["end_state"]
                if b.get("purpose"):
                    a["purpose"] = b["purpose"]
                if b.get("risks"):
                    a["risks"] = sorted({*(a.get("risks") or []),
                                         *(b.get("risks") or [])})
                del shots[j]
                merged += 1
                break  # A 已并入台词（不再参与合并），前进到下一镜
            idx += 1
        # ---- 阶段二：复演句剔除（无台词镜 → 窗口内有台词镜） ----
        for idx, a in enumerate(shots):
            if a.get("dialogue"):
                continue
            b_descs: list[str] = []
            b_excerpts: list[str] = []
            for b in shots[idx + 1:idx + 3]:
                if b.get("dialogue"):
                    b_descs.append(_norm_text(b.get("description") or ""))
                    b_excerpts.append(_norm_text(b.get("excerpt") or ""))
            if not any(b_descs) and not any(b_excerpts):
                continue
            sents = _split_sentences(a.get("description") or "")
            kept = [s for s in sents
                    if not (_norm_text(s) and any(b and _norm_text(s) in b
                                                  for b in b_descs))]
            if kept and len(kept) != len(sents):
                removed += len(sents) - len(kept)
                a["description"] = "".join(kept).strip()
            lines = (a.get("excerpt") or "").split("\n")
            ekept = [s for s in lines
                     if not (_norm_text(s) and any(b and _norm_text(s) in b
                                                   for b in b_excerpts))]
            if ekept and len(ekept) != len(lines):
                removed += len(lines) - len(ekept)
                a["excerpt"] = "\n".join(ekept).strip()
        for i, sh in enumerate(shots, start=1):
            sh["seq"] = i
    return {"merged": merged, "removed": removed}


def apply_duration_floor(episodes: list[dict],
                         bodies: list[str] | None = None, *,
                         allow_split: bool = True) -> dict:
    """语速硬门（确定性校准）：逐镜复核台词镜时长并抬升到语速下限。

    - duration < 语速折算下限 → 抬升到下限（取长优先，杜绝台词念不完）；
    - 下限 > 12s 上限（长独白/长对手戏）→ 按句读确定性拆分为链式镜头
      （transition_in=continue，生成链路自动尾帧衔接）；单句切不开时夹取
      到上限并计数。
    返回 {"raised": 抬升数, "capped": 夹取数, "split": 拆分数} 供轨迹展示。

    bodies：各集正文（与 episodes 同序，可空）——拆分时用于把父镜的源行认领
    按台词归位到子镜（认领账本守恒）。

    `allow_split=False`：只抬时长/夹取，**不拆链式镜头**——用于**落库收口**那道
    （25 号 3.1）。理由：拆分会产生子镜并重排 seq，而落库时父镜的 `video_prompt`
    已由 enrich 生成，拆出的子镜会共用父镜提示词（内容与时长都不再对应）。
    落库只做「不改变镜头数」的校准；需要拆的由预检按「整镜语速」报 error，
    交给人工/重算处理。
    """
    raised = capped = split = 0
    src_by_ep: dict[int, list[tuple[int, str, str, str]]] = {}
    for ei, ep in enumerate(episodes):
        body = (bodies[ei] if bodies and ei < len(bodies) else "") or ""
        src_by_ep[ei] = _source_lines(body) if body.strip() else []
    for ei, ep in enumerate(episodes):
        new_shots: list[dict] = []
        for sh in ep.get("shots") or []:
            # 缺 duration 时补默认值：落库口（_insert_shots）对时长本来就是容错的
            # （`sh.get("duration", 5)`），本函数却直接取键——25 号复查 R4：
            # 带台词但缺 duration 的镜头会在这里 KeyError，把整集落库带崩
            sh.setdefault("duration", SHOT_DURATION_DEFAULT)
            floor = _dialogue_duration_floor(sh)
            if floor <= 0:
                new_shots.append(sh)
                continue
            if floor > SHOT_DURATION_MAX:
                children = (_split_long_shot(sh, src_by_ep.get(ei) or None)
                            if allow_split else [])
                if len(children) > 1:
                    new_shots.extend(children)
                    split += 1
                    continue
                sh["duration"] = SHOT_DURATION_MAX
                capped += 1
            elif sh["duration"] < floor:
                sh["duration"] = floor
                raised += 1
            new_shots.append(sh)
        ep["shots"] = new_shots
        # 拆分会增加镜头：seq 重排保持连续（enrich/补拍按 seq 对账）
        for i, sh in enumerate(ep["shots"], start=1):
            sh["seq"] = i
    return {"raised": raised, "capped": capped, "split": split}


# ---------- 时序对账（video_prompt 时间小节 vs duration，确定性门禁） ----------
# 时间小节格式「00—02秒：」（容忍 -/—/–/~/～/至 等连接符）；模型按技能包省略
# 秒数（无时间小节结构）时不误报
_BEAT_SPAN_RE = re.compile(r"(\d{1,2})\s*[—\-–~～至]\s*(\d{1,2})\s*秒")
_VP_VIDEO_SECTION_RE = re.compile(r"【视频画面】\s*\n(.*?)(?=\n【|$)", re.S)


def timing_error(shot: dict) -> str:
    """时序对账：时间小节是否无缝覆盖 duration（返回错误描述；空串=通过）。

    规则：首节从 00 秒起、相邻小节秒数无缝衔接、末节止于 duration、不越界。
    无 video_prompt、无【视频画面】段或无时间小节时返回空串（缺描述由补拍
    循环按空 prompt 处理，省略秒数是模型技能包允许的写法）。
    """
    vp = shot.get("video_prompt") or ""
    if not vp.strip():
        return ""
    m = _VP_VIDEO_SECTION_RE.search(vp)
    if not m:
        return ""
    spans = [(int(a), int(b)) for a, b in _BEAT_SPAN_RE.findall(m.group(1))]
    if not spans:
        return ""
    duration = int(shot.get("duration") or 0)
    errors: list[str] = []
    if spans[0][0] != 0:
        errors.append(f"首节应从 00 秒开始（实际 {spans[0][0]} 秒）")
    for (s1, e1), (s2, _e2) in zip(spans, spans[1:]):
        if s2 != e1:
            errors.append(f"小节断档/重叠（{s1}-{e1} 秒后接 {s2} 秒起）")
    if spans[-1][1] != duration:
        errors.append(f"末节止于 {spans[-1][1]} 秒，应为 {duration} 秒")
    for s, e in spans:
        if e < s or e > duration:
            errors.append(f"小节 {s}-{e} 秒越界（duration={duration}）")
    return "；".join(errors)


# ---------- 资产名称对齐执行（批次 4 补充，根治 09 文档 B3） ----------

def _apply_asset_roster(episodes: list[dict], assets: dict) -> dict:
    """资产名单白名单强制执行（不猜语义）。

    职责切分（项目规则：LLM 交互场景禁硬编码语义判断）：
    - 语义判断（「不锈钢保温桶」≈「保温桶」）由对齐检查 LLM 输出
      aliases 映射（character/prop/scene），在 _align_loop 内**逐字应用**；
    - 此处只做确定性白名单执行：名单外残留引用剔除并计数
      （unmatched_refs 告警消费，画面不留无参考图的幽灵引用）。
    返回剔除统计 {props, chars, scenes}。
    """
    char_names = {c.get("name", "") for c in assets.get("characters") or []}
    scene_names = {s.get("name", "") for s in assets.get("scenes") or []}
    prop_names = {p.get("name", "") for p in assets.get("props") or []}
    removed = {"props": 0, "chars": 0, "scenes": 0}
    for ep in episodes:
        for sh in ep.get("shots") or []:
            kept_props = [p for p in (sh.get("prop_names") or [])
                          if p in prop_names]
            removed["props"] += len(sh.get("prop_names") or []) - len(kept_props)
            sh["prop_names"] = kept_props
            kept_chars = [c for c in (sh.get("character_names") or [])
                          if c in char_names]
            removed["chars"] += len(sh.get("character_names") or []) - len(kept_chars)
            sh["character_names"] = kept_chars
            sn = _as_str(sh.get("scene_name"))
            if sn and sn not in scene_names:
                removed["scenes"] += 1
    return removed


# 状态账本「空声明」判定：字段以这些词起首即视为「本镜没有该物」
_EMPTY_DECL_RE = re.compile(r"^(?:无|没有|不含|不出现|不涉及|暂无|未有)")


def _declares_empty(value) -> bool:
    """状态账本字段是否**明确声明为空**（「无具体道具」「没有道具」…）。

    只认「以否定词起首」这一确定性形态，不做语义推断——账本字段写的是
    「第 0 秒静止画面」的权威描述（见 api/drama.py 首帧组装注释），既然
    模型自己声明了没有，代码就不该再往里补东西。
    """
    text = _norm_text(_as_str(value)) if value else ""
    return bool(text) and bool(_EMPTY_DECL_RE.match(text))


def _backfill_prop_refs(episodes: list[dict], known_props: list[str]) -> int:
    """道具引用正向对账（批次 4 补充，确定性白名单匹配，不调 LLM 不猜语义）。

    资产道具名的**逐字字样**出现在分镜 desc/excerpt 文本中、而该分镜
    prop_names 漏标时，代码直接补引用——这是名单内正向对账（逐字匹配），
    与「名单外剔除」配套，兜住低档模型漏输出 prop_names 的波动。异名
    （「不锈钢保温桶」vs「保温桶」）不在本函数职责内——语义判断归对齐
    检查 LLM 的 prop_aliases 映射。返回补引用数。

    **例外：状态账本声明「无道具」的分镜不回填**。实测（霜刃辞3.0 镜1）
    描述里写「落向城头一个提食盒的白衣身影」、账本 position/props 都是
    「无具体人物/无具体道具」，回填后该镜**有道具无人物**——食盒失去持有者，
    成片里表现为**食盒悬空浮在画面中央**。账本既然声明了空，就不补。
    """
    filled = 0
    known_norm = [(p, _norm_text(p)) for p in known_props if _norm_text(p)]
    for ep in episodes:
        for sh in ep.get("shots") or []:
            state = sh.get("state") if isinstance(sh.get("state"), dict) else {}
            if _declares_empty(state.get("props")):
                continue  # 账本声明本镜无道具：回填即造出无主道具
            have = set(sh.get("prop_names") or [])
            text = _norm_text((sh.get("description") or "")
                              + (sh.get("excerpt") or ""))
            if not text:
                continue
            for prop, np in known_norm:
                if prop in have or len(np) < 2:
                    continue  # 已标 / 过短名（单字误命中率高）跳过
                if np in text:
                    sh.setdefault("prop_names", []).append(prop)
                    have.add(prop)
                    filled += 1
    return filled


def _source_lines(ep_body: str) -> list[tuple[int, str, str, str]]:
    """提取集内内容行（带行号）：[(行号, 类型 dlg|act|decl, 说话人, 文本)]。

    台词行：speaker：text（含 OS 备注）；动作/叙述行：其余非空内容行
    （跳过标题/引用/表格/分隔线）；声明行：「场景：/人物：/地点：/时间：」
    结构行单独归 decl（批次 2a §32.1 三分类——声明行由 scene_name/
    character_names 承接，行号对账豁免认领，强制进画面描述会诱导重复描写）。
    集级元信息头（「时长：… | 本集主题：…」）同样归 decl：它是剧本模板的头部
    字段，不是剧情内容，要求镜头认领只会得到一条永远清不掉的「源行未认领」。
    阈值 2 字：极短动作行（"他倒下。""她死了。"）往往是关键剧情节拍，
    丢弃会造成关键剧情漏拍。
    """
    lines_out: list[tuple[int, str, str, str]] = []
    # 是否已进入正文（首个场次标记/幕标题之后）：定义列表项的豁免只在头部区生效
    seen_scene_marker = False
    for idx, ln in enumerate(ep_body.splitlines()):
        ln = ln.strip()
        if not ln or ln.startswith((">", "|", "---", "***", "```")):
            continue
        if ln.startswith("#"):
            # 幕标题（「## 第一幕：拖延」）：记场次起点后跳过（标题非内容行）
            if re.match(r"^#+\s*第[^#]{0,8}幕", ln):
                seen_scene_marker = True
            continue
        if _is_scene_header_line(ln):
            seen_scene_marker = True
            lines_out.append((idx, "decl", "", ln))
            continue
        if _is_episode_meta_line(ln) or (
                not seen_scene_marker and _is_definition_bullet(ln)):
            # 元信息头（含 **加粗** 变体）/ 头部人物·场景定义列表项：decl 类
            # （豁免认领，也不参与缺口硬裁决）
            lines_out.append((idx, "decl", "", ln))
            continue
        m = _DIALOGUE_LINE_RE.match(ln)
        if m and m.group(1).strip() and "|" not in ln:
            speaker = m.group(1).strip()
            if _is_declaration_speaker(speaker):
                # 「场景：/人物：」等结构声明行：单独 decl 类（行号对账豁免，
                # 语义覆盖仍由对齐检查按 scene_name/character_names 判定）
                lines_out.append((idx, "decl", "", ln))
            else:
                # raw 必须保留完整行（含说话人前缀与表演提示）——对齐检查的
                # 台词判定以整行为依据；曾改为仅存冒号后文本导致台词行判定
                # 缺上下文而永远 missing（回归事故）
                lines_out.append((idx, "dlg", speaker, ln))
        elif len(_norm_text(ln)) >= 2:
            lines_out.append((idx, "act", "", ln))
    return lines_out


# ---------- 行号对账（批次 2a §32.1：节拍 ID 化——对账从字符串包含升级为 ID 集合运算） ----------

def _number_segments(segments: list[str],
                     src: list[tuple[int, str, str, str]]) -> list[str]:
    """为各拆解段生成 [行号] 前缀编号文本（行号 = 集内全局行号）。

    src 是全集内容行（有序）；段按顺序拼接覆盖 src 的一个子序列——逐段
    逐行从游标起做原文精确对位（_source_lines 跳过的空行/标题行保留原文
    不编号）。对位失败（边界行被两段共享等）时该行保留原文——LLM 不认领
    无行号行，该行回退字符串对账兜底。段序重排（正常不会发生）时未匹配
    行自动留空编号，不阻断解析。
    """
    numbered: list[str] = []
    pos = 0  # src 游标（用过即消费，防段界共享行重复编号）
    for seg in segments:
        out_lines: list[str] = []
        for ln in seg.splitlines():
            s = ln.strip()
            hit = None
            if s:
                j = pos
                while j < len(src) and src[j][3] != s:
                    j += 1
                if j < len(src):
                    hit = src[j]
                    pos = j + 1
            out_lines.append(f"[{hit[0]}] {s}" if hit is not None else ln)
        numbered.append("\n".join(out_lines))
    return numbered


def storyboard_line_qa(src: list[tuple[int, str, str, str]],
                       shots: list[dict]) -> dict:
    """行号认领对账（批次 2a §32.1）：覆盖/重复/配对三类结构判定。

    只认 dlg/act 行（decl 声明行由 scene_name/character_names 承接，豁免
    认领）；结构化判定 = 每镜都带 source_line_ids 键（_norm_shot 恒产出；
    零认领镜——仲裁让位/LLM 漏报——由调用方按文本命中回填）。返回
    {"missing": [(行号, 原文)], "duplicates": {行号: [seq..]},
    "suspended": [seq（台词悬空镜）], "structured": bool}。
    """
    if not shots or not all("source_line_ids" in s for s in shots):
        return {"missing": [], "duplicates": {}, "suspended": [],
                "structured": False}
    claim: dict[int, list[int]] = {}
    for si, s in enumerate(shots):
        for i in s.get("source_line_ids") or []:
            claim.setdefault(i, []).append(si)
    kind_of = {i: k for (i, k, _sp, _raw) in src}
    missing = [(i, raw) for (i, k, _sp, raw) in src
               if k in ("dlg", "act") and not claim.get(i)]
    duplicates = {i: [shots[j].get("seq") for j in sis]
                  for i, sis in claim.items()
                  if len(sis) > 1 and kind_of.get(i) in ("dlg", "act")}
    # 台词悬空镜（§33.1 检查项 1）：有 dialogue 却没有画面动作描述。
    # 以 description 内容为准（act 行认领属覆盖对账维度，与悬空分离——
    # 一行台词镜天然无独立动作行可认领，修复器补写 description 后即消账，
    # 否则修复循环每轮重复修同一镜、判定永不消账）
    suspended = [s.get("seq") for s in shots
                 if s.get("dialogue")
                 and not (s.get("description") or "").strip()]
    return {"missing": missing, "duplicates": duplicates,
            "suspended": suspended, "structured": True}


def arbitrate_line_claims(src: list[tuple[int, str, str, str]],
                          shots: list[dict]) -> int:
    """重复认领仲裁（代码级）：台词行归 dialogue 含该行文本的镜（无命中归
    首镜），动作行归首镜；其余镜移除该行号。返回仲裁次数。"""
    claim: dict[int, list[int]] = {}
    for si, s in enumerate(shots):
        for i in s.get("source_line_ids") or []:
            claim.setdefault(i, []).append(si)
    kind_of = {i: k for (i, k, _sp, _raw) in src}
    raw_of = {i: raw for (i, _k, _sp, raw) in src}
    fixed = 0
    for i, sis in claim.items():
        if len(sis) <= 1 or kind_of.get(i) not in ("dlg", "act"):
            continue
        keep = sis[0]
        if kind_of[i] == "dlg":
            m = _DIALOGUE_LINE_RE.match(raw_of[i])
            if m:
                text = _norm_text(_strip_paren(m.group(2)))
                hits = [si for si in sis
                        if text and any(
                            text in _norm_text(_strip_paren(_as_str(d.get("text"))))
                            for d in shots[si].get("dialogue") or [])]
                if len(hits) == 1:
                    keep = hits[0]
        for si in sis:
            if si != keep and i in (shots[si].get("source_line_ids") or []):
                # 重新赋值而非原地 remove（审计 13 号 K 防御层）：浅拷贝过的镜
                # 可能共享同一列表，原地删会把兄弟镜的认领一并清空
                shots[si]["source_line_ids"] = [
                    x for x in shots[si]["source_line_ids"] if x != i]
                fixed += 1
    return fixed


def vp_dialogue_order_error(shot: dict) -> str:
    """VP 台词顺序校验（批次 2a §32.1 顺序规则，warn 级因果倒置检测）。

    【视频画面】中各句台词的出现位置顺序必须与 dialogue（=源行顺序）一致；
    找不到的台词由台词覆盖检查兜底，此处只对能定位的台词判序（归一化投影
    保持单调，位置可比）。全部台词都未出现时返回空串（缺台词另有门）。
    """
    vp = shot.get("video_prompt") or ""
    if not vp.strip() or not shot.get("dialogue"):
        return ""
    m = _VP_VIDEO_SECTION_RE.search(vp)
    body = m.group(1) if m else vp
    body_n = _norm_text(body)
    if not body_n:
        return ""
    pos_list: list[tuple[int, int]] = []
    for di, d in enumerate(shot["dialogue"]):
        t = _norm_text(_strip_paren(_as_str(d.get("text"))))
        if len(t) < 6:  # 过短台词归一后误命中率高，跳过判序
            continue
        p = body_n.find(t)
        if p >= 0:
            pos_list.append((p, di))
    if len(pos_list) < 2:
        return ""
    pos_list.sort()
    order = [di for _p, di in pos_list]
    if order != sorted(order):
        return ("台词在画面描述中的出现顺序与剧本源行顺序不一致"
                "（因果倒置嫌疑），建议重算画面描述")
    return ""


def scale_rhythm_flags(cameras: list[str]) -> list[tuple[str, str]]:
    """景别节奏两查（首镜交代/景别单调）的共享判定实现。

    解析期体检（collect_storyboard_issues）与成片预检（_episode_precheck）
    消费同一实现——同一问题不再两处各判一遍（codex 评审 [低] 消重复）。
    返回 [(rule, message)]，rule ∈ first_shot | scale_mono。
    """
    flags: list[tuple[str, str]] = []
    scales = [x for x in (scale_of(c or "") for c in cameras) if x]
    if scales and scales[0] in ("近景", "特写"):
        flags.append(("first_shot",
                      "首镜直接近景/特写，建议先以远景或全景交代环境与人物关系"))
    if scales:
        top_scale, top_n = Counter(scales).most_common(1)[0]
        if top_n / len(scales) > 0.6:
            flags.append(("scale_mono",
                          f"{top_scale}占比 {top_n}/{len(scales)}，景别偏单调"))
    return flags


# 体检项白话映射（面向普通用户）：message/suggest 是内部术语，title/plain
# 用生活语言解释「这是什么问题 + 对成片的影响 + 怎么处理」——报告面板直接
# 渲染，用户不再需要懂 continue/state_chain 这类黑话
# 剧作层量化门常量（借鉴方法论报告的可量化指标，全部确定性计算）
DIALOGUE_MAX_CHARS = 35        # 单句台词上限（shuohao：≤35 字；超长念不完/气口断）
FRAME_MAX_CHARS = 3            # 单分镜同框人数上限（报告：三人同框需拆解——AI 生成易串脸）
ACTION_BEAT_TYPES = ("act", "dlg")   # 节拍类型（供「每场至少一个动作节拍」判定）

# 群体量词（20 号 §3.1 A6，官方禁用清单）：画面描述里用这些词概括人物而不点名
# ——官方案例：「不建议 `三个人面面相觑`／`众人大惊失色`／`反派们冲了上来`」，
# 应「完整写出当前镜头内出现的所有具体角色名称」。只用于**描述文本**检测，
# 不查原文摘抄（excerpt 是剧本原文，不能要求用户改剧本）。
CROWD_WORDS: tuple[str, ...] = (
    "三个人", "两个人", "几人", "几个", "数人",
    "众人", "大家", "众位", "所有人", "全体", "人群", "一群",
    "反派们", "士兵们", "弟子们", "村民们", "家人们", "孩子们",
)

# 描述性人物指代（SHOT_PROMPT k7 的**确定性安全网**）：画面里写了人却不用
# `角色@` 引用——该人物不绑定参考图，生成模型只能照字面自由发挥长相
# （实测：主角被写成「一个提食盒的白衣身影」，成片里成了一个**白头发**的
# 陌生人，与定妆照完全不符）。
#
# 两条纪律：
# ① **只查【视频画面】段**（LLM 自己写的导演级描述），不查 description——
#    后者源自剧本原文，报它等于让用户改剧本（与 CROWD_WORDS 同一条纪律）；
# ② 词表**刻意收窄**：不含「人物」「人」——【安全合规】的模板句
#    「不与镜头交汇」「不生成…额外人物」每个镜头都带，收宽了会全量误报。
UNBOUND_FIGURE_WORDS: tuple[str, ...] = (
    "身影", "人影", "男子", "女子", "男人", "女人", "少男", "少女",
    "老人", "老者", "少年", "青年", "姑娘", "汉子", "路人", "行人",
    "黑衣人", "白衣人",
)

# 不进教训账本的体检规则：它们的建议不是「下次该怎么写提示词」，归档进账本
# 只会让模型收到无关教训。
# - multi_cut：用户在关键场次勾了多切导致，不是模型写作问题；
# - first_shot：面板文案本身就写「属于风格选择，可以保留」；
# - shot_budget / scale_mono：节奏与景别，靠手动调整剪辑而非改写文本。
NON_LESSON_RULES: frozenset[str] = frozenset({
    "multi_cut", "first_shot", "shot_budget", "scale_mono",
    # 对白拆镜是**拆解层**的规矩（写提示词那一步救不回来），进教训账本
    # 只会把噪声注入「写提示词」环节；拆解侧由提示词自检（规则 12）把关
    "dialogue_split",
})


def prompt_layer_lessons(issues: list[dict]) -> dict[str, list[str]]:
    """体检问题 → **提示词层**教训明细（{规则: [反面案例]}，供 merge_lessons 归档）。

    为什么要有这一步：教训账本此前只收「能被代码自动修复的」4 类
    （transition/line_dup/suspended/line_gap），而 **unbound_figure / crowd_word /
    overload 这类「提示词写坏了」的信号一条都进不了账本**——模型每次都从零开始
    犯同样的错。归档后它们会随【历史教训】注入到**写提示词**那一步
    （见 `enrich_video_prompts` 的 lessons_text 参数）。

    收编判据（确定性）：规则有 QA_PLAIN 白话说明（没说明的展示不出来）、
    且不在 NON_LESSON_RULES 排除表里。
    """
    out: dict[str, list[str]] = {}
    for it in issues or []:
        rule = str(it.get("rule") or "")
        if not rule or rule in NON_LESSON_RULES or rule not in QA_PLAIN:
            continue
        sample = str(it.get("message") or "").strip()
        if sample:
            out.setdefault(rule, []).append(sample)
    return out

QA_PLAIN: dict[str, tuple[str, str]] = {
    "dialogue_too_long": ("单句台词太长",
                          "这一句台词超过 35 字，演员一口气念不完、字幕也排不下。建议拆成两句或删减"),
    "pure_dialogue": ("这一场没有动作节拍",
                      "整场只有台词没有可见动作，AI 生成时没有画面可写（等于广播剧）。建议至少加一个动作节拍"),
    "crowd_shot": ("同框人数过多",
                   "一个镜头里同框超过 3 人，AI 生成时容易串脸、配角乱跑。建议拆镜或减少同框人数"),
    "crowd_word": ("用群体量词概括了人物",
                   "这个镜头的画面描述用「三个人/众人/反派们」这类概括写法，AI 生成时不知道该给谁身体、谁服装。"
                   "建议逐个写出角色名（如「黄衣小哥，粉裙少女，灰头发老太」）"),
    "human_prompt_fix": ("用户重拍时改了提示词",
                         "这是**人工反馈**记下的教训：用户看过成片后判定「提示词写得不对」"
                         "并重拍。备注是原话，按它规避同类写法"),
    "unbound_figure": ("画面里的人物没绑定角色",
                       "这一镜的画面描述写了「白衣身影/一名男子」这类**没点名**的人物，"
                       "它不会绑定角色参考图——生成时长相完全由 AI 自由发挥，可能跟定妆照差很远。"
                       "请把这个人写成 角色@角色名（或删掉它，若本镜不该有人物）"),
    "first_shot": ("开场镜头没有先交代环境",
                   "第一镜直接从人物动作开始，观众还不知道人在哪儿。属于风格选择，可以保留"),
    "scale_mono": ("景别太单调",
                   "连续多个镜头用了同一种远近（如全是中景），画面节奏会显得平。可在分镜卡里手动改几个镜头的景别"),
    "shot_budget": ("镜头数偏多",
                    "这一集拆出的镜头比常规节奏偏多，成片会显得碎。可考虑合并相近的镜头"),
    "overload": ("单个镜头内容太多",
                 "这个镜头几秒内塞了太多动作或人物，画面容易赶。建议拆成两个镜头或精简动作"),
    "transition": ("镜头衔接方式与剧情对不上",
                   "这个镜头与上一镜的衔接标记和实际场景变化不一致，生成的画面可能接不上。可在分镜卡修正衔接类型"),
    "state_chain": ("人物可能凭空消失",
                    "上一镜结束时在场的角色，这一镜的画面描述和出场名单里都没出现——如果剧情里人还在，生成的画面他可能突然消失。用「重算描述」让 AI 补上"),
    "multi_cut": ("非关键场次拆了多张关键帧",
                  "这个镜头不在你勾选的关键场次里，却拆了两张关键帧（生成成本翻倍）。建议改回单切"),
    "line_gap": ("有剧本内容没被任何镜头使用",
                 "这段剧本被跳过了，成片会缺剧情。检查是否漏拆了镜头"),
    "line_dup": ("剧本原文被多个镜头重复使用",
                 "同一段剧本文字被多个镜头认领，可能演了重复画面。核对相邻镜头是否在演同一动作"),
    "suspended": ("有台词但画面没交代演什么",
                  "这个镜头有台词，但画面描述里没有任何动作或画面内容——视频模型不知道画面该演什么。用「重算描述」让 AI 按台词补写"),
    "dialogue_split": ("一轮对话被拆成了两镜",
                       "这两镜是同一轮对话的「一方说、另一方答」，按语速折算几秒就能演完——"
                       "合成一镜更顺（正反打由镜头内时间小节体现，不靠拆镜）。"
                       "属于拆解时该守的规矩，可人工合并这两镜（不提供自动修复）"),
}


# 对白拆镜检测阈值（collect_storyboard_issues 用；与拆解规则第 5/12 条
# 「一轮对话折算 ≤12 秒合一镜」同口径的**保守版**：折算 speech 只算台词本身，
# 留出反应节拍余量——双方台词合计 ≤10 秒仍被拆成两镜，基本可断定是拆碎了）
_DLG_SPLIT_SPEECH_RATE = 3.5   # 字/秒（与语速硬门公式同源口径）
_DLG_SPLIT_SPEECH_MAX = 10.0   # 台词折算合计上限（秒）


def collect_storyboard_issues(episodes: list[dict],
                              bodies: list[str] | None = None) -> list[dict]:
    """解析期分镜体检（批次 2a §33.1 确定性子集，全部本地计算不调 LLM）。

    bodies：与 episodes 平行的集正文数组（行号对账 QA 需要 src 行——episodes
    字典不携带正文）。统一 Issue 结构 {rule, level, episode, seq, message,
    suggest, title, plain}——落库告警 / precheck / 提交前硬门（批次 3）消费
    同一结构；title/plain 是面向用户的白话解释（报告面板直出）。
    检查项：首镜交代/景别单调/镜数超预算（C7 落实）/镜头过载/transition
    合理性/行号认领缺口与重复/台词悬空；state 矛盾随 2c 结构化 state 补入，
    轴线与资产未命中维持既有 precheck/unmatched_refs 通道。
    """
    issues: list[dict] = []

    def _add(rule: str, level: str, ep_no, seq, message: str, suggest: str,
             fix: dict | None = None):
        """追加一条体检项。

        fix（可空）：结构化修复意图 {field, value, label}——前端「一键修复」
        按钮据此逐字应用，不再用中文子串猜方向（审计 13 号 H：首镜 continue
        的文案不含「场景变化」，前端猜成 continue，按钮点了等于没点、报告
        永不清零、还 toast 报「已修正」；而该 error 会硬拦整集视频生成）。
        """
        title, plain = QA_PLAIN.get(rule, (rule, ""))
        item = {"rule": rule, "level": level, "episode": ep_no,
                "seq": seq, "message": message, "suggest": suggest,
                "title": title, "plain": plain}
        if fix:
            item["fix"] = fix
        issues.append(item)

    for ei, ep in enumerate(episodes):
        shots = ep.get("shots") or []
        ep_no = ep.get("seq")
        if not shots:
            continue
        body = (bodies[ei] if bodies and ei < len(bodies) else "") or ""
        # 首镜交代（warn，钩子式开场合法不自动改写）+ 景别单调（warn）
        # ——两查消费共享实现 scale_rhythm_flags（消重复拦截）
        for rule, msg in scale_rhythm_flags(
                [s.get("camera", "") for s in shots]):
            _add(rule, "warn", ep_no,
                 1 if rule == "first_shot" else None, msg,
                 "景别跳档衔接，避免均匀病"
                 if rule == "scale_mono" else "人工一键改写首镜景别")
        # 镜数超预算（warn，C7 落实）：实际时长 ÷10s/镜 ×1.3 上浮
        total_sec = sum(int(s.get("duration") or 0) for s in shots)
        budget_hi = max(6, math.ceil(total_sec / 10 * 1.3))
        if len(shots) > budget_hi:
            _add("shot_budget", "warn", ep_no, None,
                 f"镜数 {len(shots)} 超预算（时长 {total_sec}s ÷ 10s/镜 ×1.3 ≈ {budget_hi}）",
                 "拆得过碎，建议合并相邻动作/对话镜")
        # 镜头过载（warn，评审遗漏形态 #9）：长镜内动作句/出场人数密度上限
        for s in shots:
            act_sents = len(_split_sentences(s.get("description") or ""))
            n_chars = len(s.get("character_names") or [])
            if (int(s.get("duration") or 0) >= 10
                    and (act_sents >= 4 or n_chars >= 4)):
                _add("overload", "warn", ep_no, s.get("seq"),
                     f"镜{s.get('seq')} 时长 {s.get('duration')}s 内动作句 {act_sents} 个、"
                     f"出场 {n_chars} 人，密度过高",
                     "拆分叙事节拍或精简同镜人数")
        # transition 合理性（error/warn）——每条都带结构化 fix（前端逐字应用）
        if (shots[0].get("transition_in") or "") == "continue":
            _add("transition", "error", ep_no, shots[0].get("seq"),
                 "首镜标 continue（无上一镜可承接）", "首镜改 scene_cut",
                 fix={"field": "transition_in", "value": "scene_cut",
                      "label": "切换场景"})
        for prev, s in zip(shots, shots[1:]):
            t = s.get("transition_in") or ""
            same_scene = (bool(s.get("scene_name"))
                          and prev.get("scene_name") == s.get("scene_name"))
            if t == "continue" and not same_scene:
                _add("transition", "error", ep_no, s.get("seq"),
                     f"镜{prev.get('seq')}→{s.get('seq')} 标 continue 但场景变化"
                     f"（{prev.get('scene_name') or '?'}→{s.get('scene_name') or '?'}）",
                     "跨场景应标 scene_cut；同场景新地点核对场景名",
                     fix={"field": "transition_in", "value": "scene_cut",
                          "label": "切换场景"})
            if same_scene and t == "scene_cut":
                _add("transition", "warn", ep_no, s.get("seq"),
                     f"镜{prev.get('seq')}→{s.get('seq')} 同场景却标 scene_cut",
                     "同场景衔接应为 continue/time_skip",
                     fix={"field": "transition_in", "value": "continue",
                          "label": "连续动作"})
        # ---- 剧作层量化门（方法论报告指标，确定性计算）----
        # ① 单句台词长度：超 35 字一口气念不完（shuohao 门；字幕也排不下）
        for s_ in shots:
            for d in (s_.get("dialogue") or []):
                text = _strip_paren(_as_str(d.get("text")))
                if len(text) > DIALOGUE_MAX_CHARS:
                    _add("dialogue_too_long", "warn", ep_no, s_.get("seq"),
                         f"镜{s_.get('seq')} 单句台词 {len(text)} 字"
                         f"（上限 {DIALOGUE_MAX_CHARS} 字）：「{text[:20]}…」",
                         "拆成两句或删减字数（语速折算参见语速硬门）")
        # ② 每场至少一个动作节拍（反广播剧门）：本集 source_line_ids 认领的
        #    act 行总数为 0 → 整集纯对白，AI 生成时无画面可写
        _kind_of = {i: k for (i, k, _sp, _raw) in (_source_lines(body)
                                                   if body else [])}
        _claimed_action = 0
        for s_ in shots:
            for lid in (s_.get("source_line_ids") or []):
                if _kind_of.get(lid) == "act":
                    _claimed_action += 1
                    break
        if shots and _claimed_action == 0 and body.strip():
            _add("pure_dialogue", "warn", ep_no, None,
                 f"本集 {len(shots)} 个分镜均未认领动作行（整集纯对白）",
                 "至少给关键场次补一个可见动作节拍（报告：纯对白的场是广播剧，"
                 "AI 生成时没有画面可写）")
        # ③ 单分镜同框人数上限：>3 人时 AI 易串脸（报告：三人同框需拆解）
        for s_ in shots:
            n_chars = len(s_.get("character_names") or [])
            if n_chars > FRAME_MAX_CHARS:
                _add("crowd_shot", "warn", ep_no, s_.get("seq"),
                     f"镜{s_.get('seq')} 同框 {n_chars} 人（上限 {FRAME_MAX_CHARS}）",
                     "拆镜或减少同框人数——多人同框是 AI 串脸与配角乱跑的高发场景")
        # 跨镜状态链（批次 2c §32.7）：continue 镜对做出口/入口人物集合对账
        # ——前镜结束时刻在场的人物在本镜凭空消失（离场已交代的可豁免，warn）
        # 豁免：character_names 漏列但画面文本（描述/摘录/状态账本）逐字含该
        # 角色名 → 人物实际在场，仅为名单字段漏列（实测镜6 描述/state 均有
        # 张总、名单漏列触发误报）
        for prev, s in zip(shots, shots[1:]):
            if (s.get("transition_in") or "") != "continue":
                continue
            prev_end_who = (prev.get("end_state") or {}).get("who") or []
            cur_who = set(s.get("character_names") or [])
            screen_text = " ".join([
                s.get("description") or "", s.get("excerpt") or "",
                str((s.get("state") or {}).get("position") or ""),
            ])
            # 凭空出现方向：本镜出场的角色不在上一镜出场名单 ∪ 上一镜出口
            # 在场名单中（纯名单对账，结构化字段）。豁免看**上一镜**正文——
            # 本镜 description 必然提及本镜出场者，用它豁免等于永不触发；
            # 上一镜正文逐字提及（入场已铺垫）才豁免
            prev_who = set(prev.get("character_names") or []) | set(prev_end_who)
            if prev_who:
                prev_screen_text = " ".join([
                    prev.get("description") or "", prev.get("excerpt") or "",
                    str((prev.get("state") or {}).get("position") or ""),
                ])
                appeared = [w for w in (s.get("character_names") or [])
                            if w not in prev_who and w not in prev_screen_text]
                if appeared:
                    _add("state_chain", "warn", ep_no, s.get("seq"),
                         f"镜{prev.get('seq')}→{s.get('seq')} continue 衔接，"
                         f"但{'、'.join(appeared)}不在上一镜结束时的在场名单、"
                         "本镜突然出场",
                         "人物凭空出现嫌疑：补入场交代（走入画面/自门外进入）"
                         "或核对上一镜 end_state.who")
            if not prev_end_who:
                continue  # 旧数据/出口账本未产出（消失方向依赖出口账本）
            vanished = [w for w in prev_end_who
                        if w not in cur_who and w not in screen_text]
            if vanished:
                _add("state_chain", "warn", ep_no, s.get("seq"),
                     f"镜{prev.get('seq')}→{s.get('seq')} continue 衔接，"
                     f"但{'、'.join(vanished)}在上一镜结束时在场、本镜未出场",
                     "人物凭空消失嫌疑：补离场交代或改 transition"
                     "（画面已交代离场的可豁免）")
        # 群体量词概括（20 号 §3.1 A6，官方口径）：描述里用「三个人/众人/反派们」
        # 概括而不点名 → AI 不知道该给谁身体与服装，是「配角乱跑/串脸」的一类成因。
        # 只查 description（LLM 自动生成的画面描述）——excerpt 是原文摘抄，
        # 原文怎么写不能改，报它等于让用户改剧本（误报）。
        for s in shots:
            _desc = str(s.get("description") or "")
            _hit = next((w for w in CROWD_WORDS if w in _desc), "")
            if _hit:
                _add("crowd_word", "warn", ep_no, s.get("seq"),
                     f"镜{s.get('seq')} 画面描述用「{_hit}」概括人物",
                     "逐个写出角色名（AI 生成需要知道谁穿什么、站在哪）")
            # k7 安全网：**整镜没有任何 `角色@` 引用**（⇒ 该镜一个角色参考图都
            # 不会绑定）**且**画面里出现了描述性人物指代 → 这个人物只能是模型
            # 自由发挥的。
            #
            # **条件必须这么严**：实测本集 7 个「行内有身影但无 @」的候选里，
            # 只有 1 个是整镜无绑定，其余 6 个的绑定由【全局定义】的出场素材
            # 完成（角色名就在同一行、只是没用 @）——按行判会 86% 误报，那样
            # 的门禁会被无视，比没有更糟。绑定由**任何位置**的 @ 引用触发，
            # 故判据取「整镜有无 @」而不是「该行有无 @」。
            _vp = str(s.get("video_prompt") or "")
            if "角色@" in _vp:
                continue  # 本镜有角色绑定，行内措辞差异不算问题
            _vp_sec = _VP_VIDEO_SECTION_RE.search(_vp)
            if not _vp_sec:
                continue
            _unbound = next((w for w in UNBOUND_FIGURE_WORDS
                             if w in _vp_sec.group(1)), "")
            if _unbound:
                # message 里带**具体反面案例**（截到句边界，不割词）：教训账本与
                # 报告面板都靠它——「具体案例比抽象规则的约束力强」
                # （见 build_lessons_text 的设计说明）。
                # **必须取「身影」真正所在的那一行**，不是整段的第一行：
                # 整段首行是群山城墙，截出来与问题毫无关系。
                _bad_line = next((ln for ln in _vp_sec.group(1).splitlines()
                                  if _unbound in ln), "")
                _bad = clip_at_sentence(_bad_line, LESSON_SAMPLE_MAX_CHARS)
                _add("unbound_figure", "warn", ep_no, s.get("seq"),
                     f"镜{s.get('seq')} 「{_unbound}」没绑定角色：{_bad}",
                     "本镜没有 角色@ 引用，人物长相只能由 AI 自由发挥——"
                     "请用 角色@角色名 引用（或删掉这个人物，若本镜不该有人）")
        # 多切关键帧（批次 4 §35.2）：越权与非法切点检查
        for s in shots:
            cuts = s.get("cuts") or []
            if not cuts:
                continue
            if not s.get("_in_key_scene"):
                _add("multi_cut", "warn", ep_no, s.get("seq"),
                     f"镜{s.get('seq')} 非关键场次却输出多切",
                     "多切仅限关键场次（首帧成本 ×2），普通场次改回单切")
            offsets = [c.get("offset") for c in cuts]
            if offsets != sorted(offsets) or (offsets and offsets[0] != 0):
                _add("multi_cut", "error", ep_no, s.get("seq"),
                     f"镜{s.get('seq')} 切点非法（offset 须 0 起、单调递增）",
                     "修正切点秒数")
        # 行号对账残留（error/warn）：缺口/重复/台词悬空
        src = _source_lines(body) if body else []
        qa = storyboard_line_qa(src, shots) if src else {"structured": False}
        if qa.get("structured"):
            for (i, raw) in qa["missing"][:5]:
                _add("line_gap", "error", ep_no, None,
                     f"源行未认领：{raw[:40]}", "重新解析或人工补镜（缺口已点名行号）")
            for i, seqs in list(qa["duplicates"].items())[:5]:
                _add("line_dup", "warn", ep_no, None,
                     f"源行被多镜认领（{seqs}）", "后镜复演让位，检查相邻镜是否重复表演")
            for seq in qa["suspended"][:10]:
                _add("suspended", "error", ep_no, seq,
                     f"镜{seq} 有台词无画面动作（台词悬空镜）",
                     "补写该镜画面描述（只有台词的镜成片是广播剧）")
        # 对白拆镜检测（2026-10-10 用户报障：一轮问答被拆成"问一镜、答一镜"）。
        # 与拆解规则第 5/12 条同口径的**事后兜底**——拆分镜那一刻没守住时，这里
        # 让问题看得见（warn 提示，不带修复动作：合并需要人判断，不塞一键修复）。
        # 判定全确定性、宁缺毋滥：相邻两镜同场景 + 都有台词 + 说话人发生切换 +
        # 双方台词按语速（3.5 字/秒）折算合计 ≤10 秒——这种一轮交锋本应一镜。
        for _i in range(len(shots) - 1):
            _a, _b = shots[_i], shots[_i + 1]
            _da = _a.get("dialogue") or []
            _db_ = _b.get("dialogue") or []
            if not _da or not _db_:
                continue
            if (_a.get("scene_name") or "") != (_b.get("scene_name") or ""):
                continue
            _sp_a = str(_da[-1].get("speaker") or "").strip()
            _sp_b = str(_db_[0].get("speaker") or "").strip()
            if not _sp_a or not _sp_b or _sp_a == _sp_b:
                continue
            _chars = sum(len(str(_x.get("text") or ""))
                         for _x in (_da + _db_))
            _sec = _chars / _DLG_SPLIT_SPEECH_RATE
            if _sec <= _DLG_SPLIT_SPEECH_MAX:
                _add("dialogue_split", "warn", ep_no, _a.get("seq"),
                     f"镜{_a.get('seq')}→镜{_b.get('seq')} 是一轮对话的"
                     f"「{_sp_a}问/说 → {_sp_b}应答」，按语速折算约 {_sec:.1f} 秒"
                     "——本应合成一镜（正反打由时间小节体现）",
                     "拆解时该守「一轮对话一镜」；建议合并这两镜"
                     "（合并需人工判断，故不提供自动修复）")
    return issues


def _qa_repair_loop(episodes: list[dict], bodies: list[str],
                    cancel_check: CancelCheck | None = None,
                    stream_cb: Callable[[str, str], None] | None = None,
                    max_rounds: int = 2,
                    style_hint: str = "", skill_text: str = "",
                    model_skill_text: str = "",
                    characters: list[dict] | None = None,
                    scenes: list[dict] | None = None,
                    props: list[dict] | None = None) -> list[str]:
    """体检修复循环（ReAct：审核→修复→复查）。

    - transition error 确定性修复：跨场景误标 continue → scene_cut（改字段，
      零 LLM——衔接枚举是封闭集合，确定性归一）
    - line_dup warn 确定性仲裁：重复认领让位（arbitrate_line_claims 后镜让位）
    - suspended error 定向 LLM 修复：复用台词悬空修复器补 description
    - line_gap error 定向 LLM 补镜：复用缺口补镜机制（含插入位置），新镜
      立即单镜补画面描述（enrich 已过——裸镜不进落库）
    - 复查用 collect_storyboard_issues（确定性规则零成本）；上限 max_rounds
      防不收敛震荡，本轮无修复进展即提前退出（再循环也不会收敛）
    - state_chain 等不越权自动修（真离场属用户判断）——残留由调用方存档，
      报告面板白话 + 一键修复兜底
    返回每轮修复日志（空 = 首轮审核即无 error/dup）。
    """
    repair_log: list[str] = []
    # 累积本轮修复动作数——调用方据此判断**是否需要把改动回写数据库**
    # （先落库设计下，落库之后的内存改动不会自动进库，实测事故：体检日志报
    # 「悬空修复 7 镜」而库里 7 镜描述仍为空——算完、记日志、丢掉）
    changed_total = 0
    # 自学习教训样本（rule → 反面案例列表）：本轮修了什么问题、具体长什么样，
    # 供调用方归档到项目教训账本、注入下次解析的【历史教训】段落
    lesson_samples: dict[str, list[str]] = {
        "transition": [], "line_dup": [], "suspended": [], "line_gap": [],
    }
    for round_no in range(1, max_rounds + 1):
        pre = collect_storyboard_issues(episodes, bodies)
        pre_errors = [i for i in pre if i["level"] == "error"]
        has_dup = any(i["rule"] == "line_dup" for i in pre)
        if not pre_errors and not has_dup:
            break
        # 确定性修复：跨场景误标 continue → scene_cut
        det_fixed = 0
        for it in pre_errors:
            if it["rule"] != "transition" or it.get("seq") is None \
                    or "场景变化" not in it["message"]:
                continue
            ep_match = next((e for e in episodes
                             if e.get("seq") == it["episode"]), None)
            shot_match = next((s for s in (ep_match or {}).get("shots") or []
                               if s.get("seq") == it["seq"]), None)
            if shot_match and (shot_match.get("transition_in") or "") == "continue":
                shot_match["transition_in"] = "scene_cut"
                det_fixed += 1
                lesson_samples["transition"].append(
                    f"镜{it['seq']} 跨场景却标了连续衔接")
        # 确定性修复：line_dup 仲裁让位（后镜让出重复认领的行）
        for ep, body in zip(episodes, bodies):
            shots = ep.get("shots") or []
            src = _source_lines(body) if body else []
            if src and shots:
                dup_n = arbitrate_line_claims(src, shots)
                det_fixed += dup_n
                if dup_n:
                    lesson_samples["line_dup"].append("同一段剧本被多个镜头重复表演")
        # ---- 顺序修正（实测事故：修复轮空转/不收敛）----
        # ① 缺口补镜【先】执行：它会插入新镜并**重排 seq**（编号连续）。旧顺序把
        #    悬空修复排在补镜之前，补镜一改编号，悬空修复内部按 seq 建的映射就
        #    失效——修复器照常调用却写不进去（日志报「悬空修复 0 镜」，残留 error
        #    原样进报告，用户看到「报出来却没修」）。
        # ② 悬空修复【后】执行，并在补镜之后**重新采集**体检结果，按新 seq 定位。
        gap_fixed = 0
        if any(i["rule"] == "line_gap" for i in pre_errors):
            gap_fixed, gap_samples = _repair_line_gaps(
                episodes, bodies, style_hint, skill_text, model_skill_text,
                characters, scenes, props, cancel_check, stream_cb)
            lesson_samples["line_gap"].extend(gap_samples)
        llm_fixed = 0
        mid = collect_storyboard_issues(episodes, bodies)
        if any(i["rule"] == "suspended" for i in mid):
            llm_fixed, susp_samples = _repair_suspended_shots(
                episodes, bodies, cancel_check, stream_cb)
            lesson_samples["suspended"].extend(susp_samples)
        repair_log.append(
            f"第{round_no}轮：衔接修正 {det_fixed} 处、补镜 {gap_fixed} 个、"
            f"悬空修复 {llm_fixed} 镜")
        changed_total += det_fixed + gap_fixed + llm_fixed
        # 修复后立即复查：把「报了几处但复查未减」的情况显式留痕（旧实现只在
        # 完全无修复时报日志，部分修复失效被静默吞掉——用户侧的「修了但还在」）
        post = collect_storyboard_issues(episodes, bodies)
        pre_err_n = len(pre_errors)
        post_err_n = sum(1 for i in post if i["level"] == "error")
        if post_err_n and (det_fixed or llm_fixed or gap_fixed) and                 post_err_n >= pre_err_n:
            stale = "；".join(f"[{i['rule']}]{str(i.get('message'))[:40]}"
                              for i in post if i["level"] == "error")[:200]
            logger.warning("体检修复第 %s 轮调整了 %s 处但 error 未减少"
                           "（%s→%s）：%s", round_no,
                           det_fixed + llm_fixed + gap_fixed, pre_err_n,
                           post_err_n, stale)
            repair_log.append(f"第{round_no}轮：error 未减少（{pre_err_n}→{post_err_n}）"
                              "——已留痕，见体检报告")
        if not det_fixed and not llm_fixed and not gap_fixed:
            break  # 本轮无修复进展：再循环也不会收敛，交报告面板
    detail = {k: v for k, v in lesson_samples.items() if v}
    return {"log": repair_log, "detail": detail,
            "changed": changed_total > 0, "changed_count": changed_total}


def _repair_line_gaps(episodes: list[dict], bodies: list[str],
                      style_hint: str = "", skill_text: str = "",
                      model_skill_text: str = "",
                      characters: list[dict] | None = None,
                      scenes: list[dict] | None = None,
                      props: list[dict] | None = None,
                      cancel_check: CancelCheck | None = None,
                      stream_cb: Callable[[str, str], None] | None = None,
                      ) -> tuple[int, list[str]]:
    """行号缺口精确补镜（体检修复轮）：缺行交 LLM 补镜（含插入位置）。

    复用拆解期缺口补镜机制（SHOT_FILL）；补镜后 seq 重排，新镜立即单镜
    生成画面描述（enrich 已过——裸镜不进落库）。返回（补镜总数, 反面案例
    样本——供自学习账本归档）。
    """
    known_chars = [str(c.get("name") or "") for c in (characters or [])]
    known_scenes = [str(s.get("name") or "") for s in (scenes or [])]
    known_props = [str(p.get("name") or "") for p in (props or [])]
    filled_total = 0
    gap_samples: list[str] = []
    for ep, body in zip(episodes, bodies):
        shots = ep.get("shots") or []
        src = _source_lines(body) if body else []
        if not src or not shots:
            continue
        by_idx = {i: raw for (i, _k, _sp, raw) in src}
        for _ in range(2):
            qa = storyboard_line_qa(src, shots)
            if not qa.get("structured") or not qa["missing"]:
                break
            missing_idx = [i for (i, _r) in qa["missing"]]
            # 反面案例：未被覆盖的源行原文（教下次「每一行都要覆盖」）
            for (_i, raw) in qa["missing"][:2]:
                gap_samples.append(f"行「{raw[:36]}」未被任何镜覆盖")
            first, last = missing_idx[0], missing_idx[-1]
            ctx_before = next((raw for (i, _k, _sp, raw) in reversed(src)
                               if i < first), "")
            ctx_after = next((raw for (i, _k, _sp, raw) in src if i > last), "")
            fill_payload = {"style": style_hint, "skills": skill_text,
                            "context_before": ctx_before,
                            "context_after": ctx_after,
                            "lines": [{"i": i, "text": by_idx[i]}
                                      for i in missing_idx],
                            "shots": [_shot_brief(s, with_state=True)
                                      for s in shots],
                            "characters": known_chars,
                            "scenes": known_scenes,
                            "props": known_props}
            try:
                fill = _llm_call(_prompts.SHOT_FILL_SYSTEM_PROMPT,
                                 json.dumps(fill_payload, ensure_ascii=False),
                                 cancel_check, stream_cb=stream_cb,
                                 stream_label="体检修复·缺口补镜（思考中）",
                                 contract=ShotsOut)
            except ScriptParseError as e:
                logger.warning("体检修复轮缺口补镜失败（集 %s）：%s",
                               ep.get("seq"), e)
                break
            for item in fill.get("shots") or []:
                if not isinstance(item, dict):
                    continue
                shot = _norm_shot(item)
                if not (shot["description"] or shot["dialogue"]
                        or shot.get("excerpt")):
                    continue
                after = _as_int(item.get("after_seq"), len(shots), 0, len(shots))
                shots.insert(min(after, len(shots)), shot)
                # 认领回填：文本命中行号写入 source_line_ids（与 _align_loop 同款）
                s_norm = _norm_text((shot.get("excerpt") or "")
                                    + shot["description"])
                dlg_norm = _norm_text("".join(
                    f"{d.get('speaker', '')}：{d.get('text', '')}"
                    for d in shot["dialogue"]))
                matched: list[int] = []
                for (i, k, _sp, raw) in src:
                    n = _norm_text(raw)
                    if not n:
                        continue
                    if k == "dlg":
                        if n in dlg_norm or n[:20] in dlg_norm:
                            matched.append(i)
                    elif n in s_norm or n[:20] in s_norm:
                        matched.append(i)
                shot["source_line_ids"] = sorted(set(matched))
                # 插入校验（实测教训）：LLM 有时把台词原样回抄进 excerpt 却不带
                # line（如补的是"台词行"而非"缺行"）——此时缺口并未被认领，旧实现
                # 照数 +1，循环误判"有进展"，同一缺口反复补镜（实测 2 镜→6 镜仍
                # 报 line_gap）。这里只认**确实认领了本次缺口行**的插入。
                claimed_now = set(shot["source_line_ids"]) & set(missing_idx)
                if not claimed_now:
                    shots.remove(shot)
                    gap_samples.append(
                        f"补镜未认领缺口行（回抄原文），已丢弃："
                        f"{str(shot.get('excerpt'))[:36]}")
                    logger.warning("体检修复轮补镜未认领缺口行，已丢弃该补镜")
                    continue
                filled_total += 1
        # seq 重排（补镜插入后，编号连续）
        for i, s in enumerate(shots, start=1):
            s["seq"] = i
        # 新镜无画面描述：单镜生成（enrich 已过批次；顺带补历史 enrich 失败镜）
        for s in shots:
            if (s.get("video_prompt") or "").strip():
                continue
            prev = [ss for ss in shots if ss.get("seq", 0) < s.get("seq", 0)][-2:]
            try:
                s["video_prompt"], _ = regenerate_shot_prompt(
                    s, prev, style_hint, characters, scenes, props,
                    skill_text=skill_text, model_skill_text=model_skill_text,
                    cancel_check=cancel_check, stream_cb=stream_cb)
            except ScriptParseError as e:
                logger.warning("体检修复轮补镜画面描述生成失败（seq %s）：%s",
                               s.get("seq"), e)
    return filled_total, gap_samples


def _repair_suspended_shots(episodes: list[dict], bodies: list[str],
                            cancel_check: CancelCheck | None = None,
                            stream_cb: Callable[[str, str], None] | None = None,
                            ) -> tuple[int, list[str]]:
    """台词悬空定向修复（体检修复轮）：按集调台词悬空修复器补 description。

    与拆解期修复循环同款语义（description 只补画面动作，dialogue/剧情冻结）；
    返回（修复镜数, 反面案例样本——供自学习账本归档）。修复后由调用方复查。
    """
    fixed = 0
    samples: list[str] = []
    for ep, body in zip(episodes, bodies):
        shots = ep.get("shots") or []
        src = _source_lines(body) if body else []
        qa = (storyboard_line_qa(src, shots) if src
              else {"structured": False, "suspended": []})
        if not qa.get("structured") or not qa.get("suspended"):
            continue
        suspended = set(qa["suspended"])
        repair_shots = [{"seq": s["seq"], "dialogue": s.get("dialogue") or [],
                         "character_names": s.get("character_names") or [],
                         "scene_name": s.get("scene_name", ""),
                         "excerpt": s.get("excerpt", ""),
                         "state": s.get("state") or {}}
                        for s in shots if s.get("seq") in suspended]
        if not repair_shots:
            continue
        first_seq = repair_shots[0]["seq"]
        payload = {"shots": repair_shots,
                   "context_before": next(
                       (s.get("excerpt", "") for s in shots
                        if s["seq"] == first_seq - 1), ""),
                   "context_after": next(
                       (s.get("excerpt", "") for s in shots
                        if s["seq"] == first_seq + 1), "")}
        try:
            data = _llm_call(_prompts.SHOT_DESC_REPAIR_SYSTEM_PROMPT,
                             json.dumps(payload, ensure_ascii=False),
                             cancel_check, stream_cb=stream_cb,
                             stream_label=f"体检修复·台词悬空（思考中）")
        except ScriptParseError as e:
            logger.warning("体检修复轮台词悬空修复失败（集 %s）：%s",
                           ep.get("seq"), e)
            continue
        desc_by_seq = {_as_int(it.get("seq"), -1, 1, 10 ** 6):
                       _as_str(it.get("description"))
                       for it in (data.get("shots") or [])
                       if isinstance(it, dict)}
        for s in shots:
            new_desc = desc_by_seq.get(s.get("seq"))
            if s.get("seq") in suspended and new_desc:
                s["description"] = new_desc
                fixed += 1
                # 反面案例：镜号 + 台词首句（教下次「每个台词镜都要有画面动作」）
                dlg = (s.get("dialogue") or [{}])[0]
                samples.append(
                    f"镜{s.get('seq')}「{_as_str(dlg.get('text'))[:16]}」"
                    "只有台词没有画面动作")
    return fixed, samples


# 场景空间布局声明的截断上限（方位法典：出入口朝向 / 门窗分布 / 固定陈设方位）
SCENE_LAYOUT_MAX = 200


def asset_briefs(characters: list[dict] | None, scenes: list[dict] | None,
                 props: list[dict] | None) -> tuple[
                     dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    """资产锚定摘要**四元组**（角色/场景/道具/**场景空间布局**）——enrich 批量与单镜重生成共用。

    角色 = 定妆描述 + 人设要点；场景锚定必须用 image_prompt（与场景主图
    同源的生成文字）；道具 = 定妆描述（外观一致性锚点）。

    **scene_layouts 单独成字段**（不再拼进 scene_briefs）：两者是不同性质的
    信息——brief 是「画面长什么样」（供视觉锚定），layout 是「空间方位」（供
    人物调度与空间对账）。此前把 layout 前置拼进 brief 再截断到 160 字，一个
    场景的 layout 就吃掉一半预算、把图像描述挤掉（实测 207 字 → 砍 47 字）；
    而 SHOT_PROMPT 的输入契约里那个 `scene_layouts` 字段**代码从未构造过**，
    规则 g 却写着「以 scene_layouts 为准」——模型被指示去看一个不存在的字段。
    """
    char_briefs: dict[str, str] = {}
    for c in characters or []:
        name = _as_str(c.get("name"))
        if not name:
            continue
        img = (c.get("image_prompt") or "").strip()
        # **外观原文优先**（2026-09-29 用户报障「脸变了」的根因）：此前 brief 只有
        # image_prompt + persona，而 appearance（五官/发型/服装的权威描述）**从未
        # 随载荷下去**——写画面描述的模型只能凭印象编，实测把「墨发」写成「麦色
        # 长发」、并把「瓜子脸/剑眉入鬓/眼尾微挑」整段丢掉，脸因此靠参考图硬扛。
        look = (c.get("appearance") or "").strip()
        persona = (c.get("persona") or "").strip()
        parts = [p for p in (look, img) if p]
        text = "｜".join(parts)
        if persona:
            text = f"{text}（人设要点：{persona[:60]}）" if text else persona[:60]
        char_briefs[name] = text[:240]
    scene_briefs: dict[str, str] = {}
    scene_layouts: dict[str, str] = {}
    for s in scenes or []:
        name = _as_str(s.get("name"))
        if not name:
            continue
        # 场景锚定必须用 image_prompt（与场景主图**同源**的生成文字——主图
        # 就是以它生成的，陈设/光线/构图以它为准）；description 是剧本视角
        # 的环境描写，与主图存在细节漂移，仅作 image_prompt 缺失时的回退。
        body = (_as_str(s.get("image_prompt")) or _as_str(s.get("description")))
        scene_briefs[name] = body[:160]
        layout = _as_str(s.get("layout"))
        if layout:
            scene_layouts[name] = layout[:SCENE_LAYOUT_MAX]
    # 道具锚定摘要：道具定妆描述（外观一致性锚点，防道具形态跨镜漂移）
    prop_briefs: dict[str, str] = {}
    for p in props or []:
        name = _as_str(p.get("name"))
        if not name:
            continue
        prop_briefs[name] = (
            _as_str(p.get("image_prompt")) or _as_str(p.get("description"))
        )[:120]
    return char_briefs, scene_briefs, prop_briefs, scene_layouts


# 邻镜人物数变化标志（确定性计算，非语义判断）：本镜相对上一镜【从无人到有人】。
# 用途：画面描述阶段据此注入「人物不得凭空出现」的引入约束——实测《柿子红了》
# 镜2 空镜推进 → 镜3 三人骤然同框，观感是「三个人突然凭空出现」。
CONTINUITY_EMPTY_TO_POPULATED = "empty_to_populated"


def neighbor_continuity_flags(shots: list[dict]) -> list[str]:
    """逐镜计算邻镜衔接标志（仅确定性比较人物数，不做语义推断）。

    判据：上一镜出场角色与上一镜 end_state.who 均为空，且本镜有出场角色
    → 标记 CONTINUITY_EMPTY_TO_POPULATED（本镜必须把人物「引入」画面）。
    """
    flags: list[str] = []
    for idx, sh in enumerate(shots):
        if idx == 0:
            flags.append("")
            continue
        prev = shots[idx - 1]
        prev_who = set(prev.get("character_names") or [])
        prev_who |= set((prev.get("end_state") or {}).get("who") or [])
        cur_who = set(sh.get("character_names") or [])
        flags.append(CONTINUITY_EMPTY_TO_POPULATED
                     if (not prev_who and cur_who) else "")
    return flags


def _shot_prompt_payload(sh: dict) -> dict:
    """单镜画面描述 payload 条目（enrich 主批次/补拍/单镜重生成共用）。

    end_state（批次 2c §32.6）有值时携带——导演据此把末尾小节写到出口状态
    （人物位置/在场人物/道具状态与账本一致），跨镜衔接从计划态出发。
    continuity：邻镜衔接标志（empty_to_populated 时提示词强制「引入人物」写法）。
    """
    out = {"seq": sh["seq"], "duration": sh["duration"],
            "description": sh["description"],
            "excerpt": sh.get("excerpt", ""), "dialogue": sh["dialogue"],
            "scene_name": sh["scene_name"],
            "character_names": sh["character_names"],
            "prop_names": sh.get("prop_names", []),
            "transition_in": sh.get("transition_in", ""),
            "state": sh.get("state") or {}}
    end_state = sh.get("end_state") or {}
    if end_state:
        out["end_state"] = end_state
    if sh.get("_continuity"):
        out["continuity"] = sh["_continuity"]
    # 高风险动作镜标记（SHOT_PROMPT 规则 e：fluid/mirror/collision/transform/hands
    # 必须改用降低幅度的拍法）。**此前 risks 从不进 payload**，而提示词里有这条
    # 规则、`_norm_shot` 也一直在算 risks → 该规则从未被触发过（契约对账，25 号 0.3）。
    if sh.get("risks"):
        out["risks"] = list(sh["risks"])
    return out


def regenerate_shot_prompt(shot: dict, prev_shots: list[dict], style_hint: str,
                           characters: list[dict] | None,
                           scenes: list[dict] | None,
                           props: list[dict] | None,
                           skill_text: str = "", model_skill_text: str = "",
                           issues: list[str] | None = None,
                           cancel_check: CancelCheck | None = None,
                           stream_cb: Callable[[str, str], None] | None = None,
                           ) -> tuple[str, str]:
    """单镜画面描述重生成（时长编辑后时序失配重算 / 手动重抽）。

    `issues`：**可选的确定性问题清单**（如「06—08秒 那 2 秒塞了 21 字，请把
    台词分摊到相邻小节」）。带上它，这次重算就是**定向修复**而不是再抽一次
    ——实测不带清单的重算会复现同类错误、甚至引入新错误（6 镜重算后同类漏写
    仍有 2 条，还新冒出 2 条台词密度超载）。

    shot 为分镜 payload 形态（seq/duration/description/excerpt/dialogue/
    scene_name/character_names/prop_names/transition_in/state）；prev_shots
    为前序镜头摘要（衔接锚点）。返回新 video_prompt，失败抛 ScriptParseError。
    """
    char_briefs, scene_briefs, prop_briefs, scene_layouts = asset_briefs(
        characters, scenes, props)
    # 单镜重生成同样带上邻镜衔接标志（与批量 enrich 同一判定，避免两条路径
    # 行为不一致——「主路径有、旁路没有」是审计 14 号 6 条严重缺陷的共同形态）
    _prev_for_flag = (prev_shots or [])[-1:] if prev_shots else []
    _flag = neighbor_continuity_flags([*_prev_for_flag, shot])[-1]
    if _flag:
        shot = {**shot, "_continuity": _flag}
    payload: dict = {"style": style_hint, "skills": skill_text,
                     "model_skills": model_skill_text,
                     "shots": [_shot_prompt_payload(shot)]}
    # 问题清单（确定性检查的产出）：让它成为**定向修复**而不是再抽一次
    _issue_lines = [str(i).strip() for i in (issues or []) if str(i).strip()]
    if _issue_lines:
        payload["issues"] = _issue_lines
    names = set(shot.get("character_names") or [])
    sname = shot.get("scene_name")
    pnames = set(shot.get("prop_names") or [])
    briefs = {n: char_briefs[n] for n in names if char_briefs.get(n)}
    if briefs:
        payload["character_briefs"] = briefs
    if sname and scene_briefs.get(sname):
        payload["scene_briefs"] = {sname: scene_briefs[sname]}
    # 空间布局（方位法典）：与 brief 分开传——brief 管「画面长什么样」，
    # layout 管「什么东西在哪」，后者是人物调度与空间对账的依据
    if sname and scene_layouts.get(sname):
        payload["scene_layouts"] = {sname: scene_layouts[sname]}
    pbriefs = {n: prop_briefs[n] for n in pnames if prop_briefs.get(n)}
    if pbriefs:
        payload["prop_briefs"] = pbriefs
    if prev_shots:
        payload["previous_shots"] = prev_shots
    data = _llm_call(_prompts.SHOT_PROMPT_SYSTEM_PROMPT,
                     json.dumps(payload, ensure_ascii=False),
                     cancel_check, stream_cb=stream_cb,
                     stream_label="重生成画面描述（思考中）", contract=VpOut)
    for item in data.get("shots") or []:
        if isinstance(item, dict) and _as_str(item.get("video_prompt")):
            # 第二返回值：改写后的 state.position（**只在提示词里出现了姿态那条
            # issue 时才会有**，见 api/drama._shot_issue_lines）。定妆参考图是
            # 正面直立照，「背对/侧身/伏在」会被参考图压过，需改写成取景语言；
            # 空串 = 本镜无需改写，调用方保持原 position 不动
            return (_as_str(item.get("video_prompt")),
                    _as_str(item.get("state_position")))
    raise ScriptParseError("画面描述重生成返回为空，请重试")


def enrich_video_prompts(episodes: list[dict], style_hint: str,
                         progress_cb: ProgressCallback | None = None,
                         cancel_check: CancelCheck | None = None,
                         skill_text: str = "",
                         model_skill_text: str = "",
                         stream_cb: Callable[[str, str], None] | None = None,
                         characters: list[dict] | None = None,
                         scenes: list[dict] | None = None,
                         props: list[dict] | None = None,
                         trace_cb: Callable[[str, str, str], None] | None = None,
                         banned_line: str = "",
                         lessons_text: str = "",
                         ) -> None:
    """逐集为每个分镜生成导演级 video_prompt（就地写回；多集并发，单集失败留空）。

    skill_text：题材技能包正文（LLM 审读自选）；
    model_skill_text：按项目预设文生视频模型注入的提示词技能包正文（可空）；
    characters/scenes/props：资产列表——按每批出场过滤后以 character_briefs/
    scene_briefs/prop_briefs 注入（角色定妆描述、场景陈设与关键道具形态是
    一致性锚点，缺失时 LLM 只能凭空想象，跨镜服装/陈设/道具漂移由此而生）。
    banned_line：用户声明的禁用内容（21 号批次 5，可空）——注入后写进
    【安全合规】段，画面与台词都不得出现。
    lessons_text：**提示词层的历史教训**（`build_lessons_text(ledger, "prompt")`，
    可空）。这是自学习闭环的最后一环：教训账本此前只喂给「拆镜」，写提示词这
    一步收不到任何反馈——而人物未绑定/道具失控/内容过载恰恰全部发生在这里。
    """
    total = len(episodes)
    if total == 0:
        return
    _report(progress_cb, 0.0, f"开始生成 {total} 集画面描述")
    done = {"n": 0}
    timing_stats = {"fixed": 0, "failed": 0}  # 时序对账：修复数/残留数
    lock = threading.Lock()

    # 资产锚定摘要（公共函数，单镜重生成共用）
    char_briefs, scene_briefs, prop_briefs, scene_layouts = asset_briefs(
        characters, scenes, props)

    def _build_payload(batch: list[dict], prev_tail: list[dict]) -> dict:
        """组装单批 payload：按本批出场过滤资产锚定，携带前序镜头衔接摘要。"""
        # 邻镜衔接标志（确定性）：批内逐镜比较 + 批首与 prev_tail 尾镜比较，
        # 供提示词注入「人物不得凭空出现」的引入约束
        _flags = neighbor_continuity_flags(batch)
        if prev_tail:
            _tail_who = set(prev_tail[-1].get("character_names") or [])
            _tail_who |= set((prev_tail[-1].get("state") or {}).get("who") or [])
            if not _tail_who and (batch[0].get("character_names") or []):
                _flags[0] = CONTINUITY_EMPTY_TO_POPULATED
        for _sh, _f in zip(batch, _flags):
            if _f:
                _sh["continuity"] = _f
            else:
                _sh.pop("continuity", None)
        names = {n for sh in batch for n in sh.get("character_names") or []}
        snames = {sh.get("scene_name") for sh in batch if sh.get("scene_name")}
        pnames = {n for sh in batch for n in sh.get("prop_names") or []}
        payload: dict = {"style": style_hint, "skills": skill_text,
                         "model_skills": model_skill_text, "shots": batch}
        if lessons_text.strip():
            # 历史教训（**提示词层**的，见 prompt_layer_lessons）：这是「让模型
            # 自己变聪明」的那一环——把既往踩过的坑连同**具体反面案例**交给它，
            # 由它自己泛化，而不是给每类问题写一条硬规则。
            payload["lessons"] = lessons_text.strip()
        if banned_line.strip():
            # 禁用内容（用户口径）随批注入：写进【安全合规】段
            payload["banned"] = banned_line.strip()
        briefs = {n: char_briefs[n] for n in names if char_briefs.get(n)}
        if briefs:
            payload["character_briefs"] = briefs
        sbriefs = {s: scene_briefs[s] for s in snames if scene_briefs.get(s)}
        if sbriefs:
            payload["scene_briefs"] = sbriefs
        # 空间布局（方位法典）：与 brief 分开传，见 asset_briefs 的说明
        slayouts = {s: scene_layouts[s] for s in snames if scene_layouts.get(s)}
        if slayouts:
            payload["scene_layouts"] = slayouts
        pbriefs = {n: prop_briefs[n] for n in pnames if prop_briefs.get(n)}
        if pbriefs:
            payload["prop_briefs"] = pbriefs
        if prev_tail:
            payload["previous_shots"] = prev_tail
        return payload

    def _enrich_one(ep: dict, ep_index: int) -> None:
        _check_cancel(cancel_check)
        shots_payload = [_shot_prompt_payload(sh) for sh in ep["shots"]]
        # 单集分镜过多时分批（输入过大会导致输出截断丢镜）；批间携带上一批
        # 最后 2 镜摘要（含结束状态），保证批界处的动作与机位衔接
        prompts: dict[int, str] = {}
        prev_tail: list[dict] = []
        total_batches = (len(shots_payload) + 29) // 30
        for bi in range(0, len(shots_payload), 30):
            batch = shots_payload[bi:bi + 30]
            src = ep["shots"][bi:bi + 30]
            payload = _build_payload(batch, prev_tail)
            prev_tail = [{"seq": s["seq"], "description": s["description"][:60],
                          "camera": s.get("camera", ""),
                          "character_names": s.get("character_names") or [],
                          "state": (s.get("state") or {})} for s in src[-2:]]
            try:
                data = _llm_call(_prompts.SHOT_PROMPT_SYSTEM_PROMPT,
                                 json.dumps(payload, ensure_ascii=False),
                                 cancel_check, stream_cb=stream_cb,
                                 stream_label=(f"画面描述《{ep.get('title', '')[:14]}》"
                                               f"批 {bi // 30 + 1}/{total_batches}（思考中）"),
                                 contract=VpOut)
            except ScriptParseError as e:
                logger.warning("《%s》画面描述批次失败：%s", ep.get("title"), e)
                continue
            for item in data.get("shots") or []:
                if isinstance(item, dict) and _as_str(item.get("video_prompt")):
                    prompts[_as_int(item.get("seq"), -1, 1, 10 ** 6)] = \
                        _as_str(item.get("video_prompt"))
        for sh in ep["shots"]:
            sh["video_prompt"] = prompts.get(sh["seq"], "")
        # 时序对账候选：主批次产出后时间小节与 duration 不符的镜头（在补拍
        # 循环里重生成，timing_error 随 payload 注入由提示词 j 条修正）
        err_before = {sh["seq"] for sh in ep["shots"] if timing_error(sh)}
        # 缺漏/对账补拍：漏生成画面描述或时间小节与 duration 不符的分镜单独
        # 重试（最多 2 轮），保证逐镜全覆盖且时序对账（补拍与主批次共用资产
        # 锚定，避免补拍镜头的人物/场景描写漂移）
        for _ in range(2):
            missing: list[tuple[dict, str]] = []
            for sh in ep["shots"]:
                if not (sh.get("video_prompt") or "").strip():
                    missing.append((sh, ""))
                    continue
                err = timing_error(sh)
                if err:
                    missing.append((sh, err))
            if not missing:
                break
            missing_payload = [
                {**_shot_prompt_payload(sh),
                 # 时序对账错误说明（提示词 j 条按本镜 duration 重划时间小节）
                 **({"timing_error": err} if err else {})}
                for sh, err in missing]
            # 前序衔接：取第一个缺失镜之前的 2 镜（缺失镜可能不在批开头，
            # 空上下文会让补拍描写与前文断裂）
            pre_shots = [b for b in shots_payload
                         if b["seq"] < missing[0][0]["seq"]][-2:]
            payload = _build_payload(missing_payload, pre_shots)
            try:
                data = _llm_call(_prompts.SHOT_PROMPT_SYSTEM_PROMPT,
                                 json.dumps(payload, ensure_ascii=False),
                                 cancel_check, contract=VpOut)
            except ScriptParseError:
                break
            for item in data.get("shots") or []:
                if isinstance(item, dict) and _as_str(item.get("video_prompt")):
                    prompts[_as_int(item.get("seq"), -1, 1, 10 ** 6)] = \
                        _as_str(item.get("video_prompt"))
            for sh in ep["shots"]:
                sh["video_prompt"] = prompts.get(sh["seq"], "")
        # 时序对账统计：修复数（重生成后对账通过）与残留数（重试后仍不符，
        # 留痕交用户手动重生成，不阻断解析）
        err_after = {sh["seq"] for sh in ep["shots"] if timing_error(sh)}
        timing_fixed = len(err_before - err_after)
        timing_failed = len(err_after)
        with lock:
            done["n"] += 1
            timing_stats["fixed"] += timing_fixed
            timing_stats["failed"] += timing_failed
            _report(progress_cb, done["n"] / total,
                    f"画面描述：已完成 {done['n']}/{total} 集"
                    f"（本集 {sum(1 for s in ep['shots'] if s['video_prompt'])}"
                    f"/{len(ep['shots'])} 镜）")

    workers = min(3, total)
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="eps-enrich") as pool:
            # enumerate 产出 (下标, 集)——显式按签名传参（ep, ep_index）。
            # 历史 bug：*a 反参导致多集并发时 ep 收到 int 下标，enrich 必炸
            # （单集走 else 分支正确，mock 验证从未覆盖多集——终验抓出）
            list(pool.map(lambda pair: _enrich_one(pair[1], pair[0]),
                          list(enumerate(episodes))))
    else:
        for i, ep in enumerate(episodes):
            _enrich_one(ep, i)
    if trace_cb is not None and (timing_stats["fixed"] or timing_stats["failed"]):
        trace_cb("thought", "时序对账",
                 f"画面描述时间小节与分镜时长对账：修正 {timing_stats['fixed']} 镜"
                 f"（已按本镜时长重新生成画面描述），{timing_stats['failed']} 镜"
                 f"重试后仍不符（可在故事板手动重生成）")
    _gate(trace_cb, "时序对账",
          f"修正 {timing_stats['fixed']} 镜，残留 {timing_stats['failed']} 镜")
    _report(progress_cb, 1.0, "画面描述生成完成")


# ---------- 生图提示词兜底 ----------

def backfill_image_prompts(result: dict, style_hint: str,
                           cancel_check: CancelCheck | None = None,
                           model_skill_text: str = "",
                           stream_cb: Callable[[str, str], None] | None = None) -> None:
    """为缺漏 image_prompt 的角色/场景/道具补写（一次调用；与资产分析解耦兜底）。

    model_skill_text：按项目预设文生图模型注入的提示词技能包正文（可空）。
    """
    chars = [c for c in result.get("characters") or []
             if not (c.get("image_prompt") or "").strip()]
    scenes = [s for s in result.get("scenes") or []
              if not (s.get("image_prompt") or "").strip()]
    props = [p for p in result.get("props") or []
             if not (p.get("image_prompt") or "").strip()]
    if not chars and not scenes and not props:
        return
    payload = {"style": style_hint, "model_skills": model_skill_text,
               "characters": [{"name": c["name"], "role": c.get("role", ""),
                               "persona": c.get("persona", ""),
                               "appearance": c.get("appearance", "")}
                              for c in chars],
               "scenes": [{"name": s["name"], "description": s.get("description", "")}
                          for s in scenes],
               "props": [{"name": p["name"], "description": p.get("description", "")}
                         for p in props]}
    try:
        data = _llm_call(_prompts.IMAGE_PROMPT_SYSTEM_PROMPT,
                         json.dumps(payload, ensure_ascii=False), cancel_check,
                         stream_cb=stream_cb, stream_label="补写生图提示词（思考中）",
                         contract=AssetsOut)
    except ScriptParseError as e:
        logger.warning("生图提示词补全失败（%s 角色 / %s 场景 / %s 道具留空）：%s",
                       len(chars), len(scenes), len(props), e)
        return
    # 角色/场景/道具分开映射：同名时合并映射会互相覆盖（codex review 指出）
    by_char = {item.get("name"): item.get("image_prompt", "")
               for item in data.get("characters") or []
               if isinstance(item, dict) and _as_str(item.get("name"))}
    by_scene = {item.get("name"): item.get("image_prompt", "")
                for item in data.get("scenes") or []
                if isinstance(item, dict) and _as_str(item.get("name"))}
    by_prop = {item.get("name"): item.get("image_prompt", "")
               for item in data.get("props") or []
               if isinstance(item, dict) and _as_str(item.get("name"))}
    for c in chars:
        c["image_prompt"] = _prompts.strip_pose_words(
            _as_str(by_char.get(c["name"])) or c.get("image_prompt", ""))
    for s in scenes:
        s["image_prompt"] = _prompts.strip_scene_figures(
            _as_str(by_scene.get(s["name"])) or s.get("image_prompt", ""))
    for p in props:
        # 道具为白底物件图：规则已禁止人物与摆放关系，逐字采用模型输出
        p["image_prompt"] = _as_str(by_prop.get(p["name"])) or p.get("image_prompt", "")


# ---------- 逐集制作（18 号 §54：拆解 → 确定性整合 → 画面描述） ----------

def apply_asset_aliases(episodes: list[dict], aliases: list[dict]) -> int:
    """把分镜引用里的别名逐字替换成已有名单名（确定性字符串替换）。

    按集提取时 LLM 会在 aliases 里声明「本集叫法 → 名单名」（如「陈老板」→
    「老陈」），代码逐字应用，让本集引用统一到名单名——否则同物异名的引用会被
    随后的资产名单对账当「名单外残留」剔除，白白丢掉参考图锚定。

    **只作用于分镜的结构化引用字段**（角色名/场景名/道具名），不动台词与
    描述文本——别名可能恰好也是台词里的称呼（「陈老板，来碗面」），改写正文
    会篡改对白。仅替换整字段完全相等的情形，不做子串替换（避免误伤）。
    返回替换处数。
    """
    if not aliases:
        return 0
    char_map = {a["from"]: a["to"] for a in aliases if a.get("kind") == "character"}
    scene_map = {a["from"]: a["to"] for a in aliases if a.get("kind") == "scene"}
    prop_map = {a["from"]: a["to"] for a in aliases if a.get("kind") == "prop"}
    n = 0
    for ep in episodes:
        for sh in ep.get("shots") or []:
            names = sh.get("character_names") or []
            new_names = [char_map.get(x, x) for x in names]
            if new_names != names:
                sh["character_names"] = list(dict.fromkeys(new_names))
                n += 1
            props = sh.get("prop_names") or []
            new_props = [prop_map.get(x, x) for x in props]
            if new_props != props:
                sh["prop_names"] = list(dict.fromkeys(new_props))
                n += 1
            scene = sh.get("scene_name") or ""
            if scene in scene_map:
                sh["scene_name"] = scene_map[scene]
                n += 1
    return n


def decompose_episode_full(ep: dict, assets: dict, style_hint: str, *,
                           prev_exit_state: str = "", prev_tail: str = "",
                           key_scene_note: str = "", shot_skill: str = "",
                           lessons_text: str = "", tone_hint: str = "",
                           aliases: list[dict] | None = None,
                           cancel_check: CancelCheck | None = None,
                           stream_cb: Callable[[str, str], None] | None = None,
                           trace_cb: Callable[[str, str, str], None] | None = None
                           ) -> list[dict]:
    """单集拆解 + 确定性整合 + 资产名单对账（画面描述之前的所有步骤）。

    与整部解析时逐集拆解的差别：只做**一集**，且把确定性门禁（节拍整合/语速
    硬门/景别质检/名单对账）就地跑完——单集独立成任务时，这些门禁没有"全集
    一起跑"的时机。

    拆解失败自动重试一次（对齐校验不过多为 LLM 非确定性抖动，整任务重跑的
    代价是重新拆解烧钱）；取消不重试。
    """
    ep_title = ep.get("title") or ""
    ep_body = ep.get("body") or ""

    def _decompose() -> list[dict]:
        return decompose_episode(
            ep_title, ep_body, style_hint, cancel_check,
            skill_text=shot_skill,
            known_characters=[c["name"] for c in assets.get("characters") or []],
            known_scenes=[s["name"] for s in assets.get("scenes") or []],
            known_props=[p["name"] for p in assets.get("props") or []],
            trace_cb=trace_cb, stream_cb=stream_cb,
            prev_ep_tail=prev_tail, prev_exit_state=prev_exit_state,
            key_scene_note=key_scene_note, lessons_text=lessons_text,
            tone_hint=tone_hint)

    try:
        shots = _decompose()
    except ScriptParseError as e:
        if "已取消" in str(e) or (cancel_check and cancel_check()):
            raise
        logger.warning("《%s》拆解失败，自动重试一次：%s", ep_title, e)
        if trace_cb is not None:
            trace_cb("thought", f"《{ep_title}》拆解失败重试",
                     f"{str(e)[:80]}……自动重试一次（LLM 非确定性抖动）")
        shots = _decompose()

    obj = {"seq": ep.get("num") or 1, "title": ep_title, "shots": shots}
    # 确定性门禁（全部不调 LLM）：节拍整合 → 语速硬门 → 景别质检 → 名单对账
    beats = consolidate_beats([obj])
    _gate(trace_cb, "节拍整合",
          f"合并 {beats['merged']} 组拆裂的动作/台词镜，剔除 {beats['removed']} 句跨镜复演")
    floor_stats = apply_duration_floor([obj], [ep_body])
    _gate(trace_cb, "语速硬门",
          f"抬升 {floor_stats['raised']} 镜，拆分 {floor_stats['split']} 镜，"
          f"夹取 {floor_stats['capped']} 镜")
    _check_scale_rhythm([obj], trace_cb)
    filled = _backfill_prop_refs(
        [obj], [p.get("name", "") for p in assets.get("props") or []])
    # 别名归位要**先于**名单对账：同物异名的引用先换成名单名，否则会被当
    # 「名单外残留」剔除（丢掉参考图锚定）
    aliased = apply_asset_aliases([obj], aliases or [])
    if aliased and trace_cb is not None:
        trace_cb("thought", "资产别名归位",
                 f"按本集资产解析给出的别名映射，把 {aliased} 处引用统一到已有名单名")
    removed = _apply_asset_roster([obj], assets)
    _gate(trace_cb, "资产名称对齐",
          f"补引用 {filled}，剔除名单外：道具 {removed['props']}，"
          f"角色 {removed['chars']}，场景 {removed['scenes']}")
    if trace_cb is not None:
        trace_cb("thought", f"第 {obj['seq']} 集拆解完成",
                 f"《{ep_title}》拆出 {len(obj['shots'])} 个分镜"
                 f"（含对齐补镜与确定性整合）")
    return obj["shots"]


def enrich_episode(ep: dict, assets: dict, style_hint: str, *,
                   model_skill_text: str = "", t2v_skill: str = "",
                   cancel_check: CancelCheck | None = None,
                   stream_cb: Callable[[str, str], None] | None = None,
                   trace_cb: Callable[[str, str, str], None] | None = None,
                   progress_cb: ProgressCallback | None = None,
                   banned_line: str = "",
                   lessons_text: str = "") -> None:
    """单集画面描述（就地写回 shots 的 video_prompt）。

    banned_line：用户声明的禁用内容（21 号批次 5，可空）——随批注入，写进
    【安全合规】段。
    lessons_text：**提示词层**历史教训（prompt 段，可空）——见
    `enrich_video_prompts` 的同名参数。
    """
    obj = {"seq": ep.get("num") or 1, "title": ep.get("title") or "",
           "shots": ep.get("shots") or []}
    enrich_video_prompts(
        [obj], style_hint, progress_cb, cancel_check,
        skill_text=_merge_skill_text(model_skill_text, t2v_skill),
        model_skill_text=t2v_skill, stream_cb=stream_cb,
        characters=assets.get("characters") or [],
        scenes=assets.get("scenes") or [],
        props=assets.get("props") or [],
        trace_cb=trace_cb,
        banned_line=banned_line,
        lessons_text=lessons_text)


def qa_repair_episode(ep: dict, assets: dict, style_hint: str, *,
                      skill_text: str = "", t2v_skill: str = "",
                      cancel_check: CancelCheck | None = None,
                      stream_cb: Callable[[str, str], None] | None = None,
                      trace_cb: Callable[[str, str, str], None] | None = None
                      ) -> tuple[list[dict], dict]:
    """单集分镜体检 + 修复循环（ReAct：审核→修复→复查，上限 2 轮）。

    返回 (残留体检项, 修复明细)——明细由 runner 归档到项目教训账本。
    """
    obj = {"seq": ep.get("num") or 1, "title": ep.get("title") or "",
           "shots": ep.get("shots") or []}
    bodies = [ep.get("body") or ""]
    # 注：_qa_repair_loop 的返回注解写的是 list[str]，实际返回
    # {"log": [...], "detail": {...}}（注解过时，行为以实现为准）
    repair = _qa_repair_loop(
        [obj], bodies, cancel_check, stream_cb, style_hint=style_hint,
        skill_text=skill_text, model_skill_text=t2v_skill,
        characters=assets.get("characters") or [],
        scenes=assets.get("scenes") or [],
        props=assets.get("props") or [])
    repair_log = repair.get("log") or []
    if repair_log and trace_cb is not None:
        trace_cb("thought", "分镜体检修复",
                 "；".join(repair_log) + "（复查后残留项见体检报告）")
        _gate(trace_cb, "分镜体检修复", "；".join(repair_log))
    issues = collect_storyboard_issues([obj], bodies)
    errors = sum(1 for i in issues if i["level"] == "error")
    _gate(trace_cb, "分镜体检", f"error {errors}，warn {len(issues) - errors}")
    return issues, repair


# ---------- 主入口 ----------

def _render_skill_audit(audit: dict) -> str:
    """技能包加载审计摘要：渲染为一条可读文案写入过程轨迹（trace 持久化）。"""
    parts = []
    selected, loaded = audit["selected"], audit["loaded"]
    parts.append("已加载：" + ("、".join(loaded) if loaded else "无（LLM 未自选或名称未命中）"))
    missed = [n for n in selected if n not in loaded]
    if missed:
        parts.append("自选未命中：" + "、".join(missed))
    routes = audit["routes"]
    if routes["all"]:
        parts.append("注入全阶段：" + "、".join(routes["all"]))
    if routes["t2i"]:
        parts.append("注入生图提示词：" + "、".join(routes["t2i"]))
    if routes["t2v"]:
        parts.append("注入画面描述：" + "、".join(routes["t2v"]))
    return "；".join(parts)


def _merge_skill_text(*parts: str) -> str:
    """合并多段技能文本（去空、双换行分隔）——供「题材模型适配层 + 模型包」拼接。"""
    sep = chr(10) + chr(10)   # 双换行分隔（避免转义歧义）
    return sep.join(p.strip() for p in parts if p and p.strip())


def _backfill_episode_titles(episode_sections: list[dict], title: str) -> None:
    """集名为空时用剧本标题补位（确定性，不做语义判断）——避免 UI/轨迹里渲染
    成「未命名」「《》」；剧本标题本身为空时保持为空（由展示层兜底）。"""
    if not title:
        return
    for sec in episode_sections:
        if not (sec.get("title") or "").strip():
            sec["title"] = title


def _locate_episode_sections(script_text: str, review: dict) -> list[dict]:
    """定位分集正文（纯本地计算）：LLM 集头定位 + 正则交叉校验；无集头时整篇单集。"""
    _, regex_eps = split_episodes(script_text)
    regex_strong = [e for e in regex_eps if len(e["body"]) >= 500]
    _, episode_sections = split_episodes_by_heads(
        script_text, review["episode_head_lines"])
    # LLM 漏抄部分集头时定位点仍 ≥2（不触发空回退），漏掉的后集会被并入
    # 前一集——与正则强集头交叉校验：正则可定位的完整集更多时改用正则
    llm_strong = [e for e in episode_sections if len(e["body"]) >= 500]
    if len(llm_strong) < len(regex_strong):
        logger.warning("LLM 集头定位 %s 集少于正则 %s 集，改用正则切分",
                       len(llm_strong), len(regex_strong))
        _, episode_sections = split_episodes(script_text)
    if not episode_sections:
        # LLM 定位失败：回退正则集头识别
        _, episode_sections = split_episodes(script_text)
    if not episode_sections:
        # 无任何集头结构：整篇作为单集
        episode_sections = [{"num": 1, "title": "", "body": script_text}]
    return episode_sections


def llm_parse(text: str, progress_cb: ProgressCallback | None = None,
              cancel_check: CancelCheck | None = None,
              style_hint: str = "",
              model_presets: dict[str, str] | None = None,
              trace_cb: Callable[[str, str, str], None] | None = None,
              stream_cb: Callable[[str, str], None] | None = None,
              parse_cache: tuple[Callable[[str], str | None],
                                 Callable[[str, str], None]] | None = None,
              lessons_text: str = "",
              requirements_text: str = "",
              project_id: str = "") -> dict:
    """剧本建档主入口（18 号 §50 阶段 A）：审读 → 分集规划 → 资产分析 → 落库。

    **不再包含终审与拆解/画面描述**——整部终审已由逐集审核（audit_episode）
    替换，拆解与画面描述迁到逐集制作（build_episode）。建档只产出「分集计划
    + 资产名单 + 分集要点表」，单次耗时从 ~88 分钟（10 集 593 镜）降到分钟级，
    失败代价极小（不再出现「全部 LLM 工作做完却在落库前被杀、593 镜全丢」）。

    model_presets：项目生成模型预设（{"t2i": "...", "t2v": "..."}），用于按
    预设模型注入对应的提示词技能包。
    返回结构与落库消费对齐：
    {"title", "logline", "video_style", "aspect_ratio", "skill_audit",
     "skill_texts", "characters", "scenes", "props", "costume_suggestions",
     "episodes": [{seq, title, body, char_start, char_end, synopsis, beats,
                   hooks, plan_source}]}   # 无 shots（逐集制作时才产生）
    """
    text = (text or "").strip()
    if not text:
        raise ScriptParseError("剧本内容为空")
    if not llm_available():
        raise ScriptParseError(
            "LLM 未配置：请在「模型管理-服务配置」中填写 LLM 服务"
            "（AVS_LLM_PROVIDER=openai、AVS_LLM_BASE_URL、AVS_LLM_API_KEY）后重试")

    # 阶段 A1：审读（完整性 + 题材/简介/真实分集目录 + 技能包自选）
    _report(progress_cb, 0.02, "AI 通读剧本，审读结构与完整性")
    if trace_cb is not None:
        trace_cb("stage", "剧本审读中")
    review = review_script(text, cancel_check, stream_cb=stream_cb,
                           model_presets=model_presets,
                           requirements_text=requirements_text)
    style = style_hint  # 画风以项目设定为准（runner 回填，不从剧本提取）
    _report(progress_cb, 0.08, "审读完成")
    if trace_cb is not None:
        trace_cb("thought", "剧本审读结论",
                 f"剧名《{review.get('title')}》｜完整性："
                 f"{'完整剧本' if review['is_complete'] else '不含分集正文'}"
                 f"｜题材标签：{'、'.join(review.get('genre_tags') or []) or '未识别'}"
                 f"｜自选技能包：{'、'.join(review.get('skills') or []) or '无'}"
                 f"｜关键场次：{len(review.get('key_scenes') or [])} 个"
                 f"｜简介：{review.get('synopsis') or '-'}")
        trace_cb("stage", "剧本审读完成", "")

    # 误判保护（确定性）：审读判「不完整」但原文含可辨识的分集正文时强制按
    # 完整处理——杜绝误判把成品剧本拦在门外
    if not review["is_complete"]:
        _, regex_eps = split_episodes(text)
        strong = [e for e in regex_eps if len(e["body"]) >= 500]
        if strong:
            logger.warning("审读误判不完整：发现 %s 个分集正文，按完整剧本处理",
                           len(strong))
            review["is_complete"] = True

    # 技能包渐进式加载：LLM 审读时对照技能目录自选（题材包 + 模型包统一入口，
    # 预设模型已在审读提示词中展示）；第二级按名称加载正文，幻觉名称跳过。
    # 模型包按其声明的 task_type 路由注入生成阶段，代码不做模型字符串匹配
    presets = model_presets or {}
    selected = [n.strip() for n in (review.get("skills") or [])
                if isinstance(n, str) and n.strip()]
    loaded_packs = get_skill_packs(selected)
    # 题材包按【层】路由：三层包各 2000-3000 字，整包同时注入会把大量无关知识
    # 塞进提示词——分层后每阶段只收它需要的一层
    _genre_packs = with_universal_packs(
        [p for p in loaded_packs if not p.task_type])
    skill_text = render_skill_text(_genre_packs, layer=LAYER_STORY)
    shot_skill = render_skill_text(_genre_packs, layer=LAYER_SHOT)
    model_adapt = render_skill_text(_genre_packs, layer=LAYER_MODEL)
    t2i_skill = render_skill_text([p for p in loaded_packs if p.task_type == "t2i"])
    t2v_skill = render_skill_text([p for p in loaded_packs if p.task_type == "t2v"])
    if loaded_packs:
        logger.info("LLM 自选技能包 %s 并注入提示词", [p.name for p in loaded_packs])
    skill_audit = {
        "selected": selected,
        "loaded": [p.name for p in loaded_packs],
        "routes": {
            "story_layer": [p.name for p in _genre_packs],
            "shot_layer": [p.name for p in _genre_packs],
            "model_adapt_layer": [p.name for p in _genre_packs],
            "all": [p.name for p in _genre_packs],
            "t2i": [p.name for p in loaded_packs if p.task_type == "t2i"],
            "t2v": [p.name for p in loaded_packs if p.task_type == "t2v"],
        },
    }
    if trace_cb is not None:
        trace_cb("thought", "技能包加载", _render_skill_audit(skill_audit))

    # 完整性命中「只收成品剧本」：审读判不完整且正则也找不到分集正文时直接报错，
    # 不由 LLM 代笔（写作交给作者与编辑器工具的「AI 扩写」）
    if not review["is_complete"]:
        _report(progress_cb, 0.10, "未检测到分集正文")
        raise ScriptParseError(
            "未检测到分集正文：本产品只解析成品剧本。请确认剧本含分集标题"
            "（如「第1集 标题」）与各集正文后重试；若只有一句话梗概，"
            "可先用「AI 扩写剧本」扩写成完整剧本再解析。")

    # 阶段 A2/A3：分集定位（本地）+ 分集规划/要点表。
    # **建档不碰资产**（18 号 §61 用户口径）：整部资产提取已移除——32k 剧本
    # 实测 ~16 分钟、11 次调用，会让用户在建档阶段干等，违背「一集一集做」；
    # 资产改为每集制作时增量解析（extract_episode_assets），建档只切集 + 提要点。
    episode_sections = _prepare_episode_sections(
        text, review, cancel_check, stream_cb, trace_cb, progress_cb)
    _report(progress_cb, 0.90, f"剧本建档完成：切出 {len(episode_sections)} 集")
    if trace_cb is not None:
        trace_cb("thought", "建档完成（未提取资产）",
                 f"切出 {len(episode_sections)} 集；资产在每集制作时按集解析"
                 "（会把已有名单一并交给 AI，只补新增，不重复解析）")
        trace_cb("stage", "建档完成", "")

    # 前置章节（首集之前的文本，通常是总纲/人物设定）：不属于任何一集，但里面
    # 的角色设定是全剧锚点——随建档结果透出，第 1 集解析资产时一并带入
    front_text = ""
    if episode_sections:
        first_start = int(episode_sections[0].get("char_start") or 0)
        front_text = text[:first_start].strip()

    return {
        "title": review.get("title") or "未命名剧本",
        "logline": (review.get("synopsis") or "未命名剧本")[:100],
        "video_style": "",   # 项目属性，由 runner 回填
        "aspect_ratio": "",
        "skill_audit": skill_audit,
        "skill_texts": {"all": skill_text, "shot": shot_skill,
                        "model_adapt": model_adapt,
                        "t2v": t2v_skill, "t2i": t2i_skill},
        # 建档不产资产（逐集解析）；保留空数组让落库侧结构不变
        "characters": [], "scenes": [], "props": [],
        "episodes": episode_sections,
        "costume_suggestions": [],
        # 前置章节正文（第 1 集资产解析时带上）
        "front_text": front_text,
        # 关键场次（稀疏导演决策层）：逐集制作时按集号注入拆解强化指令，
        # 故必须随建档结果持久化（旧版整部一次解析时是进程内直传）
        "key_scenes": review.get("key_scenes") or [],
    }


def _prepare_episode_sections(text: str, review: dict,
                              cancel_check: CancelCheck | None,
                              stream_cb: Callable[[str, str], None] | None,
                              trace_cb: Callable[[str, str, str], None] | None,
                              progress_cb: ProgressCallback | None) -> list[dict]:
    """分集定位 + 分集规划/要点表（18 号 §51）：返回带正文与要点的分集列表。

    两条路径：**无集头且超阈值** → LLM 按剧情/爆点/钩子切集（plan_episodes）；
    否则 → 沿用原文分集，只提炼分集要点表（summarize_episodes，失败降级为空
    要点，不阻断建档）。
    """
    episode_sections = _locate_episode_sections(text, review)
    _script_title = (review.get("title") or "").strip()
    _backfill_episode_titles(episode_sections, _script_title)
    _annotate_offsets(text, episode_sections)

    if len(episode_sections) < 2 and len(text) >= settings.episode_split_min_chars:
        _report(progress_cb, 0.12, "剧本无分集结构，AI 正在按剧情切分分集")
        if trace_cb is not None:
            trace_cb("stage", "分集规划中")
            trace_cb("thought", "分集规划启动",
                     f"剧本无「第X集」集头且 {len(text)} 字 ≥ 阈值 "
                     f"{settings.episode_split_min_chars} 字——AI 按剧情走向、"
                     "爆点与钩子切分分集（两段式：分块提节拍 → 汇总切集）")
        episode_sections = plan_episodes(
            text, cancel_check, stream_cb=stream_cb, trace_cb=trace_cb,
            progress_cb=lambda frac, label: _report(
                progress_cb, 0.12 + frac * 0.10, label))
        _annotate_offsets(text, episode_sections)
        _backfill_episode_titles(episode_sections, _script_title)
        if trace_cb is not None:
            preview = "；".join(
                "第%s集《%s》%s字" % (s["num"], s.get("title") or "未命名",
                                      len(s["body"]))
                for s in episode_sections[:8])
            trace_cb("thought", "分集规划完成",
                     f"共切出 {len(episode_sections)} 集（按剧情/爆点/钩子）："
                     f"{preview}" + ("……" if len(episode_sections) > 8 else ""))
            trace_cb("stage", "分集规划完成", "")
    else:
        _report(progress_cb, 0.12, "AI 正在提炼各集剧情要点")
        episode_sections = summarize_episodes(
            text, episode_sections, cancel_check, stream_cb=stream_cb,
            trace_cb=trace_cb)
        if trace_cb is not None:
            got = sum(1 for s in episode_sections if s.get("beats"))
            trace_cb("thought", "分集要点提炼完成",
                     f"{got}/{len(episode_sections)} 集已产出剧情要点"
                     "（供逐集审核注入上下集上下文）")
    # 键名归一：内部分集结构用 num（与 split_episodes/summarize 同源），
    # 落库与逐集 runner 消费 seq——在此统一，避免两套键名各写各的
    for sec in episode_sections:
        sec["seq"] = sec.get("num") or sec.get("seq") or 0
    return episode_sections
