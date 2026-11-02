"""模型与系统信息 API。

**整模块管理员专属（2026-09-30 用户规则）**：系统管理模块下的接口一律只有
管理员可访问——模型注册表/详情/安装进度、依赖状态、系统信息（GPU/运行模式）、
provider 模型列表全部挂 `require_admin`。

普通用户要在画布/故事板/项目设置里选生成模型，走**专用精简接口**
`GET /api/model-options`：只返回「能选什么、长什么样」（模型显示名/类型/
是否已安装），不含端点地址、密钥、服务商等管理信息。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.auth import require_admin
from app.core.gpu import detect_gpu
from app.core.model_downloader import (
    DownloadError, deps_installer, get_downloader, missing_inference_deps,
)
from app.core.model_manager import get_spec, list_models
from app.core.provider_store import get_default_endpoints, list_endpoints
from app.core.providers import (
    PROVIDER_KINDS, PROVIDER_NAMES, list_provider_models, load_providers,
    provider_audio_capability, update_provider_env,
)

router = APIRouter(prefix="/api", tags=["models"])


class ProviderIn(BaseModel):
    """第三方服务配置更新入参：未提供的可选字段保留 .env 已有值。"""

    kind: str  # llm|t2i|t2v|i2v|tts
    provider: str  # local|openai|ark|custom
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


@router.get("/providers", dependencies=[Depends(require_admin)])
def get_providers():
    """各类任务的 provider 配置状态（api_key 脱敏，仅显示前 4 后 4 位；仅管理员）。"""
    return load_providers()


@router.get("/providers/models", dependencies=[Depends(require_admin)])
def get_provider_models(kind: str = Query("t2i")):
    """拉取第三方服务可用的模型列表（前端模型下拉的 provider 部分）。

    ark：优先 GET {base}/models（OpenAI 兼容），失败回退官方精选模型表；
    未配置第三方或本地模式返回空列表（前端仅展示本地注册表模型）。
    """
    if kind not in PROVIDER_KINDS:
        raise HTTPException(400, f"无效任务类别：{kind}（可选：{'/'.join(PROVIDER_KINDS)}）")
    result = list_provider_models(kind)
    return {"kind": kind, "models": result["models"], "source": result["source"]}


@router.put("/providers", dependencies=[Depends(require_admin)])
def put_providers(body: ProviderIn):
    """更新第三方服务配置：写入 server/.env（保留其他行），保存后即时生效（仅管理员）。

    配置层（providers._load_config）每次现读环境变量，update_provider_env
    同步进程 os.environ 后所有线程立即可见，任务引擎/runner 下一次调用即用
    新配置，无需重启后端。
    """
    kind = body.kind.strip().lower()
    if kind not in PROVIDER_KINDS:
        raise HTTPException(400, f"无效任务类别：{body.kind}（可选：{'/'.join(PROVIDER_KINDS)}）")
    provider = body.provider.strip().lower()
    if provider not in PROVIDER_NAMES:
        raise HTTPException(400, f"无效 provider：{body.provider}（可选：{'/'.join(PROVIDER_NAMES)}）")
    values: dict[str, str] = {"provider": provider}
    if body.base_url is not None:
        values["base_url"] = body.base_url.strip().rstrip("/")
    if body.api_key is not None:
        values["api_key"] = body.api_key.strip()
    if body.model is not None:
        values["model"] = body.model.strip()
    update_provider_env(kind, values)
    # 写入 .env + 同步进程环境变量即全链路生效，无组件缓存配置
    return {"ok": True, "providers": load_providers()}


@router.get("/models", dependencies=[Depends(require_admin)])
def get_models():
    """模型注册表 + 安装状态 + 下载器实时进度。"""
    downloader = get_downloader()
    jobs = downloader.all_jobs()
    models = []
    for m in list_models():
        job = jobs.get(m["id"])
        m["install"] = {
            "status": job["status"] if job else
                      ("done" if m["installed"] else "idle"),
            "progress": round(job["progress"], 4) if job else (1.0 if m["installed"] else 0.0),
            "downloaded_mb": job["downloaded_mb"] if job else 0.0,
            "total_mb": job["total_mb"] if job else 0.0,
            "message": job["message"] if job else
                       ("已安装" if m["installed"] else "未安装"),
            "error": job["error"] if job else "",
        }
        models.append(m)
    return models


@router.get("/model-options")
def get_model_options():
    """**普通用户可读**的精简模型清单（模型选择器专用；2026-09-30 用户规则）。

    背景：系统管理模块的接口已全部收成管理员专属，但普通用户在画布/故事板/
    项目设置里要选生成模型——这个接口就是给他们看的，只回答「能选什么」：

    - `models`：本地注册表模型（显示名/类型/是否已安装）+ 每个端点的**已保存
      默认模型**一条（id 编码 `{endpoint_id}::{model}`，与提交时的解析格式一致；
      探测远端模型列表需要管理员权限，普通用户此前也只能选到这一条）；
    - `defaults`：每类任务的默认端点展示名（各下拉空值文案「默认：xxx」用）。

    **不含**端点地址（base_url）、密钥（key_id/key_masked）、服务商（provider）
    等管理信息——那些只在管理员接口里返回。
    """
    options: list[dict] = []
    for m in list_models():
        options.append({
            "id": m["id"],
            "display_name": m.get("display_name") or m["id"],
            "task_type": m.get("task_type", ""),
            "installed": bool(m.get("installed")),
            "source": "local",
            # 产物是否自带音轨（画布视频节点徽标用）：True/False = 已知，None = 未知
            "audio": m.get("audio"),
        })
    for ep in list_endpoints():
        model = str(ep.get("model") or "").strip()
        if not model:
            continue  # 未设默认模型的端点对用户不可选（与旧行为一致：探测需管理员）
        options.append({
            "id": f"{ep['id']}::{model}",
            "display_name": f"{ep.get('name') or '服务端点'} · {model}",
            "task_type": ep.get("kind", ""),
            "installed": True,
            "source": "endpoint",
            # 端点侧按**协议**判定（agnes/方舟/comfyui/vidu 有音轨；custom 等未知为 null）
            "audio": provider_audio_capability(ep.get("provider", "")),
        })
    defaults: dict[str, dict] = {}
    for kind, d in (get_default_endpoints() or {}).items():
        if not d:
            defaults[kind] = {}
            continue
        defaults[kind] = {
            "label": (f"{d.get('endpoint_name', '')} · {d.get('model', '')}".strip(" ·")
                      if d.get("model") else d.get("endpoint_name", "")),
        }
    return {"models": options, "defaults": defaults}


@router.get("/models/detail", dependencies=[Depends(require_admin)])
def get_model(model_id: str = Query(..., description="模型 ID（含斜杠）")):
    spec = get_spec(model_id)
    if not spec:
        raise HTTPException(404, "模型不存在")
    m = next(x for x in list_models() if x["id"] == model_id)
    job = get_downloader().status(model_id)
    m["install"] = {
        "status": job["status"] if job else ("done" if m["installed"] else "idle"),
        "progress": round(job["progress"], 4) if job else (1.0 if m["installed"] else 0.0),
        "downloaded_mb": job["downloaded_mb"] if job else 0.0,
        "total_mb": job["total_mb"] if job else 0.0,
        "message": job["message"] if job else ("已安装" if m["installed"] else "未安装"),
        "error": job["error"] if job else "",
    }
    return m


@router.post("/models/install", status_code=202, dependencies=[Depends(require_admin)])
def install_model(model_id: str = Query(..., description="模型 ID（含斜杠）")):
    """后台下载模型（断点续传）。进行中再次调用返回现状（仅管理员）。"""
    if get_spec(model_id) is None:
        raise HTTPException(404, "模型不存在")
    try:
        return get_downloader().start(model_id)
    except DownloadError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/models/install", dependencies=[Depends(require_admin)])
def install_status(model_id: str = Query(..., description="模型 ID（含斜杠）")):
    """查询模型下载进度（未发起过时返回 idle 占位）。"""
    spec = get_spec(model_id)
    if not spec:
        raise HTTPException(404, "模型不存在")
    job = get_downloader().status(model_id)
    if job:
        return job
    installed = (settings_models_dir() / model_id).exists() and any(
        (settings_models_dir() / model_id).iterdir())
    return {
        "model_id": model_id,
        "status": "done" if installed else "idle",
        "progress": 1.0 if installed else 0.0,
        "downloaded_mb": 0.0,
        "total_mb": 0.0,
        "message": "已安装" if installed else "未开始",
        "error": "",
    }


@router.post("/models/cancel", status_code=202, dependencies=[Depends(require_admin)])
def cancel_model(model_id: str = Query(..., description="模型 ID（含斜杠）")):
    """取消下载（已下载块保留，可续传；仅管理员）。"""
    if get_spec(model_id) is None:
        raise HTTPException(404, "模型不存在")
    return {"ok": get_downloader().cancel(model_id)}


@router.post("/models/uninstall", status_code=202, dependencies=[Depends(require_admin)])
def uninstall_model(model_id: str = Query(..., description="模型 ID（含斜杠）")):
    """卸载模型（删除本地权重；下载中/推理占用时拒绝；仅管理员）。"""
    if get_spec(model_id) is None:
        raise HTTPException(404, "模型不存在")
    try:
        return {"ok": get_downloader().uninstall(model_id)}
    except DownloadError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/system/deps", dependencies=[Depends(require_admin)])
def system_deps():
    """推理依赖状态：缺失项 + 后台安装进度。"""
    return {
        "missing": missing_inference_deps(),
        "install": deps_installer.status(),
    }


@router.post("/system/deps/install", status_code=202, dependencies=[Depends(require_admin)])
def install_deps():
    """后台 pip 安装推理依赖（pip install -e .[inference]；仅管理员）。"""
    return deps_installer.start()


def settings_models_dir():
    from app.core.config import settings

    return settings.models_dir


@router.get("/system/gpu", dependencies=[Depends(require_admin)])
def system_gpu():
    info = detect_gpu()
    return {
        "available": info.available,
        "device_name": info.device_name,
        "total_vram_mb": info.total_vram_mb,
        "free_vram_mb": info.free_vram_mb,
        "total_ram_mb": info.total_ram_mb,
        "torch_version": info.torch_version,
        "cuda_version": info.cuda_version,
        "inference_ready": info.inference_ready,
        "notes": info.notes,
    }


@router.get("/system/info", dependencies=[Depends(require_admin)])
def system_info():
    from app.core.config import settings

    gpu = detect_gpu()
    return {
        "mock_mode": settings.mock_mode or not gpu.inference_ready,
        "data_dir": str(settings.data_dir),
        "models_dir": str(settings.models_dir),
        "artifacts_dir": str(settings.artifacts_dir),
    }
