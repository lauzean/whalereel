/** 节点规格注册表：v0.2 统一模态节点 + 生成动作（对齐 docs/design.md 4.3/4.8）。

M1 落地：text / image / video / audio / timeline / export 六种；
character / scene / prop / storyboard 为短剧&漫剧资产节点（M3）。
 */

export type DataType = "text" | "image" | "video" | "audio" | "any";

export interface PortSpec {
  id: string;
  label: string;
  /** source 端口的数据类型 */
  type: DataType;
  /** target 端口可接受的类型（不填则只接受同类型） */
  accepts?: DataType[];
  multiple?: boolean;
}

export interface ParamSpec {
  id: string;
  label: string;
  kind: "text" | "textarea" | "select" | "number";
  options?: { value: string; label: string }[];
  default?: string | number;
  min?: number;
  max?: number;
  advanced?: boolean;
  placeholder?: string;
}

export type NodeType =
  | "text" | "image" | "video" | "audio"
  | "character" | "scene" | "prop" | "storyboard"
  | "timeline" | "export";

export interface NodeSpec {
  type: NodeType;
  title: string;
  icon: string;
  category: "basic" | "asset" | "organize";
  /** 该节点承载的生成能力（由参考端口自动判定动作，见 run.ts） */
  runnerHint?: string;
  defaultModel?: string;
  params: ParamSpec[];
  /** 空白态可上传文件的类型 */
  uploadAccept?: string;
  /** 是否支持抽卡多版本 */
  versioned: boolean;
  inputs: PortSpec[];
  outputs: PortSpec[];
}

export const ASPECT_OPTIONS = [
  { value: "16:9", label: "16:9 横屏" },
  { value: "9:16", label: "9:16 竖屏" },
  { value: "1:1", label: "1:1 方形" },
];

export const VIDEO_MODELS = [
  { value: "minimax-h3/fl2va-int8", label: "MiniMax H3（原生音频）" },
  { value: "ltx-2/distilled", label: "LTX-2.3（快速草稿）" },
  { value: "wan-2.2/a14b-gguf-q5", label: "Wan 2.2 A14B（无音频）" },
  { value: "wan-2.2/ti2v-5b", label: "Wan 2.2 5B（低配）" },
];

export const IMAGE_MODELS = [
  { value: "hidream-o1/image", label: "HiDream-O1（质量档）" },
  { value: "hidream-o1/dev", label: "HiDream-O1 Dev（快速档）" },
  { value: "qwen-image", label: "Qwen-Image" },
];

const PROMPT_PARAM: ParamSpec = {
  id: "prompt", label: "提示词", kind: "textarea", default: "",
};

export const NODE_SPECS: Record<string, NodeSpec> = {
  text: {
    type: "text", title: "文本", icon: "T", category: "basic",
    params: [{ id: "text", label: "内容", kind: "textarea", default: "" }],
    versioned: false,
    inputs: [],
    outputs: [{ id: "out", label: "文本", type: "text" }],
  },
  image: {
    type: "image", title: "图片", icon: "图", category: "basic",
    runnerHint: "t2i", defaultModel: "hidream-o1/image",
    params: [
      PROMPT_PARAM,
      { id: "model", label: "模型", kind: "select", options: IMAGE_MODELS, default: "hidream-o1/image" },
      { id: "aspect", label: "比例", kind: "select", options: ASPECT_OPTIONS, default: "16:9" },
      { id: "seed", label: "种子", kind: "text", default: "",
        placeholder: "留空随机；固定后同提示词可复现同图" },
      { id: "negative_prompt", label: "负面提示词", kind: "textarea", default: "",
        placeholder: "排除不想要的元素，如：text, watermark, calligraphy（仅质量档生效）" },
    ],
    versioned: true,
    uploadAccept: "image/png,image/jpeg,image/webp",
    inputs: [{ id: "ref", label: "参考", type: "any", accepts: ["image", "text"], multiple: true }],
    outputs: [{ id: "out", label: "图片", type: "image" }],
  },
  video: {
    type: "video", title: "视频", icon: "视", category: "basic",
    runnerHint: "t2v", defaultModel: "minimax-h3/fl2va-int8",
    params: [
      PROMPT_PARAM,
      { id: "model", label: "模型", kind: "select", options: VIDEO_MODELS, default: "minimax-h3/fl2va-int8" },
      { id: "duration", label: "时长(秒)", kind: "number", default: 5, min: 4, max: 15 },
      { id: "aspect", label: "比例", kind: "select", options: ASPECT_OPTIONS, default: "16:9" },
      { id: "camera_move", label: "运镜", kind: "select", options: [], default: "" },
    ],
    versioned: true,
    uploadAccept: "video/mp4,video/webm,video/quicktime",
    inputs: [{ id: "ref", label: "参考", type: "any", accepts: ["image", "video", "text"], multiple: true }],
    outputs: [{ id: "out", label: "视频", type: "video" }],
  },
  audio: {
    type: "audio", title: "音频", icon: "音", category: "basic",
    runnerHint: "tts", defaultModel: "edge-tts",
    params: [
      { id: "prompt", label: "台词", kind: "textarea", default: "" },
      { id: "voice", label: "音色", kind: "select", default: "zh-CN-XiaoxiaoNeural", options: [
        { value: "zh-CN-XiaoxiaoNeural", label: "晓晓（女）" },
        { value: "zh-CN-YunxiNeural", label: "云希（男）" },
        { value: "zh-CN-YunjianNeural", label: "云健（男）" },
      ] },
    ],
    versioned: true,
    uploadAccept: "audio/mpeg,audio/wav,audio/mp4",
    inputs: [{ id: "ref", label: "台词来源", type: "any", accepts: ["text"], multiple: true }],
    outputs: [{ id: "out", label: "音频", type: "audio" }],
  },
  timeline: {
    type: "timeline", title: "拼接时间线", icon: "拼", category: "organize",
    runnerHint: "compose",
    params: [
      { id: "mode", label: "模式", kind: "select", default: "concat", options: [
        { value: "concat", label: "顺序拼接" },
        { value: "transition", label: "叠化转场" },
      ] },
    ],
    versioned: true,
    inputs: [
      { id: "clips", label: "视频片段", type: "video", multiple: true },
      { id: "audio", label: "音频轨", type: "audio", multiple: true },
    ],
    outputs: [{ id: "out", label: "成片", type: "video" }],
  },
  export: {
    type: "export", title: "导出成片", icon: "导", category: "organize",
    runnerHint: "compose",
    params: [
      { id: "mode", label: "模式", kind: "select", default: "export", options: [
        { value: "export", label: "导出" },
      ] },
      { id: "resolution", label: "分辨率", kind: "select", default: "source", options: [
        { value: "source", label: "原始" },
        { value: "1280x720", label: "720p" },
        { value: "1920x1080", label: "1080p" },
      ] },
    ],
    versioned: true,
    inputs: [{ id: "video", label: "视频", type: "video" }],
    outputs: [],
  },
  // 短剧&漫剧资产节点（M3 落地，先注册规格）
  character: {
    type: "character", title: "角色", icon: "角", category: "asset",
    params: [
      { id: "name", label: "名称", kind: "text", default: "" },
      { id: "persona", label: "人设", kind: "textarea", default: "" },
      { id: "appearance", label: "外观", kind: "textarea", default: "" },
    ],
    versioned: true,
    uploadAccept: "image/png,image/jpeg,image/webp",
    inputs: [],
    outputs: [{ id: "out", label: "角色形象", type: "image" }],
  },
  scene: {
    type: "scene", title: "场景", icon: "景", category: "asset",
    params: [
      { id: "name", label: "名称", kind: "text", default: "" },
      { id: "description", label: "描述", kind: "textarea", default: "" },
    ],
    versioned: true,
    uploadAccept: "image/png,image/jpeg,image/webp",
    inputs: [],
    outputs: [{ id: "out", label: "场景图", type: "image" }],
  },
  prop: {
    type: "prop", title: "道具/素材", icon: "道", category: "asset",
    params: [
      { id: "name", label: "名称", kind: "text", default: "" },
      { id: "description", label: "描述", kind: "textarea", default: "" },
    ],
    versioned: true,
    uploadAccept: "image/png,image/jpeg,image/webp",
    inputs: [],
    outputs: [{ id: "out", label: "素材图", type: "image" }],
  },
  storyboard: {
    type: "storyboard", title: "分镜板", icon: "镜", category: "organize",
    params: [],
    versioned: false,
    inputs: [],
    outputs: [{ id: "out", label: "分镜", type: "video" }],
  },
};

export const NODE_CATEGORIES: { key: NodeSpec["category"]; title: string }[] = [
  { key: "basic", title: "基础节点" },
  { key: "organize", title: "组织节点" },
  { key: "asset", title: "资产节点" },
];

/** 节点预览的产物类型（决定 <img>/<video>/<audio>） */
export function previewKindOf(type: string): "image" | "video" | "audio" | "none" {
  if (type === "image" || type === "character" || type === "scene" || type === "prop") return "image";
  if (type === "video" || type === "timeline") return "video";
  if (type === "audio") return "audio";
  return "none";
}
