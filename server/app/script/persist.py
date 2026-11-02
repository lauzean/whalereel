"""剧本解析结果落库服务：解析任务 runner 与 API 共用（无 API 层依赖）。

职责：清空旧解析数据、写入 角色/形象引用/场景/分集/分镜 与解析元信息，
并计算项目画风名称。独立成模块避免 runner（task_engine 侧）反向依赖
app.api 造成循环导入。
"""

import json
import logging
import time
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.agent import prompts as prompts_mod
from app.script.asset_field_guards import check_character_fields
from app.script.requirements import load_requirements
from app.db.models import (
    PLAN_SOURCE_HEAD, PRODUCE_BUILDING, PRODUCE_BUILT, PRODUCE_PLANNED,
    Character, CharacterImage, Episode, NodeVersion, Project, Prop, Scene,
    ScriptVersion, Setting, Shot,
)

logger = logging.getLogger(__name__)

# 画风关键词包文件（real/3d/anime/guofeng）
_PRESETS_FILE = Path(__file__).resolve().parent.parent / "data" / "style_presets.json"

# Setting 键：解析元信息（title/logline/style）与解析原文
SCRIPT_META_KEY_PREFIX = "drama_script:"
SCRIPT_TEXT_KEY_PREFIX = "drama_script_text:"
# Setting 键：当前生效解析结果的过程轨迹（版本快照/恢复按项目同步）
SCRIPT_TRACE_KEY_PREFIX = "drama_script_trace:"
# Setting 键：当前生效解析结果的版本号（header 标签展示）
SCRIPT_CUR_SEQ_KEY_PREFIX = "drama_script_cur_seq:"
# Setting 键：妆造建议清单（解析提取的换装事件，角色卡确认后生成）
COSTUME_SUGGESTIONS_KEY_PREFIX = "drama_costume_suggestions:"
# Setting 键：最近一次终审未通过的问题清单（优化时合并注入防打地鼠——
# 修复不得回退已修复项；解析成功后清除）
LAST_AUDIT_KEY_PREFIX = "drama_last_audit:"
# Setting 键前缀：解析时注入的技能包正文存档（题材包/t2v 模型包）——
# 单镜画面描述重生成等解析后 LLM 环节复用同一套技能上下文
SKILL_TEXT_KEY_PREFIX = "drama_skill_text:"

# 题材技能包【分镜层】正文（18 号：拆解与画面描述分属不同任务，各自取需要的层）
SHOT_SKILL_KEY_PREFIX = "drama_shot_skill:"
T2V_SKILL_KEY_PREFIX = "drama_t2v_skill:"
# Setting 键：解析期分镜体检报告（统一 Issue 结构，批次 2a）——提交前硬门
# （批次 3）与前端徽标消费；重解析覆盖
# 逐集资产解析结果（18 号 §61）：{"characters": n, "scenes": n, "props": n,
# "reused": {...}, "aliases": [...], "at": ts}——分集列表接口据此显示「已解析」
EP_ASSETS_KEY_PREFIX = "drama_ep_assets:"


def ep_assets_key(episode_id: str) -> str:
    """逐集资产解析结果的 Setting 键。"""
    return f"{EP_ASSETS_KEY_PREFIX}{episode_id}"


def _clean_layout_figures(layout: object, names: list[str]) -> str:
    """场景「空间布局」落库前的人物剔除（解析提示词规则的**确定性兜底**）。

    规则（2026-10-02，提示词双源已同步）：场景级字段只写静态空间。LLM 偶有
    违反（实测写进「妈妈可站在布帘后探头看向沙发」「小杰坐对面背朝房门」），
    而布局段是原样拼进生图提示词的——生成的场景图里出现了人物。这里按角色
    名单整句剔除兜底（名单=同名表里的角色名，≥2 字由 strip_figure_clauses
    内部把关）；不猜同义词、不做模糊匹配。
    """
    return prompts_mod.strip_scene_figures(
        prompts_mod.strip_figure_clauses(str(layout or ""), names))


STORYBOARD_QA_KEY_PREFIX = "drama_storyboard_qa:"


def storyboard_qa_key(project_id: str) -> str:
    """分镜体检报告 Setting 键（解析期确定性体检结果存档）。"""
    return f"{STORYBOARD_QA_KEY_PREFIX}{project_id}"


PROMPT_SNAPSHOT_KEY_PREFIX = "drama_prompt_snapshot:"
# Setting 键：大纲拍板稿（批次 4 §35.1 五件套+门禁+付费卡点标注，待确认/
# 已确认两态由 confirmed 标记区分；确认后注入创作）
# Setting 键：自学习教训账本（体检修复/终审问题的错误模式累积，跨解析保留
# ——重解析不继承旧数据但继承经验；下次解析注入【历史教训】段落防重复犯错）
LESSONS_KEY_PREFIX = "drama_lessons:"
# Setting 键：全局教训账本（跨项目抽象统计——同一系统性问题在多少项目反复
# 出现；不含具体案例名，供全局注入与提示词改进建议）
GLOBAL_LESSONS_KEY = "drama_lessons_global"

# 教训账本：单类样本保留上限与注入提示词的类别数上限/样本字数
LESSON_SAMPLE_LIMIT = 3
LESSON_INJECT_MAX_RULES = 4
LESSON_SAMPLE_MAX_CHARS = 60
# 全局层注入/建议阈值：跨项目数 ≥2 且累计 ≥5 才算「系统性弱项」（单项目
# 自嗨不进全局层——避免偶发问题被当成系统性缺陷误导所有项目）
GLOBAL_LESSON_MIN_PROJECTS = 2
GLOBAL_LESSON_MIN_COUNT = 5
# 全局层注入提示词的类别数上限
GLOBAL_LESSON_INJECT_MAX_RULES = 3
# 改进建议追加到提示词时的标记前缀（幂等判据：目标提示词已含同规则标记
# 则视为已应用，不重复追加）
LESSON_APPEND_MARK = "【自学习补充"


def lessons_key(project_id: str) -> str:
    """自学习教训账本 Setting 键（按项目累积错误模式）。"""
    return f"{LESSONS_KEY_PREFIX}{project_id}"


def _upsert_setting(db: Session, key: str, value: str) -> None:
    """Setting 按键 upsert（autoflush=False 下同事务连续写入的唯一安全通道：
    先 flush 把此前 pending 的写入落库，get 才能命中，避免重复 add 撞主键）。"""
    db.flush()
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def load_lessons(db: Session, project_id: str) -> dict:
    """读取项目教训账本（结构 {"storyboard": {rule: {count, samples, last_at}},
    "script": {issue_type: {...}}}；读取失败返回空账本，不阻断解析）。"""
    row = db.get(Setting, lessons_key(project_id))
    if not row or not row.value:
        return {"storyboard": {}, "script": {}}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {"storyboard": {}, "script": {}}
    if not isinstance(data, dict):
        return {"storyboard": {}, "script": {}}
    data.setdefault("storyboard", {})
    data.setdefault("script", {})
    return data


def merge_lessons(db: Session, project_id: str, section: str,
                  detail: dict[str, list[str]]) -> dict:
    """把本轮修复/审核明细合并进教训账本（计数累加 + 样本去重保留最近 N 条）。

    section: "storyboard"（体检修复）| "script"（终审问题）。
    detail: {规则/问题类型: [样本文本, ...]}；返回合并后的完整账本。
    """
    if not detail:
        return load_lessons(db, project_id)
    db.flush()  # 同事务先前 pending 的账本写入先落库，load 才能读到最新基线
    ledger = load_lessons(db, project_id)
    bucket = ledger.setdefault(section, {})
    now = int(time.time())
    for rule, samples in detail.items():
        entry = bucket.setdefault(rule, {"count": 0, "samples": [], "last_at": 0})
        entry["count"] = int(entry.get("count") or 0) + max(1, len(samples))
        kept = list(entry.get("samples") or [])
        for s in samples:
            s = (s or "").strip()[:LESSON_SAMPLE_MAX_CHARS]
            if s and s not in kept:
                kept.append(s)
        entry["samples"] = kept[-LESSON_SAMPLE_LIMIT:]
        entry["last_at"] = now
    _upsert_setting(db, lessons_key(project_id),
                    json.dumps(ledger, ensure_ascii=False))
    # 全局层同步（跨项目抽象统计：计数 + 贡献项目集合——不含具体案例名）
    merge_global_lessons(db, section, detail, project_id)
    db.commit()
    return ledger


def load_global_lessons(db: Session) -> dict:
    """读取全局教训账本（结构 {"storyboard": {rule: {count, projects, last_at}},
    "script": {...}}；projects 为 {项目id: 时间戳} 贡献集合，去重即为跨项目数）。"""
    row = db.get(Setting, GLOBAL_LESSONS_KEY)
    if not row or not row.value:
        return {"storyboard": {}, "script": {}}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {"storyboard": {}, "script": {}}
    if not isinstance(data, dict):
        return {"storyboard": {}, "script": {}}
    data.setdefault("storyboard", {})
    data.setdefault("script", {})
    return data


def merge_global_lessons(db: Session, section: str,
                         detail: dict[str, list[str]],
                         project_id: str) -> None:
    """全局账本合并：只累计计数与贡献项目集合（抽象层，不存具体案例名）。

    由 merge_lessons 内部调用（调用方无需单独维护全局层）；不提交事务，
    随调用方的 commit 落库。
    """
    if not detail:
        return
    db.flush()  # 同事务先前 pending 的写入先落库，load 才能读到最新基线
    ledger = load_global_lessons(db)
    bucket = ledger.setdefault(section, {})
    now = int(time.time())
    for rule, samples in detail.items():
        entry = bucket.setdefault(rule, {"count": 0, "projects": {}, "last_at": 0})
        entry["count"] = int(entry.get("count") or 0) + max(1, len(samples))
        projects = dict(entry.get("projects") or {})
        projects[project_id] = now
        entry["projects"] = projects
        entry["last_at"] = now
    _upsert_setting(db, GLOBAL_LESSONS_KEY,
                    json.dumps(ledger, ensure_ascii=False))


def build_global_lessons_text(global_ledger: dict, section: str,
                              plain_map: dict | None = None) -> str:
    """全局账本 → 【系统性弱项】注入段落（抽象统计，无具体案例名）。

    仅保留跨项目数 ≥ 阈值且累计次数 ≥ 阈值的类别（单项目偶发不注入）；
    空结果返回空串（空段不注入约定）。
    """
    bucket = (global_ledger or {}).get(section) or {}
    eligible = []
    for rule, entry in bucket.items():
        if not isinstance(entry, dict):
            continue
        n_proj = len(entry.get("projects") or {})
        n = int(entry.get("count") or 0)
        if n_proj >= GLOBAL_LESSON_MIN_PROJECTS and n >= GLOBAL_LESSON_MIN_COUNT:
            eligible.append((rule, n, n_proj))
    if not eligible:
        return ""
    eligible.sort(key=lambda t: (-t[2], -t[1]))
    lines = [f"- {(plain_map or {}).get(r, r)}：累计 {n} 次、跨 {p} 个项目"
             for r, n, p in eligible[:GLOBAL_LESSON_INJECT_MAX_RULES]]
    return ("\n【系统性弱项（多个项目的解析中反复出现的问题，本类问题极易复发，"
            "务必重点防范）】\n" + "\n".join(lines))


# 改进建议映射：系统性弱项 → (目标提示词名, 追加建议正文)——建议写入对应
# 外置提示词 md 的规则区（用户确认后应用；快照兜底可恢复）
LESSON_SUGGESTION_MAP: dict[str, tuple[str, str]] = {
    "suspended": (
        "SCRIPT_PARSE_SYSTEM_PROMPT",
        "每个含 dialogue 的分镜必须同时写清说话人的画面动作与神态"
        "（description 不得为空）——仅有台词的镜头在成片里是广播剧。" ),
    "line_gap": (
        "SCRIPT_PARSE_SYSTEM_PROMPT",
        "1:1 覆盖再强化：剧本正文的每一行都必须被某个分镜认领"
        "（source_line_ids 逐行覆盖，零缺口）。" ),
    "line_dup": (
        "SCRIPT_PARSE_SYSTEM_PROMPT",
        "相邻分镜禁止复现同一段动作原文（全景交代镜只写环境与站位，"
        "具体表演动作由对话镜承担）。" ),
    "transition": (
        "SCRIPT_PARSE_SYSTEM_PROMPT",
        "衔接类型严格判定：场景发生变化的镜头必须标 scene_cut；"
        "只有同场景剧情直接延续才标 continue。" ),
    "场景连续性与位置": (
        "SCRIPT_AUDIT_SYSTEM_PROMPT",
        "逐场核对人物位置：上场结尾人物在哪、下场必须一致或有明确的"
        "移动/转场交代（严禁无过渡的场景间位置跳变）。" ),
    "场景连续性": (
        "SCRIPT_AUDIT_SYSTEM_PROMPT",
        "逐场核对人物位置：上场结尾人物在哪、下场必须一致或有明确的"
        "移动/转场交代（严禁无过渡的场景间位置跳变）。" ),
    "时间线": (
        "SCRIPT_AUDIT_SYSTEM_PROMPT",
        "时间线自洽：昼夜/天气/季节全篇一致，事件先后顺序无矛盾。" ),
    "桥段重复": (
        "SCRIPT_AUDIT_SYSTEM_PROMPT",
        "相邻集区间内避免同目的+同手段+同结果的重复桥段"
        "（同一冲突模式不得反复上演）。" ),
}


def build_lessons_suggestions(global_ledger: dict,
                              plain_map: dict | None = None,
                              prompt_texts: dict[str, str] | None = None,
                              ) -> list[dict]:
    """全局账本 → 提示词改进建议清单（确定性生成，应用前用户确认）。

    每条 {rule, title, target, evidence, append_text, applied}：
    - 仅系统性弱项（跨项目/次数达阈值）且映射表覆盖的类别产出；
    - applied 由目标提示词正文是否已含同规则标记判定（幂等，不重复追加）。
    """
    suggestions: list[dict] = []
    texts = prompt_texts or {}
    for section in ("storyboard", "script"):
        bucket = (global_ledger or {}).get(section) or {}
        for rule, entry in bucket.items():
            mapped = LESSON_SUGGESTION_MAP.get(rule)
            if not mapped or not isinstance(entry, dict):
                continue
            n = int(entry.get("count") or 0)
            n_proj = len(entry.get("projects") or {})
            if n_proj < GLOBAL_LESSON_MIN_PROJECTS or n < GLOBAL_LESSON_MIN_COUNT:
                continue
            target, body = mapped
            mark = f"{LESSON_APPEND_MARK}·{rule}】"
            append_text = f"\n{mark}\n{body}\n"
            suggestions.append({
                "rule": rule,
                "title": (plain_map or {}).get(rule, rule),
                "target": target,
                "evidence": f"累计 {n} 次、跨 {n_proj} 个项目",
                "append_text": append_text,
                "applied": mark in (texts.get(target) or ""),
            })
    suggestions.sort(key=lambda s: s["applied"])  # 未应用的排前
    return suggestions


def build_lessons_text(ledger: dict, section: str,
                       plain_map: dict | None = None) -> str:
    """账本 → 【历史教训】注入段落（确定性模板归纳；空账本返回空串）。

    plain_map: {规则名/问题类型: 白话展示名}（调用方预转换，persist 不依赖
    llm_parser 的白话映射）；缺失时回退规则名。
    按出现次数降序取前 N 类，每类给出白话名 + 次数 + 最近一条具体反面案例
    ——具体案例比抽象规则对模型的约束力更强（「镜『都别动』只有台词没画面」
    比「避免台词悬空」更能被遵循）。
    """
    bucket = (ledger or {}).get(section) or {}
    if not bucket:
        return ""
    rules = sorted(
        ((r, e) for r, e in bucket.items() if isinstance(e, dict)),
        key=lambda kv: -int(kv[1].get("count") or 0))
    lines: list[str] = []
    for rule, entry in rules[:LESSON_INJECT_MAX_RULES]:
        label = (plain_map or {}).get(rule, rule)
        n = int(entry.get("count") or 0)
        samples = list(entry.get("samples") or [])
        sample = f"；最近一例：{samples[-1]}" if samples else ""
        lines.append(f"- {label}：此前出现 {n} 次{sample}")
    if not lines:
        return ""
    return ("\n【历史教训（本项目此前解析中反复出现的问题，本次务必避免，"
            "同样的坑不要踩第二次）】\n" + "\n".join(lines))
def prompt_snapshot_key(name: str) -> str:
    """提示词编辑快照 Setting 键（UI 在线编辑保存前的上一版，可恢复）。"""
    return f"{PROMPT_SNAPSHOT_KEY_PREFIX}{name}"


def skill_text_key(project_id: str) -> str:
    """题材技能包【模型适配层】正文存档键（画面描述/单镜重生成用）。"""
    return f"{SKILL_TEXT_KEY_PREFIX}{project_id}"


def shot_skill_key(project_id: str) -> str:
    """题材技能包【分镜层】正文存档键（逐集拆解用）。

    18 号起拆解与画面描述分别在不同任务里跑，各自需要不同的技能层：
    拆解要「怎么切镜头」，画面描述要「本模型怎么写提示词」。旧版把两层混
    存成一个键，导致单镜重生成拿到的是剧作层正文（上下文错位）。
    """
    return f"{SHOT_SKILL_KEY_PREFIX}{project_id}"


def t2v_skill_key(project_id: str) -> str:
    """文生视频模型技能包正文存档键。"""
    return f"{T2V_SKILL_KEY_PREFIX}{project_id}"


def costume_suggestions_key(project_id: str) -> str:
    """妆造建议清单 Setting 键。"""
    return f"{COSTUME_SUGGESTIONS_KEY_PREFIX}{project_id}"


COSTUME_HANDLED_KEY_PREFIX = "drama_costume_handled:"


def costume_handled_key(project_id: str) -> str:
    """已处理妆造建议 Setting 键（accept/reject 过的「角色·妆造名」，JSON 数组）。"""
    return f"{COSTUME_HANDLED_KEY_PREFIX}{project_id}"


def load_handled_costumes(db: Session, project_id: str) -> set[tuple[str, str]]:
    """已处理过的 (角色名, 妆造名) 集合（损坏/缺失返回空集，不阻断任何流程）。

    **故意不随 `reset_drama` 清理**：重建后同名角色/同名妆造会重新被提取出来，
    若这里被清空，已经接受过（妆造形象已生成）或明确拒绝过的建议会**复活**，
    用户要么重复生成一张同样的妆造图、要么再拒一次。
    """
    row = db.get(Setting, costume_handled_key(project_id))
    if not row or not row.value:
        return set()
    try:
        items = json.loads(row.value) or []
    except ValueError:
        logger.warning("已处理妆造键损坏（按空处理）project=%s", project_id)
        return set()
    out: set[tuple[str, str]] = set()
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("character") and it.get("name"):
            out.add((str(it["character"]), str(it["name"])))
    return out


def mark_costume_handled(db: Session, project_id: str,
                         character: str, name: str) -> None:
    """记录一条已处理的妆造建议（accept / reject 共用）——自带提交。"""
    if not character or not name:
        return
    row = db.get(Setting, costume_handled_key(project_id))
    try:
        items = json.loads(row.value) if row and row.value else []
    except ValueError:
        items = []
    if not isinstance(items, list):
        items = []
    if not any(isinstance(it, dict) and it.get("character") == character
               and it.get("name") == name for it in items):
        items.append({"character": character, "name": name})
        db.merge(Setting(key=costume_handled_key(project_id),
                         value=json.dumps(items, ensure_ascii=False)))
        db.commit()


def merge_costume_suggestions(db: Session, project_id: str,
                              suggestions: list[dict]) -> int:
    """把新提取的妆造建议**并入**项目清单（跨集累积），返回新增条数。

    合并规则（全部是确定性对账，不做语义判断）：
    - 同一「角色·妆造名」再次出现 → **并集适用集数**（第 5 集的「嫁衣」在第 8 集
      又出现，就是第 5、8 集都要用这套妆造）；
    - 已 accept / reject 过的键 → **跳过**（不复活，见 `load_handled_costumes`）；
    - id 递增分配（各集独立提取的 id 会撞号，撞号会让「接受」误操作到别条）。

    与建档期的 `save_parse_artifacts`（覆盖写）不同：逐集解析是**增量**的，
    不能把之前几集提取的建议冲掉。
    """
    if not suggestions:
        return 0
    row = db.get(Setting, costume_suggestions_key(project_id))
    try:
        existing = json.loads(row.value) if row and row.value else []
    except ValueError:
        existing = []
    if not isinstance(existing, list):
        existing = []
    handled = load_handled_costumes(db, project_id)
    # id 续号：取现有最大数字后缀 +1（存量 id 形如 s1/s2）
    next_no = 0
    for it in existing:
        sid = str((it or {}).get("id") or "")
        if sid.startswith("s") and sid[1:].isdigit():
            next_no = max(next_no, int(sid[1:]))
    index = {(str(it.get("character") or ""), str(it.get("name") or "")): it
             for it in existing if isinstance(it, dict)}
    added = 0
    for sug in suggestions:
        if not isinstance(sug, dict):
            continue
        key = (str(sug.get("character") or ""), str(sug.get("name") or ""))
        if not all(key) or key in handled:
            continue
        eps = sorted({int(e) for e in (sug.get("episodes") or [])
                      if isinstance(e, int)})
        if not eps:
            continue
        hit = index.get(key)
        if hit is not None:
            hit["episodes"] = sorted(set(
                [int(e) for e in (hit.get("episodes") or []) if isinstance(e, int)]
                + eps))
            continue
        next_no += 1
        entry = {**sug, "id": f"s{next_no}", "episodes": eps}
        existing.append(entry)
        index[key] = entry
        added += 1
    db.merge(Setting(key=costume_suggestions_key(project_id),
                     value=json.dumps(existing, ensure_ascii=False)))
    db.commit()
    return added


REPAIR_CLEARED_KEY_PREFIX = "drama_repair_cleared:"


def repair_cleared_key(project_id: str) -> str:
    """修复确认放行标记 Setting 键（值为修复稿的 sha256）。"""
    return f"{REPAIR_CLEARED_KEY_PREFIX}{project_id}"


def last_audit_key(project_id: str) -> str:
    """最近一次终审未通过问题清单 Setting 键（优化记忆）。"""
    return f"{LAST_AUDIT_KEY_PREFIX}{project_id}"


def script_meta_key(project_id: str) -> str:
    """解析元信息 Setting 键。"""
    return f"{SCRIPT_META_KEY_PREFIX}{project_id}"


def script_text_key(project_id: str) -> str:
    """解析原文 Setting 键。"""
    return f"{SCRIPT_TEXT_KEY_PREFIX}{project_id}"


def script_trace_key(project_id: str) -> str:
    """当前生效解析过程轨迹 Setting 键。"""
    return f"{SCRIPT_TRACE_KEY_PREFIX}{project_id}"


def script_cur_seq_key(project_id: str) -> str:
    """当前生效解析版本号 Setting 键。"""
    return f"{SCRIPT_CUR_SEQ_KEY_PREFIX}{project_id}"


def load_style_presets() -> dict:
    """画风关键词包（读取失败返回空 dict，不阻断解析）。"""
    try:
        return json.loads(_PRESETS_FILE.read_text(encoding="utf-8")).get("presets", {})
    except (OSError, json.JSONDecodeError):
        return {}


def load_style_anchors() -> dict:
    """风格锚点库（美学第二级，20 号 §2.4）：{key: {label, prompt, form, hint}}。

    与 preset 的关系：`preset` 管形式（画风规范 / 负面词），`anchor` 管具体美学锚点
    （官方要求「某某风格的电影」这类**具体**锚点，禁止「电影风格」这类留白写法）。
    读取失败返回空 dict（不阻断解析，退化为无锚点行为）。
    """
    try:
        return json.loads(_PRESETS_FILE.read_text(encoding="utf-8")).get("anchors", {})
    except (OSError, json.JSONDecodeError):
        return {}


def anchor_prompt_for(preset: str, anchor: str) -> str:
    """取锚点的全局风格限定词（含**相容性校验**：form 必须与 preset 匹配）。

    不相容（如写实项目选了动画流派）视为无效锚点，返回空串——避免把
    「京都动画风格」注入到写实剧里造成风格穿帮。校验是确定性字符串比较，
    不做语义判断（与 API 层白名单是同一判据的两道防线）。
    """
    if not anchor:
        return ""
    info = load_style_anchors().get(anchor) or {}
    if not info or info.get("form") != preset:
        return ""
    return str(info.get("prompt") or "")


def style_name_for(db: Session, project_id: str) -> str:
    """项目画风展示名（预设名，未匹配时原样返回预设标识）。"""
    project = db.get(Project, project_id)
    preset = project.style_preset if project else ""
    return load_style_presets().get(preset, {}).get("name", preset)


def project_style_context(db: Session, project_id: str) -> dict:
    """项目画风上下文（解析 runner 与单镜重生成共用的 style_hint 构建）。

    画风 = 预设名 + 关键词 + 【画风规范】（五官/发色/质感按画风差异化）；
    国家 = 人物外貌约束 + 【地区规范】（涉枪法律/官员着装等常识按国家注入）。
    返回 {"style_hint", "preset_name", "aspect_ratio", "country",
    "model_presets"}；项目不存在时给中性默认值。
    """
    from app.db.models import Project, country_norm_text  # 函数内导入防循环

    preset_name, country, aspect_ratio = "", "中国", "16:9"
    model_presets: dict[str, str] = {}
    anchor_key, anchor_prompt = "", ""
    hint = ""
    project = db.get(Project, project_id)
    if project:
        info = load_style_presets().get(project.style_preset, {})
        preset_name = info.get("name", "")
        keywords = info.get("keywords", "")
        # 风格锚点（20 号 §2.3）：全局风格限定词**置于最前、一字不差沿用**——
        # 官方口径「资产 / 关键帧 / 提示词三阶段的风格限定词必须与剧本头风格字段
        # 一字不差」，此处把它显式告知模型，避免各阶段自行改写成「电影风格」类留白
        anchor_key = str(getattr(project, "style_anchor", "") or "")
        anchor_prompt = anchor_prompt_for(project.style_preset, anchor_key)
        if anchor_prompt:
            hint = (f"全局风格限定词（Prompt 首句原样沿用，禁改写、禁简化）："
                    f"{anchor_prompt}")
        hint = (f"{hint}\n{preset_name}（关键词：{keywords}）" if hint
                else (f"{preset_name}（关键词：{keywords}）" if keywords else preset_name))
        guide = info.get("prompt_guide", "")
        if guide:
            hint = f"{hint}\n【画风规范】{guide}"
        aspect_ratio = getattr(project, "aspect_ratio", None) or "16:9"
        country = getattr(project, "country", None) or "中国"
        model_presets = dict(getattr(project, "model_presets", None) or {})
    hint = (f"{hint}；国家/地区：{country}"
            f"（人物外貌、面孔与人种特征必须符合该国设定）")
    norms = country_norm_text(country)
    if norms:
        hint = f"{hint}\n【地区规范】{norms}"
    return {"style_hint": hint, "preset_name": preset_name,
            "style_anchor": anchor_key, "anchor_prompt": anchor_prompt,
            "aspect_ratio": aspect_ratio, "country": country,
            "model_presets": model_presets}


def save_script_text(db: Session, project_id: str, text: str) -> None:
    """剧本原文存档（创作型解析时回写规范剧本，作为剧本产出物）。"""
    db.merge(Setting(key=script_text_key(project_id), value=text))
    db.commit()


def save_parse_artifacts(db: Session, project_id: str, result: dict) -> None:
    """建档收尾产物入 Setting（妆造建议/分镜体检报告/技能包正文）。

    独立封装 + 显式 commit：这些写入原先内联在 runner 的落库块尾部，
    而该块内唯一的 commit 被 `canonical_text` 条件包住——完整剧本路径
    走到块尾无 commit，Session 关闭时全部静默回滚，提交前硬门因此读到空
    体检而 fail-open。写 Setting 统一走「自带提交」的函数，杜绝再次出现
    「末尾写入依赖别处 commit」。

    18 号起建档不再产出分镜（逐集制作时才产生），体检报告按集合并写入
    （见 `runners/script_episode._merge_storyboard_qa`）；此处只清掉旧报告，
    避免新一版建档后旧集的体检项残留在硬门里。
    """
    # 妆造建议：覆盖旧清单（未确认的建议作废，以最新建档的换装事件为准）
    db.merge(Setting(key=costume_suggestions_key(project_id),
                     value=json.dumps(result.get("costume_suggestions") or [],
                                      ensure_ascii=False)))
    # 分镜体检报告：重建建档 = 分镜全部作废，清空报告（逐集制作时按集重写）
    db.execute(delete(Setting).where(Setting.key == storyboard_qa_key(project_id)))
    # 技能包正文：按层分键存档——逐集拆解取【分镜层】，画面描述与单镜重生成
    # 取【模型适配层 + t2v 包】（各阶段只收自己需要的一层）
    skill_texts = result.get("skill_texts") or {}
    db.merge(Setting(key=skill_text_key(project_id),
                     value=skill_texts.get("model_adapt") or ""))
    db.merge(Setting(key=shot_skill_key(project_id),
                     value=skill_texts.get("shot") or ""))
    db.merge(Setting(key=t2v_skill_key(project_id),
                     value=skill_texts.get("t2v") or ""))
    db.commit()


def save_script_trace(db: Session, project_id: str, trace: dict | None) -> None:
    """归档当前生效解析结果的过程轨迹（版本快照/恢复按项目同步）。

    trace 为 None/空时删键：恢复无 trace 的旧快照时清掉残留，避免新版本的
    过程轨迹错误地挂在旧版本内容上。
    """
    key = script_trace_key(project_id)
    if not trace:
        db.execute(delete(Setting).where(Setting.key == key))
    else:
        db.merge(Setting(key=key,
                         value=json.dumps(trace, ensure_ascii=False)))
    db.commit()


def load_script_trace(db: Session, project_id: str) -> dict:
    """读取当前生效解析过程轨迹（缺失/损坏返回空 dict）。"""
    setting = db.get(Setting, script_trace_key(project_id))
    if not setting or not setting.value:
        return {}
    try:
        return json.loads(setting.value) or {}
    except ValueError:
        return {}


def get_script_cur_seq(db: Session, project_id: str) -> int:
    """当前生效解析结果版本号（未解析/无记录返回 0）。"""
    setting = db.get(Setting, script_cur_seq_key(project_id))
    if not setting or not setting.value:
        return 0
    try:
        return int(setting.value)
    except (TypeError, ValueError):
        return 0


def set_script_cur_seq(db: Session, project_id: str, seq: int) -> None:
    """直接设置当前生效版本号（恢复历史版本时回拨到被恢复版本的号）。"""
    db.merge(Setting(key=script_cur_seq_key(project_id), value=str(seq)))
    db.commit()


def bump_script_version(db: Session, project_id: str) -> int:
    """解析成功后分配新版本号：max(全部历史版本 seq, 旧 cur) + 1。

    取 max 保证跳过已被历史版本占用的号（恢复旧版本后 cur 回拨，再解析
    时不得与历史 seq 撞号），返回新号并写回 Setting。
    """
    max_seq = db.scalar(
        select(func.max(ScriptVersion.seq))
        .where(ScriptVersion.project_id == project_id)
    ) or 0
    new_seq = max(max_seq, get_script_cur_seq(db, project_id)) + 1
    set_script_cur_seq(db, project_id, new_seq)
    return new_seq


def reset_drama(db: Session, project_id: str,
                keep_character_assets: bool = True) -> dict:
    """清空旧解析数据（重复解析覆盖重建）：分镜/分集/场景/元信息。

    keep_character_assets=True（默认）时保留角色形象与音色参考：形象记录暂留
    不删（persist 后由 reattach_character_assets 按角色名挂接回新角色，
    image.id 与生成版本原样保留——重新解析不再丢失已生成的定妆图/四视图/
    妆造），音色参考按角色名暂存回填。返回
    {"old_chars": {旧角色id: 角色名}, "voice_refs": {角色名: 音色asset_id}}。
    """
    rows = db.execute(
        select(Character.id, Character.name, Character.voice_ref_asset_id)
        .where(Character.project_id == project_id)
    ).all()
    old_chars = {rid: name for (rid, name, _vid) in rows}
    voice_refs = {name: vid for (_i, name, vid) in rows if vid}
    old_ids = list(old_chars)
    # 场景行删除前记录旧 id：场景主图的版本挂在 scene_{id} 节点上，
    # 重建后由 reattach 按场景名迁移（只迁角色不迁场景 = 重解析丢场景图）
    old_scenes = {sid: name for (sid, name) in db.execute(
        select(Scene.id, Scene.name).where(Scene.project_id == project_id)).all()}
    # 「设为生效」指针随行删除丢失，同步暂存（reattach 迁移版本后回填——
    # 版本 id 在迁移中不变，指针回填后依然有效）
    old_scene_prefs = {sid: pref for (sid, pref) in db.execute(
        select(Scene.id, Scene.preferred_version_id)
        .where(Scene.project_id == project_id)).all() if pref}
    # 道具定妆图同理挂在 prop_{id} 节点上，重建后按道具名迁移
    old_props = {pid_: name for (pid_, name) in db.execute(
        select(Prop.id, Prop.name).where(Prop.project_id == project_id)).all()}
    old_prop_prefs = {pid_: pref for (pid_, pref) in db.execute(
        select(Prop.id, Prop.preferred_version_id)
        .where(Prop.project_id == project_id)).all() if pref}
    if not keep_character_assets:
        db.execute(delete(CharacterImage).where(
            CharacterImage.character_id.in_(old_ids)))
    # keep 时不删形象行：暂留待 persist 后挂接，无同名新角色的在迁移时清理
    db.execute(delete(Character).where(Character.project_id == project_id))
    # 产出对账（25 号 2.5）：这一步会**弃用**已有分镜的全部产出——分镜被删后它们的
    # 版本链不再被任何分镜引用，成为**孤儿**（实测全库孤儿率 **60%**：173 条视频版本
    # 里只有 19 条仍挂着分镜）。产物文件**保留在 assets**（可回溯，不删），但必须
    # **明确告知数量**——静默丢弃付过钱的产出，比抽卡更贵的浪费。
    _shot_ids = db.execute(
        select(Shot.id).where(Shot.episode_id.in_(
            select(Episode.id).where(Episode.project_id == project_id)))).scalars().all()
    _produced_nodes = set(db.execute(
        select(NodeVersion.node_id).where(
            NodeVersion.project_id == project_id,
            NodeVersion.node_id.startswith("shot_"))).scalars())
    produced_discarded = sum(
        1 for _sid in _shot_ids
        if f"shot_{_sid}_v" in _produced_nodes or f"shot_{_sid}_ff" in _produced_nodes)
    if produced_discarded:
        logger.warning(
            "重建将弃用 %s 个已产出分镜的版本引用（产物文件保留在 assets，"
            "不再挂在任何分镜上）——project=%s", produced_discarded, project_id)
    db.execute(delete(Shot).where(
        Shot.episode_id.in_(
            select(Episode.id).where(Episode.project_id == project_id))))
    db.execute(delete(Episode).where(Episode.project_id == project_id))
    db.execute(delete(Scene).where(Scene.project_id == project_id))
    db.execute(delete(Prop).where(Prop.project_id == project_id))
    db.execute(delete(Setting).where(Setting.key == script_meta_key(project_id)))
    # 妆造建议随旧解析结果作废（成功解析会写入新清单；落库中途失败时也不留
    # 指向已删角色的旧建议）
    db.execute(delete(Setting).where(
        Setting.key == costume_suggestions_key(project_id)))
    # 分镜体检报告同作废（旧报告的 seq/行号指向已删分镜；成功解析写新报告）
    db.execute(delete(Setting).where(
        Setting.key == storyboard_qa_key(project_id)))
    return {"old_chars": old_chars, "voice_refs": voice_refs,
            "old_scenes": old_scenes, "old_props": old_props,
            "old_scene_prefs": old_scene_prefs, "old_prop_prefs": old_prop_prefs,
            # 本次重建**弃用**的已产出分镜数（25 号 2.5）——供上层外露给用户：
            # 产物文件仍在 assets 里可回溯，但已不再挂在任何分镜上
            "produced_discarded": produced_discarded}


def reattach_character_assets(db: Session, project_id: str,
                              reset_info: dict) -> int:
    """把重解析前保留的形象资产挂接回同名新角色（reset_drama 之后调用）。

    - 形象按旧角色名挂到新角色 id 上：image.id 不变 → node_id 不变 → 分镜
      妆造映射与生成版本（node_versions）全部保留，定妆图/四视图直接可见；
      挂接时同步重写版本记录的 node_id 前缀（char_{旧id}_ → char_{新id}_）；
    - 无同名新角色的旧形象为孤儿，连同其版本清理（产物 Asset 留资产库）；
    - 音色参考（voice_ref_asset_id）按角色名回填到未设置音色的新角色。
    返回迁移的形象数。
    """
    old_chars: dict[str, str] = reset_info.get("old_chars") or {}
    moved_scenes = _reattach_scene_assets(db, project_id,
                                          reset_info.get("old_scenes") or {},
                                          reset_info.get("old_scene_prefs") or {})
    # 道具定妆图迁移上提到本层（持有 reset_info）：原调用嵌在
    # _reattach_scene_assets 内且引用了不在其作用域的 reset_info（重解析
    # 含旧场景时落库必失败的 NameError），且「无旧场景」早退会连道具迁移
    # 一起跳过——道具/场景/角色三类迁移必须互相独立
    moved_scenes += _reattach_prop_assets(db, project_id,
                                          reset_info.get("old_props") or {},
                                          reset_info.get("old_prop_prefs") or {})
    logger.info("资产迁移：场景版本 %s 个", moved_scenes)
    if not old_chars:
        return moved_scenes
    new_ids = {c.name: c.id for c in db.scalars(
        select(Character).where(Character.project_id == project_id))}
    # 迁移会重写 node_id：旧 node_id 上排队/生成中的任务完成后会把版本写回
    # 旧地址（孤儿版本）——迁移前先取消这些活动任务
    from app.core.task_engine import engine  # 函数内导入避免模块级循环依赖
    from app.db.models import Task

    moved = 0
    for old_cid, name in old_chars.items():
        img_ids = [r for (r,) in db.execute(
            select(CharacterImage.id)
            .where(CharacterImage.character_id == old_cid)).all()]
        active_ids = db.scalars(select(Task.id).where(
            Task.project_id == project_id,
            Task.status.in_(("queued", "loading_model", "running")),
            Task.node_id.in_([f"char_{old_cid}_{i}" for i in img_ids]),
        )).all()
        for tid in active_ids:
            engine.cancel(tid)
        new_cid = new_ids.get(name)
        if not new_cid:
            # 无同名新角色：形象成为孤儿，清理版本与记录（产物留资产库）
            db.execute(delete(NodeVersion).where(
                NodeVersion.node_id.in_(
                    [f"char_{old_cid}_{i}" for i in img_ids]),
                NodeVersion.project_id == project_id))
            db.execute(delete(CharacterImage)
                       .where(CharacterImage.character_id == old_cid))
            continue
        for img_id in img_ids:
            old_node = f"char_{old_cid}_{img_id}"
            new_node = f"char_{new_cid}_{img_id}"
            if old_node != new_node:
                db.execute(update(NodeVersion)
                           .where(NodeVersion.node_id == old_node,
                                  NodeVersion.project_id == project_id)
                           .values(node_id=new_node))
            db.execute(update(CharacterImage)
                       .where(CharacterImage.id == img_id)
                       .values(character_id=new_cid))
            moved += 1
        logger.info("角色形象迁移：%s（%s 个形象挂接回新角色）", name, len(img_ids))
    # 音色参考回填：同名新角色未设置音色时继承旧参考（修复：原代码位于
    # return 之后永不可达——重解析后音色参考一直被静默丢弃）
    voice_moved = 0
    for name, vid in (reset_info.get("voice_refs") or {}).items():
        row = db.scalar(select(Character).where(
            Character.project_id == project_id, Character.name == name))
        if row and not (getattr(row, "voice_ref_asset_id", None) or "").strip():
            row.voice_ref_asset_id = vid
            voice_moved += 1
    if moved or moved_scenes or voice_moved:
        # 显式提交（同 _reattach_scene_assets：不提交则随 session 关闭回滚）
        db.commit()
    if voice_moved:
        logger.info("音色参考回填：%s 个角色继承旧音色参考", voice_moved)
    return moved + moved_scenes


def _reattach_scene_assets(db: Session, project_id: str,
                           old_scenes: dict[str, str],
                           old_prefs: dict[str, str] | None = None) -> int:
    """把重解析前的场景主图版本按场景名挂接回新场景（reset_drama 之后调用）。

    场景主图版本挂在 scene_{旧id} 节点上，重解析重建 Scene 行后新 id 查不到
    产物——按名称迁移 node_id（scene_{旧} → scene_{新}），与角色形象迁移
    同语义；old_prefs（旧场景 id → 生效版本 id）随版本迁移回填到新场景行
    （版本 id 不变，指针迁移后依然有效）。返回迁移的场景数。
    """
    if not old_scenes:
        return 0

    def _norm_name(n: str) -> str:
        # 场景名归一：LLM 逐次解析的命名有非确定性差异（「老周面馆 夜 内」vs
        # 「老周面馆（夜）」），按去空白/括号/分隔符后的连续字符匹配
        import re as _re

        return _re.sub(r"[\s（）()：:·、，,]", "", n or "")

    new_ids = {_norm_name(s.name): s.id for s in db.scalars(
        select(Scene).where(Scene.project_id == project_id))}
    from app.core.task_engine import engine  # 函数内导入避免模块级循环依赖
    from app.db.models import Task

    moved = 0
    for old_sid, name in old_scenes.items():
        new_sid = new_ids.get(_norm_name(name))
        if not new_sid:
            continue
        old_node = f"scene_{old_sid}"
        new_node = f"scene_{new_sid}"
        # 迁移前取消旧节点上排队/生成中的任务（完成后会把版本写回旧地址）
        active_ids = db.scalars(select(Task.id).where(
            Task.project_id == project_id,
            Task.status.in_(("queued", "loading_model", "running")),
            Task.node_id == old_node,
        )).all()
        for tid in active_ids:
            engine.cancel(tid)
        db.execute(update(NodeVersion)
                   .where(NodeVersion.node_id == old_node,
                          NodeVersion.project_id == project_id)
                   .values(node_id=new_node))
        # 回填「设为生效」指针：版本 id 迁移中不变，指向的版本已挂到新节点
        pref = (old_prefs or {}).get(old_sid)
        if pref:
            row = db.get(Scene, new_sid)
            if row:
                row.preferred_version_id = pref
        moved += 1
    if moved:
        # 必须显式提交：调用方 SessionLocal 上下文退出不 commit（回滚），
        # 此前迁移日志打成功但数据全部回滚丢失（静默事故）
        db.commit()
    return moved


def _reattach_prop_assets(db: Session, project_id: str,
                          old_props: dict[str, str],
                          old_prefs: dict[str, str] | None = None) -> int:
    """把重解析前的道具定妆图版本按道具名挂接回新道具（归一名匹配）。

    old_prefs（旧道具 id → 生效版本 id）随版本迁移回填到新道具行。
    """
    if not old_props:
        return 0

    def _norm(n: str) -> str:
        import re as _re

        return _re.sub(r"[\s（）()：:·、，,]", "", n or "")

    new_ids = {_norm(p.name): p.id for p in db.scalars(
        select(Prop).where(Prop.project_id == project_id))}
    moved = 0
    for old_pid, name in old_props.items():
        new_pid = new_ids.get(_norm(name))
        if not new_pid:
            continue
        old_node = f"prop_{old_pid}"
        new_node = f"prop_{new_pid}"
        n = db.execute(update(NodeVersion)
                       .where(NodeVersion.node_id == old_node,
                              NodeVersion.project_id == project_id)
                       .values(node_id=new_node)).rowcount
        # 回填「设为生效」指针：版本 id 迁移中不变，指向的版本已挂到新节点
        pref = (old_prefs or {}).get(old_pid)
        if pref:
            row = db.get(Prop, new_pid)
            if row:
                row.preferred_version_id = pref
        moved += 1
        if n:
            logger.info("道具定妆图迁移：%s（%s 个版本）", name, n)
    if moved:
        db.commit()
    return moved


def _apply_scene_groups(db: Session, project_id: str,
                        pairs: list, groups: object) -> int:
    """把解析输出的场景组落到库（32 号方案，2026-09-30）。

    - 组按 `(project_id, 组名)` 唯一识别，**同名并入**（确定性规则，不猜语义）；
    - `env_setting` **只在组里为空时填**——不覆盖用户改过的组环境设定；
    - 场景带空组名 = 不入组（行为与旧数据完全一致）。

    `pairs` 为 `[(Scene 行, 组名字符串), ...]`；`groups` 为解析输出里的
    `scene_groups`（[{name, env_setting}]，可缺）。返回本次新建的组数。
    """
    from app.db.models import SceneGroup

    env_by_name: dict[str, str] = {}
    for item in groups if isinstance(groups, list) else []:
        if not isinstance(item, dict):
            continue
        n = str(item.get("name") or "").strip()
        e = str(item.get("env_setting") or "").strip()
        if n and e:
            env_by_name.setdefault(n, e)
    cache: dict[str, SceneGroup] = {}
    created = 0
    for row, gname in pairs:
        gname = str(gname or "").strip()
        if not gname or row is None:
            continue
        group = cache.get(gname)
        if group is None:
            group = db.scalars(select(SceneGroup).where(
                SceneGroup.project_id == project_id,
                SceneGroup.name == gname)).first()
            if group is None:
                group = SceneGroup(project_id=project_id, name=gname,
                                   env_setting=env_by_name.get(gname, ""))
                db.add(group)
                db.flush()          # 先拿 id 再挂场景
                created += 1
            cache[gname] = group
        # 组环境设定只在为空时补（用户改过的不被解析覆盖）
        if not (group.env_setting or "") and env_by_name.get(gname):
            group.env_setting = env_by_name[gname]
        row.group_id = group.id
    return created


def persist_parse_result(db: Session, project_id: str, result: dict) -> dict:
    """解析结果落库（不清库，调用方先 reset_drama）：角色/场景/分集/分镜 + 元信息。

    返回计数 dict（characters/scenes/props/episodes/shots），供任务结果与
    进度展示；另含 unmatched_refs（分镜引用名未命中资产名单的收集——角色/
    场景/道具三类，供调用方写过程轨迹告警用户）。
    """
    char_map: dict[str, Character] = {}
    for c in result["characters"]:
        row = Character(project_id=project_id, name=c["name"],
                        role=c.get("role") or "配角",
                        persona=c.get("persona", ""),
                        image_prompt=c.get("image_prompt", ""),
                        appearance=c.get("appearance", ""),
                        voice_desc=c.get("voice_desc", ""),
                        voice_ref_asset_id=c.get("voice_ref_asset_id"))
        db.add(row)
        char_map[c["name"]] = row

    scene_map: dict[str, Scene] = {}
    # 布局落库前剔人物走位句（名单=本批解析出的角色，见 _clean_layout_figures）
    _char_names = list(char_map.keys())
    for s in result["scenes"]:
        episodes = s.get("episodes", [])
        row = Scene(project_id=project_id, name=s["name"],
                    description=s.get("description", ""),
                    layout=_clean_layout_figures(s.get("layout", ""), _char_names),
                    image_prompt=s.get("image_prompt", ""),
                    episodes_json=episodes, freq=len(episodes))
        db.add(row)
        scene_map[s["name"]] = row

    # 关键叙事道具落库（跨镜头外观一致性锚点）
    prop_map: dict[str, Prop] = {}
    for p in result.get("props") or []:
        episodes = p.get("episodes", [])
        row = Prop(project_id=project_id, name=p["name"],
                   description=p.get("description", ""),
                   image_prompt=p.get("image_prompt", ""),
                   episodes_json=episodes, freq=len(episodes))
        db.add(row)
        prop_map[p["name"]] = row
    db.flush()  # 生成主键 id，供分镜引用

    # 场景组落库（32 号，同逐集路径）：建组/同名并入 + 场景挂组
    _apply_scene_groups(db, project_id,
                        [(scene_map[s["name"]], str(s.get("group") or ""))
                         for s in result.get("scenes") or []
                         if s.get("name") in scene_map],
                        result.get("scene_groups"))

    # **解析阶段就把完整提示词组装进提示词框**（2026-09-30 用户规则）：画风前缀 /
    # 空镜约束 / 空间布局 / 风格负向词在这一刻写进 image_prompt——用户在资产卡
    # 打开就能看到「最终会发出去的内容」；点生成只是发送框里已有的文本，
    # 不再等点生成时才补（那是「事后诸葛亮」）。
    # （函数内导入：drama 模块级 import 本模块，顶部导入会成环）
    from app.api.drama import (
        compose_character_prompt, compose_prop_prompt, compose_scene_prompt,
    )
    for _ct in char_map.values():
        compose_character_prompt(db, _ct)
    for _sc in scene_map.values():
        compose_scene_prompt(db, _sc)
    for _pp in prop_map.values():
        compose_prop_prompt(db, _pp)

    shot_count = 0
    # 资产名单未命中告警：分镜引用的角色/场景/道具名不在解析资产名单时收集
    # 透出（此前静默丢引用——生成时无参考图注入且无任何提示，是质量黑洞）。
    # 告警不阻断：名称近似（LLM 微调措辞）时用户可在故事板人工修正引用。
    unmatched: dict[str, list[str]] = {"characters": [], "scenes": [], "props": []}
    maps = {"characters": char_map, "scenes": scene_map, "props": prop_map}

    for ep in result["episodes"]:
        # 分集计划落库（18 号 §55）：body 是当集正文的制作真源，
        # char_start/char_end 是回写原文的确定性锚，synopsis/beats/hooks 构成
        # 分集要点表（逐集审核的上下文来源）。缺字段给中性默认，兼容旧结构。
        episode = Episode(
            project_id=project_id, seq=ep["seq"], title=ep.get("title", ""),
            body=ep.get("body", "") or "",
            char_start=int(ep.get("char_start") or 0),
            char_end=int(ep.get("char_end") or 0),
            synopsis=ep.get("synopsis", "") or "",
            beats_json=[b for b in (ep.get("beats") or []) if b],
            hooks_json=[h for h in (ep.get("hooks") or []) if isinstance(h, dict)],
            plan_source=ep.get("plan_source") or PLAN_SOURCE_HEAD,
            produce_status=PRODUCE_PLANNED)
        db.add(episode)
        db.flush()
        # 建档阶段 episodes 不带 shots（逐集制作时才产生）；带 shots 时一并落库
        # （兼容旧调用方与回归脚本）
        shots = ep.get("shots") or []
        if shots:
            shot_count += _insert_shots(db, episode.id, shots, maps, unmatched)
            episode.produce_status = PRODUCE_BUILT

    # 解析元信息（产出物：标题/简介/画风/视频风格/画幅 + 关键场次）入 Setting，
    # GET /script 读取。关键场次随建档持久化——逐集制作时按集号注入拆解强化
    # 指令（旧版整部一次解析是进程内直传，拆分后必须落库才能跨任务传递）
    meta = {"title": result["title"], "logline": result["logline"],
            "style": style_name_for(db, project_id),
            "video_style": result.get("video_style", ""),
            "aspect_ratio": result.get("aspect_ratio", ""),
            "key_scenes": result.get("key_scenes") or [],
            # 前置章节正文（首集之前的文本，通常总纲/人物设定）：第 1 集按集解析
            # 资产时一并带给 LLM，否则总纲里的人设锚点会整段丢失
            "front_text": (result.get("front_text") or "")[:20000],
            # 解析前置需求（21 号批次 5，I2 注入）：逐集阶段（拆解/画面描述）是
            # 独立任务，必须随 meta 持久化才能按集注入——否则「剧作基调/禁用内容」
            # 只影响审读那一刻，逐集制作时静默丢失
            "requirements": load_requirements(db, project_id)}
    db.add(Setting(key=script_meta_key(project_id),
                   value=json.dumps(meta, ensure_ascii=False)))
    db.commit()
    return {"characters": len(char_map), "scenes": len(result["scenes"]),
            "props": len(prop_map), "episodes": len(result["episodes"]),
            "shots": shot_count, "unmatched_refs": unmatched}


def _miss_factory(unmatched: dict[str, list[str]]):
    """未命中名单收集器（按桶去重）。"""
    def _miss(bucket: str, name: str) -> None:
        if name and name not in unmatched[bucket]:
            unmatched[bucket].append(name)
    return _miss


def _insert_shots(db: Session, episode_id: str, shots: list[dict],
                  maps: dict, unmatched: dict[str, list[str]]) -> int:
    """写入一集的分镜（供建档与逐集制作共用）。返回写入条数。

    引用按**名称**解析成资产 id（场景/角色/道具），未命中记入 unmatched
    供上层告警——不做任何名称近似（红线：语义判断归模型）。

    **落库前跑语速硬门**（25 号 3.1）：拆解期已跑过一次，但补镜（`SHOT_FILL`）、
    手工编辑、单镜重算等路径都会**绕过拆解**产出或改写分镜——实测 342 个台词镜里
    **160 个时长低于语速下限**（含硬门上线之后的产出，最狠 5 秒 86 字 = 17.2 字/秒）。
    落库是唯一收口点，故在此再校准一次；`allow_split=False` 只抬时长/夹取，
    **不改变镜头数**（落库时父镜的 video_prompt 已生成，拆出的子镜会共用它）。
    """
    from app.script.llm_parser import apply_duration_floor  # 延迟导入：避免循环

    _floor_stats = apply_duration_floor([{"shots": shots}], allow_split=False)
    if _floor_stats["raised"] or _floor_stats["capped"]:
        logger.info("落库前语速硬门：抬升 %s 镜、夹取 %s 镜（补镜/手工编辑/重算等路径）",
                    _floor_stats["raised"], _floor_stats["capped"])
    char_map = maps["characters"]
    scene_map = maps["scenes"]
    prop_map = maps["props"]
    _miss = _miss_factory(unmatched)
    n = 0
    for sh in shots:
        scene_name = (sh.get("scene_name", "") or "").strip()
        scene_ref = scene_map.get(scene_name)
        if not scene_ref and scene_name:
            _miss("scenes", scene_name)
        char_ids = [char_map[nm].id for nm in sh.get("character_names", [])
                    if nm in char_map]
        for nm in sh.get("character_names", []):
            if nm not in char_map:
                _miss("characters", nm)
        prop_ids = [prop_map[nm].id for nm in sh.get("prop_names", [])
                    if nm in prop_map]
        for nm in sh.get("prop_names", []):
            if nm not in prop_map:
                _miss("props", nm)
        # 妆造映射：按「角色名 → 形象名」重建 id 映射；形象查不到则丢键
        costume_map: dict[str, str] = {}
        for cname, img_name in (sh.get("costume_map_name") or {}).items():
            c = char_map.get(cname)
            if not c:
                continue
            img = db.scalars(
                select(CharacterImage)
                .where(CharacterImage.character_id == c.id,
                       CharacterImage.name == img_name)
                .order_by(CharacterImage.created_at, CharacterImage.id)
                .limit(1)
            ).first()
            if img:
                costume_map[c.id] = img.id
        db.add(Shot(
            episode_id=episode_id, seq=sh["seq"],
            description=sh["description"],
            excerpt=sh.get("excerpt", ""),
            video_prompt=sh.get("video_prompt", ""),
            camera=sh.get("camera", "固定"),
            duration=sh.get("duration", 5),
            scene_ref_id=scene_ref.id if scene_ref else None,
            character_ref_ids_json=char_ids,
            prop_ref_ids_json=prop_ids,
            transition_in=sh.get("transition_in", ""),
            state_json=sh.get("state") or {},
            end_state_json=sh.get("end_state") or {},
            cuts_json=sh.get("cuts") or [],
            purpose=sh.get("purpose", ""),
            risks_json=sh.get("risks") or [],
            dialogue_json=sh.get("dialogue", []),
            costume_map_json=costume_map,
        ))
        n += 1
    return n


def asset_maps_by_name(db: Session, project_id: str) -> dict:
    """按名称索引的项目资产（逐集制作时解析分镜引用用）。"""
    return {
        "characters": {c.name: c for c in db.scalars(
            select(Character).where(Character.project_id == project_id)).all()},
        "scenes": {s.name: s for s in db.scalars(
            select(Scene).where(Scene.project_id == project_id)).all()},
        "props": {p.name: p for p in db.scalars(
            select(Prop).where(Prop.project_id == project_id)).all()},
    }


def upsert_episode_assets(db: Session, project_id: str, seq: int,
                          assets: dict) -> dict:
    """按集资产落库（18 号 §61）：只新建**名单外**的，同名的复用已有记录。

    语义判断（哪些是本集新出现的）归 LLM——`extract_episode_assets` 已按已有
    名单过滤并做过白名单校验；这里只做确定性写入：同名逐字跳过（不覆盖已有
    设定，避免把早先更完整的 persona 冲掉）、新名新建并记 `origin_seq`
    （资产页据此标注「第 N 集解析」）与 `order_seq`（本集内的出场顺序，取
    LLM 输出数组的下标——提示词已要求数组按首次出场先后排；同名资产若在前
    几集已建，这里跳过，于是它保留**首次出场**时记下的顺序号）。

    返回 `{"characters": n, "scenes": n, "props": n, "reused": {...},
    "field_warnings": [...]}`（field_warnings 为字段边界告警，见 asset_field_guards）。
    """
    added = {"characters": 0, "scenes": 0, "props": 0}
    # 本次新建的行（组装写回提示词框时要用到；已存在的行不动）
    _created: dict[str, list] = {"characters": [], "scenes": [], "props": []}
    # 字段边界告警（G7/G8）：人设里写外观、外观里写性格是生图质量的上游污染源
    # ——只告警不拦截（门禁只拦 critical 的项目口径），按角色名留痕便于定点返工
    field_warnings: list[str] = []

    def _role(v: object) -> str:
        r = str(v or "").strip()
        return r if r in ("主角", "配角") else "配角"

    for idx, c in enumerate(assets.get("characters") or [], start=1):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        exists = db.scalars(select(Character).where(
            Character.project_id == project_id, Character.name == name)).first()
        if exists:
            continue
        persona = str(c.get("persona") or "")
        appearance = str(c.get("appearance") or "")
        for warn in check_character_fields(persona, appearance):
            field_warnings.append(f"{name}：{warn}")
            logger.warning("资产字段边界告警 %s（第 %d 集）：%s", name, seq, warn)
        _new_char = Character(project_id=project_id, name=name,
                              role=_role(c.get("role")),
                              persona=persona,
                              appearance=appearance,
                              voice_desc=str(c.get("voice_desc") or ""),
                              image_prompt=str(c.get("image_prompt") or ""),
                              origin_seq=seq, order_seq=idx)
        db.add(_new_char)
        _created["characters"].append(_new_char)
        added["characters"] += 1

    # 布局清洗用的角色名单：本集新建的角色 + 项目里已落库的角色（新建行还在
    # 会话里、查库查不到，所以要两处合并；≥2 字由 strip_figure_clauses 把关）
    _layout_roster = [c.name for c in _created["characters"]] + [
        n for n in db.scalars(select(Character.name).where(
            Character.project_id == project_id)).all() if n]

    for idx, s in enumerate(assets.get("scenes") or [], start=1):
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        exists = db.scalars(select(Scene).where(
            Scene.project_id == project_id, Scene.name == name)).first()
        if exists:
            continue
        row = Scene(project_id=project_id, name=name,
                    description=str(s.get("description") or ""),
                    # 布局落库前剔人物走位句（名单=本集新建角色 + 已落库角色）
                    layout=_clean_layout_figures(s.get("layout"), _layout_roster),
                    image_prompt=str(s.get("image_prompt") or ""),
                    origin_seq=seq, order_seq=idx)
        db.add(row)
        # 场景组：把组名与行一起暂存（建组/挂组在下面统一做，32 号）
        _created["scenes"].append((row, str(s.get("group") or "")))
        added["scenes"] += 1

    for idx, p in enumerate(assets.get("props") or [], start=1):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        exists = db.scalars(select(Prop).where(
            Prop.project_id == project_id, Prop.name == name)).first()
        if exists:
            continue
        _row = Prop(project_id=project_id, name=name,
                    description=str(p.get("description") or ""),
                    image_prompt=str(p.get("image_prompt") or ""),
                    origin_seq=seq, order_seq=idx)
        db.add(_row)
        _created["props"].append(_row)
        added["props"] += 1

    # 场景组落库（32 号）：建组/同名并入 + 场景挂组（必须在组装提示词之前——
    # 组装时要把组环境设定注入提示词框）
    _grp_added = _apply_scene_groups(db, project_id, _created["scenes"],
                                     assets.get("scene_groups"))
    if _grp_added:
        logger.info("第 %s 集场景组：新建 %s 个", seq, _grp_added)

    # **解析阶段就把完整提示词组装进提示词框**（2026-09-30 用户规则，同整篇解析）：
    # 逐集补资产也走同一条路，避免「整篇解析的框是完整的、逐集新增的又缺系统块」。
    # （函数内导入：drama 模块级 import 本模块，顶部导入会成环）
    from app.api.drama import (
        compose_character_prompt, compose_prop_prompt, compose_scene_prompt,
    )
    for _ct in _created["characters"]:
        compose_character_prompt(db, _ct)
    for _sc_row, _ in _created["scenes"]:
        compose_scene_prompt(db, _sc_row)
    for _pp in _created["props"]:
        compose_prop_prompt(db, _pp)

    db.commit()
    if field_warnings:
        added["field_warnings"] = field_warnings
    return added


def persist_episode_shots(db: Session, project_id: str, episode_id: str,
                          shots: list[dict]) -> dict:
    """逐集制作第一步：**立即落库该集分镜**（不等画面描述）。

    这是对 2026-09-22 事故（10 集 593 镜跑完 88 分钟，在落库前被看门狗杀掉、
    593 镜全丢）的直接修复：拆解一完成就写库，即使后续画面描述阶段失败或被杀，
    分镜本身已在库里，重跑只需补画面描述。

    覆盖语义：先清空该集旧分镜再写入（重跑该集 = 重建，不叠加）。
    """
    db.execute(delete(Shot).where(Shot.episode_id == episode_id))
    unmatched: dict[str, list[str]] = {"characters": [], "scenes": [], "props": []}
    n = _insert_shots(db, episode_id, shots, asset_maps_by_name(db, project_id),
                      unmatched)
    episode = db.get(Episode, episode_id)
    if episode is not None:
        episode.produce_status = PRODUCE_BUILDING
    db.commit()
    return {"shots": n, "unmatched_refs": unmatched}


def update_shot_video_prompts(db: Session, episode_id: str,
                              shots: list[dict]) -> int:
    """逐集制作第二步：把画面描述回填到已落库的分镜（按 seq 对账，UPDATE）。

    与 `persist_episode_shots` 分开是刻意的：分镜先落库（保命），画面描述
    后到（增强）。回填失败不影响已落库的分镜数据。
    """
    by_seq = {int(s.get("seq") or 0): s for s in shots}
    rows = db.scalars(select(Shot).where(Shot.episode_id == episode_id)).all()
    n = 0
    for row in rows:
        src = by_seq.get(row.seq)
        if not src:
            continue
        vp = src.get("video_prompt") or ""
        if vp and vp != row.video_prompt:
            row.video_prompt = vp
            n += 1
        # 画面描述阶段可能微调 duration/state（时序补拍会改时间小节，duration
        # 本身由语速硬门定稿，此处只同步描述类字段，不动 duration）
        if src.get("description") and src["description"] != row.description:
            row.description = src["description"]
    db.commit()
    return n
