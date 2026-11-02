/** 模型管理内容区：Key 管理 + 模型服务端点 CRUD（系统配置页，替代旧服务配置表单）。

一条端点 = 名称 + 模型类型 + 调用协议 + 服务地址 + Key + 默认模型；
默认模型下拉经端点探测自动获取（远端 /models，失败回退精选表/手填）。
保存后调用方需刷新模型下拉缓存（clearModelOptionsCache）。
 */

import { useCallback, useEffect, useState } from "react";
import {
  api,
  ENDPOINT_KINDS,
  ENDPOINT_KIND_LABELS,
  ENDPOINT_PROVIDER_HINTS,
  ENDPOINT_PROVIDER_KINDS,
  ENDPOINT_PROVIDER_OPTIONS,
  type EndpointInfo,
  type EndpointKind,
  type EndpointProvider,
  type KeyInfo,
} from "./api/client";
import { clearModelOptionsCache } from "./api/modelOptions";
import { confirmDialog, toast } from "./ui/feedback";

/** Key 管理子区：列表 + 行内新增/编辑/删除 */
function KeysSection({ keys, onChange }: { keys: KeyInfo[]; onChange: () => void }) {
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [remark, setRemark] = useState("");

  const reset = () => {
    setAdding(false);
    setEditingId(null);
    setName("");
    setApiKey("");
    setRemark("");
  };

  const save = async () => {
    try {
      if (editingId) {
        await api.updateKey(editingId, {
          name: name.trim(),
          api_key: apiKey.trim() || undefined, // 留空 = 不修改
          remark: remark.trim(),
        });
        toast.success("Key 已更新");
      } else {
        await api.createKey({ name: name.trim(), api_key: apiKey.trim(), remark: remark.trim() });
        toast.success("Key 已创建");
      }
      reset();
      onChange();
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const remove = async (k: KeyInfo) => {
    const ok = await confirmDialog({
      title: "删除 Key",
      message: `确定删除 Key「${k.name}」？引用它的端点将自动解绑（共 ${k.used_by.length} 个）。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      const r = await api.deleteKey(k.id);
      toast.success(
        r.unbound.length > 0 ? `已删除，并从 ${r.unbound.length} 个端点解绑` : "Key 已删除");
      onChange();
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const formOpen = adding || editingId !== null;
  return (
    <div className="provider-section">
      <div className="provider-section-title">
        API Key 管理
        <button
          className="mm-btn"
          onClick={() => (formOpen ? reset() : setAdding(true))}
        >
          {formOpen ? "收起" : "+ 新增 Key"}
        </button>
      </div>
      {formOpen && (
        <div className="provider-fields ep-form">
          <div className="provider-field">
            <label>Key 名称</label>
            <input value={name} placeholder="如 Agnes 主力 Key"
              onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="provider-field">
            <label>API Key{editingId ? "（留空保持不变）" : ""}</label>
            <input type="password" value={apiKey}
              placeholder={editingId ? "留空保持现有 Key 不变" : "粘贴服务商控制台生成的 Key"}
              onChange={(e) => setApiKey(e.target.value)} />
          </div>
          <div className="provider-field">
            <label>备注（可选）</label>
            <input value={remark} placeholder="用途/额度说明"
              onChange={(e) => setRemark(e.target.value)} />
          </div>
          <button className="provider-save ep-save" onClick={() => void save()}>
            {editingId ? "保存修改" : "创建 Key"}
          </button>
        </div>
      )}
      <div className="mm-list">
        {keys.length === 0 && <div className="provider-local-hint">还没有 Key，先新增一个</div>}
        {keys.map((k) => (
          <div key={k.id} className="mm-item">
            <div className="mm-info">
              <div className="mm-name">{k.name}</div>
              <div className="mm-meta">
                {k.api_key}
                {k.remark && ` · ${k.remark}`}
                {k.used_by.length > 0 && ` · 被 ${k.used_by.length} 个端点引用`}
              </div>
            </div>
            <div className="mm-right">
              <button className="mm-btn" onClick={() => {
                setEditingId(k.id);
                setAdding(false);
                setName(k.name);
                setApiKey("");
                setRemark(k.remark);
              }}>
                编辑
              </button>
              <button className="mm-btn" onClick={() => void remove(k)}>删除</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 端点表单状态（新增/编辑共用） */
interface EndpointForm {
  name: string;
  kind: EndpointKind;
  provider: EndpointProvider;
  base_url: string;
  key_id: string;
  model: string;
  /** 支持多模态（批次 3.5，仅 llm 类）：视觉审片判级准入 */
  multimodal: boolean;
  /**
   * 探测用的临时 Key（**不进库**，只在「探测模型列表」这一次请求里用）。
   *
   * 为什么不能拿已保存的 Key 去探测（2026-09-30 安全整改）：探测要把密钥放进
   * Authorization 头、发往 base_url，而 base_url 由表单决定——如果服务端肯替你
   * 取库里的密钥，任何能调这个接口的人都能把 base_url 填成自己的服务器，让服务端
   * 把明文密钥送过去。现在服务端只接受请求里直填的 key，所以这里**必须**有输入框：
   * 想探测就先粘一次 Key（保存端点仍用上面选的已存 Key）。
   */
  probe_key: string;
  /** 计费费率（33 号方案，2026-10-09：按模型服务端点配置）——字符串便于输入，
   *  保存时转整数；对应 kind 的那一项才有意义（llm=每千 token、t2i=每张图、
   *  t2v/i2v=每秒视频），0 = 该类消耗不计费。 */
  bill_p1k: string;
  bill_img: string;
  bill_sec: string;
  /** 计费开关（关 = 该服务的全部调用不计费） */
  bill_enabled: boolean;
}

const EMPTY_FORM: EndpointForm = {
  name: "", kind: "t2i", provider: "agnes", base_url: "", key_id: "", model: "",
  multimodal: false, probe_key: "",
  bill_p1k: "0", bill_img: "0", bill_sec: "0", bill_enabled: true,
};

/** 端点费率摘要（端点列表行展示；未配置 = 该服务免费） */
function rateSummaryOf(ep: EndpointInfo): string {
  if (!ep.bill_enabled) return "计费：已关闭（不扣分）";
  const parts: string[] = [];
  if (ep.credits_per_1k_tokens > 0) {
    parts.push(`${ep.credits_per_1k_tokens} 分/千 token`);
  }
  if (ep.credits_per_image > 0) parts.push(`${ep.credits_per_image} 分/张图`);
  if (ep.credits_per_video_second > 0) {
    parts.push(`${ep.credits_per_video_second} 分/秒视频`);
  }
  return parts.length ? `计费：${parts.join(" · ")}` : "计费：未配置（免费）";
}

function EndpointsSection({
  endpoints,
  keys,
  onChange,
}: {
  endpoints: EndpointInfo[];
  keys: KeyInfo[];
  onChange: () => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<EndpointForm>(EMPTY_FORM);
  // 模型探测结果（null = 未探测/失败，回退手填输入框）
  const [probed, setProbed] = useState<string[] | null>(null);
  const [probing, setProbing] = useState(false);
  // Key 内联新增（端点表单里快速建 Key）
  const [inlineKey, setInlineKey] = useState(false);
  const [inlineKeyName, setInlineKeyName] = useState("");
  const [inlineKeyValue, setInlineKeyValue] = useState("");

  const patch = (part: Partial<EndpointForm>) => setForm((prev) => ({ ...prev, ...part }));

  const reset = () => {
    setEditingId(null);
    setAdding(false);
    setForm(EMPTY_FORM);
    setProbed(null);
    setInlineKey(false);
    setInlineKeyName("");
    setInlineKeyValue("");
  };

  /** 探测模型列表：base_url 就绪 + 有临时 Key 时拉取远端 /models；
   * comfyui 是本地服务（无鉴权）——不要求 Key，探测返回的是内置工作流模板 */
  const probe = async () => {
    if (!form.base_url.trim()) {
      toast.warning("请先填写服务地址");
      return;
    }
    if (form.provider !== "comfyui" && !form.probe_key.trim()) {
      toast.warning("请先在「探测用 Key」里粘贴一次密钥（服务端不再代取已保存的 Key）");
      return;
    }
    setProbing(true);
    try {
      const r = await api.probeEndpointModels({
        kind: form.kind,
        provider: form.provider,
        base_url: form.base_url.trim(),
        api_key: form.probe_key.trim(),
      });
      setProbed(r.models.map((m) => m.id));
      if (r.models.length === 0) {
        // 本地服务连不上时后端会给具体原因（error），优先展示而不是空列表
        toast.warning(r.error
          ? `探测失败：${r.error}`
          : `未获取到模型列表（${r.source}），可手动填写模型 ID`);
      }
    } catch (e) {
      setProbed(null);
      toast.error(`探测失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setProbing(false);
    }
  };

  /** 内联新增 Key：创建后自动选中 */
  const createInlineKey = async () => {
    if (!inlineKeyName.trim() || !inlineKeyValue.trim()) return;
    try {
      const k = await api.createKey({
        name: inlineKeyName.trim(), api_key: inlineKeyValue.trim(),
      });
      toast.success("Key 已创建");
      patch({ key_id: k.id });
      setInlineKey(false);
      setInlineKeyName("");
      setInlineKeyValue("");
      onChange();
    } catch (e) {
      toast.error(`创建失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const save = async () => {
    if (!form.name.trim()) {
      toast.warning("请填写端点名称");
      return;
    }
    // 费率转整数并校验非负（字符串输入）
    const rates = [form.bill_p1k, form.bill_img, form.bill_sec].map(
      (v) => Number(v || 0));
    if (rates.some((n) => !Number.isFinite(n) || n < 0)) {
      toast.warning("计费费率必须是不小于 0 的数字");
      return;
    }
    const body = {
      name: form.name.trim(),
      kind: form.kind,
      provider: form.provider,
      base_url: form.base_url.trim(),
      key_id: form.key_id || null,
      model: form.model.trim(),
      multimodal: form.kind === "llm" ? form.multimodal : false,
      // 计费费率（33 号方案）：哪类有消耗算哪类，0 = 不计费
      credits_per_1k_tokens: Math.round(rates[0]),
      credits_per_image: Math.round(rates[1]),
      credits_per_video_second: Math.round(rates[2]),
      bill_enabled: form.bill_enabled,
    };
    try {
      if (editingId) {
        await api.updateEndpoint(editingId, body);
        toast.success("端点已更新");
      } else {
        await api.createEndpoint(body);
        toast.success("端点已创建");
      }
      clearModelOptionsCache();
      reset();
      onChange();
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const remove = async (ep: EndpointInfo) => {
    const ok = await confirmDialog({
      title: "删除端点",
      message: `确定删除端点「${ep.name}」？若它是某类任务的默认端点，该默认将被清除。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteEndpoint(ep.id);
      toast.success("端点已删除");
      clearModelOptionsCache();
      onChange();
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const startEdit = (ep: EndpointInfo) => {
    setAdding(false);
    setEditingId(ep.id);
    setForm({
      name: ep.name, kind: ep.kind, provider: ep.provider,
      base_url: ep.base_url, key_id: ep.key_id ?? "", model: ep.model,
      multimodal: !!ep.multimodal,
      probe_key: "",   // 探测用临时 Key 每次编辑都清空（绝不从已存 Key 回填）
      bill_p1k: String(ep.credits_per_1k_tokens ?? 0),
      bill_img: String(ep.credits_per_image ?? 0),
      bill_sec: String(ep.credits_per_video_second ?? 0),
      bill_enabled: ep.bill_enabled ?? true,
    });
    setProbed(ep.model ? [ep.model] : null); // 已存模型先作为可选项
    setInlineKey(false);
  };

  /** 一键给未配置的端点填入建议费率（33 号方案；不覆盖手调值） */
  const applySuggestions = async () => {
    const ok = await confirmDialog({
      title: "应用建议费率",
      message: "将给**尚未配置费率**的模型服务端点填入建议值：对话模型 1 分/千 "
        + "token、生图 5 分/张、生视频 3 分/秒；已手动配过的端点不会被覆盖。"
        + "之后可在各端点里按实际成本调整。应用后积分计费即按新费率结算。",
      confirmText: "应用建议费率",
    });
    if (!ok) return;
    try {
      const r = await api.applySuggestedEndpointPrices(false);
      toast.success(`已应用建议费率（更新 ${r.updated} 个端点）——计费即时生效`);
      onChange();
    } catch (e) {
      toast.error(`应用失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const hint = ENDPOINT_PROVIDER_HINTS[form.provider];
  const formOpen = adding || editingId !== null;
  return (
    <div className="provider-section">
      <div className="provider-section-title">
        模型服务端点
        <button
          className="mm-btn"
          title="给未配置费率的端点填入建议值（可再逐个调整）"
          onClick={() => void applySuggestions()}
        >
          应用建议费率
        </button>
        <button
          className="mm-btn"
          onClick={() => (formOpen ? reset() : setAdding(true))}
        >
          {formOpen ? "收起" : "+ 新增端点"}
        </button>
      </div>
      {formOpen && (
        <div className="provider-fields ep-form">
          <div className="provider-field">
            <label>端点名称</label>
            <input value={form.name} placeholder="如 Agnes 生图"
              onChange={(e) => patch({ name: e.target.value })} />
          </div>
          <div className="provider-field">
            <label>模型类型</label>
            <select value={form.kind}
              onChange={(e) => patch({ kind: e.target.value as EndpointKind })}>
              {/* 协议对类型有限制时只列可用项（如 comfyui 目前只有视频工作流） */}
              {(ENDPOINT_PROVIDER_KINDS[form.provider] ?? ENDPOINT_KINDS).map((k) => (
                <option key={k} value={k}>{ENDPOINT_KIND_LABELS[k]}</option>
              ))}
            </select>
          </div>
          <div className="provider-field">
            <label>服务协议</label>
            <select value={form.provider}
              onChange={(e) => {
                const provider = e.target.value as EndpointProvider;
                const allowed = ENDPOINT_PROVIDER_KINDS[provider];
                patch({
                  provider,
                  // 协议对类型有限制时同步收拢类型（否则保存必被后端拒绝）
                  kind: allowed && !allowed.includes(form.kind) ? allowed[0] : form.kind,
                  // 切协议时若地址还是空/上个协议提示值，预填新协议提示地址
                  base_url: !form.base_url || Object.values(ENDPOINT_PROVIDER_HINTS)
                    .some((h) => h.base_url === form.base_url)
                    ? ENDPOINT_PROVIDER_HINTS[provider].base_url
                    : form.base_url,
                });
              }}>
              {ENDPOINT_PROVIDER_OPTIONS.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </div>
          <div className="provider-field">
            <label>服务地址 base_url</label>
            <input value={form.base_url} placeholder={hint.base_url}
              onChange={(e) => patch({ base_url: e.target.value })} />
          </div>
          <div className="provider-field">
            <label>API Key</label>
            {!inlineKey ? (
              <div className="ep-key-row">
                <select value={form.key_id}
                  onChange={(e) => patch({ key_id: e.target.value })}>
                  <option value="">未选择{keys.length === 0 ? "（请先新增 Key）" : ""}</option>
                  {keys.map((k) => (
                    <option key={k.id} value={k.id}>{k.name}（{k.api_key}）</option>
                  ))}
                </select>
                <button className="mm-btn" onClick={() => setInlineKey(true)}>+ 新增 Key</button>
              </div>
            ) : (
              <div className="ep-key-row">
                <input value={inlineKeyName} placeholder="Key 名称"
                  onChange={(e) => setInlineKeyName(e.target.value)} />
                <input type="password" value={inlineKeyValue} placeholder="API Key 值"
                  onChange={(e) => setInlineKeyValue(e.target.value)} />
                <button className="mm-btn-primary" onClick={() => void createInlineKey()}>创建</button>
                <button className="mm-btn" onClick={() => setInlineKey(false)}>取消</button>
              </div>
            )}
          </div>
          {form.provider === "comfyui" && (
            <div className="provider-local-hint">
              本机 ComfyUI 无鉴权：API Key 留空即可（不用新增 Key）
            </div>
          )}
          <div className="provider-field">
            <label>默认模型{probed && probed.length > 0 ? "（探测到，可直接选择）" : ""}</label>
            <div className="ep-key-row">
              {probed && probed.length > 0 ? (
                <select value={form.model}
                  onChange={(e) => patch({ model: e.target.value })}>
                  <option value="">不指定（用协议精选默认）</option>
                  {probed.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              ) : (
                <input value={form.model} placeholder={hint.model}
                  onChange={(e) => patch({ model: e.target.value })} />
              )}
              <button className="mm-btn" disabled={probing}
                onClick={() => void probe()}>
                {probing ? "获取中…" : "⟳ 获取模型列表"}
              </button>
            </div>
          </div>
          {/* comfyui 是本地无鉴权服务：探测不需要 Key，该字段隐藏 */}
          {form.provider !== "comfyui" && (
            <div className="provider-field">
              <label title="只在这一次探测请求里使用，不会保存到服务端">
                探测用 Key（不保存，仅本次探测）
              </label>
              <input
                type="password"
                value={form.probe_key}
                placeholder="粘贴一次 API Key 即可探测；留空只能手动填模型 ID"
                onChange={(e) => patch({ probe_key: e.target.value })}
              />
            </div>
          )}
          {form.kind === "llm" && (
            <label className="ep-multimodal" title="勾选后可用于视觉审片判级（把镜头抽帧拼图发给该模型判级）">
              <input
                type="checkbox"
                checked={form.multimodal}
                onChange={(e) => patch({ multimodal: e.target.checked })}
              />
              支持多模态（可接收图片输入）
            </label>
          )}
          {/* 计费费率（33 号方案 2026-10-09）：积分按本服务的实际调用计费——
              哪类有消耗算哪类；全 0 = 本服务免费。按模型类型只显示相关一项 */}
          <div className="provider-field ep-billing">
            <label title="每次调用按实际用量 × 本费率扣积分；留 0 表示该类消耗不计费">
              计费费率（积分）
            </label>
            <div className="ep-bill-row">
              {form.kind === "llm" && (
                <label>每千 token
                  <input type="number" min={0} value={form.bill_p1k}
                         onChange={(e) => patch({ bill_p1k: e.target.value })} />
                  分
                </label>
              )}
              {form.kind === "t2i" && (
                <label>每张图
                  <input type="number" min={0} value={form.bill_img}
                         onChange={(e) => patch({ bill_img: e.target.value })} />
                  分
                </label>
              )}
              {(form.kind === "t2v" || form.kind === "i2v") && (
                <label>每秒视频
                  <input type="number" min={0} value={form.bill_sec}
                         onChange={(e) => patch({ bill_sec: e.target.value })} />
                  分
                </label>
              )}
              {form.kind === "tts" && (
                <span className="billing-note">语音暂不参与按量计费</span>
              )}
              <label className="ep-bill-enable"
                     title="关闭后该服务的全部调用都不计费">
                <input type="checkbox" checked={form.bill_enabled}
                       onChange={(e) => patch({ bill_enabled: e.target.checked })} />
                计入计费
              </label>
            </div>
          </div>
          <button className="provider-save ep-save" onClick={() => void save()}>
            {editingId ? "保存修改" : "创建端点"}
          </button>
        </div>
      )}
      <div className="mm-list">
        {endpoints.length === 0 && (
          <div className="provider-local-hint">还没有端点，新增后即可在各处模型下拉中选择</div>
        )}
        {endpoints.map((ep) => (
          <div key={ep.id} className="mm-item">
            <div className="mm-info">
              <div className="mm-name">
                {ep.name}
                <span className="ep-kind-tag">{ENDPOINT_KIND_LABELS[ep.kind]}</span>
              </div>
              <div className="mm-meta">
                {ep.provider} · {ep.base_url} · {ep.key_name || "未绑 Key"} · 模型 {ep.model || "默认"}
              </div>
              {/* 计费费率摘要（33 号方案）：一眼看到每个服务多少钱 */}
              <div className="mm-meta ep-bill-meta">{rateSummaryOf(ep)}</div>
            </div>
            <div className="mm-right">
              <button className="mm-btn" onClick={() => startEdit(ep)}>编辑</button>
              <button className="mm-btn" onClick={() => void remove(ep)}>删除</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 模型管理内容区（Key 管理 + 端点 CRUD），数据变更后统一刷新 */
export function EndpointsContent() {
  const [keys, setKeys] = useState<KeyInfo[]>([]);
  const [endpoints, setEndpoints] = useState<EndpointInfo[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(() => {
    Promise.all([api.listKeys(), api.listEndpoints()])
      .then(([k, e]) => {
        setKeys(k);
        setEndpoints(e);
      })
      .catch((err) => {
        toast.error(`加载失败：${err instanceof Error ? err.message : String(err)}`);
      });
  }, []);

  useEffect(() => {
    refresh();
    setLoading(false);
  }, [refresh]);

  if (loading) return <div className="provider-loading">加载中...</div>;
  return (
    <div className="provider-body">
      <KeysSection keys={keys} onChange={refresh} />
      <EndpointsSection endpoints={endpoints} keys={keys} onChange={refresh} />
    </div>
  );
}
