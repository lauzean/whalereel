"""生成就绪度与信息确认态（单一聚合服务）。

设计来源（2026-09-13 借鉴 Jellyfish，见 docs/design/15-jellyfish-borrowings.md）：

**三条状态语义分离**——本项目此前的状态混用是审计出的多条严重缺陷的共同成因
（`shot.generating` 既当运行态又当 UI 门控 → 断线重连后永久假进度条；
`Episode.status` 有 composed 分支却从无人写入；前端把「批次完成」当「产物就绪」）。
本模块把三者显式拆开：

1. **信息确认态**（`info_status`）：镜头的资产引用是否都能对上名单/都有参考图
   —— `ready` / `pending`，纯确定性推导，与运行态无关；
2. **运行时状态**：来自任务系统（`_shot_dict` 的 `status`/`generating` 字段），
   本模块不参与；
3. **视频就绪度**（`video_readiness`）：能否立刻提交视频生成——本模块聚合返回
   **结构化检查项**（`{key, ok, message}`），供预检、提交前硬门、前端向导
   共用同一份判定——「同一份保证散落在多条路径上」是审计 14 号报告里 6 条
   严重缺陷的共同形态，单一来源即可根治。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (  # noqa: F401 类型提示用
    Character, CharacterImage, Episode, NodeVersion, Prop, Scene, Setting, Shot,
)

# 检查项键名常量（前端按 key 做定位与文案分组，禁止散落字符串）
CHECK_FIRST_FRAME = "first_frame_ready"
CHECK_SCENE_ASSET = "scene_asset_ready"
CHECK_PROP_ASSET = "prop_asset_ready"
CHECK_CHARACTER_ASSET = "character_asset_ready"
CHECK_REFERENCES = "references_bound"
CHECK_STORYBOARD = "storyboard_checked"
# 整集级体检残留（seq=None：某段剧本没被任何镜头认领、源行被多镜认领等）。
# 与 CHECK_STORYBOARD（本镜缺陷）分开：整集级问题是「剧本完整性」而非「这镜
# 拍不了」，不该挂到具体镜号上，也不该拦住单镜出片。
CHECK_STORYBOARD_EPISODE = "storyboard_episode_checked"
CHECK_VIDEO_MODEL = "video_model_ready"
CHECK_DURATION = "duration_ready"
# 端点健康（25 号 1.5）：欠费/队列满的**前置探测**——账本由 providers 在每次
# 调用失败/成功时维护（见 services/provider_health），本模块只读不探测
CHECK_PROVIDER_HEALTH = "provider_health"
# 场景补充视角（反打/交互区特写）：声明了却缺图 → 只提示不拦（见下方检查项）
CHECK_VIEW_REF = "view_ref_ready"

# 信息确认态取值（与运行态严格区分：运行态由 task 系统表达）
INFO_READY = "ready"
INFO_PENDING = "pending"


def _check(key: str, ok: bool, message: str, advisory: bool = False,
           **extra: Any) -> dict:
    """构造一条结构化检查项（前端统一渲染 + 可按 key 定位）。

    advisory=True 表示「非阻断提示」：未满足时**不拦截提交**（如缺首帧会被
    提交层跳过、场景/道具缺图只降级一致性），仅供前端展示与预检提示——
    与 error 级（角色缺图/未绑引用/体检 error）严格区分。
    """
    return {"key": key, "ok": ok, "message": message,
            "advisory": advisory, **extra}


def provider_health_check(db: Session, project_id: str, kind: str,
                          model_id: str = "") -> dict:
    """端点健康前置探测（25 号 1.5，**只读账本、不发任何探测请求**）。

    各平台都没有免费探针（任何真实调用都会计费/耗时），所以「前置」靠的是
    **上一次调用的结果**：`providers._record_model_call` 在每次对外调用后写账本
    （欠费 → 30 分钟；队列满 → 10 分钟；任一调用成功即解除）。本函数把账本里
    的结论翻译成一条就绪度检查项——提交前硬门、成片预检、前端向导共用。

    kind：`t2i`（首帧/定妆图）或 `t2v`（视频）——两者可能落在不同服务商上。
    model_id：**本次实际要用的模型**（提交侧传入）。用户在下拉里选了具体模型时
    与项目预设可能不是同一个端点，查错端点会误拦（预设欠费、所选健康）或漏拦
    （25 号复查后端 #8）；留空 = 按项目预设解析。
    """
    from app.api.drama import _resolve_model
    from app.core.providers import endpoint_host
    from app.services import provider_health

    label = {"t2i": "生图", "t2v": "视频"}.get(kind, kind)
    host = endpoint_host(kind, _resolve_model(db, project_id, kind, model_id))
    mark = provider_health.health_of(db, host) if host else None
    if mark is None:
        return _check(CHECK_PROVIDER_HEALTH, True,
                      f"{label}端点可用（{host or '本地推理/未配置'}）")
    return _check(CHECK_PROVIDER_HEALTH, False,
                  f"{label}端点 {host} 当前不可用："
                  f"{provider_health.describe(mark)}",
                  host=host, mark_kind=str(mark.get("kind") or ""))


def shot_info_status(db: Session, project_id: str, shot: Shot) -> str:
    """镜头【信息确认态】（确定性，零 LLM）：资产引用是否都已确认可用。

    与运行态（是否已生成）**无关**——一条从未生成的镜头可以是 ready，
    一条生成失败的镜头也不因此变成 pending。

    判据（宁严勿松：确认态由「可核对的事实」构成，不做语义猜测）：
    - 场景引用存在且有主图（无主图 = 陈设无锚点，需先确认/生成）；
    - 出场角色引用存在且有可用的定妆参考图（缺图 = 一致性裸奔，硬门已拦）；
    - 道具引用存在且有定妆图（缺图仅降级一致性，记为未确认但不阻断）。
    """
    from app.api.drama import _character_artifact, _prop_artifact, _scene_artifact

    scene = db.get(Scene, shot.scene_ref_id) if shot.scene_ref_id else None
    if scene is not None and not _scene_artifact(db, scene):
        return INFO_PENDING
    for cid in shot.character_ref_ids_json or []:
        if not _character_artifact(db, project_id, cid, None):
            return INFO_PENDING
    for pid in shot.prop_ref_ids_json or []:
        prop = db.get(Prop, pid)
        if prop is not None and not _prop_artifact(db, prop):
            return INFO_PENDING
    return INFO_READY


def shot_video_readiness(db: Session, project_id: str, shot: Shot,
                         episode: Episode | None = None) -> dict:
    """单镜视频生成就绪度：聚合全部前置条件，返回结构化检查项清单。

    这是「能不能提交视频生成」的**唯一判定来源**——提交前硬门、成片预检、
    前端向导全部消费本函数，不再各自实现一套（审计 14 号报告：同一份保证
    散落多条路径是 6 条严重缺陷的共同形态）。

    返回 {"ready": bool, "checks": [{key, ok, message}], "info_status": str}
    """
    from app.api.drama import (
        _character_artifact, _episode_precheck, _extra_ref_artifacts,
        _first_frame_artifact, _latest_artifact, _prop_artifact,
        _scene_artifact,
        _unresolved_mentions, storyboard_qa_key,
    )

    checks: list[dict] = []

    # 1. 首帧（视频以首帧起像；缺失时提交层会 skip）
    ff = _first_frame_artifact(db, project_id, shot)
    checks.append(_check(
        CHECK_FIRST_FRAME, bool(ff),
        "首帧已就绪" if ff else "缺首帧：视频以首帧起像，需先生成首帧",
        advisory=True))

    # 2. 资产参考图（角色=error 级，场景/道具=降级一致性）
    scene = db.get(Scene, shot.scene_ref_id) if shot.scene_ref_id else None
    scene_ok = bool(scene is None) or bool(_scene_artifact(db, scene))
    checks.append(_check(
        CHECK_SCENE_ASSET, scene_ok,
        "场景主图已就绪" if scene_ok else
        f"场景「{scene.name if scene else ''}」缺主图：陈设一致性下降",
        advisory=True))

    chars_missing: list[str] = []
    for cid in shot.character_ref_ids_json or []:
        if not _character_artifact(db, project_id, cid, None):
            char = db.get(Character, cid)
            if char:
                chars_missing.append(char.name)
    checks.append(_check(
        CHECK_CHARACTER_ASSET, not chars_missing,
        "出场角色定妆图已就绪" if not chars_missing else
        f"以下出场角色尚未生成基础形象：{'、'.join(chars_missing)}"
        "（人物一致性依赖参考图，裸提示词生成会逐镜漂移）"))

    props_missing: list[str] = []
    for pid in shot.prop_ref_ids_json or []:
        prop = db.get(Prop, pid)
        if prop is not None and not _prop_artifact(db, prop):
            props_missing.append(prop.name)
    checks.append(_check(
        CHECK_PROP_ASSET, not props_missing,
        "道具定妆图已就绪" if not props_missing else
        f"以下道具缺定妆图：{'、'.join(props_missing)}（道具外观可能跨镜漂移）",
        advisory=True))

    # 3. 未绑定 @ 引用（error 级）：提示词里仍带前缀 @ = 参考图没绑上
    if (shot.video_prompt or "").strip():
        unbound = [it for it in _unresolved_mentions(db, project_id, shot)
                   if it.get("level") == "error"]
        checks.append(_check(
            CHECK_REFERENCES, not unbound,
            "参考图引用已全部绑定" if not unbound else
            "以下引用无法绑定参考图：" + "；".join(
                f"{it['kind']}@{it['name']}（{it['reason']}）" for it in unbound[:4])))

    # 4. 分镜体检 error 残留（按集过滤——项目级存档需按 episode 维度收敛）
    ep_seq = episode.seq if episode is not None else None
    qa_setting = db.get(Setting, storyboard_qa_key(project_id))
    qa_all: list[dict] = []
    if qa_setting and qa_setting.value:
        try:
            qa_all = json.loads(qa_setting.value)
        except ValueError:
            qa_all = []
    # 拆两层：本镜缺陷（seq=本镜）→ 阻断；整集缺陷（seq=None）→ 仅提示。
    # 旧实现把 seq=None 也按 `in (None, shot.seq)` 匹配，于是**每一镜**都会报
    # 同一条整集级问题，文案还是「镜1：分镜体检存在 error：[line_gap]源行未
    # 认领：…」——镜1 根本没有这个问题（张冠李戴），而且 [line_gap] 是内部
    # 规则名，用户读不懂（实测报障 2026-09-23）。
    _ep_qa = [i for i in qa_all
              if i.get("level") == "error" and i.get("episode") in (None, ep_seq)]
    qa_errors = [i for i in _ep_qa if i.get("seq") == shot.seq]
    qa_ep_errors = [i for i in _ep_qa if i.get("seq") is None]
    checks.append(_check(
        CHECK_STORYBOARD, not qa_errors,
        "分镜体检通过" if not qa_errors else
        # 用 title（白话短标）而非 message（含 [rule] 内部标记）——message 是
        # 给日志/面板明细用的，直接抛给用户是黑话
        "本镜体检存在 error：" + "；".join(
            str(i.get("title") or i.get("message") or "")[:30]
            for i in qa_errors[:3])))
    checks.append(_check(
        CHECK_STORYBOARD_EPISODE, not qa_ep_errors,
        "整集体检通过" if not qa_ep_errors else
        f"整集还有 {len(qa_ep_errors)} 处待处理：" + "；".join(
            str(i.get("title") or i.get("message") or "")[:24]
            for i in qa_ep_errors[:2]),
        advisory=True))

    # 5. 模型与时长（生成参数可满足性）
    from app.api.drama import KIND_T2V, _resolve_model  # 局部导入防循环

    model = _resolve_model(db, project_id, KIND_T2V, "")
    # 「视频模型是否可用」要真的能判否。旧实现写的是 `bool(model) or True`
    # ——**恒为 True**，这条检查从来没有拦住过任何人、也不会报出任何问题
    # （2026-09-30 修：恒真断言等于没有这道检查）。
    #
    # 判据：项目预设解析出来的模型优先；为空时**不算错**——提交时 runner 会
    # 用自己的 default_model 兜底（见 _resolve_model docstring），所以空串是
    # 合法状态，只是把「将要使用哪个模型」如实写进 message。真要报不 ok 的
    # 只有一种情形：解析出来的**本地模型权重没装**（那时提交必失败，
    # 已在 task_engine 提交预检里硬拦，这里只是提前说出来）。
    video_model_ok = True
    video_model_note = ""
    if model:
        try:
            from app.core.model_manager import installed_spec_ids, get_spec

            spec = get_spec(model)
            if spec is not None and model not in installed_spec_ids():
                video_model_ok = False
                video_model_note = "（本地权重未安装，提交会被拦下）"
        except Exception:  # noqa: BLE001 探测失败按就绪处理，不在就绪度里造新的失败源
            video_model_ok = True
    checks.append(_check(
        CHECK_VIDEO_MODEL, video_model_ok,
        f"视频模型：{model or '未指定（用端点/runner 默认）'}{video_model_note}",
        advisory=not video_model_ok))
    checks.append(_check(
        CHECK_DURATION, 5 <= int(shot.duration or 0) <= 12,
        f"时长 {shot.duration}s"))

    # 5.5 场景补充视角（反打/交互区特写）：**声明了却缺图**必须说出来——
    #     代码是「产物存在才挂」，缺图时**静默不挂**：用户以为挂上了、其实没有，
    #     然后又是一轮"改了没用"（2026-09-29 用户追问「补充视角 llm 会自动引用？」时
    #     发现这条链路完全没有检查）。只提示不拦：缺图仍可出片（凭空编另一侧空间）。
    from app.api.drama import SCENE_EXTRA_VIEWS, _latest_artifact

    _vref = str((shot.state_json or {}).get("view_ref") or "").strip()
    if _vref in SCENE_EXTRA_VIEWS:
        _scene_obj = db.get(Scene, shot.scene_ref_id) if shot.scene_ref_id else None
        _have = bool(_scene_obj) and bool(_latest_artifact(
            db, project_id, f"scene_{_scene_obj.id}_view_{_vref}"))
        _label = {"reverse": "反打视角", "interaction": "交互区特写"}.get(_vref, _vref)
        checks.append(_check(
            CHECK_VIEW_REF, _have,
            f"补充视角已就绪（{_label}）" if _have else
            f"本镜声明要用**{_label}**，但该场景还没有这张图——生成时会**静默不挂**"
            "（模型只能凭空编另一侧的空间）。请到「资产 → 场景」里生成补充视角",
            advisory=True))

    # 6. 端点健康（25 号 1.5）：欠费/队列满**在生成前**告知——等提交时才收到
    #    403/503，等于白排一轮队（批量提交还会同一原因连错 N 次）
    checks.append(provider_health_check(db, project_id, KIND_T2V))

    # ready 只看阻断项（advisory 未满足不拦提交，仅提示）
    ready = all(c["ok"] for c in checks if not c.get("advisory"))
    return {"ready": ready, "checks": checks,
            "info_status": shot_info_status(db, project_id, shot)}


def episode_video_readiness(db: Session, project_id: str,
                            episode: Episode, shots: list[Shot],
                            progress_cb: Callable[[int, int], None] | None = None
                            ) -> dict:
    """整集视频就绪度汇总：逐镜检查 + 汇总计数（前端向导/批量提交前用）。"""
    per_shot: list[dict] = []
    for i, shot in enumerate(shots):
        result = shot_video_readiness(db, project_id, shot, episode)
        per_shot.append({"shot_id": shot.id, "seq": shot.seq, **result})
        if progress_cb is not None:
            progress_cb(i + 1, len(shots))
    blocked = [s for s in per_shot if not s["ready"]]
    return {
        "ready": not blocked,
        "shots": len(per_shot),
        "blocked": len(blocked),
        "blocked_shots": [{"seq": s["seq"],
                           "reasons": [c["message"] for c in s["checks"]
                                       if not c["ok"] and not c.get("advisory")]}
                          for s in blocked],
        "detail": per_shot,
    }
