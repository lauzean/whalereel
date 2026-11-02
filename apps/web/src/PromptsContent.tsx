/** 提示词在线编辑内容区（系统配置·提示词页签，批次 2b §32.4 D-4 决策）。

左列外置提示词清单（按注入阶段分组排序），右侧编辑器全文编辑：
保存 = 快照上一版 → 写盘 → 模块全局热生效（下次 LLM 调用即读新内容）；
恢复 = 回滚到上次保存前的快照。locked 条目（editable=false）只读展示。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type LessonsSuggestion,
  type PromptDetail,
  type PromptMeta,
} from "./api/client";
import { confirmDialog, toast } from "./ui/feedback";

/** 阶段标记中文（与 frontmatter stage 对应，仅展示用） */
const STAGE_LABELS: Record<string, string> = {
  review: "审读",
  audit: "终审",
  optimize: "优化",
  create: "创作",
  decompose: "分镜拆解",
  align: "对齐/补镜",
  assets: "资产",
  costume: "妆造",
  enrich: "画面描述",
  canvas: "画布 Agent",
};

export function PromptsContent() {
  const [items, setItems] = useState<PromptMeta[]>([]);
  const [active, setActive] = useState<string>("");
  const [detail, setDetail] = useState<PromptDetail | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [saving, setSaving] = useState(false);
  /** 自学习改进建议（跨项目系统性弱项 → 提示词改进，应用前确认） */
  const [suggestions, setSuggestions] = useState<LessonsSuggestion[]>([]);
  const [applying, setApplying] = useState("");

  const loadList = (prefer?: string) =>
    api
      .listPrompts()
      .then((r) => {
        setItems(r.prompts);
        setActive((cur) => prefer ?? cur ?? r.prompts[0]?.name ?? "");
      })
      .catch((e) => toast.error(`提示词清单加载失败：${e.message}`));

  const loadSuggestions = useCallback(() => {
    api
      .getLessonsInsights()
      .then((r) => setSuggestions(r.suggestions))
      .catch(() => setSuggestions([])); // 建议拉取失败静默（非核心功能）
  }, []);

  const applySuggestion = async (s: LessonsSuggestion) => {
    const ok = await confirmDialog({
      title: `应用改进建议：${s.title}`,
      message: `将把以下内容追加到 ${s.target}（保存前自动快照，可一键恢复）：\n\n${s.append_text.trim()}`,
      confirmText: "应用",
    });
    if (!ok) return;
    setApplying(s.rule);
    try {
      const r = await api.applyLessonsSuggestion(s.rule);
      toast.success(r.already_applied
        ? "该建议已应用过（幂等跳过）"
        : `已追加到 ${s.target} 并热生效`);
      loadSuggestions();
      // 正在编辑目标提示词时同步编辑器内容
      if (active === s.target) {
        const d = await api.getPrompt(s.target);
        setDetail(d);
        setDraft(d.content);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setApplying("");
    }
  };

  // 选中条目变化 → 拉取正文
  useEffect(() => {
    if (!active) return;
    setDetail(null);
    api
      .getPrompt(active)
      .then((d) => {
        setDetail(d);
        setDraft(d.content);
      })
      .catch((e) => toast.error(`提示词加载失败：${e.message}`));
  }, [active]);

  useEffect(() => {
    void loadList();
    loadSuggestions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dirty = detail !== null && draft !== detail.content;

  const onSave = async () => {
    if (!detail || !dirty) return;
    setSaving(true);
    try {
      await api.savePrompt(detail.name, draft);
      toast.success(`${detail.name} 已保存并热生效`);
      // 重新拉取（has_snapshot 状态变化）
      const d = await api.getPrompt(detail.name);
      setDetail(d);
      setDraft(d.content);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const onRestore = async () => {
    if (!detail?.has_snapshot) return;
    const ok = await confirmDialog({
      title: "恢复上一版",
      message: `将把 ${detail.name} 恢复到本次编辑前的快照版本，继续？`,
    });
    if (!ok) return;
    try {
      await api.restorePrompt(detail.name);
      toast.success(`${detail.name} 已恢复上一版`);
      const d = await api.getPrompt(detail.name);
      setDetail(d);
      setDraft(d.content);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const grouped = useMemo(() => {
    const map = new Map<string, PromptMeta[]>();
    for (const it of items) {
      const key = it.stage || "其他";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(it);
    }
    return [...map.entries()];
  }, [items]);

  const pendingSuggestions = suggestions.filter((s) => !s.applied);
  return (
    <div className="prompts-wrap">
      {/* 自学习改进建议：跨项目系统性弱项 → 提示词改进（应用=追加+快照+热生效） */}
      {pendingSuggestions.length > 0 && (
        <div className="lessons-banner">
          <div className="lessons-banner-title">
            自学习改进建议（多个项目反复出现的问题 → 建议强化提示词本体）
          </div>
          {pendingSuggestions.map((s) => (
            <div key={s.rule} className="lessons-item">
              <div className="lessons-item-head">
                <span className="lessons-item-title">{s.title}</span>
                <span className="lessons-item-evidence">{s.evidence}</span>
                <span className="lessons-item-target">→ {s.target}</span>
                <button
                  className="prompt-btn primary"
                  disabled={!!applying}
                  onClick={() => void applySuggestion(s)}
                >
                  {applying === s.rule ? "应用中…" : "应用到提示词"}
                </button>
              </div>
              {/* **正文必须看得见**：这段文字就是「应用」后会追加到提示词里的内容，
                  看不到就没法判断建议对不对（面板上只给标题与证据时，用户只能盲点） */}
              <div className="lessons-item-body-label">
                应用后会追加到 {s.target} 的正文：
              </div>
              <pre className="lessons-item-body">{s.append_text.trim()}</pre>
            </div>
          ))}
        </div>
      )}
      <div className="prompts-layout">
      <div className="prompts-list">
        {grouped.map(([stage, list]) => (
          <div key={stage} className="prompts-group">
            <div className="prompts-group-title">
              {STAGE_LABELS[stage] ?? stage}
            </div>
            {list.map((it) => (
              <button
                key={it.name}
                className={`prompts-item${it.name === active ? " on" : ""}`}
                onClick={() => setActive(it.name)}
                title={it.description}
              >
                {it.name.replace(/_SYSTEM_PROMPT$/, "")}
              </button>
            ))}
          </div>
        ))}
        {!items.length && <div className="prompts-empty">暂无外置提示词</div>}
      </div>
      <div className="prompts-editor">
        {detail ? (
          <>
            <div className="prompts-editor-head">
              <span className="prompts-name" title={detail.name}>
                {detail.description || detail.name}
              </span>
              <span className="prompts-actions">
                {detail.has_snapshot && (
                  <button className="prompt-btn" onClick={onRestore}>
                    恢复上一版
                  </button>
                )}
                <button
                  className="prompt-btn primary"
                  disabled={!dirty || saving || !detail.editable}
                  onClick={onSave}
                >
                  {saving ? "保存中…" : "保存（热生效）"}
                </button>
              </span>
            </div>
            {!detail.editable && (
              <div className="prompts-locked-hint">
                该提示词为锁定状态，仅可查看。
              </div>
            )}
            <textarea
              className="prompts-textarea"
              value={draft}
              disabled={!detail.editable}
              onChange={(e) => setDraft(e.target.value)}
              spellCheck={false}
            />
            <div className="prompts-footnote">
              保存即热生效（下次解析/生成调用读取新内容，无需重启）。
              改提示词后请先跑 _verify_pipeline.py 全量验证。
            </div>
          </>
        ) : (
          <div className="prompts-empty">选择左侧提示词查看与编辑</div>
        )}
      </div>
      </div>
    </div>
  );
}
