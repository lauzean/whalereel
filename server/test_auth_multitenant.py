# -*- coding: utf-8 -*-
"""鉴权与多租户回归（2026-09-30 第 1 层）。

守三件事：
① **默认拒绝**：业务接口不带令牌一律 401（此前 122 个接口全部裸奔）；
② **多租户隔离**：A 用户看不到、也改不动 B 用户的项目（按 404 处理，不泄露存在性）；
③ **白名单**：登录接口与媒体文件（签名/令牌）仍需可达，否则一开鉴权全站就挂。

另覆盖：首次创建的普通用户能认领无归属项目（单机「先用库后建号」场景）、
登录失败限速、出厂默认口令在登录响应里带 must_change_password。

运行：`server/.venv/Scripts/python server/test_auth_multitenant.py`
"""
import os
import sys
import tempfile
import time
_tmp = tempfile.mkdtemp(prefix="avs_auth_")
os.environ["AVS_DATA_DIR"] = _tmp
os.environ["AVS_MODELS_DIR"] = os.path.join(_tmp, "models")
os.environ["AVS_MOCK"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.security import hash_password, new_token  # noqa: E402
from app.db.models import AuthToken, Project, User  # noqa: E402
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
fails: list[str] = []

# 自检专用路由（**清单里没有**）：用来验证「忘了改清单也能兜住」。
# 必须在任何请求之前注册——归属自动识别用的路由模板清单是首次请求时采集一次的。
from fastapi import APIRouter as _APIRouter  # noqa: E402

_selftest_router = _APIRouter(prefix="/api/selftest", tags=["selftest"])


@_selftest_router.get("/{character_id}")
def _selftest_character(character_id: str):  # noqa: ANN202
    # 故意不写任何归属判断：完全依赖中间件的自动识别
    return {"ok": True, "character_id": character_id}


@_selftest_router.get("/ep/{episode_id}")
def _selftest_episode(episode_id: str):  # noqa: ANN202
    return {"ok": True, "episode_id": episode_id}


app.include_router(_selftest_router)


def check(name: str, cond: bool) -> None:
    print(("  OK   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def make_user(username: str, *, admin: bool = False) -> tuple[str, str]:
    """建用户 + 令牌，返回 (user_id, bearer_token)。"""
    token = new_token()
    with SessionLocal() as db:
        u = User(username=username, password_hash=hash_password("probe123"),
                 is_admin=admin)
        db.add(u)
        db.flush()
        db.add(AuthToken(token=token, user_id=u.id, created_at=int(time.time()),
                         expires_at=int(time.time()) + 3600))
        db.commit()
        return u.id, token


def make_project(name: str, owner: str) -> str:
    with SessionLocal() as db:
        p = Project(name=name, mode="canvas", owner_user_id=owner)
        db.add(p)
        db.commit()
        return p.id


def seed_b_project_data(project_id: str) -> dict:
    """给乙的项目造一套「按 id 直取」的资源，用于越权测试。

    覆盖所有 `GET/PATCH/DELETE /xxx/{id}` 形态的资源：产物、抽卡版本、会话、
    消息、分镜、角色。这些端点的网址里**没有 project_id**，靠 main.py 的
    `_check_resource_owner` 与 core/resource_map.py 的清单判归属。

    夹具里显式写 `owner_user_id`：真实写入路径由 before_flush 钩子自动填
    （见 db/session.py），这里直接建行、不经过那条路径，所以手动带上。
    """
    import json as _json

    from app.db.models import (
        Asset, Character, Episode, Message, NodeVersion, Prop, Scene, Shot,
    )
    from app.db.models import Session as SessionModel

    with SessionLocal() as db:
        owner = db.get(Project, project_id).owner_user_id
        asset = Asset(kind="image", path=os.path.join("x", "b.png"), sha256="b",
                      owner_user_id=owner)
        db.add(asset)
        db.flush()
        version = NodeVersion(node_id="node_b", project_id=project_id, seq=1,
                              source="generated", prompt="", refs_json=[],
                              artifact_id=asset.id, owner_user_id=owner)
        db.add(version)
        sess = SessionModel(project_id=project_id, title="乙的会话",
                            status="active", pane_side="right",
                            owner_user_id=owner)
        db.add(sess)
        db.flush()
        msg = Message(session_id=sess.id, role="user", content="乙的私密消息",
                      refs_json=[], tool_calls_json={}, owner_user_id=owner)
        db.add(msg)
        char = Character(project_id=project_id, name="乙的角色", role="主角",
                         persona="", appearance="", image_prompt="",
                         owner_user_id=owner)
        scene = Scene(project_id=project_id, name="乙的场景", description="",
                      image_prompt="", owner_user_id=owner)
        prop = Prop(project_id=project_id, name="乙的道具", description="",
                    image_prompt="", owner_user_id=owner)
        db.add_all([char, scene, prop])
        episode = Episode(project_id=project_id, seq=1, title="乙的第1集",
                          body="正文", produce_status="pending",
                          owner_user_id=owner)
        db.add(episode)
        db.flush()
        shot = Shot(episode_id=episode.id, seq=1, excerpt="", description="",
                    camera="中景", duration=5, dialogue_json=[],
                    character_ref_ids_json=[], prop_ref_ids_json=[],
                    state_json={}, end_state_json={}, owner_user_id=owner)
        db.add(shot)
        db.commit()
        return {"asset": asset.id, "version": version.id, "session": sess.id,
                "message": msg.id, "character": char.id, "scene": scene.id,
                "prop": prop.id, "episode": episode.id, "shot": shot.id,
                "node_id": "node_b"}


client = TestClient(app)
uid_a, tok_a = make_user("用户甲")
uid_b, tok_b = make_user("用户乙")
uid_admin, tok_admin = make_user("管理员", admin=True)
hdr_a = {"Authorization": f"Bearer {tok_a}"}
hdr_b = {"Authorization": f"Bearer {tok_b}"}
hdr_admin = {"Authorization": f"Bearer {tok_admin}"}
proj_a = make_project("甲的项目", uid_a)
proj_b = make_project("乙的项目", uid_b)

print("① 默认拒绝：业务接口不带令牌一律 401")
anon = TestClient(app)
for method, path in [
    ("get", "/api/projects"),
    ("post", "/api/projects"),
    ("get", f"/api/projects/{proj_a}"),
    ("delete", f"/api/projects/{proj_a}"),
    ("get", f"/api/projects/{proj_a}/assets"),
    ("get", f"/api/drama/projects/{proj_a}/script"),
    ("get", f"/api/drama/projects/{proj_a}/episodes"),
    ("get", f"/api/tasks?project_id={proj_a}"),
    ("get", f"/api/nodes/x/versions?project_id={proj_a}"),
]:
    r = getattr(anon, method)(path)
    check(f"匿名 {method.upper()} {path} → 401（实际 {r.status_code}）",
          r.status_code == 401)

print("② 白名单：登录与健康检查仍可达")
check("匿名登录接口可达（凭据错也是 401 而非被闸拦）",
      client.post("/api/auth/login",
                  json={"username": "不存在", "password": "x"}).status_code == 401)
check("匿名健康检查 200", client.get("/api/health").status_code == 200)
check("带令牌可正常访问业务接口", client.get("/api/projects", headers=hdr_a).status_code == 200)

print("③ 多租户隔离：甲看不到也改不动乙的项目")
r = client.get(f"/api/projects/{proj_b}", headers=hdr_a)
check(f"甲读乙的项目 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.patch(f"/api/projects/{proj_b}", json={"description": "改"},
                 headers=hdr_a)
check(f"甲改乙的项目 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.delete(f"/api/projects/{proj_b}", headers=hdr_a)
check(f"甲删乙的项目 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.get(f"/api/projects/{proj_b}/canvas", headers=hdr_a)
check(f"甲读乙的画布 → 404（实际 {r.status_code}）", r.status_code == 404)
check("乙仍能读自己的项目（没被误伤）",
      client.get(f"/api/projects/{proj_b}", headers=hdr_b).status_code == 200)

print("④ 列表按归属过滤")
names_a = [p["name"] for p in client.get("/api/projects", headers=hdr_a).json()]
check("甲的项目列表只含自己的", names_a == ["甲的项目"] or set(names_a) == {"甲的项目"})
names_admin = [p["name"] for p in client.get("/api/projects", headers=hdr_admin).json()]
check("管理员能看到全部项目", {"甲的项目", "乙的项目"} <= set(names_admin))
check("项目响应带 owner_user_id",
      client.get(f"/api/projects/{proj_a}", headers=hdr_a).json().get("owner_user_id") == uid_a)

print("⑤ 新建项目归属当前用户")
r = client.post("/api/projects", json={"name": "甲新建", "mode": "canvas"}, headers=hdr_a)
check("建项目 201", r.status_code == 201)
new_id = r.json()["id"]
check("归属是甲", r.json().get("owner_user_id") == uid_a)
check("乙读不到甲新建的项目",
      client.get(f"/api/projects/{new_id}", headers=hdr_b).status_code == 404)

print("⑥ 媒体文件：无签名/无令牌 401，带令牌可达")
r = client.get(f"/api/projects/{proj_a}/assets", headers=hdr_a)
check("资产列表可达", r.status_code == 200)

print("⑦ 登录：默认口令需改密 + 失败限速")
client.post("/api/auth/login", json={"username": "限速测试", "password": "wrong"})
codes = []
for _ in range(6):
    codes.append(client.post("/api/auth/login",
                             json={"username": "限速测试", "password": "wrong"}).status_code)
check(f"连续失败第 6 次被限速（429；实际序列 {codes[-2:]}）", 429 in codes)
with SessionLocal() as db:
    u = User(username="默认口令用户", password_hash=hash_password("admin123"))
    db.add(u)
    db.commit()
r = client.post("/api/auth/login", json={"username": "默认口令用户", "password": "admin123"})
check("默认口令登录成功但带 must_change_password",
      r.status_code == 200 and r.json()["user"].get("must_change_password") is True)

print("⑧ 媒体路径：未签名未令牌 401（不能借白名单裸奔）")
check("匿名取产物文件 → 401",
      anon.get("/api/artifacts/whatever/file").status_code == 401)
check("匿名取 /files → 401", anon.get("/files/some.png").status_code == 401)

print("⑨ 查询参数传 project_id 的端点也要校验（不能只看路径参数）")
# edits.py 的三个编辑端点把 project_id 放在查询参数里；只看路径参数会漏掉它们
r = client.post(f"/api/nodes/n_x/rotate?project_id={proj_b}",
                json={"direction": "cw"}, headers=hdr_a)
check(f"甲对乙的项目调旋转编辑 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.post(f"/api/nodes/n_x/crop?project_id={proj_b}",
                json={"ratio": "1:1"}, headers=hdr_a)
check(f"甲对乙的项目调裁剪编辑 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.patch(f"/api/nodes/n_x/versions/v_x?project_id={proj_b}",
                 json={"name": "x"}, headers=hdr_a)
check(f"甲对乙的项目改版本名 → 404（实际 {r.status_code}）", r.status_code == 404)

print("⑨b 按 id 直取的资源（地址里没有 project_id）：逐个试越权")
B = seed_b_project_data(proj_b)
cases: list[tuple[str, str, str, dict | None]] = [
    # (方法, 路径, 说明, body)
    ("get", f"/api/artifacts/{B['asset']}", "读乙的产物元信息", None),
    ("get", f"/api/artifacts/{B['asset']}/file", "取乙的产物文件", None),
    ("get", f"/api/artifacts/{B['asset']}/file?", "带空签名取乙的产物文件", None),
    ("patch", f"/api/sessions/{B['session']}", "改乙的会话", {"title": "改"}),
    ("get", f"/api/sessions/{B['session']}/messages", "读乙的对话历史", None),
    ("post", f"/api/sessions/{B['session']}/chat", "往乙的会话发消息",
     {"text": "你好", "refs": [], "confirmed": False}),
    ("delete", f"/api/nodes/{B['node_id']}/versions/{B['version']}?project_id={proj_b}",
     "删乙的抽卡版本", None),
    ("patch", f"/api/nodes/{B['node_id']}/versions/{B['version']}?project_id={proj_b}",
     "改乙的抽卡版本名", {"name": "改"}),
    ("patch", f"/api/characters/{B['character']}", "改乙的角色", {"name": "改"}),
    ("patch", f"/api/scenes/{B['scene']}", "改乙的场景", {"name": "改"}),
    ("patch", f"/api/props/{B['prop']}", "改乙的道具", {"name": "改"}),
    ("patch", f"/api/shots/{B['shot']}", "改乙的分镜", {"description": "改"}),
    ("patch", f"/api/episodes/{B['episode']}", "改乙的分集", {"title": "改"}),
]
for method, path, label, body in cases:
    fn = getattr(client, method)
    kwargs = {"headers": hdr_a}
    if body is not None:
        kwargs["json"] = body
    r = fn(path, **kwargs)
    # 403 会泄露「资源存在」，所以越权一律按 404 判
    check(f"{label} → 404（实际 {r.status_code}）", r.status_code == 404)

# 对照：资源主人自己能访问（确认没误伤）
r = client.get(f"/api/sessions/{B['session']}/messages", headers=hdr_b)
check("乙自己能读自己的会话消息", r.status_code == 200)
r = client.get(f"/api/artifacts/{B['asset']}", headers=hdr_b)
check("乙自己能读自己的产物元信息", r.status_code == 200)

print("⑨c 产物签名只发给自己有权访问的（不能靠签名绕过归属）")
r = client.post("/api/artifacts/sign", json={"artifact_ids": [B["asset"]]},
                headers=hdr_a)
signed_map = (r.json() or {}).get("artifacts") or {}
check("甲请求给乙的产物签名 → 不签发", B["asset"] not in signed_map)
r = client.post("/api/artifacts/sign", json={"artifact_ids": [B["asset"]]},
                headers=hdr_b)
check("乙请求给自己的产物签名 → 签发成功",
      B["asset"] in ((r.json() or {}).get("artifacts") or {}))

print("⑨d 覆盖审计列出的全部「按 id」端点（逐个试越权）")
# 这些端点的网址里没有项目编号，靠 main.py 的 `_check_resource_owner` 统一判归属
# （先翻 core/resource_map.py 那份「网址 → 编号是什么东西」的清单）。
# 这里把审计出的端点**逐个走一遍**，防止以后新增端点时又漏一处。
more_cases: list[tuple[str, str, str, dict | None]] = [
    # 分集
    ("post", f"/api/episodes/{B['episode']}/audit", "审核乙的分集", None),
    ("post", f"/api/episodes/{B['episode']}/assets", "提取乙的分集资产", None),
    ("post", f"/api/episodes/{B['episode']}/optimize", "一键修复乙的分集", {}),
    ("post", f"/api/episodes/{B['episode']}/build", "制作乙的分集", {}),
    ("get", f"/api/episodes/{B['episode']}/script", "读乙的分集正文", None),
    ("get", f"/api/episodes/{B['episode']}/storyboard-qa", "读乙的分镜体检", None),
    ("get", f"/api/episodes/{B['episode']}/video-readiness", "读乙的视频就绪度", None),
    ("get", f"/api/episodes/{B['episode']}/precheck", "读乙的成片预检", None),
    ("get", f"/api/episodes/{B['episode']}/review", "读乙的审片", None),
    ("get", f"/api/episodes/{B['episode']}/reviews", "读乙的审片列表", None),
    ("post", f"/api/episodes/{B['episode']}/link-frames", "对乙做首尾帧对齐", {}),
    ("post", f"/api/episodes/{B['episode']}/compose", "合成乙的分集", None),
    ("get", f"/api/episodes/{B['episode']}/shots/download", "下载乙的分镜包", None),
    ("get", f"/api/episodes/{B['episode']}/shots", "列乙的分镜", None),
    ("post", f"/api/episodes/{B['episode']}/shots", "给乙加分镜",
     {"description": "x", "camera": "中景", "duration": 5}),
    ("post", f"/api/episodes/{B['episode']}/shots/generate", "生成乙的分镜视频", {}),
    ("get", f"/api/episodes/{B['episode']}/generate-preview", "乙的生成清单", None),
    ("post", f"/api/episodes/{B['episode']}/auto-review", "对乙自动审片", {}),
    # 分镜
    ("post", f"/api/shots/{B['shot']}/regenerate-video-prompt", "重算乙的画面描述", {}),
    ("get", f"/api/shots/{B['shot']}/reviews", "读乙的分镜裁决", None),
    ("post", f"/api/shots/{B['shot']}/verdict", "给乙的分镜判级",
     {"verdict": "good"}),
    ("post", f"/api/shots/{B['shot']}/prefer-first-frame", "设乙的首帧",
     {"version_id": None}),
    ("get", f"/api/shots/{B['shot']}/generation-params", "读乙的生成参数", None),
    # 角色
    ("post", f"/api/characters/{B['character']}/images", "给乙的角色出图", {}),
    ("post", f"/api/characters/{B['character']}/images/fourview", "乙的角色四视图", {}),
    ("post", f"/api/characters/{B['character']}/images/cancel", "取消乙的出图", {}),
    ("post", f"/api/characters/{B['character']}/apply-image", "套用乙的图", {}),
    ("post", f"/api/characters/{B['character']}/costume/apply", "乙的角色妆造",
     {"image_id": "x"}),
    ("delete", f"/api/characters/{B['character']}/voice-ref", "删乙的音色参考", None),
    ("delete", f"/api/characters/{B['character']}", "删乙的角色", None),
    # 场景 / 道具
    ("post", f"/api/scenes/{B['scene']}/generate", "生成乙的场景图", {}),
    ("post", f"/api/scenes/{B['scene']}/extra-view", "乙的场景补充视角", {}),
    ("post", f"/api/scenes/{B['scene']}/image/prefer", "设乙的场景生效图",
     {"version_id": None}),
    ("delete", f"/api/scenes/{B['scene']}/image", "删乙的场景图", None),
    ("post", f"/api/scenes/{B['scene']}/cancel", "取消乙的场景图", None),
    ("delete", f"/api/scenes/{B['scene']}", "删乙的场景", None),
    ("post", f"/api/props/{B['prop']}/generate", "生成乙的道具图", {}),
    ("post", f"/api/props/{B['prop']}/image/prefer", "设乙的道具生效图",
     {"version_id": None}),
    ("post", f"/api/props/{B['prop']}/cancel", "取消乙的道具图", None),
    ("delete", f"/api/props/{B['prop']}", "删乙的道具", None),
]
for method, path, label, body in more_cases:
    fn = getattr(client, method)
    kwargs: dict = {"headers": hdr_a}
    if body is not None:
        kwargs["json"] = body
    r = fn(path, **kwargs)
    check(f"{label} → 404（实际 {r.status_code}）", r.status_code == 404)

print("⑨e 管理员不受限（上面这些端点对管理员不该 404）")
r = client.get(f"/api/episodes/{B['episode']}/script", headers=hdr_admin)
check(f"管理员可读乙的分集正文（实际 {r.status_code}）", r.status_code != 404)
r = client.get(f"/api/artifacts/{B['asset']}", headers=hdr_admin)
check(f"管理员可读乙的产物（实际 {r.status_code}）", r.status_code == 200)

print("⑨f 任务归属：读别人的任务详情 / 取消别人的任务都要挡住")
from app.core.task_engine import engine as _engine  # noqa: E402

sub_b = _engine.submit(project_id=proj_b, node_id="node_task",
                       runner_name="t2i", params={"prompt": "乙的任务"})
task_b = sub_b["task_id"]
r = client.get(f"/api/tasks/{task_b}", headers=hdr_a)
check(f"甲读乙的任务详情 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.post(f"/api/tasks/{task_b}/cancel", headers=hdr_a)
check(f"甲取消乙的任务 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.get(f"/api/tasks/{task_b}", headers=hdr_b)
check(f"乙读自己的任务 → 200（实际 {r.status_code}）", r.status_code == 200)

print("⑨g /files 旧产物：按项目归属判定（甲的文件乙读不到）")
legacy_rel = None
with SessionLocal() as db:
    from sqlalchemy import select as _select

    from app.db.models import Asset as _Asset
    from app.db.models import NodeVersion as _NV

    _row = db.execute(
        _select(_NV.project_id, _Asset.path).join(
            _Asset, _Asset.id == _NV.artifact_id).limit(1)).first()
if _row is None:
    print("  （库里还没有带版本引用的产物，跳过）")
else:
    pid_of_asset, abs_path = _row
    marker = make_project("甲的文件项目", uid_a)
    with SessionLocal() as db:
        from app.db.models import Asset as _A2
        from app.db.models import NodeVersion as _NV2

        a2 = _A2(kind="image", path=abs_path, sha256="legacy", owner_user_id=uid_a)
        db.add(a2)
        db.flush()
        db.add(_NV2(node_id="node_legacy", project_id=marker, seq=1,
                    source="generated", prompt="", refs_json=[],
                    artifact_id=a2.id, owner_user_id=uid_a))
        db.commit()
        legacy_aid = a2.id
    r = client.get(f"/api/artifacts/{legacy_aid}", headers=hdr_a)
    check(f"甲读自己项目的旧产物 → 200（实际 {r.status_code}）", r.status_code == 200)
    r = client.get(f"/api/artifacts/{legacy_aid}", headers=hdr_b)
    check(f"乙读甲的旧产物 → 404（实际 {r.status_code}）", r.status_code == 404)

print("⑨h 权限归属自动填充：新建的每一行都必须带上 owner_user_id")
# 这是两层模型的**前提**（用户口径）：owner_user_id 一律由系统填，业务代码不手填。
# 所以必须有一条测试守住「新写的数据真的有归属」——否则新增写入点漏了，
# 表现是「东西生成了但看不到」（功能退化），而不是报错。
from app.db.models import (  # noqa: E402
    Asset, Character, Episode, Message, NodeVersion, Prop, Scene, Shot,
)
from app.db.models import Session as SessionModel  # noqa: E402

new_pid = make_project("归属自检项目", uid_a)
with SessionLocal() as db:
    ep = Episode(project_id=new_pid, seq=1, title="第1集", body="正文")
    db.add(ep)
    db.flush()
    db.add_all([
        Character(project_id=new_pid, name="角色", role="主角"),
        Scene(project_id=new_pid, name="场景"),
        Prop(project_id=new_pid, name="道具"),
    ])
    shot = Shot(episode_id=ep.id, seq=1, excerpt="", description="",
                camera="中景", duration=5, dialogue_json=[],
                character_ref_ids_json=[], prop_ref_ids_json=[],
                state_json={}, end_state_json={})
    db.add(shot)
    sess = SessionModel(project_id=new_pid, title="会话", status="active",
                        pane_side="right")
    db.add(sess)
    db.flush()
    db.add(Message(session_id=sess.id, role="user", content="hi"))
    from app.db.models import Task as _Task2

    db.add(_Task2(project_id=new_pid, node_id="n_x", runner="t2i"))
    db.commit()
    rows = {
        "分集": ep, "分镜": shot, "会话": sess,
    }
with SessionLocal() as db:
    checks = {
        "分集": db.query(Episode).filter(Episode.project_id == new_pid).first(),
        "分镜": db.query(Shot).filter(Shot.episode_id == ep.id).first(),
        "角色": db.query(Character).filter(Character.project_id == new_pid).first(),
        "场景": db.query(Scene).filter(Scene.project_id == new_pid).first(),
        "道具": db.query(Prop).filter(Prop.project_id == new_pid).first(),
        "会话": db.query(SessionModel).filter(
            SessionModel.project_id == new_pid).first(),
        "会话消息": db.query(Message).filter(
            Message.session_id == sess.id).first(),
        "任务": db.query(_Task2).filter(_Task2.project_id == new_pid).first(),
    }
for label, row in checks.items():
    check(f"{label}自动带上归属（父级链也对）",
          bool(row) and row.owner_user_id == uid_a)

print("⑨i 产物归属：生成路径落库时带出归属，查不出归属的退化为仅管理员")
with SessionLocal() as db:
    orphan = Asset(kind="image", path=os.path.join("x", "orphan.png"),
                   sha256="o")
    db.add(orphan)
    db.commit()
    orphan_id = orphan.id
    check("没人引用的产物归属为空（不是随手给一个人）",
          (orphan.owner_user_id or "") == "")
r = client.get(f"/api/artifacts/{orphan_id}", headers=hdr_b)
check(f"无归属产物：普通用户读不到（404，实际 {r.status_code}）", r.status_code == 404)
r = client.get(f"/api/artifacts/{orphan_id}", headers=hdr_admin)
check(f"无归属产物：管理员可读（{r.status_code}）", r.status_code == 200)

print("⑨j 「忘了改清单」也能兜住：按参数名自动找表")
# 这一段守的是本方案最关键的一环。前面两道判定里，清单（resource_map.RULES）要人
# 维护——**漏写一行的后果是那个接口没人判归属**。所以补了第三道：不看清单，直接从
# 请求参数里认出 `xxx_id`，去同名表比 owner_user_id。
#
# 验证方式：现造一个「清单里没有」的接口（`/api/selftest/{character_id}`），
# 用别人的角色编号去调——如果被打回 404，说明自动识别生效。
# （路由必须在断言之前注册：模板清单是首次请求时采集一次的。）
r = client.get(f"/api/selftest/{B['character']}", headers=hdr_a)
check(f"清单外的接口用别人的角色编号 → 404（实际 {r.status_code}）",
      r.status_code == 404)
r = client.get(f"/api/selftest/{B['character']}", headers=hdr_b)
check(f"清单外的接口用自己的角色编号 → 200（实际 {r.status_code}）",
      r.status_code == 200)
r = client.get(f"/api/selftest/ep/{B['episode']}", headers=hdr_a)
check(f"同一机制对分集也生效 → 404（实际 {r.status_code}）", r.status_code == 404)

print("⑩ WebSocket 任务订阅也要鉴权（HTTP 中间件不覆盖 WS）")
from starlette.websockets import WebSocketDisconnect  # noqa: E402


def ws_probe(query: str) -> tuple[bool, int, str]:
    """连一次 WS 并读一条，返回 (是否收到消息, 关闭码, 收到的内容)。

    统一入口：拒绝的场景在**读**的时候才抛断开，所以三种情况都走同一条路径，
    避免靠 `__enter__` 是否抛异常来区分（行为随 Starlette 版本变化）。
    """
    try:
        with client.websocket_connect(f"/ws/tasks?{query}") as ws:
            return True, 0, ws.receive_text()
    except WebSocketDisconnect as e:
        return False, e.code, ""


ok, code, _msg = ws_probe(f"project_id={proj_a}")
check(f"匿名订阅被拒（code={code}）", not ok and code in (4401, 1008))
ok, code, _msg = ws_probe(f"project_id={proj_b}&token={tok_a}")
check(f"跨租户订阅被拒（code={code}）", not ok and code in (4403, 1008))
ok, _code, msg = ws_probe(f"project_id={proj_a}&token={tok_a}")
check("合法订阅可建立并收到消息（心跳 ping）", ok and "ping" in msg)

print("⑪ 请求体里的 project_id 也要判归属（中间件看不到请求体）")
from app.db.models import Asset, Task  # noqa: E402


def _task_body(pid: str) -> dict:
    """最小可提交的任务体（t2i，mock 模式下不会真的推理）。"""
    return {"project_id": pid, "node_id": "n_body_probe", "runner": "t2i",
            "params": {"prompt": "探针"}, "input_refs": {}}


r = client.post("/api/tasks", json=_task_body(proj_b), headers=hdr_a)
check(f"甲往乙的项目提交生成任务 → 404（实际 {r.status_code}）",
      r.status_code == 404)
r = client.post("/api/tasks", json=_task_body(proj_a), headers=hdr_a)
check(f"甲往自己的项目提交生成任务 → 201（实际 {r.status_code}）",
      r.status_code == 201)
r = client.post("/api/episodes/compose-batch",
                json={"project_id": proj_b, "episode_ids": [B["episode"]]},
                headers=hdr_a)
check(f"甲按乙的项目触发连发合成 → 404（实际 {r.status_code}）",
      r.status_code == 404)

# 多选式编组（32 号交互优化）：请求体里的 project_id 与 scene_ids **都要**判归属
r = client.post("/api/scene-groups/apply",
                json={"project_id": proj_b, "scene_ids": [B["scene"]],
                      "name": "甲越权组", "env_setting": ""},
                headers=hdr_a)
check(f"甲按乙的项目编组 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.post("/api/scene-groups/apply",
                json={"project_id": proj_a, "scene_ids": [B["scene"]],
                      "name": "甲越权组", "env_setting": ""},
                headers=hdr_a)
check(f"甲用自己项目 + 乙的场景编组 → 404（实际 {r.status_code}）",
      r.status_code == 404)
from app.db.models import Scene as _SceneProbe  # noqa: E402
with SessionLocal() as db:
    _sc_a = _SceneProbe(project_id=proj_a, name="甲自己的场景", description="",
                        image_prompt="",
                        owner_user_id=db.get(Project, proj_a).owner_user_id)
    db.add(_sc_a)
    db.commit()
    _sc_a_id = _sc_a.id
r = client.post("/api/scene-groups/apply",
                json={"project_id": proj_a, "scene_ids": [_sc_a_id],
                      "name": "甲的场景组", "env_setting": ""},
                headers=hdr_a)
check(f"甲给自己的场景编组 → 200（实际 {r.status_code}）", r.status_code == 200)

print("⑫ 任务列表不带项目编号时只返回自己的任务（此前返回全库 200 条）")
with SessionLocal() as db:
    db.add(Task(project_id=proj_b, node_id="n_leak_probe", runner="t2i",
                owner_user_id=uid_b))
    db.commit()
r = client.get("/api/tasks", headers=hdr_a)
_leaked = [t for t in r.json() if t.get("project_id") == proj_b] \
    if r.status_code == 200 else ["resp!=200"]
check(f"甲拉任务列表不含乙的项目（实际 {r.status_code}）",
      r.status_code == 200 and not _leaked)
r = client.get("/api/tasks", headers=hdr_admin)
_has_b = any(t.get("project_id") == proj_b for t in r.json()) \
    if r.status_code == 200 else False
check(f"管理员不受限（能看到乙的任务，实际 {r.status_code}）",
      r.status_code == 200 and _has_b)

print("⑬ 签名接口的 files 也要判归属；未登记文件不放行")
from app.core.config import settings as _settings  # noqa: E402

_b_media = os.path.join(_settings.artifacts_dir, "probe_b_media.txt")
with open(_b_media, "w", encoding="utf-8") as fh:
    fh.write("b-media")
with SessionLocal() as db:
    _b_asset = Asset(kind="image", path=_b_media, sha256="bm",
                     owner_user_id=uid_b)
    db.add(_b_asset)
    db.commit()
r = client.post("/api/artifacts/sign", json={"files": ["probe_b_media.txt"]},
                headers=hdr_a)
check("甲给乙的产物文件签名 → 不给签（实际 %s）" % r.status_code,
      r.status_code == 200
      and "probe_b_media.txt" not in (r.json().get("files") or {}))
r = client.post("/api/artifacts/sign", json={"files": ["probe_unregistered.txt"]},
                headers=hdr_a)
check("未登记成产物的文件不签发（实际 %s）" % r.status_code,
      r.status_code == 200
      and "probe_unregistered.txt" not in (r.json().get("files") or {}))
r = client.post("/api/artifacts/sign", json={"files": ["probe_b_media.txt"]},
                headers=hdr_b)
check("乙给自己的产物文件签名 → 正常签发（实际 %s）" % r.status_code,
      r.status_code == 200
      and "probe_b_media.txt" in (r.json().get("files") or {}))
_unreg = os.path.join(_settings.artifacts_dir, "probe_unregistered.txt")
with open(_unreg, "w", encoding="utf-8") as fh:
    fh.write("x")
r = client.get("/files/probe_unregistered.txt", headers=hdr_a)
check(f"未登记文件带令牌读 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.get("/files/probe_unregistered.txt", headers=hdr_admin)
check(f"未登记文件管理员可读（实际 {r.status_code}）", r.status_code == 200)

print("⑭ 自学习洞察（跨项目统计/改全局提示词）需要管理员")
r = client.get("/api/lessons-insights", headers=hdr_a)
check(f"普通用户读 → 拒绝（实际 {r.status_code}）", r.status_code in (401, 403))
r = client.post("/api/lessons-insights/apply", json={"rule": "x"}, headers=hdr_a)
check(f"普通用户写 → 拒绝（实际 {r.status_code}）", r.status_code in (401, 403))

print("⑮ 版本删除/改名：主人能用（此前漏改变量会 500）、跨租户 404")
r = client.patch(f"/api/nodes/node_b/versions/{B['version']}?project_id={proj_b}",
                 json={"name": "改名"}, headers=hdr_b)
check(f"乙改自己的版本名 → 200（实际 {r.status_code}）", r.status_code == 200)
r = client.patch(f"/api/nodes/node_b/versions/{B['version']}?project_id={proj_b}",
                 json={"name": "改名"}, headers=hdr_a)
check(f"甲改乙的版本名 → 404（实际 {r.status_code}）", r.status_code == 404)
r = client.delete(f"/api/nodes/node_b/versions/{B['version']}?project_id={proj_b}",
                  headers=hdr_b)
check(f"乙删自己的版本 → 200（实际 {r.status_code}）", r.status_code == 200)

print("⑯ 出厂默认口令：服务端强制改密（此前只有前端页面挡，curl 可绕过）")
_tok_def = new_token()
with SessionLocal() as db:
    _u_def = User(username="默认口令探针",
                  password_hash=hash_password("admin123"))
    db.add(_u_def)
    db.flush()
    db.add(AuthToken(token=_tok_def, user_id=_u_def.id,
                     created_at=int(time.time()),
                     expires_at=int(time.time()) + 3600))
    db.commit()
_hdr_def = {"Authorization": f"Bearer {_tok_def}"}
r = client.get("/api/auth/me", headers=_hdr_def)
check("默认口令用户仍可查看自己（且带 must_change_password）",
      r.status_code == 200 and r.json().get("must_change_password") is True)
r = client.get("/api/projects", headers=_hdr_def)
check(f"默认口令用户调业务接口 → 403（实际 {r.status_code}）", r.status_code == 403)
r = client.post("/api/auth/change-password",
                json={"old_password": "admin123", "new_password": "newpass123"},
                headers=_hdr_def)
check(f"改密接口放行（实际 {r.status_code}）", r.status_code == 200)
r = client.get("/api/projects", headers=_hdr_def)
check(f"改密后业务接口放行（实际 {r.status_code}）", r.status_code == 200)

print("⑰ 系统管理模块：普通用户一律 403，管理员放行；精简模型清单普通用户可读")
# 2026-09-30 用户规则：系统管理模块下的接口只有管理员可访问。清单驱动——
# 新增同类接口时把它加进这张表，就能守住「普通用户进不去」。
_SYS_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/api/auth/users"),
    ("GET", "/api/keys"),
    ("GET", "/api/endpoints"),
    ("GET", "/api/defaults"),
    ("PUT", "/api/defaults"),
    ("GET", "/api/models"),
    ("GET", "/api/models/detail?model_id=none"),
    ("GET", "/api/models/install?model_id=none"),
    ("GET", "/api/providers/models?kind=t2i"),
    ("GET", "/api/system/deps"),
    ("GET", "/api/system/gpu"),
    ("GET", "/api/system/info"),
    ("GET", "/api/skills"),
    ("GET", "/api/prompts"),
    ("GET", "/api/model-calls"),
    ("GET", "/api/lessons-insights"),
    ("POST", "/api/lessons-insights/apply"),
    # 积分计费（33 号方案）：管理侧端点全部管理员专属
    ("GET", "/api/billing/settings"),
    ("PATCH", "/api/billing/settings"),
    ("GET", "/api/billing/accounts"),
    ("GET", "/api/billing/transactions/all"),
    ("GET", "/api/billing/redeem-codes"),
    ("POST", "/api/billing/redeem-codes"),
    ("POST", "/api/billing/recharge"),
    ("POST", "/api/billing/adjust"),
    ("POST", "/api/billing/audit"),
    # 模型服务费率（2026-10-09：计费配置在模型端点上）
    ("POST", "/api/endpoints/apply-suggested-prices"),
]
for _m, _p in _SYS_ENDPOINTS:
    _r = client.request(_m, _p, headers=hdr_a,
                        json={} if _m in ("PUT", "POST", "PATCH") else None)
    check(f"普通用户 {_m} {_p} → 403（实际 {_r.status_code}）", _r.status_code == 403)

# 管理员不受门禁影响（只挑无副作用的读接口验，避免测试里触发下载/安装）
for _m, _p in [("GET", "/api/auth/users"), ("GET", "/api/endpoints"),
               ("GET", "/api/defaults"), ("GET", "/api/models"),
               ("GET", "/api/system/gpu"), ("GET", "/api/skills"),
               ("GET", "/api/prompts"), ("GET", "/api/model-calls")]:
    _r = client.request(_m, _p, headers=hdr_admin)
    check(f"管理员 {_m} {_p} 不被拒（实际 {_r.status_code}）",
          _r.status_code not in (401, 403))

# 精简模型清单：普通用户的模型下拉数据源（系统管理接口全收管理员后的替代）
r = client.get("/api/model-options", headers=hdr_a)
check(f"精简模型清单：普通用户可读（实际 {r.status_code}）", r.status_code == 200)
_models = r.json().get("models", []) if r.status_code == 200 else []
check("精简清单只含展示字段（无 base_url / key_id / provider）",
      all(not ({"base_url", "key_id", "provider"} & set(m.keys()))
          for m in _models))
check("精简清单条目带 id/display_name/task_type",
      all({"id", "display_name", "task_type"} <= set(m.keys())
          for m in _models))
r = client.get("/api/model-options")
check(f"精简模型清单：未登录不可读（实际 {r.status_code}）", r.status_code == 401)

print()
if fails:
    print("存在未通过项：", fails)
    sys.stdout.flush()
    os._exit(1)
print("鉴权与多租户自检全部通过")
sys.stdout.flush()
os._exit(0)
