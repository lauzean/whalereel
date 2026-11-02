"""模型服务端点存储层：Key 管理 / 端点 CRUD / 每类默认端点（DB 化配置）。

替代旧「.env 五槽位」的第三方服务配置模式：
- provider_keys：API Key（可被多个端点复用，列表脱敏）
- provider_endpoints：模型服务端点（模型类型 kind + 调用协议 provider + 地址 + key + 默认模型）
- settings.default_endpoints：每类任务的默认端点映射 {"llm": endpoint_id, ...}

只依赖 app.db.*，不 import app.core.providers（providers 反向引用本模块，
避免循环导入）。所有读取现查现用（保存即生效），DB 异常向上返回 None 由
调用方回退 .env 兼容层，绝不打断生成链路。
"""

import json

from sqlalchemy import select

from app.db.models import ProviderEndpoint, ProviderKey, Setting
from app.db.session import SessionLocal

# 默认端点映射在 settings 表的键
DEFAULT_ENDPOINTS_KEY = "default_endpoints"

# 任务级模型编码串分隔符："{endpoint_id}::{model_id}"（endpoint_id 为 uuid hex，
# 不含冒号；model_id 为远端模型名，不含 "::"）
ENDPOINT_MODEL_SEP = "::"

# 端点可用的调用协议（不含 local：本地推理不走端点）
ENDPOINT_PROVIDERS = ("openai", "ark", "agnes", "custom", "comfyui", "vidu")

# 协议 × 模型类型限制（只限制新增的窄协议；既有协议不设限，避免影响存量端点）：
# comfyui 目前只有「参考生视频」工作流，建 t2i/llm 类端点会在生成时才发现不支持；
# vidu 只接图生视频（首帧必填），故只许建视频类端点——t2v 与 i2v 都放行，
# 理由同 comfyui：故事板「视频模型」下拉按 t2v 过滤，只许 i2v 会导致选不到
_KIND_PROVIDER_RESTRICTIONS: dict[str, tuple[str, ...]] = {
    "comfyui": ("t2v", "i2v"),
    "vidu": ("t2v", "i2v"),
}

# 默认端点覆盖的任务类别（与 providers.PROVIDER_KINDS 对齐）
DEFAULT_KINDS = ("llm", "t2i", "t2v", "i2v", "tts")


class ProviderStoreError(ValueError):
    """端点/Key 管理的数据校验错误（信息直接面向用户展示）。"""


def encode_endpoint_model(endpoint_id: str, model: str) -> str:
    """端点 + 模型编码为任务级模型串（前端下拉选项的 value）。"""
    return f"{endpoint_id}{ENDPOINT_MODEL_SEP}{model}"


def decode_endpoint_model(model_id: str) -> tuple[str, str] | None:
    """解析任务级模型串，非编码串返回 None。"""
    mid = (model_id or "").strip()
    if ENDPOINT_MODEL_SEP not in mid:
        return None
    endpoint_id, _, model = mid.partition(ENDPOINT_MODEL_SEP)
    if not endpoint_id:
        return None
    return endpoint_id, model


def is_endpoint_model(model_id: str) -> bool:
    """判定任务级模型串是否为端点编码串。"""
    return decode_endpoint_model(model_id) is not None


# ---------- Key 管理 ----------

def mask_key(key: str) -> str:
    """api_key 脱敏：仅显示前 4 后 4 位（过短则全遮）。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def list_keys() -> list[dict]:
    """全部 Key（api_key 脱敏，附 used_by 引用端点名列表）。"""
    with SessionLocal() as db:
        endpoints = db.scalars(select(ProviderEndpoint)).all()
        by_key: dict[str, list[str]] = {}
        for ep in endpoints:
            if ep.key_id:
                by_key.setdefault(ep.key_id, []).append(ep.name)
        keys = db.scalars(select(ProviderKey).order_by(ProviderKey.created_at)).all()
        return [
            {
                "id": k.id,
                "name": k.name,
                "api_key": mask_key(k.api_key),
                "remark": k.remark,
                "used_by": sorted(by_key.get(k.id, [])),
                "created_at": k.created_at,
                "updated_at": k.updated_at,
            }
            for k in keys
        ]


def create_key(name: str, api_key: str, remark: str = "") -> dict:
    """新建 Key（name/api_key 必填）。"""
    if not name.strip():
        raise ProviderStoreError("Key 名称不能为空")
    if not api_key.strip():
        raise ProviderStoreError("API Key 不能为空")
    with SessionLocal() as db:
        row = ProviderKey(name=name.strip(), api_key=api_key.strip(), remark=remark.strip())
        db.add(row)
        db.commit()
        return {"id": row.id, "name": row.name}


def update_key(key_id: str, name: str | None = None,
               api_key: str | None = None, remark: str | None = None) -> None:
    """更新 Key（api_key 为 None/空串 = 不修改，避免脱敏值回写）。"""
    with SessionLocal() as db:
        row = db.get(ProviderKey, key_id)
        if not row:
            raise ProviderStoreError("Key 不存在")
        if name is not None:
            if not name.strip():
                raise ProviderStoreError("Key 名称不能为空")
            row.name = name.strip()
        if remark is not None:
            row.remark = remark.strip()
        if api_key:  # 空串/None 均不修改
            row.api_key = api_key.strip()
        db.commit()


def delete_key(key_id: str) -> list[str]:
    """删除 Key：引用它的端点解绑（key_id 置空），返回受影响端点名列表。"""
    with SessionLocal() as db:
        row = db.get(ProviderKey, key_id)
        if not row:
            raise ProviderStoreError("Key 不存在")
        affected = db.scalars(select(ProviderEndpoint).where(
            ProviderEndpoint.key_id == key_id)).all()
        for ep in affected:
            ep.key_id = None
        db.delete(row)
        db.commit()
        return [ep.name for ep in affected]


# ---------- 端点 CRUD ----------

def _validate_endpoint_fields(kind: str, provider: str, base_url: str,
                              model: str) -> tuple[str, str, str, str]:
    """端点字段校验与归一化（create/update 共用），返回归一化后的四元组。"""
    from app.core.providers import PROVIDER_KINDS

    kind = (kind or "").strip().lower()
    if kind not in PROVIDER_KINDS:
        raise ProviderStoreError(
            f"无效模型类型：{kind}（可选：{'/'.join(PROVIDER_KINDS)}）")
    provider = (provider or "").strip().lower()
    if provider not in ENDPOINT_PROVIDERS:
        raise ProviderStoreError(
            f"无效服务协议：{provider}（可选：{'/'.join(ENDPOINT_PROVIDERS)}）")
    allowed_kinds = _KIND_PROVIDER_RESTRICTIONS.get(provider)
    if allowed_kinds and kind not in allowed_kinds:
        raise ProviderStoreError(
            f"服务协议 {provider} 不支持「{kind}」类型"
            f"（仅支持：{'/'.join(allowed_kinds)}）")
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ProviderStoreError("base_url 须以 http(s):// 开头")
    model = (model or "").strip()
    if len(model) > 100:
        # 端点模型串入 Task.model_id（编码后 32+2+长度），限制长度保证可控
        raise ProviderStoreError("模型 ID 过长（上限 100 字符）")
    return kind, provider, base_url, model


def list_endpoints(kind: str | None = None) -> list[dict]:
    """端点列表（附 key_name/key_masked；kind 非空时按类型过滤）。"""
    with SessionLocal() as db:
        query = select(ProviderEndpoint).order_by(ProviderEndpoint.created_at)
        if kind:
            query = query.where(ProviderEndpoint.kind == kind)
        endpoints = db.scalars(query).all()
        keys = {k.id: k for k in db.scalars(select(ProviderKey)).all()}
        result = []
        for ep in endpoints:
            key = keys.get(ep.key_id) if ep.key_id else None
            result.append({
                "id": ep.id,
                "name": ep.name,
                "kind": ep.kind,
                "provider": ep.provider,
                "base_url": ep.base_url,
                "key_id": ep.key_id,
                "key_name": key.name if key else "",
                "key_masked": mask_key(key.api_key) if key else "",
                "model": ep.model,
                "multimodal": bool(getattr(ep, "multimodal", False)),
                # 计费费率（33 号方案 2026-10-09：按模型服务端点配置）
                "credits_per_1k_tokens": int(
                    getattr(ep, "credits_per_1k_tokens", 0) or 0),
                "credits_per_image": int(
                    getattr(ep, "credits_per_image", 0) or 0),
                "credits_per_video_second": int(
                    getattr(ep, "credits_per_video_second", 0) or 0),
                "bill_enabled": bool(getattr(ep, "bill_enabled", True)),
                "created_at": ep.created_at,
            })
        return result


def create_endpoint(name: str, kind: str, provider: str, base_url: str,
                    key_id: str | None = None, model: str = "",
                    multimodal: bool = False,
                    credits_per_1k_tokens: int = 0, credits_per_image: int = 0,
                    credits_per_video_second: int = 0,
                    bill_enabled: bool = True) -> dict:
    """新建端点（key_id 提供时校验存在；multimodal 仅 llm 类有意义；
    费率字段为 33 号方案的积分计费配置，默认全 0 = 该服务免费）。"""
    if not name.strip():
        raise ProviderStoreError("端点名称不能为空")
    for label, v in (("每千 token 积分", credits_per_1k_tokens),
                     ("每张图积分", credits_per_image),
                     ("每秒视频积分", credits_per_video_second)):
        if int(v or 0) < 0:
            raise ProviderStoreError(f"{label}不能为负数")
    kind, provider, base_url, model = _validate_endpoint_fields(
        kind, provider, base_url, model)
    with SessionLocal() as db:
        if key_id and not db.get(ProviderKey, key_id):
            raise ProviderStoreError("关联的 Key 不存在")
        row = ProviderEndpoint(name=name.strip(), kind=kind, provider=provider,
                               base_url=base_url, key_id=key_id or None, model=model,
                               multimodal=bool(multimodal) if kind == "llm" else False,
                               credits_per_1k_tokens=int(credits_per_1k_tokens or 0),
                               credits_per_image=int(credits_per_image or 0),
                               credits_per_video_second=int(
                                   credits_per_video_second or 0),
                               bill_enabled=bool(bill_enabled))
        db.add(row)
        db.commit()
        return {"id": row.id, "name": row.name}


def update_endpoint(endpoint_id: str, name: str | None = None, kind: str | None = None,
                    provider: str | None = None, base_url: str | None = None,
                    key_id: str | None = None, model: str | None = None,
                    multimodal: bool | None = None,
                    credits_per_1k_tokens: int | None = None,
                    credits_per_image: int | None = None,
                    credits_per_video_second: int | None = None,
                    bill_enabled: bool | None = None) -> None:
    """更新端点（key_id 显式传 None = 解绑；字段缺省 = 不修改；
    费率字段为 33 号方案计费配置，提供即更新）。"""
    with SessionLocal() as db:
        row = db.get(ProviderEndpoint, endpoint_id)
        if not row:
            raise ProviderStoreError("端点不存在")
        if name is not None:
            if not name.strip():
                raise ProviderStoreError("端点名称不能为空")
            row.name = name.strip()
        kind_v, provider_v, base_v, model_v = _validate_endpoint_fields(
            kind if kind is not None else row.kind,
            provider if provider is not None else row.provider,
            base_url if base_url is not None else row.base_url,
            model if model is not None else row.model,
        )
        row.kind, row.provider, row.base_url, row.model = kind_v, provider_v, base_v, model_v
        if key_id is not None:  # 显式传 None = 解绑；未提供 = 不修改
            if key_id and not db.get(ProviderKey, key_id):
                raise ProviderStoreError("关联的 Key 不存在")
            row.key_id = key_id or None
        if multimodal is not None:  # 多模态标记（批次 3.5：视觉判级的准入开关）
            row.multimodal = bool(multimodal) if row.kind == "llm" else False
        for label, value, field in (
                ("每千 token 积分", credits_per_1k_tokens, "credits_per_1k_tokens"),
                ("每张图积分", credits_per_image, "credits_per_image"),
                ("每秒视频积分", credits_per_video_second,
                 "credits_per_video_second")):
            if value is not None:
                if int(value) < 0:
                    raise ProviderStoreError(f"{label}不能为负数")
                setattr(row, field, int(value))
        if bill_enabled is not None:
            row.bill_enabled = bool(bill_enabled)
        db.commit()


def delete_endpoint(endpoint_id: str) -> None:
    """删除端点并同步清除 default_endpoints 中的引用（防悬挂）。"""
    with SessionLocal() as db:
        row = db.get(ProviderEndpoint, endpoint_id)
        if not row:
            raise ProviderStoreError("端点不存在")
        defaults = _read_defaults(db)
        defaults = {k: v for k, v in defaults.items() if v != endpoint_id}
        db.merge(Setting(key=DEFAULT_ENDPOINTS_KEY,
                         value=json.dumps(defaults, ensure_ascii=False)))
        db.delete(row)
        db.commit()


# ---------- 默认端点 ----------

def _read_defaults(db) -> dict[str, str]:
    """读 settings 表的默认端点映射（行缺失/JSON 异常均按空映射）。"""
    row = db.get(Setting, DEFAULT_ENDPOINTS_KEY)
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def get_default_endpoints() -> dict[str, dict | None]:
    """每类任务的默认端点（5 类齐全，未设置为 None；附端点摘要供展示）。"""
    with SessionLocal() as db:
        defaults = _read_defaults(db)
        endpoints = {ep.id: ep for ep in db.scalars(select(ProviderEndpoint)).all()}
        result: dict[str, dict | None] = {}
        for kind in DEFAULT_KINDS:
            ep = endpoints.get(defaults.get(kind, ""))
            result[kind] = None if not ep else {
                "endpoint_id": ep.id,
                "endpoint_name": ep.name,
                "provider": ep.provider,
                "model": ep.model,
            }
        return result


def set_default_endpoints(mapping: dict[str, str | None]) -> dict[str, dict | None]:
    """写入每类任务的默认端点（值为 None/空串 = 清除该类；校验端点存在且 kind 匹配）。"""
    with SessionLocal() as db:
        current = _read_defaults(db)
        for kind, endpoint_id in (mapping or {}).items():
            if kind not in DEFAULT_KINDS:
                raise ProviderStoreError(f"无效任务类别：{kind}")
            if not endpoint_id:
                current.pop(kind, None)
                continue
            ep = db.get(ProviderEndpoint, endpoint_id)
            if not ep:
                raise ProviderStoreError(f"端点不存在：{endpoint_id}")
            if ep.kind != kind:
                raise ProviderStoreError(
                    f"端点「{ep.name}」的模型类型为 {ep.kind}，不能设为 {kind} 的默认")
            current[kind] = endpoint_id
        db.merge(Setting(key=DEFAULT_ENDPOINTS_KEY,
                         value=json.dumps(current, ensure_ascii=False)))
        db.commit()
    return get_default_endpoints()


# ---------- 运行时取数（providers 层消费） ----------

def load_default_config(kind: str) -> tuple[str, str, str, str] | None:
    """查指定任务类别的默认端点配置，返回 (provider, base_url, api_key, model)。

    kind="i2v" 无自己的默认映射时回退 t2v；端点/Key 缺失或 DB 异常返回 None
    （调用方回退 .env 兼容层）。
    """
    try:
        with SessionLocal() as db:
            defaults = _read_defaults(db)
            endpoint_id = defaults.get(kind)
            if not endpoint_id and kind == "i2v":
                endpoint_id = defaults.get("t2v")
            if not endpoint_id:
                return None
            ep = db.get(ProviderEndpoint, endpoint_id)
            if not ep:
                return None
            api_key = ""
            if ep.key_id:
                key = db.get(ProviderKey, ep.key_id)
                api_key = key.api_key if key else ""
            return ep.provider, ep.base_url, api_key, ep.model
    except Exception:  # noqa: BLE001 DB 层任何异常都回退 .env，不打断生成链路
        return None


def load_endpoint_config(endpoint_id: str) -> tuple[str, str, str, str] | None:
    """按端点 ID 取配置（任务级编码串解析用），返回元组同 load_default_config。"""
    try:
        with SessionLocal() as db:
            ep = db.get(ProviderEndpoint, endpoint_id)
            if not ep:
                return None
            api_key = ""
            if ep.key_id:
                key = db.get(ProviderKey, ep.key_id)
                api_key = key.api_key if key else ""
            return ep.provider, ep.base_url, api_key, ep.model
    except Exception:  # noqa: BLE001 同上，回退兜底
        return None


def load_llm_multimodal(model_id: str = "") -> bool:
    """LLM 端点的多模态标记（批次 3.5 视觉审片准入；无端点=False）。

    `model_id` 为端点编码串时按其端点判定，空串 = 当前默认端点。
    **项目级 LLM 预设必须按预设端点判定**：项目选了纯文本 LLM 时，视觉判级要
    指着那个端点报错；若拿全局默认的多模态标记放行，调用必然失败，而错误信息
    会指向一个跟本任务无关的端点（排查时被带偏）。
    """
    try:
        decoded = decode_endpoint_model(model_id) if model_id else None
        with SessionLocal() as db:
            if decoded:
                ep = db.get(ProviderEndpoint, decoded[0])
                return bool(ep.multimodal) if ep else False
            endpoint_id = _read_defaults(db).get("llm")
            if not endpoint_id:
                return False
            ep = db.get(ProviderEndpoint, endpoint_id)
            return bool(ep.multimodal) if ep else False
    except Exception:  # noqa: BLE001
        return False
