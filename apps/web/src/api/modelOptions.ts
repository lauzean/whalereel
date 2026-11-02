/** 生成模型选项数据源：本地注册表 + 模型服务端点（第三方）合并，多组件共用。
 *
 * **权限口径（2026-09-30 用户规则）**：系统管理模块的接口一律管理员专属——
 * `/api/models`、`/api/endpoints`、`/api/defaults` 都已收成管理员可访问。
 * 普通用户的模型下拉改走**精简清单接口** `GET /api/model-options`
 * （本地模型 + 每个端点的默认模型一条，不含端点地址/密钥/服务商）；
 * 管理员保持原有数据源（能列出每个端点探测到的全部远端模型），体验不变。
 *
 * 端点选项的任务级模型串为 "{endpoint_id}::{model_id}"（后端按端点配置路由请求），
 * 本地模型选项仍为裸模型 ID。60s TTL 缓存，端点/Key/默认配置增删改后调
 * clearModelOptionsCache() 立即失效。
 */

import {
  api,
  type EndpointInfo,
  type EndpointKind,
  type ModelInfo,
  type ModelOption,
  type ModelPresetKind,
} from "./client";
import { useAuth } from "../stores/auth";

/** 选项缓存 TTL（毫秒）：避免每次打开下拉都打远端探测 */
const OPTIONS_TTL_MS = 60_000;

const optionsCache = new Map<string, { at: number; data: ModelInfo[] }>();

/** 精简清单整包缓存（普通用户；模型与默认标签同源返回，一次拉取共用） */
let packCache: { at: number; models: ModelOption[]; defaults: Record<string, string> } | null = null;

/** 当前用户是否管理员（管理员保留原数据源；未登录按非管理员处理） */
function isAdmin(): boolean {
  return Boolean(useAuth.getState().user?.is_admin);
}

/** 端点/Key/默认配置增删改后调用：清空选项与全局默认缓存 */
export function clearModelOptionsCache(): void {
  optionsCache.clear();
  packCache = null;
  knownIds = null;
  warming = null;
}

// ---------- 普通用户：精简清单（/api/model-options） ----------

/** 拉取（带 TTL 缓存）整份精简清单；失败返回空（下拉退化，不阻塞页面） */
async function fetchPack(): Promise<NonNullable<typeof packCache>> {
  if (packCache && Date.now() - packCache.at < OPTIONS_TTL_MS) return packCache;
  try {
    const r = await api.modelOptions();
    const defaults: Record<string, string> = {};
    for (const [kind, v] of Object.entries(r.defaults ?? {})) {
      const label = (v as { label?: string } | null)?.label;
      if (label) defaults[kind] = label;
    }
    packCache = { at: Date.now(), models: r.models ?? [], defaults };
  } catch {
    // 拿不到清单时给空结果（与旧实现「接口失败 → 空下拉」一致）
    packCache = { at: Date.now(), models: [], defaults: {} };
  }
  return packCache;
}

/** 精简条目 → 下拉用的 ModelInfo（缺省字段填空；端点条目视作已安装） */
function toModelInfo(o: ModelOption): ModelInfo {
  return {
    id: o.id,
    family: o.source === "endpoint" ? "provider" : "",
    task_type: o.task_type,
    display_name: o.display_name,
    quant: "", vram_mb: 0, repo_id: "", files: [],
    notes: o.source === "endpoint" ? "第三方服务模型" : "",
    installed: o.installed, install_path: "", install: null as never,
    audio: o.audio ?? null,
  };
}

// ---------- 管理员：原数据源（本地注册表 + 端点探测） ----------

/** 构造下拉用的伪 ModelInfo（端点模型：installed 恒 true，不显示「未安装」） */
function pseudoModel(id: string, display: string, taskType: string,
                     audio: boolean | null = null): ModelInfo {
  return {
    id, family: "provider", task_type: taskType,
    display_name: display, quant: "", vram_mb: 0,
    repo_id: "", files: [], notes: "第三方服务模型",
    installed: true, install_path: "", install: null as never,
    audio,
  };
}

/** 端点默认模型兜底：probe 失败时用端点已存的 model 生成一条选项 */
function endpointFallbackItem(ep: EndpointInfo): ModelInfo | null {
  if (!ep.model) return null;
  return pseudoModel(`${ep.id}::${ep.model}`, `${ep.name} · ${ep.model}`,
                     ep.kind, ep.audio ?? null);
}

/** 拉取一个端点的模型选项（管理员可探测远端列表；失败回退端点默认模型） */
async function fetchEndpointItems(ep: EndpointInfo): Promise<ModelInfo[]> {
  try {
    const r = await api.probeEndpointModels({
      kind: ep.kind, provider: ep.provider, base_url: ep.base_url,
    });
    // 音频能力是**端点**的属性（后端按协议判定），同一个端点的每个模型都一样
    const items = r.models.map((m) =>
      pseudoModel(`${ep.id}::${m.id}`, `${ep.name} · ${m.display_name || m.id}`,
                 ep.kind, ep.audio ?? null));
    if (items.length > 0) return items;
  } catch {
    // 探测失败（网络/端点不可用）：回退端点默认模型
  }
  const fallback = endpointFallbackItem(ep);
  return fallback ? [fallback] : [];
}

/** 管理员选项 = 本地注册表（按 task_type 过滤）+ 端点探测模型 */
async function fetchAdminOptions(kind: ModelPresetKind): Promise<ModelInfo[]> {
  const local: ModelInfo[] = await api.listModels()
    .then((all) => all.filter((m) => m.task_type === kind))
    .catch(() => []);
  local.sort((a, b) => Number(b.installed) - Number(a.installed));

  const endpoints = await api.listEndpoints(kind as EndpointKind)
    .catch(() => [] as EndpointInfo[]);
  const endpointItems = (await Promise.all(endpoints.map(fetchEndpointItems))).flat();
  return [...local, ...endpointItems];
}

// ---------- 对外：下拉选项 / 默认标签 / 已知模型 id ----------

/** 生成模型下拉选项（普通用户走精简清单；管理员保持原数据源） */
export async function fetchModelOptions(kind: ModelPresetKind): Promise<ModelInfo[]> {
  const cached = optionsCache.get(kind);
  if (cached && Date.now() - cached.at < OPTIONS_TTL_MS) return cached.data;

  let data: ModelInfo[];
  if (isAdmin()) {
    data = await fetchAdminOptions(kind);
  } else {
    const pack = await fetchPack();
    const items = pack.models.filter((m) => m.task_type === kind);
    // installed 的本地模型排前（未安装仍列出，选择时后端会明确报错）
    items.sort((a, b) => Number(b.installed) - Number(a.installed));
    data = items.map(toModelInfo);
  }
  optionsCache.set(kind, { at: Date.now(), data });
  return data;
}

/** 该类任务全局默认端点的展示名（如「Agnes 生图 · agnes-image-2.5-flash」；
 * 未设置返回 null，调用方使用原文案）。数据来自精简清单，管理员/普通用户同源。 */
export async function defaultEmptyLabel(kind: ModelPresetKind): Promise<string | null> {
  const pack = await fetchPack();
  return pack.defaults[kind] ?? null;
}

// ---- 已知模型 id 缓存：画布 run.ts 旧节点模型迁移校验放行用 ----

let knownIds: Set<string> | null = null;
let warming: Promise<void> | null = null;

/** 预热全部已知模型 id（本地注册表 + 端点默认模型裸 id），CanvasView 挂载时调用 */
export function warmKnownModelIds(): Promise<void> {
  if (warming) return warming;
  warming = (async () => {
    const ids = new Set<string>();
    const pack = await fetchPack();
    for (const m of pack.models) {
      ids.add(m.id);
      // 端点默认模型的**裸 id** 也纳入（端点编码串由 run.ts 显式放行，不依赖此集合）
      const i = m.id.indexOf("::");
      if (i >= 0) ids.add(m.id.slice(i + 2));
    }
    knownIds = ids;
  })();
  return warming;
}

/** 读取已预热的已知模型 id 集合（未预热/预热失败返回 null，调用方按放行处理） */
export function peekKnownModelIds(): Set<string> | null {
  return knownIds;
}

/** 判定任务级模型串是否为端点编码串（"{endpoint_id}::{model_id}"） */
export function isEndpointModelId(modelId: string): boolean {
  return (modelId || "").includes("::");
}

// ---------- 音频能力（画布视频节点徽标） ----------

/** 模型 id → 是否有原生音频。**只收已知项**（未列入 = 未知，界面不显示断言）。 */
export async function fetchAudioCaps(): Promise<Record<string, boolean>> {
  const caps: Record<string, boolean> = {};
  const put = (id: string, audio: boolean | null | undefined) => {
    if (typeof audio === "boolean") caps[id] = audio;
  };
  if (isAdmin()) {
    // 管理员：本地注册表（逐模型登记）+ 端点（后端按协议判定，见 /api/endpoints）
    const local = await api.listModels().catch(() => [] as ModelInfo[]);
    for (const m of local) put(m.id, m.audio);
    const eps = await api.listEndpoints().catch(() => [] as EndpointInfo[]);
    for (const ep of eps) {
      if (!ep.model) continue;
      put(`${ep.id}::${ep.model}`, ep.audio);
    }
  } else {
    // 普通用户：精简清单（本地模型 + 各端点默认模型，同样带 audio 字段）
    const pack = await fetchPack();
    for (const m of pack.models) put(m.id, m.audio);
  }
  return caps;
}
