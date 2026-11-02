"""模型后台下载器 + 推理依赖安装。

模型下载（对齐设计文档 3.6）：
- 下载源（对齐设计文档 3.6）：
- ModelScope 官方源优先（国内官方高速），未注册/不可达时回退 HF 镜像
- AVS_DOWNLOAD_SOURCE=auto|modelscope|hf|mirror 可强制指定（默认 auto）
- 基于 huggingface_hub.snapshot_download，天然支持断点续传（已下载块跳过）
- 落盘目录 = models_dir / 模型 ID（与 default_loader / installed_spec_ids 路径一致）
- 后台线程下载 + 独立进度线程统计目录大小，内存态维护进度，供 API 轮询
- 单模型互斥（同一模型并发发起时返回已在进行）；磁盘空间预检
- 镜像源：AVS_HF_ENDPOINT（默认 https://hf-mirror.com，国内直连友好，可改官方）

推理依赖安装：
- detect 缺失推理依赖（torch/diffusers/transformers/accelerate/safetensors）
- 后台 pip install -e .[inference]（针对本项目），进度/日志内存态保留
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import PROJECT_ROOT, settings
from app.core.model_manager import get_spec

logger = logging.getLogger(__name__)

# 下载源端点（AVS_HF_ENDPOINT 可覆盖 HF 镜像；改为 https://huggingface.co 即官方）
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"
HF_OFFICIAL_ENDPOINT = "https://huggingface.co"
MS_ENDPOINT = "https://www.modelscope.cn"

# 推理依赖缺失项（供检测与一键安装提示）
REQUIRED_INFERENCE_DEPS = (
    ("diffusers", "diffusers"),
    ("transformers", "transformers"),
    ("accelerate", "accelerate"),
    ("safetensors", "safetensors"),
    ("sentencepiece", "sentencepiece"),
    ("cv2", "opencv-python"),
)

# 下载器全局单例
_downloader: "ModelDownloader | None" = None


def get_downloader() -> "ModelDownloader":
    global _downloader
    if _downloader is None:
        _downloader = ModelDownloader()
    return _downloader


class DownloadError(RuntimeError):
    pass


def _dir_size_mb(path: Path) -> float:
    """递归统计目录大小（MB），不存在返回 0。"""
    if not path.exists():
        return 0.0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (root / Path(name)).stat().st_size
            except OSError:
                pass
    return total / (1024 * 1024)


def _hf_endpoint() -> str:
    return os.environ.get("AVS_HF_ENDPOINT", "").strip() or DEFAULT_HF_ENDPOINT


def _download_source() -> str:
    """下载源策略（AVS_DOWNLOAD_SOURCE，默认 auto）：
    auto=注册了 ModelScope 官方仓库则优先（国内官方高速），否则走 HF 镜像；
    modelscope/hf/mirror 为强制指定。"""
    return os.environ.get("AVS_DOWNLOAD_SOURCE", "").strip().lower() or "auto"


def _ms_list_files(ms_repo: str) -> list[dict]:
    """ModelScope 仓库文件清单（含大小 MB）：
    GET /api/v1/models/{repo}/repo/files?Recursive=true"""
    url = f"{MS_ENDPOINT}/api/v1/models/{ms_repo}/repo/files?Recursive=true"
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    return [
        {"path": f["Path"], "size_mb": f["Size"] / (1024 * 1024)}
        for f in (data.get("Data") or {}).get("Files") or []
        if f.get("Type") == "blob" and f.get("Path") and f.get("Size")
    ]


# socket 默认超时兜底（秒）：huggingface_hub 的 list_repo_files 等接口不透传
# timeout，镜像源不可达（如 hf-mirror 被墙/黑洞路由）时无超时请求会永久挂起，
# 任务将永远停留在「解析下载源」。设置全局 blocking socket 超时封住这一路径；
# 本项目其余网络调用（httpx/LLM 客户端）自带显式超时，asyncio WS 为非阻塞
# socket 不受默认值影响，30s 对所有调用均为安全上界。
_SOCKET_GUARD_SEC = 30
_socket_guard_applied = False


def _apply_socket_guard() -> None:
    """应用 socket 默认超时兜底（幂等，仅 HF 解析路径前调用一次）。"""
    global _socket_guard_applied
    if _socket_guard_applied:
        return
    import socket

    if socket.getdefaulttimeout() is None:
        socket.setdefaulttimeout(_SOCKET_GUARD_SEC)
    _socket_guard_applied = True


# ---------- 仓库快照下载过滤 ----------
# ModelScope/HF 仓库常同时携带多精度变体（fp8/fp4/fp16）、旧格式（.bin）、
# ComfyUI 单文件布局与 LoRA 附加物；全量快照下载浪费数倍磁盘与带宽
# （实测 LTX-2 仓库 159GB 中 diffusers 实际需要的不足 60GB）。

# 非权重支撑文件（精确文件名，含子目录布局）：分词器/配置/索引/预处理
_SUPPORT_FILES = frozenset({
    "model_index.json", "config.json", "configuration.json",
    "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
    "added_tokens.json", "vocab.json", "vocab.txt", "merges.txt",
    "spiece.model", "tokenizer.model", "chat_template.json",
    "generation_config.json", "preprocessor_config.json",
    "processor_config.json", "video_preprocessor_config.json",
})
# 多精度/量化变体与附加物标签（文件名包含即排除，仅保留默认变体）
_VARIANT_TAGS = (".fp16.", ".fp8.", ".fp4.", ".bf16.", ".int8.", ".nvfp4.", "lora")
# 明确不需要的媒体/文档/杂项后缀
_JUNK_SUFFIXES = (".md", ".gitattributes", ".png", ".jpg", ".jpeg", ".webp",
                  ".gif", ".mp4", ".mov", ".ckpt", ".pt", ".pth")


def _filter_download_paths(paths: list[str], spec) -> list[str]:
    """仓库文件快照过滤：只保留当前模型实际会加载的权重与支撑文件。

    规则（按顺序）：
    1) 支撑文件（model_index / 配置 / 分词器 / 分片索引）全部保留；
    2) spec.files 声明了具体权重文件名（如 GGUF 量化档）→ 精确白名单，
       未声明的同仓库权重全部排除（qwen3-8b 只下 Q4_K_M 一个）；
    3) 仓库存在 diffusers 布局（model_index.json）时，根目录裸权重是
       ComfyUI 单文件格式，diffusers 不加载，排除；
    4) 多精度变体（文件名含 fp8/fp4/fp16 等）、LoRA 附加物排除；
    5) 同名 .safetensors 已存在时排除冗余 .bin 旧格式；未知后缀保守保留。
    """
    declared = {(f.get("name") or "").lower() for f in (spec.files or [])}
    has_layout = any(p.lower() == "model_index.json" or p.lower().endswith("/model_index.json")
                     for p in paths)
    stems = {p.lower().rsplit("/", 1)[0] for p in paths
             if p.lower().endswith(".safetensors")}  # 含 safetensors 的目录
    kept: list[str] = []
    for p in paths:
        low = p.lower()
        name = low.rsplit("/", 1)[-1]
        # 1) 支撑文件全保留（布局清单/配置/分词器/分片索引）
        if p.lower() == "model_index.json" or name in _SUPPORT_FILES \
                or name.endswith(".index.json"):
            kept.append(p)
            continue
        # 2) 媒体/文档/杂项直接排除
        if name.endswith(_JUNK_SUFFIXES):
            continue
        # 3) spec.files 声明了具体权重（如 GGUF 量化档）→ 精确白名单
        if declared:
            if name in declared:
                kept.append(p)
            continue
        # 4) 权重文件的变体/格式/布局过滤
        if name.endswith((".safetensors", ".bin", ".gguf")):
            if any(tag in name for tag in _VARIANT_TAGS):
                continue  # 多精度变体 / LoRA 附加物
            if name.endswith(".bin") and low.rsplit("/", 1)[0] in stems:
                continue  # 同目录已有 safetensors，.bin 为旧格式冗余
            if has_layout and "/" not in p:
                continue  # diffusers 布局下根目录裸权重 = ComfyUI 单文件
            kept.append(p)
            continue
        # 5) 未知类型保守保留
        kept.append(p)
    return kept


# ModelScope 同名仓库探测缓存（repo_id -> 是否存在）：避免每次下载重复探测
_ms_repo_probe_cache: dict[str, bool] = {}


def _ms_repo_exists(repo_id: str) -> bool:
    """探测 ModelScope 上是否存在与 HF 同名的仓库（魔搭优先策略，进程内缓存）。

    大量 HF 热门仓库在 ModelScope 有同步镜像；未注册 ms_repo 的模型据此
    自动优先走魔搭。网络异常按不存在处理（回退 HF 镜像），结果进程内缓存。
    """
    if repo_id in _ms_repo_probe_cache:
        return _ms_repo_probe_cache[repo_id]
    exists = False
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(f"{MS_ENDPOINT}/api/v1/models/{repo_id}")
            exists = resp.status_code == 200 and resp.json().get("Code") == 200
    except Exception:  # noqa: BLE001 网络异常按不存在处理
        exists = False
    _ms_repo_probe_cache[repo_id] = exists
    if exists:
        logger.info("ModelScope 命中 HF 同名镜像 %s（下载源魔搭优先）", repo_id)
    return exists


class ModelDownloader:
    """内存态模型下载器。线程安全由 RLock 保护。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}  # model_id -> 状态快照

    # ---------- 对外接口 ----------
    def start(self, model_id: str) -> dict:
        """发起后台下载（幂等：进行中直接返回现状）。"""
        spec = get_spec(model_id)
        if not spec:
            raise DownloadError(f"模型不存在：{model_id}")
        if not spec.repo_id:
            raise DownloadError(f"模型 {model_id} 无需下载（内置/云端服务）")
        with self._lock:
            job = self._jobs.get(model_id)
            if job and job["status"] == "canceling":
                # 旧下载线程尚在收尾（仍在写 .part），此时再起新线程会双写同一文件
                raise DownloadError(f"模型 {model_id} 正在取消，请稍候片刻再重试")
            if job and job["status"] in ("queued", "downloading"):
                return dict(job)
            self._jobs[model_id] = {
                "model_id": model_id,
                "status": "queued",
                "progress": 0.0,
                "downloaded_mb": 0.0,
                "total_mb": 0.0,
                "message": "等待下载",
                "error": "",
            }
            threading.Thread(
                target=self._run, args=(model_id,), daemon=True,
                name=f"avs-download-{model_id}",
            ).start()
            return dict(self._jobs[model_id])

    def status(self, model_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(model_id)
            return dict(job) if job else None

    def cancel(self, model_id: str) -> bool:
        """标记取消（线程在下一个分片边界停止，已下载块保留供续传）。"""
        with self._lock:
            job = self._jobs.get(model_id)
            if job and job["status"] in ("queued", "downloading"):
                job["status"] = "canceling"
                return True
        return False

    def all_jobs(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._jobs.items()}

    def uninstall(self, model_id: str) -> bool:
        """删除模型目录（正在下载/占用推理时拒绝）。"""
        spec = get_spec(model_id)
        if not spec:
            raise DownloadError(f"模型不存在：{model_id}")
        path = settings.models_dir / model_id
        if not path.exists():
            return False
        with self._lock:
            job = self._jobs.get(model_id)
            if job and job["status"] in ("queued", "downloading", "canceling"):
                raise DownloadError(f"模型 {model_id} 正在下载，无法卸载")
        from app.inference.pool import pool

        if any(r["model_id"] == model_id for r in pool.stats()["resident"]):
            raise DownloadError(f"模型 {model_id} 正在被推理占用，请稍后再卸载")
        shutil.rmtree(path, ignore_errors=True)
        with self._lock:
            self._jobs.pop(model_id, None)
        return True

    # ---------- 内部实现 ----------
    def _update(self, model_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(model_id)
            if job:
                job.update(kwargs)

    def _run(self, model_id: str) -> None:
        spec = get_spec(model_id)
        target = settings.models_dir / model_id
        try:
            self._update(model_id, status="downloading", message="解析下载源…")
            files = self._resolve_files(spec)
            if not files:
                raise DownloadError("下载源无可下载文件，请重试或更换下载源")
            total_mb = round(sum(f[2] for f in files), 1)
            self._update(model_id, total_mb=total_mb,
                         message=f"共 {len(files)} 个文件 / {total_mb:.1f} MB")
            self._check_disk(spec, total_mb)

            # 进度线程：每 0.5s 统计已下载大小，遇取消/完成退出
            stop = threading.Event()

            def _ticker() -> None:
                while not stop.is_set():
                    done = _dir_size_mb(target)
                    job = self.status(model_id)
                    total = job["total_mb"] if job else 0
                    progress = (done / total) if total > 0 else 0.0
                    self._update(model_id, downloaded_mb=round(done, 1),
                                 progress=round(min(1.0, progress), 4),
                                 message=f"已下载 {done:.1f} MB / {total:.1f} MB")
                    if stop.wait(0.5):
                        return

            # 下载主体：多文件并行（默认 4 并发；单文件 GET + Range 断点续传），
            # 大文件优先调度避免尾部单文件瓶颈；chunk 级检查任务终态
            from concurrent.futures import ThreadPoolExecutor, as_completed

            ticker = threading.Thread(target=_ticker, daemon=True)
            ticker.start()
            try:
                target.mkdir(parents=True, exist_ok=True)
                workers = min(4, max(1, len(files)))
                # shutdown(wait=False)：取消/失败时不阻塞等待文件线程——各线程在
                # 分片/重试边界自检任务终态后自行退出（.part 残留供续传）。
                # 此前 with 退出 = shutdown(wait=True)，会卡满最长 read 超时×重试链
                # （最坏 10 分钟+），用户看到的「正在取消…」久转不结束即源于此。
                ex = ThreadPoolExecutor(max_workers=workers)
                try:
                    futures = [ex.submit(self._download_file, model_id, target, p, url)
                               for p, url, _s in files]
                    for fut in as_completed(futures):
                        if not fut.result():
                            break  # 任一文件报告取消：主循环立即收尾，线程自行退出
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)
            finally:
                stop.set()
                ticker.join(timeout=2)

            # 校验：取消标志；目录非空即成功
            job = self.status(model_id)
            if job and job["status"] == "canceling":
                self._update(model_id, status="canceled", message="已取消（已下载块保留，可续传）")
                return
            if not target.exists() or not any(target.iterdir()):
                raise DownloadError("下载完成但目录为空，请重试")
            done = _dir_size_mb(target)
            self._update(model_id, status="done", progress=1.0,
                         downloaded_mb=round(done, 1),
                         message=f"完成，共 {done:.1f} MB")
        except DownloadError as e:
            logger.error("模型下载失败 model=%s: %s", model_id, e)
            self._update(model_id, status="failed", error=str(e), message=str(e))
        except Exception as e:  # noqa: BLE001 huggingface_hub 网络/解析异常
            logger.exception("模型下载异常 model=%s", model_id)
            self._update(model_id, status="failed",
                         error=f"下载失败：{e}", message=f"下载失败：{e}")

    def _resolve_files(self, spec) -> list[tuple[str, str, float]]:
        """解析待下载文件清单 [(相对路径, 直链, 大小MB)]，按大小降序（大文件先并行）。

        源策略（AVS_DOWNLOAD_SOURCE，默认 auto）——所有源**优先魔搭**：
        1. 注册表确认的官方镜像（ms_repo）；
        2. 未注册时自动探测 ModelScope 同名仓库（大量 HF 仓库在魔搭有同步
           镜像，探测结果进程内缓存）——命中即走魔搭并在 job message 注明；
        3. 均未命中 / 解析失败 → HF 镜像兜底（失败原因透出，不静默）。
        - modelscope / hf / mirror 为强制指定。
        """
        source = _download_source()
        if source in ("auto", "modelscope"):
            # 候选魔搭仓库：注册表镜像优先，未注册则探测 HF 同名仓库
            candidates = []
            if spec.ms_repo:
                candidates.append(spec.ms_repo)
            elif spec.repo_id and _ms_repo_exists(spec.repo_id):
                candidates.append(spec.repo_id)
            for ms_repo in candidates:
                try:
                    files = _ms_list_files(ms_repo)
                    if files:
                        # 快照过滤：只保留实际会加载的权重与支撑文件
                        keep = set(_filter_download_paths([f["path"] for f in files], spec))
                        files = [f for f in files if f["path"] in keep]
                        base = f"{MS_ENDPOINT}/models/{ms_repo}/resolve/master"
                        if ms_repo == spec.repo_id and not spec.ms_repo:
                            # 同名镜像（非注册表确认）——注明来源供用户甄别
                            self._update(spec.id,
                                         message=f"已命中 ModelScope 同名镜像 {ms_repo}（魔搭优先）")
                        return [
                            (f["path"], f"{base}/{quote(f['path'], safe='/')}", f["size_mb"])
                            for f in sorted(files, key=lambda x: x["size_mb"], reverse=True)
                        ]
                except Exception as e:  # noqa: BLE001 ModelScope 网络异常
                    if source == "modelscope":
                        raise DownloadError("ModelScope 连接失败，请检查网络") from None
                    # auto 模式：回退 HF 镜像，但把失败原因透出到任务状态（不静默）
                    self._update(spec.id, message=f"ModelScope 解析失败（{e}），回退 HF 镜像…")

        endpoint = HF_OFFICIAL_ENDPOINT if source == "hf" else _hf_endpoint()
        os.environ.setdefault("HF_ENDPOINT", endpoint)
        _apply_socket_guard()
        from huggingface_hub import HfApi

        api = HfApi(endpoint=endpoint)
        names = api.list_repo_files(spec.repo_id, revision=spec.revision or None)
        names = _filter_download_paths(names, spec)  # 快照过滤（与 ModelScope 源同规则）
        # 一次批量查询文件大小，按先大后小排序；元数据失败则保持默认顺序
        sizes: dict[str, float] = {}
        try:
            for p in api.get_paths_info(
                spec.repo_id, paths=names, revision=spec.revision or None,
            ):
                if p.size:
                    sizes[p.path] = p.size / (1024 * 1024)
        except Exception:  # noqa: BLE001
            pass
        return [
            (f, f"{endpoint.rstrip('/')}/{spec.repo_id}/resolve/"
                f"{spec.revision or 'main'}/{quote(f, safe='/')}", sizes.get(f, 0.0))
            for f in sorted(names, key=lambda x: sizes.get(x, 0.0), reverse=True)
        ]

    def _cancel_requested(self, model_id: str) -> bool:
        """任务已脱离可下载状态（取消中/已取消/失败等），文件线程应立即退出。

        统一判据 = job 不处于 queued/downloading：覆盖 canceling、canceled、
        failed 及任务记录缺失等一切终态，保证任一状态转换后所有下载线程
        都能在下一个检查点退出（此前只认 canceling，canceled/failed 后
        线程继续写盘）。
        """
        job = self.status(model_id)
        return job is None or job["status"] not in ("queued", "downloading")

    def _download_file(self, model_id: str, target: Path, fname: str, url: str,
                       max_retries: int = 5) -> bool:
        """下载单个文件到 target/fname。

        手写 HTTP 下载（绕开 huggingface_hub 对镜像源 HEAD 跨域重定向的兼容问题）：
        - GET + Range 头断点续传（.part 文件记录已下载字节）
        - 取消检查覆盖三处：每次尝试开始前 / 每个分片边界 / 退避睡眠分段——
          卡在无数据 read 时的最长退出延迟为单次 read 超时（120s），
          其余场景秒级响应（此前退避整段 sleep 不检查，最坏 10 分钟+）
        - 网络瞬断自动重试（指数退避，从 .part 断点续传），大文件长跑抗抖动
        返回 False 表示被取消。
        """
        dest = target / fname
        part = Path(str(dest) + ".part")
        if dest.exists():
            # 已完整下载（dest 仅在单文件下载完成后落地）：清残留 .part 后跳过
            if part.exists():
                part.unlink(missing_ok=True)
            return True
        dest.parent.mkdir(parents=True, exist_ok=True)
        last_err: Exception | None = None
        for attempt in range(1, max_retries + 1):
            # 每次尝试（含重试）开始前检查：取消/失败即退出，不再重试
            if self._cancel_requested(model_id):
                return False
            offset = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with httpx.stream(
                    "GET", url, headers=headers, follow_redirects=True,
                    timeout=httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=15.0),
                ) as r:
                    if r.status_code == 416:  # 范围越界：part 已是完整文件
                        os.replace(part, dest)
                        return True
                    r.raise_for_status()
                    with open(part, "ab" if offset else "wb") as f:
                        for chunk in r.iter_bytes(1024 * 1024):
                            # 终态（取消中/已取消/失败）都停写：取消秒级生效
                            if self._cancel_requested(model_id):
                                return False
                            f.write(chunk)
                os.replace(part, dest)
                return True
            except httpx.HTTPError as e:
                last_err = e
                if attempt < max_retries:
                    # 退避分段睡眠（每 0.5s 检查任务状态）：整段 sleep 不响应取消
                    # 会让取消卡满退避时长（最坏 62s）
                    wait_sec = min(60, 2 ** attempt)  # 2/4/8/16/32s 退避
                    for _ in range(wait_sec * 2):
                        if self._cancel_requested(model_id):
                            return False
                        time.sleep(0.5)
                    continue
        raise DownloadError(f"文件 {fname} 下载失败（重试 {max_retries} 次）：{last_err}") from last_err

    def _check_disk(self, spec, need_mb: float) -> None:
        """磁盘空间预检：剩余 < 所需 1.2 倍时报错，避免下载中断。"""
        try:
            usage = shutil.disk_usage(settings.models_dir)
        except OSError:
            return
        need_bytes = max(need_mb * 1024 * 1024 * 1.2, 2 * 1024 * 1024 * 1024)
        if usage.free < need_bytes:
            raise DownloadError(
                f"磁盘空间不足：{spec.id} 约需 {need_mb:.1f} MB，"
                f"当前剩余 {(usage.free / 1024 ** 3):.1f} GB，请清理后重试")


# ---------- 推理依赖检测与一键安装 ----------

def missing_inference_deps() -> list[str]:
    """返回缺失的推理依赖项（importlib 探测）。"""
    import importlib.util

    missing = []
    for module, pip_name in REQUIRED_INFERENCE_DEPS:
        if importlib.util.find_spec(module) is None:
            missing.append(pip_name)
    return missing


class DepsInstaller:
    """后台 pip 安装推理依赖，进度/日志内存态保留。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict = {"status": "idle", "log": "", "error": ""}

    def start(self) -> dict:
        with self._lock:
            if self._state["status"] in ("installing",):
                return dict(self._state)
            self._state = {"status": "installing", "log": "", "error": ""}
        threading.Thread(target=self._run, daemon=True,
                         name="avs-deps-install").start()
        return dict(self._state)

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _run(self) -> None:
        cmd = [sys_executable(), "-m", "pip", "install", "-e", ".[inference]"]
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
            )
            log: list[str] = []
            if proc.stdout:
                for line in proc.stdout:
                    line = line.rstrip()
                    if not line:
                        continue
                    log.append(line)
                    # 仅保留末尾 200 行，避免内存膨胀
                    if len(log) > 200:
                        log = log[-200:]
                    with self._lock:
                        self._state["log"] = "\n".join(log)
            code = proc.wait(timeout=60 * 30)
            if code != 0:
                raise RuntimeError(f"pip 安装失败（exit {code}），详见日志")
            with self._lock:
                self._state.update(status="done", log=self._state["log"])
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._state.update(status="failed", error=f"安装失败：{e}")


def sys_executable() -> str:
    import sys

    return sys.executable


deps_installer = DepsInstaller()
