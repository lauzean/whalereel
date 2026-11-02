/** 项目资产库面板（M2 资产三方联动，设计文档 4.1 第 6 条 / 4.14）。

列出项目全部产物（含已删除节点的产物），支持分类过滤与搜索；
点击产物插入画布（删除节点后可从资产库找回）。
 */

import { useEffect, useState } from "react";
import { fileUrl, type ProjectAssetInfo } from "../api/client";
import { useCanvas } from "../stores/canvas";
import { toast } from "../ui/feedback";

const KIND_TABS: { key: string; label: string }[] = [
  { key: "", label: "全部" },
  { key: "image", label: "图片" },
  { key: "video", label: "视频" },
  { key: "audio", label: "音频" },
];

function AssetThumb({ asset }: { asset: ProjectAssetInfo }) {
  const url = fileUrl(asset.artifact_id);
  if (asset.kind === "image") {
    return <img className="asset-thumb" src={url} alt={asset.name} loading="lazy" />;
  }
  if (asset.kind === "video") {
    return <video className="asset-thumb" src={url} muted preload="metadata" />;
  }
  return <span className="asset-thumb asset-audio">{asset.kind === "audio" ? "音" : "·"}</span>;
}

export function AssetsPanel() {
  const projectId = useCanvas((s) => s.projectId);
  const assets = useCanvas((s) => s.assets);
  const loadAssets = useCanvas((s) => s.loadAssets);
  const insertAssetNode = useCanvas((s) => s.insertAssetNode);
  const [kind, setKind] = useState("");
  const [keyword, setKeyword] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  // 分类/关键词变化时重新拉取（列表已在 store 全量缓存，前端过滤即可）
  const filtered = assets.filter((a) => {
    if (kind && a.kind !== kind) return false;
    if (keyword) {
      const kw = keyword.toLowerCase();
      if (!a.name.toLowerCase().includes(kw) && !a.prompt.toLowerCase().includes(kw)) {
        return false;
      }
    }
    return true;
  });

  useEffect(() => {
    if (projectId) loadAssets();
  }, [projectId, loadAssets]);

  const insert = async (asset: ProjectAssetInfo) => {
    if (busyId) return;
    setBusyId(asset.artifact_id);
    try {
      await insertAssetNode(asset);
    } catch (e) {
      toast.error(`插入失败：${(e as Error).message}`);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="assets-panel">
      <div className="assets-toolbar">
        <div className="assets-tabs">
          {KIND_TABS.map((t) => (
            <button
              key={t.key}
              className={kind === t.key ? "on" : ""}
              onClick={() => setKind(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <input
          className="assets-search"
          placeholder="搜索提示词/名称…"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
      </div>
      {filtered.length === 0 ? (
        <div className="assets-empty">暂无产物，生成或上传后自动入库</div>
      ) : (
        <div className="asset-list">
          {filtered.map((a) => (
            <button
              key={a.artifact_id}
              className="asset-item"
              title={`${a.name}${a.prompt ? `\n${a.prompt}` : ""}\n点击插入画布`}
              disabled={busyId === a.artifact_id}
              onClick={() => insert(a)}
            >
              <AssetThumb asset={a} />
              <span className="asset-name">{a.name}</span>
              <span className="asset-kind">{a.kind === "image" ? "图" : a.kind === "video" ? "视" : "音"}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
