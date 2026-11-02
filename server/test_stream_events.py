# -*- coding: utf-8 -*-
"""验证流式思考事件全链路：publish_stream → task_trace 事件（begin/delta/end）。"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="avs_stream_")
os.environ["AVS_DATA_DIR"] = _tmp
os.environ["AVS_MODELS_DIR"] = os.path.join(_tmp, "models")
sys.path.insert(0, "server")

from fastapi.testclient import TestClient

import app.runners.script_parse as sp_runner
from app.main import app

FAKE_RESULT = {
    "title": "流式冒烟", "logline": "简介", "video_style": "写实电影感",
    "aspect_ratio": "16:9",
    "characters": [{"name": "甲", "role": "主角", "persona": "", "image_prompt": "x"}],
    "scenes": [{"name": "场地", "description": "", "image_prompt": "x", "episodes": [1]}],
    "episodes": [{"seq": 1, "title": "一", "shots": [
        {"seq": 1, "excerpt": "e", "description": "d", "camera": "中景", "duration": 5,
         "scene_name": "场地", "character_names": ["甲"],
         "dialogue": [{"speaker": "甲", "text": "台词", "os": False}],
         "video_prompt": "p"}]}],
}


def fake_llm_parse(text, progress_cb=None, cancel_check=None, style_hint="",
                   trace_cb=None, stream_cb=None, **_kw):
    if stream_cb is not None:
        stream_cb("begin", "审读剧本（思考中）")
        stream_cb("delta", "思考片段一：")
        stream_cb("delta", "先通读结构…")
        stream_cb("end", "")
    if trace_cb is not None:
        trace_cb("thought", "剧本审读结论", "完整剧本")
        trace_cb("stage", "剧本审读完成")
    return dict(FAKE_RESULT)


sp_runner.llm_parse = fake_llm_parse


def _auth_headers() -> dict[str, str]:
    """造一个用户 + 令牌的请求头（2026-09-30 起业务接口默认拒绝匿名）。"""
    import time

    from app.core.security import hash_password, new_token
    from app.db.models import AuthToken, User
    from app.db.session import SessionLocal, init_db

    init_db()   # 表可能还没建（TestClient 的 startup 晚于本函数）
    token = new_token()
    with SessionLocal() as db:
        u = User(username="流式冒烟用户", password_hash=hash_password("probe123"),
                 is_admin=True)
        db.add(u)
        db.flush()
        db.add(AuthToken(token=token, user_id=u.id, created_at=int(time.time()),
                         expires_at=int(time.time()) + 3600))
        db.commit()
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    events: list[tuple[str, dict]] = []
    # 带令牌的 TestClient：所有请求自动带 Authorization（全局鉴权闸要求）
    with TestClient(app, headers=_auth_headers()) as client:
        pid = client.post("/api/projects", json={"name": "流式冒烟", "mode": "drama",
                                                 "style_preset": "real"}).json()["id"]
        # 捕获 WS 推送（直接挂在引擎 _publish 上）
        import app.core.task_engine as te
        orig_publish = te.TaskEngine._publish
        te.TaskEngine._publish = (  # type: ignore[method-assign]
            lambda self, p, event, data: events.append((event, data))
        )

        task_id = client.post(f"/api/projects/{pid}/script/parse",
                              json={"text": "第一集\n甲：台词。"}).json()["task_id"]
        for _ in range(60):
            task = client.get(f"/api/tasks/{task_id}").json()
            if task["status"] in ("done", "failed"):
                break
            import time
            time.sleep(0.2)
        assert task["status"] == "done", task

        stream_events = [d for ev, d in events if ev == "task_trace"
                         and d.get("kind", "").startswith("stream")]
        kinds = [d["kind"] for d in stream_events]
        print("流式事件 kind 序列:", kinds)
        assert kinds[0] == "stream_begin" and kinds[-1] == "stream_end", kinds
        assert "stream" in kinds
        begin = stream_events[0]
        assert begin["title"] == "审读剧本（思考中）" and begin["node_id"] == "script_parse"
        deltas = [d["text"] for d in stream_events if d["kind"] == "stream"]
        assert deltas == ["思考片段一：", "先通读结构…"], deltas

        # 任务详情接口回读持久化轨迹（thought + stage 均在，stream 不落库）
        trace = task["trace"]
        kinds_persisted = [t["title"] for t in trace["thoughts"]]
        assert any("剧本审读结论" in t for t in kinds_persisted), kinds_persisted
        assert all("思考片段" not in t for t in kinds_persisted), "流式增量不应落库"
        print("[PASS] 流式事件：begin(带标签) → delta ×N → end，前端可流式渲染")
        print("[PASS] 持久化轨迹：thought/stage 落库，stream 增量仅实时推送")

    print("\nSTREAM EVENT PASS")


if __name__ == "__main__":
    main()
