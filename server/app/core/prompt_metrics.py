"""提示词质量指标（确定性计算，零 LLM）。

用途：把「提示词干不干净」变成可度量、可对比的数字。设计文档 20 号 §0 的纪律——
**改动前后必须对同一批镜头跑对比，指标不降即为无效改动**。本模块提供那把尺。

指标口径（全部纯字符串计算，不调用任何模型）：

| 指标 | 含义 | 为什么重要 |
| --- | --- | --- |
| `length` | 总字数 | 超长会稀释模型注意力（官方：简洁精确优于堆叠华丽词汇） |
| `ban_count` | 禁令词出现次数 | 实测首帧 3 次、video_prompt 最高 9 次——同一约束重复写是噪音 |
| `constraint_ratio` | 约束类句子字数占比 | 实测曾有「约束占 63%、画面内容仅 17%」的事故 |
| `redundancy_rate` | 重复短语字数占比 | 「重复」是用户最直观的观感问题（实测画风词块出现 4 次） |
| `seg_lengths` | 五段式各段字数 | 分段配额的执行依据（20 号 §1.2 整改 3） |
| `suspect_truncation` | 疑似硬截断（尾部残句 / 句中短残句） | 实测 `opening[:60]` 把句子切成「…动画电影质感、柔）」 |

**边界**：本模块只做**确定性信号**，不做语义判断——同义改写造成的重复（换词写第二遍）
不在此列（那是提示词层的纪律，见 20 号 §1.2 整改 5）。
"""

from __future__ import annotations

import re
import statistics

# —— 常量（禁魔术数字/字符串：全部具名集中定义）——

# 禁令类词：出现即计入 ban_count（同一约束反复写＝注意力噪音）
BAN_WORDS: tuple[str, ...] = ("禁止", "不得", "严禁", "不出现", "避免")

# 约束类句子的判定词：句子含任一即整句计入约束字数
CONSTRAINT_WORDS: tuple[str, ...] = BAN_WORDS + (
    "约束", "仅限", "只出现", "只能", "必须", "一律", "严禁生成", "控制身份",
)

# 句子分隔符（中英文标点 + 换行）：指标计算与裁剪都以「句」为最小单位
SENTENCE_SEP_RE = re.compile(r"[。；！？\n]")
# 短语分隔符（比句子更细一级，用于重复检测：中文逗号/顿号也切）
CLAUSE_SEP_RE = re.compile(r"[，,。；、！？\n·]")

# 重复检测的最小短语长度（4 字起）：低于此长度的高频词（如「画面」）不视为冗余
MIN_CLAUSE_LEN = 4

# 五段式段名（与 SHOT_PROMPT 的契约一致；顺带用于分段配额核对）
SEGMENT_NAMES: tuple[str, ...] = (
    "【全局定义】", "【视频画面】", "【画面要求】", "【声音设计】", "【安全合规】",
)
# 具名引用：调用方按名字取，**不写下标**——下标是魔术数字，且段序调整时会静默错位
SEGMENT_REQUIREMENT: str = SEGMENT_NAMES[2]

# 结尾疑似截断的判定：最后一个标点之后的残句长度 ≤ 此值即报警
SUSPECT_TAIL_MAX_LEN = 2
# 句中短残句形态：标点 + 1-2 字 + 收尾括号（实测 `opening[:60]` 截断的产物形态）
SHORT_BEFORE_CLOSER_RE = re.compile(r"[，,、；;][^，,、；;。！？\n]{1,2}[）)】」』]")
# 逗号后紧跟收尾括号的片段（供「割词」判据取候选：片段是否为他处词的起头）
FRAGMENT_BEFORE_CLOSER_RE = re.compile(r"[，,、；;]([^，,、；;。！？\n]{1,12})[）)】」』]")
# 归一化时剔除的字符（空白与标点）：比较短语重复时忽略排版差异
_NORM_STRIP_RE = re.compile(r"[\s，,。；、！？：:·\-—…“”‘’「」『』（）()【】\[\]]+")

# ============ 23 号 §4：画面内容的「密度」与「清洁度」度量 ============
#
# 口径总则（23 号 §4）：**A 组要「升」**（画面内容写足），**B 组要「降」**（删噪音）。
# 三令五申的一条：`length` / `prompt_budget.over` / `constraint_ratio` 只作观测，
# **不作验收目标**——代理指标一旦成为目标，被优化掉的正是画面细节（23 号 §8）。
# 本模块提供两组的确定性部分；语义判断（细节维度覆盖）不在这里（见该文 §4-A 注）。

# —— A 组：画面内容密度 ——

# 时间小节标记：五段式【视频画面】段内的时间锚，形态「00—02秒：」
# （破折号在真实数据里是 —，但不同模型产出过 –/-/~/－，全部兼容；秒数 1-3 位）
# **两个捕获组 = 起止秒数**（台词密度按小节算时长要用；`group(0)` 仍是完整标记，
# 既有的切分/命中统计不受影响）
BEAT_ANCHOR_RE = re.compile(
    r"(\d{1,3})\s*[—–\-~－]\s*(\d{1,3})\s*秒\s*[:：]?")

# 五段式中承载「本镜内容」的那一段；其余四段（全局定义/画面要求/声音设计/
# 安全合规）以模板成分为主——实测 324 条里它们的中位分别是 193/107/44/86 字，
# 而【视频画面】中位 226 字却是全部创作所在，按时间小节摊开后中位仅 82 字。
PICTURE_SEGMENT_NAME = "【视频画面】"

# 小节字数均衡度目标（23 号 §4-A）：最短 ÷ 中位 ≥ 此值
# 实测基线 0.21（最短 17 字 / 中位 82 字）——同是镜头，方差达 14 倍。
BEAT_BALANCE_TARGET = 0.5

# —— B 组：清洁度（噪音形态）——

# 段名标记泄漏：代码组装的内部段名出现在成品提示词里（实测首帧 21% 命中）。
# 20 号引官方规范：「删除方括号、字段说明和空槽，合并为一个自然段」。
LEAK_SEGMENT_RE = re.compile(
    r"【(?:约束|全局定义|视频画面|画面要求|声音设计|安全合规|画面内容|素材与参考|风格)】")

# 悬空段落引用：**指向一个在成品里并不存在的段落**——比单纯标记泄漏更严重，
# 模型读到「按【全局定义】中的机位声明」时，全文根本没有那一段。
# 实测形态：「机位与朝向按【全局定义】中的机位声明」。
LEAK_DANGLING_REF_RE = re.compile(r"(?:按|见|依|参见|同)【[^】\n]{2,8}】")

# 元说明泄漏：写给「带时间小节的提示词」的说明语句本身进了成品（实测 16.3%）。
# 生图模型读不懂「时间小节」这类内部术语，只会当噪音。
LEAK_META_PHRASES: tuple[str, ...] = (
    "本画面为首帧", "第 0 秒静止定格", "以上时间小节", "该首帧", "本首帧",
)

# 时间流逝词：静止图不能承载「缓缓浮现/淡出」这类随时间变化的过程（实测 15.9%）。
# 【启发式】「缓缓」也可能确指画面内某物的真实状态，本项供审计，不承担拦截。
LEAK_MOTION_WORDS: tuple[str, ...] = (
    "缓缓", "慢慢", "渐渐", "逐渐", "淡出", "淡入", "浮现", "切入正片",
)

# 拼接痕迹：中文不会出现连续同向标点，出现即多来源拼接的产物（实测 15.3%）。
LEAK_DUP_PUNCT_RE = re.compile(r"。。|，，|；；|、、")

# 首帧不该出现的运镜词【启发式】：静止图写运镜 → 模型画成运镜终点或运动模糊。
# 20 号明确要求首帧剥离运镜词，实测仍有 28.6% 泄漏。
# 【会误报】「马车横移入画」里的「横移」是画面内容而非运镜——故只审计不拦截。
FIRST_FRAME_MOTION_WORDS: tuple[str, ...] = (
    "横移", "推近", "推进", "拉远", "摇镜", "跟拍", "升降", "环绕",
    "手持", "变焦", "横摇", "缓推", "缓拉", "镜头前移", "镜头后拉",
)


def _normalize(text: str) -> str:
    """短语归一化：剔除空白与标点，用于「同短语」比较（忽略排版差异）。"""
    return _NORM_STRIP_RE.sub("", text or "")


def split_clauses(text: str) -> list[str]:
    """按标点切短语（保留原文顺序，过滤空串与过短片段）。"""
    return [c.strip() for c in CLAUSE_SEP_RE.split(text or "")
            if len(_normalize(c)) >= MIN_CLAUSE_LEN]


def split_sentences(text: str) -> list[str]:
    """按句子分隔符切句（保留原文顺序，过滤空串）。"""
    return [s.strip() for s in SENTENCE_SEP_RE.split(text or "") if s and s.strip()]


def redundant_chars(text: str) -> int:
    """重复短语的冗余字数：同一短语（归一化后）第 2 次及以后的出现全部计入。

    实测场景：画风词块「3D 动画风格，精致建模，柔和全局光照…」被拼接层重复注入
    4 次；本函数按短语逐个对账，把重复的那部分字数算出来（首次出现不算冗余）。
    """
    clauses = split_clauses(text)
    if not clauses:
        return 0
    counts: dict[str, int] = {}
    total = 0
    for clause in clauses:
        norm = _normalize(clause)
        counts[norm] = counts.get(norm, 0) + 1
        if counts[norm] > 1:  # 第 2 次及以后：整条短语都算冗余
            total += len(clause)
    return total


# 「【视频画面】」后紧跟时间标记（只隔空格/全角空格，无换行）——需要补一个换行。
# 成因：时间小节重排重建该段时吃掉标题与首节之间的分隔（2026-09-30 已修重建逻辑，
# 这里是**写入侧的归一**与历史数据修补共用的一份规则）。
_PICTURE_HEAD_TIGHT_RE = re.compile(r"【视频画面】[ \t　]*(?=\d\d—)")


def normalize_picture_head_newline(video_prompt: str) -> str:
    """把「【视频画面】」与首个时间标记之间的接缝归一成换行（幂等，不动其它内容）。"""
    return _PICTURE_HEAD_TIGHT_RE.sub(f"{PICTURE_SEGMENT_NAME}\n", video_prompt or "")


def split_segments(text: str) -> dict[str, str]:
    """按五段式段名切分正文 → {段名: 段正文}（无段名结构时返回空 dict）。

    `segment_lengths` 是它的字数视图；两者同源，避免两处切分逻辑漂移。
    """
    body = text or ""
    positions: list[tuple[int, str]] = []
    for name in SEGMENT_NAMES:
        idx = body.find(name)
        if idx >= 0:
            positions.append((idx, name))
    if not positions:
        return {}
    positions.sort()
    out: dict[str, str] = {}
    for i, (idx, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(body)
        out[name] = body[idx + len(name):end]
    return out


def segment_lengths(text: str) -> dict[str, int]:
    """按五段式段名切分并统计各段字数（无段名结构时返回空 dict）。"""
    return {name: len(body) for name, body in split_segments(text).items()}


def _is_partial_word_fragment(fragment: str, body: str) -> bool:
    """残片是否为「被割断的词」：它是文中他处出现过的词的**真前缀**。

    判据（精确、低误报）：残片 f 在正文别处存在 `f + 后续字符`（后续字符非标点）
    的出现——即 f 是某个更长词的起头。实测 `opening[:60]` 把「动画电影质感」
    割成「动画电影质」，而「动画电影质感」在正文别处仍存在 → 命中。
    """
    frag = fragment.strip()
    if len(frag) < MIN_CLAUSE_LEN:
        return False
    for m in re.finditer(re.escape(frag), body):
        nxt = body[m.end():m.end() + 1]
        if nxt and nxt not in _NORM_STRIP_RE.pattern and not nxt.isspace():
            return True
    return False


def suspect_truncation(text: str) -> bool:
    """是否疑似存在硬截断（字符级切片切出的破碎 token）。

    实测 `opening[:60]` 把「…3D渲染、动画电影质感、柔和全局光照」切成
    「…动画电影质）」「…柔）」——这类破碎 token 会污染生成（模型读到半个词）。

    三类信号（任一命中即报）：
    ① **尾部残句**：最后一个标点后的残句 ≤ 2 字；
    ② **句中短残句**：逗号后仅 1-2 字即遇收尾括号（`…，柔）` 形态）；
    ③ **割词证据**：括号前的片段是文中他处出现过词的**真前缀**（`…动画电影质）`
       ← 文中另有「动画电影质感」）—— 这是最精确的一类，不依赖长度阈值。

    **保守性说明**：本检测只用于审计存量数据与回归观察，**不承担拦截职责**——
    根治手段是组装侧不做字符切片（见 `clip_at_sentence`）。
    """
    body = (text or "").rstrip()
    if not body:
        return False
    normalized_tail = _normalize(re.split(r"[。；！？，,、：\n]", body)[-1])
    if 0 < len(normalized_tail) <= SUSPECT_TAIL_MAX_LEN:
        return True
    if SHORT_BEFORE_CLOSER_RE.search(body):
        return True
    for m in FRAGMENT_BEFORE_CLOSER_RE.finditer(body):
        if _is_partial_word_fragment(m.group(1), body):
            return True
    return False


def count_ban_words(text: str) -> int:
    """禁令词出现总次数。"""
    body = text or ""
    return sum(body.count(w) for w in BAN_WORDS)


def clip_at_sentence(text: str, limit: int) -> str:
    """按**标点边界**裁剪到不超过 limit 字（**不切碎词句**）。

    实测事故：旧实现用 `opening[:60]` 字符切片，把「…3D渲染、动画电影质感、
    柔和全局光照」切成「…动画电影质）」——残留半个词，模型读到破碎 token。

    实现：以「标点为不可切单位」贪心装填（标点含 `，、；。！？`），
    放不下下一段就停。
    - 不先用句号级切分：中文分镜描述常是长句，句号级装填会把 60 字额度
      浪费到只剩十几字（实测 274 字段落只装进 14 字）；
    - 只有通篇无标点时才退化为字符切片（极端兜底）。
    """
    body = (text or "").strip()
    if limit <= 0:
        return ""
    if len(body) <= limit:
        return body
    acc = ""
    for part in re.split(r"(?<=[。；！？，,、])", body):
        if not part:
            continue
        if len(acc) + len(part) > limit:
            break
        acc += part
    if acc:
        return acc.rstrip("，,、；; ")
    # 通篇无标点（极少见）：退化字符切片
    return body[:limit]


def constraint_chars(text: str) -> int:
    """约束类句子的字数（含任一约束词即整句计入）。"""
    return sum(len(s) for s in split_sentences(text)
               if any(w in s for w in CONSTRAINT_WORDS))


def prompt_quality_metrics(text: str,
                           segments: dict[str, int] | None = None) -> dict:
    """提示词质量指标总入口：返回可直接塞进 trace_json.generation 的 dict。

    全部为确定性计算，可安全地在每次生成时调用（成本可忽略）。

    `segments`：调用方**显式给出的分段字数**。首帧提示词走「画风/画面内容/
    素材与参考/约束」四段组装，**没有五段式那类段名标记**，从成品文本反推不出
    分段（实测 `segment_lengths` 对 t2i 恒为空——那是正确行为，不是埋点坏了）。
    给了就用它；不给则回退按段名解析（视频五段式那一侧）。
    """
    body = text or ""
    length = len(body)
    if length == 0:
        return {}
    redundant = redundant_chars(body)
    constrained = constraint_chars(body)
    segs = dict(segments) if segments else segment_lengths(body)
    return {
        "length": length,
        "ban_count": count_ban_words(body),
        # 冗余率与约束占比均保留 4 位小数，便于前后对比
        "redundancy_rate": round(redundant / length, 4),
        "redundancy_chars": redundant,
        "constraint_ratio": round(constrained / length, 4),
        "suspect_truncation": suspect_truncation(body),
        "seg_lengths": segs,
    }


# ============ 23 号 §4：A 组（密度）/ B 组（清洁度）的度量入口 ============


def _hits(pattern: re.Pattern, body: str) -> list[str]:
    """正则命中的去重片段（保留原文，供报告逐条展示）。"""
    return sorted({m.group(0) for m in pattern.finditer(body)})


def _word_hits(words: tuple[str, ...], body: str) -> list[str]:
    """词表命中的去重词（启发式项专用；各项的边界见常量处注释）。"""
    return sorted({w for w in words if w in body})


def beat_lengths(picture_text: str) -> list[int]:
    """把【视频画面】段正文按时间小节标记切分，返回各小节字数（不含标记本身）。

    实测基线（23 号 §1.4，324 条 video_prompt）：该段中位 226 字，按小节摊开后
    **中位仅 82 字**——而 SHOT_PROMPT 对这一段的要求是「细节密度拉满：身体部位级
    动作 + 微表情 + 道具交互 + 环境反馈」。本函数提供那个「摊开」的动作。

    无时间小节标记时返回空列表（该镜不按小节度量，不是错误）。
    """
    body = picture_text or ""
    marks = list(BEAT_ANCHOR_RE.finditer(body))
    if not marks:
        return []
    out: list[int] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append(max(0, end - m.end()))
    return out


def picture_density(video_prompt: str) -> dict:
    """【视频画面】段的密度指标（23 号 §4-A 的确定性部分）。

    返回空 dict 表示无五段式结构（首帧提示词或旧格式）——不是错误，
    调用方按「该提示词不适用本组指标」处理。

    字段：`chars` 段字数 / `beat_count` 小节数 / `beats` 各小节字数 /
    `beat_median` 每小节字数中位（**A 组主指标**）/ `beat_min` 最短 /
    `balance` 最短 ÷ 中位（目标见 `BEAT_BALANCE_TARGET`）。
    """
    seg = split_segments(video_prompt).get(PICTURE_SEGMENT_NAME)
    if seg is None:
        return {}
    beats = beat_lengths(seg)
    if not beats:
        return {"chars": len(seg.strip()), "beat_count": 0, "beats": [],
                "beat_median": 0, "beat_min": 0, "balance": 0.0}
    median = statistics.median(beats)
    return {
        "chars": len(seg.strip()),
        "beat_count": len(beats),
        "beats": beats,
        "beat_median": int(median),
        "beat_min": min(beats),
        "balance": round(min(beats) / median, 4) if median else 0.0,
    }


# 台词密度上限（字/秒）：中文正常播报 4-5、急促上限约 6。超过即超载——原生配音
# 只能把话加速念完，成片听感就是「语速快得不像话」。
# **与拆镜期的整镜折算是两个粒度**：那时按 3.5-4 字/秒校验的是「整镜装不装得下」，
# 管不了台词在时间小节之间分得匀不匀——实测一镜整镜 10s 装 3 句 39 字是合格的，
# 但某个 2s 小节被塞了 2 句 26 字 = **13 字/秒**（快 3 倍）。
DIALOGUE_RATE_MAX = 6.0


def shot_dialogue_rate(duration: int, dialogues: list[str] | None = None) -> dict:
    """**整镜**语速（确定性）：台词总字数 ÷ 镜头时长 → {"chars","secs","rate"}。

    与 `dialogue_rate_by_beat`（小节级）是**两个维度**，别混：

    | 维度 | 管什么 | 重排小节能改善吗 |
    | --- | --- | --- |
    | 小节级 | 台词落在哪个小节、那一节装不装得下 | **能** |
    | **整镜级（本函数）** | **总时长装不装得下全部台词** | **不能**——总量与总时长都没变 |

    实测（2026-09-27 全库 342 个有台词的镜头）：30% 偏快（4~6 字/秒 16%、
    6~8 字/秒 7%、**>8 字/秒 5%**），最狠一镜 **5 秒 86 字 = 17.2 字/秒**
    ——物理上念不完。正常对白 4~5 字/秒。
    """
    secs = int(duration or 0)
    chars = sum(len((d or "").strip()) for d in (dialogues or []))
    return {"chars": chars, "secs": secs,
            "rate": round(chars / secs, 1) if secs > 0 else 0.0}


def dialogue_rate_by_beat(video_prompt: str,
                          dialogues: list[str] | None = None) -> list[dict]:
    """按时间小节算台词密度（确定性）→ [{start, end, secs, chars, rate}]。

    用**已知台词文本**定位归属小节，不靠引号形态猜测——台词在提示词里的写法
    不固定（带引号 / 无引号 / 熔进句子流里都出现过，同一批次两种形态并存）。

    无五段式结构、或无时间小节时返回空列表（该提示词不适用本度量）。
    """
    seg = split_segments(video_prompt).get(PICTURE_SEGMENT_NAME)
    if seg is None:
        return []
    marks = list(BEAT_ANCHOR_RE.finditer(seg))
    if not marks:
        return []
    beats: list[dict] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(seg)
        start, stop = int(m.group(1)), int(m.group(2))
        secs = stop - start
        body = _normalize(seg[m.end():end])
        chars = 0
        for d in dialogues or []:
            nd = _normalize(d)
            if nd and nd in body:
                chars += len((d or "").strip())
        beats.append({
            "start": start, "end": stop, "secs": secs, "chars": chars,
            "rate": round(chars / secs, 1) if secs > 0 else 0.0,
        })
    return beats


# 时间小节的**最短时长**（秒）：重平衡时不允许把邻居压得比它更短——太短的
# 小节在成片里只是一个闪帧，画面内容等于被丢弃。
MIN_BEAT_SECS = 1


def rebalance_beat_marks(video_prompt: str, dialogues: list[str] | None = None,
                         max_rate: float = DIALOGUE_RATE_MAX) -> str:
    """把**超载小节**的边界向相邻小节扩展，直到台词密度达标（确定性）。

    只改**时间标记**（`00—03秒` 这类），**不动画面描述文本**——扩展后相邻小节
    原有的画面描述仍留在原处，只是归属的时间窗变了。覆盖 duration 与无缝衔接
    由「总时长不变、边界连续」保证。

    **为什么不让 LLM 做**：实测带问题清单的重算**会调整小节划分但不给台词节
    扩容**（镜11 从 3 节切成 4 节，台词那节仍是 3 秒 20 字 = 6.7 字/秒），还会
    引入新问题（同一次重算把场景引用弄丢了）。密度是**算术**，该由代码保证；
    LLM 那边只保留「按新时间窗重新分配动作」这一层。

    借时间的顺序：先前节借（把起点前移）→ 再后节借（把终点后移）→ 都借不够
    则两边均摊。任何一步会把邻居压到 `MIN_BEAT_SECS` 以下就停止（改不动就不改，
    交给体检继续报——不制造更差的结果）。
    """
    seg = split_segments(video_prompt).get(PICTURE_SEGMENT_NAME)
    if seg is None:
        return video_prompt
    marks = list(BEAT_ANCHOR_RE.finditer(seg))
    if len(marks) < 2:
        return video_prompt
    beats = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(seg)
        beats.append({
            "start": int(m.group(1)), "end": int(m.group(2)),
            "head": m.group(0), "body": seg[m.end():end],
        })
    # 每节的台词字数
    for b in beats:
        norm_body = _normalize(b["body"])
        b["chars"] = sum(len((d or "").strip()) for d in (dialogues or [])
                         if _normalize(d) and _normalize(d) in norm_body)
    changed = False
    for i, b in enumerate(beats):
        span = b["end"] - b["start"]
        if span <= 0 or b["chars"] / span <= max_rate:
            continue
        need = -(-b["chars"] // int(max_rate))  # 向上取整：达标所需秒数
        delta = need - span
        if delta <= 0:
            continue
        # 先前节借：起点前移（第一节的起点固定为 0，借不到）
        if i > 0:
            room = (b["start"] - beats[i - 1]["start"]) - MIN_BEAT_SECS
            take = min(delta, max(0, room))
            if take:
                b["start"] -= take
                beats[i - 1]["end"] = b["start"]
                delta -= take
                changed = True
        # 再后节借：终点后移（最后一节的终点固定，借不到）
        if delta > 0 and i + 1 < len(beats):
            room = (beats[i + 1]["end"] - b["end"]) - MIN_BEAT_SECS
            take = min(delta, max(0, room))
            if take:
                b["end"] += take
                beats[i + 1]["start"] = b["end"]
                delta -= take
                changed = True
        # 还差就两边均摊（各借一半，仍受 MIN_BEAT_SECS 约束）
        if delta > 0 and i > 0 and i + 1 < len(beats):
            half = -(-delta // 2)
            front_room = (b["start"] - beats[i - 1]["start"]) - MIN_BEAT_SECS
            back_room = (beats[i + 1]["end"] - b["end"]) - MIN_BEAT_SECS
            take_f = min(half, max(0, front_room))
            take_b = min(delta - take_f, max(0, back_room))
            if take_f:
                b["start"] -= take_f
                beats[i - 1]["end"] = b["start"]
            if take_b:
                b["end"] += take_b
                beats[i + 1]["start"] = b["end"]
            if take_f or take_b:
                changed = True
    if not changed:
        return video_prompt
    # 重建【视频画面】段：时间标记换成新边界，正文原样
    rebuilt = "".join(
        f"{b['start']:02d}—{b['end']:02d}秒：{b['body']}" for b in beats)
    idx = video_prompt.find(PICTURE_SEGMENT_NAME) + len(PICTURE_SEGMENT_NAME)
    end = video_prompt.find(SEGMENT_REQUIREMENT, idx)
    if end < 0:
        end = len(video_prompt)
    # 同 rescale：标题与首节之间的换行/空行必须保留（重建只拼小节本体）
    head_gap = seg[:marks[0].start()] if marks else "\n"
    sep = head_gap if head_gap.strip() == "" else "\n"
    return video_prompt[:idx] + sep + rebuilt + video_prompt[end:]


def rescale_beat_marks(video_prompt: str, duration: int) -> str:
    """把小节时间轴**按比例缩放**到 duration（确定性）。

    LLM 写的小节常与 duration 不符——实测 13 镜：末节止于 5 秒而 duration 是 6，
    或小节一直排到 10 秒而 duration 只有 5。`SHOT_PROMPT` 的 j 规则要求了「首节
    从 00 秒起、相邻小节无缝衔接、末节止于该镜 duration」，但模型不遵守，而此前
    只有**体检告警**（`timing_error` → 前端「时长待重算」标签）没有对账修复，
    于是用户点了重算标签还在（重算仍可能写错）。

    按比例缩放既保住相对节奏、又保证首尾对齐。取整时用「前一节终点即本节起点」
    吸收累积误差，并要求每节 ≥ `MIN_BEAT_SECS`；**空间不足时原样返回**（不硬凑
    出零长小节）。只改时间标记，画面描述文本原样。
    """
    seg = split_segments(video_prompt).get(PICTURE_SEGMENT_NAME)
    if seg is None or duration <= 0:
        return video_prompt
    marks = list(BEAT_ANCHOR_RE.finditer(seg))
    if not marks:
        return video_prompt
    beats: list[dict] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(seg)
        beats.append({"start": int(m.group(1)), "end": int(m.group(2)),
                      "body": seg[m.end():end]})
    # **以每节的长度为准重排**（而不是用绝对边界）——LLM 除了「总和 ≠ duration」
    # 还会写出**断档**（0—3 秒后接 4 秒起，中间空一秒）与**重叠**；用长度累加重建
    # 能把三种形态一次归正（实测镜2 就是断档，按绝对边界缩放不动它）
    lengths = [max(0, b["end"] - b["start"]) for b in beats]
    total = sum(lengths)
    # 已规整（总和 == duration 且每节起点 = 前面长度累加，无断档/重叠）→ 原样返回
    if total == duration:
        acc = 0
        aligned = True
        for b, ln in zip(beats, lengths):
            if b["start"] != acc:
                aligned = False
                break
            acc += ln
        if aligned:
            return video_prompt
    n = len(beats)
    if total <= 0 or n * MIN_BEAT_SECS > duration:
        return video_prompt  # 空间不足以放下 n 个最短小节，原样返回
    scale = duration / total
    bounds = [0]
    for i, ln in enumerate(lengths[:-1]):
        remain = n - 1 - i  # 本节点之后还有几节（含末节）
        lo = bounds[-1] + MIN_BEAT_SECS
        hi = duration - remain * MIN_BEAT_SECS
        if lo > hi:
            return video_prompt
        bounds.append(max(lo, min(hi, bounds[-1] + round(ln * scale))))
    bounds.append(duration)
    rebuilt = "".join(
        f"{bounds[i]:02d}—{bounds[i + 1]:02d}秒：{beats[i]['body']}"
        for i in range(n))
    idx = video_prompt.find(PICTURE_SEGMENT_NAME) + len(PICTURE_SEGMENT_NAME)
    end = video_prompt.find(SEGMENT_REQUIREMENT, idx)
    if end < 0:
        end = len(video_prompt)
    # **标题与首节之间的分隔必须保留**（2026-09-30 用户报障「【视频画面】后没
    # 换行」）：重建只拼小节本体，原实现把这段空白吃掉了——取原文里标题到首个
    # 时间标记之间的内容（空白则原样沿用，异常时退回一个换行）
    head_gap = seg[:marks[0].start()]
    sep = head_gap if head_gap.strip() == "" else "\n"
    return video_prompt[:idx] + sep + rebuilt + video_prompt[end:]


def first_frame_noise(text: str) -> dict[str, list[str]]:
    """首帧提示词的清洁度信号（23 号 §4-B）→ {类别: [命中片段]}。

    **边界（重要）**：只做**形态判定**。`segment_marks` / `dangling_refs` /
    `time_anchors` / `dup_punct` 是字面事实；`meta_phrases` / `motion_words` /
    `camera_motion` 含**启发式**（「马车横移入画」里的「横移」是画面内容而非运镜；
    片尾字幕卡的「缓缓浮现」是合理的），因此全部只用于审计存量与回归观察，
    **不承担拦截职责**——删不删由 LLM 精炼环节判断（23 号 §3.3）。
    """
    body = text or ""
    out: dict[str, list[str]] = {
        "segment_marks": _hits(LEAK_SEGMENT_RE, body),
        "dangling_refs": _hits(LEAK_DANGLING_REF_RE, body),
        "time_anchors": _hits(BEAT_ANCHOR_RE, body),
        "meta_phrases": _word_hits(LEAK_META_PHRASES, body),
        "motion_words": _word_hits(LEAK_MOTION_WORDS, body),
        "camera_motion": _word_hits(FIRST_FRAME_MOTION_WORDS, body),
        "dup_punct": _hits(LEAK_DUP_PUNCT_RE, body),
    }
    return {k: v for k, v in out.items() if v}
