/** 逐集剧情审核面板：问题清单（勾选指哪修哪）+ 一键修复 + 放行入口（18 号 §52/§53）。
 *
 * 与旧整部审核面板的区别：
 * - 审核带**上一集/下一集上下文**（上一集要点+结尾状态+尾锚 / 本集全文 / 下一集要点+头锚）；
 * - 修复**修完不复审**：补丁命中情况由代码逐条对账后回写清单，面板按条标出 ✓/✗；
 * - 面板挂在本集上（不是全局），所以「待修复 → 已放行 → 已出分镜」的推进一目了然。
 */

import { useEffect, useMemo, useState } from "react";
import type { EpisodeInfo } from "../api/client";
import { isOutstandingIssue, outstandingIssues, PRODUCE_STATUS } from "../api/client";
import { BILLING_KEYS } from "../billingKeys";
import { useDrama } from "../stores/drama";
import { costLine, useCostTip } from "../stores/billing";
import { confirmDialog, toast } from "../ui/feedback";

/** 严重度中文标签 */
const SEVERITY_LABEL: Record<string, string> = {
  critical: "严重",
  major: "较大",
  minor: "轻微",
};

export function EpisodeAuditPanel({ episode }: { episode: EpisodeInfo }) {
  const auditEpisode = useDrama((s) => s.auditEpisode);
  const optimizeEpisode = useDrama((s) => s.optimizeEpisode);
  const buildEpisode = useDrama((s) => s.buildEpisode);
  const epTask = useDrama((s) => s.epTask);
  // 花钱按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const auditCostTip = useCostTip(BILLING_KEYS.episodeAudit);
  const optimizeCostTip = useCostTip(BILLING_KEYS.episodeOptimize);
  const buildCostTip = useCostTip(BILLING_KEYS.episodeBuild);
  const [picked, setPicked] = useState<Set<number>>(new Set());

  const audit = episode.audit;
  const issues = useMemo(() => audit?.issues ?? [], [audit]);
  const status = episode.produce_status || PRODUCE_STATUS.planned;
  const busy = epTask?.episodeId === episode.id;
  const auditing = busy && epTask?.kind === "audit";
  const fixing = busy && epTask?.kind === "optimize";
  const building = busy && epTask?.kind === "build";

  // 只数**未修**的严重问题：修完不复审，清单保留已修条目（带 patch.applied），
  // 直接按 severity 数会把已修的也算成待修（实测：修完仍显示「2 条严重」）
  const critCount = outstandingIssues(audit, "critical").length;
  const outstanding = outstandingIssues(audit).length;
  // 严重度排序：critical 置顶（只拦 critical，用户第一眼要看的就是"哪几条挡着制作"）
  const sorted = useMemo(
    () => [...issues].sort(
      (a, b) => (a.severity === "critical" ? 0 : 1) - (b.severity === "critical" ? 0 : 1)),
    [issues]);

  // 指哪修哪：勾选要修的问题，critical 默认勾选；清单变化时重置。
  // **已修项（patch.applied）不进默认勾选、也不可再勾**——修完不复审，重复
  // 提交同一批补丁只会再改一遍正文（长度闸反复收放），没有任何收益。
  useEffect(() => {
    setPicked(new Set(
      issues.map((it, idx) =>
        (it.severity === "critical" && isOutstandingIssue(it)) ? idx : -1)
        .filter((idx) => idx >= 0)));
  }, [issues]);

  const toggle = (idx: number) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  };

  const runAudit = async () => {
    const res = await auditEpisode(episode.id);
    if (res) toast.success(`第 ${episode.seq} 集剧情审核已提交`);
    else toast.error("审核任务提交失败，请查看提示");
  };

  const runFix = async () => {
    const chosen = issues.filter((it, idx) => picked.has(idx) && isOutstandingIssue(it));
    if (!chosen.length) {
      toast.error("请先勾选要修复的问题（严重项已默认勾选）");
      return;
    }
    const ok = await confirmDialog({
      title: "一键修复本集",
      message: `将定点修复勾选的 ${chosen.length} 个问题（每条只动定位点及衔接句，`
        + "输出长度受闸约束）。修复完成后不再复审，直接写回本集正文与剧本原文，"
        + "可继续生成分镜。"
        + (costLine(BILLING_KEYS.episodeOptimize)
            ? `\n\n${costLine(BILLING_KEYS.episodeOptimize)}` : ""),
      confirmText: "开始修复",
    });
    if (!ok) return;
    const res = await optimizeEpisode(episode.id, chosen);
    if (res) toast.success("修复任务已提交，命中情况完成后在清单中标出");
    else toast.error("修复任务提交失败，请查看提示");
  };

  const runBuild = async () => {
    const ok = await confirmDialog({
      title: "生成本集分镜",
      message: "将按本集正文拆解分镜并生成导演级画面描述。"
        + "拆解完成后立即入库，即使后续画面描述失败也不会丢分镜（可重跑补齐）。"
        + (costLine(BILLING_KEYS.episodeBuild)
            ? `\n\n${costLine(BILLING_KEYS.episodeBuild)}` : ""),
      confirmText: "开始制作",
    });
    if (!ok) return;
    const res = await buildEpisode(episode.id);
    if (res) toast.success(`第 ${episode.seq} 集制作任务已提交`);
    else toast.error("制作任务提交失败，请查看提示");
  };

  // 未审核：给一个明确起点（逐集流程的第一步）
  if (!audit || (!issues.length && !audit.verdict && !audit.patched && !audit.craft_note)) {
    return (
      <div className="ep-audit">
        <div className="ep-audit-empty">
          <p className="ep-audit-empty-title">本集尚未做剧情审核</p>
          <p className="ep-audit-empty-hint">
            审核会连同上一集与下一集的剧情要点一起分析，重点核对跨集衔接
            （人物位置/时间/持物/钩子是否对得上），只拦会让拍摄翻车的硬伤。
          </p>
          <button className="gen-btn primary" disabled={auditing}
                  onClick={() => void runAudit()}
                  title={auditCostTip || undefined}>
            {auditing ? "审核中…" : "开始剧情审核"}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className={`ep-audit${audit.passed ? " advisory" : ""}`}>
      <div className="ep-audit-head">
        {/* 徽标按**未修**项说话：audit.passed 是审核当时的结论，修复后不会更新
            （修完不复审）——直接用它会出现「已修完却还写审核未通过」 */}
        <span className={`ep-audit-badge${audit.passed || critCount === 0 ? " pass" : ""}`}>
          {critCount > 0
            ? `审核未通过 · ${critCount} 条严重待修`
            : audit.patched
              ? (outstanding > 0
                  ? `已修复 · 余 ${outstanding} 条建议`
                  : "已修复 · 问题已清")
              : audit.passed
                ? (issues.length ? `审核通过 · ${issues.length} 条建议` : "审核通过 · 无问题")
                : "审核通过"}
        </span>
        {audit.verdict && <span className="ep-audit-verdict">{audit.verdict}</span>}
        {audit.patched && <span className="ep-audit-tag">已修复过</span>}
        {audit.craft_note && (
          <span className="ep-audit-craft"
                title="钩子与冲突强度的经营提醒——给编剧的追看性参考，不参与判定与修复">
            经营提醒：{audit.craft_note}
          </span>
        )}
        {audit.cached && <span className="ep-audit-tag" title="同输入命中缓存，零调用">缓存</span>}
        <button className="ghost-btn" disabled={auditing} onClick={() => void runAudit()}
                title={auditCostTip || undefined}>
          {auditing ? "审核中…" : "重新审核"}
        </button>
      </div>

      {audit.exit_state && (
        <div className="ep-audit-exit">
          <span className="ep-audit-exit-label">本集出口状态</span>
          <span>{audit.exit_state}</span>
        </div>
      )}

      {issues.length > 0 && (
        <>
          <div className="ep-audit-hint">
            {critCount > 0
              ? "严重问题会阻断制作：勾选后一键修复（修完不再复审），或忽略问题继续。"
              : audit.patched
                ? "严重问题已修复，可直接生成分镜；剩余建议项可按需勾选修复。"
                : "这些是建议，不阻断制作。可勾选后一键修复，或直接生成分镜。"}
          </div>
          <ol className="ep-audit-issues">
            {sorted.map((it, i) => {
              const idx = issues.indexOf(it);
              const patch = it.patch;
              return (
                <li key={i} className={`ep-audit-issue sev-${it.severity}`}>
                  <div className="ep-audit-issue-head">
                    {/* 已修项复选框置灰：修完不复审，再提交一次同一批补丁只会把
                        同一段正文再改一遍，没有收益（此前可重复勾选 → 重复修复） */}
                    <label className="ep-audit-pick"
                           title={patch?.applied
                             ? "该问题已修复，无需重复处理"
                             : "勾选后只修勾选的问题"}>
                      <input type="checkbox" checked={picked.has(idx)}
                             disabled={patch?.applied === true}
                             onChange={() => toggle(idx)} />
                    </label>
                    <span className={`ep-sev sev-${it.severity}`}>
                      {SEVERITY_LABEL[it.severity] || it.severity}
                    </span>
                    <span className="ep-audit-loc">{it.location || "未定位"}</span>
                    <span className="ep-audit-type">{it.type}</span>
                    {patch && (
                      <span className={`ep-audit-patch ${patch.applied ? "ok" : "miss"}`}
                            title={patch.reason || ""}>
                        {patch.applied ? "✓ 已改" : "✗ 未命中"}
                      </span>
                    )}
                  </div>
                  <div className="ep-audit-desc">{it.description}</div>
                  {it.suggestion && (
                    <div className="ep-audit-suggest">建议：{it.suggestion}</div>
                  )}
                  {it.quote && (
                    <div className="ep-audit-quote" title="修复定位锚（逐字原句）">
                      “{it.quote}”
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </>
      )}

      <div className="ep-audit-actions">
        <button className="gen-btn" disabled={fixing || picked.size === 0}
                onClick={() => void runFix()}
                title={optimizeCostTip || undefined}>
          {fixing ? "修复中…" : `一键修复（${picked.size}）`}
        </button>
        <button className="gen-btn primary" disabled={building}
                onClick={() => void runBuild()}
                title={buildCostTip || undefined}>
          {building
            ? "制作中…"
            : (status === PRODUCE_STATUS.built || episode.shot_count > 0
                ? "重新生成本集分镜" : "生成本集分镜")}
        </button>
        {critCount > 0 && (
          <span className="ep-audit-escape">
            不修也能生成分镜（产物质量可能受影响，问题会留痕）
          </span>
        )}
      </div>
    </div>
  );
}
