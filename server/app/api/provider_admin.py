"""模型服务管理 API：Key 管理 / 模型服务端点 CRUD / 每类默认端点。

系统配置-「模型管理」「默认配置」两个界面的后端。存储层见
app/core/provider_store.py（provider_keys / provider_endpoints /
settings.default_endpoints）；运行时解析链为
任务级编码串 > 默认端点 > .env 回退层（见 app/core/providers.py）。

管理员门禁：Key/端点/默认配置的写操作与 Key 列表（含敏感材料域）仅管理员；
**模型探测同样仅管理员**——项目设置与分镜卡的模型选择器对全部登录用户可用，
但「用服务端已保存的密钥去调用任意 URL」这条能力只留给管理员（见下）。

**模型探测为什么必须收口（2026-09-30 安全整改）**：
旧实现 `POST /api/endpoints/probe-models` 无任何鉴权，且支持传 `key_id`
表示「用库里已保存的那把密钥」——而 `base_url` 由调用方随意指定。攻击链
只要两步：① `GET /api/endpoints` 拿到 key_id；② 把 base_url 填成自己的
服务器，服务端就会把**明文密钥**放进 Authorization 头主动送过去（同时构成
通用 SSRF）。现在两道口子同时焊死：探测挂管理员门禁，且**不再接受 key_id**
（只能用请求里直填的 api_key，或已登记端点自己的 base_url）——服务端不再
代替调用方去取库内密钥。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import require_admin
from app.core.provider_store import (
    ProviderStoreError,
    create_endpoint,
    create_key,
    delete_endpoint,
    delete_key,
    get_default_endpoints,
    list_endpoints,
    list_keys,
    set_default_endpoints,
    update_endpoint,
    update_key,
)
from app.core.providers import probe_provider_models, provider_audio_capability

router = APIRouter(prefix="/api", tags=["provider-admin"])


def _store_error(e: ProviderStoreError):
    """存储层校验错误转 HTTP 400（信息面向用户展示）。"""
    raise HTTPException(400, str(e)) from e


# ---------- Key 管理 ----------

class KeyIn(BaseModel):
    name: str
    api_key: str
    remark: str = ""


class KeyPatchIn(BaseModel):
    name: str | None = None
    api_key: str | None = None  # 缺省/空串 = 不修改（避免脱敏值回写）
    remark: str | None = None


@router.get("/keys", dependencies=[Depends(require_admin)])
def get_keys():
    """Key 列表（api_key 脱敏，附引用端点名；仅管理员）。"""
    return list_keys()


@router.post("/keys", status_code=201, dependencies=[Depends(require_admin)])
def post_key(body: KeyIn):
    """新建 Key（仅管理员）。"""
    try:
        return create_key(body.name, body.api_key, body.remark)
    except ProviderStoreError as e:
        _store_error(e)


@router.patch("/keys/{key_id}", dependencies=[Depends(require_admin)])
def patch_key(key_id: str, body: KeyPatchIn):
    """更新 Key（api_key 未提供或空串 = 保持不变；仅管理员）。"""
    try:
        update_key(key_id, body.name, body.api_key, body.remark)
        return {"ok": True}
    except ProviderStoreError as e:
        _store_error(e)


@router.delete("/keys/{key_id}", dependencies=[Depends(require_admin)])
def remove_key(key_id: str):
    """删除 Key：引用它的端点自动解绑，返回受影响端点名列表（仅管理员）。"""
    try:
        unbound = delete_key(key_id)
        return {"ok": True, "unbound": unbound}
    except ProviderStoreError as e:
        _store_error(e)


# ---------- 模型服务端点 ----------

class EndpointIn(BaseModel):
    name: str
    kind: str  # llm|t2i|t2v|i2v|tts
    provider: str  # openai|ark|agnes|custom
    base_url: str
    key_id: str | None = None
    model: str = ""
    # 多模态标记（批次 3.5）：仅 llm 类有意义——勾选 = 视觉审片判级可用
    multimodal: bool = False
    # 计费费率（33 号方案 2026-10-09：按模型服务端点配置，按实际调用计费）
    credits_per_1k_tokens: int = 0
    credits_per_image: int = 0
    credits_per_video_second: int = 0
    bill_enabled: bool = True


class EndpointPatchIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    provider: str | None = None
    base_url: str | None = None
    key_id: str | None = None  # 显式传 null = 解绑 Key
    model: str | None = None
    multimodal: bool | None = None
    # 计费费率（提供即更新；0 = 该类消耗不计费）
    credits_per_1k_tokens: int | None = None
    credits_per_image: int | None = None
    credits_per_video_second: int | None = None
    bill_enabled: bool | None = None


@router.get("/endpoints", dependencies=[Depends(require_admin)])
def get_endpoints(kind: str | None = None):
    """端点列表（kind 可选过滤；附 key 摘要）。**仅管理员**。

    2026-09-30 用户规则：系统管理模块下的接口一律只有管理员可访问——此前
    这里对登录用户开放（只把 key_id 置空），会把端点地址/服务商这类管理信息
    暴露给普通用户。普通用户的模型选择改走专用精简接口
    `GET /api/model-options`（只含显示名/类型，不含 base_url/密钥/服务商）。
    """
    items = list_endpoints(kind)
    # 附「产物是否自带音轨」能力位（画布模型徽标用；按协议判定，未知为 null）
    for it in items:
        it["audio"] = provider_audio_capability(it.get("provider", ""))
    return items


@router.post("/endpoints", status_code=201, dependencies=[Depends(require_admin)])
def post_endpoint(body: EndpointIn):
    """新建端点（仅管理员；费率字段为 33 号方案的积分计费配置）。"""
    try:
        return create_endpoint(body.name, body.kind, body.provider,
                               body.base_url, body.key_id, body.model,
                               body.multimodal,
                               body.credits_per_1k_tokens,
                               body.credits_per_image,
                               body.credits_per_video_second,
                               body.bill_enabled)
    except ProviderStoreError as e:
        _store_error(e)


def _invalidate_pricing_caches() -> None:
    """端点费率变更后让 providers 的费率缓存即时失效（改价立即生效）。"""
    try:
        from app.core import providers
        providers.invalidate_endpoint_rates()
    except Exception:  # noqa: BLE001 失效失败不致命（TTL 30 秒兜底）
        pass


@router.patch("/endpoints/{endpoint_id}", dependencies=[Depends(require_admin)])
def patch_endpoint(endpoint_id: str, body: EndpointPatchIn):
    """更新端点（key_id 显式 null = 解绑；字段缺省 = 不修改；仅管理员）。

    费率字段（credits_per_* / bill_enabled）提供即更新——保存后费率缓存
    即时失效，计费立刻按新价走。
    """
    try:
        update_endpoint(endpoint_id, body.name, body.kind, body.provider,
                        body.base_url, body.key_id, body.model,
                        body.multimodal,
                        body.credits_per_1k_tokens, body.credits_per_image,
                        body.credits_per_video_second, body.bill_enabled)
        _invalidate_pricing_caches()
        return {"ok": True}
    except ProviderStoreError as e:
        _store_error(e)


@router.post("/endpoints/apply-suggested-prices",
             dependencies=[Depends(require_admin)])
def post_apply_suggested_prices(body: dict | None = None):
    """一键给各模型服务端点填入**建议费率**（「模型管理」页按钮；仅管理员）。

    建议值按服务类型：llm 1 分/千 token、t2i 5 分/张、t2v/i2v 3 分/秒
    （overwrite=false 默认只填完全未配置的端点，不覆盖手调值）。
    """
    from app.core import billing
    overwrite = bool((body or {}).get("overwrite", False))
    updated = billing.apply_suggested_endpoint_prices(overwrite=overwrite)
    return {"ok": True, "updated": updated}


@router.delete("/endpoints/{endpoint_id}", dependencies=[Depends(require_admin)])
def remove_endpoint(endpoint_id: str):
    """删除端点（同步清除默认配置中的引用；仅管理员）。"""
    try:
        delete_endpoint(endpoint_id)
        return {"ok": True}
    except ProviderStoreError as e:
        _store_error(e)


class ProbeIn(BaseModel):
    kind: str
    provider: str
    base_url: str
    # 仅接受直填密钥（表单「先填 key 再探测」场景）。**刻意不提供 key_id**：
    # 允许用库内已保存的密钥去请求调用方指定的 URL，等于把密钥交给任何能调
    # 这个接口的人（2026-09-30 安全整改）。
    api_key: str | None = None


@router.post("/endpoints/probe-models", dependencies=[Depends(require_admin)])
def post_probe_models(body: ProbeIn):
    """探测端点的远端模型列表（模型管理表单的模型下拉数据源；仅管理员）。

    **只使用请求里直填的 api_key**——不接受 key_id（理由见模块 docstring）。
    密钥留空时仍会探测 `{base_url}/models`（部分端点列表接口不需要鉴权），
    失败则回退精选官方模型表。
    """
    api_key = (body.api_key or "").strip()
    try:
        return probe_provider_models(body.provider.strip().lower(), body.kind,
                                     body.base_url.strip(), api_key)
    except Exception as e:  # noqa: BLE001 探测失败返回空列表而非 500
        return {"models": [], "source": "none", "error": str(e)[:200]}


# ---------- 默认配置 ----------

class DefaultsIn(BaseModel):
    defaults: dict[str, str | None]  # kind → endpoint_id（null = 清除该类）


@router.get("/defaults", dependencies=[Depends(require_admin)])
def get_defaults():
    """每类任务的默认端点（5 类齐全，未设置为 null）。**仅管理员**（2026-09-30
    用户规则；普通用户下拉的「默认：xxx」文案改走 /api/model-options）。"""
    return {"defaults": get_default_endpoints()}


@router.put("/defaults", dependencies=[Depends(require_admin)])
def put_defaults(body: DefaultsIn):
    """写入每类任务的默认端点（校验端点存在且模型类型匹配；仅管理员）。"""
    try:
        return {"defaults": set_default_endpoints(body.defaults)}
    except ProviderStoreError as e:
        _store_error(e)
