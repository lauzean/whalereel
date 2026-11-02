"""资产绑定的作用域继承（借鉴 Jellyfish 的 scoped link，见 docs/design/15）。

**解决的问题**：本项目此前用「逐镜 blob 映射（`Shot.costume_map_json`）+
形象自带适用集数列表（`CharacterImage.episodes_json`）」表达换装，是**双账本**：
- 逐镜打标漏一个镜头就漂移；
- 形象上的集数列表与分镜的实际归属可能不一致；
- 「整集统一换装」这个最常见诉求没有直接表达方式（要 N 镜打 N 次）。

**机制**：一条 `CharacterAssetLink` 表达「哪个形象在什么范围内生效」，三级作用域
**就近覆盖**（scope 越具体优先级越高）：

    shot（单镜覆盖） > episode（整集生效） > project（整剧默认）

解析时按具体度排序取第一条命中；未命中任何绑定时返回 None，由调用方回退到
原有选图链（灰度兼容，绑定是增强而非替代）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CharacterAssetLink, Episode

# 作用域常量（禁止散落字符串：DB 值与排序权重同源）
SCOPE_PROJECT = "project"
SCOPE_EPISODE = "episode"
SCOPE_SHOT = "shot"
_SCOPE_ORDER = {SCOPE_SHOT: 0, SCOPE_EPISODE: 1, SCOPE_PROJECT: 2}


def bind_character_image(db: Session, *, project_id: str, character_id: str,
                         image_id: str, scope: str = SCOPE_PROJECT,
                         episode_id: str | None = None,
                         shot_id: str | None = None,
                         note: str = "") -> CharacterAssetLink:
    """建立/更新一条绑定（同作用域幂等覆盖——一个范围只允许一个生效形象）。

    作用域与定位字段必须自洽：episode 需给 episode_id，shot 需给 shot_id，
    project 两者都为空——不自洽直接报错（拼接出「无定位的作用域」会让解析
    永远命中不到，是静默失效的温床）。
    """
    if scope == SCOPE_EPISODE and not episode_id:
        raise ValueError("episode 作用域必须提供 episode_id")
    if scope == SCOPE_SHOT and not shot_id:
        raise ValueError("shot 作用域必须提供 shot_id")
    if scope == SCOPE_PROJECT:
        episode_id = shot_id = None
    elif scope == SCOPE_SHOT:
        episode_id = None  # 单镜作用域由 shot_id 唯一定位

    existing = db.scalars(select(CharacterAssetLink).where(
        CharacterAssetLink.project_id == project_id,
        CharacterAssetLink.character_id == character_id,
        CharacterAssetLink.scope == scope,
        CharacterAssetLink.episode_id == episode_id,
        CharacterAssetLink.shot_id == shot_id,
    )).first()
    if existing is not None:
        existing.image_id = image_id
        if note:
            existing.note = note
        return existing
    row = CharacterAssetLink(
        project_id=project_id, character_id=character_id, image_id=image_id,
        scope=scope, episode_id=episode_id, shot_id=shot_id, note=note)
    db.add(row)
    return row


def resolve_bound_image_id(db: Session, *, project_id: str, character_id: str,
                           episode_seq: int | None = None,
                           shot_id: str | None = None) -> str | None:
    """解析该角色在此镜头应使用的形象（就近覆盖）；无绑定返回 None。

    定位参数用 `episode_seq`（集号）与调用方现有口径一致——集号 → episode_id
    的换算在本函数内完成，避免调用方为绑定机制改签名。一次查询取回全部候选行
    后在内存排序：绑定总量是「角色数 × 作用域数」量级，避免按作用域反复查库。
    """
    rows = db.scalars(select(CharacterAssetLink).where(
        CharacterAssetLink.project_id == project_id,
        CharacterAssetLink.character_id == character_id,
    )).all()
    if not rows:
        return None
    episode_id: str | None = None
    if episode_seq is not None:
        episode = db.scalars(select(Episode).where(
            Episode.project_id == project_id, Episode.seq == episode_seq)).first()
        episode_id = episode.id if episode is not None else None

    def matches(row: CharacterAssetLink) -> bool:
        if row.scope == SCOPE_SHOT:
            return bool(shot_id) and row.shot_id == shot_id
        if row.scope == SCOPE_EPISODE:
            return bool(episode_id) and row.episode_id == episode_id
        return True  # project 级恒命中（兜底）

    hits = [r for r in rows if matches(r)]
    if not hits:
        return None
    hits.sort(key=lambda r: _SCOPE_ORDER.get(r.scope, 99))
    return hits[0].image_id


def unbind(db: Session, *, project_id: str, character_id: str, scope: str,
           episode_id: str | None = None, shot_id: str | None = None) -> bool:
    """解除一条绑定（返回是否命中删除）。"""
    row = db.scalars(select(CharacterAssetLink).where(
        CharacterAssetLink.project_id == project_id,
        CharacterAssetLink.character_id == character_id,
        CharacterAssetLink.scope == scope,
        CharacterAssetLink.episode_id == episode_id,
        CharacterAssetLink.shot_id == shot_id,
    )).first()
    if row is None:
        return False
    db.delete(row)
    return True


def list_bindings(db: Session, project_id: str,
                  character_id: str | None = None) -> list[dict]:
    """列出绑定（按具体度排序，UI 展示「哪条在生效」）。"""
    q = select(CharacterAssetLink).where(
        CharacterAssetLink.project_id == project_id)
    if character_id:
        q = q.where(CharacterAssetLink.character_id == character_id)
    rows = db.scalars(q).all()
    rows = sorted(rows, key=lambda r: _SCOPE_ORDER.get(r.scope, 99))
    return [{"id": r.id, "character_id": r.character_id,
             "image_id": r.image_id, "scope": r.scope,
             "episode_id": r.episode_id, "shot_id": r.shot_id,
             "note": r.note} for r in rows]
