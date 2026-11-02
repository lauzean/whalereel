/** 生成模型选择下拉（角色/场景/分镜/项目设置/首页创建共用）。 */

import { MODEL_PRESET_EMPTY, type ModelInfo } from "../api/client";

export function ModelSelect({
  models,
  value,
  onChange,
  emptyLabel = "默认",
  label,
}: {
  models: ModelInfo[];
  value: string;
  onChange: (v: string) => void;
  /** 空值选项文案：默认显示「默认」（有全局默认端点时由调用方传「默认：端点名」） */
  emptyLabel?: string;
  /** 下拉上方的说明文本（如「生图模型」），不传则只渲染下拉本身 */
  label?: string;
}) {
  const select = (
    <select
      className="model-select"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      title="生成模型（未选择时由项目预设/系统默认解析）"
    >
      <option value={MODEL_PRESET_EMPTY}>{emptyLabel}</option>
      {models.map((m) => (
        <option key={m.id} value={m.id}>
          {m.display_name || m.id}
          {m.installed ? "" : "（未安装）"}
        </option>
      ))}
    </select>
  );
  if (!label) return select;
  return (
    <div className="model-select-wrap">
      <span className="model-select-label">{label}</span>
      {select}
    </div>
  );
}
