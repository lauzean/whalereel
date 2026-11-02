/** 图片放大预览灯箱（资产页/分镜卡共用）：点击遮罩或 Esc 关闭。

**传的是 artifact_id，不是文件地址**——内部自己拼 `/api/artifacts/<id>/file`。
（参数原名 `url` 容易让人误传 fileUrl() 的结果，实测因此出现地址套两层、图片裂；
改名 artifactId 让契约自解释。）样式复用全局 .preview-lightbox。
 */

import { useEffect } from "react";
import { createPortal } from "react-dom";
import { fileUrl } from "../api/client";

export function ImageLightbox({ artifactId, onClose }: { artifactId: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return createPortal(
    <div className="preview-lightbox" onClick={onClose}>
      <img src={fileUrl(artifactId)} alt="预览" onClick={(e) => e.stopPropagation()} />
    </div>,
    document.body,
  );
}
