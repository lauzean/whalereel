# -*- coding: utf-8 -*-
"""ComfyUI 本地模型服务端点回归（docs/design/34-comfyui-local-endpoint.md）。

守的底线（任何一条红了都不许合并）：
① **契约不跑偏**：提交是整份工作流（POST /prompt）、轮询看 /history「没有该 id
   = 未完成」、产物在 outputs 的 images 键下（视频也在 images，实测如此）、
   下载走 GET /view——与 docs/references/comfyui-api 的实测文档逐条一致；
② **编号对账**：第 N 张图挂 ref_image_{N-1}、提示词里**字面写** `<Picture N>`
   （N = 图片参数下标 0 起 + 1），首帧挂末位并前置「起始画面」声明；组装侧
   （drama）与提交侧（providers）用同一函数幂等改写——**参数弹窗与调用日志的
   提示词必须逐字一致**（2026-10-10 用户实测发现的问题形态）；
③ **参数不外泄错值**：帧数吸附 n%17==5（3/5/15 秒 → 73/124/362 帧）、宽高吸附
   32 的倍数、像素封顶 1.0MP 且缩回后不越界；
④ **不回退不重提**：已提交后取回失败必须带任务号且不可自动重试（BILLED）；
⑤ **长任务不假死**：轮询期间按心跳周期回调进度（任务看门狗 20 分钟无进度判卡死）；
⑥ **取消有边界**：只对「队列里确属本任务」的条目中断/删除，不误伤别人的任务。

运行：`server/.venv/Scripts/python test_comfyui.py`
（数据目录用项目内 server/_tmp/test_comfyui，不落系统临时目录）
"""
import json
import os
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TMP = _HERE / "_tmp" / "test_comfyui"
shutil.rmtree(_TMP, ignore_errors=True)
_TMP.mkdir(parents=True, exist_ok=True)
os.environ["AVS_DATA_DIR"] = str(_TMP)
os.environ["AVS_MODELS_DIR"] = str(_TMP / "models")
os.environ["AVS_MOCK"] = "1"
sys.path.insert(0, str(_HERE))

from app.core import comfyui  # noqa: E402
from app.core.provider_store import (  # noqa: E402
    ProviderStoreError, _validate_endpoint_fields,
)
from app.core.providers import (  # noqa: E402
    FailClass, ProviderConfig, ProviderError, _comfyui_model, _frame_declaration,
    download_video, resolve_seed, t2v_comfyui_fetch, t2v_comfyui_submit,
    validate_video_request, video_ref_budget,
)
from app.db.session import init_db  # noqa: E402

init_db()

# 桩服务与真实客户端之间的等待节奏：压到毫秒级，测试跑得快
comfyui.POLL_INTERVAL_SEC = 0.05
comfyui.HEARTBEAT_INTERVAL_SEC = 0.05


class StubComfyUI:
    """最小 ComfyUI 桩：记录收到的请求，由测试脚本摆放 /history 与队列状态。"""

    def __init__(self):
        self.uploads: list[dict] = []      # 上传记录（顺序即挂载顺序）
        self.submits: list[dict] = []      # POST /prompt 的完整请求体
        self.history: dict = {}            # prompt_id -> /history 条目
        self.running: list[str] = []       # 队列-运行中
        self.pending: list[str] = []       # 队列-排队中
        self.interrupts = 0
        self.deleted: list[str] = []
        self.upload_status = 200
        self.counter = 0
        self._httpd: ThreadingHTTPServer | None = None
        self.base = ""

    # ---- 生命周期 ----
    def start(self) -> "StubComfyUI":
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
                if path.startswith("/history/"):
                    pid = path.rsplit("/", 1)[-1]
                    entry = stub.history.get(pid)
                    return self._json(200, {pid: entry} if entry else {})
                if path == "/queue":
                    return self._json(200, {
                        "queue_running": [[0, p] for p in stub.running],
                        "queue_pending": [[0, p] for p in stub.pending]})
                if path == "/system_stats":
                    return self._json(200, {"system": {"comfyui_version": "0.39.0-stub"}})
                if path == "/view":
                    body = b"\x00\x00\x00\x18ftypmp42" + b"stub-video" * 8
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
                path = self.path.split("?")[0]
                if path == "/upload/image":
                    if stub.upload_status >= 400:
                        return self._json(stub.upload_status, {"error": "stub"})
                    m = re.search(rb'filename="([^"]*)"', body)
                    name = m.group(1).decode("utf-8", "replace") if m else "x.png"
                    needle = b'name="subfolder"\r\n\r\n'
                    i = body.find(needle)
                    sub = ""
                    if i >= 0:
                        j = body.find(b"\r\n", i + len(needle))
                        sub = body[i + len(needle):j].decode("utf-8", "replace")
                    stub.uploads.append({"filename": name, "subfolder": sub})
                    return self._json(200, {"name": name, "subfolder": sub,
                                            "type": "input"})
                if path == "/prompt":
                    payload = json.loads(body.decode("utf-8"))
                    stub.counter += 1
                    pid = f"pid-{stub.counter}"
                    stub.submits.append(payload)
                    stub.pending.append(pid)
                    return self._json(200, {"prompt_id": pid, "number": stub.counter})
                if path == "/queue":
                    data = json.loads(body.decode("utf-8")) if body else {}
                    for pid in data.get("delete") or []:
                        stub.deleted.append(pid)
                        if pid in stub.pending:
                            stub.pending.remove(pid)
                    return self._json(200, {})
                if path == "/interrupt":
                    stub.interrupts += 1
                    return self._json(200, {})
                return self._json(404, {})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    # ---- 测试助手 ----
    def cfg(self) -> ProviderConfig:
        return ProviderConfig(provider_kind="t2v", name="comfyui", base_url=self.base)

    def done(self, pid: str, *, filename: str = "avs_x_00001_.mp4",
             subfolder: str = "avs") -> None:
        """把一个任务标记为成功完成（/history 出现该 id）。"""
        if pid in self.pending:
            self.pending.remove(pid)
        self.history[pid] = {
            "outputs": {"15": {"images": [
                {"filename": filename, "subfolder": subfolder, "type": "output"}]}},
            "status": {"status_str": "success", "completed": True, "messages": []}}


def _ref_image(name: str, data: bytes = b"png-bytes") -> str:
    """造一个假参考图文件（内容不同 → 上传名不同，用于验证哈希命名）。"""
    path = _TMP / name
    path.write_bytes(data + name.encode("utf-8"))
    return str(path)


def test_limits() -> None:
    """① 帧数与分辨率：吸附规则、封顶不越界、模板装载。"""
    assert comfyui.duration_to_frames(3) == 73, "3 秒应吸附到 73 帧"
    assert comfyui.duration_to_frames(5) == 124, "5 秒应吸附到 124 帧"
    assert comfyui.duration_to_frames(15) == 362, "15 秒应吸附到 362 帧"
    for f in (comfyui.duration_to_frames(s) for s in range(3, 16)):
        assert f % comfyui.FRAME_MODULO == comfyui.FRAME_REMAINDER, f
    # 分辨率：864×480 是文档实测档（原样保留）；854×480 吸附（同 ResolutionSelector）；
    # 1920×1080 缩回封顶（向下吸附，绝不越 1.0MP）
    assert comfyui.plan_size(864, 480) == (864, 480, ""), "实测档不应被改动"
    assert comfyui.plan_size(854, 480)[:2] == (864, 480)
    assert comfyui.plan_size(1280, 720)[:2] == (1280, 736)
    for w, h in ((1920, 1080), (3840, 2160), (2520, 1080), (1080, 1920)):
        pw, ph, note = comfyui.plan_size(w, h)
        assert pw * ph <= comfyui.MAX_PIXELS, f"{w}×{h} 缩回后仍越界：{pw}×{ph}"
        assert pw % 32 == 0 and ph % 32 == 0 and note, f"{w}×{h} 吸附不合法"
    assert comfyui.plan_size(1920, 1080)[:2] == (1312, 736), "1.0MP 档应为文档同款"
    # 模板：可列出、可装载、元数据键被剥掉
    items = comfyui.list_workflows()
    assert any(x["id"] == comfyui.DEFAULT_WORKFLOW_ID for x in items), items
    wf = comfyui.load_workflow(comfyui.DEFAULT_WORKFLOW_ID)
    assert all(not str(k).startswith("_") for k in wf), "元数据键未剥掉"
    for cls in (comfyui.CLASS_VIDEO, comfyui.CLASS_SCHEDULER,
                comfyui.CLASS_NOISE, comfyui.CLASS_SAVE):
        comfyui._find_node(wf, cls)  # 缺节点会抛 ComfyWorkflowError
    print("[PASS] 帧数/分辨率吸附（73/124/362、32 倍数、1.0MP 封顶不越界）+ 模板装载")


def test_picture_placeholders() -> None:
    """② 占位口径：<Picture N>（N = 图片下标 0 起 +1）字面写出 + 幂等 + 空图不改写。"""
    # 组装侧（drama）先改一遍：两种标注形态（带名括号 / 裸标注）都要命中
    src = ("【全局定义】素材：沈青梧（参考图1）、老周面馆 夜（参考图2）。\n"
           "【视频画面】00—02秒：沈青梧缓步前行。\n"
           "第 5 秒起画面切换为与参考图2完全一致的人物与构图")
    out, changed = comfyui.apply_picture_labels(src)
    assert changed == 3, f"应命中 3 处编号，实为 {changed}：{out}"
    assert "参考图" not in out, f"仍有中文编号残留：{out}"
    for n in (1, 2):
        assert f"<Picture {n}>" in out, out
    # 幂等：再改一遍不变（提交侧会再调一次，必须零副作用）
    assert comfyui.apply_picture_labels(out) == (out, 0), "占位改写不幂等"
    # 尖括号边界防呆：已是「<参考图N>」形态不再套一层（宁可不动，不许改坏）
    assert comfyui.apply_picture_labels("<参考图1>") == ("<参考图1>", 0), \
        "尖括号包裹的形态被套了两层"
    # 起始画面声明：用共享句（agnes 同一份 _frame_declaration），已有则不重复
    fin = comfyui.finalize_prompt(out, picture_total=3, has_first_frame=True)
    assert fin.startswith(_frame_declaration(3)), "起始画面声明未用共享句"
    assert comfyui.finalize_prompt(fin, picture_total=3, has_first_frame=True) == fin, \
        "finalize 不幂等（提交侧会再调一次，重复声明会稀释注意力）"
    assert comfyui.finalize_prompt(out, picture_total=2, has_first_frame=False) == out, \
        "无首帧时不应添加声明"
    print("[PASS] 占位口径：<Picture N> 字面写出（两种形态命中）+ 幂等 + 声明共用")


def test_submit_shape(stub: StubComfyUI) -> None:
    """③ 提交契约：ref_image_N 对账、提示词与组装侧逐字一致、种子与前缀。"""
    ref1 = _ref_image("a.png", b"AAA")
    ref2 = _ref_image("b.png", b"BBB")
    ff = _ref_image("ff.png", b"FFF")
    seed = 20261010
    # 模拟 drama 组装侧：先用公开函数改写（弹窗记录的就是它），再交给提交侧
    raw = ("【全局定义】素材：沈青梧（参考图1）、老城楼 夜（参考图2）。\n"
           "沈青梧（参考图1）缓步前行，衣料与场景参考图2的光线一致")
    assembled = comfyui.finalize_prompt(raw, picture_total=3, has_first_frame=True)
    pid = t2v_comfyui_submit(
        assembled, 3, 864, 480, "", first_frame_path=ff,
        ref_paths=[ref1, ref2], seed=seed, run_tag="task-abc", cfg=stub.cfg())
    assert pid == "pid-1" and stub.submits, "提交未到达桩服务"
    # 上传顺序 = 参考图在前、首帧挂末位
    assert [u["subfolder"] for u in stub.uploads] == ["avs"] * 3, stub.uploads
    names = [u["filename"] for u in stub.uploads]
    assert len(set(names)) == 3 and all(n.endswith(".png") for n in names), names
    # 挂载名 = 服务端返回的「子目录/文件名」（与参考客户端同口径）
    mounted = [f"{u['subfolder']}/{u['filename']}" for u in stub.uploads]
    payload = stub.submits[0]
    wf = payload["prompt"]
    assert all(not str(k).startswith("_") for k in wf), "元数据键混进了请求"
    video = comfyui._find_node(wf, comfyui.CLASS_VIDEO)["inputs"]
    # 编号对账：第 N 张图 = ref_image_{N-1}（下标 0 起）= 提示词 <Picture N>
    for i in range(3):
        node_id = video[f"ref_images.ref_image_{i}"][0]
        assert wf[node_id]["class_type"] == "LoadImage", wf[node_id]
        assert wf[node_id]["inputs"]["image"] == mounted[i], \
            "挂到的不是上传返回的文件名"
    assert video["width"] == 864 and video["height"] == 480
    assert video["length"] == 73, "时长 3 秒应换算成 73 帧"
    # **核心性质：组装侧走过的提示词，提交侧不得再动一个字**
    # （参数弹窗与调用日志逐字一致的根因就在这里）
    assert video["prompt"] == assembled, "提交侧又改了提示词（弹窗与调用日志会不一致）"
    assert "参考图" not in video["prompt"], "中文编号未改成 <Picture N> 占位"
    assert "<Picture 3> 是本镜头的起始画面" in video["prompt"], "首帧未声明为起始画面"
    noise = comfyui._find_node(wf, comfyui.CLASS_NOISE)["inputs"]
    assert noise["noise_seed"] == seed, "种子未下发（模板固定值会出同片）"
    save = comfyui._find_node(wf, comfyui.CLASS_SAVE)["inputs"]
    assert save["filename_prefix"] == "avs/task-abc", save
    assert payload["model"] == comfyui.DEFAULT_WORKFLOW_ID, "调用日志模型名缺失"
    assert payload["duration"] == 3, "计费量纲（秒）缺失"
    # 画布入口（用户自写提示词，未经组装侧）：提交侧兜底改写，但**无图不改写**
    # （改写会造出指向不存在图片的悬空 <Picture N>）
    pid2 = t2v_comfyui_submit("参考图1里的角色抬头（画布直发）", 3, 864, 480, "",
                              first_frame_path=ff, ref_paths=None, cfg=stub.cfg())
    p2 = comfyui._find_node(stub.submits[1]["prompt"], comfyui.CLASS_VIDEO)["inputs"]
    assert "<Picture 1>里的角色抬头" in p2["prompt"], p2["prompt"]
    assert "<Picture 1> 是本镜头的起始画面" in p2["prompt"], "画布路径缺起始画面声明"
    pid3 = t2v_comfyui_submit("参考图1里的角色抬头（无图）", 3, 864, 480, "",
                              cfg=stub.cfg())
    p3 = comfyui._find_node(stub.submits[2]["prompt"], comfyui.CLASS_VIDEO)["inputs"]
    assert "参考图1" in p3["prompt"] and "<Picture" not in p3["prompt"], \
        "无图时不应造出悬空占位"
    assert pid2 and pid3
    print("[PASS] 提交契约：编号对账 + 提示词与组装侧逐字一致（弹窗=调用日志）"
          "+ 画布兜底改写且无图不改写 + 种子/前缀/量纲齐备")


def test_poll_and_fetch(stub: StubComfyUI) -> None:
    """③ 轮询契约：未完成不返回、心跳按周期触发、完成后取回 /view 地址。"""
    calls: list[tuple[float, str]] = []
    pid = "pid-poll"
    threading.Timer(0.25, lambda: stub.done(pid)).start()
    t0 = time.time()
    data = t2v_comfyui_fetch(stub.cfg(), pid, duration=5,
                             on_tick=lambda p, s: calls.append((p, s)))
    elapsed = time.time() - t0
    assert elapsed >= 0.2, f"未等任务完成就返回（{elapsed:.2f}s）"
    assert calls and calls[0][0] <= comfyui.PROGRESS_RUNNING_CAP, calls
    url = str(data["video_url"])
    assert url.startswith(f"{stub.base}/view?"), url
    assert "filename=avs_x_00001_.mp4" in url and "subfolder=avs" in url, url
    content = download_video(url)
    assert content.startswith(b"\x00\x00\x00\x18ftyp"), "产物不是 MP4 头"
    print(f"[PASS] 轮询取回：等待 {elapsed:.2f}s、心跳 {len(calls)} 次、/view 下载成功")


def test_failure_paths(stub: StubComfyUI) -> None:
    """④ 失败与超时：报错原文透出、分类确定性、超时带任务号。"""
    pid = "pid-fail"
    stub.history[pid] = {
        "outputs": {},
        "status": {"status_str": "error", "completed": True, "messages": [
            ["execution_error", {"node_type": "MiniMaxH3ReferenceToVideo",
                                 "exception_message": "CUDA out of memory"}]]}}
    try:
        t2v_comfyui_fetch(stub.cfg(), pid)
        raise AssertionError("失败任务应抛错")
    except ProviderError as e:
        assert "CUDA out of memory" in str(e) and "MiniMaxH3ReferenceToVideo" in str(e)
        assert getattr(e, "fail_class", "") == FailClass.PARAM, "执行失败应为确定性错误"
    # 超时：把总超时压到极小，验证超时文案带任务号（人工可回捞）
    old = comfyui.POLL_TIMEOUT_SEC
    comfyui.POLL_TIMEOUT_SEC = 0.15
    try:
        t2v_comfyui_fetch(stub.cfg(), "pid-timeout", duration=15)
        raise AssertionError("超时应抛错")
    except ProviderError as e:
        assert "pid-timeout" in str(e) and "超时" in str(e)
    finally:
        comfyui.POLL_TIMEOUT_SEC = old
    # 缺产物：完成但没有输出文件
    stub.history["pid-empty"] = {"outputs": {}, "status": {"status_str": "success"}}
    try:
        t2v_comfyui_fetch(stub.cfg(), "pid-empty")
        raise AssertionError("无产物应抛错")
    except ProviderError as e:
        assert "未找到产物" in str(e)
    print("[PASS] 失败路径：报错原文透出 + 确定性分类 + 超时/缺产物可诊断")


def test_cancel_scope(stub: StubComfyUI) -> None:
    """⑤ 取消边界：排队的删队列、运行的才中断，且只认本任务的编号。"""
    # 场景一：本任务在排队 → 删除，不中断（不能掐掉正在跑的别人的任务）
    stub.pending = ["pid-mine", "pid-other"]
    stub.running = ["pid-someone-else"]
    try:
        t2v_comfyui_fetch(stub.cfg(), "pid-mine", cancel_check=lambda: True)
        raise AssertionError("取消应抛错")
    except ProviderError as e:
        assert "已取消" in str(e)
    assert stub.deleted == ["pid-mine"] and stub.interrupts == 0, \
        (stub.deleted, stub.interrupts)
    assert "pid-other" in stub.pending, "别人的排队任务被误删"
    # 场景二：本任务在运行 → 中断；队列里没有别的任务被动
    stub.pending = []
    stub.running = ["pid-mine"]
    try:
        t2v_comfyui_fetch(stub.cfg(), "pid-mine", cancel_check=lambda: True)
        raise AssertionError("取消应抛错")
    except ProviderError:
        pass
    assert stub.interrupts == 1 and stub.deleted == ["pid-mine"], \
        (stub.interrupts, stub.deleted)
    # 场景三：任务已不在队列（已完成）→ 什么都不动
    stub.running = ["pid-other"]
    try:
        t2v_comfyui_fetch(stub.cfg(), "pid-gone", cancel_check=lambda: True)
    except ProviderError:
        pass
    assert stub.interrupts == 1 and "pid-other" in stub.running, "误伤了队列里的其他任务"
    print("[PASS] 取消边界：排队删队列 / 运行才中断 / 不误伤他人任务")


def test_capability_and_guardrails(stub: StubComfyUI) -> None:
    """⑥ 能力表与防呆：校验放行、预算按首帧占位、种子自动补、端点类型限制。"""
    assert validate_video_request("comfyui", duration=3, aspect="16:9",
                                  ref_count=8, has_first_frame=True) == [], "合法参数被拦"
    problems = validate_video_request("comfyui", duration=3, aspect="16:9",
                                      ref_count=9, has_first_frame=True)
    assert problems, "超 8 张额外参考图（首帧占 1 位）应拦截"
    assert validate_video_request("comfyui", duration=16, aspect="16:9",
                                  ref_count=0, has_first_frame=False), "16 秒应拦截"
    assert video_ref_budget("comfyui") == 8, "有首帧时额外参考图预算应为 8"
    assert video_ref_budget("comfyui", has_first_frame=False) == 9
    seed, note = resolve_seed("comfyui", "")
    assert isinstance(seed, int) and 0 <= seed < 2**31 and "随机种子" in note, \
        (seed, note)
    assert resolve_seed("agnes", "") == (None, ""), "其它端点不应被连带改成自动补种子"
    assert _comfyui_model(stub.cfg(), "") == comfyui.DEFAULT_WORKFLOW_ID
    # 端点登记校验：comfyui 只许建视频类，且地址须 http(s)
    assert _validate_endpoint_fields("t2v", "comfyui", "http://127.0.0.1:8000/", "") \
        == ("t2v", "comfyui", "http://127.0.0.1:8000", "")
    try:
        _validate_endpoint_fields("t2i", "comfyui", "http://127.0.0.1:8000", "")
        raise AssertionError("comfyui 建 t2i 端点应被拒")
    except ProviderStoreError as e:
        assert "不支持" in str(e)
    # 防呆：绕过 plan_size 直接提交非法宽高/超量参考图 → 明确报错，不发请求
    before = len(stub.submits)
    for kwargs in ({"width": 854, "height": 480}, {"width": 1600, "height": 1600}):
        try:
            comfyui.submit_video(stub.base, comfyui.DEFAULT_WORKFLOW_ID,
                                 prompt="x", duration=3, image_paths=[], **kwargs)
            raise AssertionError("非法分辨率应被防呆拦下")
        except ProviderError as e:
            assert "plan_size" in str(e)
    try:
        comfyui.submit_video(stub.base, comfyui.DEFAULT_WORKFLOW_ID, prompt="x",
                             duration=3, width=864, height=480,
                             image_paths=["/tmp/x.png"] * 10)
        raise AssertionError("超量参考图应被拦下")
    except ProviderError as e:
        assert "上限" in str(e)
    assert len(stub.submits) == before, "防呆失败时不应发出请求"
    print("[PASS] 能力表/防呆：校验放行、预算占位、种子自补、类型限制、非法参数拦在本地")


def test_upload_failure(stub: StubComfyUI) -> None:
    """⑦ 上传失败=未提交：报错可重试（SERVER 非 BILLED），且不产生提交。"""
    stub.upload_status = 500
    before = len(stub.submits)
    try:
        t2v_comfyui_submit("x", 3, 864, 480, "", ref_paths=[_ref_image("u.png", b"U")],
                           cfg=stub.cfg())
        raise AssertionError("上传失败应抛错")
    except ProviderError as e:
        assert getattr(e, "fail_class", "") == FailClass.SERVER, e
    assert len(stub.submits) == before, "上传失败后不应提交"
    stub.upload_status = 200
    print("[PASS] 上传失败：明确抛错、未提交（不会成为「已提交不可重试」）")


def test_probe(stub: StubComfyUI) -> None:
    """⑧ 探测：连通时列出内置模板；连不上给具体原因而不是空列表。"""
    r = comfyui.probe(stub.base)
    assert r["source"] == "comfyui" and r["models"], r
    assert r["models"][0]["id"] == comfyui.DEFAULT_WORKFLOW_ID, r
    assert "0.39.0-stub" in r.get("version", ""), r
    bad = comfyui.probe("http://127.0.0.1:1")
    assert bad["models"] == [] and bad["source"] == "none" and bad.get("error"), bad
    print("[PASS] 探测：模板清单 + 版本号；连不上返回具体原因（管理页可提示）")


def main() -> None:
    stub = StubComfyUI().start()
    try:
        test_limits()
        test_picture_placeholders()
        test_submit_shape(stub)
        test_poll_and_fetch(stub)
        test_failure_paths(stub)
        test_cancel_scope(stub)
        test_capability_and_guardrails(stub)
        test_upload_failure(stub)
        test_probe(stub)
    finally:
        stub.stop()
    print("\nCOMFYUI ENDPOINT PASS")


if __name__ == "__main__":
    main()
