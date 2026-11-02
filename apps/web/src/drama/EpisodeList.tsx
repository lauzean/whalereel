/** 左侧分集列表：每集状态徽标 + 点选切换当前集 + 重新分集入口（18 号 §56）。
 *
 * 为什么做成常驻左栏：逐集流水线里「第几集做到哪一步」是用户最常问的问题。
 * 旧界面把它藏在「分集视频」页签的卡片流里，进故事板还要全屏接管且不改 URL，
 * 刷新即回列表——逐集推进时不可用。
 */

import { useEffect, useRef } from "react";
import { PRODUCE_STATUS, PRODUCE_STATUS_LABEL, type EpisodeInfo } from "../api/client";
import { BILLING_KEYS } from "../billingKeys";
import { useDrama } from "../stores/drama";
import { useCostTipWith } from "../stores/billing";

/** 状态徽标样式类（needs_fix/failed 用醒目色，其余中性） */
function statusClass(status: string): string {
  if (status === PRODUCE_STATUS.needsFix) return "needs-fix";
  if (status === PRODUCE_STATUS.failed) return "failed";
  if (status === PRODUCE_STATUS.built) return "built";
  if (status === PRODUCE_STATUS.building) return "busy";
  if (status === PRODUCE_STATUS.audited) return "audited";
  return "planned";
}

/** 单集行：序号 + 集名 + 徽标 + 字数/分镜数 */
function EpisodeRow({
  ep, active, busy, onPick,
}: {
  ep: EpisodeInfo;
  active: boolean;
  /** 该集正在跑任务（左栏显示转圈，避免用户重复点） */
  busy: boolean;
  onPick: () => void;
}) {
  const status = ep.produce_status || PRODUCE_STATUS.planned;
  const critical = (ep.audit?.issues ?? []).filter(
    (i) => i.severity === "critical").length;
  return (
    <button
      className={`ep-row${active ? " on" : ""}`}
      onClick={onPick}
      title={ep.synopsis || ep.title}
    >
      <span className="ep-seq">{ep.seq}</span>
      <span className="ep-main">
        {/* 标题独占整行：状态徽标下移到元信息行——旧版徽标与标题抢宽度，
            集名被截成「雪夜叩…」，10 集列表全靠悬浮提示才认得出是哪一集 */}
        <span className="ep-title">{ep.title || "未命名"}</span>
        <span className="ep-meta">
          <span className="ep-meta-text">
            {/* 元信息按「有才显示」拼接：旧写法缺字数时会渲染成「— · 1 镜」，
                破折号与分隔点看着像缺数据（老项目未重新解析时常见） */}
            {[
              ep.body_chars ? `${ep.body_chars} 字` : "",
              ep.shot_count > 0 ? `${ep.shot_count} 镜` : "",
              status === PRODUCE_STATUS.needsFix && critical > 0
                ? `${critical} 条待修` : "",
            ].filter(Boolean).join(" · ") || "未解析"}
          </span>
          {ep.status === "composed" && (
            <span className="ep-composed" title="已合成">成片</span>
          )}
          <span className={`ep-badge ${statusClass(status)}`}>
            {busy ? "…" : (PRODUCE_STATUS_LABEL[status] || status)}
          </span>
        </span>
      </span>
    </button>
  );
}

export function EpisodeList({
  activeSeq, onPick, onReplan,
}: {
  activeSeq: number | null;
  onPick: (ep: EpisodeInfo) => void;
  /** 重新分集入口：作为列表最后一项渲染（手机档即横滑条的最后一张卡） */
  onReplan?: () => void;
}) {
  const episodes = useDrama((s) => s.episodes);
  const loadEpisodes = useDrama((s) => s.loadEpisodes);
  const epTask = useDrama((s) => s.epTask);
  // 「按剧情重新分集」的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const replanCostTip = useCostTipWith(
    "按剧情走向重新切分分集边界（会清空现有分镜、重建）",
    BILLING_KEYS.episodeReplan);
  /** 集列表滚动容器（手机档是横滑条，桌面档是纵向列表） */
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    loadEpisodes().catch(() => {});
  }, [loadEpisodes]);

  // 手机档：切集后把当前集滚进可视区。横滑条一屏只放得下 2 张卡，切集（比如从剧本页的
  // 分集计划点进来）后当前集常常在屏幕外，看不出「现在在哪一集」。
  // 用 scrollWidth > clientWidth 判定：只有横滑条才滚，桌面纵向列表保持不动。
  useEffect(() => {
    const body = bodyRef.current;
    if (!body || activeSeq == null) return;
    if (body.scrollWidth <= body.clientWidth + 1) return;
    body.querySelector<HTMLElement>(".ep-row.on")
      ?.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
  }, [activeSeq, episodes.length]);

  return (
    <div className="ep-list">
      <div className="ep-list-head">
        <span>分集</span>
        <span className="ep-list-count">{episodes.length} 集</span>
      </div>
      <div className="ep-list-body" ref={bodyRef}>
        {episodes.map((ep) => (
          <EpisodeRow
            key={ep.id}
            ep={ep}
            active={ep.seq === activeSeq}
            busy={epTask?.episodeId === ep.id}
            onPick={() => onPick(ep)}
          />
        ))}
        {episodes.length === 0 && (
          <div className="ep-list-empty">
            还没有分集：请在「剧本」页粘贴剧本并点击「AI 解析」
          </div>
        )}
        {/* 重新分集是全局动作（重切所有集边界、清空分镜），挂在列表末尾而不单独占一行 */}
        {onReplan && episodes.length > 0 && (
          <button
            className="ep-replan"
            onClick={onReplan}
            title={replanCostTip}
          >
            按剧情重新分集
          </button>
        )}
      </div>
    </div>
  );
}
