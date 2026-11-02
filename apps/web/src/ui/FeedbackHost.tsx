/** 反馈组件宿主：Toast 堆叠渲染 + Confirm/Prompt 弹窗（App 根部挂载一次）。 */

import { useEffect, useState } from "react";
import { useFeedback, type ConfirmOptions, type PromptOptions } from "./feedback";

/** 弹窗默认按钮文案 */
const DEFAULT_CONFIRM_TEXT = "确定";
const DEFAULT_CANCEL_TEXT = "取消";

/** Toast 类型图标（纯文本符号，无需图标库） */
const TOAST_ICONS: Record<string, string> = {
  error: "✕",
  success: "✓",
  warning: "!",
  info: "i",
};

/** 确认弹窗视图：焦点默认落在确认按钮（Enter 即确认），危险操作红色按钮 */
function ConfirmView({ opts }: { opts: ConfirmOptions }) {
  const closeDialog = useFeedback((s) => s.closeDialog);
  return (
    <>
      <div className="fb-dialog-title">{opts.title}</div>
      <div className="fb-dialog-message">{opts.message}</div>
      <div className="fb-dialog-actions">
        <button className="fb-btn ghost" onClick={() => closeDialog(false)}>
          {opts.cancelText || DEFAULT_CANCEL_TEXT}
        </button>
        <button
          className={`fb-btn primary${opts.danger ? " danger" : ""}`}
          autoFocus
          onClick={() => closeDialog(true)}
        >
          {opts.confirmText || DEFAULT_CONFIRM_TEXT}
        </button>
      </div>
    </>
  );
}

/** 输入弹窗视图：受控输入，Enter 提交（空输入拦截），初始光标定位尾部便于修改 */
function PromptView({ opts }: { opts: PromptOptions }) {
  const closeDialog = useFeedback((s) => s.closeDialog);
  const [value, setValue] = useState(opts.initial ?? "");
  const isEmpty = value.trim() === "";

  // 初始焦点 + 光标定位尾部（与原生 prompt 行为一致，避免误改写整段文本）
  const focusTail = (e: React.FocusEvent<HTMLInputElement>) => {
    const len = e.currentTarget.value.length;
    e.currentTarget.setSelectionRange(len, len);
  };

  /** 提交：空输入不允许（与原调用点的非空校验语义一致） */
  const submit = () => {
    if (isEmpty) return;
    closeDialog(value);
  };

  return (
    <>
      <div className="fb-dialog-title">{opts.title}</div>
      {opts.message && <div className="fb-dialog-message">{opts.message}</div>}
      <input
        className="fb-dialog-input"
        autoFocus
        value={value}
        placeholder={opts.placeholder}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        onFocus={focusTail}
      />
      <div className="fb-dialog-actions">
        <button className="fb-btn ghost" onClick={() => closeDialog(null)}>
          {DEFAULT_CANCEL_TEXT}
        </button>
        <button className="fb-btn primary" disabled={isEmpty} onClick={submit}>
          {opts.confirmText || DEFAULT_CONFIRM_TEXT}
        </button>
      </div>
    </>
  );
}

export function FeedbackHost() {
  const toasts = useFeedback((s) => s.toasts);
  const dismissToast = useFeedback((s) => s.dismissToast);
  const dialog = useFeedback((s) => s.dialog);
  const closeDialog = useFeedback((s) => s.closeDialog);

  // ESC 关闭弹窗（按取消兑现）；遮罩点击同语义。监听随弹窗挂载/卸载
  useEffect(() => {
    if (!dialog) return;
    const cancelValue = dialog.kind === "prompt" ? null : false;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeDialog(cancelValue);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dialog, closeDialog]);

  const cancelDialog = () => {
    if (!dialog) return;
    closeDialog(dialog.kind === "prompt" ? null : false);
  };

  return (
    <>
      {toasts.length > 0 && (
        <div className="fb-toast-wrap">
          {toasts.map((t) => (
            <div
              key={t.id}
              className={`fb-toast ${t.kind}`}
              onClick={() => dismissToast(t.id)}
              title="点击关闭"
            >
              <span className="fb-toast-icon">{TOAST_ICONS[t.kind]}</span>
              <span className="fb-toast-msg">{t.message}</span>
              <button className="fb-toast-close" onClick={(e) => { e.stopPropagation(); dismissToast(t.id); }}>
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      {dialog && (
        <div className="fb-mask" onClick={cancelDialog} onContextMenu={(e) => { e.preventDefault(); cancelDialog(); }}>
          <div className="fb-dialog" onClick={(e) => e.stopPropagation()}>
            {dialog.kind === "confirm" && dialog.confirm && <ConfirmView opts={dialog.confirm} />}
            {dialog.kind === "prompt" && dialog.prompt && <PromptView opts={dialog.prompt} />}
          </div>
        </div>
      )}
    </>
  );
}
