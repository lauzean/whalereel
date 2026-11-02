/** 剧本页：左栏剧本编辑器（铺满）+ 右栏「剧本解析」（过程 + 分集计划）。
 *
 * 布局依据 docs/prototype/drama-ui.html：解析过程从独立页签并回本页右栏（观测并入
 * 剧本页），分集计划从编辑器下方挪到右栏下半——「看过程」和「看结果（分集）」在同一
 * 列的上下两段，同时「点任一条分集进入该集」把用户直接送进逐集流水线。
 *
 * 职责边界（18 号 §56 起）：本页只负责「把剧本变成分集计划」（解析）；
 * 逐集的审核/修复/资产解析/制作在「分集」页里做。
 */

import { useEffect, useRef, useState } from "react";
import { api, type ParseRequirements } from "../api/client";
import { BILLING_KEYS } from "../billingKeys";
import { navigate } from "../router";
import { useDrama } from "../stores/drama";
import { useCostTip } from "../stores/billing";
import { confirmDialog, promptDialog, toast } from "../ui/feedback";
import { ParseRequirementsDialog } from "./ParseRequirementsDialog";
import { ParseTracePanel, PREPARE_STAGES } from "./ParseTracePanel";

/** 标准格式标记：含任一视为完整剧本，无需扩写（与后端 expander 判定一致） */
const FORMAT_MARK_RE =
  /第[0-9一二三四五六七八九十百千零两]+[集章幕]|^场景[:：]|^人物[:：]|^△/m;

/** 一句话梗概长度上限（与后端 expander 一致） */
const ONE_LINER_MAX = 300;

/** 上传支持的文档扩展名（与后端 SCRIPT_DOC_EXTENSIONS 对齐） */
const DOC_ACCEPT = ".txt,.md,.markdown,.docx,.pdf";

/** 分集来源标签（LLM 按剧情切 vs 原文集头切分） */
const PLAN_SOURCE_LABEL: Record<string, string> = {
  llm: "AI 按剧情切分",
  head: "原文集头",
};

export function ScriptTab() {
  const {
    projectId, scriptText, setScriptText, parseScript, parsing, parseProgress,
    parseStopped, parseFailed, stopParseScript, script, error, expandScript,
    scriptVersions, loadScriptVersions, restoreScriptVersion,
    parseTrace, parseLive,
    episodes, patchEpisode,
  } = useDrama();
  // 花钱按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const parseCostTip = useCostTip(BILLING_KEYS.scriptParse);
  const expandCostTip = useCostTip(BILLING_KEYS.scriptExpand);
  const [expanding, setExpanding] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState("");
  /** 解析需求弹窗（非空 = 已过「保留旧版本」一问，待收集需求后提交） */
  const [parseOpen, setParseOpen] = useState<{ keepVersion: boolean } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const text = scriptText.trim();
  const oneLiner =
    text.length > 0 && text.length <= ONE_LINER_MAX && !FORMAT_MARK_RE.test(scriptText);
  const epCount = episodes.length;

  // 解析收尾提示：parsing 由 true → false 且无错误时展示成功信息
  // （失败/取消原因由 store.error 直接展示；停止不报「已完成」——审计 13 号教训）
  const prevParsing = useRef(false);
  useEffect(() => {
    if (prevParsing.current && !parsing && !error
        && !parseStopped && !parseFailed) {
      setNotice(`解析完成：已切出 ${epCount} 集，可点右侧分集计划里的任一条进入该集`
        + "做剧情审核与制作（资产在「分集」页逐集解析）");
    }
    prevParsing.current = parsing;
  }, [parsing, error, parseStopped, parseFailed, epCount]);

  // 进入页面拉取历史版本列表
  useEffect(() => {
    if (projectId) loadScriptVersions().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // AI 解析：已有结果时先问是否保留旧版本（只快照剧本原文），再收集解析需求
  //（21 号批次 5），最后提交——需求传 null = 沿用存档/默认值，行为与改造前一致
  const openParseDialog = async () => {
    setNotice("");
    let keepVersion = false;
    if (script?.analyzed) {
      keepVersion = await confirmDialog({
        title: "保留旧版本？",
        message: "检测到已有解析结果。选择保留后，当前剧本原文将存为一版（可随时回填"
          + "编辑器重新解析）；不保留则直接覆盖。",
        confirmText: "保留旧版本并解析",
        cancelText: "不保留",
      });
    }
    setParseOpen({ keepVersion });
  };

  const doParse = async (keepVersion: boolean, reqs: ParseRequirements | null) => {
    const res = await parseScript(keepVersion, reqs ?? undefined);
    setParseOpen(null);
    if (res) setNotice("解析任务已提交：审读 → 分集规划，过程见右侧「剧本解析」面板");
  };

  const runStop = async () => {
    const ok = await confirmDialog({
      title: "停止解析",
      message: "AI 正在解析，中途停止将丢弃本轮未完成的部分，恢复到之前的解析结果。确定停止吗？",
      confirmText: "确认停止",
      danger: true,
    });
    if (!ok) return;
    await stopParseScript();
    setNotice("已停止解析，数据恢复到之前的结果");
  };

  // AI 扩写：一句话梗概 → 标准剧本，结果填回输入框供确认编辑后再解析
  const runExpand = async () => {
    if (expanding || parsing || !oneLiner) return;
    setExpanding(true);
    setNotice("");
    try {
      const res = await expandScript(text);
      if (res.mode === "passthrough") {
        setNotice("内容已符合标准剧本格式，无需扩写");
      } else {
        setScriptText(res.text);
        setNotice("已通过 AI 扩写为标准剧本，请确认内容后点击「重新解析」");
      }
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "扩写失败，请稍后重试");
    } finally {
      setExpanding(false);
    }
  };

  // 文档上传提取：txt/md/docx/pdf → 纯文本填回编辑器，确认后走解析流程
  const uploadDoc = async (file: File | undefined) => {
    if (!file || uploading || parsing || !projectId) return;
    setUploading(true);
    setNotice("");
    try {
      const res = await api.extractScriptDoc(projectId, file);
      setScriptText(res.text);
      setNotice(`已从「${res.filename}」提取 ${res.chars} 字，请确认内容后点击「重新解析」`);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "文档提取失败，请稍后重试");
    } finally {
      setUploading(false);
      // 清空 input.value 使同一文件可重复选择上传
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  // 恢复历史版本（版本 = 剧本原文 → 回填编辑器，确认后重新解析）
  const runRestore = async (versionId: string, seq: number) => {
    const ok = await confirmDialog({
      title: "恢复剧本原文",
      message: `确定把「版本 ${seq}」的剧本原文填回编辑器？当前编辑器内容将被替换。`,
      confirmText: "恢复",
    });
    if (!ok) return;
    try {
      const res = await restoreScriptVersion(versionId);
      setScriptText(res.text);
      setNotice(`已恢复版本 ${seq} 的剧本原文（${res.chars} 字），确认后点击「重新解析」`);
      toast.success(`已恢复版本 ${seq}`);
    } catch (e) {
      toast.error(`恢复失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 改集名（分集计划可编辑：AI 切的集名不合意时直接改，不重跑 LLM）
  const renameEpisode = async (episodeId: string, seq: number, current: string) => {
    const name = await promptDialog({
      title: `第 ${seq} 集 · 修改集名`,
      message: "集名只影响展示与检索，不改动剧本内容。",
      initial: current,
      confirmText: "保存",
    });
    if (name === null || name === current) return;
    await patchEpisode(episodeId, { title: name });
    toast.success("集名已更新");
  };

  /** 跳到某集的工作台（分集计划行的主点击行为） */
  const gotoEpisode = (seq: number) => {
    if (!projectId) return;
    navigate(`#/project/${projectId}/episodes/${seq}`);
  };

  return (
    <div className="scr">
      {/* ---------------- 左栏：剧本编辑器（铺满） ---------------- */}
      <div className="scr-l">
        <div className="stage-bar">
          剧本原文
          <span className="stage-bar-sub">粘贴或上传，AI 只解析成品剧本</span>
        </div>
        <textarea
          className="scr-ed"
          spellCheck={false}
          placeholder={"粘贴完整剧本（推荐含「第X集」集头），也可上传 txt/md/docx/pdf 文档\n\n"
            + "若剧本没有集头且较长，AI 会按剧情走向、爆点与钩子自动切分分集"}
          value={scriptText}
          onChange={(e) => setScriptText(e.target.value)}
        />
        <div className="scr-acts">
          <input
            ref={fileInputRef}
            type="file"
            accept={DOC_ACCEPT}
            hidden
            onChange={(e) => void uploadDoc(e.target.files?.[0])}
          />
          <button
            className="parse-btn upload"
            disabled={uploading || parsing}
            onClick={() => fileInputRef.current?.click()}
          >
            {uploading ? "提取中…" : "上传文档"}
          </button>
          {oneLiner && (
            <button className="parse-btn expand" disabled={expanding || parsing}
                    onClick={runExpand}
                    title={expandCostTip || undefined}>
              {expanding ? "扩写中…" : "AI 扩写剧本"}
            </button>
          )}
          <button
            className={`parse-btn ${parsing ? "stop" : ""}`}
            disabled={(!parsing && !text) || uploading}
            onClick={() => (parsing ? void runStop() : void openParseDialog())}
            // 计费说明只在「要提交解析」时给（停止态不展示）
            title={!parsing ? (parseCostTip || undefined) : undefined}
          >
            {parsing
              ? (<><span className="parse-stop-icon">■</span>停止解析</>)
              : (script?.analyzed ? "重新解析" : "AI 解析")}
          </button>
          <span className="parse-hint">
            {parsing
              ? parseProgress.label || "解析进行中…（过程见右侧「剧本解析」）"
              : oneLiner
                ? "检测到一句话梗概：可先 AI 扩写为完整剧本，或直接解析"
                : "AI 解析 = 审读 + 分集规划；资产在「分集」里逐集解析"}
          </span>
        </div>
        {notice && <div className="script-notice">{notice}</div>}
        {/* 解析需求收集（21 号批次 5）：确认/跳过都走同一提交链路 */}
        {parseOpen && projectId && (
          <ParseRequirementsDialog
            projectId={projectId}
            onConfirm={(reqs) => doParse(parseOpen.keepVersion, reqs)}
            onClose={() => setParseOpen(null)}
          />
        )}
        {error && !parsing && <div className="script-notice error">{error}</div>}

        {scriptVersions.length > 0 && (
          <details className="script-versions">
            <summary>历史版本（{scriptVersions.length}）</summary>
            <ul>
              {scriptVersions.map((v) => (
                <li key={v.id}>
                  <span className="ver-seq">V{v.seq}</span>
                  <span className="ver-chars">{v.text_chars} 字</span>
                  <span className="ver-note">{v.note || "—"}</span>
                  <button className="ghost-btn"
                          onClick={() => void runRestore(v.id, v.seq)}>
                    恢复
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>

      {/* ---------------- 右栏：剧本解析（过程 + 分集计划） ---------------- */}
      <div className="scr-r">
        <div className="stage-bar">
          剧本解析
          <span className="stage-bar-sub">
            {parsing ? "解析中" : script?.analyzed ? "已完成" : "未解析"}
          </span>
        </div>
        <div className="scr-proc">
          {/* 解析结果摘要（解析产出的事实，过程面板之上） */}
          {script?.analyzed && (
            <div className="scr-facts">
              <div className="scr-fact">
                剧名《{script.title || "未命名"}》· 共 {epCount} 集 ·
                角色 {(script.characters ?? []).length} · 场景 {(script.scenes ?? []).length}
              </div>
              {script.logline && (
                <div className="scr-fact">简介：{script.logline}</div>
              )}
            </div>
          )}
          <ParseTracePanel
            trace={parseTrace}
            live={parseLive}
            running={parsing}
            progressPct={Math.round((parseProgress.progress || 0) * 100)}
            stopped={parseStopped}
            failed={parseFailed}
            stages={PREPARE_STAGES}
            title="剧本解析"
            runningText="解析中"
          />
        </div>

        {/* 分集计划：整块挪到右栏下半，点任一条进入该集；集名仍可点改 */}
        <div className="planbox">
          <div className="planbox-h">
            分集计划
            <span className="planbox-sub">
              {epCount > 0 ? `${epCount} 集 · 点任一条进入该集` : "解析后自动切分"}
            </span>
          </div>
          <div className="planbox-b">
            {episodes.map((ep) => (
              <div
                key={ep.id}
                className="prow"
                role="button"
                tabIndex={0}
                title="进入该集工作台"
                onClick={() => gotoEpisode(ep.seq)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    gotoEpisode(ep.seq);
                  }
                }}
              >
                <div className="prow-head">
                  <span className="prow-seq">第 {ep.seq} 集</span>
                  {/* 集名可点改：阻止冒泡，避免连带进入该集 */}
                  <button
                    className="prow-title"
                    title="点击修改集名"
                    onClick={(e) => {
                      e.stopPropagation();
                      void renameEpisode(ep.id, ep.seq, ep.title);
                    }}
                  >
                    {ep.title || "未命名"}
                  </button>
                  <span className="prow-meta">
                    {ep.body_chars ? `${ep.body_chars} 字` : ""}
                    {ep.plan_source
                      ? ` · ${PLAN_SOURCE_LABEL[ep.plan_source] || ep.plan_source}` : ""}
                  </span>
                  {/* 进入入口显式化：集名点击是「改集名」，进集走这里或整行空白处 */}
                  <span className="prow-go">进入 ▸</span>
                </div>
                {ep.synopsis && <div className="prow-line">{ep.synopsis}</div>}
                {(ep.beats?.length ?? 0) > 0 && (
                  <div className="prow-line">要点：{(ep.beats ?? []).join("；")}</div>
                )}
                {(ep.hooks?.length ?? 0) > 0 && (
                  <div className="prow-line prow-hooks">
                    钩子：{(ep.hooks ?? []).map((h) => h.text).filter(Boolean).join("；")}
                  </div>
                )}
              </div>
            ))}
            {epCount === 0 && (
              <div className="planbox-empty">
                还没有分集：粘贴剧本后点「AI 解析」，AI 会审读并切出分集计划
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
