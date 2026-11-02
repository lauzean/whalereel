"""应用配置：路径与环境。"""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 后端项目根目录：server/（data/models/.env 均相对此目录）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings:
    def __init__(self) -> None:
        # 加载 server/.env（不覆盖已有环境变量），第三方服务配置持久化于此
        try:
            from dotenv import load_dotenv

            load_dotenv(PROJECT_ROOT / ".env", override=False)
        except ImportError:
            pass  # python-dotenv 未安装时仅使用进程环境变量
        self.project_root = PROJECT_ROOT
        # 环境变量可覆盖默认目录（部署/开发灵活）
        self.data_dir = Path(os.environ.get("AVS_DATA_DIR", PROJECT_ROOT / "data"))
        self.models_dir = Path(os.environ.get("AVS_MODELS_DIR", PROJECT_ROOT / "models"))
        self.artifacts_dir = self.data_dir / "artifacts"
        self.cache_dir = self.data_dir / "cache"
        # mock 模式：未安装推理依赖时，生成占位产物以跑通全链路
        self.mock_mode = os.environ.get("AVS_MOCK", "").lower() in ("1", "true", "yes")
        # 负面约束档位（20 号 §4.3 的 A/B 开关）：full（现行，画面类负面约束照写）|
        # positive_only（对照组：画面类禁止句改写为不写，只保留字幕/音频类负面词）。
        # 官方口径：Seedance 对画面内容不支持负向控制，「否定词会成为注意力标签，
        # 反而把被否定的东西拉进画面」；切换后**提示词侧指标可立即对比**
        # （prompt_quality.constraint_ratio 应显著下降），FTSR 对比需真跑一批镜头。
        self.prompt_negative_mode = os.environ.get(
            "AVS_PROMPT_NEGATIVE_MODE", "full").strip().lower()
        # ffmpeg 可执行文件（PATH 或随包分发）
        self.ffmpeg_bin = os.environ.get("AVS_FFMPEG", "ffmpeg")
        # 缓存上限（GB），0 表示不限制
        self.cache_limit_gb = int(os.environ.get("AVS_CACHE_LIMIT_GB", "20"))
        # 媒体访问是否强制要求签名（方案 A，2026-09-30）。默认关闭 = 向后兼容：
        # 前端仍可用裸 URL 取图，但**签名基础设施与前端签名取址已就位**——
        # 打开即进入「产物必须带签名才能访问」的状态（需先把登录鉴权挂上，
        # 否则签名只是把「永久公开」变成「限时公开」）。见 core/media_sign.py。
        self.media_require_signature = os.environ.get(
            "AVS_MEDIA_REQUIRE_SIGNATURE", "").lower() in ("1", "true", "yes")
        # 媒体签名有效期（秒）：前端按它提前续签
        self.media_sign_ttl_sec = int(
            os.environ.get("AVS_MEDIA_SIGN_TTL", str(12 * 3600)))
        # 队列空闲多久后卸载驻留模型（秒）。驻留池跨画布/项目共享，此值只影响
        # 「闲置多久后放掉显存」：默认 1800s（30 分钟）——反复切画布/查看结果
        # 的间隔通常在分钟级，10 分钟会频繁把驻留模型悄悄卸掉（重载一次 1-60 分钟）
        self.model_idle_unload_sec = int(os.environ.get("AVS_MODEL_IDLE_UNLOAD", "1800"))
        # 触发 LLM 分集的字数阈值（18 号 §51.2）：剧本无「第X集」集头且超过该
        # 字数时，才让 LLM 按剧情/爆点/钩子切集。默认 8000 字——实测单集
        # 2000-4000 字（库内 39 集中位 2197；真实长篇《霜刃辞》均值 3113），
        # 8000 字约等于 2 集以上，才值得多插一次分集调用。低于此整篇按单集走，
        # 用户可在界面手动「重新分集」（阈值只是自动线，不是硬限制）。
        self.episode_split_min_chars = int(
            os.environ.get("AVS_EPISODE_SPLIT_MIN_CHARS", "8000"))
        # llama.cpp server（OpenAI 兼容）地址；为空时 Agent 编排走规则规划
        # 向后兼容：设置了 AVS_LLM_ENDPOINT 且未设置 AVS_LLM_BASE_URL 时，
        # 去掉 /chat/completions 后缀后作为 LLM base_url 默认值（providers 层读取）
        self.llm_endpoint = os.environ.get("AVS_LLM_ENDPOINT", "").strip()
        if self.llm_endpoint and not os.environ.get("AVS_LLM_BASE_URL", "").strip():
            base = self.llm_endpoint.rstrip("/").removesuffix("/chat/completions")
            os.environ["AVS_LLM_BASE_URL"] = base
            os.environ.setdefault("AVS_LLM_PROVIDER", "openai")
        for d in (self.data_dir, self.models_dir, self.artifacts_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()

# 无法归属到项目的产物目录名（上传/编辑类产物在极端情况下的兜底归属）。
UNASSIGNED_PROJECT_DIR = "_unassigned"


# 项目目录名白名单（2026-09-30 安全整改）：项目 id 一律 32 位十六进制，
# 其余（含 `..`、盘符、分隔符）先被换掉再落盘，杜绝路径穿越。
_SAFE_DIR_RE = re.compile(r"[^0-9a-zA-Z_-]")


def artifact_dir_for(project_id: str | None) -> Path:
    """产物落盘的**项目子目录**：`data/artifacts/{project_id}/`。

    为什么按项目分目录（2026-09-24 用户决策）：
    - 原先是 `data/artifacts/{task_id}.{ext}` **平铺**，文件名第一段是任务 UUID，
      **不含任何项目维度**——按项目清理/打包/排查都做不到；
    - `assets` 表也没有 `project_id`（它只登记「文件在哪」），归属只能靠
      `node_versions` 反查，实测 1030 个产物里 264 个反查不到。

    **只影响新产物**：`assets.path` 存的是**绝对路径**（不是拼接的），读侧一律走
    `db.get(Asset, id).path`，所以存量留在根目录照样能读——无需迁移、无 URL 变化
    （前端走 `/api/artifacts/{id}/file`，按 id 寻址，与文件位置无关）。

    传 None/空串时落 `_unassigned/`：宁可集中一处，也不要在根目录继续散落。

    **安全收紧（2026-09-30）**：`project_id` 是**请求参数**（上传接口直接收），
    旧实现把它原样拼进路径——传 `../../../../tmp/x` 就能在 data 目录之外建目录
    写文件。现在只保留 `[0-9a-zA-Z_-]`（合法项目 id 为 32 位 hex，不受影响），
    去掉分隔符与 `..`；再做一次 resolve 后的**归属校验**兜底。

    **目录不存在则创建**（调用方都是拿它来写文件，省得每个写点各写一遍 mkdir）。
    """
    # 先判空再 str()：`str(None)` 会得到字面量 "None"，
    # 直接 `str(project_id).strip() or ...` 会建出一个名叫 `None` 的目录
    pid = str(project_id).strip() if project_id else ""
    safe = _SAFE_DIR_RE.sub("_", pid)[:64] if pid else ""
    d = settings.artifacts_dir / (safe or UNASSIGNED_PROJECT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    # 归属兜底：归一化后仍在 artifacts 根之内，否则退回 _unassigned
    # （正常情况下上面那步已经不可能越界，这条是防御性断言式检查）
    try:
        root = settings.artifacts_dir.resolve()
        if not d.resolve().is_relative_to(root):
            logger.warning("产物目录越界已改判：project_id=%r", project_id)
            d = root / UNASSIGNED_PROJECT_DIR
            d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d
