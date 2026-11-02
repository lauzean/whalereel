/** 右侧工作区：当前集的一集工作台（集头部接力条 + 水平步骤条 + 下方面板）。
 *
 * 布局依据 docs/prototype/drama-ui.html：一集的工作 = 四个阶段顺序推进
 * （① 剧情审核 → ② 本集资产 → ③ 分镜 → ④ 生成与输出）。步骤条显示顺序与进度，
 * 点任一步切到下方面板；**未解锁的步骤不可点**（灰化），完成后自动解锁下一步
 * ——前沿（frontier）由集的状态推导，不由点击推进，因此不存在跳步。
 *
 * 复用而非重写：① 直接嵌 EpisodeAuditPanel；④ 直接嵌 Storyboard（分镜卡样式与
 * 全部操作原样保留）；② 用资产页同款卡片组件（EpisodeAssetsPanel）。
 */

import { useEffect, useRef, useState } from "react";
import type { EpisodeInfo } from "../api/client";
import { outstandingIssues, PRODUCE_STATUS, PRODUCE_STATUS_LABEL } from "../api/client";
import { BILLING_KEYS } from "../billingKeys";
import { useDrama } from "../stores/drama";
import { costLine, useCostTip } from "../stores/billing";
import { confirmDialog } from "../ui/feedback";
import { EpisodeAssetsPanel, useEpisodeAssets, useEpisodeAssetsParsed } from "./EpisodeAssetsPanel";
import { EpisodeAuditPanel } from "./EpisodeAuditPanel";
import { Storyboard } from "./Storyboard";

/** 四个阶段（顺序即数组顺序，序号用于锁定判定与展示） */
const STEPS = [
  { no: 1, label: "剧情审核" },
  { no: 2, label: "本集资产" },
  { no: 3, label: "分镜" },
  { no: 4, label: "生成与输出" },
] as const;

/** 逐集任务的进行中文案（任务条左侧说明） */
const TASK_KIND_LABEL: Record<string, string> = {
  audit: "剧情审核中",
  optimize: "一键修复中",
  build: "分集制作中",
  assets: "资产解析中",
  replan: "重新分集中",
};

export function EpisodeWorkspace({ episode }: { episode: EpisodeInfo }) {
  const loadShots = useDrama((s) => s.loadShots);
  const setActiveEpisode = useDrama((s) => s.setActiveEpisode);
  const epTask = useDrama((s) => s.epTask);
  const stopEpisodeTask = useDrama((s) => s.stopEpisodeTask);
  const buildEpisode = useDrama((s) => s.buildEpisode);
  const shotsMap = useDrama((s) => s.shots);
  // 制作按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const buildCostTip = useCostTip(BILLING_KEYS.episodeBuild);
  /** 当前步骤（受前沿约束：只能在前沿内切换） */
  const [step, setStep] = useState(1);

  // 切换集时同步 activeEpisodeId 并拉分镜（生成链路按此 id 归账）
  useEffect(() => {
    setActiveEpisode(episode.id);
    loadShots(episode.id).catch(() => {});
  }, [episode.id, setActiveEpisode, loadShots]);

  const shots = shotsMap[episode.id] ?? [];
  const status = episode.produce_status || PRODUCE_STATUS.planned;
  const hasShots = (episode.shot_count ?? 0) > 0;
  const busy = epTask?.episodeId === episode.id;
  const building = busy && epTask?.kind === "build";

  // 审核是否已跑过（判据与 EpisodeAuditPanel 的空态判定一致）
  const audit = episode.audit;
  const audited = !!audit
    && ((audit.issues?.length ?? 0) > 0 || !!audit.verdict || !!audit.patched);
  // 只数**未修**的问题：修完不复审，清单保留已修条目（带 patch.applied），
  // 直接按 severity 数会把已修的也算成待修——实测 2 条 critical 已修完，
  // 步骤角标仍写「2 条严重」、审核步骤永远标不成完成
  const critCount = outstandingIssues(audit, "critical").length;
  const issueCount = outstandingIssues(audit).length;
  // 审核放行（无未修的严重问题）才算这一步做完
  const auditPassed = audited && critCount === 0;

  const assets = useEpisodeAssets(episode.seq);
  const assetsParsed = useEpisodeAssetsParsed(episode, assets.count);

  // 解锁前沿：已出分镜 → 四步全开；本集资产已解析 → 可到第 3 步；
  // 已审核 → 可到第 2 步；未审核/待修复 → 只能到第 1 步
  const frontier = hasShots ? 4 : assetsParsed ? 3 : audited ? 2 : 1;

  // 步骤默认停在该集的前沿（数据加载期间前沿变化也跟着走，即「完成后自动解锁」）；
  // 用户一旦手动点过步骤，就只在前沿收紧时回退，不再自动跳步
  // （原型 setFrontier：curStage 超出前沿才回退）
  const prevEpRef = useRef<string | null>(null);
  const pickedRef = useRef(false);
  useEffect(() => {
    const switched = prevEpRef.current !== episode.id;
    prevEpRef.current = episode.id;
    if (switched) pickedRef.current = false;
    setStep((cur) => (switched || !pickedRef.current ? frontier : Math.min(cur, frontier)));
  }, [episode.id, frontier]);

  const ffCount = shots.filter((s) => s.first_frame_artifact_id).length;
  const videoCount = shots.filter((s) => s.video_artifact_id).length;
  const promptCount = shots.filter((s) => (s.video_prompt || "").trim()).length;
  const composed = !!episode.compose_artifact_id || episode.status === "composed";

  // 步骤角标（不必逐个点开就知道进行到哪）。原「进度接力条」的 7 个胶囊已并入
  // 此处：剧本字数在标题行、审核/资产/分镜本就是步骤 1-3、首帧/视频/合成是
  // 步骤 4 的内容——两排进度指示器说的是同一件事，且上面那排不可点。
  const stepMeta = [
    audited
      ? (critCount > 0 ? `${critCount} 条严重待修`
          : audit?.patched ? "已修复" : "已放行")
      : "尚未审核",
    assetsParsed ? (assets.count > 0 ? `新增 ${assets.count} 个` : "已解析") : "未解析",
    hasShots ? `${episode.shot_count} 镜` : "未生成",
    hasShots
      ? (composed
          ? `已成片 · ${videoCount} 视频`
          : `首帧 ${ffCount} · 视频 ${videoCount}`)
      : "—",
  ];
  const stepDone = [auditPassed, assetsParsed, hasShots, hasShots && composed];

  // 生成本集分镜（与审核面板里的同一动作：拆解立即入库，画面描述失败不丢分镜）
  const runBuild = async () => {
    // 审核门禁（与「门禁只拦 critical、其余仅提示」同口径）：有**未修的严重
    // 问题**时先拦一道，但要**说清后果并给逃生口**——审核可能误判，用户有权
    // 坚持制作。此前这里只看 building，审核结论完全没接到门上：步骤条写着
    // 「N 条严重待修」，按钮却照点不误（实测第 1 步 2 条 critical 仍做出了 32 镜）。
    const blocking = critCount > 0
      ? `\n\n⚠ 本集还有 ${critCount} 条**严重**剧情问题未修（第 1 步「剧情审核」里可查看与一键修复）。`
        + "带着这些问题制作，分镜很可能建在断裂的因果上，后续重做更贵。"
      : "";
    const ok = await confirmDialog({
      title: critCount > 0 ? "仍有严重剧情问题，仍要制作？" : "生成本集分镜",
      message: "将按本集正文拆解分镜并生成导演级画面描述。"
        + "拆解完成后立即入库，即使后续画面描述失败也不会丢分镜（可重跑补齐）。"
        + blocking
        + (costLine(BILLING_KEYS.episodeBuild)
            ? `\n\n${costLine(BILLING_KEYS.episodeBuild)}` : ""),
      confirmText: critCount > 0 ? "仍要制作" : "开始制作",
      danger: critCount > 0,
    });
    if (!ok) return;
    await buildEpisode(episode.id);
  };

  const runStopTask = async () => {
    const ok = await confirmDialog({
      title: "停止当前任务",
      message: `确定停止第 ${episode.seq} 集的${
        TASK_KIND_LABEL[epTask?.kind ?? ""] ?? "当前任务"}吗？已落库的产物（分镜等）不会丢失。`,
      confirmText: "确认停止",
      danger: true,
    });
    if (!ok) return;
    await stopEpisodeTask();
  };

  return (
    <div className="ep-workspace">
      {/* 集头部：集号 / 集名 / 字数·镜数 / 状态徽标 + 进度接力条 */}
      <div className="ep-ws-head">
        <div className="ep-ws-head-l1">
          <span className="ep-ws-seq">第 {episode.seq} 集</span>
          <span className="ep-ws-title" title={episode.synopsis || episode.title}>
            {episode.title || "未命名"}
          </span>
          <span className="ep-ws-meta">
            {episode.body_chars ? `${episode.body_chars} 字` : ""}
            {hasShots ? ` · ${episode.shot_count} 镜` : ""}
          </span>
          <span className={`ep-ws-status st-${status}`}>
            {busy ? (epTask?.label || "任务进行中…")
                   : (PRODUCE_STATUS_LABEL[status] || status)}
          </span>
        </div>
      </div>

      {/* no-scroll：第 4 步的故事板自带内部滚动（卡片区横向 + 卡片内纵向），
          外层再滚动会出现双重滚动条并把卡片撑到内容全高 */}
      <div className={`ep-ws-body${step === 4 && hasShots ? " no-scroll" : ""}`}>
        {/* 任务条：本集有在途任务时显示（进度 + 停止入口，替代旧「AI 过程」页） */}
        {busy && (
          <div className="ep-taskbar">
            <span className="ep-taskbar-spin" aria-label="loading" />
            <span>
              {TASK_KIND_LABEL[epTask?.kind ?? ""] ?? "任务进行中"} · 第 {episode.seq} 集
            </span>
            <span className="ep-taskbar-pct">
              {Math.round((epTask?.progress ?? 0) * 100)}%
            </span>
            <button className="ghost-btn" onClick={() => void runStopTask()}>
              停止任务
            </button>
          </div>
        )}

        {/* 水平步骤条：带连接线显顺序；未解锁的步骤灰化不可点 */}
        <div className="ep-stepper">
          {STEPS.map((s) => {
            const idx = s.no - 1;
            const locked = s.no > frontier;
            return (
              <button
                key={s.no}
                className={`ep-step${step === s.no ? " on" : ""}`
                  + `${locked ? " locked" : ""}${stepDone[idx] ? " done" : ""}`}
                disabled={locked}
                title={locked ? "请先完成前一步" : ""}
                onClick={() => {
                  pickedRef.current = true;
                  setStep(s.no);
                }}
              >
                <span className="ep-step-dot">{stepDone[idx] ? "✓" : s.no}</span>
                <span className="ep-step-lb">{s.label}</span>
                <span className={`ep-step-ms${s.no === 1 && critCount > 0 ? " warn" : ""}`}>
                  {stepMeta[idx]}
                </span>
              </button>
            );
          })}
        </div>

        {/* ① 剧情审核：复用现有审核面板（要点/钩子作为同一份上下文展示在其上） */}
        {step === 1 && (
          <div className="ep-pane">
            {(episode.beats?.length ?? 0) > 0 && (
              <div className="ep-ws-beats">
                <span className="ep-ws-beats-label">剧情要点</span>
                <span>{(episode.beats ?? []).join("；")}</span>
                {(episode.hooks?.length ?? 0) > 0 && (
                  <span className="ep-ws-hooks">
                    钩子：{(episode.hooks ?? []).map((h) => h.text).filter(Boolean).join("；")}
                  </span>
                )}
              </div>
            )}
            <EpisodeAuditPanel episode={episode} />
          </div>
        )}

        {/* ② 本集资产 */}
        {step === 2 && <EpisodeAssetsPanel episode={episode} />}

        {/* ③ 分镜 */}
        {step === 3 && (
          <div className="ep-pane">
            <div className="ep-pane-lead">
              拆解本集正文为分镜并生成导演级画面描述。拆解完成立即入库，
              即使画面描述失败也不会丢分镜（可重跑补齐）。
            </div>
            <div className="ep-pane-acts">
              <button
                className="gen-btn primary"
                disabled={building}
                onClick={() => void runBuild()}
                title={buildCostTip || undefined}
              >
                {building ? "制作中…" : hasShots ? "重新生成本集分镜" : "生成本集分镜"}
              </button>
              {hasShots && (
                <span className="ep-pane-note">
                  {episode.shot_count} 个分镜已入库
                  {shots.length > 0 ? ` · 画面描述 ${promptCount}/${shots.length} 已生成` : ""}
                </span>
              )}
            </div>
          </div>
        )}

        {/* ④ 生成与输出：直接嵌现有故事板（分镜卡样式与全部操作原样） */}
        {step === 4 && (
          hasShots ? (
            <section className="ep-ws-storyboard">
              <Storyboard episode={episode} />
            </section>
          ) : (
            <div className="ep-pane">
              <div className="ep-pane-lead">
                本集还没有分镜：先回到第 3 步「生成本集分镜」，再来做首帧 / 视频生成、
                预检审片与合成输出。
              </div>
            </div>
          )
        )}
      </div>
    </div>
  );
}
