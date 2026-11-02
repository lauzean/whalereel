/** AI 解析前置需求弹窗（21 号批次 5）。
 *
 * 为什么加这一环：官方短剧 skill 的 planner 阶段有明确的「澄清清单」——解析前澄清
 * 会显著影响结果的关键参数。此前一切靠 LLM 自己猜，下游（分镜节奏、爽感机制、
 * 技能包选择）只能吃猜出来的基调，这是「解析完发现不对味、要重来」的返工来源。
 *
 * 设计纪律（用户口径「合理不过度、抓重点」）：
 * - **默认值全部预填**，点「跳过，用默认值解析」与改造前行为完全一致；
 * - 只收「不填会显著影响质量」的项：基调（决定节奏档）/ 单集时长（决定镜数预算）
 *   / 题材（影响技能包选择）/ 受众（影响爽感机制）；其余收进折叠区；
 * - 项目新建时已收集的（画风/画幅/国家/模型预设）**一律不重复问**；
 * - 重解析时**回填上次的值**（值来自后端单一真源，前端不硬编码档位）。
 */

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, type ParseRequirements } from "../api/client";
import { toast } from "../ui/feedback";

/** 完全默认值（与后端 normalize_requirements 的默认一致；拉取失败时兜底） */
const FALLBACK: ParseRequirements = {
  tone: "hook",
  episode_minutes: 6,
  genre: "",
  audience: "domestic",
  style_anchor: "",
  banned: "",
  note: "",
};

export function ParseRequirementsDialog({
  projectId,
  onConfirm,
  onClose,
}: {
  projectId: string;
  /** 确认解析：回传需求（点「跳过」时回传 null = 沿用存档/默认） */
  onConfirm: (reqs: ParseRequirements | null) => void | Promise<void>;
  onClose: () => void;
}) {
  const [reqs, setReqs] = useState<ParseRequirements>(FALLBACK);
  const [tones, setTones] = useState<{ value: string; label: string }[]>([
    { value: "hook", label: "短视频钩子驱动" },
    { value: "film", label: "微电影三幕式" },
  ]);
  const [minutes, setMinutes] = useState<number[]>([5, 6, 8, 10]);
  const [audiences, setAudiences] = useState<{ value: string; label: string }[]>([
    { value: "domestic", label: "国内（大众/下沉）" },
    { value: "overseas", label: "海外" },
  ]);
  const [anchors, setAnchors] = useState<
    { key: string; label: string; hint: string; form: string }[]
  >([]);
  const [stylePreset, setStylePreset] = useState("");
  const [showMore, setShowMore] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // 回填上次需求 + 拉选项表（后端单一真源；失败不阻断——用前端兜底默认值继续）
  useEffect(() => {
    let alive = true;
    api.getScriptRequirements(projectId)
      .then((r) => {
        if (!alive) return;
        setReqs({ ...FALLBACK, ...r.requirements });
        if (r.options?.tones?.length) setTones(r.options.tones);
        if (r.options?.episode_minutes?.length) setMinutes(r.options.episode_minutes);
        if (r.options?.audiences?.length) setAudiences(r.options.audiences);
      })
      .catch(() => { /* 读不到用默认值 */ });
    // 风格锚点按项目画风过滤（不相容的锚点选了不起作用，索性不展示）
    api.getProject(projectId)
      .then((p) => {
        if (alive) setStylePreset(p.style_preset);
      })
      .catch(() => { /* 忽略 */ });
    api.listStyleAnchors()
      .then((r) => {
        if (alive) setAnchors(r.anchors);
      })
      .catch(() => { /* 忽略 */ });
    return () => {
      alive = false;
    };
  }, [projectId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, submitting]);

  const submit = async (useDefaults: boolean) => {
    if (submitting) return;
    setSubmitting(true);
    try {
      await onConfirm(useDefaults ? null : reqs);
    } catch (e) {
      toast.error(`提交解析失败：${e instanceof Error ? e.message : String(e)}`);
      setSubmitting(false);
    }
  };

  // portal 到 body：脱离可能带 transform 的祖先（见 PromptOptimizeDialog 注释）
  return createPortal(
    <div className="settings-mask" onClick={() => !submitting && onClose()}>
      <div
        className="settings-dialog parse-req-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="settings-head">
          <span className="settings-title">解析前的几个设置</span>
          <button className="provider-close" disabled={submitting} onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="project-settings-body">
          <p className="project-settings-hint">
            以下设置影响分镜节奏、镜头预算与技能包选择，已按常用值预填——
            不修改可直接开始解析。
          </p>

          <div className="project-settings-row np-top">
            <span>剧作基调</span>
            <div className="np-pills">
              {tones.map((t) => (
                <button
                  key={t.value}
                  className={`style-btn ${reqs.tone === t.value ? "on" : ""}`}
                  onClick={() => setReqs({ ...reqs, tone: t.value })}
                  title={
                    t.value === "film"
                      ? "三幕式：单一核心事件、允许留白与呼吸、落点优先情感闭环"
                      : "钩子驱动：3 秒钩子、反转密、爽点集中释放（短剧常用）"
                  }
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>

          <div className="project-settings-row">
            <span>单集时长</span>
            <div className="np-pills">
              {minutes.map((m) => (
                <button
                  key={m}
                  className={`style-btn ${reqs.episode_minutes === m ? "on" : ""}`}
                  onClick={() => setReqs({ ...reqs, episode_minutes: m })}
                >
                  {m} 分钟
                </button>
              ))}
            </div>
          </div>

          <div className="project-settings-row">
            <span>题材</span>
            <input
              className="np-desc"
              placeholder="留空 = AI 自动判断（会影响技能包选择）"
              value={reqs.genre}
              maxLength={40}
              onChange={(e) => setReqs({ ...reqs, genre: e.target.value })}
            />
          </div>

          <div className="project-settings-row">
            <span>目标受众</span>
            <div className="np-pills">
              {audiences.map((a) => (
                <button
                  key={a.value}
                  className={`style-btn ${reqs.audience === a.value ? "on" : ""}`}
                  onClick={() => setReqs({ ...reqs, audience: a.value })}
                  title={
                    a.value === "overseas"
                      ? "海外更吃个人主义爽感：宿命拉扯、独立觉醒、荷尔蒙张力"
                      : "国内更吃集体主义爽感：围观群众与势利眼配角衬托主角"
                  }
                >
                  {a.label}
                </button>
              ))}
            </div>
          </div>

          <button
            className="link-btn parse-req-more"
            type="button"
            onClick={() => setShowMore((v) => !v)}
          >
            {showMore ? "▾" : "▸"} 更多选项（可选）
          </button>

          {showMore && (
            <>
              {anchors.some((a) => a.form === stylePreset) && (
                <div className="project-settings-row">
                  <span>风格锚点</span>
                  <select
                    className="model-select"
                    value={reqs.style_anchor}
                    onChange={(e) => setReqs({ ...reqs, style_anchor: e.target.value })}
                  >
                    <option value="">跟随项目画风（不指定）</option>
                    {anchors
                      .filter((a) => a.form === stylePreset)
                      .map((a) => (
                        <option key={a.key} value={a.key}>
                          {a.label}｜{a.hint}
                        </option>
                      ))}
                  </select>
                </div>
              )}
              <label className="project-settings-row">
                <span>禁用内容</span>
                <textarea
                  className="np-desc"
                  rows={2}
                  placeholder="敏感边界（如「血腥、烟酒、迷信」），画面与台词都不得出现，可空"
                  value={reqs.banned}
                  maxLength={300}
                  onChange={(e) => setReqs({ ...reqs, banned: e.target.value })}
                />
              </label>
              <label className="project-settings-row">
                <span>补充说明</span>
                <textarea
                  className="np-desc"
                  rows={2}
                  placeholder="其他要求（可空），例如「节奏偏慢、多给环境镜头」"
                  value={reqs.note}
                  maxLength={500}
                  onChange={(e) => setReqs({ ...reqs, note: e.target.value })}
                />
              </label>
            </>
          )}
        </div>

        {/* 按钮统一走系统弹窗标准件 `.fb-btn`（次要=ghost / 主要=primary）——
            此前用 ghost-btn + gen-btn（第三套词汇），与系统弹窗不一致 */}
        <div className="prompt-opt-foot">
          <button className="fb-btn ghost" disabled={submitting} onClick={() => void submit(true)}>
            跳过，用默认值解析
          </button>
          <button className="fb-btn primary" disabled={submitting}
                  onClick={() => void submit(false)}>
            {submitting ? "提交中…" : "开始解析"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
