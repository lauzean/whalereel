# -*- coding: utf-8 -*-
"""Vidu 图生视频端点回归（docs/design/35-vidu-endpoint.md）。

守的底线（任何一条红了都不许合并）：
① **契约不跑偏**：提交 POST /ent/v2/img2video、轮询 GET /ent/v2/tasks/{id}/
   creations、鉴权头是 `Token {key}`（**不是 Bearer**）、产物取 creations[].url
   （无水印那条）——与 Vidu 官方接口文档逐条一致；
② **参数不外泄错值**：images 恒为 1 张（首帧本身、不压缩）、is_rec=false
   （系统组装的提示词到端点零改写）、audio=true（音画同出）、watermark=false、
   **不下发 seed**（未实测，见能力表）、时长原样透传（本仓 4-15s ⊂ 官方 3-16s）；
③ **首帧必填不降级**：没有首帧图时明确报错、**不发任何请求**（不静默改用别的
   端点、不静默降级出片）；
④ **不回退不重提**：已提交后取回失败必须带任务号（runner 侧归 BILLED，不可
   自动重试——重提 = 再买一次）；
⑤ **长任务不假死**：轮询期间按心跳周期回调进度（任务看门狗 20 分钟无进度判卡死）；
⑥ **失败看得见**：远端 state=failed 时原样透出 err_code（不猜、不翻译）。

运行：`server/.venv/Scripts/python test_vidu.py`
（数据目录用项目内 server/_tmp/test_vidu，不落系统临时目录）
"""
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TMP = _HERE / "_tmp" / "test_vidu"
shutil.rmtree(_TMP, ignore_errors=True)
_TMP.mkdir(parents=True, exist_ok=True)
os.environ["AVS_DATA_DIR"] = str(_TMP)
os.environ["AVS_MODELS_DIR"] = str(_TMP / "models")
os.environ["AVS_MOCK"] = "1"
sys.path.insert(0, str(_HERE))

from app.core import providers  # noqa: E402
from app.core.provider_store import (  # noqa: E402
    ProviderStoreError, _validate_endpoint_fields,
)
from app.core.providers import (  # noqa: E402
    FailClass, ProviderConfig, ProviderError, _build_vidu_payload, _vidu_base,
    _vidu_resolution, probe_provider_models, provider_outputs_audio, resolve_seed,
    t2v_vidu_fetch, t2v_vidu_submit, validate_video_request, video_ref_budget,
)
from app.db.session import init_db  # noqa: E402

init_db()

# 桩服务与真实客户端之间的等待节奏：压到毫秒级，测试跑得快
providers.T2V_POLL_INTERVAL_SEC = 0.05
providers._T2V_HEARTBEAT_INTERVAL_SEC = 0.05

_API_KEY = "test-vidu-key"


class StubVidu:
    """最小 Vidu 桩：记录收到的请求，由测试脚本摆放任务状态。"""

    def __init__(self):
        self.submits: list[dict] = []       # POST /ent/v2/img2video 的请求体
        self.headers: list[dict] = []       # 同上的请求头（验鉴权格式）
        self.polls = 0                      # 轮询次数
        self.states: dict[str, list[str]] = {}   # task_id -> 剩余状态序列
        self.err_code = "sensitive-content"
        self.submit_status = 200
        self.counter = 0
        self._httpd: ThreadingHTTPServer | None = None
        self.base = ""

    # ---- 生命周期 ----
    def start(self) -> "StubVidu":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静音
                pass

            def _json(self, code: int, obj) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path.endswith("/creations"):
                    tid = path.split("/tasks/")[-1].split("/")[0]
                    stub.polls += 1
                    seq = stub.states.get(tid) or ["success"]
                    state = seq.pop(0) if len(seq) > 1 else seq[0]
                    stub.states[tid] = seq
                    if state == "failed":
                        return self._json(200, {"id": tid, "state": "failed",
                                                "err_code": stub.err_code,
                                                "creations": []})
                    if state != "success":
                        return self._json(200, {"id": tid, "state": state,
                                                "err_code": "", "creations": []})
                    return self._json(200, {
                        "id": tid, "state": "success", "err_code": "", "credits": 4,
                        "creations": [{
                            "id": f"{tid}-c1",
                            "url": f"{stub.base}/files/{tid}.mp4",
                            "cover_url": f"{stub.base}/files/{tid}.jpg",
                            "watermarked_url": f"{stub.base}/files/{tid}_wm.mp4",
                        }]})
                if path.startswith("/files/"):
                    body = b"\x00\x00\x00\x18ftypmp42" + b"stub-vidu" * 8
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self._json(404, {})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.path.split("?")[0] != "/ent/v2/img2video":
                    return self._json(404, {})
                if stub.submit_status >= 400:
                    return self._json(stub.submit_status, {"message": "stub 拒绝"})
                stub.counter += 1
                tid = f"tid-{stub.counter}"
                stub.submits.append(json.loads(body.decode("utf-8")))
                # 请求头归一为小写键（httpx 可能改写大小写，逐字比对会假红）
                stub.headers.append({k.lower(): v for k, v in self.headers.items()})
                stub.states.setdefault(tid, ["created"])
                return self._json(200, {"task_id": tid, "state": "created",
                                        "model": "viduq4-preview"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    # ---- 测试助手 ----
    def cfg(self, *, base: str = "", api_key: str = _API_KEY) -> ProviderConfig:
        return ProviderConfig(provider_kind="t2v", name="vidu",
                              base_url=base or self.base, api_key=api_key)

    def set_states(self, tid: str, *states: str) -> None:
        """摆放某任务后续轮询要返回的状态序列（列表尾项此后重复）。"""
        self.states[tid] = list(states) or ["success"]


def _first_frame(name: str = "ff.png") -> str:
    """造一个假首帧文件（内容固定 → data URL 可逐字对账）。"""
    path = _TMP / name
    path.write_bytes(b"\x89PNG\r\n\x1a\nstub-first-frame")
    return str(path)




def test_submit_contract(stub: StubVidu) -> None:
    """① 提交契约：路径/鉴权头/载荷逐项对账（含 images 单张与固化开关）。"""
    from app.core import providers as P

    ff = _first_frame()
    tid = t2v_vidu_submit("画面描述正文", duration=7, width=1920, height=1080,
                          model_id="", first_frame_path=ff, cfg=stub.cfg())
    assert tid == "tid-1", tid
    payload = stub.submits[-1]
    assert set(payload) == {"model", "images", "prompt", "is_rec", "duration",
                            "resolution", "audio", "watermark"}, \
        f"请求体字段与官方契约不符：{sorted(payload)}"
    assert payload["model"] == P._VIDU_DEFAULT_MODEL, "未走默认模型 viduq4-preview"
    assert payload["is_rec"] is False, "提示词扩写必须关（组装完成的提示词零改写）"
    assert payload["audio"] is True, "音画同出开关应为开（2026-10-14 用户拍板）"
    assert payload["watermark"] is False, "水印应为关（与其它端点同口径）"
    assert "seed" not in payload, "未实测的 seed 不得下发"
    assert payload["duration"] == 7, "时长必须原样透传（本仓 4-15s 全在官方 3-16s 内）"
    assert payload["resolution"] == "1080p", payload["resolution"]
    assert len(payload["images"]) == 1, "只收 1 张图（首帧本身）"
    assert payload["images"][0].startswith("data:image/png;base64,"), \
        "首帧须为带内容类型的 base64 data URL（官方要求）"
    assert payload["prompt"] == "画面描述正文", "提示词被端点外改写（应逐字）"
    # 鉴权头是 Token 前缀（不是 Bearer），这是与其它协议的直接差别
    auth = stub.headers[-1].get("authorization", "")
    assert auth == f"Token {_API_KEY}", f"鉴权头不符：{auth!r}"
    print("[PASS] 提交契约：/ent/v2/img2video + Token 鉴权 + 单张首帧 + "
          "is_rec/audio/watermark 三开关 + 无 seed + 时长原样")


def test_first_frame_required(stub: StubVidu) -> None:
    """② 首帧必填：没有首帧时明确报错且**不发任何请求**（不静默降级）。"""
    before = len(stub.submits)
    raised = None
    try:
        t2v_vidu_submit("正文", 5, 1280, 720, cfg=stub.cfg())
    except ProviderError as e:
        raised = e
    assert raised is not None, "无首帧竟未报错"
    assert raised.fail_class == FailClass.PARAM, f"失败类别应为 param：{raised.fail_class}"
    assert "首帧" in str(raised), f"报错未说清缺什么：{raised}"
    assert len(stub.submits) == before, "无首帧却仍发出了请求（静默降级形态）"
    # 鉴权缺失同样是明确报错（不是发匿名请求）
    raised2 = None
    try:
        t2v_vidu_submit("正文", 5, 1280, 720, first_frame_path=_first_frame(),
                        cfg=stub.cfg(api_key=""))
    except ProviderError as e:
        raised2 = e
    assert raised2 is not None and "API Key" in str(raised2), "缺 Key 未报错"
    assert raised2.fail_class == FailClass.PARAM, \
        f"缺 Key 应与远端 401 同档（鉴权=配置问题）：{raised2.fail_class}"
    assert len(stub.submits) == before, "缺 Key 却仍发出了请求"
    print("[PASS] 首帧必填/缺 Key：明确报错 + 零请求（无静默降级、无匿名调用）")


def test_poll_and_fetch(stub: StubVidu) -> None:
    """③ 轮询：状态推进（created/queueing/processing→success）+ 产物取无水印 url
    + 心跳回调（长任务不假死）。"""
    ff = _first_frame("ff2.png")
    tid = t2v_vidu_submit("正文", 5, 1280, 720, first_frame_path=ff,
                          cfg=stub.cfg())
    stub.set_states(tid, "created", "queueing", "processing", "success")
    ticks: list[tuple[float, str]] = []
    data = t2v_vidu_fetch(stub.cfg(), tid, on_tick=lambda p, l: ticks.append((p, l)),
                          duration=5)
    assert data["video_url"] == f"{stub.base}/files/{tid}.mp4", \
        f"产物应取 creations[].url（无水印）：{data}"
    assert "_wm" not in data["video_url"], "取到了带水印产物（本端点 watermark=false）"
    assert stub.polls >= 4, f"状态推进未逐次轮询：{stub.polls}"
    assert ticks, "轮询期间无心跳回调（长任务会被看门狗误判卡死）"
    assert all(0 < p <= 1 for p, _l in ticks), f"心跳进度越界：{ticks}"
    print("[PASS] 轮询与取回：状态推进 + 无水印产物 + 心跳回调")


def test_failure_paths(stub: StubVidu) -> None:
    """④ 失败看得见：远端 failed 原样透出 err_code；超时与取消各有明确出口。"""
    ff = _first_frame("ff3.png")
    tid = t2v_vidu_submit("正文", 5, 1280, 720, first_frame_path=ff,
                          cfg=stub.cfg())
    stub.set_states(tid, "processing", "failed")
    raised = None
    try:
        t2v_vidu_fetch(stub.cfg(), tid)
    except ProviderError as e:
        raised = e
    assert raised is not None, "远端 failed 未抛错"
    assert stub.err_code in str(raised), f"未透出 err_code：{raised}"
    # err_code 原文进错误信息后由**既有**特征词表分类：含 sensitive 归内容安全
    # （不占创作重摇预算）。这是共享表的作用，不是对 Vidu 错误码的知识——
    # 真实 err_code 若不在特征词表里，分类回落 unknown（不猜语义）。
    assert raised.fail_class == FailClass.CONTENT_SAFE, \
        f"err_code 含 sensitive 应归内容安全：{raised.fail_class}"
    # 取消：cancel_check 为真时中止（不把远端任务当成功）
    tid2 = t2v_vidu_submit("正文", 5, 1280, 720, first_frame_path=ff,
                           cfg=stub.cfg())
    stub.set_states(tid2, "processing")
    raised2 = None
    try:
        t2v_vidu_fetch(stub.cfg(), tid2, cancel_check=lambda: True)
    except ProviderError as e:
        raised2 = e
    assert raised2 is not None and "取消" in str(raised2), f"取消未中止：{raised2}"
    # 超时：把总时限压到毫秒级，验证超时出口
    old_timeout = providers.T2V_POLL_TIMEOUT_SEC
    providers.T2V_POLL_TIMEOUT_SEC = 0.2
    try:
        tid3 = t2v_vidu_submit("正文", 5, 1280, 720, first_frame_path=ff,
                               cfg=stub.cfg())
        stub.set_states(tid3, "processing")
        raised3 = None
        try:
            t2v_vidu_fetch(stub.cfg(), tid3)
        except ProviderError as e:
            raised3 = e
        assert raised3 is not None and "超时" in str(raised3), f"超时未报错：{raised3}"
    finally:
        providers.T2V_POLL_TIMEOUT_SEC = old_timeout
    print("[PASS] 失败三出口：远端 failed 透出 err_code（归内容安全）+ 取消中止 + 超时明确")


def test_capability_and_guardrails() -> None:
    """⑤ 能力表与护栏：单图预算、时长域、画幅不拦、种子不下发、类型限制。"""
    from app.api.drama import _video_size
    from app.core.contracts import resolution_tier

    # 三条固定映射（短边档 → Vidu 枚举）
    assert _vidu_resolution(1280, 720) == "720p"
    assert _vidu_resolution(1920, 1080) == "1080p"
    assert _vidu_resolution(854, 480) == "540p", "480p 档应就近上取 540p"
    # 项目 6 画幅 × 3 档全组合：映射必须与契约层档位判定同源、且落在官方枚举内
    for aspect in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"):
        for tier in ("480p", "720p", "1080p"):
            w, h = _video_size(aspect, tier)
            got = _vidu_resolution(w, h)
            assert got == providers._VIDU_RESOLUTION_TIERS[resolution_tier(w, h)], \
                f"{aspect}@{tier}（{w}×{h}）映射为 {got}，与契约层档位判定不同源"
            assert got in ("540p", "720p", "1080p", "2K", "4K"), got
    # 参考图预算恒 0（端点只收首帧那张）；超预算的额外参考图会被校验拦下
    assert video_ref_budget("vidu", "viduq4-preview") == 0, "额外面数应为 0"
    assert video_ref_budget("vidu", "viduq4-preview", has_first_frame=True) == 0
    assert validate_video_request("vidu", duration=15, aspect="16:9",
                                  ref_count=0, has_first_frame=True) == [], \
        "15 秒（本仓上限）应通过"
    assert validate_video_request("vidu", duration=3, aspect="21:9",
                                  ref_count=0, has_first_frame=True) == [], \
        "3 秒（官方下限）与超宽画幅都应通过（本端点不接收画幅参数）"
    problems = validate_video_request("vidu", duration=5, aspect="16:9",
                                      ref_count=1, has_first_frame=True)
    assert problems, "额外参考图未被拦（端点只收 1 张图）"
    # 种子：未实测 → 不下发、不声称可复现
    seed, note = resolve_seed("vidu", 12345)
    assert seed is None and "种子" in note, f"种子应不下发并说明：{seed} {note}"
    # 音轨如实记录（按**实测**登记，不是按「除 comfyui 外都没有」的旧结论）：
    # vidu 音画同出；agnes/方舟抽检产物都带真实音轨（2026-10-14 修正）；
    # 未实测的 custom 与本地推理保持 False（不声称）
    assert provider_outputs_audio("vidu") is True, "音画同出应记 has_audio=True"
    assert provider_outputs_audio("comfyui") is True, "本地出片（H3 工作流）带音轨"
    assert provider_outputs_audio("agnes") is True and \
        provider_outputs_audio("ark") is True, \
        "agnes/方舟产物自带音轨（抽检实证），不得再写死 False"
    assert provider_outputs_audio("custom") is False and \
        provider_outputs_audio("local") is False, "未实测/本地推理不应被当成有音轨"
    probed = probe_provider_models("vidu", "t2v", "https://api.vidu.cn", "k")
    assert [m["id"] for m in probed["models"]] == ["viduq4-preview"], probed
    print("[PASS] 能力与护栏：分辨率档映射全组合 + 参考图预算 0 + 时长域 3-16 + "
          "种子不下发 + has_audio 如实 + 探测精选表")


def test_endpoint_registration() -> None:
    """⑥ 端点登记：协议×类型限制（只许视频类）+ base_url 兼容两种填法。"""
    assert _validate_endpoint_fields("t2v", "vidu", "https://api.vidu.cn", "")[1] == "vidu"
    assert _validate_endpoint_fields("i2v", "vidu", "https://api.vidu.cn/", "")[0] == "i2v"
    raised = None
    try:
        _validate_endpoint_fields("t2i", "vidu", "https://api.vidu.cn", "")
    except ProviderStoreError as e:
        raised = e
    assert raised is not None, "vidu 建 t2i 端点未被拦（要跑到生成时才炸）"
    # base_url：主机根与已带 /ent/v2 的填写都要拼出同一条提交路径
    root = _vidu_base(ProviderConfig(provider_kind="t2v", name="vidu",
                                     base_url="https://api.vidu.cn/"))
    prefixed = _vidu_base(ProviderConfig(provider_kind="t2v", name="vidu",
                                         base_url="https://api.vidu.cn/ent/v2/"))
    assert root == prefixed == "https://api.vidu.cn", (root, prefixed)
    print("[PASS] 端点登记：只许 t2v/i2v + base_url 两种填法同源")


def test_payload_builder() -> None:
    """⑦ 组装函数：载荷构建与提交路径同源（门禁对账用的那一份）。"""
    ff = _first_frame("ff4.png")
    payload = _build_vidu_payload("P", 9, 1280, 720, "viduq4-preview", ff)
    assert payload["is_rec"] is False and payload["audio"] is True
    assert payload["watermark"] is False and "seed" not in payload
    assert payload["resolution"] == "720p" and payload["duration"] == 9
    print("[PASS] 载荷构建同源：开关固化项与提交段共用一份")


def main() -> None:
    stub = StubVidu().start()
    try:
        test_submit_contract(stub)
        test_first_frame_required(stub)
        test_poll_and_fetch(stub)
        test_failure_paths(stub)
        test_capability_and_guardrails()
        test_endpoint_registration()
        test_payload_builder()
    finally:
        stub.stop()
    print("\nVIDU ENDPOINT PASS")


if __name__ == "__main__":
    main()
