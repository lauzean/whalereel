/** 轻量反馈组件状态与命令式 API：Toast 提示 + Confirm/Prompt 弹窗。

替换全部原生浏览器弹窗（alert/confirm/prompt），风格与全局暗色主题统一。
命令式入口（toast.* / confirmDialog / promptDialog）在 React 组件外
（zustand store、工具模块）同样可用；渲染由 App 根部挂载的 FeedbackHost 承担。
 */

import { create } from "zustand";

/** Toast 类型：error 错误 / success 成功 / info 提示 / warning 警告 */
export type ToastKind = "error" | "success" | "info" | "warning";

/** Toast 条目 */
export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

/** 确认弹窗选项 */
export interface ConfirmOptions {
  title: string;
  message: string;
  /** 确认按钮文案（缺省「确定」） */
  confirmText?: string;
  /** 取消按钮文案（缺省「取消」；语义为「不执行确认动作」时可定制，如「不保留」） */
  cancelText?: string;
  /** 危险操作：确认按钮红色（删除/卸载等不可逆操作） */
  danger?: boolean;
}

/** 输入弹窗选项 */
export interface PromptOptions {
  title: string;
  /** 标题下的说明文字（可选） */
  message?: string;
  /** 输入框初始值 */
  initial?: string;
  placeholder?: string;
  /** 确认按钮文案（缺省「确定」） */
  confirmText?: string;
}

/** 弹窗内部状态：kind 区分确认/输入，resolve 由用户操作兑现（单弹窗同时只存在一个） */
interface DialogState {
  kind: "confirm" | "prompt";
  confirm: ConfirmOptions | null;
  prompt: PromptOptions | null;
  /** confirm 兑现布尔、prompt 兑现输入值（取消为 null） */
  resolve: (value: boolean | string | null) => void;
}

// 屏幕上最多同时可见的 Toast 数（超出挤掉最早的，防错误风暴刷屏）
const TOAST_MAX_VISIBLE = 5;

// Toast 自动消失时长（毫秒，按类型区分：错误停留更久便于阅读）
const TOAST_AUTO_DISMISS_MS: Record<ToastKind, number> = {
  error: 8000,
  warning: 6000,
  info: 4000,
  success: 3000,
};

interface FeedbackState {
  toasts: ToastItem[];
  dialog: DialogState | null;
  pushToast: (kind: ToastKind, message: string) => void;
  dismissToast: (id: number) => void;
  openConfirm: (opts: ConfirmOptions) => Promise<boolean>;
  openPrompt: (opts: PromptOptions) => Promise<string | null>;
  /** 关闭当前弹窗并兑现结果（confirm 传布尔，prompt 传输入值或 null） */
  closeDialog: (result: boolean | string | null) => void;
}

// 自增 ID：Toast 条目唯一键（含自动移除定时器定位）
let nextId = 1;

export const useFeedback = create<FeedbackState>((set, get) => ({
  toasts: [],
  dialog: null,

  pushToast: (kind, message) => {
    const id = nextId++;
    // 超出可见上限时丢弃最早的（slice 负数取尾部）
    const rest = get().toasts.slice(-(TOAST_MAX_VISIBLE - 1));
    set({ toasts: [...rest, { id, kind, message }] });
    window.setTimeout(() => get().dismissToast(id), TOAST_AUTO_DISMISS_MS[kind]);
  },

  dismissToast: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  openConfirm: (opts) => {
    // 已有弹窗未关闭时先按取消兑现，避免悬挂的 Promise
    get().dialog?.resolve(false);
    return new Promise<boolean>((resolve) => {
      set({
        dialog: {
          kind: "confirm", confirm: opts, prompt: null,
          resolve: resolve as (value: boolean | string | null) => void,
        },
      });
    });
  },

  openPrompt: (opts) => {
    get().dialog?.resolve(null);
    return new Promise<string | null>((resolve) => {
      set({
        dialog: {
          kind: "prompt", confirm: null, prompt: opts,
          resolve: resolve as (value: boolean | string | null) => void,
        },
      });
    });
  },

  closeDialog: (result) => {
    const dialog = get().dialog;
    if (!dialog) return;
    set({ dialog: null });
    dialog.resolve(result);
  },
}));

/** Toast 命令式入口（组件内外均可调用） */
export const toast = {
  error: (message: string) => useFeedback.getState().pushToast("error", message),
  success: (message: string) => useFeedback.getState().pushToast("success", message),
  info: (message: string) => useFeedback.getState().pushToast("info", message),
  warning: (message: string) => useFeedback.getState().pushToast("warning", message),
};

/** 确认弹窗命令式入口：await confirmDialog({...}) → 是否确认 */
export const confirmDialog = (opts: ConfirmOptions): Promise<boolean> =>
  useFeedback.getState().openConfirm(opts);

/** 输入弹窗命令式入口：await promptDialog({...}) → 输入值（取消返回 null） */
export const promptDialog = (opts: PromptOptions): Promise<string | null> =>
  useFeedback.getState().openPrompt(opts);
