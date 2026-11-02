/** AI 优化提示词弹窗（20 号批次 3，通用组件）。
 *
 * 交互契约（用户口径）：
 * - 点「AI 优化」→ 弹窗：原提示词只读 + 上下文清单可展开核对 + 修改需求输入框；
 * - 点「优化」→ loading（期间禁用按钮，防连点产生多次计费调用）；
 * - 结果落在**可编辑**文本框里，用户可继续手改；
 * - 只有点「应用」才把文本交回调用方（`onApply`），取消/关闭零副作用；
 * - **点遮罩/空白不关闭**——结果没应用就关掉等于白花一次调用（用户报障）。
 *
 * 本组件不直接写库：调用方在自己的保存链路里应用（分镜走 updateShot、
 * 资产走 updateCharacter/Scene/Prop），避免另开一条写路径。
 */

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import { BILLING_KEYS } from "../billingKeys";
import { useCostTip } from "../stores/billing";
import { toast } from "./feedback";

export interface PromptOptimizeTarget {
  /** character | costume | scene | prop | shot（与后端 target 一一对应） */
  target: string;
  /** 对象 id */
  id: string;
  /** 弹窗标题里显示的对象名（如角色名/场景名/「第 3 镜」） */
  label: string;
  /** 当前提示词原文（只读展示，也是服务端校验的对照基准） */
  currentPrompt: string;
}

export function PromptOptimizeDialog({
  projectId,
  target,
  onApply,
  onClose,
}: {
  projectId: string;
  target: PromptOptimizeTarget;
  /** 用户点「应用」时回传优化后的提示词（调用方负责写回与提示） */
  onApply: (text: string) => void | Promise<void>;
  onClose: () => void;
}) {
  // 优化按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const optimizeCostTip = useCostTip(BILLING_KEYS.promptsOptimize);
  const [instruction, setInstruction] = useState("");
  const [running, setRunning] = useState(false);
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState("");
  const [notes, setNotes] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [digest, setDigest] = useState<string[]>([]);
  const [metrics, setMetrics] = useState<string>("");
  const [showContext, setShowContext] = useState(false);
  const [showCurrent, setShowCurrent] = useState(false);

  // ESC 关闭（与其它弹窗一致；生成中不关闭，避免用户误以为已取消）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !running) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, running]);

  const run = async () => {
    if (running) return;
    if (!instruction.trim()) {
      toast.error("请先写明修改需求（例如「把光线改成冷调月光」）");
      return;
    }
    setRunning(true);
    setWarnings([]);
    try {
      const r = await api.optimizePrompt(projectId, {
        target: target.target,
        id: target.id,
        instruction: instruction.trim(),
        // 编辑中的文本一并上送：对象尚无持久提示词时（妆造建议草稿）服务端用它兜底
        current_prompt: target.currentPrompt,
      });
      setResult(r.prompt);
      setNotes(r.notes);
      setWarnings(r.warnings ?? []);
      setDigest(r.context_digest ?? []);
      const m = r.metrics;
      setMetrics(
        `${m.length} 字（建议 ≤${m.limit}${m.over > 0 ? `，超出 ${m.over}` : ""}）`
        + `｜约束占比 ${(m.constraint_ratio * 100).toFixed(0)}%`
        + `｜冗余 ${(m.redundancy_rate * 100).toFixed(1)}%`
        + (m.suspect_truncation ? "｜疑似断句" : ""),
      );
    } catch (e) {
      toast.error(`AI 优化失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setRunning(false);
    }
  };

  const apply = async () => {
    if (!result.trim() || applying) return;
    setApplying(true);
    try {
      await onApply(result.trim());
    } finally {
      setApplying(false);
    }
  };

  // 必须 portal 到 body：本组件从资产卡片内部唤起，而 `.asset-card:hover`
  // 带 `transform: translateY(-1px)`——鼠标停在卡片上时卡片成为 `position:fixed`
  // 的包含块，弹窗会被囚禁在卡片里（实测用户反馈「弹在卡片内」）。同 ImageLightbox。
  return createPortal(
    // **点遮罩/空白不关闭**：这个弹窗里躺着刚花模型调用换来的优化结果，
    // 一次误触就全丢了（用户实测报障）。关闭只走右上「关闭」按钮或 Esc（都会
    // 明确表明意图），生成中则一律不关。
    <div className="settings-mask">
      <div className="settings-dialog prompt-opt-dialog">
        <div className="settings-head">
          <span className="settings-title">AI 优化提示词 · {target.label}</span>
          <button className="provider-close" disabled={running} onClick={onClose}>
            关闭
          </button>
        </div>

        <div className="project-settings-body">
          <div className="prompt-opt-block">
            <button
              className="link-btn"
              onClick={() => setShowCurrent((v) => !v)}
              type="button"
            >
              {showCurrent ? "▾" : "▸"} 当前提示词（{target.currentPrompt.length} 字）
            </button>
            {showCurrent && <pre className="prompt-opt-pre">{target.currentPrompt}</pre>}
          </div>

          <label className="prompt-opt-label">
            修改需求
            <textarea
              className="prompt-opt-input"
              rows={5}
              placeholder="用一句话说清你想要什么，例如：把光线改成冷调月光；让人物再靠近镜头一点；去掉多余的背景细节"
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              disabled={running}
            />
          </label>

          <div className="prompt-opt-actions">
            <button className="fb-btn primary" disabled={running} onClick={() => void run()}
                    title={optimizeCostTip || undefined}>
              {running ? "优化中…" : result ? "重新优化" : "优化"}
            </button>
            {digest.length > 0 && (
              <button
                className="link-btn"
                type="button"
                onClick={() => setShowContext((v) => !v)}
              >
                {showContext ? "▾" : "▸"} 本次参考的上下文（{digest.length} 项）
              </button>
            )}
          </div>

          {digest.length > 0 && showContext && (
            <ul className="prompt-opt-digest">
              {digest.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          )}

          {result && (
            <>
              <label className="prompt-opt-label">
                优化结果（可直接编辑后再应用）
                <textarea
                  className="prompt-opt-input prompt-opt-result"
                  rows={10}
                  value={result}
                  onChange={(e) => setResult(e.target.value)}
                />
              </label>
              {metrics && <div className="prompt-opt-metrics">{metrics}</div>}
              {notes && <div className="prompt-opt-notes">AI 说明：{notes}</div>}
              {warnings.length > 0 && (
                <ul className="prompt-opt-warn">
                  {warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>

        <div className="prompt-opt-foot">
          {/* 按钮统一走系统弹窗标准件 `.fb-btn`（次要=ghost / 主要=primary）：
              此前用 `.parse-btn`（大号蓝底 10px 26px / 14px），与系统弹窗
              的 7px 18px / 13px 不一致，用户反馈「突然变蓝变大了」。 */}
          <button className="fb-btn ghost" disabled={running || applying} onClick={onClose}>
            取消
          </button>
          <button
            className="fb-btn primary"
            disabled={!result.trim() || running || applying}
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
