# -*- coding: utf-8 -*-
"""媒体签名（方案 A）回归：产物访问不再「谁拿到 URL 谁就能看」。

背景：`<img>`/`<video>` 发请求带不了 Authorization 头，所以产物文件不能用登录
门禁保护，改走**签名临时链接**（`?e=<过期>&s=<签名>`）。本脚本守三件事：签名
不可篡改/不可过期复用、路径穿越被拒、严格模式开关行为正确。

覆盖：
A. 签名/校验：正确签名通过，篡改资源标识/篡改签名/过期/缺参数一律拒绝；
B. 路径归一化：`..` 被拒、绝对路径剥成相对、`resolve_within` 越界返回 None；
C. 全链路：登录 → POST /api/artifacts/sign 拿签名地址 → 带签名取文件 200；
   未登录签发 401；
D. 严格模式（AVS_MEDIA_REQUIRE_SIGNATURE=1）：无签名 403、带签名 200、
   关掉后向后兼容；
E. /files 路由：正常相对路径 200、穿越路径 404、严格模式需签名。

运行：`server/.venv/Scripts/python server/test_media_signature.py`
"""
import os
import sys
import tempfile
import time

_tmp = tempfile.mkdtemp(prefix="avs_signprobe_")
os.environ["AVS_DATA_DIR"] = _tmp
os.environ["AVS_MODELS_DIR"] = os.path.join(_tmp, "models")
os.environ["AVS_MOCK"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import media_sign                      # noqa: E402
from app.core.config import settings                 # noqa: E402
from app.core.security import hash_password, new_token  # noqa: E402
from app.db.models import Asset, AuthToken, Setting, User  # noqa: E402
from app.db.session import SessionLocal, init_db     # noqa: E402
from app.main import app                             # noqa: E402

init_db()
fails: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  OK   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


# 造一个真实产物文件 + 资产行
prod = os.path.join(settings.artifacts_dir, "probe_media.txt")
with open(prod, "w", encoding="utf-8") as fh:
    fh.write("hello-media")
with SessionLocal() as db:
    asset = Asset(kind="image", path=prod, sha256="z")
    db.add(asset)
    db.commit()
    aid = asset.id

print("A. 签名与校验")
url = media_sign.artifact_url(aid, 3600)
check("artifact_url 带 e= 与 s=", "?e=" in url and "&s=" in url)
exp = url.split("e=")[1].split("&")[0]
sig = url.split("s=")[1]
check("正确签名通过", media_sign.verify(media_sign.artifact_resource(aid), exp, sig))
check("篡改资源标识被拒",
      not media_sign.verify(media_sign.artifact_resource(other), exp, sig)
      if (other := aid + "x") else False)
check("篡改签名被拒",
      not media_sign.verify(media_sign.artifact_resource(aid), exp, sig[:-2] + "aa"))
check("过期签名被拒", not media_sign.verify(
    media_sign.artifact_resource(aid), str(int(time.time()) - 7200),
    media_sign.make_signature(media_sign.artifact_resource(aid),
                              int(time.time()) - 7200)))
check("缺参数被拒", not media_sign.verify(media_sign.artifact_resource(aid), None, None))

print("B. 路径归一化")
check(".. 被拒", media_sign.normalize_rel_path("../../etc/passwd") == "")
check("内嵌 .. 被拒", media_sign.normalize_rel_path("a/../../b") == "")
check("绝对路径被剥成相对", media_sign.normalize_rel_path("/a/b.txt") == "a/b.txt")
check("反斜杠统一", media_sign.normalize_rel_path("a\\b.txt") == "a/b.txt")
check("resolve_within 越界返回 None",
      media_sign.resolve_within(settings.artifacts_dir, "../outside.txt") is None)

print("C. 前后端链路（登录 → 签发 → 取文件）")
tok = new_token()
with SessionLocal() as db:
    u = User(username="probe", password_hash=hash_password("probe123"), is_admin=True)
    db.add(u)
    db.flush()
    db.add(AuthToken(token=tok, user_id=u.id, created_at=int(time.time()),
                     expires_at=int(time.time()) + 3600))
    db.commit()
hdr = {"Authorization": f"Bearer {tok}"}
client = TestClient(app)
r = client.post("/api/artifacts/sign", json={"artifact_ids": [aid]}, headers=hdr)
check("签发接口 200", r.status_code == 200)
signed = (r.json().get("artifacts") or {}).get(aid, "")
check("返回里含签名地址", signed.startswith(f"/api/artifacts/{aid}/file?e="))
r2 = client.get(signed)
check("带签名取文件 200", r2.status_code == 200)
check("文件内容正确", r2.text == "hello-media")
r3 = client.post("/api/artifacts/sign", json={"artifact_ids": [aid]})
check("未登录签发被拒（401）", r3.status_code == 401)

print("D. 产物文件：无签名无令牌 401、带签名 200；严格模式下签名是唯一凭证")
settings.media_require_signature = True
check("无签名无令牌 401（全局鉴权闸）",
      client.get(f"/api/artifacts/{aid}/file").status_code == 401)
check("严格模式：带令牌但无签名 403（签名是唯一凭证）",
      client.get(f"/api/artifacts/{aid}/file", headers=hdr).status_code == 403)
check("严格模式：带签名 200（媒体标签无需令牌）", client.get(signed).status_code == 200)
settings.media_require_signature = False
check("宽松模式：带令牌 200（登录用户可直接取）",
      client.get(f"/api/artifacts/{aid}/file", headers=hdr).status_code == 200)
check("宽松模式：既无签名也无令牌仍 401（不能借白名单裸奔）",
      client.get(f"/api/artifacts/{aid}/file").status_code == 401)

print("E. /files 路由（1050 个旧产物走这里）")
legacy = os.path.join(settings.artifacts_dir, "legacy.txt")
with open(legacy, "w", encoding="utf-8") as fh:
    fh.write("legacy")
check("/files 带令牌 200", client.get("/files/legacy.txt", headers=hdr).status_code == 200)
check("/files 无令牌无签名 401", client.get("/files/legacy.txt").status_code == 401)
furl = media_sign.file_url("legacy.txt", 3600)
check("file_url 生成签名地址", bool(furl) and "?e=" in furl)
check("/files 带签名 200（媒体标签路径）", client.get(furl).status_code == 200)
check("/files 穿越路径 404（带令牌也不放行）",
      client.get("/files/..%2F..%2Fmain.py", headers=hdr).status_code == 404)
settings.media_require_signature = True
check("严格模式 /files 无签名无令牌 401", client.get("/files/legacy.txt").status_code == 401)
check("严格模式 /files 带签名 200", client.get(furl).status_code == 200)
settings.media_require_signature = False

print()
if fails:
    print("存在未通过项：", fails)
    sys.stdout.flush()
    os._exit(1)
print("媒体签名自检全部通过")
sys.stdout.flush()
os._exit(0)
