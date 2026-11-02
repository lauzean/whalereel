/** AI 过程观测窗：阶段步骤条 + 思考条目（折叠/自动滚动）+ 流式思考块。
 *
 * 从 ScriptTab 抽出（18 号 §56）：解析（审读/分集规划/资产分析）与逐集任务
 * （审核/修复/制作）各有各的阶段，故阶段定义由调用方传入——面板本身不认识
 * 任何具体阶段名，只按「默认 → 执行中 → 完成（带时间）」渲染状态机。
 *
 * 与解析共用同一份展示状态（store.parseTrace / parseLive / parseProgress）：
 * 同时只跑一个任务，混在一起不会乱，反而把「审核 → 修复 → 制作」串成可读过程。
 */

import { useEffect, useRef, useState } from "react";
import type { TaskTrace } from "../api/client";

/** 一个阶段的三种标签（状态机靠这三个字面量匹配后端写的阶段事件） */
export interface StageDef {
  label: string;
  startLabel: string;
  doneLabel: string;
}

/** 解析流水线阶段（与后端 llm_parse 的 stage 事件一致） */
export const PREPARE_STAGES: StageDef[] = [
  { label: "剧本审读", startLabel: "剧本审读中", doneLabel: "剧本审读完成" },
  { label: "分集规划", startLabel: "分集规划中", doneLabel: "分集规划完成" },
  { label: "资产分析", startLabel: "资产分析中", doneLabel: "资产分析完成" },
];

/** 逐集审核阶段 */
export const AUDIT_STAGES: StageDef[] = [
  { label: "剧情审核", startLabel: "剧情审核中", doneLabel: "剧情审核完成" },
];

/** 逐集制作阶段（拆解 → 画面描述；体检在拆解之后由轨迹体现） */
export const BUILD_STAGES: StageDef[] = [
  { label: "分集制作", startLabel: "分集制作中", doneLabel: "分集制作完成" },
];

/** 逐集修复阶段 */
export const OPTIMIZE_STAGES: StageDef[] = [
  { label: "逐集修复", startLabel: "逐集修复中", doneLabel: "逐集修复完成" },
];

/** 逐集资产解析阶段（ep_assets_{集id} 任务的 stage 事件） */
export const ASSETS_STAGES: StageDef[] = [
  { label: "本集资产解析", startLabel: "本集资产解析中", doneLabel: "本集资产解析完成" },
];

/** 重新分集阶段 */
export const REPLAN_STAGES: StageDef[] = [
  { label: "重新分集", startLabel: "重新分集中", doneLabel: "重新分集完成" },
];

/** 时间戳 → 展示（HH:MM:SS） */
function fmtTime(at: number) {
  const d = new Date(at * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

type StageState = "pending" | "running" | "done";

/** 从阶段事件序列推导各阶段状态（完成 > 执行中 > 默认） */
function deriveStageStates(stages: { label: string; at?: number }[],
                          defs: StageDef[]) {
  const labels = new Set(stages.map((s) => s.label));
  return defs.map((def) => {
    if (labels.has(def.doneLabel)) {
      const at = stages.find((s) => s.label === def.doneLabel)?.at ?? null;
      return { ...def, state: "done" as StageState, time: at };
    }
    if (labels.has(def.startLabel)) {
      return { ...def, state: "running" as StageState, time: null };
    }
    return { ...def, state: "pending" as StageState, time: null };
  });
}

/** 思考条目：标题行常显 + 内容可折叠（折叠用 max-height 过渡，内容不脱离文档流） */
function ThoughtItem({ t, open, onToggle }: {
  t: { title: string; content: string; at: number };
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div className={`trace-thought-item ${open ? "open" : ""}`}>
      <button className="trace-thought-head" onClick={onToggle}>
        <span className="trace-fold-chevron">{open ? "▾" : "▸"}</span>
        <span className="trace-thought-time">{fmtTime(t.at)}</span>
        <span className="trace-thought-title">{t.title}</span>
      </button>
      <div className="trace-thought-content">{t.content}</div>
    </div>
  );
}

export function ParseTracePanel({
  trace, live, running, progressPct, stopped, failed,
  stages = PREPARE_STAGES, title = "AI 解析过程", runningText,
}: {
  trace: TaskTrace;
  live: { title: string; text: string } | null;
  running: boolean;
  progressPct: number;
  stopped: boolean;
  failed: boolean;
  /** 阶段定义（默认解析流水线；逐集任务传 AUDIT_STAGES / BUILD_STAGES 等） */
  stages?: StageDef[];
  /** 面板标题 */
  title?: string;
  /** 运行中的状态文案前缀 */
  runningText?: string;
}) {
  const stageEvents = trace.stages ?? [];
  const thoughts = trace.thoughts ?? []; // 按时间升序
  const gates = trace.gates ?? [];
  const listRef = useRef<HTMLDivElement>(null);
  const liveRef = useRef<HTMLDivElement>(null);
  // 折叠状态：null = 跟随默认（最新一条展开，其余折叠）；点选后固定到该条
  const [openIdx, setOpenIdx] = useState<number | null>(null);

  // 新思考条目/流式增量到达时自动滚动到底部（最新内容始终可见）
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [thoughts.length, live?.text, gates.length]);
  useEffect(() => {
    const el = liveRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [live?.text]);

  const rows = deriveStageStates(stageEvents, stages);

  return (
    <details className="parse-trace-panel" open={(running || stopped) || undefined}>
      <summary className="trace-head">
        {running ? (
          <span className="trace-spinner" aria-label="loading" />
        ) : (
          <span className="trace-done-badge">✓</span>
        )}
        <span className="trace-head-title">{title}</span>
        <span
          className={`trace-head-status ${failed ? "failed" : running || stopped ? "" : "done"}`}
        >
          {running
            ? `${runningText ?? "解析中"} ${progressPct}%${live ? ` · ${live.title}` : ""}`
            : failed
              ? "任务失败（错误原因见上方提示）"
              : stopped
                ? "已停止"
                : "已完成"}
        </span>
      </summary>
      <div className="trace-body">
        <div className="trace-steps">
          <ol className="trace-steps-list">
            {rows.map((row) => (
              <li key={row.label} className={`stage-${row.state}`}>
                <span className="trace-step-dot">{row.state === "done" ? "✓" : ""}</span>
                <span className="trace-step-label">
                  {row.state === "done"
                    ? `${row.label}完成`
                    : row.state === "running"
                      ? `${row.label}中`
                      : row.label}
                </span>
                {row.state === "done" && row.time != null && (
                  <span className="trace-step-time">{fmtTime(row.time)}</span>
                )}
              </li>
            ))}
          </ol>
        </div>
        <div className="trace-main">
          <div className="trace-thought-list" ref={listRef}>
            {/* 确定性门禁留痕单独成条（与 LLM 思考区分：这些是代码算出来的） */}
            {gates.map((g, i) => (
              <div className="trace-thought-item gate" key={`g${i}`}>
                <div className="trace-thought-head static">
                  <span className="trace-thought-time">{fmtTime(g.at)}</span>
                  <span className="trace-thought-title">门禁 · {g.gate}</span>
                </div>
                <div className="trace-thought-content">{g.content}</div>
              </div>
            ))}
            {thoughts.map((t, i) => {
              // 默认最新一条展开（流式输出可见），阶段完成/新条目到达后自动折叠
              const isOpen = openIdx === null ? i === thoughts.length - 1 : openIdx === i;
              return (
                <ThoughtItem
                  key={i}
                  t={t}
                  open={isOpen}
                  onToggle={() => setOpenIdx(isOpen ? null : i)}
                />
              );
            })}
            {thoughts.length === 0 && gates.length === 0 && (
              <div className="trace-thought-empty">AI 正在思考，过程将实时流式显示在这里…</div>
            )}
          </div>
          {live && (
            <div className="trace-live" ref={liveRef}>
              <div className="trace-live-title">
                <span className="trace-live-cursor">▍</span>
                {live.title}
              </div>
              <div className="trace-live-text">{live.text || "…"}{running && <span className="trace-live-cursor">▍</span>}</div>
            </div>
          )}
        </div>
      </div>
    </details>
  );
}
