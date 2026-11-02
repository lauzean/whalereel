# -*- coding: utf-8 -*-
"""积分计费体系回归（33 号方案；2026-10-09 口径定稿：按模型服务端点费率、
按调用情况计费）。

守的底线（任何一条红了都不许合并）：
① **钱不会错**：原子扣减防并发透支；余额永不为负；流水与余额对账全平；
② **不会多收**：请求失败自动退款、任务失败/取消结算退款、缓存命中退款，
   且全部幂等（同一笔只退一次）；
③ **不会白嫖**：批量部分成功不退全款、单任务「远端已计费」不退、余额不足
   直接 402 且业务完全不执行；
④ **链路不静默**：HTTP 请求 → 预扣 → 任务落 billing_txn_id（ContextVar 全链路
   守护——传播失效这里就会红，不会悄悄变成"退款关联丢失"）；
⑤ **口径正确（2026-10-09）**：计费闸清单只含大模型调用接口（系统平台接口
   免费）；行级计价按「所用模型服务端点费率 × 用量」；费率在模型端点上配置；
⑥ **权限**：普通用户只碰自己的账户/流水，充值/兑换码生成/端点费率全是管理员专属。

运行：`server/.venv/Scripts/python test_billing.py`
"""
import os
import sys
import tempfile
import threading
import time

_tmp = tempfile.mkdtemp(prefix="avs_bill_")
os.environ["AVS_DATA_DIR"] = _tmp
os.environ["AVS_MODELS_DIR"] = os.path.join(_tmp, "models")
os.environ["AVS_MOCK"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import billing, providers  # noqa: E402
from app.core.security import hash_password, new_token  # noqa: E402
from app.db.models import (AuthToken, CreditAccount,  # noqa: E402
                           CreditTransaction, ModelCall, Project, Task, User)
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
# startup 里会做的事（TestClient 不触发 startup）：计费闸清单与路由对账
_missing_gated = billing.verify_gated_endpoints(app)

fails: list[str] = []
_n_ok = 0


def check(name: str, cond: bool) -> None:
    global _n_ok
    if cond:
        _n_ok += 1
        print("  OK   " + name)
    else:
        fails.append(name)
        print("  FAIL " + name)


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
        p = Project(name=name, mode="drama", owner_user_id=owner)
        db.add(p)
        db.commit()
        return p.id


def balance_of(user_id: str) -> int:
    with SessionLocal() as db:
        row = db.scalar(sa.select(CreditAccount).where(
            CreditAccount.user_id == user_id))
        return int(row.balance) if row else 0


def set_endpoint_rates(host: str, model: str, *, tokens: int = 0,
                       images: int = 0, seconds: int = 0, kind: str = "llm",
                       enabled: bool = True) -> str:
    """造一个「模型服务端点」并配费率（2026-10-09 口径：费率挂模型端点上）。

    host 与调用日志的 host 对齐（行级计价的匹配就是按域名逐字比对）。
    返回端点 id。改完显式失效 providers 费率缓存（模拟「模型管理」保存）。
    """
    from app.db.models import ProviderEndpoint
    with SessionLocal() as db:
        row = ProviderEndpoint(
            name=f"测试服务-{host}", kind=kind, provider="custom",
            base_url=f"https://{host}/v1", model=model,
            credits_per_1k_tokens=tokens, credits_per_image=images,
            credits_per_video_second=seconds, bill_enabled=enabled)
        db.add(row)
        db.commit()
        endpoint_id = row.id
    providers.invalidate_endpoint_rates()
    return endpoint_id


def make_call(*, credits: int, request_id: str = "", task_id: str = "",
              ok: bool = True, host: str = "test.example.com") -> None:
    """造一行调用日志（行级积分 = credits；「按调用情况计费」的最小单元）。

    行级积分的**计算**逻辑（费率匹配 × 用量折算）由「十」组的专项断言守；
    结算类测试直接给定 credits 值，专注把「结算/退款/幂等」这条链测透。
    """
    with SessionLocal() as db:
        db.add(ModelCall(what="测试调用", ok=ok, host=host,
                         request_id=request_id or "",
                         task_id=task_id or None, credits=int(credits),
                         usage_json={}))
        db.commit()


def make_task(project_id: str, *, txn_id: str, status: str = "queued",
              runner: str = "t2i") -> str:
    with SessionLocal() as db:
        t = Task(project_id=project_id, node_id="node_t", runner=runner,
                 status=status, billing_txn_id=txn_id or None)
        db.add(t)
        db.commit()
        return t.id


def set_task_status(task_id: str, status: str) -> None:
    with SessionLocal() as db:
        t = db.get(Task, task_id)
        t.status = status
        db.commit()


def txn_status(txn_id: str) -> str:
    with SessionLocal() as db:
        return db.get(CreditTransaction, txn_id).status


client = TestClient(app)

uid_a, tok_a = make_user("计费用户甲")
uid_b, tok_b = make_user("计费用户乙")
uid_admin, tok_admin = make_user("计费管理员", admin=True)
hdr_a = {"Authorization": f"Bearer {tok_a}"}
hdr_b = {"Authorization": f"Bearer {tok_b}"}
hdr_admin = {"Authorization": f"Bearer {tok_admin}"}
proj_a = make_project("甲的项目", uid_a)

print("== 一、扣费正确性与全链路 ==")
# 新用户首扣：自动建户 + 初始赠送（默认 200）。
# POST /api/tasks 在计费闸清单里（预扣 30 内置）——无需任何配置即生效
# （2026-10-09 口径：预扣额内置、费率按模型服务端点配置）。
check("建户前无账户行", balance_of(uid_a) == 0)
r = client.post("/api/tasks",
                json={"project_id": proj_a, "node_id": "n1", "runner": "t2i"},
                headers=hdr_a)
check("计费端点调用成功（201）", r.status_code == 201)
check("首扣：赠送 200 - 预扣 30 = 170", balance_of(uid_a) == 170)
task_id = r.json()["task_id"]
with SessionLocal() as db:
    t = db.get(Task, task_id)
    check("任务落 billing_txn_id（ContextVar 全链路）",
          bool(t.billing_txn_id))
    txn = db.get(CreditTransaction, t.billing_txn_id)
    check("流水 pending → consumed（请求成功后计费成立）",
          txn is not None and txn.status == "consumed")
    check("流水金额/接口键/预扣额正确",
          txn.amount == -30 and txn.endpoint == "POST /api/tasks"
          and txn.price == 30 and txn.balance_after == 170)

# 免费端点（GET）不扣费
r = client.get("/api/projects", headers=hdr_a)
check("免费端点（GET）不扣费", r.status_code == 200 and balance_of(uid_a) == 170)

print("== 二、余额不足（402）与请求失败退款 ==")
# 请求失败退款：422（缺字段）→ 扣了立即退，净变化 0
r = client.post("/api/tasks", json={"project_id": proj_a}, headers=hdr_a)
check("坏请求返回 422", r.status_code == 422)
check("422 自动退款（净 0）", balance_of(uid_a) == 170)

# 余额不足：调到 3（小于预扣 30）
billing.adjust(uid_a, -(balance_of(uid_a) - 3), operator_user_id=uid_admin,
               note="测试清零")
r = client.post("/api/tasks",
                json={"project_id": proj_a, "node_id": "n2", "runner": "t2i"},
                headers=hdr_a)
body = r.json()
check("余额不足返回 402", r.status_code == 402)
check("402 契约字段齐全",
      body.get("code") == "insufficient_credits" and body.get("required") == 30
      and body.get("balance") == 3)
with SessionLocal() as db:
    n_tasks = db.scalar(sa.select(sa.func.count()).select_from(Task)
                        .where(Task.node_id == "n2"))
check("余额不足时业务完全不执行（没建任务）", int(n_tasks or 0) == 0)
check("402 不扣费（余额仍 3）", balance_of(uid_a) == 3)

print("== 三、并发不透支（原子扣减） ==")
uid_c, _tok_c = make_user("并发用户")
results: list[dict] = []
lock = threading.Lock()


def _concurrent_charge() -> None:
    r = billing.charge(uid_c, "POST /api/tasks", 30)  # 200 / 30 = 6 笔该成功
    with lock:
        results.append(r)


threads = [threading.Thread(target=_concurrent_charge) for _ in range(12)]
for t in threads:
    t.start()
for t in threads:
    t.join()
ok_n = sum(1 for r in results if r["ok"])
check("12 个并发请求恰好 6 笔成功（200/30）", ok_n == 6)
check("并发后余额 20（不为负、不超卖）", balance_of(uid_c) == 20)

print("== 四、任务失败/取消结算（退款规则） ==")


def fresh_txn(user_id: str, price: int) -> str:
    """造一笔消费流水（模拟真实时序：扣费 → HTTP 成功返回 → 计费成立）。

    必须先 mark_consumed 再让任务跑：真实流程里任务开始执行时，请求早已
    返回 200、流水已转 consumed；若停在 pending，启动恢复会把「任务失败但
    流水仍 pending」误当崩溃遗留处理（测试夹具与真实流程的已知差异点）。
    """
    r = billing.charge(user_id, "POST /api/episodes/x/audit", price)
    assert r["ok"], "测试扣费失败（余额不够？）"
    billing.mark_consumed(r["txn_id"])
    return r["txn_id"]


# 单任务失败 → 全退
t1 = fresh_txn(uid_b, 10)
before = balance_of(uid_b)
task1 = make_task(proj_a, txn_id=t1)
set_task_status(task1, "failed")
billing.settle_after_terminal(t1)
check("单任务失败 → 自动退款", balance_of(uid_b) == before + 10)
check("消费流水转 refunded", txn_status(t1) == "refunded")
billing.settle_after_terminal(t1)  # 重复结算
check("重复结算幂等（不双退）", balance_of(uid_b) == before + 10)

# 单任务取消 → 全退
t2 = fresh_txn(uid_b, 10)
before = balance_of(uid_b)
task2 = make_task(proj_a, txn_id=t2)
set_task_status(task2, "canceled")
billing.settle_after_terminal(t2)
check("单任务取消 → 自动退款", balance_of(uid_b) == before + 10)

# 单任务 cached → 退（零成本）
t3 = fresh_txn(uid_b, 10)
before = balance_of(uid_b)
task3 = make_task(proj_a, txn_id=t3, status="cached")
billing.settle_after_terminal(t3)
check("缓存命中（cached）→ 自动退款", balance_of(uid_b) == before + 10)

# 批量：全失败 → 全退
t4 = fresh_txn(uid_b, 20)
before = balance_of(uid_b)
ta = make_task(proj_a, txn_id=t4)
tb = make_task(proj_a, txn_id=t4)
set_task_status(ta, "failed")
billing.settle_after_terminal(t4)
check("批量：还有一个在跑 → 先不退", balance_of(uid_b) == before)
set_task_status(tb, "failed")
billing.settle_after_terminal(t4)
check("批量：全失败 → 全退", balance_of(uid_b) == before + 20)

# 批量：部分成功 → 只按**成功任务**的调用记录结算（退差额；失败任务不计）
t5 = fresh_txn(uid_b, 20)
before = balance_of(uid_b)
tc = make_task(proj_a, txn_id=t5)
td = make_task(proj_a, txn_id=t5)
make_call(credits=8, task_id=tc)  # 成功任务的调用记录：8 分
set_task_status(tc, "done")
set_task_status(td, "failed")
billing.settle_after_terminal(t5)
# before 取在预扣之后：结算再退差额 12 → 最终 = before + 12（实际只花 8 分）
check("批量部分成功：按成功任务调用结算（8 分，退 12）",
      balance_of(uid_b) == before + 12)

# 单任务 billed 失败 → 不退（远端已计费）
t6 = fresh_txn(uid_b, 10)
before = balance_of(uid_b)
task6 = make_task(proj_a, txn_id=t6)
set_task_status(task6, "failed")
billing.settle_after_terminal(t6, fail_class="billed")
check("单任务「远端已计费」失败 → 不退", balance_of(uid_b) == before)

print("== 五、崩溃恢复三分类 ==")
# pending + 关联任务仍在跑 → 置 consumed（不退款）。
# 手工造「纯 pending」流水（不经 mark_consumed——那才是崩溃遗留的真实形态）
r7 = billing.charge(uid_b, "POST /api/episodes/x/audit", 10)
assert r7["ok"]
t7 = r7["txn_id"]
before = balance_of(uid_b)
make_task(proj_a, txn_id=t7, status="running")
billing.recover_pending_transactions()
check("遗留 pending + 任务仍在跑 → 计费成立不退款",
      txn_status(t7) == "consumed" and balance_of(uid_b) == before)
# pending + 无关联任务 → 全额退款（同样手工保持 pending）
r8 = billing.charge(uid_b, "POST /api/episodes/x/audit", 10)
assert r8["ok"]
t8 = r8["txn_id"]
before = balance_of(uid_b)
billing.recover_pending_transactions()
check("遗留 pending + 无任务（业务没发生）→ 全额退款",
      txn_status(t8) == "refunded" and balance_of(uid_b) == before + 10)

print("== 六、充值 / 调整 / 兑换码 ==")
before = balance_of(uid_b)
billing.recharge(uid_b, 100, operator_user_id=uid_admin, note="测试充值")
check("管理员充值到账", balance_of(uid_b) == before + 100)
try:
    billing.recharge(uid_b, -5, operator_user_id=uid_admin)
    check("充值负数被拒", False)
except ValueError:
    check("充值负数被拒", True)
before = balance_of(uid_b)
billing.adjust(uid_b, -50, operator_user_id=uid_admin, note="扣错纠正")
check("负调整生效", balance_of(uid_b) == before - 50)
try:
    billing.adjust(uid_b, -(balance_of(uid_b) + 1), operator_user_id=uid_admin)
    check("负调整不许扣成负数", False)
except ValueError:
    check("负调整不许扣成负数", True)

codes = billing.create_redeem_codes(50, 3, note="测试批次", created_by=uid_admin)
check("批量生成兑换码（3 张）", len(codes) == 3 and len(set(codes)) == 3)
before = balance_of(uid_b)
res = billing.redeem_code(uid_b, codes[0])
check("兑换码核销成功 +50", res["ok"] and balance_of(uid_b) == before + 50)
res2 = billing.redeem_code(uid_b, codes[0])
check("同码二次兑换被拒", (not res2["ok"]) and balance_of(uid_b) == before + 50)
res3 = billing.redeem_code(uid_b, "WRFAKECODEWRFAKECODE")
check("伪造码被拒", not res3["ok"])
# 并发兑换同一张码：只有一个人成功
redeem_results: list[dict] = []


def _concurrent_redeem(user_id: str) -> None:
    r = billing.redeem_code(user_id, codes[1])
    with lock:
        redeem_results.append(r)


uid_d, _tok_d = make_user("并发兑换用户")
tr = [threading.Thread(target=_concurrent_redeem, args=(u,))
      for u in (uid_b, uid_d, uid_b, uid_d)]
for t in tr:
    t.start()
for t in tr:
    t.join()
check("并发兑换同码只成功一次",
      sum(1 for r in redeem_results if r["ok"]) == 1)

print("== 七、权限（普通用户 vs 管理员） ==")
check("普通用户：查自己账户 200",
      client.get("/api/billing/account", headers=hdr_a).status_code == 200)
for path, method in (("/api/billing/settings", "get"),
                     ("/api/billing/accounts", "get"),
                     ("/api/billing/redeem-codes", "get"),
                     ("/api/billing/transactions/all", "get")):
    r = getattr(client, method)(path, headers=hdr_a)
    check(f"普通用户访问管理接口被拒 403：{path}", r.status_code == 403)
r = client.post("/api/endpoints/apply-suggested-prices", headers=hdr_a)
check("普通用户应用端点费率被拒 403", r.status_code == 403)
r = client.post("/api/billing/recharge",
                json={"user_id": uid_a, "amount": 100}, headers=hdr_a)
check("普通用户给自己充值被拒 403", r.status_code == 403)
check("普通用户无权调整自己（哪怕给自己加钱）", balance_of(uid_a) == 3)
# 管理员全通（端点列表带费率字段——费率配置的读取面）
r = client.get("/api/endpoints", headers=hdr_admin)
check("管理员：端点列表 200 且带费率字段", r.status_code == 200
      and all("credits_per_1k_tokens" in x for x in r.json()))
r = client.post("/api/billing/recharge",
                json={"user_id": uid_a, "amount": 100, "note": "管理员测试"},
                headers=hdr_admin)
check("管理员给用户充值 200 + 到账", r.status_code == 200
      and balance_of(uid_a) == 103)

print("== 八、流水与隔离 ==")
r = client.get("/api/billing/transactions", headers=hdr_b)
check("用户乙能查到自己流水", r.status_code == 200
      and r.json()["total"] > 0)
items = r.json()["items"]
check("用户乙的流水全是自己的",
      all(it["user_id"] == uid_b for it in items))
r = client.get("/api/billing/transactions", headers=hdr_a)
check("用户甲查不到乙的流水",
      all(it["user_id"] == uid_a for it in r.json()["items"]))

print("== 九、全局配置（开关矩阵） ==")
# 总开关关闭 → 全免费
billing.update_settings({"enabled": False})
before = balance_of(uid_a)
r = client.post("/api/tasks",
                json={"project_id": proj_a, "node_id": "n9", "runner": "t2i"},
                headers=hdr_a)
check("计费总开关关闭 → 端点不扣费", r.status_code == 201
      and balance_of(uid_a) == before)
billing.update_settings({"enabled": True})
# 管理员也计费（默认）——先充值建户，让余额基数确定（否则首扣带赠送不好断言）
billing.recharge(uid_admin, 100, operator_user_id=uid_admin, note="测试建户")
before = balance_of(uid_admin)
r = client.post("/api/tasks",
                json={"project_id": proj_a, "node_id": "n10", "runner": "t2i"},
                headers=hdr_admin)
check("管理员也计费（默认）→ 预扣 30", r.status_code == 201
      and balance_of(uid_admin) == before - 30)
# 管理员免费开关打开
billing.update_settings({"admin_free": True})
before = balance_of(uid_admin)
r = client.post("/api/tasks",
                json={"project_id": proj_a, "node_id": "n11", "runner": "t2i"},
                headers=hdr_admin)
check("管理员免费开关打开 → 不扣", r.status_code == 201
      and balance_of(uid_admin) == before)
billing.update_settings({"admin_free": False})

print("== 十、计费闸清单与模型服务端点费率（2026-10-09 口径定稿） ==")
# ① 计费闸清单：只含大模型调用接口；预扣额内置、无需配置；系统平台接口免费
check("计费闸清单 19 个接口且路由全部存在",
      len(billing.GATED_ENDPOINTS) == 19 and _missing_gated == [])
for _m, _p in (("POST", "/api/auth/login"), ("POST", "/api/projects"),
               ("PATCH", "/api/episodes/x"), ("POST", "/api/billing/recharge")):
    check(f"系统平台接口免费（不在计费闸）：{_m} {_p}",
          billing.match_rule(_m, _p) is None)
check("计费接口命中内置预扣（对话 10）",
      billing.match_rule("POST", "/api/sessions/abc/chat")
      == ("POST /api/sessions/{session_id}/chat", 10))
# ② pricing 接口 = 前端「预扣 N」徽标的数据源（内置清单视图）
r = client.get("/api/billing/pricing", headers=hdr_a)
rules = r.json()["rules"]
check("pricing 返回内置清单（19 项、含预扣与中文名）",
      len(rules) == 19
      and all(x["precharge"] > 0 and x["label"] for x in rules))
check("pricing 键与前端约定一致（METHOD /path）",
      all(" " in x["key"] for x in rules))
# ③ 模型服务端点费率（行级计价的来源）：匹配与折算断言
set_endpoint_rates("ark.test.local", "doubao-x", tokens=2, kind="llm")
set_endpoint_rates("agnes.test.local", "agnes-img", images=5, kind="t2i")
set_endpoint_rates("free.test.local", "local-llm", kind="llm")  # 全 0 = 免费
set_endpoint_rates("off.test.local", "off-llm", tokens=2, kind="llm",
                   enabled=False)  # 计费开关关闭
check("行级计价：LLM 12500 token × 2 分/千 → 26 分",
      providers._call_credits("ark.test.local", "doubao-x",
                              {"tokens": {"total_tokens": 12500}}) == 26)
check("行级计价：生图 3 张 × 5 分/张 → 15 分",
      providers._call_credits("agnes.test.local", "agnes-img",
                              {"images": 3}) == 15)
set_endpoint_rates("vid.test.local", "vid-x", seconds=3, kind="t2v")
check("行级计价：视频 6 秒 × 3 分/秒 → 18 分",
      providers._call_credits("vid.test.local", "vid-x",
                              {"req_duration": 6}) == 18)
check("行级计价：费率全 0 的服务 → 0 分（免费）",
      providers._call_credits("free.test.local", "local-llm",
                              {"tokens": {"total_tokens": 9999}}) == 0)
check("行级计价：计费开关关闭 → 0 分",
      providers._call_credits("off.test.local", "off-llm",
                              {"tokens": {"total_tokens": 9999}}) == 0)
check("行级计价：未登记的服务 → 0 分（宁缺毋滥）",
      providers._call_credits("unknown.test.local", "x", {"images": 9}) == 0)
check("行级计价：同域不同模型名 → 按该域端点兜底",
      providers._call_credits("ark.test.local", "other-name",
                              {"tokens": {"total_tokens": 2000}}) == 4)
# ④ 「应用建议费率」（模型管理页按钮）：只填未配置的端点、不覆盖手调值
from app.db.models import ProviderEndpoint  # noqa: E402
with SessionLocal() as db:
    db.add(ProviderEndpoint(name="建议-llm", kind="llm", provider="custom",
                            base_url="https://sug.test.local/v1", model="m"))
    db.commit()
updated = billing.apply_suggested_endpoint_prices(overwrite=False)
with SessionLocal() as db:
    sug = db.scalars(sa.select(ProviderEndpoint).where(
        ProviderEndpoint.name == "建议-llm")).first()
    ark = db.scalars(sa.select(ProviderEndpoint).where(
        ProviderEndpoint.name.like("%ark.test.local%"))).first()
check("应用建议费率：未配置的 llm 端点填入 1 分/千 token",
      sug.credits_per_1k_tokens == 1 and updated >= 1)
check("应用建议费率：不覆盖已手调的端点（ark 仍 2 分/千）",
      ark.credits_per_1k_tokens == 2)

print("== 十一、按调用记录结算（预扣 → 退差/补扣） ==")
uid_e, _tok_e = make_user("按用量用户")
# 先充值建户，让余额基数确定（新用户首扣会带初始赠送，不好断言差额）
billing.recharge(uid_e, 100, operator_user_id=uid_admin, note="测试建户")

# ① 预扣 → 实际更少 → 退差额（调用记录的行级积分之和 = 实际）
before = balance_of(uid_e)
r = billing.charge(uid_e, "POST /x", 30, request_id="req-u1")
check("预扣 30", r["ok"] and balance_of(uid_e) == before - 30)
make_call(credits=27, request_id="req-u1")
actual = billing.settle_usage(r["txn_id"], request_id="req-u1")
check("按调用记录结算：实际 27 分", actual == 27)
check("退差额 3 分", balance_of(uid_e) == before - 27)
check("结算幂等（重复结算 None 且余额不变）",
      billing.settle_usage(r["txn_id"], request_id="req-u1") is None
      and balance_of(uid_e) == before - 27)

# ② 实际更多 → 补扣差额
before = balance_of(uid_e)
r2 = billing.charge(uid_e, "POST /x", 30, request_id="req-u2")
make_call(credits=30, request_id="req-u2")
make_call(credits=20, request_id="req-u2")  # 合计 50 分（多笔调用累计）
actual = billing.settle_usage(r2["txn_id"], request_id="req-u2")
check("多笔调用累计结算：50 分", actual == 50)
check("补扣差额 20 分", balance_of(uid_e) == before - 50)

# ③ 失败调用的记录不计费
before = balance_of(uid_e)
r3 = billing.charge(uid_e, "POST /x", 30, request_id="req-u3")
make_call(credits=0, ok=False, request_id="req-u3")  # 失败行：不计费
actual = billing.settle_usage(r3["txn_id"], request_id="req-u3")
check("失败调用的记录不计费（实际 0 → 全额退预扣）", actual == 0
      and balance_of(uid_e) == before)

# ④ 已结算流水不被启动恢复误退（同步端点结算后状态应为 consumed）
with SessionLocal() as db:
    t = db.get(CreditTransaction, r["txn_id"])
    check("结算后原流水为 consumed 且带结算时间戳",
          t.status == "consumed" and t.settled_at > 0)
billing.recover_pending_transactions()
check("启动恢复不误动已结算流水（余额不变）", balance_of(uid_e) == before)

# ⑤ 任务化：done 任务按实际调用结算；全失败全退预扣
before = balance_of(uid_e)
r4 = billing.charge(uid_e, "POST /x", 30, request_id="req-u4")
# 模拟真实时序：请求先成功返回（流水 consumed），任务之后才开始跑/终结
# （提交窗口保护要求 pending 不结算——窗口内任务可能只建了一部分）
billing.mark_consumed(r4["txn_id"])
t_done = make_task(proj_a, txn_id=r4["txn_id"])
t_fail = make_task(proj_a, txn_id=r4["txn_id"])
make_call(credits=9, task_id=t_done)  # done 任务名下的调用：9 分
set_task_status(t_done, "done")
billing.settle_after_terminal(r4["txn_id"])
check("批量未全终态：先不结算", balance_of(uid_e) == before - 30)
set_task_status(t_fail, "failed")
billing.settle_after_terminal(r4["txn_id"])
check("全终态且有 done：按成功任务的调用记录结算（9 分，退 21）",
      balance_of(uid_e) == before - 9)
# 全失败：全退预扣
before = balance_of(uid_e)
r5 = billing.charge(uid_e, "POST /x", 30, request_id="req-u5")
billing.mark_consumed(r5["txn_id"])  # 同上：请求先返回，任务后终结
ta = make_task(proj_a, txn_id=r5["txn_id"])
set_task_status(ta, "failed")
billing.settle_after_terminal(r5["txn_id"])
check("任务全失败：全额退预扣（不按调用收）", balance_of(uid_e) == before)

# ⑥ 提交窗口保护（评审修正）：请求未返回（流水 pending）时任何结算触发
#    都不动作——批量提交中「第一镜 cached」不得误退整笔预扣
before = balance_of(uid_e)
r6 = billing.charge(uid_e, "POST /x", 30, request_id="req-u6")
make_task(proj_a, txn_id=r6["txn_id"], status="cached")  # 兄弟任务还没建完
billing.settle_after_terminal(r6["txn_id"])
check("提交窗口保护：pending 不结算（不误退整笔预扣）",
      balance_of(uid_e) == before - 30)
billing.mark_consumed(r6["txn_id"])  # 响应返回（全部任务已提交完）
billing.settle_after_terminal(r6["txn_id"])
check("响应返回后统一触发：全 cached → 退全款", balance_of(uid_e) == before)

# ⑦ 自动退款开关：关闭后失败按「调用成立」收全款（流水收尾，不悬挂）
billing.update_settings({"auto_refund": False})
before = balance_of(uid_e)
r7 = billing.charge(uid_e, "POST /x", 30, request_id="req-u7")
billing.mark_consumed(r7["txn_id"])
ta7 = make_task(proj_a, txn_id=r7["txn_id"])
set_task_status(ta7, "failed")
billing.settle_after_terminal(r7["txn_id"])
check("自动退款关闭：失败不退且流水收尾为 consumed",
      balance_of(uid_e) == before - 30
      and txn_status(r7["txn_id"]) == "consumed")
billing.update_settings({"auto_refund": True})

# ⑧ 新用户首扣余额不足：建户与初始赠送保留（评审修正——旧实现 rollback 丢赠送）
uid_f, _tok_f = make_user("首扣不足用户")
r8 = billing.charge(uid_f, "POST /x", 250, request_id="req-u8")  # 250 > 赠送 200
check("首扣不足：返回当前余额含赠送（200 而不是 0）",
      (not r8["ok"]) and r8["balance"] == 200)
check("首扣不足：赠送已落库（账户存在且余额 200）", balance_of(uid_f) == 200)

print("== 十二、对账 ==")
audit = billing.audit_consistency()
check("全账户对账全平", audit["bad"] == [] and audit["checked"] >= 4)

# 收尾：每个账户的余额 == 流水汇总（独立复算一遍，防对账函数自身错）
with SessionLocal() as db:
    all_ok = True
    for (uid2,) in db.execute(sa.select(CreditAccount.user_id)).all():
        bal = db.scalar(sa.select(CreditAccount.balance).where(
            CreditAccount.user_id == uid2))
        s = db.scalar(sa.select(sa.func.coalesce(sa.func.sum(
            CreditTransaction.amount), 0)).where(
            CreditTransaction.user_id == uid2))
        if int(bal) != int(s):
            all_ok = False
            print(f"  不平：{uid2} balance={bal} sum={s}")
check("独立复算：余额 == 流水汇总（全账户）", all_ok)

print()
print(f"计费体系回归：通过 {_n_ok} / {_n_ok + len(fails)} 项")
if fails:
    print("存在未通过项：")
    for f in fails:
        print("  - " + f)
    sys.stdout.flush()
    os._exit(1)
print("计费体系回归全部通过")
sys.stdout.flush()
os._exit(0)
