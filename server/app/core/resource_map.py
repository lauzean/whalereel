# -*- coding: utf-8 -*-
"""越权检查用的「网址清单」：每个网址对应的是哪种东西，该去查谁。

**这份清单解决什么问题（大白话）**：
有些接口的网址里只有一串编号、没有项目编号，比如「改角色」是
`PATCH /api/characters/8f3a...`。服务器光看这串编号，**不知道这个角色是哪个
项目的**，也就没法判断「来的人是不是这个项目的主人」——本轮排查实测：这类接口
以前只检查「这行数据存不存在」，所以**任何登录用户都能改别人的角色/分集/分镜**。

要判断归属，得先知道「这串编号是什么东西」，然后顺着它查到项目：
- 编号是**角色/场景/道具/分集/剧本版本/会话** → 这些表里直接有「属于哪个项目」；
- 编号是**分镜** → 分镜表里没有项目编号，要先按它的 `episode_id` 查到分集；
- 编号是**产物** → 产物表里也没有项目编号，要反查「哪个分镜/节点用过它」；
- 编号是**任务** → 任务表里直接有项目编号。

这份清单就是把「网址长什么样」对上「编号是什么东西」的那一列。服务器收到请求时
先翻这一列，知道该查谁，再判断归属。

**为什么写成一列清单，而不是在 46 个接口里各写一句判断**：
那种写法下一版新增接口时**必然又漏一个**（审计 14 号报告里「同一份保证散落在多条
路径」就是这个毛病）。写成一列之后，新增同类接口只要在这里加一行，接口函数本身
不用动。守着这份清单的是 `test_auth_multitenant.py` 里逐个接口试越权的用例。

清单每行 = (网址的正则, 这种东西的类型)。正则的第一个括号里是编号。
"""

from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import (
    Character, Episode, NodeVersion, Project, Prop, Scene, ScriptVersion, Shot,
    ShotReview,
)

# 网址 → 编号是什么东西。类型决定去哪张表查、怎么一路查到项目。
# 顺序重要：更具体的网址要排在更笼统的前面（如 /shots/{id}/verdict 与 /shots/{id}）。
RULES: tuple[tuple[str, str], ...] = (
    # ---------- 短剧：分集 ----------
    (r"^/api/episodes/([^/]+)", "episode"),
    # ---------- 短剧：分镜 ----------
    (r"^/api/shots/([^/]+)", "shot"),
    # ---------- 短剧：角色 / 场景 / 道具 ----------
    (r"^/api/characters/([^/]+)", "character"),
    (r"^/api/scenes/([^/]+)", "scene"),
    (r"^/api/props/([^/]+)", "prop"),
    # ---------- 剧本版本 ----------
    (r"^/api/script/versions/([^/]+)", "script_version"),
    # ---------- Agent 会话 ----------
    (r"^/api/sessions/([^/]+)", "session"),
    # ---------- 产物（归属经 node_versions 反查，见 access.ensure_artifact_access）----------
    # 注意：`sign` 是签发端点自己的路径段，**不能**被当成 artifact_id
    # （否则「给合法产物签发链接」会被误判成越权——实测踩到过一次）
    (r"^/api/artifacts/(?!sign$)([^/]+)/file$", "artifact"),
    (r"^/api/artifacts/(?!sign$)([^/]+)$", "artifact"),
    # ---------- 抽卡版本（需 node_id 与 project_id，两个都从 query 取）----------
    (r"^/api/nodes/([^/]+)/versions/([^/]+)$", "version"),
    # ---------- 任务（归属：发起人优先，历史任务按项目）----------
    # 覆盖 /api/tasks/{id} 与 /api/tasks/{id}/cancel 等子路径
    (r"^/api/tasks/([^/]+)(?:/.*)?$", "task"),
    # ---------- 旧产物静态文件（按文件路径反查 Asset → 归属项目）----------
    # 库里有 1050 个「按项目分目录之前」的产物散在 artifacts 根下，它们只能走
    # /files；不判归属的话，知道文件名就能跨租户取图/取视频。
    (r"^/files/(.+)$", "file"),
)

_COMPILED = tuple((re.compile(pat), kind) for pat, kind in RULES)


def _project_of(db: Session, kind: str, match: re.Match, path: str,
                query: dict) -> str | None:
    """按资源类型取归属项目 id；取不到返回 None（表示无法判定 → 拒绝）。"""
    gid = match.group(1)
    if kind == "episode":
        row = db.get(Episode, gid)
        return getattr(row, "project_id", None) if row else None
    if kind == "shot":
        row = db.get(Shot, gid)
        if not row:
            return None
        ep = db.get(Episode, row.episode_id)
        return getattr(ep, "project_id", None) if ep else None
    if kind == "character":
        row = db.get(Character, gid)
        return getattr(row, "project_id", None) if row else None
    if kind == "scene":
        row = db.get(Scene, gid)
        return getattr(row, "project_id", None) if row else None
    if kind == "prop":
        row = db.get(Prop, gid)
        return getattr(row, "project_id", None) if row else None
    if kind == "script_version":
        row = db.get(ScriptVersion, gid)
        return getattr(row, "project_id", None) if row else None
    if kind == "session":
        from app.db.models import Session as SessionModel

        row = db.get(SessionModel, gid)
        return getattr(row, "project_id", None) if row else None
    if kind == "version":
        node_id = gid
        version_id = match.group(2)
        row = db.get(NodeVersion, version_id)
        if not row or row.node_id != node_id:
            return None
        return row.project_id or None
    return None


# 每种「编号」对应哪张表。取值时**只看该行自己的 owner_user_id**（权限归属），
# 不再沿「分镜→分集→项目→主人」这类反查链（2026-09-30 用户定的两层模型：
# project_id 管业务归属，owner_user_id 管权限归属，判权限一步直达）。
KIND_MODEL = {
    "episode": Episode,
    "shot": Shot,
    "character": Character,
    "scene": Scene,
    "prop": Prop,
    "script_version": ScriptVersion,
    "session": None,      # 运行时导入（避免循环依赖）
    "version": None,      # 抽卡版本：要连 node_id 一起校验，单独处理
    "task": None,         # 运行时导入
}


def _model_for(kind: str):
    if kind == "session":
        from app.db.models import Session as SessionModel

        return SessionModel
    if kind == "task":
        from app.db.models import Task

        return Task
    return KIND_MODEL.get(kind)


def resource_owner(db: Session, path: str, query: dict) -> tuple[str, str] | None:
    """返回 (归属类型, 值)；不适用/查不到返回 None。

    归属类型有两种，调用方按它决定怎么用这个值：
    - `"user"`：值是**用户 id**（该行的 `owner_user_id`）——判权限直接拿它比。
      空串表示这行没有归属（共享资源/遗留行）→ 只有管理员可见；
    - `"artifact"`：值是产物 id（产物没有 project_id，归属判定走
      `access.ensure_artifact_access`，同样只看它自己的 owner_user_id）。

    刻意**只有这两种**：混着用会判错（例如把「用户 id」当「项目 id」去查项目，
    实测踩到过一次）。
    """
    for pattern, kind in _COMPILED:
        m = pattern.match(path)
        if not m:
            continue
        if kind == "artifact":
            return ("artifact", m.group(1))
        if kind == "file":
            # 旧产物静态文件：先按路径找到产物行，再按它自己的归属判。
            # 查不到（未登记的散落文件）返回 ("artifact", "") → 中间件按「交给
            # 端点自己 404」处理，不会误放行也不会误拦管理员。
            #
            # 用 media_sign.resolve_artifact_relpath 解析（**只读**）：它同时做
            # 归一化与目录归属校验，和发文件那条路用同一个解析器，避免两套口径。
            from app.core import media_sign
            from app.db.models import Asset

            resolved = media_sign.resolve_artifact_relpath(m.group(1))
            if resolved is None:
                return ("artifact", "")
            asset = db.query(Asset).filter(Asset.path == str(resolved)).first()
            if asset is None:
                return ("artifact", "")
            return ("artifact", asset.id)
        if kind == "version":
            # 抽卡版本：必须先确认这个版本属于这个节点（否则拿 A 节点的版本号配
            # B 节点的 id 就能绕过），再看它自己的归属
            node_id, version_id = m.group(1), m.group(2)
            row = db.get(NodeVersion, version_id)
            if not row or row.node_id != node_id:
                return ("user", "")
            return ("user", row.owner_user_id or "")
        model = _model_for(kind)
        if model is None:
            continue
        row = db.get(model, m.group(1))
        if row is None:
            return ("user", "")
        return ("user", getattr(row, "owner_user_id", "") or "")
    return None


# ---------- 按「参数名」自动找表（2026-09-30 补：不再依赖手写清单）----------
#
# 上面那份清单要人维护，**漏写一行的后果是「那个接口没人判归属」而测试不会红**。
# 这一层改用「按参数名找表」自动兜底：
# - 参数名去掉 `_id` 得到资源名（`character_id`→character、`shot_id`→shot）；
# - 在 SQLAlchemy 注册的表里找同名/同复数的表（character→characters）；
# - 找到就按那一行的 `owner_user_id` 判归属；
# - **找不到就留痕告警**（这是一条需要人看一眼的信号，不是静默放过）。
#
# 为什么不能只看清单：新增接口的人不会记得回来改清单；而参数叫 `character_id`
# 是天然约定，按它找表是确定性的、不需要人维护。
def _table_for_stem(stem: str):
    """按参数名的「词干」找表：`character`→characters、`shot`→shots。

    找不到返回 None（调用方留痕告警，不猜）。
    """
    from app.db.models import Base

    if not stem:
        return None
    for table in Base.metadata.sorted_tables:
        name = table.name.lower()
        if name in (stem, f"{stem}s", f"{stem}es"):
            return table
    return None


def owner_from_params(db: Session, params: dict, user_id: str) -> tuple[str, bool]:
    """按请求里的资源参数判归属（**不需要手写清单**）。

    返回 `(拒绝原因, 是否识别到资源)`：
    - 第一个值非空 = 越权，调用方直接拿它当 404 文案；
    - 第二个值为 False = 这段请求里**没有任何可识别的资源参数**，调用方据此决定
      要不要留痕（用于发现「新接口没纳入判定」这种情况）。
    """
    import logging

    logger = logging.getLogger(__name__)
    seen_any = False
    for name, value in (params or {}).items():
        if not name.endswith("_id") or name == "project_id" or not value:
            continue
        if not isinstance(value, str):
            continue
        stem = name[:-3].lower()
        table = _table_for_stem(stem)
        if table is None:
            # 认不出来（如 image_id 对应的表叫 character_images）：不猜，留痕让人看。
            # 这条 warning 就是「机制兜底」的落点——它把静默漏判变成了可发现的信号。
            logger.warning(
                "归属检查认不出参数 %s（找不到对应表）——若这是新接口，"
                "请在 core/resource_map.py 的 RULES 里补一行", name)
            continue
        seen_any = True
        row = db.get(_model_of(table), value)
        if row is None:
            continue        # 行不存在：交给端点自己 404
        owner = (getattr(row, "owner_user_id", "") or "")
        if not owner:
            continue        # 无归属（共享资源）：端点自行处理
        if owner != user_id:
            return ("资源不存在", True)
    return ("", seen_any)


def _model_of(table):
    """由表对象取对应模型类（供 db.get 用）。

    按**表名**比对，不用对象身份比较：`mapper.local_table` 与
    `metadata.sorted_tables` 里的元素实测不是同一个对象实例，用 `is` 会全部落空
    （踩过：于是自动识别静默失效、接口照样放行）。
    """
    from app.db.models import Base

    name = getattr(table, "name", "")
    for mapper in Base.registry.mappers:
        if getattr(mapper.local_table, "name", "") == name:
            return mapper.class_
    return None
