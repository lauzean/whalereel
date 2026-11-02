"""只读排查：最近任务状态 + 各项目画布/版本/资产关联。"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB = Path(__file__).resolve().parent / "data" / "studio.db"
conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
now = int(time.time())

print("=== 最近 12 条任务 ===")
rows = conn.execute(
    "SELECT id, project_id, node_id, runner, model_id, status, error, progress, "
    "created_at, started_at, finished_at FROM tasks ORDER BY created_at DESC LIMIT 12"
).fetchall()
for r in rows:
    age = now - r["created_at"]
    print(f"[{r['status']}] {r['runner']}/{r['model_id']} node={r['node_id']} "
          f"progress={r['progress']} age={age//60}min err={str(r['error'])[:70]}")
    if r["status"] in ("queued", "running", "loading_model") and age > 600:
        print(f"    -> 卡住任务: {r['id']} 已 {age//60} 分钟")

print()
print("=== 活跃任务（非终态） ===")
for r in conn.execute(
    "SELECT id, runner, status, progress, created_at, started_at FROM tasks "
    "WHERE status IN ('queued','running','loading_model') ORDER BY created_at"
):
    print(f"{r['id'][:8]} {r['runner']} {r['status']} p={r['progress']} "
          f"age={ (now - r['created_at'])//60 }min")

print()
print("=== 项目列表 ===")
projects = conn.execute(
    "SELECT id AS project_id, name, mode, created_at FROM projects ORDER BY created_at"
).fetchall()
for p in projects:
    print(f"{p['project_id'][:8]} {p['name']} mode={p['mode']}")

print()
print("=== 画布节点 vs 任务 node_id ===")
for p in projects:
    pid = p["project_id"]
    g = conn.execute(
        "SELECT graph_json FROM canvases WHERE project_id=?", (pid,)
    ).fetchone()
    nodes = json.loads(g["graph_json"]).get("nodes", []) if g and g["graph_json"] else []
    task_nodes = [
        r["node_id"] for r in conn.execute(
            "SELECT DISTINCT node_id FROM tasks WHERE project_id=?", (pid,)
        )
    ]
    versions = [
        r["node_id"] for r in conn.execute(
            "SELECT DISTINCT node_id FROM node_versions WHERE project_id=?", (pid,)
        )
    ]
    print(f"「{p['name']}」画布节点={len(nodes)} 任务节点={task_nodes} 版本节点={versions}")
    for n in nodes:
        print(f"  画布: {n.get('id')[:16]} type={n.get('type')}")

print()
print("=== node_versions 最近 12 条 ===")
for r in conn.execute(
    "SELECT node_id, project_id, artifact_id, created_at FROM node_versions "
    "ORDER BY created_at DESC LIMIT 12"
):
    print(f"node={r['node_id']} artifact={r['artifact_id']}")

print()
print("=== assets 最近 8 条 ===")
for r in conn.execute(
    "SELECT id, kind, path, created_at FROM assets ORDER BY created_at DESC LIMIT 8"
):
    print(f"{r['kind']} {r['path'][:90]}")
conn.close()
