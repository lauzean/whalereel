/** 新建项目弹窗：项目名/描述/模式/画风/画幅/视频分辨率/国家/模型预设，创建后直接进入工作台。 */

import { useEffect, useState } from "react";
import {
  api,
  type ModelInfo,
  type ModelPresetKind,
  type ModelPresetMap,
  type ProjectInfo,
  MODEL_PRESET_KINDS,
} from "../api/client";
import { defaultEmptyLabel, fetchModelOptions } from "../api/modelOptions";
import { DRAMA_STYLES } from "../drama/DramaView";
import {
  ASPECT_OPTIONS, DEFAULT_VIDEO_RESOLUTION, VIDEO_RESOLUTIONS,
} from "../drama/ProjectSettingsDialog";
import { ModelSelect } from "../ui/ModelSelect";
import { toast } from "../ui/feedback";

/** 新建表单的模型预设类别与展示文案（canvas/drama 两模式共用） */
const FORM_MODEL_PRESETS: { kind: ModelPresetKind; label: string }[] = [
  { kind: "t2i", label: "文生图模型" },
  { kind: "t2v", label: "文生视频模型" },
  // 项目级 LLM（与项目设置弹窗同一份选项；留空 = 全局默认）
  { kind: "llm", label: "对话模型 LLM" },
];

/** 国家预设选项（文生图/文生视频据此约束人物外貌） */
const COUNTRY_OPTIONS = ["中国", "美国", "英国", "法国", "日本", "韩国"];

export function NewProjectDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  /** 创建成功：回传新项目（调用方负责跳转工作台） */
  onCreated: (p: ProjectInfo) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<"canvas" | "drama">("canvas");
  const [stylePreset, setStylePreset] = useState<string>("real");
  // 风格锚点（美学第二级，可空）：选项表来自后端（单一真源），按画风形式过滤
  const [styleAnchor, setStyleAnchor] = useState<string>("");
  const [anchors, setAnchors] = useState<
    { key: string; label: string; prompt: string; form: string; hint: string }[]
  >([]);
  const [aspectRatio, setAspectRatio] = useState<string>("16:9");
  // 视频分辨率档（与项目设置同字段）：480p/720p/1080p，默认 720p
  const [resolution, setResolution] = useState<string>(DEFAULT_VIDEO_RESOLUTION);
  const [country, setCountry] = useState<string>("中国");
  const [presets, setPresets] = useState<ModelPresetMap>({});
  // 初始态从 MODEL_PRESET_KINDS 推导（不写死键：新增类别时不会再漏）
  const [options, setOptions] = useState<Record<ModelPresetKind, ModelInfo[]>>(
    () => Object.fromEntries(
      MODEL_PRESET_KINDS.map((k) => [k, [] as ModelInfo[]]),
    ) as Record<ModelPresetKind, ModelInfo[]>);
  const [creating, setCreating] = useState(false);

  // 模型下拉选项懒加载一次（本地注册表 + 模型服务端点模型）
  const [defaultLabels, setDefaultLabels] = useState<Record<string, string>>({});
  useEffect(() => {
    let alive = true;
    FORM_MODEL_PRESETS.forEach(({ kind }) => {
      fetchModelOptions(kind).then((models) => {
        if (alive) setOptions((prev) => ({ ...prev, [kind]: models }));
      });
      // 空值文案显示全局默认端点名（未设置保持「系统默认」）
      defaultEmptyLabel(kind).then((label) => {
        if (alive && label) setDefaultLabels((prev) => ({ ...prev, [kind]: label }));
      });
    });
    return () => {
      alive = false;
    };
  }, []);

  // 风格锚点选项：后端为单一真源（前端不硬编码，避免加了锚点前端看不到）
  useEffect(() => {
    let alive = true;
    api.listStyleAnchors()
      .then((r) => {
        if (alive) setAnchors(r.anchors);
      })
      .catch(() => {
        /* 锚点表读不到不阻断新建（退化为「不指定锚点」） */
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

  /** 提交创建：空值不入模型预设（后端按缺省处理 = 系统默认模型） */
  const create = async () => {
    setCreating(true);
    try {
      const modelPresets: ModelPresetMap = {};
      FORM_MODEL_PRESETS.forEach(({ kind }) => {
        const mid = presets[kind];
        if (mid) modelPresets[kind] = mid;
      });
      const p = await api.createProject({
        name: name.trim() || `项目 ${new Date().toLocaleDateString()}`,
        mode,
        style_preset: stylePreset,
        style_anchor: styleAnchor,
        aspect_ratio: aspectRatio,
        video_resolution: resolution,
        country,
        description: description.trim(),
        model_presets: modelPresets,
      });
      toast.success(`已创建项目「${p.name}」`);
      onCreated(p);
    } catch (e) {
      toast.error(`创建失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setCreating(false);
    }
  };

  return (
    <div
      className="settings-mask"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="settings-dialog create-project-dialog">
        <div className="settings-head">
          <span className="settings-title">新建项目</span>
          <button className="provider-close" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="project-settings-body np-body">
          <label className="project-settings-row">
            <span>项目名称</span>
            <input
              className="np-name"
              autoFocus
              placeholder={mode === "drama" ? "如：都市逆袭 30 集" : "输入项目名，开始创作"}
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void create()}
            />
          </label>
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
          <div className="project-settings-row">
            <span>项目类型</span>
            <div className="np-pills">
              <button
                className={`style-btn ${mode === "canvas" ? "on" : ""}`}
                onClick={() => setMode("canvas")}
              >
                创作 Agent
              </button>
              <button
                className={`style-btn ${mode === "drama" ? "on" : ""}`}
                onClick={() => setMode("drama")}
              >
                短剧&漫剧 Agent
              </button>
            </div>
          </div>
          {mode === "drama" && (
            <div className="project-settings-row">
              <span>画风</span>
              <div className="np-pills">
                {DRAMA_STYLES.map((s) => (
                  <button
                    key={s.key}
                    className={`style-btn ${stylePreset === s.key ? "on" : ""}`}
                    onClick={() => {
                      setStylePreset(s.key);
                      // 画风切换后原锚点可能不相容（写实↔动画流派）→ 清空
                      setStyleAnchor("");
                    }}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            </div>
          )}
          {/* 风格锚点（美学第二级）：与画风正交——画风定形式，锚点定美学；
              官方要求具体锚点（禁「电影风格」这类留白写法），不选则用画风默认 */}
          {mode === "drama" && anchors.some((a) => a.form === stylePreset) && (
            <div className="project-settings-row">
              <span>风格锚点</span>
              <div className="np-pills">
                <button
                  className={`style-btn ${styleAnchor === "" ? "on" : ""}`}
                  onClick={() => setStyleAnchor("")}
                  title="不指定锚点，使用画风预设的默认质感"
                >
                  默认
                </button>
                {anchors
                  .filter((a) => a.form === stylePreset)
                  .map((a) => (
                    <button
                      key={a.key}
                      className={`style-btn ${styleAnchor === a.key ? "on" : ""}`}
                      onClick={() => setStyleAnchor(a.key)}
                      title={`${a.prompt}｜${a.hint}`}
                    >
                      {a.label}
                    </button>
                  ))}
              </div>
            </div>
          )}
          {/* 画幅比例与分辨率：与项目设置同字段（新建设定，改设置只影响之后的任务） */}
          <label className="project-settings-row">
            <span>画幅比例</span>
            <select
              className="model-select"
              value={aspectRatio}
              onChange={(e) => setAspectRatio(e.target.value)}
            >
              {ASPECT_OPTIONS.map((a) => (
                <option key={a.value} value={a.value}>
                  {a.label}
                </option>
              ))}
            </select>
          </label>
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
          <label className="project-settings-row">
            <span>国家</span>
            <select
              className="model-select"
              value={country}
              onChange={(e) => setCountry(e.target.value)}
            >
              {COUNTRY_OPTIONS.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          {FORM_MODEL_PRESETS.map(({ kind, label }) => (
            <label key={kind} className="project-settings-row">
              <span>{label}</span>
              <ModelSelect
                models={options[kind]}
                value={presets[kind] ?? ""}
                onChange={(v) => setPresets((prev) => ({ ...prev, [kind]: v }))}
                emptyLabel={defaultLabels[kind]
                  ? `默认：${defaultLabels[kind]}`
                  : "系统默认"}
              />
            </label>
          ))}
          <button
            className="fb-btn primary np-submit"
            disabled={creating}
            onClick={() => void create()}
          >
            {creating ? "创建中…" : "创建项目"}
          </button>
        </div>
      </div>
    </div>
  );
}
