# -*- coding: utf-8 -*-
"""ComfyUI 本地服务客户端（模型服务端点的 comfyui 协议实现）。

ComfyUI 的接口契约与云端各家（ark/agnes/custom）完全不同，按实测文档实现
（docs/references/comfyui-api/DMAD_H3_API_文档.md，本机 0.39.0 验证）：

- 提交：POST /prompt，请求体是**整份工作流 JSON**（不是 {prompt,duration} 参数），
  返回 {"prompt_id"}；校验失败返回 HTTP 400 且 node_errors 里给具体节点；
- 轮询：GET /history/{prompt_id}——**任务未完成时该 id 不出现**在响应里
  （不是 status:pending）；失败时 status.status_str == "error"，报错原文藏在
  status.messages 的 execution_error.exception_message；
- 产物：outputs[节点]["images"][] 给 filename/subfolder/type（**视频也在 images
  键下**，实测如此），下载走 GET /view?filename=&subfolder=&type=；
- 参考图：先 POST /upload/image（multipart）拿到服务端文件名，再动态挂 LoadImage
  节点到 ref_images.ref_image_N。

工作流模板放在 app/core/comfyui_workflows/（内置随代码走，加模板 = 往目录里放
一个 JSON 文件，不用改代码）；端点里的「模型」字段填模板名（文件名去扩展名）。
模板顶部可带 "_meta"（label = 管理页显示名），提交前会被剥掉——ComfyUI 不认识
这个键，混进去会让工作流校验报「未知节点」。

参考图编号（来自实测文档 §3，**2026-10-10 用户实测纠正**）：
节点输入键 ref_image_0..8（**0 起**）↔ 提示词占位 <Picture 1..9>（**1 起**），
即第 i 张图挂 ref_image_{i-1}、提示词里写 <Picture i>（N = 图片参数下标 + 1）。
提示词里必须**字面写出** `<Picture N>` 占位，只加一句「（参考图N）对应
<Picture N>」的说明句不符合模型要求——由 `finalize_prompt` 在**组装侧**改写
落字（组装侧先改，参数弹窗与调用日志才是同一份提示词）。
挂载顺序由调用方给定（参考图在前、首帧挂末位并声明为起始画面，与文档示例
「2 个角色 + 末位起始帧」一致）。

耗时与超时（文档实测，RTX 4090 24GB / 0.4MP）：15 秒视频约 7 分钟、5 秒约 1
分钟；首次调用要加载 11GB 底模 + 4.9GB 编码器，冷启动约 3 分钟——轮询总超时
取 3600 秒（冷启动 + 长片 + 排队都在预算内）。轮询期间每 30 秒推一次进度心跳：
任务看门狗按「20 分钟无任何进度」判卡死，心跳是长任务不被误杀的保命线。
"""

import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# 工作流模板目录（随代码内置；加模板 = 放一个 JSON 文件）
WORKFLOWS_DIR = Path(__file__).resolve().parent / "comfyui_workflows"
# 模板元数据键前缀（"_meta" 等；提交前剥掉，见模块 docstring）
META_KEY_PREFIX = "_"
# 端点「模型」字段留空时的默认模板
DEFAULT_WORKFLOW_ID = "minimax_h3_dmad_r2v"
# 模板名白名单（端点「模型」字段来自请求，防路径穿越：只允许字母数字下划线连字符）
WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")

# ---------- 帧数与分辨率（H3 硬约束，文档 §4.2 / §7） ----------

# 输出帧率：模型固定 24fps（CreateVideo 节点同值，模板里不改）
FPS = 24
# 帧数约束：length 必须满足 n % 17 == 5，否则模型内部自行吸附、时间码对不上
FRAME_MODULO = 17
FRAME_REMAINDER = 5
MIN_FRAMES = 5
# 官方训练区间（约 5.2~15.1 秒）：超出不拦（官方标 untested），仅作参考
TRAINED_MIN_FRAMES = 124
TRAINED_MAX_FRAMES = 362

# 宽高必须是 32 的倍数（文档 §4.2）
RESOLUTION_MULTIPLE = 32
# 像素量封顶（2026-10-10 用户拍板：跟随项目分辨率档、封顶 1.0MP——
# 文档只实测过 0.4MP 档，再高属外推，24G 显存长片有爆显存风险）
MAX_PIXELS = 1_000_000

# 参考图上限：ref_image_0..8 ↔ <Picture 1..9>
MAX_REF_IMAGES = 9

# ---------- 超时与轮询 ----------

POLL_INTERVAL_SEC = 3.0        # /history 轮询间隔（与参考客户端同值，查询是幂等 GET）
# 总超时：2026-10-10 实测修正——本机 H3 出片**单条可超 90 分钟**（GPU 满负载
# 持续推进、非卡死：nvidia-smi 100%、心跳正常），旧的 60 分钟上限把真在生成的
# 产物判死、还连锁误杀队列里的下一条。真卡死由任务心跳看门狗兜（fetch 每 30 秒
# 有 tick，卡死即无 tick）——总超时只防「永远排不上队的僵尸」，放到 4 小时。
POLL_TIMEOUT_SEC = 14400.0
SUBMIT_TIMEOUT_SEC = 60.0      # POST /prompt
UPLOAD_TIMEOUT_SEC = 300.0     # POST /upload/image
QUEUE_TIMEOUT_SEC = 30.0       # GET /queue
POLL_GET_TIMEOUT_SEC = 30.0    # GET /history、GET /view 的单次超时

# 进度心跳间隔（秒）：见模块 docstring——看门狗按 20 分钟无心跳判卡死
HEARTBEAT_INTERVAL_SEC = 30.0
# 预估总耗时（进度条参考值）：文档实测 15 秒 ≈ 420 秒 → 约 27 秒/秒视频。
# 只用于把进度条从 10% 缓慢推到 85%，标签里写明「参考耗时」，不假装精确。
EST_SEC_PER_VIDEO_SEC = 27.0
EST_MIN_SEC = 60.0
PROGRESS_QUEUED = 0.05
PROGRESS_RUNNING_START = 0.10
PROGRESS_RUNNING_SPAN = 0.75
PROGRESS_RUNNING_CAP = 0.85

# 上传参考图的子目录名（与用户手放进 input/ 的文件隔离，避免同名互相覆盖）
UPLOAD_SUBFOLDER = "avs"
# 输出文件前缀命名空间（产物落 ComfyUI 的 output/avs/ 子目录）
OUTPUT_PREFIX_ROOT = "avs"
# 输出前缀里允许的字符（远端任务号是 uuid hex，清洗只为防御）
_TAG_RE = re.compile(r"[^A-Za-z0-9_\-]")

# ---------- 节点定位（按 class_type 找，模板重编号也不受影响） ----------

# 需要注入参数的核心节点类（缺任何一个 → 明确报错，不静默乱挂）
CLASS_VIDEO = "MiniMaxH3ReferenceToVideo"   # 提示词/宽高/帧数/参考图
CLASS_SCHEDULER = "BasicScheduler"          # 步数（模板值 = DMAD 4 步 LoRA，不改）
CLASS_NOISE = "RandomNoise"                 # noise_seed（每次必须显式下发）
CLASS_SAVE = "SaveVideo"                    # filename_prefix（产物可追溯到任务）
CLASS_LOAD_IMAGE = "LoadImage"              # 参考图节点（每次提交前重建）
# 产物输出键：视频也放在 images 键下（实测，见文档 §6.2）
OUTPUT_ITEM_KEYS = ("images", "videos", "gifs")


class ComfyWorkflowError(Exception):
    """工作流模板本身的问题（缺文件/缺节点/非 H3 模板）——提交前就该发现。"""


# ---------------------------------------------------------------------------
# 模板装载
# ---------------------------------------------------------------------------

def list_workflows() -> list[dict]:
    """扫描模板目录，返回 [{"id": 模板名, "display_name": 管理页显示名}]。

    display_name 取模板 "_meta.label"（没有则用文件名），坏 JSON 跳过并告警
    （不进列表 = 管理页选不到，比列出来再在生成时炸更早暴露问题）。
    """
    items: list[dict] = []
    if not WORKFLOWS_DIR.exists():
        return items
    for path in sorted(WORKFLOWS_DIR.glob("*.json")):
        label = ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            meta = data.get(f"{META_KEY_PREFIX}meta") if isinstance(data, dict) else None
            if isinstance(meta, dict):
                label = str(meta.get("label") or "").strip()
        except (OSError, ValueError) as e:
            logger.warning("ComfyUI 工作流模板不可用，已跳过：%s（%s）", path.name, e)
            continue
        items.append({"id": path.stem, "display_name": label or path.stem})
    return items


def load_workflow(workflow_id: str) -> dict:
    """按模板名加载工作流 JSON；剥掉 "_" 前缀的元数据键。

    模板名不在白名单格式内 / 文件不存在 / JSON 坏 → 抛 ComfyWorkflowError
    （中文说明可用模板），由调用方转成 ProviderError 抛出。
    """
    wid = (workflow_id or "").strip() or DEFAULT_WORKFLOW_ID
    if not WORKFLOW_ID_RE.match(wid):
        raise ComfyWorkflowError(
            f"工作流模板名不合法：{workflow_id!r}（只允许字母数字下划线连字符）")
    path = WORKFLOWS_DIR / f"{wid}.json"
    if not path.exists():
        available = "、".join(w["id"] for w in list_workflows()) or "（无）"
        raise ComfyWorkflowError(
            f"工作流模板不存在：{wid}（目录里现有：{available}）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ComfyWorkflowError(f"工作流模板读取失败：{wid}（{e}）") from e
    if not isinstance(data, dict):
        raise ComfyWorkflowError(f"工作流模板格式错误（应为节点字典）：{wid}")
    return {k: v for k, v in data.items() if not str(k).startswith(META_KEY_PREFIX)}


def _find_node(workflow: dict, class_type: str) -> dict:
    """按类名找节点（同类多次出现取节点号最小者）；找不到抛 ComfyWorkflowError。"""
    matches = [(nid, node) for nid, node in workflow.items()
               if isinstance(node, dict) and node.get("class_type") == class_type]
    if not matches:
        raise ComfyWorkflowError(
            f"工作流模板缺少 {class_type} 节点，无法注入参数——本端点的 comfyui "
            "协议当前只支持 MiniMax-H3 参考生视频模板（如 minimax_h3_dmad_r2v）")

    def _order(key: str) -> tuple[int, str]:
        """节点号排序：纯数字按数值、其余按文本（模板里编号混杂时也稳定）。"""
        return (int(key), "") if key.isdigit() else (10**9, str(key))

    return min(matches, key=lambda kv: _order(kv[0]))[1]


# ---------------------------------------------------------------------------
# 帧数与分辨率换算（纯函数，提交前由 runner 调用并把说明外露给用户）
# ---------------------------------------------------------------------------

def duration_to_frames(seconds: float) -> int:
    """时长（秒）→ 合法帧数（按 24fps 换算后向上吸附到 n % 17 == 5）。"""
    n = max(MIN_FRAMES, int(round(float(seconds) * FPS)))
    return n + (FRAME_REMAINDER - n % FRAME_MODULO) % FRAME_MODULO


def _snap_side(value: int, *, floor_only: bool = False) -> int:
    """吸附到 32 的倍数：默认四舍五入（与 ComfyUI ResolutionSelector 同口径：
    0.4MP + 16:9 得 864×480 而不是 832×448，见文档 §4.2）；floor_only=True
    时只向下取（封顶路径用——向上取会把缩回的结果又顶出像素上限）。
    """
    step = RESOLUTION_MULTIPLE
    if floor_only:
        return max(step, int(value // step) * step)
    return max(step, int((value + step / 2) // step) * step)


def plan_size(width: int, height: int) -> tuple[int, int, str]:
    """请求宽高 → H3 可用的 (宽, 高, 说明文本)。

    规则（确定性，无模型判断）：
    ① 每边吸附到 32 的倍数（模型硬约束；四舍五入对齐 ComfyUI 惯例）；
    ② 总像素超 1.0MP 时按比例缩回再**向下**吸附（封顶是用户拍板值，
       向上吸附会压不住上限——1920×1080 缩回时会算出 1344×768 = 1.03MP）。
    说明文本非空时由调用方推给用户（「参数被系统改过」必须看得见）。
    """
    w, h = _snap_side(int(width)), _snap_side(int(height))
    if w * h > MAX_PIXELS:
        scale = (MAX_PIXELS / (w * h)) ** 0.5
        w2 = _snap_side(int(w * scale), floor_only=True)
        h2 = _snap_side(int(h * scale), floor_only=True)
        note = (f"分辨率 {int(width)}×{int(height)} 超出本端点像素上限"
                f"（{MAX_PIXELS / 1_000_000:.1f}MP），已缩到 {w2}×{h2}")
        return w2, h2, note
    if (w, h) != (int(width), int(height)):
        note = (f"分辨率已吸附到 {RESOLUTION_MULTIPLE} 的倍数（模型硬约束）："
                f"{int(width)}×{int(height)} → {w}×{h}")
        return w, h, note
    return w, h, ""


# ---------------------------------------------------------------------------
# 提示词占位（<Picture N>）——H3/Qwen3-VL 认的是提示词里**字面写出**的占位
# ---------------------------------------------------------------------------

# 我们自己的参考图编号 token（`_resolve_video_prompt` 两遍法分配的「（参考图N）」
# 与切点句里的裸「参考图N」）。只改编号 token 本身，不触碰其它文字。
# 前后加尖括号边界防呆：已经是「<参考图N>」形态的不再套一层（改写只会发生在
# 我们自己生成的形态上，边界外的一律不动——宁可不改，不改坏）。
PICTURE_LABEL_RE = re.compile(r"(?<!<)参考图(\d+)(?!>)")
# 占位格式：N = 给 ComfyUI 的图片参数下标（**0 起**）+ 1，即 ref_image_0 ↔ <Picture 1>
PICTURE_LABEL_FMT = "<Picture {n}>"
# 起始画面声明的识别锚（幂等判据：已有该声明就不再重复添加）
FRAME_DECL_RE = re.compile(r"<Picture \d+> 是本镜头的起始画面")


def apply_picture_labels(prompt: str) -> tuple[str, int]:
    """把提示词里的「参考图N」改成字面 `<Picture N>` 占位，返回 (新提示词, 改动处数)。

    这是**确定性格式归一**（改的是我们自己生成的编号 token，不做语义判断），
    重复调用幂等。编号口径：N = 图片参数下标（0 起）+ 1——挂载顺序是
    「参考图在前、首帧挂末位」，所以提示词里的「参考图N」与 <Picture N> 天然
    同号（与参考文档 §3 的示例「2 个角色 + 末位起始帧」一致）。
    """
    text, changed = PICTURE_LABEL_RE.subn(
        lambda m: PICTURE_LABEL_FMT.format(n=int(m.group(1))), prompt or "")
    return text, changed


def finalize_prompt(prompt: str, *, picture_total: int, has_first_frame: bool) -> str:
    """ComfyUI 端点的**最终提示词**（幂等）：占位改写 + 起始画面声明。

    为什么做成幂等：提示词组装侧（drama，写参数弹窗的那一份）与提交侧
    （providers）都调用它——组装侧先改，弹窗与调用日志才是同一份文字（2026-10-10
    用户实测发现两处不一致：此前只在提交前加说明句，弹窗里没有）。
    已有起始画面声明时不再重复添加（编号以本次挂载为准，声明文案与 agnes
    共用 `providers._frame_declaration`，单一来源）。
    """
    text, _ = apply_picture_labels(prompt)
    if has_first_frame and not FRAME_DECL_RE.search(text):
        from app.core.providers import _frame_declaration  # 局部导入避免循环依赖

        text = _frame_declaration(picture_total) + text
    return text


# ---------------------------------------------------------------------------
# HTTP 基础（错误分类与 providers._post_json_inner 同口径：
# 连不上 = NETWORK 可重试；连上了但响应异常 = 不自动重试）
# ---------------------------------------------------------------------------

def _provider_error(message: str, fail_class: str, url: str) -> Exception:
    """构造 providers.ProviderError（局部导入避免与 providers 循环依赖）。"""
    from app.core.providers import ProviderError

    return ProviderError(message, fail_class)


def _http_error(url: str, e: Exception, what: str) -> Exception:
    """httpx 异常 → ProviderError（分类口径与 providers._post_json_inner 一致）。"""
    from app.core.providers import FailClass

    if isinstance(e, httpx.ConnectError):
        return _provider_error(f"{what}服务连接失败（{url}）：{e}", FailClass.NETWORK, url)
    if isinstance(e, httpx.TimeoutException):
        return _provider_error(f"{what}请求超时（{url}）", FailClass.TIMEOUT, url)
    return _provider_error(f"{what}请求中断（{url}）：{e}", FailClass.BILLED, url)


# ---------------------------------------------------------------------------
# 参考图上传
# ---------------------------------------------------------------------------

def upload_image(base_url: str, path: str) -> str:
    """上传本地图片到 ComfyUI 的 input/avs/ 子目录，返回可挂 LoadImage 的文件名。

    文件名 = 内容哈希前 16 位 + 原扩展名：同一张图重复上传幂等覆盖、不同图
    绝不重名——多任务并发时若按原名上传，「0.png」互相覆盖会把 A 镜的图挂到
    B 镜上（产物错脸且事后无从对账）。上传失败抛 ProviderError（未提交阶段，
    连不上归 NETWORK 可重试），失败才写调用日志（成功不写：一次生成最多 9 张，
    逐张记账会把真正的模型调用记录淹掉，与轮询 GET 同一口径）。
    """
    from app.core.providers import FailClass, _record_model_call

    url = f"{base_url}/upload/image"
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise _provider_error(f"参考图读取失败：{path}（{e}）", FailClass.PARAM, "") from e
    digest = hashlib.sha1(data).hexdigest()[:16]
    name = f"{digest}{Path(path).suffix.lower() or '.png'}"
    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            url,
            files={"image": (name, data, "application/octet-stream")},
            data={"subfolder": UPLOAD_SUBFOLDER, "overwrite": "true"},
            timeout=UPLOAD_TIMEOUT_SEC)
    except httpx.HTTPError as e:
        err = _http_error(url, e, "ComfyUI 参考图上传")
        _record_model_call("ComfyUI 参考图上传", url, {"image": name}, ok=False,
                           elapsed_ms=(time.perf_counter() - t0) * 1000,
                           fail_class=getattr(err, "fail_class", ""), error=str(err))
        raise err from e
    if resp.status_code >= 400:
        err = _provider_error(
            f"ComfyUI 参考图上传失败（HTTP {resp.status_code}）：{resp.text[:200]}",
            FailClass.SERVER, url)
        _record_model_call("ComfyUI 参考图上传", url, {"image": name}, ok=False,
                           elapsed_ms=(time.perf_counter() - t0) * 1000,
                           fail_class=FailClass.SERVER, error=str(err),
                           http_status=resp.status_code)
        raise err
    try:
        result = resp.json()
    except ValueError as e:
        raise _provider_error(f"ComfyUI 参考图上传响应非 JSON：{e}",
                              FailClass.NETWORK, url) from e
    sub = str(result.get("subfolder") or "")
    filename = str(result.get("name") or name)
    return f"{sub}/{filename}" if sub else filename


# ---------------------------------------------------------------------------
# 提交 / 取回
# ---------------------------------------------------------------------------

def submit_video(base_url: str, workflow_id: str, *, prompt: str, width: int,
                 height: int, duration: int, image_paths: list[str] | None = None,
                 seed: int | None = None, run_tag: str = "") -> str:
    """提交一次本地出片，返回 ComfyUI 的 prompt_id（远端任务号）。

    image_paths 的顺序即图片参数顺序（ref_image_0..N-1 ↔ 提示词 <Picture 1..N>），
    调用方负责：参考图在前、首帧挂末位、**prompt 已过 `finalize_prompt`**
    （占位字面写成 <Picture N>）。宽高必须已是 32 的倍数且 ≤1.0MP（先过
    plan_size）——这里做防呆断言而非再算一遍，字段被改了要报错而不是悄悄改第二遍。
    """
    from app.core.providers import FailClass, _post_json

    if not base_url:
        raise _provider_error("ComfyUI 服务地址未配置", FailClass.PARAM, "")
    if width % RESOLUTION_MULTIPLE or height % RESOLUTION_MULTIPLE:
        raise _provider_error(
            f"分辨率 {width}×{height} 不是 {RESOLUTION_MULTIPLE} 的倍数"
            "（H3 硬约束）：提交前应先经 plan_size 吸附", FailClass.PARAM, "")
    if width * height > MAX_PIXELS:
        raise _provider_error(
            f"分辨率 {width}×{height} 超出本端点像素上限 {MAX_PIXELS // 1_000_000}MP："
            "提交前应先经 plan_size 缩回", FailClass.PARAM, "")
    paths = list(image_paths or [])
    if len(paths) > MAX_REF_IMAGES:
        raise _provider_error(
            f"参考图 {len(paths)} 张超出本模型上限 {MAX_REF_IMAGES} 张（接线错误："
            "上游已按端点能力截断）", FailClass.PARAM, "")

    workflow = load_workflow(workflow_id)
    video_node = _find_node(workflow, CLASS_VIDEO)
    noise_node = _find_node(workflow, CLASS_NOISE)
    save_node = _find_node(workflow, CLASS_SAVE)
    _find_node(workflow, CLASS_SCHEDULER)  # 存在性校验（步数随 LoRA，模板自带）

    # 每次提交重建参考图接线：先清模板里既有的 LoadImage 与 ref_images.* 槽位
    inputs = video_node.setdefault("inputs", {})
    for key in [k for k in inputs if str(k).startswith("ref_images.")]:
        inputs.pop(key)
    for nid in [n for n, node in list(workflow.items())
                if isinstance(node, dict)
                and node.get("class_type") == CLASS_LOAD_IMAGE]:
        workflow.pop(nid)
    # 动态 LoadImage 节点号：从模板现有最大数字编号 +1 起，绝不与模板撞号
    base_id = max([int(k) for k in workflow if str(k).isdigit()], default=0) + 1
    for index, path in enumerate(paths):
        name = upload_image(base_url, path)
        node_id = str(base_id + index)
        workflow[node_id] = {"class_type": CLASS_LOAD_IMAGE,
                             "inputs": {"image": name}}
        inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]

    inputs["prompt"] = prompt
    inputs["width"] = int(width)
    inputs["height"] = int(height)
    inputs["length"] = duration_to_frames(duration)
    if seed is not None:
        # 种子必须下发：模板里的 noise_seed 是固定值，不下发等于每次同片
        noise_node.setdefault("inputs", {})["noise_seed"] = int(seed)
    tag = _TAG_RE.sub("", str(run_tag or ""))[:48]
    save_node.setdefault("inputs", {})["filename_prefix"] = (
        f"{OUTPUT_PREFIX_ROOT}/{tag}" if tag else OUTPUT_PREFIX_ROOT)

    client_id = uuid.uuid4().hex
    # 顶层 "model"/"duration" 是给调用日志与计费量纲用的附加键（ComfyUI 只读
    # prompt/client_id 等已知键，未知键被忽略——实测 0.39.0）
    payload = {"prompt": workflow, "client_id": client_id,
               "model": workflow_id, "duration": int(duration)}
    data = _post_json(f"{base_url}/prompt", payload, {}, SUBMIT_TIMEOUT_SEC,
                      "ComfyUI 提交")
    prompt_id = str(data.get("prompt_id") or "")
    if not prompt_id:
        raise _provider_error(
            f"ComfyUI 提交响应缺少 prompt_id：{str(data)[:200]}",
            FailClass.SERVER, f"{base_url}/prompt")
    logger.info("ComfyUI 已提交 prompt_id=%s 模板=%s 帧数=%s 参考图=%d 张",
                prompt_id, workflow_id,
                duration_to_frames(duration), len(paths))
    return prompt_id


def _history_entry(base_url: str, prompt_id: str) -> dict | None:
    """查一次 /history：完成（成功或失败）返回该条目，未完成返回 None。

    未完成的状态表现是「响应里没有这个 id」（实测），不是 status:pending。
    """
    from app.core.providers import _poll_get_json

    data = _poll_get_json(f"{base_url}/history/{prompt_id}", {}, "ComfyUI 任务状态")
    entry = data.get(prompt_id)
    return entry if isinstance(entry, dict) else None


def _execution_error(status: dict) -> str:
    """从 status.messages 里取 execution_error 的报错原文（取不到给兜底文本）。"""
    for message in status.get("messages") or []:
        if (isinstance(message, (list, tuple)) and len(message) >= 2
                and message[0] == "execution_error"
                and isinstance(message[1], dict)):
            detail = message[1]
            node = detail.get("node_type") or detail.get("node_id") or ""
            text = str(detail.get("exception_message") or "").strip()
            return f"节点 {node}：{text}" if node else text
    return "ComfyUI 未给出报错详情（可在 ComfyUI 界面查看历史任务）"


def _parse_done(entry: dict, base_url: str, prompt_id: str) -> dict:
    """完成条目 → {"video_url"}（失败抛 ProviderError）。"""
    from app.core.providers import FailClass

    status = entry.get("status") or {}
    if str(status.get("status_str") or "") == "error":
        # 工作流执行失败是确定性错误（参数/显存不够等），不自动重试；
        # 又因已提交，调用方按「不回退、不重提」处理
        raise _provider_error(
            f"ComfyUI 出片失败（{prompt_id}）：{_execution_error(status)}",
            FailClass.PARAM, "")
    for out in (entry.get("outputs") or {}).values():
        if not isinstance(out, dict):
            continue
        for key in OUTPUT_ITEM_KEYS:
            for item in out.get(key) or []:
                filename = str((item or {}).get("filename") or "")
                if not filename:
                    continue
                query = urlencode({
                    "filename": filename,
                    "subfolder": str(item.get("subfolder") or ""),
                    "type": str(item.get("type") or "output"),
                })
                return {"video_url": f"{base_url}/view?{query}",
                        "remote_task_id": prompt_id}
    raise _provider_error(
        f"ComfyUI 任务完成但未找到产物文件（{prompt_id}）："
        "请检查模板是否接到 SaveVideo 节点", FailClass.PARAM, "")


def _queue_note(base_url: str, prompt_id: str) -> str:
    """本任务在 ComfyUI 队列里的位置说明；已在运行/查不到返回空串。

    查队列只为心跳标签更准确，失败不影响轮询（吞掉异常返回空）。
    """
    from app.core.providers import _poll_get_json

    try:
        q = _poll_get_json(f"{base_url}/queue", {}, "ComfyUI 队列")
    except Exception:  # noqa: BLE001 队列查询失败不影响主流程
        return ""
    running = [str(it[1]) for it in (q.get("queue_running") or [])
               if isinstance(it, (list, tuple)) and len(it) > 1]
    pending = [str(it[1]) for it in (q.get("queue_pending") or [])
               if isinstance(it, (list, tuple)) and len(it) > 1]
    if prompt_id in running:
        return ""
    if prompt_id in pending:
        return f"前方 {pending.index(prompt_id)} 个任务"
    return ""


def _tick(elapsed: float, est_sec: float, queued_note: str) -> tuple[float, str]:
    """心跳 (进度值, 标签)：排队 5%；生成中按「已等待 ÷ 参考耗时」推进、封顶 85%。

    时长只能估（文档实测 15 秒 ≈ 7 分钟），所以标签写明「参考耗时」，不假装精确。
    """
    if queued_note:
        return PROGRESS_QUEUED, (f"本地出片排队中（{queued_note}），"
                                 f"已等待 {int(elapsed // 60)} 分钟")
    ratio = min(1.0, elapsed / max(EST_MIN_SEC, est_sec))
    progress = PROGRESS_RUNNING_START + PROGRESS_RUNNING_SPAN * ratio
    return min(PROGRESS_RUNNING_CAP, progress), (
        f"本地 ComfyUI 生成中：已等待 {int(elapsed // 60)} 分 "
        f"{int(elapsed % 60)} 秒（参考耗时约 {max(1, int(est_sec // 60))} 分钟）")


def cancel_remote(base_url: str, prompt_id: str) -> None:
    """取消远端任务：排队中 → 从队列删除；正在运行 → 中断当前执行。

    只在**队列里确实是我们这个 prompt_id** 时才动手——/interrupt 打断的是
    「当前正在跑的那个」，不做归属检查会把别人的任务一起掐掉。失败只告警
    （取消是尽力而为，本地服务重启后队列本来就空）。
    """
    try:
        q = httpx.get(f"{base_url}/queue", timeout=QUEUE_TIMEOUT_SEC).json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("ComfyUI 取消时查队列失败（忽略）：%s", e)
        return
    running = [str(it[1]) for it in (q.get("queue_running") or [])
               if isinstance(it, (list, tuple)) and len(it) > 1]
    pending = [str(it[1]) for it in (q.get("queue_pending") or [])
               if isinstance(it, (list, tuple)) and len(it) > 1]
    endpoint, body = "", None
    if prompt_id in running:
        endpoint, body = "/interrupt", {}
    elif prompt_id in pending:
        endpoint, body = "/queue", {"delete": [prompt_id]}
    if not endpoint:
        return  # 已完成/已清掉：不动（可能另一实例已处理）
    try:
        httpx.post(f"{base_url}{endpoint}", json=body, timeout=QUEUE_TIMEOUT_SEC)
        logger.info("ComfyUI 已取消任务 %s（%s）", prompt_id, endpoint)
    except httpx.HTTPError as e:
        logger.warning("ComfyUI 取消请求失败（忽略）：%s", e)


def fetch_result(base_url: str, prompt_id: str, *, duration: int = 0,
                 cancel_check=None, on_tick=None) -> dict:
    """轮询 /history 直到出片，返回 {"video_url"}（可重入，重复调用幂等）。

    - 每 POLL_INTERVAL_SEC 查一次；完成即返回、失败即抛（含报错原文）；
    - 每 HEARTBEAT_INTERVAL_SEC 调 on_tick(进度, 标签)：任务看门狗按「20 分钟
      无任何进度」判卡死，长任务靠它保命（标签里带队列位置与已等待时长）；
    - cancel_check() 为 True 时取消远端任务并抛 ProviderError（调用方转「已取消」）；
    - 总超时 POLL_TIMEOUT_SEC，超时抛 ProviderError（附 prompt_id 供人工在
      ComfyUI 里查看——产物可能仍在输出目录生成）。
    """
    from app.core.providers import FailClass

    started = time.monotonic()
    deadline = started + POLL_TIMEOUT_SEC
    est_sec = max(EST_MIN_SEC, EST_SEC_PER_VIDEO_SEC * max(0, int(duration)))
    last_tick = 0.0
    while True:
        if cancel_check is not None and cancel_check():
            cancel_remote(base_url, prompt_id)
            raise _provider_error(f"本地出片任务已取消（ComfyUI {prompt_id}）",
                                  FailClass.PARAM, "")
        entry = _history_entry(base_url, prompt_id)
        if entry is not None:
            return _parse_done(entry, base_url, prompt_id)
        elapsed = time.monotonic() - started
        if on_tick is not None and elapsed - last_tick >= HEARTBEAT_INTERVAL_SEC:
            last_tick = elapsed
            progress, label = _tick(elapsed, est_sec,
                                    _queue_note(base_url, prompt_id))
            on_tick(progress, label)
        if time.monotonic() >= deadline:
            raise _provider_error(
                f"ComfyUI 出片超时未完成（{prompt_id}，上限 "
                f"{int(POLL_TIMEOUT_SEC // 60)} 分钟）：产物可能仍在生成或已在"
                "输出目录产生，可稍后在 ComfyUI 界面查看", FailClass.TIMEOUT, "")
        time.sleep(POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# 连通性探测（模型管理页「获取模型列表」）
# ---------------------------------------------------------------------------

def probe(base_url: str, timeout: float = 10.0) -> dict:
    """探测 ComfyUI 服务：GET /system_stats 验连通 + 列出内置工作流模板。

    返回 {"models": [{id, display_name}], "source": "comfyui"}；连不上返回
    source="none" + error 文案（管理页据此提示，而不是给一个空列表）。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return {"models": [], "source": "none", "error": "未填写服务地址"}
    try:
        resp = httpx.get(f"{base}/system_stats", timeout=timeout)
    except httpx.HTTPError as e:
        return {"models": [], "source": "none",
                "error": f"连接 ComfyUI 失败：{e}"}
    if resp.status_code >= 400:
        return {"models": [], "source": "none",
                "error": f"ComfyUI 返回 HTTP {resp.status_code}（地址是否正确？）"}
    try:
        version = str(((resp.json().get("system") or {})
                       .get("comfyui_version")) or "")
    except ValueError:
        version = ""
    models = list_workflows()
    logger.info("ComfyUI 探测成功：版本 %s，%d 个内置模板", version or "未知", len(models))
    return {"models": models, "source": "comfyui", "version": version}
