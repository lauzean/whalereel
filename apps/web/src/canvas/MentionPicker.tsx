/** @引用选择器：列出画布节点（含 selected 版本产物），点击插入引用（v0.2 4.6）。
 *  onPick 缺省写入节点 refs；对话输入框等场景通过 onPick 自行接管（M2 4.6/5.2）。 */

import { useMemo, useState } from "react";
import { useCanvas } from "../stores/canvas";
import { NODE_SPECS, previewKindOf } from "./specs";
import { fileUrl } from "../api/client";

export function MentionPicker({
  nodeId,
  onPick,
  onClose,
}: {
  nodeId?: string;
  /** 自定义选中回调（提供时不再写入节点 refs） */
  onPick?: (nodeId: string, label: string) => void;
  onClose: () => void;
}) {
  const nodes = useCanvas((s) => s.nodes);
  const addRef = useCanvas((s) => s.addRef);
  const [query, setQuery] = useState("");

  const candidates = useMemo(() => {
    const q = query.trim().toLowerCase();
    return nodes
      .filter((n) => n.id !== nodeId) // 不引用自己
      .filter((n) => {
        if (!q) return true;
        const spec = NODE_SPECS[n.data.specType];
        const title = String(n.data.params.name || n.data.label || spec?.title || "");
        return title.toLowerCase().includes(q) || n.id.includes(q);
      });
  }, [nodes, nodeId, query]);

  const pick = (nodeId_: string) => {
    const node = nodes.find((n) => n.id === nodeId_);
    if (!node) return;
    const spec = NODE_SPECS[node.data.specType];
    const label = String(node.data.params.name || spec?.title || node.id.slice(0, 8));
    if (onPick) onPick(nodeId_, label);
    else if (nodeId) addRef(nodeId, { nodeId: nodeId_, label });
    onClose();
  };

  return (
    <div className="mention-picker">
      <input
        autoFocus
        placeholder="搜索画布节点…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && onClose()}
      />
      <div className="mention-list">
        {candidates.map((n) => {
          const spec = NODE_SPECS[n.data.specType];
          const label = String(n.data.params.name || spec?.title || n.id.slice(0, 8));
          const kind = previewKindOf(n.data.specType);
          const hasArtifact = !!n.data.artifactId;
          return (
            <button key={n.id} className="mention-item" onClick={() => pick(n.id)}>
              {hasArtifact && n.data.artifactId ? (
                kind === "image" ? (
                  <img src={fileUrl(n.data.artifactId)} alt="" />
                ) : (
                  <span className="mention-kind">{spec?.icon}</span>
                )
              ) : (
                <span className="mention-kind">{spec?.icon}</span>
              )}
              <span className="mention-label">{label}</span>
              {n.data.stale && <span className="mention-stale">已变更</span>}
            </button>
          );
        })}
        {candidates.length === 0 && <div className="mention-empty">无匹配节点</div>}
      </div>
    </div>
  );
}
