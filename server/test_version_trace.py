# -*- coding: utf-8 -*-
"""版本管理与过程轨迹机制快测（mock llm_parse，秒级；不触真实 LLM）。

覆盖：过程轨迹持久化（仅 LLM 回复结论，流式思考不落库）、当前生效版本号
分配、**版本降级为剧本原文**（v1.1：入档只存原文、恢复=回填编辑器且不动解析
产物与版本号）、恢复后再入档不撞号、解析失败不影响已生效的归档轨迹与版本号。
"""
import os
import sys
import tempfile
import time

_tmp = tempfile.mkdtemp(prefix="avs_ver_")
os.environ["AVS_DATA_DIR"] = _tmp
os.environ["AVS_MODELS_DIR"] = os.path.join(_tmp, "models")
sys.path.insert(0, "server")

from fastapi.testclient import TestClient

import app.runners.script_parse as sp_runner
from app.main import app
from app.script.llm_parser import ScriptParseError

FAKE_RESULT = {
    "title": "测试剧本", "logline": "简介", "video_style": "写实电影感",
    "aspect_ratio": "16:9",
    "characters": [{"name": "甲", "role": "主角", "persona": "", "image_prompt": "x"}],
    "scenes": [{"name": "场地", "description": "", "image_prompt": "x", "episodes": [1]}],
    "episodes": [{"seq": 1, "title": "一", "shots": [
        {"seq": 1, "excerpt": "e", "description": "d", "camera": "中景", "duration": 5,
         "scene_name": "场地", "character_names": ["甲"],
         "dialogue": [{"speaker": "甲", "text": "台词", "os": False}],
         "video_prompt": "p"}]}],
}
calls = {"n": 0}


def fake_llm_parse(text, progress_cb=None, cancel_check=None, style_hint="",
                   trace_cb=None, model_presets=None, stream_cb=None, **_):
    """假解析：固定结果 + 并发结论归档 + 流式思考推送（验证持久化链路）。

    第 3 次调用抛错模拟解析失败（验证失败保留旧数据与旧轨迹/版本号）。
    """
    calls["n"] += 1
    if calls["n"] == 3:
        raise ScriptParseError("模拟解析失败")
    if trace_cb is not None:
        # 模拟三路并发结论归档（对齐真实资产分析/分集拆解的并发路径，
        # 验证 _append_trace 读-改-写锁：无锁会互相覆盖丢条目）
        import concurrent.futures

        def _conclude_one(label: str) -> None:
            trace_cb("thought", f"{label}结论", f"{label}的回复结论内容")
            trace_cb("stage", f"{label}完成", "")

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(_conclude_one, ["甲路", "乙路", "丙路"]))
    if stream_cb is not None:
        # 模拟流式思考推送（不落库，仅验证推送链路不异常）
        stream_cb("begin", "审读剧本（思考中）")
        stream_cb("delta", "思考增量" * 100)
        stream_cb("end", "")
    return dict(FAKE_RESULT)


sp_runner.llm_parse = fake_llm_parse


def wait_task(client, task_id: str) -> dict:
    """轮询任务到终态（超时 60 轮）。"""
    for _ in range(60):
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in ("done", "failed", "canceled"):
            return task
        time.sleep(0.2)
    return task


def _auth_headers() -> dict[str, str]:
    """造一个用户 + 令牌的请求头（2026-09-30 起业务接口默认拒绝匿名）。"""
    import time

    from app.core.security import hash_password, new_token
    from app.db.models import AuthToken, User
    from app.db.session import SessionLocal, init_db

    init_db()   # 表可能还没建（TestClient 的 startup 晚于本函数）
    token = new_token()
    with SessionLocal() as db:
        u = User(username="版本冒烟用户", password_hash=hash_password("probe123"),
                 is_admin=True)
        db.add(u)
        db.flush()
        db.add(AuthToken(token=token, user_id=u.id, created_at=int(time.time()),
                         expires_at=int(time.time()) + 3600))
        db.commit()
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    # 带令牌的 TestClient：所有请求自动带 Authorization（全局鉴权闸要求）
    with TestClient(app, headers=_auth_headers()) as client:
        pid = client.post("/api/projects", json={"name": "版本冒烟", "mode": "drama",
                                                 "style_preset": "real"}).json()["id"]

        def parse(text: str, keep: bool) -> dict:
            tid = client.post(f"/api/projects/{pid}/script/parse",
                              json={"text": text, "keep_version": keep}
                              ).json()["task_id"]
            return wait_task(client, tid)

        # 1. 首次解析（keep=true 但无旧结果 → 不产生版本）
        task = parse("第一集\n甲：台词。", True)
        assert task["status"] == "done", task
        # trace 持久化：并发结论条目完整落库（锁防覆盖），流式思考不落库
        thoughts = task["trace"]["thoughts"]
        concluded = [t for t in thoughts if t["title"].endswith("结论")]
        assert len(concluded) == 3 and {t["title"] for t in concluded} == {
            "甲路结论", "乙路结论", "丙路结论"}, "并发结论归档丢失或未隔离"
        assert all(t["content"] == f"{t['title'][:-2]}的回复结论内容"
                   for t in concluded), "结论内容保存不符"
        assert not any("思考" in t["title"] for t in thoughts), \
            "流式思考原文不应持久化"
        assert task["trace"]["stages"], "阶段轨迹未持久化"
        # 当前生效版本号与归档轨迹
        script = client.get(f"/api/projects/{pid}/script").json()
        assert script["version"] == 1, script["version"]
        assert script["trace"]["thoughts"], "当前生效轨迹未归档"
        print("[PASS] 首次解析：轨迹持久化（仅结论+阶段）、流式思考不落库、version=1")

        # 2. 二次解析 keep=true → 旧稿原文入历史版本（v1.1：只存原文，不存产物）
        task2 = parse("第一集\n甲：新台词。", True)
        assert task2["status"] == "done", task2
        versions = client.get(f"/api/projects/{pid}/script/versions").json()
        print("历史版本:", [(v["seq"], v["note"], v["text_chars"]) for v in versions])
        assert len(versions) == 1 and versions[0]["seq"] == 1
        assert "shots" not in versions[0] and "characters" not in versions[0], \
            "版本列表不应再有整包产物统计（v1.1 降级）"
        assert versions[0]["text_chars"] > 0, "版本未存原文"
        script = client.get(f"/api/projects/{pid}/script").json()
        assert script["version"] == 2, script["version"]
        # 入档行只存原文：data_json 不再写入（老数据仍可读但不消费）
        import json as _json
        from app.db.session import SessionLocal
        from app.db.models import ScriptVersion as SV
        with SessionLocal() as db:
            row = db.get(SV, versions[0]["id"])
        assert row.text.startswith("第一集") and not (row.data_json or ""), \
            "版本行应只存原文"
        print("[PASS] 二次解析 keep：旧稿原文入档（无产物快照），version 升至 2")

        # 3. 第三次解析失败 → 入档 seq=2，任务 failed，生效数据/轨迹/版本号保持
        task3 = parse("第一集\n甲：失败台词。", True)
        assert task3["status"] == "failed", task3
        script = client.get(f"/api/projects/{pid}/script").json()
        assert script["version"] == 2, "失败不应推进版本号"
        assert script["trace"]["thoughts"], "失败不应清掉已生效轨迹"
        # 4. 失败后再 keep 解析 → 入档 seq=3（跳过已占号）
        task4 = parse("第一集\n甲：再台词。", True)
        assert task4["status"] == "done", task4
        versions = client.get(f"/api/projects/{pid}/script/versions").json()
        assert [v["seq"] for v in versions] == [1, 2, 3], versions
        assert client.get(f"/api/projects/{pid}/script").json()["version"] == 4
        print("[PASS] 解析失败不推进版本号；失败后 keep 入档 seq 不撞号")

        # 5. 恢复历史版本 → 把该版**剧本原文**填回编辑器（不重放解析产物）
        resp = client.post(f"/api/script/versions/{versions[0]['id']}/restore")
        assert resp.json()["ok"], resp.text
        assert resp.json()["text"].startswith("第一集"), "恢复未返回原文"
        script = client.get(f"/api/projects/{pid}/script").json()
        _expect_text = "第一集\n甲：台词。"
        assert script["text"].strip() == _expect_text.strip(), \
            "恢复未把原文写回编辑器存档"
        # 解析产物与版本号不受文本恢复影响（恢复文本 ≠ 恢复解析结果）
        assert script["characters"][0]["name"] == "甲", "恢复不应改动解析产物"
        assert script["version"] == 4, "文本恢复不应回拨版本号"
        eps = client.get(f"/api/projects/{pid}/episodes").json()
        shots = client.get(f"/api/episodes/{eps[0]['id']}/shots").json()
        # 产物仍是最近一次成功解析的结果（mock 固定产出「台词」），未被文本恢复改写
        assert shots[0]["dialogue"][0]["text"] == "台词", \
            f"文本恢复不应重放/覆盖解析产物：{shots[0]['dialogue']}"
        print("[PASS] 恢复历史版本：原文回填编辑器（产物与版本号不受影响）")

        # 6. 恢复后再 keep 解析 → 入档 seq=max(3+1,1)=4，version 升至 5
        task5 = parse("第一集\n甲：恢复后再解析。", True)
        assert task5["status"] == "done", task5
        versions = client.get(f"/api/projects/{pid}/script/versions").json()
        assert [v["seq"] for v in versions] == [1, 2, 3, 4], versions
        assert client.get(f"/api/projects/{pid}/script").json()["version"] == 5
        print("[PASS] 恢复后再 keep 解析：入档 seq 取下一空号，无撞号")

    print("\nVERSION+TRACE SMOKE PASS")


if __name__ == "__main__":
    main()
