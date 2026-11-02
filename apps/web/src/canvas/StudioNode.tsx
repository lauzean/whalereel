/** 统一模态节点卡片：空白态（上传/生成）+ 抽卡版本抽屉 + @引用 + 运镜（v0.2 4.3-4.5）。 */

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { memo, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, fileUrl } from "../api/client";
import { BILLING_KEYS } from "../billingKeys";
import { useCostTip } from "../stores/billing";
import { toast } from "../ui/feedback";
import { runNode } from "./run";
import { NODE_SPECS, previewKindOf } from "./specs";
import { useCanvas, type StudioNodeData } from "../stores/canvas";
import { MentionPicker } from "./MentionPicker";

const STATUS_META: Record<string, { color: string; label: string }> = {
  idle: { color: "#8b93a7", label: "就绪" },
  queued: { color: "#f0b429", label: "排队中" },
  loading_model: { color: "#f0b429", label: "加载模型" },
  running: { color: "#4cc2ff", label: "生成中" },
  done: { color: "#3fd68f", label: "完成" },
  cached: { color: "#3fd68f", label: "缓存" },
  failed: { color: "#ff5c5c", label: "失败" },
  canceled: { color: "#8b93a7", label: "已取消" },
};

/** 空版本数组常量：选择器返回稳定引用，避免每次快照新建数组触发无限渲染 */
const EMPTY_VERSIONS: never[] = [];

export const StudioNode = memo(({ id, data, selected }: NodeProps) => {
  const d = data as StudioNodeData;
  const spec = NODE_SPECS[d.specType];
  const task = useCanvas((s) => s.tasks[id]);
  const versions = useCanvas((s) => s.versions[id] ?? EMPTY_VERSIONS);
  const cameraMoves = useCanvas((s) => s.cameraMoves);
  const audioCaps = useCanvas((s) => s.audioCaps);
  const updateNodeParam = useCanvas((s) => s.updateNodeParam);
  const selectVersion = useCanvas((s) => s.selectVersion);
  const uploadFile = useCanvas((s) => s.uploadFile);
  const removeRef = useCanvas((s) => s.removeRef);
  // 生成按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const runCostTip = useCostTip(BILLING_KEYS.createTask);

  const [showVersions, setShowVersions] = useState(false);
  const [showMention, setShowMention] = useState(false);
  const [errCopied, setErrCopied] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const previewKind = previewKindOf(d.specType);

  // 预览灯箱：Esc 关闭
  useEffect(() => {
    if (!previewUrl) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPreviewUrl(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [previewUrl]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  if (!spec) return null;
  const status = task?.status ?? "idle";
  const meta = STATUS_META[status] ?? STATUS_META.idle;
  const progress = task?.progress ?? 0;
  const hasResult = !!d.artifactId;
  const busy = status === "running" || status === "queued" || status === "loading_model";
  const nodeTitle = String(d.params.name || spec.title);
  const isVideoModel = d.specType === "video";
  const modelId = String(d.params.model || spec.defaultModel || "");

  // ---------- 预览区 ----------

  const renderPreview = () => {
    if (!hasResult) {
      // 空白态：上传或输入提示词生成（复刻小云雀空白节点）
      return (
        <div className="preview blank">
          {spec.uploadAccept && (
            <>
              <button className="upload-btn nodrag" onClick={() => fileInputRef.current?.click()}>
                上传素材
              </button>
              <span className="blank-tip">或输入提示词生成</span>
            </>
          )}
          {!spec.uploadAccept && <span className="blank-tip">填写内容</span>}
        </div>
      );
    }
    const url = fileUrl(d.artifactId!);
    if (previewKind === "video") {
      return (
        <video
          className="nodrag preview clickable"
          src={url}
          controls
          preload="metadata"
          title="点击放大预览"
          onClick={() => setPreviewUrl(url)}
        />
      );
    }
    if (previewKind === "image") {
      return (
        <img
          className="nodrag preview clickable"
          src={url}
          alt="产物"
          title="点击放大预览"
          onClick={() => setPreviewUrl(url)}
        />
      );
    }
    if (previewKind === "audio") {
      return <audio className="preview" src={url} controls preload="metadata" />;
    }
    return null;
  };

  // ---------- 参数 ----------

  const renderParam = (pid: string, label: string, kind: string,
    options?: { value: string; label: string }[],
    placeholder?: string) => {
    const value = d.params[pid] ?? "";
    if (kind === "textarea" || kind === "text") {
      // textarea（提示词等多行）4 行起步；text（种子等单行）1 行且不受 min-height 影响
      const singleLine = kind === "text";
      return (
        <label key={pid} className="param">
          <span className="param-label">{label}</span>
          <textarea
            rows={singleLine ? 1 : 4}
            className={singleLine ? "single-line" : ""}
            value={value as string}
            placeholder={placeholder || "输入内容…"}
            onChange={(e) => updateNodeParam(id, pid, e.target.value)}
          />
        </label>
      );
    }
    if (kind === "select" && options) {
      return (
        <label key={pid} className="param">
          <span className="param-label">{label}</span>
          <select
            value={String(value || options[0]?.value)}
            onChange={(e) => updateNodeParam(id, pid, e.target.value)}
          >
            {options.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>
      );
    }
    if (kind === "number") {
      return (
        <label key={pid} className="param">
          <span className="param-label">{label}</span>
          <input
            type="number"
            value={Number(value || 0)}
            onChange={(e) => updateNodeParam(id, pid, Number(e.target.value))}
          />
        </label>
      );
    }
    return null;
  };

  // 运镜选择器（视频节点专用，选项来自运镜库）
  const renderCameraMove = () => {
    if (d.specType !== "video" || !cameraMoves.length) return null;
    const current = String(d.params.camera_move || "");
    const grouped = cameraMoves.reduce<Record<string, typeof cameraMoves>>((acc, m) => {
      (acc[m.category] ||= []).push(m);
      return acc;
    }, {});
    return (
      <label className="param">
        <span className="param-label">运镜</span>
        <select
          value={current}
          onChange={(e) => updateNodeParam(id, "camera_move", e.target.value)}
        >
          <option value="">不指定</option>
          {Object.entries(grouped).map(([cat, moves]) => (
            <optgroup key={cat} label={cat}>
              {moves.map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
    );
  };

  // ---------- 版本抽屉（抽卡记录） ----------

  const renderVersions = () => {
    if (!spec.versioned || !showVersions) return null;
    return (
      <div className="version-drawer">
        {versions.length === 0 && <div className="version-empty">暂无抽卡记录</div>}
        <div className="version-list">
          {versions.map((v) => {
            const seed = typeof v.meta?.seed === "number" ? v.meta.seed : null;
            return (
              <button
                key={v.id}
                className={`nodrag version-item ${v.id === d.selectedVersionId ? "active" : ""}`}
                title={seed !== null ? `seed: ${seed}${v.prompt ? ` · ${v.prompt}` : ""}` : v.prompt || v.source}
                onClick={() => selectVersion(id, v.id)}
              >
                {v.artifact_id && v.kind === "image" ? (
                  <img src={fileUrl(v.artifact_id)} alt={`v${v.seq}`} />
                ) : (
                  <span className="version-kind">{v.kind?.slice(0, 1) ?? "·"}</span>
                )}
                <span className="version-seq">v{v.seq}</span>
                <span className="version-source">{v.source === "uploaded" ? "传" : "生"}</span>
              </button>
            );
          })}
        </div>
        <div className="version-hint">点底部「生成」按钮可再抽一张新版本</div>
      </div>
    );
  };

  // ---------- @引用 chips ----------

  const renderRefs = () => {
    if (!d.refs?.length) return null;
    return (
      <div className="ref-chips">
        {d.refs.map((r) => (
          <span key={r.nodeId} className="ref-chip nodrag" title={`@${r.nodeId}`}>
            @{r.label}
            <button onClick={() => removeRef(id, r.nodeId)}>×</button>
          </span>
        ))}
      </div>
    );
  };

  const onUpload = async (file: File | undefined) => {
    if (!file) return;
    try {
      await uploadFile(id, file);
    } catch (e) {
      toast.error(`上传失败：${(e as Error).message}`);
    }
  };

  return (
    <div className={`studio-node category-${spec.category} ${selected ? "selected" : ""} ${d.stale ? "stale" : ""}`}>
      {spec.inputs.map((p) => (
        <Handle
          key={`in-${p.id}`}
          id={p.id}
          type="target"
          position={Position.Left}
          style={{ background: "#8b93a7" }}
        />
      ))}
      {spec.outputs.map((p) => (
        <Handle
          key={`out-${p.id}`}
          id={p.id}
          type="source"
          position={Position.Right}
          style={{ background: p.type === "image" ? "#5b8cff" : p.type === "video" ? "#ff7ab8" : p.type === "audio" ? "#3fd68f" : "#8b93a7" }}
        />
      ))}

      {spec.uploadAccept && (
        <input
          ref={fileInputRef}
          type="file"
          accept={spec.uploadAccept}
          hidden
          onChange={(e) => onUpload(e.target.files?.[0])}
        />
      )}

      <div className="node-header">
        <button
          className="mention-btn"
          title="引用画布节点作为参考"
          onClick={() => setShowMention((v) => !v)}
        >
          @
        </button>
        <span className="node-title" title="双击改名">
          {nodeTitle}
        </span>
        {spec.versioned && versions.length > 0 && (
          <button
            className="version-toggle nodrag"
            onClick={() => setShowVersions((v) => !v)}
          >
            v{versions.find((v) => v.id === d.selectedVersionId)?.seq ?? versions.length} ▾
          </button>
        )}
        <span className="node-status" style={{ color: meta.color }}>
          {(status === "loading_model" || status === "running") && progress > 0
            ? `${meta.label} ${Math.round(progress * 100)}%`
            : meta.label}
        </span>
      </div>

      {renderPreview()}
      {renderVersions()}

      <div className="node-params">
        {spec.params
          .filter((p) => !p.advanced && p.id !== "camera_move")
          .map((p) => renderParam(p.id, p.label, p.kind, p.options, p.placeholder))}
        {renderCameraMove()}
        {renderRefs()}
      </div>

      {/* 音频徽标：读后端能力（local 注册表逐模型登记 / 端点按协议判定）。
          **未知不显示**——写死名单曾把 agnes/方舟等云端模型全标成「无音频」，错在
          拿「不知道」当「没有」（2026-10-14 修正，见 docs/design/35）。 */}
      {isVideoModel && typeof audioCaps[modelId] === "boolean" && (
        <div className="model-badge">
          {audioCaps[modelId] ? "原生生成音频" : "无音频，建议接配音"}
        </div>
      )}

      {(status === "running" || status === "loading_model") && (
        <div className="progress-track">
          <div className="progress-bar" style={{ width: `${Math.round(progress * 100)}%` }} />
        </div>
      )}

      {task?.error && (
        <div
          className="node-error"
          title="双击复制报错信息"
          onDoubleClick={() => {
            void navigator.clipboard.writeText(task.error).then(() => {
              setErrCopied(true);
              setTimeout(() => setErrCopied(false), 1500);
            }).catch(() => {});
          }}
        >
          {errCopied ? "✓ 已复制报错信息" : task.error}
        </div>
      )}

      <div className="node-footer">
        <button
          className="run-btn nodrag"
          disabled={busy}
          onClick={() => runNode(id)}
          // 仅可运行节点消耗积分（保存态节点走同一按钮但不提交任务）
          title={spec.runnerHint ? (runCostTip || undefined) : undefined}
        >
          {busy ? "生成中…" : spec.runnerHint ? "生成" : "保存"}
        </button>
        {task?.id && busy && (
          <button className="cancel-btn nodrag" onClick={() => api.cancelTask(task.id).catch(() => {})}>
            取消
          </button>
        )}
        {d.stale && <span className="stale-badge">参考已变更</span>}
      </div>

      {showMention && (
        <MentionPicker nodeId={id} onClose={() => setShowMention(false)} />
      )}

      {/* 全屏预览灯箱：portal 到 body（React Flow 的 transform 祖先会劫持 fixed 定位） */}
      {previewUrl &&
        createPortal(
          <div className="preview-lightbox" onClick={() => setPreviewUrl(null)}>
            {previewKind === "video" ? (
              <video
                src={previewUrl}
                controls
                autoPlay
                loop
                onClick={(e) => e.stopPropagation()}
              />
            ) : (
              <img src={previewUrl} alt="预览" onClick={(e) => e.stopPropagation()} />
            )}
          </div>,
          document.body,
        )}
    </div>
  );
});
