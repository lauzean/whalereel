# -*- coding: utf-8 -*-
"""可靠性整改回归（2026-09-30）：守住四处「静默失效」的机制。

覆盖（每条都是真实存在过、且不修就静默烧钱/卡死的缺陷）：

- **缓存读写键一致**：随机 seed 必须在缓存键算好**之后**才注入，否则读键与写键
  永远不同 → 缓存只写不读、「多级缓存省 70% 成本」形同虚设；
- **看门狗补泳道**：条件是「worker 未完成」（卡死的 worker 恰恰未完成），
  且同一卡死任务只补一次（否则每轮循环各堆一个 worker）；
- **缓存淘汰口径**：统计**被引用产物的真实字节数**，不是 marker 自身大小
  （旧实现统计约 120 字节的 json，20GB 上限需 1.7 亿个 marker 才触发 → 永不淘汰）；
- **就绪度不写恒真断言**：`bool(model) or True` 这种「永不可能失败」的检查等于没有。
- **重摇语义**：reroll=True 同参数二次提交必须**不命中**（点「重摇」要出新图），
  reroll=False 必须命中（切回旧参数零成本）——上一轮「修缓存」时把重摇修成了
  「拿回同一张图」，代理指标式的断言全绿也抓不到，这里用行为断言守住。

不写正式库：AVS_DATA_DIR 指向临时目录，AVS_MOCK=1。
运行：`server/.venv/Scripts/python server/test_reliability_fixes.py`
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="avs_fixprobe_")
os.environ["AVS_DATA_DIR"] = _tmp
os.environ["AVS_MODELS_DIR"] = os.path.join(_tmp, "models")
os.environ["AVS_MOCK"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.core import cache                                   # noqa: E402
from app.core.task_engine import engine                       # noqa: E402
from app.db.models import Asset, Project, Setting             # noqa: E402
from app.db.session import SessionLocal, init_db              # noqa: E402

init_db()
with SessionLocal() as db:
    proj = Project(name="整改自检", mode="canvas")
    db.add(proj)
    db.commit()
    pid = proj.id

fails: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  OK   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------- A. 缓存键口径 ----------
print("A. 缓存读写键一致（seed 不参与键）")
params = {"prompt": "同一个提示词", "aspect": "9:16"}
refs: dict = {}
key_before = cache.make_key("t2i", "m1", dict(params), refs)
injected = dict(params)
injected["seed"] = 123456
key_after = cache.make_key("t2i", "m1", injected, refs)
check("注入 seed 后键会变（所以键必须在注入前算）", key_before != key_after)

# 写一个 marker，再用**不含 seed 的键**查——必须命中
with SessionLocal() as db:
    asset = Asset(kind="image", path=__file__, sha256="x")
    db.add(asset)
    db.commit()
    aid = asset.id
cache.store("t2i", "m1", {"p": 1}, {}, aid, key="fixed-key")
check("按固化键 store 后 lookup 命中", cache.lookup("t2i", "m1", {}, {}, key="fixed-key") == aid)
check("用别的键查不命中", cache.lookup("t2i", "m1", {}, {}, key="other-key") is None)

# ---------- B. 看门狗补泳道条件 ----------
print("B. 看门狗补泳道：worker 活着才补，且只补一次")
check("已登记的标记集合存在", hasattr(engine, "_stall_compensated"))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "app", "core", "task_engine.py"), encoding="utf-8").read()
check("条件是「worker 未完成」而非「已完成」",
      "if task is not None and not task.done():" in src)
check("补之前检查 had_heartbeat（防每轮重复补）",
      "if not had_heartbeat or tid in self._stall_compensated:" in src)
check("任务收尾复位标记", "_stall_compensated.discard(task_id)" in src)

# ---------- C. 缓存淘汰统计口径 ----------
print("C. 缓存淘汰统计真实产物字节")
check("新增 _referenced_bytes", hasattr(cache, "_referenced_bytes"))
big = os.path.join(_tmp, "big.bin")
with open(big, "wb") as f:
    f.write(b"0" * 2_000_000)   # 2MB 假产物
with SessionLocal() as db:
    a2 = Asset(kind="video", path=big, sha256="y")
    db.add(a2)
    db.commit()
    aid2 = a2.id
cache.store("t2v", "m2", {}, {}, aid2, key="big-key")
sizes = cache._referenced_bytes(list((__import__("pathlib").Path(_tmp) / "cache").glob("*.json")))
check("统计到的是产物真实字节（>=2MB），不是 marker 的百字节级",
      sum(sizes.values()) >= 2_000_000)

# ---------- D. readiness 不再恒真 ----------
print("D. readiness 视频模型检查")
r_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "app", "services", "readiness.py"), encoding="utf-8").read()
# 只看**代码行**（注释里引用了旧写法作说明，不能把注释也判成违规）
code_lines = [ln for ln in r_src.splitlines() if not ln.lstrip().startswith("#")]
check("恒真写法已从代码中消除",
      not any("bool(model) or True" in ln for ln in code_lines))
check("补了真实的「本地权重未安装」判据",
      "installed_spec_ids" in r_src and "video_model_ok" in r_src)

# ---------- E. submit 的真实行为（键先算、seed 后注入、键留待写侧复用） ----------
print("E. submit 的调用顺序与键固化")
from app.db.models import Task   # noqa: E402

sub1 = engine.submit(project_id=pid, node_id="n_seed", runner_name="t2i",
                     params={"prompt": "同一提示词"}, input_refs={})
with SessionLocal() as db:
    t1 = db.get(Task, sub1["task_id"])
    p1 = dict(t1.params_json or {})
check("reroll 默认注入随机 seed（抽卡语义仍未破坏）", str(p1.get("seed") or "") != "")
check("未命中时把缓存键留给写侧复用（键不随 seed 漂移）",
      engine._cache_keys.get(sub1["task_id"], "") != "")

# reroll=False 时不注入 seed（系统内部重算要能命中缓存）
sub2 = engine.submit(project_id=pid, node_id="n_noroll", runner_name="t2i",
                     params={"prompt": "同一提示词"}, input_refs={}, reroll=False)
with SessionLocal() as db:
    t2 = db.get(Task, sub2["task_id"])
    p2 = dict(t2.params_json or {})
check("reroll=False 不注入 seed", not str(p2.get("seed") or ""))

# ---------- F. 重摇语义：行为断言（2026-09-30 回修） ----------
# doc 14 §4.1 的口径：同参数二次提交——reroll=True 必不命中（点「重摇」要出新图）、
# reroll=False 必须命中（切回旧参数零成本）。上一轮修「缓存永不命中」时把
# reroll=True 也修成了命中，用户点重摇会拿回同一张图；而当时的断言只看
# 「params 里有 seed」（代理指标），全绿也抓不到。这里改成看**提交返回的状态**。
print("F. 重摇语义：reroll=True 不命中 / reroll=False 命中（行为断言）")
sub3 = engine.submit(project_id=pid, node_id="n_hit", runner_name="t2i",
                     params={"prompt": "同一提示词"}, input_refs={})
with SessionLocal() as db:
    a3 = Asset(kind="image", path=__file__, sha256="h")
    db.add(a3)
    db.commit()
    aid3 = a3.id
# 模拟「上一单已生成完成」：按提交侧固化的键写 marker（与执行侧写键同源）
engine_key = engine._cache_keys.get(sub3["task_id"], "")
cache.store("t2i", "", {}, {}, aid3, key=engine_key)
sub4 = engine.submit(project_id=pid, node_id="n_reroll", runner_name="t2i",
                     params={"prompt": "同一提示词"}, input_refs={})
check("reroll=True 同参数二次提交不命中（重摇出新图）",
      sub4["status"] != "cached")
sub5 = engine.submit(project_id=pid, node_id="n_hit2", runner_name="t2i",
                     params={"prompt": "同一提示词"}, input_refs={},
                     reroll=False)
check("reroll=False 同参数提交命中（切回旧参数零成本）",
      sub5["status"] == "cached")

print()
if fails:
    print("存在未通过项：", fails)
    sys.stdout.flush()
    os._exit(1)
print("整改自检全部通过")
# mock 模式下泳道 worker 会因「模型未安装」写失败日志并让进程带非零码退出，
# 这里显式以 0 退出——失败与否由上面的 fails 决定（内容不受 mock 噪音影响）。
# 注意：os._exit 不走 atexit/flush，所以退出前必须手动 flush，否则重定向到文件时
# 输出会整段丢失（实测：只拿到空文件）。
sys.stdout.flush()
os._exit(0)
