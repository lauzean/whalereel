# -*- coding: utf-8 -*-
"""模型调用日志回归（2026-09-30）：分页 / 明细留痕 / 敏感打码 / 权限。

覆盖（调用日志页「分页加载 + 查看详情」的后端契约）：
- 列表：`days=0`（全部档）不截时间；`offset`/`limit` 分页且 `total` 与过滤同口径；
  `only_failed` / `q` 过滤后 total 正确；
- 明细：`GET /api/model-calls/{id}` 返回完整地址与请求/响应留痕；
  `stats` 路由不被 `{call_id}` 抢占；不存在的 id → 404；
- 留痕裁剪：敏感键打码（api_key/token 等）、超长字符串截断且带原文长度标记；
- 权限：系统管理模块接口——普通用户 403，管理员可读。

不写正式库：AVS_DATA_DIR 指向临时目录，AVS_MOCK=1。
运行：`server/.venv/Scripts/python server/test_calls_log.py`
"""
import os
import sys
import tempfile
import time

_tmp = tempfile.mkdtemp(prefix="avs_calls_")
os.environ["AVS_DATA_DIR"] = _tmp
os.environ["AVS_MODELS_DIR"] = os.path.join(_tmp, "models")
os.environ["AVS_MOCK"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import providers  # noqa: E402
from app.core.security import hash_password, new_token  # noqa: E402
from app.db.models import AuthToken, ModelCall, User  # noqa: E402
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
fails: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  OK   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def make_user(username: str, *, admin: bool = False) -> str:
    """建用户 + 令牌，返回 bearer 令牌。"""
    token = new_token()
    with SessionLocal() as db:
        u = User(username=username, password_hash=hash_password("probe123"),
                 is_admin=admin)
        db.add(u)
        db.flush()
        db.add(AuthToken(token=token, user_id=u.id, created_at=int(time.time()),
                         expires_at=int(time.time()) + 3600))
        db.commit()
    return token


tok_admin = make_user("调用日志管理员", admin=True)
tok_user = make_user("调用日志普通用户")
hdr_admin = {"Authorization": f"Bearer {tok_admin}"}
hdr_user = {"Authorization": f"Bearer {tok_user}"}

now = int(time.time())
with SessionLocal() as db:
    fresh = ModelCall(what="LLM", host="a.example", model="m1", ok=True,
                      created_at=now - 10, elapsed_ms=120,
                      url="https://a.example/v1/chat/completions",
                      request_json='{"model": "m1", "prompt": "hi"}',
                      response_json='{"choices": [{"content": "yo"}]}')
    old_fail = ModelCall(what="Agnes 文生图", host="b.example", model="m2",
                         ok=False, fail_class="rate_limit", http_status=429,
                         error="触发限流", created_at=now - 86400 * 2,
                         elapsed_ms=80)
    ancient = ModelCall(what="LLM", host="a.example", model="m1", ok=True,
                        created_at=now - 86400 * 40, elapsed_ms=50)
    db.add_all([fresh, old_fail, ancient])
    db.commit()
    fresh_id, ancient_id = fresh.id, ancient.id

client = TestClient(app)

print("A. 列表分页与时间窗")
r = client.get("/api/model-calls?days=0&limit=2&offset=0", headers=hdr_admin)
body = r.json()
check(f"days=0（全部）total=3、第一页 2 条（实际 {r.status_code}）",
      r.status_code == 200 and body["total"] == 3 and len(body["items"]) == 2)
page1_ids = {it["id"] for it in body["items"]}
r2 = client.get("/api/model-calls?days=0&limit=2&offset=2", headers=hdr_admin)
check("第二页拿到剩余 1 条且与第一页不重叠",
      len(r2.json()["items"]) == 1
      and r2.json()["items"][0]["id"] not in page1_ids)
check("详情端点没抢走 stats 路由（days=0 时 stats 仍可用）",
      client.get("/api/model-calls/stats?days=0", headers=hdr_admin).status_code == 200)
r3 = client.get("/api/model-calls?days=7", headers=hdr_admin)
check(f"近 7 天窗口排除 40 天前的记录（total 实际 {r3.json()['total']}）",
      r3.json()["total"] == 2)
check("新记录排在最前（倒序）", body["items"][0]["id"] == fresh_id)

print("B. 过滤条件与 total 同口径")
r4 = client.get("/api/model-calls?days=0&only_failed=true", headers=hdr_admin)
check(f"只看失败：total=1（实际 {r4.json()['total']}）", r4.json()["total"] == 1)
r5 = client.get("/api/model-calls?days=0&q=Agnes", headers=hdr_admin)
check(f"关键词过滤：total=1（实际 {r5.json()['total']}）", r5.json()["total"] == 1)
r6 = client.get("/api/model-calls?days=0&offset=99", headers=hdr_admin)
check("越界 offset：total 不变、items 为空",
      r6.json()["total"] == 3 and r6.json()["items"] == [])

print("C. 明细留痕")
r = client.get(f"/api/model-calls/{fresh_id}", headers=hdr_admin)
call = r.json().get("call", {}) if r.status_code == 200 else {}
check(f"明细返回完整地址（实际 {r.status_code}）",
      call.get("url") == "https://a.example/v1/chat/completions")
check("明细含请求留痕（原样的 JSON 字符串）",
      '"prompt"' in str(call.get("request_json", "")))
check("明细含响应留痕", "choices" in str(call.get("response_json", "")))
check("不存在的调用 → 404",
      client.get("/api/model-calls/nope", headers=hdr_admin).status_code == 404)

print("D. 留痕裁剪（写入前打码 + 截断）")
clipped = providers._clip_trace({
    "api_key": "sk-very-secret", "token": "tok-1",
    "prompt": "x" * 9000, "nested": {"password": "pw"}, "list": list(range(500)),
})
check("敏感键打码（密钥/令牌/口令都不落日志）",
      "sk-very-secret" not in clipped and "tok-1" not in clipped
      and "pw" not in clipped and '"***"' in clipped)
check("超长字段截断且带原文长度标记",
      "（截断，原长" in clipped and len(clipped) < 9000)
check("超长列表截断（不整份落库）", "已截断" in clipped)

print("E. 权限：系统管理模块，普通用户一律 403")
check("普通用户列表 → 403",
      client.get("/api/model-calls", headers=hdr_user).status_code == 403)
check("普通用户明细 → 403",
      client.get(f"/api/model-calls/{fresh_id}", headers=hdr_user).status_code == 403)
check("普通用户看板 → 403",
      client.get("/api/model-calls/stats", headers=hdr_user).status_code == 403)
check("匿名列表 → 401", client.get("/api/model-calls").status_code == 401)

print()
if fails:
    print("存在未通过项：", fails)
    sys.stdout.flush()
    os._exit(1)
print("调用日志自检全部通过")
sys.stdout.flush()
os._exit(0)
