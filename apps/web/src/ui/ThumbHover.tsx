/** 缩略图悬浮预览：鼠标停在缩略图上时，在它**旁边**浮出一张较大的预览图。
 *
 * 为什么用 fixed 定位 + portal 挂到 body：缩略图所在的容器几乎都是滚动区
 * （故事板左侧资产条是 `overflow-y: auto`、分镜卡自己也会滚），绝对定位的浮层会被
 * 父级裁掉——和右键菜单当初踩的是同一个坑。fixed 不受祖先裁剪，再用 JS 把它摆到
 * 缩略图旁边（贴屏幕右缘时自动翻到左侧，纵向夹在视口内）。
 *
 * 触摸端没有 hover，本组件自然不生效；手机端缩略图是点按（定位分镜）语义，无需替代。
 */

import { useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { fileUrl } from "../api/client";

/** 预览图最大尺寸（CSS 里同值限制，见 styles.css 的 .thumb-hover），用于摆位与翻边 */
const PREVIEW_MAX_W = 260;
const PREVIEW_MAX_H = 300;
/** 缩略图与预览图之间的间距、与屏幕边缘的安全边距 */
const GAP = 10;
const MARGIN = 8;
/** 预览图下方名称条的高度（用于纵向夹取，避免贴底时被切） */
const CAPTION_H = 30;

/** 当前悬浮的缩略图：画哪张、叫什么、摆在屏幕哪里 */
interface HoverState {
  artifactId: string;
  name: string;
  x: number;
  y: number;
}

export function useThumbHover() {
  const [hover, setHover] = useState<HoverState | null>(null);

  /** 挂到缩略图元素上（传 artifact_id；没有图时返回的处理器不做事） */
  const hoverProps = (artifactId?: string | null, name?: string) => ({
    onMouseEnter: (e: MouseEvent<HTMLElement>) => {
      if (!artifactId) return;
      const r = e.currentTarget.getBoundingClientRect();
      // 默认放右侧；右边放不下就翻到左侧
      const x = r.right + GAP + PREVIEW_MAX_W + MARGIN <= window.innerWidth
        ? r.right + GAP
        : Math.max(MARGIN, r.left - GAP - PREVIEW_MAX_W);
      // 纵向与缩略图居中对齐，并夹在视口内（算上名称条，否则贴底时会被切掉一截）
      const y = Math.min(
        Math.max(MARGIN, r.top + r.height / 2 - PREVIEW_MAX_H / 2),
        Math.max(MARGIN, window.innerHeight - PREVIEW_MAX_H - CAPTION_H - MARGIN),
      );
      setHover({ artifactId, name: name ?? "", x, y });
    },
    onMouseLeave: () => setHover(null),
  });

  /** 浮层本体：与 hoverProps 在同一组件内渲染一次即可 */
  const hoverNode = hover
    ? createPortal(
      // pointer-events: none：浮层不能挡住鼠标（否则会立刻触发 mouseleave 抖动）
      <div className="thumb-hover" style={{ left: hover.x, top: hover.y }}>
        <img src={fileUrl(hover.artifactId)} alt={hover.name} />
        {hover.name && <span className="thumb-hover-name">{hover.name}</span>}
      </div>,
      document.body,
    )
    : null;

  return { hoverProps, hoverNode };
}
