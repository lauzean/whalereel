/** 默认配置内容区：五类任务各选一个默认端点（从模型管理录入的端点中选择）。

运行时解析链：任务显式模型 > 项目预设 > 此处的全局默认端点 > .env 回退层。
i2v 未设置时自动跟随 t2v 的默认端点（视频类 runner 共用 t2v 类别）。
 */

import { useEffect, useState } from "react";
import {
  api,
  ENDPOINT_KINDS,
  ENDPOINT_KIND_LABELS,
  type EndpointInfo,
  type EndpointKind,
  type GlobalDefaults,
  type GlobalDefaultsBody,
} from "./api/client";
import { clearModelOptionsCache } from "./api/modelOptions";
import { toast } from "./ui/feedback";

export function DefaultConfigContent() {
  const [endpoints, setEndpoints] = useState<EndpointInfo[]>([]);
  const [defaults, setDefaults] = useState<GlobalDefaults>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    Promise.all([api.listEndpoints(), api.getDefaults()])
      .then(([eps, d]) => {
        setEndpoints(eps);
        setDefaults(d.defaults);
      })
      .catch((e) => {
        toast.error(`加载失败：${e instanceof Error ? e.message : String(e)}`);
      });
  }, []);

  /** 保存全部：仅提交有值的类别（值 = 端点 ID；null/未设置不发送） */
  const save = async () => {
    setSaving(true);
    try {
      const body: GlobalDefaultsBody = {};
      ENDPOINT_KINDS.forEach((kind) => {
        const v = defaults[kind];
        if (v?.endpoint_id) body[kind] = v.endpoint_id;
      });
      const r = await api.updateDefaults(body);
      setDefaults(r.defaults);
      clearModelOptionsCache();
      toast.success("默认配置已保存，即时生效");
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  const pick = (kind: EndpointKind, endpointId: string) => {
    setDefaults((prev) => ({
      ...prev,
      [kind]: endpointId
        ? {
            endpoint_id: endpointId,
            endpoint_name: endpoints.find((e) => e.id === endpointId)?.name ?? "",
            provider: endpoints.find((e) => e.id === endpointId)?.provider ?? "",
            model: endpoints.find((e) => e.id === endpointId)?.model ?? "",
          }
        : null,
    }));
  };

  return (
    <div className="provider-body">
      <div className="provider-section">
        <div className="provider-section-title">每类任务的默认端点</div>
        <div className="provider-fields ep-form">
          {ENDPOINT_KINDS.map((kind) => {
            const candidates = endpoints.filter((e) => e.kind === kind);
            const current = defaults[kind]?.endpoint_id ?? "";
            return (
              <div key={kind} className="provider-field">
                <label>{ENDPOINT_KIND_LABELS[kind]}</label>
                <select
                  value={current}
                  onChange={(e) => pick(kind, e.target.value)}
                >
                  <option value="">
                    {kind === "i2v" ? "未设置（跟随文生视频默认）" : "未设置（回退 .env/本地）"}
                  </option>
                  {candidates.map((ep) => (
                    <option key={ep.id} value={ep.id}>
                      {ep.name} · {ep.model || "默认模型"}
                    </option>
                  ))}
                </select>
                {kind === "i2v" && (
                  <span className="provider-local-hint">
                    ref2v 等视频任务与文生视频共用同一类别
                  </span>
                )}
              </div>
            );
          })}
          {endpoints.length === 0 && (
            <div className="provider-local-hint">
              请先在「模型管理」中录入模型服务端点
            </div>
          )}
          <button className="provider-save ep-save" disabled={saving} onClick={() => void save()}>
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
