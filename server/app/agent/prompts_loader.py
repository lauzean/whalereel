"""核心提示词外置加载器（批次 2b §32.4，D-4 决策：外置 + UI 在线编辑）。

- 目录 `agent/prompts_md/*.md`：frontmatter（name/description/stage/editable）
  + 正文（system prompt 全文）；
- `apply_external_prompts`：**模块导入时**用 md 正文覆盖 `prompts.py` 同名常量
  （md 缺失/损坏回退内置常量——双保险）；
- `reload_prompts`：**再次重放全部外置 md**（复用 `apply_external_prompts`）。
  保存 / 快照恢复 / 自学习追加三条路径在写盘后都调它，配合「消费方一律**属性访问**
  `prompts.X`」（取用时才查模块全局）实现**保存即生效、无需重启**（25 号批次 0.4）。
  **生效范围说清楚**：① UI 保存 / 恢复 / 追加 → 立即生效；② **手改 md 文件本身**
  仍需触发上述任一操作或重启进程（导入只发生一次）。
- `save_prompt`：编辑保存——写盘前旧内容快照进 Setting（可恢复），写盘后由调用方
  调 `reload_prompts()`；
- `restore_prompt`：从快照恢复上一版。

**身份来源 = 文件名**（`path.stem`）：frontmatter 的 `name` 只做一致性校验，
不一致时以文件名为准并告警。踩过的坑（V1.0 事故，日志里复发过三次）：曾以
frontmatter 的 `name` 为准定位文件，某份 md 把 `name` 写成了别的名字，于是
① 该 md 永远加载不进去（按错名找不到文件）② `list_prompts` 把错名当真实
提示词暴露给 UI ③ `save_prompt` 按错名另写一个文件，从此一真一假。
**这三条原先都没有任何日志**——故本模块现在**任何跳过都必须留痕**。

locked 段（安全合规/输出契约）以文件内段落形式存在，编辑器约定不动段名
结构；完整性自检 = 非空且 ≥50 字（防误清空）。
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 提示词文件目录（与 skills/ 同体例）
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts_md"
# 编辑快照 Setting 键前缀（drama.py 侧读写 DB，本模块只管文件）
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.M)


def _parse_md(text: str) -> tuple[dict[str, str], str]:
    """解析 frontmatter 与正文：返回 (meta, body)。无 frontmatter 时 meta 为空。"""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}, (text or "")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()
    return meta, m.group(2).lstrip("\n")


def list_prompts() -> list[dict]:
    """枚举外置提示词（目录为准，缺失字段给中性默认）。

    **name 一律取文件名**（`path.stem`），不取 frontmatter——见模块注释「身份来源」。
    frontmatter 声明了不一致的 name 时告警，并把声明值放进 `name_declared`
    供管理端外露：**不一致本身不是错误，静默才是不一致**。
    """
    items: list[dict] = []
    if not PROMPTS_DIR.is_dir():
        return items
    for path in sorted(PROMPTS_DIR.glob("*.md")):
        try:
            meta, _body = _parse_md(path.read_text(encoding="utf-8"))
        except OSError:
            logger.warning("提示词文件读取失败，已跳过：%s", path.name)
            continue
        declared = meta.get("name", "")
        if declared and declared != path.stem:
            logger.warning(
                "提示词 frontmatter 的 name 与文件名不一致（**以文件名为准**）："
                "%s 声明 %r", path.name, declared)
        items.append({
            "name": path.stem,
            "name_declared": declared,
            "description": meta.get("description", ""),
            "stage": meta.get("stage", ""),
            "editable": meta.get("editable", "true") != "false",
        })
    return items


def load_prompt(name: str) -> str | None:
    """按名读外置提示词正文（缺失/损坏返回 None，调用方回退内置常量）。

    正文统一 strip 首尾空白——setattr 热生效与快照恢复的比对口径一致。
    **返回 None 时必留日志**（缺失 / 读取失败 / 正文为空，三种原因分开写）：
    静默回退内置常量正是 V1.0 事故「某份 md 从未生效却无人发现」的成因。
    """
    path = PROMPTS_DIR / f"{name}.md"
    try:
        _meta, body = _parse_md(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("提示词 md 不存在，回退内置常量：%s", path.name)
        return None
    except (OSError, ValueError) as exc:
        logger.warning("提示词 md 读取失败（%s），回退内置常量：%s", exc, path.name)
        return None
    if not body.strip():
        logger.warning("提示词 md 正文为空，回退内置常量：%s", path.name)
        return None
    return body.strip()


def reload_prompts() -> int:
    """重新把外置 md 全部应用到 prompts 模块全局（保存 / 快照恢复 / 追加之后调用）。

    为什么不能只 `setattr(prompts_mod, name, 新值)`：消费方此前都是
    `from app.agent.prompts import X` 的**导入时值绑定**，setattr 改不到它们手里的
    引用——实测「保存后必须重启才生效」，而代码注释一度写着「无需重启」。
    现两步走：① 消费方改为**属性访问**（`prompts.X`，取用时才查模块全局）；
    ② 本函数重放全部外置 md（复用启动时的 `apply_external_prompts`），
    于是 UI 保存、快照恢复、自学习追加三条路径**立即生效**。

    返回本次覆盖条数（与启动时同口径）。
    """
    from app.agent import prompts as prompts_mod   # 延迟导入：prompts.py 末尾会回调本模块

    return apply_external_prompts(prompts_mod.__dict__)


def save_prompt(name: str, text: str, editable_check: bool = True) -> dict:
    """保存编辑后的提示词：完整性自检 → 写盘（调用方负责快照与热生效）。

    返回 {"ok": True, "path": ...}；校验失败抛 ValueError（API 转 400）。
    """
    items = {it["name"]: it for it in list_prompts()}
    if name not in items:
        raise ValueError(f"未知提示词：{name}")
    if editable_check and not items[name]["editable"]:
        raise ValueError(f"{name} 为锁定提示词，不允许在线编辑")
    body = (text or "").strip()
    if len(body) < 50:
        raise ValueError("提示词正文过短（疑似误清空），保存被拒绝")
    path = PROMPTS_DIR / f"{name}.md"
    meta_lines = [f"name: {name}"]
    if items[name].get("description"):
        meta_lines.append(f"description: {items[name]['description']}")
    if items[name].get("stage"):
        meta_lines.append(f"stage: {items[name]['stage']}")
    meta_lines.append(f"editable: {'true' if items[name]['editable'] else 'false'}")
    path.write_text("---\n" + "\n".join(meta_lines)
                    + "\n---\n\n" + body + "\n", encoding="utf-8")
    return {"ok": True, "path": str(path)}


def apply_external_prompts(namespace: dict) -> int:
    """用外置 md 正文覆盖 prompts 模块同名的 *_SYSTEM_PROMPT 常量。

    在 prompts.py 尾部调用（传入 globals()）；md 缺失/损坏/为空时保留内置
    常量（双保险）。返回覆盖数（日志观测）。

    **每一次跳过都留痕**，并在「目录里有几条 ≠ 实际生效几条」时告警汇总——
    此前 skip 是裸 `continue`、分文不记，于是「某份 md 从未生效」可以长期
    无人发现（日志里已复发过三次）。跳过原因分三类，便于一眼定位：
    命名不符 / prompts.py 无同名常量 / 正文缺失。
    """
    applied = 0
    skipped: list[str] = []
    if not PROMPTS_DIR.is_dir():
        logger.warning("提示词目录不存在，全部回退内置常量：%s", PROMPTS_DIR)
        return applied
    for item in list_prompts():
        name = item["name"]
        if not name.endswith("_SYSTEM_PROMPT"):
            skipped.append(f"{name}（非 _SYSTEM_PROMPT 命名）")
            continue
        if name not in namespace:
            skipped.append(f"{name}（prompts.py 无同名常量）")
            continue
        body = load_prompt(name)
        if body:
            namespace[name] = body
            applied += 1
        else:
            skipped.append(f"{name}（正文缺失，已回退内置）")
    if applied:
        logger.info("外置提示词加载 %s 条（prompts_md/）", applied)
    if skipped:
        logger.warning("外置提示词**未生效** %s 条：%s", len(skipped), "；".join(skipped))
    return applied
