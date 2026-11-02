"""技能包管理 API（系统管理·技能包页签）。

技能包为 agent/skills/*.md（frontmatter + 正文，见 app.agent.skills），解析/生成
每次实时扫描目录——写盘即热生效，无需重启。本路由提供清单/读取/保存/新建/
删除/快照恢复；整路由仅管理员（技能包影响全部项目的生成质量，属系统设置域）。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.skills import (
    SKILL_FILENAME_RE, delete_skill_file, load_skill_packs_with_files,
    read_skill_raw, write_skill_file,
)
from app.api.auth import require_admin
from app.db.models import Setting
from app.db.session import get_db

# 整路由管理员门禁（技能包为系统设置域资产）
router = APIRouter(prefix="/api/skills", tags=["skills"],
                   dependencies=[Depends(require_admin)])

# 编辑快照 Setting 键前缀（每次保存覆盖式快照上一版，与提示词快照同模式）
_SNAPSHOT_KEY_PREFIX = "skill_snapshot:"


def _snapshot_key(filename: str) -> str:
    return f"{_SNAPSHOT_KEY_PREFIX}{filename}"


class SkillSaveIn(BaseModel):
    content: str  # 全文（含 frontmatter）


class SkillCreateIn(BaseModel):
    file: str  # 文件名（含 .md）


def _meta_dict(p) -> dict:
    """技能包元信息（清单展示；kind 区分题材包/模型包）。"""
    return {
        "file": p.name,  # 占位，下方按文件名修正
        "name": p.name,
        "kind": "model" if p.models else "genre",
        "genres": p.genres,
        "models": p.models,
        "task_type": p.task_type,
        "description": p.description,
        "chars": len(p.body),
    }


@router.get("")
def list_skills():
    """技能包清单（元信息附真实文件名；前端据 kind 分组展示）。"""
    items = []
    for filename, pack in load_skill_packs_with_files():
        meta = _meta_dict(pack)
        meta["file"] = filename
        items.append(meta)
    return items


@router.get("/{filename}")
def get_skill(filename: str, db: Session = Depends(get_db)):
    """读取技能包全文（含 frontmatter；附快照存在标记）。"""
    raw = read_skill_raw(filename)
    if raw is None:
        raise HTTPException(404, "技能包不存在")
    return {"ok": True, "file": filename, "content": raw,
            "has_snapshot": db.get(Setting, _snapshot_key(filename)) is not None}


@router.put("/{filename}")
def save_skill(filename: str, body: SkillSaveIn,
               db: Session = Depends(get_db)):
    """保存技能包：快照上一版 → 校验写盘（写盘即时生效）。"""
    if not SKILL_FILENAME_RE.match(filename):
        raise HTTPException(400, f"无效文件名：{filename}")
    old = read_skill_raw(filename)
    if old is None:
        raise HTTPException(404, "技能包不存在")
    try:
        write_skill_file(filename, body.content)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # 覆盖式快照（恢复只回退一步，与提示词编辑一致）
    db.merge(Setting(key=_snapshot_key(filename), value=old))
    db.commit()
    return {"ok": True}


@router.post("", status_code=201)
def create_skill(body: SkillCreateIn):
    """新建技能包：写入最小可用模板（frontmatter + 正文占位），创建后在线编辑。"""
    filename = body.file.strip().lower()
    try:
        write_skill_file(filename, _TEMPLATE)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "file": filename}


@router.delete("/{filename}")
def remove_skill(filename: str, db: Session = Depends(get_db)):
    """删除技能包文件（快照一并清除；产物不受影响）。"""
    try:
        delete_skill_file(filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    snap = db.get(Setting, _snapshot_key(filename))
    if snap:
        db.delete(snap)
        db.commit()
    return {"ok": True}


@router.post("/{filename}/restore")
def restore_skill(filename: str, db: Session = Depends(get_db)):
    """从快照恢复上一版（恢复后快照清除，再次保存会重新快照）。"""
    snap = db.get(Setting, _snapshot_key(filename))
    if not snap or not (snap.value or "").strip():
        raise HTTPException(404, "无可恢复的快照")
    try:
        write_skill_file(filename, snap.value)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.delete(snap)
    db.commit()
    return {"ok": True}


# 新建模板：占位 frontmatter（genres 满足校验）+ 正文骨架，用户保存前自行修改
_TEMPLATE = """---
name: （技能包名称）
genres: [题材]
description: 一句话说明该包的适用场景与作用
---

## 使用规范
- 在此编写专业分镜/制作/提示词知识。
- 删除本占位内容前请先补全上方 frontmatter 与正文。
"""
