/** 重摇原因选择框（25 号 2.4；2026-10-10 改**多选**——用户要求）。
 *
 * 用途：所有**重出已有产出**的入口（单镜重出视频 / 单镜重新生成首帧 / 批量
 * 生成里的重出部分 / 审片重拍）都必须先过这一框——原因进镜头日志，是后续
 * 优化的唯一靶子。
 *
 * 交互契约（多选）：
 * - 可勾多个原因（如「人物不一致」+「构图」常常同时不满）；至少选一个才能提交；
 * - 选了「其他」时备注必填（否则这条记录无从分析，等于没选）；
 * - 备注对其它原因可选（一句话补充，进 evidence）；
 * - 提交时原因按逗号连接（"identity,pose"）传给调用方，后端逐项校验存储；
 * - 取消零副作用（不发任何请求）。
 */

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { REROLL_REASON_OTHER, ROLL_REASONS } from "../drama/rollReasons";

export function RollReasonDialog({
  title,
  subtitle,
  confirmText = "提交",
  busy = false,
  onSubmit,
  onClose,
}: {
  /** 弹窗标题（如「重出镜 4 的视频」） */
  title: string;
  /** 一行说明：这次要重出什么、为什么要选原因 */
  subtitle?: string;
  /** 主按钮文案（如「提交重出」） */
  confirmText?: string;
  /** 提交中：禁用关闭与按钮 */
  busy?: boolean;
  /** 点主按钮回传（已校验：至少一个原因、其他必带备注；reason 为逗号连接串） */
  onSubmit: (reason: string, note: string) => void | Promise<void>;
  onClose: () => void;
}) {
  /** 已勾选的原因码（保持勾选先后顺序 → 提交串稳定） */
  const [reasons, setReasons] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, submitting, busy]);

  const locked = submitting || busy;
  const needNote = reasons.includes(REROLL_REASON_OTHER);
  const canSubmit = reasons.length > 0 && (!needNote || !!note.trim()) && !locked;

  /** 勾选/取消勾选一个原因（多选） */
  const toggle = (key: string) => {
    setReasons((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  };

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await onSubmit(reasons.join(","), note.trim());
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div className="settings-mask" onClick={() => !locked && onClose()}>
      <div className="settings-dialog roll-reason-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="settings-head">
          <span className="settings-title">{title}</span>
          <button className="provider-close" disabled={locked} onClick={onClose}>
            取消
          </button>
        </div>
        <div className="project-settings-body">
          {subtitle && <div className="roll-reason-sub">{subtitle}</div>}
          <div className="roll-reason-list" role="group" aria-label="重摇原因（可多选）">
            {ROLL_REASONS.map((r) => (
              <button
                key={r.key}
                type="button"
                role="checkbox"
                aria-checked={reasons.includes(r.key)}
                className={`roll-reason-item${reasons.includes(r.key) ? " on" : ""}`}
                disabled={locked}
                onClick={() => toggle(r.key)}
              >
                <span className="roll-reason-name">{r.label}</span>
                <span className="roll-reason-hint">{r.hint}</span>
              </button>
            ))}
          </div>
          <label className="prompt-opt-label">
            备注{needNote ? "（选「其他」时必填）" : "（可选，一句话补充）"}
            <input
              className="roll-reason-note"
              value={note}
              disabled={locked}
              placeholder="例：第 3 秒场景里多出一个人"
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
        </div>
        <div className="prompt-opt-foot">
          <span className="prompt-opt-hint">
            可多选；原因会记入镜头日志：重摇的靶子在哪，优化才有方向
          </span>
          <button className="fb-btn ghost" disabled={locked} onClick={onClose}>
            取消
          </button>
          <button className="fb-btn primary" disabled={!canSubmit} onClick={() => void submit()}>
            {submitting ? "提交中…" : confirmText}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
