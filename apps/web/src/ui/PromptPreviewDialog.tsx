/** 提示词预览确认框（通用组件）。
 *
 * 用途：**会整段覆盖**提示词的操作，先把结果摆给用户看，确认后才落库。
 * 起因：分镜卡的「重算」此前是点一下直接 `row.video_prompt = vp` 落库——
 * 用户既看不到新内容，也没法拒绝，反馈「像抽盲盒」。同卡片的「AI 优化」
 * 却是「预览 → 可编辑 → 应用才覆盖」，两者安全级别不一致。
 *
 * 交互契约（与 AI 优化弹窗一致）：
 * - 「新的」一版可**直接编辑**（改完再应用）；
 * - 只有点「应用」才回传文本给调用方，取消/关闭零副作用；
 * - 生成中禁用关闭，避免用户误以为已取消；
 * - **点遮罩/空白不关闭**——新描述没应用就关掉等于白花一次调用。
 *
 * 本组件不直接写库：调用方在自己的保存链路里应用（分镜走 applyShotPrompt）。
 */

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

export function PromptPreviewDialog({
  title,
  current,
  next,
  hint,
  onApply,
  onClose,
}: {
  /** 弹窗标题（如「重算画面描述 · 镜头 4」） */
  title: string;
  /** 当前生效的文本（只读对照，可折叠） */
  current: string;
  /** 新生成的结果（可编辑后再应用） */
  next: string;
  /** 底部一行白话说明（可空） */
  hint?: string;
  /** 用户点「应用」时回传（可能被编辑过的）文本 */
  onApply: (text: string) => void | Promise<void>;
  onClose: () => void;
}) {
  const [text, setText] = useState(next);
  const [applying, setApplying] = useState(false);
  const [showCurrent, setShowCurrent] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !applying) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, applying]);

  const apply = async () => {
    if (!text.trim() || applying) return;
    setApplying(true);
    try {
      await onApply(text.trim());
    } finally {
      setApplying(false);
    }
  };

  // portal 到 body：调用方可能位于带 transform 的卡片内（见 PromptOptimizeDialog 注释）
  return createPortal(
    // 点遮罩/空白不关闭：里面是刚重算出来的新描述，误触即丢（同 AI 优化弹窗）
    <div className="settings-mask">
      <div className="settings-dialog prompt-opt-dialog">
        <div className="settings-head">
          <span className="settings-title">{title}</span>
          <button className="provider-close" disabled={applying} onClick={onClose}>
            关闭
          </button>
        </div>

        <div className="project-settings-body">
          <div className="prompt-opt-block">
            <button
              className="link-btn"
              type="button"
              onClick={() => setShowCurrent((v) => !v)}
            >
              {showCurrent ? "▾" : "▸"} 当前生效的描述（{current.length} 字，点「应用」才会被替换）
            </button>
            {showCurrent && <pre className="prompt-opt-pre">{current || "（空）"}</pre>}
          </div>

          <label className="prompt-opt-label">
            新的描述（可直接编辑后再应用）
            <textarea
              className="prompt-opt-input prompt-opt-result"
              rows={10}
              value={text}
              onChange={(e) => setText(e.target.value)}
              disabled={applying}
            />
          </label>
          <div className="prompt-opt-metrics">{text.length} 字</div>
        </div>

        <div className="prompt-opt-foot">
          {hint && <span className="prompt-opt-hint">{hint}</span>}
          <button className="fb-btn ghost" disabled={applying} onClick={onClose}>
            取消
          </button>
          <button
            className="fb-btn primary"
            disabled={!text.trim() || applying}
            onClick={() => void apply()}
          >
            {applying ? "应用中…" : "应用"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
