"""一句话剧本扩写：仅 LLM 扩写（无规则模板兜底，失败直接报错）。

流程：一句话梗概 → LLM 扩写为完整剧本 → 前端展示供确认编辑 → 再走
LLM 拆解落库（llm_parser.llm_parse）。

创作方法论按需注入：扩写是 v1.1 链路里唯一的 LLM 写作入口，方法论技能包
（`agent/skills/doubao_drama_craft.md`，豆包系四段产物法提炼）在此处加载——
"按需调用"：只有用户点「AI 扩写」才消费，不进解析流水线的技能目录路由。
"""

import re

from app.agent.skills import SKILLS_DIR, load_skill_packs_with_files
from app.core.providers import ProviderError, llm_available, llm_chat

# 标准格式标记：含任一即视为完整剧本，无需扩写
_FORMAT_MARK_RE = re.compile(
    r"第[0-9一二三四五六七八九十百千零两]+[集章幕]|^场景[:：]|^人物[:：]|^△",
    re.MULTILINE,
)

# 一句话梗概判定上限：无标记且长度不超过该值时才触发扩写
_ONE_LINER_MAX = 300

# 创作方法论技能包文件名（与 skills 目录对齐；读不到时扩写退化为纯格式契约）
_CRAFT_PACK_FILE = "doubao_drama_craft.md"

_SYSTEM_PROMPT = (
    "你是短剧编剧。把用户给出的一句话梗概扩写为标准短剧剧本，共 3 集，"
    "每集包含完整场景与分镜。动笔前先在内部完成四段对齐：剧情梗概（起承转合）→ "
    "人物小传（动机/弧线/反派合理动机）→ 主要场景清单（2-4 个，全剧只取用清单内"
    "场景，场景名前后一字不差）→ 再逐集写正文。\n"
    "创作遵循【创作方法论】（单集章节结构、黄金开局、动作外化情绪、台词规范等），"
    "与下方格式规则冲突时以格式规则为准。\n"
    "格式规则（严格遵守）：\n"
    "1. 每集以「第一集 标题」格式开头；\n"
    "2. 「场景：地点 时间」声明场景；\n"
    "3. 「人物：角色1，角色2」声明出场角色；\n"
    "4. 「△ 动作画面描述」一行一个分镜镜头；\n"
    "5. 「角色（OS）：台词」为台词，OS 表示画外音。\n"
    "只输出剧本文本，不要任何解释或额外标记。"
)


def _craft_methodology() -> str:
    """加载创作方法论技能包正文（读取失败返回空串，不阻断扩写）。"""
    try:
        for filename, pack in load_skill_packs_with_files():
            if filename == _CRAFT_PACK_FILE:
                return pack.body
    except Exception:  # noqa: BLE001 方法论缺失不阻断扩写
        pass
    return ""


def _build_system_prompt() -> str:
    """扩写 system prompt：格式契约 + 创作方法论技能包（存在才注入）。"""
    craft = _craft_methodology()
    if not craft:
        return _SYSTEM_PROMPT
    return f"{_SYSTEM_PROMPT}\n\n【创作方法论】\n{craft}"


def is_one_liner(text: str) -> bool:
    """无标准格式标记且长度较短时视为一句话梗概。"""
    text = (text or "").strip()
    if not text or len(text) > _ONE_LINER_MAX:
        return False
    return not _FORMAT_MARK_RE.search(text)


def expand(text: str) -> dict:
    """扩写入口：仅走 LLM。完整剧本原样返回（passthrough），失败抛 ProviderError。

    返回 {text, mode}；mode 为 llm（已扩写）或 passthrough（无需扩写）。
    """
    text = (text or "").strip()
    if not is_one_liner(text):
        # 完整剧本或超长文本：原样返回，由调用方提示无需扩写
        return {"text": text, "mode": "passthrough"}
    if not llm_available():
        raise ProviderError(
            "LLM 未配置：请在「模型管理-服务配置」中填写 LLM 服务后使用 AI 扩写")
    expanded = llm_chat(
        [{"role": "system", "content": _build_system_prompt()},
         {"role": "user", "content": f"一句话梗概：{text}"}],
        temperature=0.8,
    ).strip()
    if not expanded:
        raise ProviderError("AI 扩写返回内容为空，请重试")
    return {"text": expanded, "mode": "llm"}
