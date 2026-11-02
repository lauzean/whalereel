"""剧本智能体 Skill 技能包库：Claude Code 式渐进式加载（目录 → LLM 自选 → 按名加载）。

技能包为 markdown 文件（agent/skills/ 目录），frontmatter 声明名称与元信息，
正文是专业分镜/制作/提示词知识。全部技能包统一走渐进式加载，代码不做模型/语义匹配：
- 第一级：render_skill_directory() 把全部技能包的「名称+适用+摘要」随审读提示词注入，
  由 LLM 对照剧本题材与项目预设生成模型自主选择（模型 ID 可能带 provider 前缀，
  目录展示「适用模型」声明，由 LLM 判断对应关系，不依赖字符串规则）；
- 第二级：get_skill_packs() 按名称精确加载 LLM 点名的包正文，幻觉选中的名称跳过。
frontmatter 字段仅作目录展示与注入路由，不是匹配规则：
- genres：题材适用标签（题材知识包，注入全阶段）；
- models：适用模型说明（模型提示词包，目录展示供 LLM 对照项目预设）；
- task_type：注入路由 t2i→生图提示词 / t2v→画面描述（模型提示词包）。
新增一种制作知识 = 新增一个 md 文件，无需改代码。
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 技能包目录
SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# frontmatter 字段行（key: value）
_FRONT_KEY_RE = re.compile(r"^([a-zA-Z_]+)\s*:\s*(.+)$")
# genres 列表解析（[a, b] 或 a、b）
_LIST_SPLIT_RE = re.compile(r"[,\[\]、\s]+")


@dataclass
class SkillPack:
    """单个技能包：名称、题材标签、适用模型、注入任务类别、简介、正文。"""

    name: str
    # 题材适用标签（题材知识包声明，目录展示）
    genres: list[str] = field(default_factory=list)
    # 适用模型说明（模型提示词包声明，目录展示供 LLM 对照项目预设，不做代码匹配）
    models: list[str] = field(default_factory=list)
    # 注入路由任务类别（t2i|t2v）：模型包声明注入哪个生成阶段；空 = 全阶段
    task_type: str = ""
    description: str = ""
    body: str = ""


def _parse_skill_text(text: str, default_name: str, source: str) -> SkillPack | None:
    """解析技能包文本：--- 包围的 frontmatter（name/genres/models/task_type/description）+ 正文。

    default_name 为无 frontmatter name 时的兜底名（文件版传文件名 stem），
    source 仅用于日志定位。
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        logger.warning("技能包缺少 frontmatter，跳过：%s", source)
        return None
    front_raw, body = m.group(1), m.group(2).strip()
    name, genres, models, task_type, description = default_name, [], [], "", ""
    for line in front_raw.splitlines():
        fm = _FRONT_KEY_RE.match(line.strip())
        if not fm:
            continue
        key, value = fm.group(1).lower(), fm.group(2).strip()
        if key == "name":
            name = value
        elif key == "genres":
            genres = [g for g in _LIST_SPLIT_RE.split(value) if g]
        elif key == "models":
            models = [x.strip() for x in _LIST_SPLIT_RE.split(value) if x.strip()]
        elif key == "task_type":
            task_type = value.lower()
        elif key == "description":
            description = value
    # 题材技能包（genres）与模型技能包（models）至少声明其一
    if not genres and not models:
        logger.warning("技能包未声明 genres/models，跳过：%s", source)
        return None
    if models and not task_type:
        logger.warning("模型技能包未声明 task_type，将注入全部生成阶段：%s", source)
    return SkillPack(name=name, genres=genres, models=models, task_type=task_type,
                     description=description, body=body)


def _parse_skill_file(path: Path) -> SkillPack | None:
    """解析技能包 md 文件（读取失败/格式不合法返回 None 并记日志）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("技能包读取失败 %s：%s", path.name, e)
        return None
    return _parse_skill_text(text, path.stem, path.name)


def load_skill_packs_with_files() -> list[tuple[str, SkillPack]]:
    """加载全部技能包并附带文件名（按文件名排序；供管理 UI 与注入共用）。"""
    if not SKILLS_DIR.exists():
        return []
    items: list[tuple[str, SkillPack]] = []
    for path in sorted(SKILLS_DIR.glob("*.md")):
        pack = _parse_skill_file(path)
        if pack:
            items.append((path.name, pack))
    return items


def load_skill_packs() -> list[SkillPack]:
    """加载全部技能包（按文件名排序，保证目录顺序稳定）。"""
    return [pack for _filename, pack in load_skill_packs_with_files()]


def render_skill_directory() -> str:
    """技能目录（渐进式加载第一级）：全部技能包每行「名称+适用+摘要」。

    始终随审读提示词注入，供 LLM 自行判断需要哪个技能包——题材包对照剧本题材、
    模型包对照项目预设生成模型，选择权在 LLM。
    """
    packs = load_skill_packs()
    if not packs:
        return ""
    lines = []
    for p in packs:
        # 适用范围：模型包展示适用模型，题材包展示题材标签
        applies = "、".join(p.models) if p.models else "、".join(p.genres)
        lines.append(f"- {p.name}（适用：{applies}）：{p.description}")
    return "\n".join(lines)


def get_skill_packs(names: list[str]) -> list[SkillPack]:
    """按名称加载 LLM 选中的技能包对象（渐进式加载第二级）。

    仅按名称精确查找——代码不做语义/模型匹配；LLM 选了不存在的名称时跳过。
    返回的包对象带 task_type 等元数据，供调用方按注入位置分组渲染。
    """
    by_name = {p.name: p for p in load_skill_packs()}
    loaded: list[SkillPack] = []
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        pack = by_name.get(name)
        if pack is None:
            logger.warning("LLM 选择的技能包不存在，跳过：%s", name)
            continue
        loaded.append(pack)
    return loaded


# 题材包的三层标题（撰写规范见 docs/design/16-skill-pack-spec.md）：
# 「## 一、剧作层」「## 二、分镜层」「## 三、模型适配层」（括号注记可有可无）
_LAYER_HEAD_RE = re.compile(r"^##\s*(一|二|三)、.*$", re.M)
# 层名 → 序号（调用方按语义传"story"/"shot"/"model"，不传序号）
LAYER_STORY = "story"
LAYER_SHOT = "shot"
LAYER_MODEL = "model"
_LAYER_INDEX = {LAYER_STORY: "一", LAYER_SHOT: "二", LAYER_MODEL: "三"}


def skill_layer(pack: SkillPack, layer: str) -> str:
    """取技能包指定层正文（story/shot/model）；无分层结构时返回全文。

    为什么分层（而非整包注入）：三层包各 2000-3000 字，整包注入会在提示词里
    塞入大量与当前阶段无关的知识（剧作规则对生图无意义、模型适配对编剧无意义），
    既浪费上下文又稀释当前任务的注意力。分层后每阶段只收到它真正需要的一层。
    """
    body = (pack.body or "").strip()
    want = _LAYER_INDEX.get(layer)
    if not want:
        return body
    marks = [(m.start(), m.group(1)) for m in _LAYER_HEAD_RE.finditer(body)]
    if not marks:
        return body  # 旧格式包（未分层）：整包注入，灰度兼容
    for i, (pos, num) in enumerate(marks):
        if num != want:
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        return body[pos:end].strip()
    return ""  # 该包无此层（如模型包未被分层）


def render_skill_text(packs: list[SkillPack], max_chars_per_skill: int = 0,
                      layer: str = "") -> str:
    """把技能包渲染为【技能包·名】段落，供提示词注入。

    layer 非空时只取该层正文（见 skill_layer）；层内仍有上限保护
    （max_chars_per_skill>0 时生效，默认不限——分层后单层体量可控，
    截断会静默丢知识，是「写了规范却不生效」的根源）。
    """
    blocks: list[str] = []
    for pack in packs:
        body = skill_layer(pack, layer) if layer else pack.body.strip()
        if not body:
            continue
        if max_chars_per_skill and len(body) > max_chars_per_skill:
            logger.warning("技能包 %s 的 %s 层超长（%s 字）已截断——"
                           "请检查包内容或按需分层", pack.name, layer or "全文",
                           len(body))
            body = body[:max_chars_per_skill].rstrip() + "……"
        blocks.append(f"【技能包·{pack.name}】{pack.description}\n{body}")
    return "\n\n".join(blocks)


# ---------- 文件管理（系统管理·技能包页签的存储层） ----------

# 技能包文件名规则：小写字母/数字开头，小写字母数字下划线连字符，.md 结尾
SKILL_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,48}\.md$")


def read_skill_raw(filename: str) -> str | None:
    """按文件名读技能包原文（含 frontmatter；不存在返回 None）。"""
    if not SKILL_FILENAME_RE.match(filename):
        return None
    try:
        return (SKILLS_DIR / filename).read_text(encoding="utf-8")
    except OSError:
        return None


def validate_skill_text(text: str) -> str | None:
    """校验技能包全文（frontmatter + 正文），合法返回 None，否则返回错误说明。"""
    pack = _parse_skill_file_text(text, fallback_name="validate")
    if pack is not None:
        return None
    # 区分两类常见错误给出可读提示（解析函数只返回 None）
    if not re.match(r"\A---\s*\n", text or ""):
        return "缺少 frontmatter（需以 --- 包围的 name/genres|models/description 头部）"
    return "frontmatter 校验失败：题材包需声明 genres、模型包需声明 models（至少其一），" \
           "模型包建议同时声明 task_type（t2i|t2v）"


def write_skill_file(filename: str, text: str) -> dict:
    """新建/覆盖技能包文件：文件名与内容校验通过后写盘（校验失败抛 ValueError）。

    写盘即时生效——load_skill_packs 每次调用都重新扫描目录，无需重启。
    """
    if not SKILL_FILENAME_RE.match(filename):
        raise ValueError(f"无效文件名：{filename}（小写字母/数字开头，仅含小写字母数字下划线连字符，.md 结尾）")
    if not (text or "").strip():
        raise ValueError("技能包内容不能为空")
    error = validate_skill_text(text)
    if error:
        raise ValueError(error)
    path = SKILLS_DIR / filename
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return {"ok": True, "path": str(path)}


def delete_skill_file(filename: str) -> None:
    """删除技能包文件（不存在抛 ValueError）。"""
    if not SKILL_FILENAME_RE.match(filename):
        raise ValueError(f"无效文件名：{filename}")
    path = SKILLS_DIR / filename
    if not path.exists():
        raise ValueError(f"技能包不存在：{filename}")
    path.unlink()


def _parse_skill_file_text(text: str, fallback_name: str) -> SkillPack | None:
    """解析技能包文本（校验复用入口；规则与文件版完全一致，无日志副作用）。"""
    return _parse_skill_text(text, fallback_name, "（在线编辑校验）")
