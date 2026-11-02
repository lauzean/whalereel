"""模型注册表：内置模型清单 + 安装状态。

对齐设计文档 3.6：登记模型 ID、任务类型、量化档、显存指纹、文件与下载源。
M0 阶段注册表内置静态清单；安装状态存 SQLite。
"""

from dataclasses import dataclass, field

from app.core.config import settings


@dataclass
class ModelSpec:
    id: str
    family: str
    task_type: str  # t2v|i2v|fl2v|ref2v|t2i|edit|tts|llm
    display_name: str
    quant: str
    vram_mb: int
    repo_id: str  # HuggingFace 仓库
    ms_repo: str = ""  # ModelScope 官方仓库（国内官方高速下载源；空则走 HF）
    revision: str = ""
    files: list[dict] = field(default_factory=list)  # {name, size_gb}
    notes: str = ""
    # 成片是否自带音轨（**仅视频类模型有意义**）：True/False = 已知（按模型事实登记，
    # 依据写在 notes 里）；None = 未知。**未知不得当成「无音频」显示**——画布视频
    # 节点的音频徽标读这一位（经 /api/models → modelOptions.fetchAudioCaps），
    # 不再写死模型名单（2026-10-14 修正：写死名单曾把云端模型全标成「无音频」）。
    audio: bool | None = None


# ---- 内置注册表（按设计文档 3.1-3.4）----
REGISTRY: list[ModelSpec] = [
    ModelSpec(
        id="minimax-h3/fl2va-int8",
        family="minimax-h3",
        task_type="fl2v",  # 覆盖 t2v（无帧输入）与 i2v（仅首帧）
        display_name="MiniMax H3 (FL2VA INT8 蒸馏)",
        quant="int8",
        vram_mb=18432,
        repo_id="MiniMaxAI/MiniMax-H3",
        files=[
            {"name": "transformer (int8)", "size_gb": 21.0},
            {"name": "text encoder (nvfp4)", "size_gb": 15.7},
            {"name": "video_vae (fp16)", "size_gb": 5.2},
            {"name": "audio_vae (fp32)", "size_gb": 0.6},
        ],
        notes="768p / 4-15s / 原生双声道音频；文生视频+首尾帧生视频",
        audio=True,
    ),
    ModelSpec(
        id="minimax-h3/ref2va-int8",
        family="minimax-h3",
        task_type="ref2v",
        display_name="MiniMax H3 (Ref2VA INT8)",
        quant="int8",
        vram_mb=18432,
        repo_id="MiniMaxAI/MiniMax-H3",
        notes="参考生视频：最多 9 图 + 3 视频 + 3 音频混合输入",
        audio=True,  # 与 fl2va 同族（H3 自带音频 VAE）
    ),
    ModelSpec(
        id="ltx-2/distilled",
        family="ltx-2",
        task_type="t2v",
        display_name="LTX-2.3 Distilled",
        quant="fp8",
        vram_mb=14336,
        repo_id="Lightricks/LTX-2",
        ms_repo="Lightricks/LTX-2",  # ModelScope 官方镜像（国内高速下载源）
        notes="草稿快速迭代：1080p / 8 步蒸馏 / 原生音画同步",
        audio=True,
    ),
    ModelSpec(
        id="wan-2.2/a14b-gguf-q5",
        family="wan-2.2",
        task_type="fl2v",
        display_name="Wan 2.2 A14B (GGUF Q5)",
        quant="q5_k_m",
        vram_mb=12800,
        repo_id="Wan-AI/Wan2.2-TI2V-A14B-GGUF",
        notes="无音频备选质量档；Apache 2.0 可商用",
        audio=False,
    ),
    ModelSpec(
        id="wan-2.2/ti2v-5b",
        family="wan-2.2",
        task_type="fl2v",
        display_name="Wan 2.2 TI2V-5B",
        quant="fp16",
        vram_mb=8192,
        # 必须用 -Diffusers 仓库：from_pretrained 需要 model_index.json 布局
        repo_id="Wan-AI/Wan2.2-TI2V-5B-Diffusers",
        ms_repo="Wan-AI/Wan2.2-TI2V-5B",
        notes="低配降档：720p / 5s / 8GB 显存",
        audio=False,  # Wan 家族导出无音轨（实测抽检确认）
    ),
    ModelSpec(
        id="hidream-o1/image",
        family="hidream-o1",
        task_type="t2i",
        display_name="HiDream-O1-Image (8B)",
        quant="bf16",  # fp32 权重存储（35GB），加载时转 bf16（约 16GB 显存）
        vram_mb=20480,
        repo_id="HiDream-ai/HiDream-O1-Image",
        ms_repo="HiDream-ai/HiDream-O1-Image",
        files=[{"name": "uit (fp32→bf16)", "size_gb": 35.2}],
        notes="文生图主力：UiT 无 VAE/文本编码器，原生 2048²；MIT；50 步",
    ),
    ModelSpec(
        id="hidream-o1/dev",
        family="hidream-o1",
        task_type="t2i",
        display_name="HiDream-O1-Image-Dev (8B 蒸馏)",
        quant="bf16",
        vram_mb=20480,
        repo_id="HiDream-ai/HiDream-O1-Image-Dev",
        ms_repo="HiDream-ai/HiDream-O1-Image-Dev",
        files=[{"name": "uit (fp32→bf16)", "size_gb": 35.2}],
        notes="快速档：28 步蒸馏 / 无 CFG，速度约 full 的 4 倍；MIT",
    ),
    ModelSpec(
        id="qwen-image",
        family="qwen-image",
        task_type="t2i",
        display_name="Qwen-Image",
        quant="fp16",
        # fp8 layerwise casting 落位分层 offload 的真实门槛（20B transformer）
        vram_mb=22528,
        repo_id="Qwen/Qwen-Image",
        ms_repo="Qwen/Qwen-Image",
        notes="中文提示词友好；Apache 2.0；低显存自动 fp8 存储降载",
    ),
    ModelSpec(
        id="qwen3-8b-q4",
        family="llm",
        task_type="llm",
        display_name="Qwen3-8B (Q4_K_M)",
        quant="q4_k_m",
        vram_mb=6144,
        repo_id="Qwen/Qwen3-8B-GGUF",
        ms_repo="Qwen/Qwen3-8B-GGUF",
        files=[{"name": "qwen3-8b-q4_k_m.gguf", "size_gb": 5.2}],
        notes="剧本/分镜/提示词/Agent；llama.cpp 常驻",
    ),
    ModelSpec(
        id="edge-tts",
        family="tts",
        task_type="tts",
        display_name="Edge-TTS",
        quant="",
        vram_mb=0,
        repo_id="",
        notes="免费云服务（微软）；零显存",
    ),
]

REGISTRY_BY_ID: dict[str, ModelSpec] = {m.id: m for m in REGISTRY}


def get_spec(model_id: str) -> ModelSpec | None:
    return REGISTRY_BY_ID.get(model_id)


def installed_spec_ids() -> set[str]:
    """探测模型目录下已就位的模型（目录名 = 模型 ID）。

    判定 = 目录非空且不含 .part 下载残留：下载进行中/取消续传的目录都带
    .part 分片，不算已安装（否则点击下载会立刻显示「已安装」）。
    """
    installed: set[str] = set()
    if not settings.models_dir.exists():
        return installed
    for spec in REGISTRY:
        model_dir = settings.models_dir / spec.id
        if not model_dir.exists():
            continue
        names = [f.name for f in model_dir.rglob("*") if f.is_file()]
        if names and not any(n.endswith(".part") for n in names):
            installed.add(spec.id)
    # edge-tts 无需权重
    installed.add("edge-tts")
    return installed


def list_models() -> list[dict]:
    installed = installed_spec_ids()
    result = []
    for spec in REGISTRY:
        result.append({
            "id": spec.id,
            "family": spec.family,
            "task_type": spec.task_type,
            "display_name": spec.display_name,
            "quant": spec.quant,
            "vram_mb": spec.vram_mb,
            "repo_id": spec.repo_id,
            "ms_repo": spec.ms_repo,
            "files": spec.files,
            "notes": spec.notes,
            "audio": spec.audio,
            "installed": spec.id in installed,
            "install_path": str(settings.models_dir / spec.id),
        })
    return result


# ---- 项目生成模型预设 ----

# 预设任务类别（与模型注册表 task_type / provider PROVIDER_KINDS 的取值对齐）
KIND_T2I = "t2i"  # 文生图
KIND_T2V = "t2v"  # 文生视频（ref2v 等视频类 runner 也归入此预设类别）
KIND_LLM = "llm"  # 对话模型（解析/审核/扩写等全部 LLM 调用）

# 项目可预设的任务类别。
# `llm` 是 2026-09-24 用户新增的：「设置后所有 LLM 端点优先使用项目设置的端点」——
# 典型场景是不同项目用不同的 LLM（推理型 vs 快速型、不同厂商对比）。
# 未设置 = 空串 = 走全局默认端点（**向后兼容**，老项目行为不变）。
# TTS 仍走全局配置（项目级需求未提出，不预先设计）。
PRESET_KINDS = (KIND_T2I, KIND_T2V, KIND_LLM)


def project_preset(project, kind: str) -> str:
    """读项目生成模型预设（project 可为 None；未设置返回空串）。

    返回空串表示未预设，调用方应继续走 runner default_model 兜底；
    getattr 防御旧库迁移遗漏（与 projects.py 的 getattr 惯例一致）。
    """
    if project is None or kind not in PRESET_KINDS:
        return ""
    presets = getattr(project, "model_presets", None) or {}
    return str(presets.get(kind, "") or "")
