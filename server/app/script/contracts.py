"""LLM 输出契约（批次 2b §32.3）：结构化输出的宽松 schema 校验与定向重试。

设计口径（报告 APO-04/05 思想，codex 评审后落地）：
- schema 宽松：只拦「结构性错误」（缺 shots 列表 / dialogue 非列表 / 关键
  字段缺失或类型错），字段级容错归一（_norm_shot 等）保留为防御层——契约
  不拦「内容瑕疵」，误拦的门比没有门更糟；
- 校验失败把具体违规项（字段路径 + 原因）拼进重试消息定向重试一次，再失败
  走 _llm_call 既有三档阶梯——重试有据，不再笼统换档；
- schema 与提示词的输出格式说明同源演进：改 schema 必须同步提示词（批次
  2b 提示词外置后由外置文件承载，见 §32.4）。
"""

from pydantic import BaseModel, ConfigDict, ValidationError


class _Loose(BaseModel):
    """宽松基类：多余字段放行（防御层清理），只约束声明字段。"""

    model_config = ConfigDict(extra="allow")


class NamedItem(_Loose):
    """带名条目（角色/场景/道具——name 是资产库锚点，缺失即结构错误）。"""

    name: str


class IssueItem(_Loose):
    """终审问题条目（v1.2 定位契约）：quote 是自动修复的定位锚。

    quote 必须是剧本原文的逐字摘句——修复端用它在文本中做字符串匹配定位，
    转述/改写会使补丁无法命中。跨集定位由 `location` 文本承载。

    原另有 `episodes: list[int]`（注释称「供修复路由」），但**提示词从不声明它、
    代码从不读它**——模型不会产出、也没有消费方，属死契约字段（25 号批次 0.3
    契约对账移除；需要跨集路由时按同一处的方式重新立项，别留半截字段）。
    """

    description: str
    severity: str = "major"
    quote: str


class ReviewOut(_Loose):
    """审读输出（阶段 0）：完整性判定与分集目录是主链路分流依据，必填。"""

    title: str
    is_complete_script: bool
    skills: list[str] = []
    episode_head_lines: list[str] = []
    key_scenes: list[dict] = []
    genre_tags: list[str] = []
    synopsis: str = ""


class AuditOut(_Loose):
    """逐集审核输出（18 号 §52）：passed 是放行/待修复裁决依据，必填；
    exit_state 为该集出口状态快照（供下一集审核与拆解做衔接锚）。

    craft_note（2026-10-10 用户拍板）：**钩子与冲突强度的经营提醒**——只展示、
    不进 issues、不参与 pass/fail、不参与一键修复（用户明确要求「不要放进
    一键修复」）。它是给编剧的追看性提醒，不是拍摄硬伤。
    """

    passed: bool
    verdict: str = ""
    issues: list[IssueItem] = []
    exit_state: str = ""
    craft_note: str = ""


class BeatItem(_Loose):
    """节拍条目（分块提节拍）：anchor 是**逐字原句**，是后续切集的定位锚。

    转述/改写会让锚句在原文中匹配不上——与 IssueItem.quote 同一条纪律。
    """

    anchor: str
    kind: str = "setup"   # setup | turn | climax | hook
    note: str = ""


class EpisodeBeatsOut(_Loose):
    """分块节拍表输出（SCRIPT_EPISODE_BEATS）。"""

    beats: list[BeatItem] = []


class EpisodePlanItem(_Loose):
    """分集条目（分集规划 / 要点提炼共用）。

    start_anchor 仅 LLM 分集路径需要（代码据此逐字定位切分）；剧本已有集头时
    由本地切分决定边界，该字段留空。
    """

    seq: int
    title: str = ""
    synopsis: str = ""
    beats: list[str] = []
    hooks: list[dict] = []
    start_anchor: str = ""
    exit_hint: str = ""


class EpisodeTableOut(_Loose):
    """分集要点表输出（SCRIPT_EPISODE_PLAN / SCRIPT_EPISODE_SUMMARY）。"""

    episodes: list[EpisodePlanItem] = []


class AssetsOut(_Loose):
    """资产提取输出（按集）：角色/场景/道具均为带名条目。

    用户消息含「已有资产名单」时，characters/scenes/props **只应含新增**，
    已有资产的别名写进 aliases（代码据此把本集引用统一到名单名）。
    """

    characters: list[NamedItem] = []
    scenes: list[NamedItem] = []
    props: list[NamedItem] = []
    aliases: list[dict] = []
    # 场景组（32 号）：组名 → 组环境设定；scene 条目的 group 字段指向这里的 name。
    # 条目结构宽松（{name, env_setting}），脏数据在 llm_parser 里确定性归一。
    scene_groups: list[dict] = []


class ShotItem(_Loose):
    """分镜条目（拆解/补镜）：excerpt 与 description 至少承载其一。"""

    excerpt: str = ""
    description: str = ""


class ShotsOut(_Loose):
    """分镜列表输出（SCRIPT_PARSE / SHOT_FILL）。"""

    shots: list[ShotItem]


class VpShotItem(_Loose):
    """画面描述条目（导演级 video_prompt，seq 对账键）。"""

    seq: int
    video_prompt: str
    # 改写后的 state.position（**仅在该镜被判「姿态用了指令语言」时返回**，
    # 见 api/drama._shot_issue_lines 的姿态那一条）。定妆参考图是正面直立照，
    # 「背对/侧身/伏在」这类姿态指令会被参考图压过，必须改写成取景语言
    # （「只见背影与后脑」）——改写是语义工作，交给模型；落库与否则由代码决定
    state_position: str = ""


class VpOut(_Loose):
    """画面描述输出（SHOT_PROMPT 主批次/补拍/单镜重生成）。"""

    shots: list[VpShotItem]


class SuggestionItem(_Loose):
    """妆造建议条目（character 键是角色卡匹配依据）。"""

    character: str = ""


class CostumeOut(_Loose):
    """妆造建议输出。"""

    suggestions: list[SuggestionItem] = []


class GroupsOut(_Loose):
    """场景归并分组输出（SCENE_MERGE）。"""

    groups: list[list[str]] = []


class AutoReviewOut(_Loose):
    """视觉自动判级输出（批次 3.5 §34.3）：verdict 白名单在调用方收敛。"""

    verdict: str
    evidence: str = ""


class PatchItem(_Loose):
    """单个编辑补丁：quote＝要改的逐字原句，new＝修复后的句子。"""

    index: int
    quote: str
    new: str


class PatchOut(_Loose):
    """补丁生成输出（一键优化）：每条问题至少一个补丁，由代码逐字应用。"""

    patches: list[PatchItem] = []


def contract_violations(data: object, contract: type[BaseModel]) -> list[str]:
    """校验 data 是否满足契约：返回违规项列表（字段路径 + 原因，截取 6 条）。

    空列表 = 通过。ValidationError 的 loc 精确到字段路径，直接可拼进重试
    消息——LLM 拿到的是「哪里错了」而不是「又失败了」。
    """
    try:
        contract.model_validate(data)
        return []
    except ValidationError as e:
        return [f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                for err in e.errors()[:6]]


def contract_retry_suffix(violations: list[str]) -> str:
    """定向重试的 user 消息后缀：把违规项原样点名，要求修复后完整重输。"""
    bullets = "\n".join(f"- {v}" for v in violations)
    return (f"\n\n【上一次输出的结构违规（必须全部修复）】\n{bullets}\n"
            "请严格按系统提示规定的输出格式重新输出完整 JSON，修复上述全部"
            "违规；除修复违规外不得改变任何内容。")
