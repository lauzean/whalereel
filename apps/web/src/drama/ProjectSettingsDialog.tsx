/** 项目设置弹窗：编辑生成模型预设（文生图/文生视频），保存后同步项目元信息。 */

import { useEffect, useState } from "react";
import {
  api,
  MODEL_PRESET_KINDS,
  type ModelInfo,
  type ModelPresetKind,
  type ModelPresetMap,
  type ProjectInfo,
} from "../api/client";
import { defaultEmptyLabel, fetchModelOptions } from "../api/modelOptions";
import { ModelSelect } from "../ui/ModelSelect";
import { toast } from "../ui/feedback";

/** 预设编辑项（类别与后端 PRESET_KINDS 对齐，展示文案统一在此定义） */
const PRESET_ITEMS: { kind: ModelPresetKind; label: string }[] = [
  { kind: "t2i", label: "文生图模型" },
  { kind: "t2v", label: "文生视频模型" },
  // 项目级 LLM：设置后本项目**所有** LLM 调用（解析/审核/修复/扩写/视觉判级/
  // 重算）都优先用它；留空 = 全局默认。视觉判级需端点勾选「支持多模态」。
  { kind: "llm", label: "对话模型 LLM" },
];

/** 视频分辨率档（与后端 drama.VIDEO_RESOLUTION_HEIGHTS 同源）：
 *  档位=短边像素，位深随档位（480p/720p 为 8bit，1080p 为 10bit）。
 *  导出供新建项目弹窗共用（同字段同选项）。 */
export const VIDEO_RESOLUTIONS: { value: string; label: string }[] = [
  { value: "480p", label: "480p（8bit 位深）" },
  { value: "720p", label: "720p（8bit 位深）" },
  { value: "1080p", label: "1080p（10bit 位深）" },
];
export const DEFAULT_VIDEO_RESOLUTION = "720p";

/** 视频画幅比例（与后端 drama.ASPECT_WH / ASPECT_ADAPTIVE 同源）：
 *  adaptive = 自适应（尺寸交由端点按首帧/参考图决定）。导出供新建弹窗共用。 */
export const ASPECT_OPTIONS: { value: string; label: string }[] = [
  { value: "16:9", label: "16:9" },
  { value: "4:3", label: "4:3" },
  { value: "1:1", label: "1:1" },
  { value: "3:4", label: "3:4" },
  { value: "9:16", label: "9:16" },
  { value: "21:9", label: "21:9" },
  { value: "adaptive", label: "adaptive（自适应）" },
];

export function ProjectSettingsDialog({
  project,
  onClose,
  onSaved,
}: {
  project: {
    id: string;
    style_preset?: string;
    style_anchor?: string;
    model_presets?: ModelPresetMap;
    country?: string;
    aspect_ratio?: string;
    video_resolution?: string;
    /** 首帧提示词精炼开关（23 号批次 3）；缺省按 true（旧项目与新项目同行为） */
    refine_first_frame?: boolean;
    /** 组内环境锁定（32 号场景组） */
    scene_group_lock?: boolean;
    description?: string;
  };
  onClose: () => void;
  /** 保存成功后回传更新后的项目（同步 App 层项目元信息，避免陈旧值） */
  onSaved: (p: ProjectInfo) => void;
}) {
  // 初始态从 MODEL_PRESET_KINDS 推导（不写死键：新增类别时不会再漏）
  const [options, setOptions] = useState<Record<ModelPresetKind, ModelInfo[]>>(
    () => Object.fromEntries(
      MODEL_PRESET_KINDS.map((k) => [k, [] as ModelInfo[]]),
    ) as Record<ModelPresetKind, ModelInfo[]>);
  const [presets, setPresets] = useState<ModelPresetMap>({
    ...(project.model_presets ?? {}),
  });
  // 国家预设（文生图/文生视频据此约束人物外貌，默认中国）
  const [country, setCountry] = useState(project.country ?? "中国");
  // 项目描述（首页卡片展示）
  const [description, setDescription] = useState(project.description ?? "");
  // 视频画幅比例与分辨率档（新建时设定，改设置只影响之后提交的任务）
  // 风格锚点（美学第二级）：选项表来自后端（单一真源），按项目画风形式过滤
  const [styleAnchor, setStyleAnchor] = useState(project.style_anchor ?? "");
  const [anchors, setAnchors] = useState<
    { key: string; label: string; prompt: string; form: string; hint: string }[]
  >([]);
  const [aspect, setAspect] = useState(project.aspect_ratio ?? "16:9");
  /** 首帧提示词精炼（23 号批次 3）：默认开；缺省按 true（旧项目与新项目同行为） */
  const [refineFF, setRefineFF] = useState(project.refine_first_frame ?? true);
  // 组内环境锁定（32 号场景组）：开 = 同组场景生成带环境母版图作参考
  const [groupLock, setGroupLock] = useState(project.scene_group_lock ?? true);
  const [resolution, setResolution] = useState(
    project.video_resolution ?? DEFAULT_VIDEO_RESOLUTION,
  );
  const [saving, setSaving] = useState(false);

  // 模型下拉选项懒加载一次（本地注册表 + 模型服务端点模型）
  const [defaultLabels, setDefaultLabels] = useState<Record<string, string>>({});

  // 风格锚点选项：后端为单一真源（读不到时退化为「不指定锚点」，不阻断保存）
  useEffect(() => {
    let alive = true;
    api.listStyleAnchors()
      .then((r) => {
        if (alive) setAnchors(r.anchors);
      })
      .catch(() => {
        /* 忽略：锚点表不可用时保持默认档 */
      });
    return () => {
      alive = false;
    };
  }, []);
  useEffect(() => {
    let alive = true;
    PRESET_ITEMS.forEach(({ kind }) => {
      fetchModelOptions(kind).then((models) => {
        if (alive) setOptions((prev) => ({ ...prev, [kind]: models }));
      });
      // 空值文案显示全局默认端点名（未设置显示「默认」）
      defaultEmptyLabel(kind).then((label) => {
        if (alive && label) setDefaultLabels((prev) => ({ ...prev, [kind]: label }));
      });
    });
    return () => {
      alive = false;
    };
  }, []);

  // ESC 关闭弹窗
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  /** 保存预设：空值剔除（= 未设置，该类任务走系统默认模型），整体替换后端预设 */
  const save = async () => {
    setSaving(true);
    try {
      const next: ModelPresetMap = {};
      MODEL_PRESET_KINDS.forEach((kind) => {
        const mid = presets[kind];
        if (mid) next[kind] = mid;
      });
      const updated = await api.updateProjectSettings(
        project.id, next, country, aspect, resolution, description, styleAnchor,
        refineFF, groupLock);
      onSaved(updated);
      toast.success("项目预设已更新");
      onClose();
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="settings-mask"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="settings-dialog project-settings-dialog">
        <div className="settings-head">
          <span className="settings-title">项目设置</span>
          <button className="provider-close" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="project-settings-body">
          <p className="project-settings-hint">
            预设模型作为本项目内容生成的默认模型（角色/场景/分镜/画布 Agent
            建节点），各生成入口仍可临时选择其他模型；修改仅影响之后的任务。
          </p>
          {/* 项目描述：首页卡片展示 */}
          <label className="project-settings-row np-top">
            <span>项目描述</span>
            <textarea
              className="np-desc"
              rows={2}
              maxLength={500}
              placeholder="一句话简介（展示在首页项目卡片上，可空）"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          {PRESET_ITEMS.map(({ kind, label }) => (
            <label key={kind} className="project-settings-row">
              <span>{label}</span>
              <ModelSelect
                models={options[kind]}
                value={presets[kind] ?? ""}
                onChange={(v) => setPresets((prev) => ({ ...prev, [kind]: v }))}
                emptyLabel={defaultLabels[kind]
                  ? `默认：${defaultLabels[kind]}`
                  : undefined}
              />
            </label>
          ))}
          {/* 国家预设：文生图/文生视频据此约束人物外貌（如 中国=东亚面孔），默认中国 */}
          <label className="project-settings-row">
            <span>国家</span>
            <select
              className="model-select"
              value={country}
              onChange={(e) => setCountry(e.target.value)}
            >
              {["中国", "美国", "英国", "法国", "日本", "韩国"].map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          {/* 风格锚点（美学第二级）：画风定形式、锚点定美学；官方要求具体锚点
              （禁「电影风格」这类留白写法）。改后只影响之后提交的生成任务 */}
          {anchors.some((a) => a.form === (project.style_preset ?? "")) && (
            <label className="project-settings-row">
              <span>风格锚点</span>
              <select
                className="model-select"
                value={styleAnchor}
                onChange={(e) => setStyleAnchor(e.target.value)}
                title={anchors.find((a) => a.key === styleAnchor)?.hint ?? ""}
              >
                <option value="">默认（不指定锚点）</option>
                {anchors
                  .filter((a) => a.form === project.style_preset)
                  .map((a) => (
                    <option key={a.key} value={a.key}>
                      {a.label}｜{a.hint}
                    </option>
                  ))}
              </select>
            </label>
          )}
          {/* 视频分辨率档：决定端点出片规格（档位=短边像素） */}
          <label className="project-settings-row">
            <span>视频分辨率</span>
            <select
              className="model-select"
              value={resolution}
              onChange={(e) => setResolution(e.target.value)}
            >
              {VIDEO_RESOLUTIONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          {/* 画幅比例：adaptive 交给端点按首帧自适应 */}
          <label className="project-settings-row">
            <span>画幅比例</span>
            <select
              className="model-select"
              value={aspect}
              onChange={(e) => setAspect(e.target.value)}
            >
              {ASPECT_OPTIONS.map((a) => (
                <option key={a.value} value={a.value}>
                  {a.label}
                </option>
              ))}
            </select>
          </label>
          {/* 首帧提示词精炼（23 号批次 3）：每镜首帧在提交生图前先用 LLM 精炼一次
              —— 删运镜/时间标记等噪音、补画面细节、修景别与画面规模的矛盾。失败
              自动回退初稿（fail-open），不影响出图。改后只影响之后提交的首帧任务 */}
          <div className="project-settings-row">
            <span>首帧精炼</span>
            <label className="project-settings-check">
              <input
                type="checkbox"
                checked={refineFF}
                onChange={(e) => setRefineFF(e.target.checked)}
              />
              提交前用 LLM 精炼首帧提示词（失败自动回退初稿）
            </label>
          </div>
          <div className="project-settings-row">
            <span>场景组环境</span>
            <label className="project-settings-check">
              <input
                type="checkbox"
                checked={groupLock}
                onChange={(e) => setGroupLock(e.target.checked)}
              />
              同组场景生成时用「环境母版图」作参考（32 号：一套房的不同房间环境一致）
            </label>
          </div>
          <button className="fb-btn primary" disabled={saving} onClick={() => void save()}>
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
