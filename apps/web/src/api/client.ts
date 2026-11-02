/** REST + WebSocket 客户端。 */

import {
  cachedSignedFileUrl,
  cachedSignedUrl,
  primeSignedUrls,
} from "./fileUrls";

export interface TaskInfo {
  id: string;
  project_id: string;
  node_id: string;
  runner: string;
  model_id: string;
  status: "queued" | "loading_model" | "running" | "done" | "failed" | "cached" | "canceled";
  progress: number;
  artifact_id: string | null;
  version_id: string | null;
  /** 批次聚合 id（批量首帧/视频共用一个批次） */
  batch_id: string | null;
  error: string;
  timings: Record<string, unknown>;
  /** 过程轨迹（解析任务持久化：进度步骤 + 思考轮次） */
  trace?: TaskTrace;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

/** 解析过程轨迹（持久化 + WS task_trace 实时推送） */
export interface TaskTrace {
  stages: { label: string; progress: number | null; at: number }[];
  thoughts: { title: string; content: string; at: number }[];
  /** 确定性门禁留痕（节拍整合/语速硬门/资产对账/体检等，gates 桶独立于 thoughts） */
  gates?: { gate: string; content: string; at: number }[];
}

/** 终审问题条目（与后端 audit issues 结构对齐） */
export interface AuditIssue {
  severity: string;
  location: string;
  type: string;
  description: string;
  suggestion: string;
  /** 问题所在的逐字原句（修复补丁的定位锚） */
  quote?: string;
  /** 逐集修复的命中对账（代码逐字应用补丁后回写）：applied=false 说明未命中 */
  patch?: { applied: boolean; reason?: string };
}

/** 逐集审核结构化结果（落在 Episode.audit_json，随分集列表透出）
 *
 * passed=false 表示存在 critical（该集待处理）；issues 里的 major/minor 只是建议。
 * exit_state 是本集出口状态快照，供下一集审核与拆解做衔接锚。
 */
export interface EpisodeAuditPayload {
  passed: boolean;
  verdict: string;
  issues: AuditIssue[];
  exit_state?: string;
  /** 钩子与冲突强度的经营提醒（2026-10-10）：只展示，不进 issues、不参与修复 */
  craft_note?: string;
  /** 是否已跑过一键修复（面板据此区分「待修复」与「已修复」） */
  patched?: boolean;
  cached?: boolean;
  at?: number;
}

/** 一条问题是否**尚未修复**。
 *
 * 修完不复审，所以清单会**保留**已修的条目（只打 patch.applied 标记）——任何
 * 「还剩几条要修」「能不能放行」的判定都必须先过这一层，否则已修的问题会一直
 * 被算作待修（实测：2 条 critical 已修，进度条仍显示「2 条严重待修」、审核步骤
 * 永远标不成完成）。patch.applied=false 是「补丁未命中」，仍属待修。
 */
export function isOutstandingIssue(it: AuditIssue): boolean {
  return it.patch?.applied !== true;
}

/** 审核清单里未修复的问题（severity 过滤可选） */
export function outstandingIssues(
  audit: EpisodeAuditPayload | null | undefined,
  severity?: string,
): AuditIssue[] {
  return (audit?.issues ?? []).filter(
    (it) => isOutstandingIssue(it) && (!severity || it.severity === severity));
}

/** 外置提示词条目（系统配置·提示词页签，批次 2b §32.4） */

/** 镜头裁决日志条目（批次 3 §34.1：Take N · changed · verdict · evidence） */
export interface ShotReviewInfo {
  take_seq: number;
  verdict: "keep" | "post" | "edit" | "reroll" | "rewrite" | "pending" | string;
  changed: string;
  /** 重摇原因（25 号 2.4；空 = 首拍/自动重试，或历史存量记录） */
  reason: string;
  evidence: string;
  reviewer: string;
  observed: Record<string, unknown>;
  created_at: number;
}

/** 孤儿产物（25 号 2.5：不再被任何现存对象引用的版本——产物文件仍在磁盘） */
export interface OrphanVersionInfo {
  version_id: string;
  node_id: string;
  seq: number;
  /** shot_v 分镜视频 / shot_ff 分镜首帧 / char 角色形象 / scene 场景主图 / prop 道具图 */
  kind: string;
  entity_id: string;
  artifact_id: string;
  asset_kind: string;
  name: string;
  prompt: string;
  bytes: number;
  on_disk: boolean;
  created_at: number;
}

export interface PromptMeta {
  name: string;
  description: string;
  stage: string;
  editable: boolean;
}

export interface PromptDetail extends PromptMeta {
  content: string;
  has_snapshot: boolean;
}

/** AI 解析前置需求（21 号批次 5）：每项都必须回答「不填会怎样」才立项——
 *  剧作基调决定分镜节奏档、单集时长决定镜数预算、题材影响技能包选择、
 *  受众影响爽感机制；其余为可折叠的可选项。 */
export interface ParseRequirements {
  /** 剧作基调：hook（短视频钩子驱动）| film（微电影三幕式） */
  tone: string;
  /** 单集目标时长（分钟） */
  episode_minutes: number;
  /** 题材标签（空 = AI 自动判断） */
  genre: string;
  /** 目标受众：domestic | overseas */
  audience: string;
  /** 风格锚点（空 = 跟随项目画风） */
  style_anchor: string;
  /** 禁用内容/敏感边界（自由文本，空 = 无） */
  banned: string;
  /** 补充说明（自由文本，空 = 无） */
  note: string;
}

/** 剧本原文历史版本（v1.1 起只存原文，恢复＝回填编辑器） */
export interface ScriptVersionInfo {
  id: string;
  seq: number;
  note: string;
  created_at: number;
  /** 该版剧本原文字数（v1.1：版本只存原文，不再有整包产物统计） */
  text_chars: number;
}

export interface NodeVersionInfo {
  id: string;
  node_id: string;
  seq: number;
  source: "generated" | "uploaded" | "edited";
  /** 版本命名（空 = 前端按默认格式「版本N」展示） */
  name: string;
  prompt: string;
  refs: string[];
  task_id: string | null;
  artifact_id: string | null;
  kind: "image" | "video" | "audio" | null;
  meta: Record<string, unknown>;
  created_at: number;
}

export interface CameraMove {
  id: string;
  name: string;
  category: string;
  prompt: string;
}

// ---------- 项目生成模型预设（与后端 model_manager.PRESET_KINDS 对齐） ----------

/** 项目可预设的任务类别（与后端 model_manager.PRESET_KINDS 对齐）。
 *
 * `llm`：设置后该项目的**所有** LLM 调用（解析/审核/修复/扩写/视觉判级/重算）
 * 优先走这个端点；未设置 = 走全局默认（向后兼容）。TTS 仍走全局配置。 */
export const MODEL_PRESET_KINDS = ["t2i", "t2v", "llm"] as const;
export type ModelPresetKind = (typeof MODEL_PRESET_KINDS)[number];
/** 项目模型预设的键值映射 */
export type ModelPresetMap = Partial<Record<ModelPresetKind, string>>;
/** 模型下拉空值选项 value（空串 = 后端按项目预设/全局默认/系统默认解析） */
export const MODEL_PRESET_EMPTY = "";

export interface ProjectInfo {
  id: string;
  name: string;
  mode: "canvas" | "drama";
  style_preset: "real" | "3d" | "anime" | "guofeng";
  /** 风格锚点（美学第二级，可空串=不指定）：取值见 GET /api/projects/style-anchors */
  style_anchor: string;
  /** 视频画幅比例：16:9 | 4:3 | 1:1 | 3:4 | 9:16 | 21:9 | adaptive（自适应） */
  aspect_ratio: string;
  /** 视频分辨率档：480p(8bit) | 720p(8bit) | 1080p(10bit)，默认 720p */
  video_resolution: string;
  /** 国家预设（默认中国，文生图/文生视频据此约束人物外貌） */
  country: string;
  /** 首帧提示词精炼（23 号批次 3，默认 true）：开启后每镜首帧先用 LLM 精炼一次
   *  （删噪音 / 补画面细节 / 修矛盾）；失败自动回退初稿。关掉 = 退回改造前行为 */
  refine_first_frame: boolean;
  /** 项目描述（项目设置填写，可空） */
  description: string;
  /** 剧本一句话简介（解析产出，首页卡片描述优先展示；未解析为空串） */
  logline: string;
  /** 生成模型预设：{"t2i": "...", "t2v": "..."}，缺键 = 用系统默认 */
  model_presets: ModelPresetMap;
  created_at: number;
  updated_at: number;
  cover_artifact_id: string | null;
}

// ---------- 用户体系（登录 / 账号管理） ----------

/** 登录用户信息（/api/auth/me 与账号管理列表共用） */
export interface AuthUserInfo {
  id: string;
  username: string;
  is_admin: boolean;
  disabled: boolean;
  created_at: number;
  last_login_at: number | null;
  /** 仍是出厂默认口令（首个启动种子的 admin/admin123）→ 强制引导改密 */
  must_change_password?: boolean;
}

/** 登录响应：令牌 + 用户信息 */
export interface LoginResult {
  token: string;
  expires_at: number;
  user: AuthUserInfo;
}

// ---------- 技能包（系统管理·技能包页签） ----------

/** 技能包元信息（kind：genre 题材知识包 | model 模型提示词包） */
export interface SkillPackMeta {
  file: string;
  name: string;
  kind: "genre" | "model";
  genres: string[];
  models: string[];
  task_type: string;
  description: string;
  chars: number;
}

/** 技能包全文（含 frontmatter） */
export interface SkillPackDetail {
  ok: boolean;
  file: string;
  content: string;
  has_snapshot: boolean;
}

/** 项目产物资产（资产库三方联动，4.14）：删除节点后产物仍保留，可找回 */
export interface ProjectAssetInfo {
  artifact_id: string;
  kind: "image" | "video" | "audio";
  name: string;
  prompt: string;
  node_id: string;
  source: string;
  created_at: number;
}

// ---------- Agent 会话（M2 5.1/5.2/5.5） ----------

/** 会话：右侧 Agent 对话面板，多会话并行（4.2/5.1） */
export interface SessionInfo {
  id: string;
  project_id: string;
  title: string;
  status: "active" | "closed";
  pane_side: "left" | "right";
  created_at: number;
  updated_at: number;
}

/** 消息 @引用（ID 级引用，version_id 缺省取节点当前生效版本） */
export interface ChatMessageRef {
  node_id: string;
  version_id?: string;
}

/** 对话消息（含 @引用与工具调用记录） */
export interface ChatMessage {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  refs: ChatMessageRef[];
  tool_calls: unknown[];
  created_at: number;
}

/** 计划确认卡：批量/长耗时操作先确认再执行（5.5） */
export interface ChatPlan {
  node_count: number;
  model: string;
  estimated_seconds: number;
  summary: string;
}

/** Agent 动作：node 携带画布节点载荷（与 studio 节点结构一致），task_id 关联排队任务 */
export interface ChatAction {
  type: string;
  node_id: string;
  node?: {
    id: string;
    type: string;
    position: { x: number; y: number };
    data?: {
      specType?: string;
      label?: string;
      params?: Record<string, string | number>;
      refs?: { nodeId: string; label: string }[];
    };
  };
  task_id?: string;
}

/** 发消息入参（POST /api/sessions/{sid}/chat） */
export interface ChatSendBody {
  text: string;
  refs?: ChatMessageRef[];
  confirmed?: boolean;
}

/** 发消息响应：助手回复 + 画布动作 + 待确认计划 */
export interface ChatResponse {
  message_id: string;
  reply: string;
  actions: ChatAction[];
  plan: ChatPlan | null;
}

// ---------- 短剧&漫剧（M3） ----------

/** 剧本文档提取结果（txt/md/docx/pdf → 纯文本，不直接落库） */
export interface ScriptExtractResult {
  filename: string;
  chars: number;
  text: string;
}

/** 角色形象（基础形象 / 妆造形象），artifact_id 为定妆图产物 */
export interface CharacterImageInfo {
  id: string;
  name: string;
  kind: string;
  /** 适用集数（seq 列表，空 = 未设置） */
  episodes?: number[];
  artifact_id: string | null;
  /** 用户「设为生效」的版本 id（空 = 跟随最新版本） */
  preferred_version_id?: string | null;
}

export interface CharacterInfo {
  id: string;
  name: string;
  /** 角色定位（LLM 按剧本定义，可手动调整）：主角 | 配角 */
  role: string;
  persona: string;
  appearance: string;
  /** 音色描述（LLM 按人设生成，用于配音音色选型） */
  voice_desc: string;
  /** 音色参考音频（用户上传的 Asset.id，生成视频时作配音音色参考） */
  voice_ref_asset_id: string | null;
  /** 定妆图生图提示词（LLM 基于剧本大背景生成，可编辑） */
  image_prompt: string;
  images: CharacterImageInfo[];
  /** 首次解析出该资产的集号（逐集资产解析写入；null/缺失 = 解析期或手工创建） */
  origin_seq?: number | null;
  /** 出场顺序（首次出现在该集正文里的先后；0/缺省 = 未记录，老数据） */
  order_seq?: number | null;
  generating?: boolean;
}

/** 妆造建议（解析提取的换装事件，角色卡确认后生成妆造图） */
export interface CostumeSuggestionInfo {
  id: string;
  character: string;
  name: string;
  episodes: number[];
  appearance_desc: string;
  evidence: string;
}

/** 资产删除的映射统计（预检 references / 已清除 cleared 共用） */
export interface AssetDeleteRefs {
  /** 引用该资产的分镜数 */
  shots: number;
  /** 引用分镜涉及的集 seq（去重升序） */
  episodes: number[];
  /** 角色专用：妆造映射的分镜数 */
  costume_maps?: number;
  /** 角色专用：形象作用域绑定条数 */
  asset_links?: number;
}

/** 资产删除响应（confirm=false 预检返回 references；confirm=true 返回 cleared） */
export interface AssetDeleteResponse {
  ok: boolean;
  deleted: boolean;
  name?: string;
  references?: AssetDeleteRefs;
  cleared?: AssetDeleteRefs;
}

export interface SceneInfo {
  id: string;
  name: string;
  description: string;
  /** 场景主图生图提示词（LLM 基于剧本世界观生成，可编辑） */
  image_prompt: string;
  /** 场景组（32 号）：同一真实空间的成员信息；null/缺失 = 未分组 */
  group?: { id: string; name: string; env_setting: string } | null;
  artifact_id: string | null;
  /** 用户「设为生效」的版本 id（空 = 跟随最新版本） */
  preferred_version_id: string | null;
  /** 出现频数 */
  freq?: number;
  /** 出现集数描述 */
  episodes?: string;
  /** 首次解析出该资产的集号（逐集资产解析写入；null/缺失 = 解析期或手工创建） */
  origin_seq?: number | null;
  /** 出场顺序（首次出现在该集正文里的先后；0/缺省 = 未记录，老数据） */
  order_seq?: number | null;
  generating?: boolean;
}

export interface PropInfo {
  id: string;
  name: string;
  description: string;
  /** 道具定妆图生图提示词（LLM 生成，可编辑） */
  image_prompt: string;
  artifact_id: string | null;
  /** 用户「设为生效」的版本 id（空 = 跟随最新版本） */
  preferred_version_id: string | null;
  /** 出现频数 */
  freq?: number;
  /** 出现集数描述 */
  episodes?: string;
  /** 首次解析出该资产的集号（逐集资产解析写入；null/缺失 = 解析期或手工创建） */
  origin_seq?: number | null;
  /** 出场顺序（首次出现在该集正文里的先后；0/缺省 = 未记录，老数据） */
  order_seq?: number | null;
  generating?: boolean;
}

/** 单镜生成参数（排查视图：模型实际收到的 prompt 与参考图清单） */
export interface GenerationParamsTask {
  id: string;
  node_id: string;
  runner: string;
  model_id: string;
  status: string;
  /** 最终 prompt（含画风/国家注入后，含「参考图N」标注） */
  prompt: string;
  /** 其余生成参数（aspect/seed/duration 等） */
  params: Record<string, unknown>;
  /** 原始引用（artifact id 有序，顺序即参考图编号） */
  input_refs: { image?: string; extra_images?: string[] } & Record<string, unknown>;
  /** runner 侧结构化记录（实际上送路径与截断数量；旧任务为 null） */
  generation: {
    runner?: string;
    model_id?: string;
    prompt?: string;
    ref_paths_used?: string[];
    refs_in?: number;
    refs_used?: number;
    refs_dropped?: number;
    /** 提交前对提示词做过的改动说明（脱参考图标注 / 剥离秒数锚点等；旧任务无此字段） */
    prompt_notes?: string[];
  } | null;
  created_at: number;
}

export interface EpisodeInfo {
  id: string;
  seq: number;
  title: string;
  status?: string;
  shot_count: number;
  /** 已合成的成片产物（episode 节点最新 done 任务；null = 未合成或已失效） */
  compose_artifact_id?: string | null;
  /** 当集正文字数（正文本身是大字段，按需查 /episodes/{id}/script） */
  body_chars?: number;
  /** 一句话梗概（分集要点表） */
  synopsis?: string;
  /** 剧情要点：关键情节点（逐集审核的上下文来源） */
  beats?: string[];
  /** 钩子/爆点清单 */
  hooks?: { kind?: string; text?: string }[];
  /** 分集来源：head（原文集头切分）| llm（LLM 按剧情切集） */
  plan_source?: string;
  /** 制作状态：planned|needs_fix|audited|building|built|failed */
  produce_status?: string;
  /** 最近一次该集审核结果 */
  audit?: EpisodeAuditPayload;
  /** 逐集资产解析产出存档（**空对象 = 本集尚未解析**）。
   *
   * 字段名与后端逐一对应：后端返回的是 `assets`（`_episode_assets()` 的存档），
   * 此前这里错写成 `asset_seqs: number[]`——**契约对不上**，前端恒读到 undefined，
   * 叠加 `origin_seq` 未序列化，导致「本集资产」永远显示「未解析」。
   * 资产首见集号见各资产自身的 `origin_seq`。 */
  assets?: {
    characters?: number;
    scenes?: number;
    props?: number;
    /** 资产字段二分门禁告警（G7/G8，仅提示不拦截） */
    field_warnings?: string[];
    reused?: { characters?: string[]; scenes?: string[]; props?: string[] };
    aliases?: unknown[];
    /** 补充视角建议（资产解析时 LLM **只提示**，是否生成由用户决定） */
    view_hints?: { scene: string; view: string; reason: string }[];
    at?: number;
  };
}

/** 分集制作状态（与后端 PRODUCE_* 常量逐字一致） */
export const PRODUCE_STATUS = {
  planned: "planned",
  needsFix: "needs_fix",
  audited: "audited",
  building: "building",
  built: "built",
  failed: "failed",
} as const;

/** 制作状态徽标文案（未知状态原样显示，便于排查） */
export const PRODUCE_STATUS_LABEL: Record<string, string> = {
  [PRODUCE_STATUS.planned]: "未审核",
  [PRODUCE_STATUS.needsFix]: "待修复",
  [PRODUCE_STATUS.audited]: "已放行",
  [PRODUCE_STATUS.building]: "制作中",
  [PRODUCE_STATUS.built]: "已出分镜",
  [PRODUCE_STATUS.failed]: "制作失败",
};

/** 台词行（说话人 + 内容，os 为旁白标记） */
export interface DialogueLine {
  speaker: string;
  text: string;
  os?: boolean;
}

export interface ShotInfo {
  id: string;
  seq: number;
  description: string;
  /** 导演级视频画面描述（LLM 生成，@角色/@场景 引用素材） */
  video_prompt: string;
  camera: string;
  duration: number;
  scene_ref_id: string | null;
  character_ref_ids: string[];
  /** 道具引用（资产栏定位/提及映射数据源） */
  prop_ref_ids: string[];
  /** 妆造映射：character_id → character_image_id */
  costume_map?: Record<string, string>;
  dialogue: DialogueLine[];
  /** 时序对账：画面描述时间小节与当前时长是否一致（false=时长被编辑过或生成失配，建议重算） */
  timing_ok?: boolean;
  /** 多切关键帧（批次 4 §35.2，仅 key_scenes）：段内 2 切每切一张关键帧 */
  cuts?: { cut: number; offset: number; description: string; artifact_id: string | null }[];
  /** 镜头目的（批次 2b §32.5）：establish|reaction|turn|insert|transition */
  purpose?: string;
  /** 已知脆弱点（批次 2b §32.5）：face|prop|text|motion|crowd */
  risks?: string[];
  /** 结束状态账本（批次 2c §32.6）：状态链对账出口态 */
  end_state?: { who?: string[]; position?: string; props?: string; time?: string };
  first_frame_artifact_id: string | null;
  video_artifact_id: string | null;
  status: string;
  generating: boolean;
}

/** 分镜更新入参（PATCH /api/shots/{sid}） */
export interface ShotPatchBody {
  description?: string;
  /** 导演级视频画面描述（@角色/@场景 引用素材） */
  video_prompt?: string;
  duration?: number;
  scene_ref_id?: string | null;
  character_ref_ids?: string[];
  camera?: string;
  /** 与上一镜的衔接类型（continue/time_skip/scene_cut）——体检报告一键修正 */
  transition_in?: string;
  /** 妆造映射：character_id → character_image_id */
  costume_map?: Record<string, string>;
}

/** 模型调用日志：一次对外调用一行（系统管理·调用日志页） */
export interface ModelCallInfo {
  id: string;
  at: number;
  /** 调用点（中文标签：LLM / Agnes 文生图 / 方舟文生视频提交 …） */
  what: string;
  /** 服务域名（由请求地址确定性取出） */
  host: string;
  /** 模型名（由请求内容确定性取出） */
  model: string;
  ok: boolean;
  http_status: number | null;
  /** 失败类别（欠费/限流/内容安全…，成功时为空） */
  fail_class: string;
  error: string;
  elapsed_ms: number;
  /** 消费信息：tokens / images / video 秒数等（取不到为空对象） */
  usage: Record<string, unknown>;
  project_id: string;
  task_id: string;
}

/** 单次调用详情（调用日志「查看详情」弹窗）：含完整地址与请求/响应留痕。
 *  留痕在服务端写入时已打码敏感字段并截断超长内容（超长带截断标记）。 */
export interface ModelCallDetail extends ModelCallInfo {
  /** 完整请求地址 */
  url: string;
  /** 接口入参（JSON 字符串；空串 = 该调用没有可留痕的入参） */
  request_json: string;
  /** 接口响应/输出（JSON 字符串；流式 LLM 为 {"content": "..."}） */
  response_json: string;
}

/** 模型调用看板聚合结果 */
export interface ModelCallStats {
  window_days: number;
  /** 实际生效的时间窗（自定义区间时 label 形如「2026-09-01 至 2026-09-15」） */
  window: { since: number; until: number; label: string };
  total: { calls: number; ok: number; failed: number };
  consumption: { images: number; video_seconds: number; tokens: number };
  by_model: {
    what: string;
    host: string;
    model: string;
    calls: number;
    failed: number;
    avg_ms: number;
    images: number;
    tokens: number;
    video_seconds: number;
  }[];
  by_day: { date: string; calls: number; failed: number }[];
}

/** 自学习全局洞察：跨项目系统性弱项（抽象统计，不含具体案例名） */
export interface LessonsPattern {
  rule: string;
  title: string;
  count: number;
  projects: number;
  section: string;
}

/** 自学习提示词改进建议（应用前用户确认；applied=目标提示词已含同规则标记） */
export interface LessonsSuggestion {
  rule: string;
  title: string;
  /** 目标提示词名（如 SCRIPT_PARSE_SYSTEM_PROMPT） */
  target: string;
  /** 证据（累计 N 次、跨 M 个项目） */
  evidence: string;
  /** 待追加到目标提示词的正文 */
  append_text: string;
  applied: boolean;
}

/** 分镜体检项的结构化修复意图（后端给出，前端逐字应用——禁止猜语义） */
export interface QaFixIntent {
  /** 目标字段（如 transition_in） */
  field: string;
  /** 目标值（如 scene_cut） */
  value: string;
  /** 按钮文案（如「切换场景」） */
  label: string;
}

/** 分镜体检报告项（解析期存档；title/plain 为面向用户的白话解释） */
export interface QaIssueInfo {
  rule: string;
  level: "error" | "warn";
  episode: number | null;
  seq: number | null;
  /** 内部术语描述（面向懂行的人） */
  message: string;
  suggest: string;
  /** 白话标题（如「人物可能凭空消失」） */
  title: string;
  /** 白话说明（问题影响与处理方式） */
  plain: string;
  /** 结构化修复意图（可缺省：有则渲染一键修复按钮） */
  fix?: QaFixIntent;
}

/** 剧本解析结果（GET script / POST script/parse） */
export interface ScriptData {
  analyzed: boolean;
  title: string;
  logline: string;
  style: string;
  /** 视频风格（审读产出物，如：水墨写意+冷色调渲染） */
  video_style: string;
  /** 视频画幅比例（审读产出物：16:9 / 9:16 等） */
  aspect_ratio: string;
  /** 剧本内容（创作型解析后为 LLM 规范化的完整剧本） */
  text: string;
  /** 当前生效解析结果的版本号（未解析为 0，header 标签展示） */
  version: number;
  /** 当前生效解析过程轨迹（观测窗恢复；随历史版本保存/恢复同步） */
  trace?: TaskTrace;
  characters: CharacterInfo[];
  scenes: SceneInfo[];
  /** 关键叙事道具资产（跨镜头外观一致性锚点） */
  props: PropInfo[];
  episodes: EpisodeInfo[];
  shots: number;
}

// ---------- 推理服务配置 ----------

/** 单类任务的第三方服务配置状态（api_key 脱敏，仅前 4 后 4 位） */
export interface ProviderInfo {
  kind: string;
  provider: "local" | "openai" | "custom";
  base_url: string;
  model: string;
  api_key: string;
  is_local: boolean;
}

/** 更新第三方服务配置入参（未提供的可选字段保留 .env 已有值） */
export interface ProviderUpdateBody {
  provider: string;
  base_url?: string;
  api_key?: string;
  model?: string;
}

/** 更新第三方服务配置响应（配置层现读环境变量，保存后即时生效） */
export interface ProviderUpdateResult {
  ok: boolean;
  providers: Record<string, ProviderInfo>;
}

// ---------- 模型服务管理（Key 管理 / 模型服务端点 / 默认配置） ----------

/** 端点的模型类型枚举（与后端 PROVIDER_KINDS 对齐） */
export const ENDPOINT_KINDS = ["llm", "t2i", "t2v", "i2v", "tts"] as const;
export type EndpointKind = (typeof ENDPOINT_KINDS)[number];
/** 模型类型的中文标签（单点定义） */
export const ENDPOINT_KIND_LABELS: Record<EndpointKind, string> = {
  llm: "对话模型 LLM",
  t2i: "文生图 T2I",
  t2v: "文生视频 T2V",
  i2v: "图生视频 I2V",
  tts: "语音 TTS",
};
/** 端点可用的调用协议（不含 local：本地推理不走端点） */
export const ENDPOINT_PROVIDER_OPTIONS = ["openai", "ark", "agnes", "custom", "comfyui", "vidu"] as const;
export type EndpointProvider = (typeof ENDPOINT_PROVIDER_OPTIONS)[number];

/** 各协议的服务地址与模型填写提示 */
export const ENDPOINT_PROVIDER_HINTS: Record<EndpointProvider, { base_url: string; model: string }> = {
  openai: {
    base_url: "https://api.example.com/v1",
    model: "OpenAI 兼容端点的模型名",
  },
  ark: { base_url: "https://ark.cn-beijing.volces.com/api/v3", model: "如 doubao-seedream-4-0-250828" },
  agnes: {
    base_url: "https://apihub.agnes-ai.com/v1",
    model: "如 agnes-2.5-flash / agnes-image-2.5-flash / agnes-video-2.5-flash",
  },
  custom: { base_url: "https://your-endpoint.example.com", model: "自定义端点模型名（可空）" },
  comfyui: {
    base_url: "http://127.0.0.1:8000",
    model: "内置工作流模板名（留空用 minimax_h3_dmad_r2v）",
  },
  vidu: {
    base_url: "https://api.vidu.cn",
    model: "如 viduq4-preview（Vidu 图生视频，需首帧图）",
  },
};

/** 协议 × 模型类型限制（与后端 provider_store 的 _KIND_PROVIDER_RESTRICTIONS 同源）：
 * comfyui 目前只有参考生视频工作流，只支持视频类端点；
 * vidu 只接图生视频（首帧必填），同样只支持视频类端点（t2v 放行是为了在
 * 故事板「视频模型」下拉里能选到它——那个下拉按 t2v 过滤） */
export const ENDPOINT_PROVIDER_KINDS: Partial<Record<EndpointProvider, readonly EndpointKind[]>> = {
  comfyui: ["t2v", "i2v"],
  vidu: ["t2v", "i2v"],
};

/** API Key（api_key 为脱敏值，仅前 4 后 4 位） */
export interface KeyInfo {
  id: string;
  name: string;
  api_key: string;
  remark: string;
  /** 引用该 Key 的端点名列表 */
  used_by: string[];
  created_at: number;
  updated_at: number;
}

/** 模型服务端点（一条 = 类型 + 协议 + 地址 + Key + 默认模型 + 计费费率） */
export interface EndpointInfo {
  id: string;
  name: string;
  kind: EndpointKind;
  provider: EndpointProvider;
  base_url: string;
  key_id: string | null;
  key_name?: string;
  key_masked?: string;
  model: string;
  /** 产物是否自带音轨（画布视频节点徽标用）：true/false = 已知，null/undefined = 未知
   * （未知不显示断言；端点侧由后端按协议判定，见 providers.provider_audio_capability） */
  audio?: boolean | null;
  /** 支持多模态（批次 3.5，仅 llm 类）：视觉审片判级准入开关 */
  multimodal?: boolean;
  /** 计费费率（33 号方案 2026-10-09）：每 1000 tokens 积分数（0=不计此项） */
  credits_per_1k_tokens: number;
  /** 每张图积分数 */
  credits_per_image: number;
  /** 每秒视频积分数 */
  credits_per_video_second: number;
  /** 计费开关（关 = 该服务的全部调用不计费） */
  bill_enabled: boolean;
  created_at: number;
}

/** 端点探测的模型列表响应（source: 协议名=远端列表 | curated=精选表 | none） */
export interface ProbeModelsResult {
  models: { id: string; display_name: string }[];
  source: string;
  error?: string;
}

/** 每类任务的默认端点（值为 null = 未设置） */
export type GlobalDefaults = Partial<
  Record<EndpointKind, { endpoint_id: string; endpoint_name: string; provider: string; model: string } | null>
>;

/** 默认配置保存入参：kind → 端点 ID（null = 清除该类） */
export type GlobalDefaultsBody = Partial<Record<EndpointKind, string | null>>;

/** Key 保存入参（api_key 编辑时留空 = 不修改） */
export interface KeySaveBody {
  name: string;
  api_key?: string;
  remark?: string;
}

/** 端点保存入参（key_id: null = 解绑；缺省 = 不修改） */
export interface EndpointSaveBody {
  name?: string;
  kind?: EndpointKind;
  provider?: EndpointProvider;
  base_url?: string;
  key_id?: string | null;
  model?: string;
  /** 支持多模态（批次 3.5，仅 llm 类有意义）：视觉审片判级准入开关 */
  multimodal?: boolean;
  /** 计费费率（33 号方案）：提供即更新；0 = 该类消耗不计费 */
  credits_per_1k_tokens?: number;
  credits_per_image?: number;
  credits_per_video_second?: number;
  /** 计费开关（关 = 该服务不计费） */
  bill_enabled?: boolean;
}

/** 模型探测入参（**只传直填的 api_key**）。
 *
 * 刻意不再支持 key_id：后端已不接受「用服务端已保存的密钥去请求调用方指定
 * 的 URL」（那等于把明文密钥交给任何能调这个接口的人，2026-09-30 安全整改）。
 * 探测接口同时挂了管理员门禁；普通用户拿不到实时模型列表，下拉回退到端点
 * 已保存的默认模型。
 */
export interface ProbeModelsBody {
  kind: EndpointKind;
  provider: EndpointProvider;
  base_url: string;
  api_key?: string;
}

// ---------- 模型管理 ----------

/** 模型下载进度状态 */
export interface InstallStatus {
  model_id: string;
  status: "idle" | "queued" | "downloading" | "canceling" | "canceled" | "done" | "failed";
  progress: number;
  downloaded_mb: number;
  total_mb: number;
  message: string;
  error: string;
}

/** 模型注册表条目（含安装状态与下载进度） */
export interface ModelInfo {
  id: string;
  family: string;
  task_type: string;
  display_name: string;
  quant: string;
  vram_mb: number;
  repo_id: string;
  ms_repo?: string;
  files: { name: string; size_gb: number }[];
  notes: string;
  installed: boolean;
  install_path: string;
  install: InstallStatus;
  /** 产物是否自带音轨（仅视频模型有意义）：true/false = 已知，null/undefined = 未知 */
  audio?: boolean | null;
}

/** 精简模型清单条目（GET /api/model-options；普通用户可读的模型选择数据源）。
 *  系统管理接口（/api/models、/api/endpoints、/api/defaults）均为管理员专属，
 *  这里只给下拉要用的字段，不含端点地址、密钥、服务商。 */
export interface ModelOption {
  id: string;
  display_name: string;
  task_type: string;
  installed: boolean;
  source: "local" | "endpoint";
  /** 产物是否自带音轨（画布视频节点徽标用）：true/false = 已知，null = 未知 */
  audio?: boolean | null;
}

// ---------- 积分计费（33 号方案） ----------

/** 我的积分账户（GET /api/billing/account） */
export interface CreditAccountInfo {
  user_id: string;
  balance: number;
  total_recharged: number;
  total_consumed: number;
  created_at: number;
}

/** 积分流水一行（user 视图与管理员全局视图共用） */
export interface CreditTransactionInfo {
  id: string;
  user_id: string;
  /** 管理员视图带出；用户视图为空串 */
  username: string;
  /** recharge 充值 | consume 消费 | refund 退款 | adjust 管理员调整 | bonus 初始赠送 */
  kind: string;
  /** 变动量：正 = 入账，负 = 出账 */
  amount: number;
  /** 变动后余额快照 */
  balance_after: number;
  /** pending 处理中 | consumed 计费成立 | refunded 已退款 | 空（充值类） */
  status: string;
  /** 计费端点键（如 "POST /api/tasks"；充值为空） */
  endpoint: string;
  price: number;
  project_id: string | null;
  task_id: string | null;
  /** 退款行指向的原消费流水 */
  ref_txn_id: string | null;
  note: string;
  operator_user_id: string;
  created_at: number;
}

/** 计费闸清单一行（前端「预扣 N」徽标的唯一数据源；2026-10-09 口径）。
 *
 * 预扣 = 发起时先押的积分（内置默认额，无需配置）；实际扣费按
 * 「所用模型服务端点的费率 × 真实调用」结算、多退少补——费率在「模型管理」
 * 页按服务端点配置。未列出的接口即免费（系统平台接口）。 */
export interface BillingPricingItem {
  /** 接口键（"METHOD /path/template"，与后端路由模板逐字一致） */
  key: string;
  method: string;
  path_template: string;
  /** 预扣押金（发起时先扣、完成后按实际调用多退少补） */
  precharge: number;
  label: string;
}

/** 计费全局配置 */
export interface BillingSettingsInfo {
  enabled: boolean;
  signup_grant: number;
  auto_refund: boolean;
  admin_free: boolean;
}

/** 兑换码一行（管理视图） */
export interface RedeemCodeInfo {
  id: string;
  code: string;
  credits: number;
  note: string;
  used_by_user_id: string | null;
  used_by_username: string;
  used_at: number | null;
  created_by: string;
  created_at: number;
}

/** 积分流水类型的中文标签（「我的积分」页与计费管理页共用；
 *  与后端 TXN_* 常量一一对应——业务字符串单点定义） */
export const CREDIT_KIND_LABEL: Record<string, string> = {
  recharge: "充值",
  consume: "消费",
  refund: "退款",
  adjust: "调整",
  bonus: "赠送",
};

/** 用户积分概览（管理视图：账号管理页余额列） */
export interface BillingAccountRow {
  user_id: string;
  username: string;
  is_admin: boolean;
  balance: number;
  total_recharged: number;
  total_consumed: number;
}

// ---------- 认证令牌（Bearer；存储与 401 全局处理） ----------

/** 登录令牌的 localStorage 键 */
const TOKEN_STORAGE_KEY = "avs_auth_token";

/** 读取本地保存的登录令牌（未登录为空串） */
export function getStoredToken(): string {
  return localStorage.getItem(TOKEN_STORAGE_KEY) ?? "";
}

/** 写入/清除本地登录令牌（空串 = 清除） */
export function setStoredToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_STORAGE_KEY, token);
  else localStorage.removeItem(TOKEN_STORAGE_KEY);
}

/** 401 全局处理器（auth store 注册：清会话并跳登录页） */
let unauthorizedHandler: (() => void) | null = null;

/** 注册 401 全局处理器（登录接口自身的 401 不触发，避免错误密码误登出） */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

/** 余额不足（402）的结构化信息（后端契约：code=insufficient_credits） */
export interface InsufficientCreditsInfo {
  /** 本次操作需要的积分 */
  required: number;
  /** 当前余额 */
  balance: number;
  /** 服务端给的中文提示（含「请充值后重试」） */
  detail: string;
}

/** 402 全局处理器（billing store 注册：弹充值引导）。
 *
 * 与 401 的无参 handler 不同：402 **必须保留结构化数据**（需要/现有积分），
 * 因为引导弹窗要展示它们——不能像 401 一样只留一个字符串。 */
let insufficientCreditsHandler: ((info: InsufficientCreditsInfo) => void) | null =
  null;

/** 注册/清除 402 全局处理器（传 null 清除） */
export function setInsufficientCreditsHandler(
  fn: ((info: InsufficientCreditsInfo) => void) | null,
): void {
  insufficientCreditsHandler = fn;
}

/** 解析 402 响应体文本为结构化信息（非本契约返回 null）。
 *  收字符串而不是 Response：响应体只能读一次，调用方先读文本再解析 ——
 *  评审修正：旧实现先在这里消费 body，回退路径再让 errorMessage(resp)
 *  读第二次会拿到空串，非本契约的 402 真实错误信息就丢了。 */
function parseInsufficientCredits(text: string): InsufficientCreditsInfo | null {
  try {
    const data = JSON.parse(text) as Record<string, unknown>;
    if (data && data.code === "insufficient_credits") {
      return {
        required: Number(data.required) || 0,
        balance: Number(data.balance) || 0,
        detail: String(data.detail || ""),
      };
    }
  } catch {
    /* 非 JSON：走通用错误 */
  }
  return null;
}

/** 提取错误响应里的人类可读信息（FastAPI 约定 {"detail": "..."}；422 校验错误为数组） */
async function errorMessage(resp: Response): Promise<string> {
  const text = await resp.text().catch(() => "");
  try {
    const detail = (JSON.parse(text) as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail) {
      return detail;
    }
    if (Array.isArray(detail)) {
      // pydantic 422 校验错误项：{loc, msg, type}
      const msgs = detail
        .map((d) => (typeof d === "object" && d !== null
          ? String((d as { msg?: string }).msg ?? JSON.stringify(d))
          : String(d)));
      if (msgs.length) return msgs.join("；");
    }
  } catch {
    // 非 JSON 响应体，走下方原文回退
  }
  return text.slice(0, 200) || `请求失败（HTTP ${resp.status}）`;
}

/** 默认请求超时（毫秒）。
 *
 * 为什么必须有（2026-09-30 修「白屏」根因之一）：`fetch` 默认**永不超时**——
 * 后端卡住（线程池耗尽、DB 锁等待）时，`/api/auth/me` 会一直挂着，而 App 的
 * 启动门禁是 `if (!ready) return null`，用户看到的就是一片空白底色，刷新也没用。
 */
const DEFAULT_TIMEOUT_MS = 30_000;
/** 长耗时接口单独放宽（解析/审核/合成等任务提交或长查询超过 30s 属正常） */
const LONG_TIMEOUT_MS = 300_000;
/** 需要放宽超时的路径前缀（提交类与重计算类） */
const LONG_PATHS = [
  "/api/drama/projects/", "/api/tasks", "/api/edits", "/api/frames",
  "/api/models/install", "/api/skills/",
  // 以下三个前缀里含**同步调 LLM** 的接口（提示词 AI 优化 / 剧本扩写 / 场景补视角
  // 重写 / 画面描述重算）——长提示词下模型要跑 30 秒以上，用默认 30 秒超时会出现
  // 「后端算成功、前端报请求超时」（2026-09-30 用户报障「提示词优化成功却报超时」：
  // 白名单里写的是老前缀 /api/drama/projects/，与实际的 /api/projects/… 不匹配）
  "/api/projects/", "/api/scenes/", "/api/shots/",
];

function timeoutFor(path: string): number {
  return LONG_PATHS.some((p) => path.startsWith(p))
    ? LONG_TIMEOUT_MS
    : DEFAULT_TIMEOUT_MS;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {};
  if (!(init?.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  // 携带登录令牌（未登录为空，后端按匿名处理）
  const token = getStoredToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  // 超时中止：调用方显式传了 signal 就用它的，否则自己造一个可中止的
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutFor(path));
  let resp: Response;
  try {
    resp = await fetch(path, {
      headers,
      ...init,
      signal: init?.signal ?? controller.signal,
    });
  } catch (e) {
    // AbortError 分两种：外部主动取消（原样抛）与超时（给可读提示）
    if (e instanceof DOMException && e.name === "AbortError") {
      if (init?.signal?.aborted) throw e;
      throw new Error(
        `请求超时（${Math.round(timeoutFor(path) / 1000)} 秒未响应）：${path}`);
    }
    throw e;
  } finally {
    window.clearTimeout(timer);
  }
  if (!resp.ok) {
    // 会话失效全局处理：清 token 并跳登录页（登录接口除外——那是密码错误的 401）
    if (resp.status === 401 && unauthorizedHandler
        && !path.startsWith("/api/auth/login")) {
      unauthorizedHandler();
    }
    // 余额不足（402）：结构化数据交给全局处理器弹充值引导，再按普通错误抛出
    // （调用方的 catch/toast 行为不变；handler 未注册时退化为普通错误）。
    // 响应体只读一次：先取文本再解析——非本契约的 402 用原文兜底（不能
    // 再走 errorMessage 二次读 body）。
    if (resp.status === 402) {
      const text = await resp.text().catch(() => "");
      const info = parseInsufficientCredits(text);
      if (info) {
        insufficientCreditsHandler?.(info);
        throw new Error(info.detail || "积分余额不足，请充值后重试");
      }
      throw new Error(text.slice(0, 200) || `请求失败（HTTP ${resp.status}）`);
    }
    throw new Error(await errorMessage(resp));
  }
  return resp.json() as Promise<T>;
}

export const api = {
  // 项目
  listProjects: () => request<ProjectInfo[]>("/api/projects"),
  /** 单个项目详情（路由化刷新后按 ID 恢复项目元信息） */
  getProject: (projectId: string) =>
    request<ProjectInfo>(`/api/projects/${projectId}`),
  /** 新建项目（描述/分辨率可空走后端默认） */
  createProject: (body: {
    name: string;
    mode: string;
    style_preset: string;
    style_anchor: string;
    aspect_ratio: string;
    video_resolution: string;
    country: string;
    description: string;
    model_presets: ModelPresetMap;
  }) =>
    request<ProjectInfo>("/api/projects", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** 更新项目设置（模型预设整体替换；country/description 提供时替换） */
  updateProjectSettings: (
    projectId: string,
    modelPresets: ModelPresetMap,
    country?: string,
    /** 视频画幅比例（含 adaptive 自适应）；不传则不改 */
    aspectRatio?: string,
    /** 视频分辨率档（480p/720p/1080p）；不传则不改 */
    videoResolution?: string,
    /** 项目描述；不传则不改 */
    description?: string,
    /** 风格锚点（美学第二级）；不传则不改，传空串=清除锚点 */
    styleAnchor?: string,
    /** 首帧提示词精炼开关（23 号批次 3）；不传则不改 */
    refineFirstFrame?: boolean,
    /** 组内环境锁定（32 号场景组）；不传则不改 */
    sceneGroupLock?: boolean,
  ) =>
    request<ProjectInfo>(`/api/projects/${projectId}`, {
      method: "PATCH",
      body: JSON.stringify({
        model_presets: modelPresets,
        ...(country !== undefined ? { country } : {}),
        ...(aspectRatio !== undefined ? { aspect_ratio: aspectRatio } : {}),
        ...(videoResolution !== undefined
          ? { video_resolution: videoResolution }
          : {}),
        ...(description !== undefined ? { description } : {}),
        ...(styleAnchor !== undefined ? { style_anchor: styleAnchor } : {}),
        ...(refineFirstFrame !== undefined
          ? { refine_first_frame: refineFirstFrame }
          : {}),
        ...(sceneGroupLock !== undefined
          ? { scene_group_lock: sceneGroupLock }
          : {}),
      }),
    }),
  /** AI 优化提示词（20 号批次 3）：服务端组装上下文并做确定性校验（引用保护/名单/画风词）。
   *  纯计算不落库——写回由用户在弹窗点「应用」后走既有更新接口。 */
  optimizePrompt: (
    projectId: string,
    body: {
      /** character（角色定妆）| costume（角色妆造）| scene（场景主图）| prop（道具图）| shot（分镜视频） */
      target: string;
      /** 对象 id */
      id: string;
      /** 用户的修改需求（自然语言） */
      instruction: string;
      /** 编辑中的文本（可空）：仅用于「对象尚无持久提示词」的场景（妆造建议草稿） */
      current_prompt?: string;
    },
  ) =>
    request<{
      target: string;
      prompt: string;
      notes: string;
      metrics: {
        length: number;
        ban_count: number;
        redundancy_rate: number;
        constraint_ratio: number;
        suspect_truncation: boolean;
        limit: number;
        over: number;
      };
      /** 本次实际注入的上下文清单（弹窗折叠区展示，用户可核对） */
      context_digest: string[];
      /** 确定性校验告警（引用丢失/名单外/画风词/超长）——必须展示给用户 */
      warnings: string[];
    }>(`/api/projects/${projectId}/prompts/optimize`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** 风格锚点选项表（后端为单一真源，前端不硬编码锚点列表） */
  listStyleAnchors: () =>
    request<{
      anchors: {
        key: string;
        label: string;
        prompt: string;
        form: string;
        hint: string;
      }[];
    }>("/api/projects/style-anchors"),
  /** 删除项目（级联删除画布/任务/会话/短剧数据，资产库产物保留） */
  deleteProject: (projectId: string) =>
    request<{ ok: boolean }>(`/api/projects/${projectId}`, { method: "DELETE" }),
  getCanvas: (projectId: string) =>
    request<{ graph: Record<string, unknown> }>(`/api/projects/${projectId}/canvas`),
  saveCanvas: (projectId: string, graph: Record<string, unknown>,
               keepalive = false) =>
    request<{ ok: boolean }>(`/api/projects/${projectId}/canvas`, {
      method: "PUT",
      body: JSON.stringify({ graph }),
      // keepalive：页面正在关闭（pagehide）时让请求在页面销毁后仍有机会发出
      // （2026-09-30 复查 F2 残留）。超过浏览器 64KB 上限时该请求会被拒——
      // 调用方兜住异常即可（与「不保存」等价，不倒退）。
      keepalive,
    }),

  // 项目资产库（M2 资产三方联动）
  listAssets: (projectId: string, kind?: string, q?: string) => {
    const params = new URLSearchParams();
    if (kind) params.set("kind", kind);
    if (q) params.set("q", q);
    const qs = params.toString();
    return request<ProjectAssetInfo[]>(
      `/api/projects/${projectId}/assets${qs ? `?${qs}` : ""}`,
    );
  },
  /** 孤儿产物清单（25 号 2.5：重建分集后不再被引用的已产出——看得见、能下载） */
  orphanVersions: (projectId: string, limit?: number) =>
    request<{
      ok: boolean;
      total: number;
      bytes: number;
      by_kind: { kind: string; count: number; bytes: number }[];
      items: OrphanVersionInfo[];
    }>(`/api/projects/${projectId}/orphan-versions${limit ? `?limit=${limit}` : ""}`),
  /** 找回产物：为节点挂载已有产物版本（删除节点后重建） */
  restoreAsset: (projectId: string, nodeId: string, artifactId: string) =>
    request<NodeVersionInfo>(`/api/projects/${projectId}/nodes/${nodeId}/restore`, {
      method: "POST",
      body: JSON.stringify({ artifact_id: artifactId }),
    }),
  /** 应用图片产物为角色形象（4.9 P0） */
  applyCharacterImage: (characterId: string, artifactId: string) =>
    request<{ ok: boolean }>(`/api/characters/${characterId}/apply-image`, {
      method: "POST",
      body: JSON.stringify({ artifact_id: artifactId }),
    }),
  /** 手工新建场景（解析之外补充资产，名称必填） */
  createScene: (
    projectId: string,
    body: { name: string; description?: string; image_prompt?: string },
  ) =>
    request<SceneInfo>(`/api/projects/${projectId}/scenes`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** 应用图片产物为场景主图（4.9 P0） */
  applySceneImage: (sceneId: string, artifactId: string) =>
    request<{ ok: boolean }>(`/api/scenes/${sceneId}/apply-image`, {
      method: "POST",
      body: JSON.stringify({ artifact_id: artifactId }),
    }),

  // 节点版本（抽卡）
  listVersions: (projectId: string, nodeId: string) =>
    request<NodeVersionInfo[]>(`/api/nodes/${nodeId}/versions?project_id=${projectId}`),
  /** 删除单个抽卡版本（产物保留资产库可找回） */
  deleteNodeVersion: (projectId: string, nodeId: string, versionId: string) =>
    request<{ ok: boolean }>(
      `/api/nodes/${nodeId}/versions/${versionId}?project_id=${projectId}`,
      { method: "DELETE" },
    ),
  /** 版本命名（三卡通用；空名 = 恢复默认命名「版本N」） */
  renameNodeVersion: (projectId: string, nodeId: string, versionId: string, name: string) =>
    request<NodeVersionInfo>(
      `/api/nodes/${nodeId}/versions/${versionId}?project_id=${projectId}`,
      { method: "PATCH", body: JSON.stringify({ name }) },
    ),
  uploadVersion: (projectId: string, nodeId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<NodeVersionInfo>(`/api/nodes/${nodeId}/upload?project_id=${projectId}`, {
      method: "POST",
      body: form,
    });
  },
  /** 视频抽帧（4.9 P0）：从节点最新视频版本抽帧，追加为该节点 edited 版本 */
  extractFrame: (
    nodeId: string,
    projectId: string,
    position: "first" | "last" | "current",
    timeSec?: number,
  ) =>
    request<NodeVersionInfo>(`/api/nodes/${nodeId}/extract-frame?project_id=${projectId}`, {
      method: "POST",
      body: JSON.stringify(
        timeSec !== undefined ? { position, time_sec: timeSec } : { position },
      ),
    }),

  // 节点编辑工具（M2 4.9 P1）：编辑产物均追加为 edited 版本
  /** 图片旋转 90 度（cw 顺时针 / ccw 逆时针） */
  rotateImage: (nodeId: string, projectId: string, direction: "cw" | "ccw") =>
    request<NodeVersionInfo>(`/api/nodes/${nodeId}/rotate?project_id=${projectId}`, {
      method: "POST",
      body: JSON.stringify({ direction }),
    }),
  /** 图片按比例居中裁剪（1:1 / 4:3 / 3:4 / 16:9 / 9:16） */
  cropImage: (nodeId: string, projectId: string, ratio: string) =>
    request<NodeVersionInfo>(`/api/nodes/${nodeId}/crop?project_id=${projectId}`, {
      method: "POST",
      body: JSON.stringify({ ratio }),
    }),
  /** 视频时间区间裁取（秒） */
  trimVideo: (nodeId: string, projectId: string, start: number, end: number) =>
    request<NodeVersionInfo>(`/api/nodes/${nodeId}/trim?project_id=${projectId}`, {
      method: "POST",
      body: JSON.stringify({ start, end }),
    }),
  /** 多选拼接：产物拼接后追加为 destNodeId 新版本 */
  concatAssets: (
    projectId: string,
    kind: "image" | "video",
    artifactIds: string[],
    destNodeId: string,
  ) =>
    request<NodeVersionInfo>(`/api/projects/${projectId}/concat`, {
      method: "POST",
      body: JSON.stringify({ kind, artifact_ids: artifactIds, dest_node_id: destNodeId }),
    }),

  // 运镜库
  cameraMoves: () => request<CameraMove[]>("/api/camera-moves"),

  // 任务
  createTask: (body: {
    project_id: string;
    node_id: string;
    runner: string;
    model_id?: string;
    params?: Record<string, unknown>;
    input_refs?: Record<string, unknown>;
  }) =>
    request<{ task_id: string; status: string; cached: boolean }>("/api/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listTasks: (projectId?: string, batchId?: string) => {
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    if (batchId) params.set("batch_id", batchId);
    const qs = params.toString();
    return request<TaskInfo[]>(`/api/tasks${qs ? `?${qs}` : ""}`);
  },
  getTask: (taskId: string) => request<TaskInfo>(`/api/tasks/${taskId}`),
  cancelTask: (taskId: string) =>
    request<{ ok: boolean }>(`/api/tasks/${taskId}/cancel`, { method: "POST" }),

  // Agent 会话（M2）
  listSessions: (projectId: string) =>
    request<SessionInfo[]>(`/api/projects/${projectId}/sessions`),
  createSession: (projectId: string, title?: string) =>
    request<SessionInfo>(`/api/projects/${projectId}/sessions`, {
      method: "POST",
      body: JSON.stringify(title ? { title } : {}),
    }),
  updateSession: (
    sessionId: string,
    body: { title?: string; status?: "active" | "closed"; pane_side?: "left" | "right" },
  ) =>
    request<SessionInfo>(`/api/sessions/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  listMessages: (sessionId: string) =>
    request<ChatMessage[]>(`/api/sessions/${sessionId}/messages`),
  sendChat: (sessionId: string, body: ChatSendBody) =>
    request<ChatResponse>(`/api/sessions/${sessionId}/chat`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // 用户体系（登录 / 账号管理）
  login: (username: string, password: string) =>
    request<LoginResult>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () =>
    request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  me: () => request<AuthUserInfo>("/api/auth/me"),
  /** 批量签发媒体访问链接（方案 A）：产物 id / files 相对路径 */
  signMedia: (body: { artifact_ids?: string[]; files?: string[] }) =>
    request<{
      ttl: number;
      expires_at: number;
      artifacts: Record<string, string>;
      files: Record<string, string>;
    }>("/api/artifacts/sign", {
      method: "POST",
      body: JSON.stringify({
        artifact_ids: body.artifact_ids ?? [],
        files: body.files ?? [],
      }),
    }),
  /** 修改自己的密码（成功后其他端登录态被吊销） */
  changePassword: (oldPassword: string, newPassword: string) =>
    request<{ ok: boolean }>("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  // 账号管理（仅管理员）
  listUsers: () => request<AuthUserInfo[]>("/api/auth/users"),
  createUser: (username: string, password: string, isAdmin: boolean) =>
    request<AuthUserInfo>("/api/auth/users", {
      method: "POST",
      body: JSON.stringify({ username, password, is_admin: isAdmin }),
    }),
  updateUser: (
    userId: string,
    body: { password?: string; is_admin?: boolean; disabled?: boolean },
  ) =>
    request<AuthUserInfo>(`/api/auth/users/${userId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteUser: (userId: string) =>
    request<{ ok: boolean }>(`/api/auth/users/${userId}`, { method: "DELETE" }),

  // 积分计费（33 号方案）：用户侧
  /** 我的积分账户（余额/累计充值/累计消费） */
  myCredits: () => request<CreditAccountInfo>("/api/billing/account"),
  /** 我的积分明细（分页；kind 可过滤） */
  myCreditTransactions: (opts?: { offset?: number; limit?: number; kind?: string }) => {
    const params = new URLSearchParams();
    if (opts?.offset) params.set("offset", String(opts.offset));
    if (opts?.limit) params.set("limit", String(opts.limit));
    if (opts?.kind) params.set("kind", opts.kind);
    const qs = params.toString();
    return request<{
      ok: boolean; total: number; offset: number; limit: number;
      items: CreditTransactionInfo[];
    }>(`/api/billing/transactions${qs ? `?${qs}` : ""}`);
  },
  /** 收费端点价目（前端「消耗 N 积分」标注的唯一数据源；未列出的端点即免费） */
  billingPricing: () =>
    request<{ ok: boolean; rules: BillingPricingItem[] }>("/api/billing/pricing"),
  /** 兑换码充值（自助） */
  redeemCreditCode: (code: string) =>
    request<{ ok: boolean; credits: number; balance: number }>(
      "/api/billing/redeem",
      { method: "POST", body: JSON.stringify({ code }) },
    ),

  // 积分计费：管理侧（仅管理员）
  billingSettings: () =>
    request<{ ok: boolean; settings: BillingSettingsInfo }>(
      "/api/billing/settings"),
  updateBillingSettings: (patch: Partial<BillingSettingsInfo>) =>
    request<{ ok: boolean; settings: BillingSettingsInfo }>(
      "/api/billing/settings",
      { method: "PATCH", body: JSON.stringify(patch) },
    ),
  /** 全部用户积分概览（账号管理页余额列） */
  billingAccounts: () =>
    request<{ ok: boolean; accounts: BillingAccountRow[] }>(
      "/api/billing/accounts"),
  /** 管理员充值 */
  adminRecharge: (body: { user_id: string; amount: number; note?: string }) =>
    request<{ ok: boolean; user_id: string; balance: number; delta: number }>(
      "/api/billing/recharge",
      { method: "POST", body: JSON.stringify(body) },
    ),
  /** 管理员调整（amount 可负） */
  adminAdjustCredit: (body: { user_id: string; amount: number; note?: string }) =>
    request<{ ok: boolean; user_id: string; balance: number; delta: number }>(
      "/api/billing/adjust",
      { method: "POST", body: JSON.stringify(body) },
    ),
  /** 批量生成兑换码（返回码清单，仅此一次便于复制） */
  createRedeemCodes: (body: { credits: number; count: number; note?: string }) =>
    request<{ ok: boolean; codes: string[] }>("/api/billing/redeem-codes", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** 兑换码列表（管理） */
  listRedeemCodes: (opts?: { status?: "unused" | "used"; offset?: number; limit?: number }) => {
    const params = new URLSearchParams();
    if (opts?.status) params.set("status", opts.status);
    if (opts?.offset) params.set("offset", String(opts.offset));
    if (opts?.limit) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return request<{
      ok: boolean; total: number; offset: number; limit: number;
      items: RedeemCodeInfo[];
    }>(`/api/billing/redeem-codes${qs ? `?${qs}` : ""}`);
  },
  /** 全局流水（管理审计；可按用户/类型过滤） */
  allCreditTransactions: (opts?: { user_id?: string; kind?: string; offset?: number; limit?: number }) => {
    const params = new URLSearchParams();
    if (opts?.user_id) params.set("user_id", opts.user_id);
    if (opts?.kind) params.set("kind", opts.kind);
    if (opts?.offset) params.set("offset", String(opts.offset));
    if (opts?.limit) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return request<{
      ok: boolean; total: number; offset: number; limit: number;
      items: CreditTransactionInfo[];
    }>(`/api/billing/transactions/all${qs ? `?${qs}` : ""}`);
  },
  /** 手动对账（balance == 流水汇总；不平的账户原样返回） */
  billingAudit: () =>
    request<{ ok: boolean; checked: number; bad: { user_id: string; balance: number; ledger_sum: number }[] }>(
      "/api/billing/audit", { method: "POST" }),

  // 技能包（系统管理·技能包页签，仅管理员）
  listSkills: () => request<SkillPackMeta[]>("/api/skills"),
  getSkill: (file: string) =>
    request<SkillPackDetail>(`/api/skills/${file}`),
  /** 保存技能包全文（后端快照上一版，写盘即时热生效） */
  saveSkill: (file: string, content: string) =>
    request<{ ok: boolean }>(`/api/skills/${file}`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),
  /** 新建技能包（写入占位模板，创建后在线编辑） */
  createSkill: (file: string) =>
    request<{ ok: boolean; file: string }>("/api/skills", {
      method: "POST",
      body: JSON.stringify({ file }),
    }),
  deleteSkill: (file: string) =>
    request<{ ok: boolean }>(`/api/skills/${file}`, { method: "DELETE" }),
  restoreSkill: (file: string) =>
    request<{ ok: boolean }>(`/api/skills/${file}/restore`, { method: "POST" }),

  // 系统
  gpuInfo: () =>
    request<{
      available: boolean;
      device_name: string;
      total_vram_mb: number;
      inference_ready: boolean;
      notes: string[];
    }>("/api/system/gpu"),
  systemInfo: () => request<{ mock_mode: boolean }>("/api/system/info"),

  // 模型管理（下载/卸载/依赖）
  /** 精简模型清单（**普通用户可读**；模型选择器专用）。
   *  系统管理接口全部管理员专属后，普通用户下拉的数据一律走这里。 */
  modelOptions: () =>
    request<{
      models: ModelOption[];
      defaults: Partial<Record<EndpointKind, { label?: string }>>;
    }>("/api/model-options"),
  // 模型 ID 含斜杠（如 minimax-h3/fl2va-int8），一律用查询参数传递避免路径解析歧义
  listModels: () => request<ModelInfo[]>("/api/models"),
  /** 第三方服务可用模型列表（ark 等；source: ark|openai|curated|none） */
  listProviderModels: (kind: string) =>
    request<{ kind: string; models: { id: string; display_name: string }[]; source: string }>(
      `/api/providers/models?kind=${kind}`,
    ),
  installModel: (modelId: string) =>
    request<InstallStatus>(`/api/models/install?model_id=${encodeURIComponent(modelId)}`, {
      method: "POST",
    }),
  installStatus: (modelId: string) =>
    request<InstallStatus>(`/api/models/install?model_id=${encodeURIComponent(modelId)}`),
  cancelModel: (modelId: string) =>
    request<{ ok: boolean }>(`/api/models/cancel?model_id=${encodeURIComponent(modelId)}`, {
      method: "POST",
    }),
  uninstallModel: (modelId: string) =>
    request<{ ok: boolean }>(`/api/models/uninstall?model_id=${encodeURIComponent(modelId)}`, {
      method: "POST",
    }),
  depsStatus: () =>
    request<{ missing: string[]; install: { status: string; log: string; error: string } }>(
      "/api/system/deps",
    ),
  installDeps: () =>
    request<{ status: string; log: string; error: string }>("/api/system/deps/install", {
      method: "POST",
    }),

  // 推理服务配置（.env 兼容回退层，前端设置页已改用端点管理）
  listProviders: () =>
    request<Record<string, ProviderInfo>>("/api/providers"),
  updateProvider: (kind: string, body: ProviderUpdateBody) =>
    request<ProviderUpdateResult>("/api/providers", {
      method: "PUT",
      body: JSON.stringify({ kind, ...body }),
    }),

  // 模型服务管理（Key 管理 / 模型服务端点 / 默认配置）
  listKeys: () => request<KeyInfo[]>("/api/keys"),
  createKey: (body: KeySaveBody) =>
    request<{ id: string; name: string }>("/api/keys", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateKey: (keyId: string, body: KeySaveBody) =>
    request<{ ok: boolean }>(`/api/keys/${keyId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  /** 删除 Key：引用它的端点自动解绑，unbound 为受影响端点名列表 */
  deleteKey: (keyId: string) =>
    request<{ ok: boolean; unbound: string[] }>(`/api/keys/${keyId}`, {
      method: "DELETE",
    }),
  listEndpoints: (kind?: EndpointKind) => {
    const qs = kind ? `?kind=${kind}` : "";
    return request<EndpointInfo[]>(`/api/endpoints${qs}`);
  },
  createEndpoint: (body: EndpointSaveBody) =>
    request<{ id: string; name: string }>("/api/endpoints", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateEndpoint: (endpointId: string, body: EndpointSaveBody) =>
    request<{ ok: boolean }>(`/api/endpoints/${endpointId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteEndpoint: (endpointId: string) =>
    request<{ ok: boolean }>(`/api/endpoints/${endpointId}`, { method: "DELETE" }),
  /** 探测端点远端模型列表（仅管理员；只接受直填 api_key） */
  probeEndpointModels: (body: ProbeModelsBody) =>
    request<ProbeModelsResult>("/api/endpoints/probe-models", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** 一键给各模型服务端点填入建议费率（33 号方案；overwrite=false 只填
   *  未配置的端点）。费率=积分计费的配置对象：每千 token / 每张图 /
   *  每秒视频各多少积分。 */
  applySuggestedEndpointPrices: (overwrite = false) =>
    request<{ ok: boolean; updated: number }>(
      "/api/endpoints/apply-suggested-prices",
      { method: "POST", body: JSON.stringify({ overwrite }) },
    ),
  getDefaults: () => request<{ defaults: GlobalDefaults }>("/api/defaults"),
  updateDefaults: (defaults: GlobalDefaultsBody) =>
    request<{ defaults: GlobalDefaults }>("/api/defaults", {
      method: "PUT",
      body: JSON.stringify({ defaults }),
    }),

  // 短剧&漫剧（M3）
  /**
   * 剧本解析：提交长任务（node_id=script_parse）立即返回，
   * 进度/结果经 WS task_update/task_progress 推送（store.handleTaskEvent 处理）
   */
  /** 剧本解析：提交长任务（node_id=script_parse）立即返回；keepVersion=先把当前
   * 剧本原文存为历史版本 */
  parseScript: (
    projectId: string,
    text: string,
    keepVersion = false,
    /** AI 解析前置需求（21 号批次 5，可空 = 沿用上次/默认值） */
    requirements?: ParseRequirements,
  ) =>
    request<{ task_id: string; status: string }>(`/api/projects/${projectId}/script/parse`, {
      method: "POST",
      body: JSON.stringify({
        text, keep_version: keepVersion,
        ...(requirements ? { requirements } : {}),
      }),
    }),
  /** 读取项目的 AI 解析前置需求（重解析对话框回填；含选项表，后端为单一真源） */
  getScriptRequirements: (projectId: string) =>
    request<{
      requirements: ParseRequirements;
      options: {
        tones: { value: string; label: string }[];
        episode_minutes: number[];
        audiences: { value: string; label: string }[];
      };
    }>(`/api/projects/${projectId}/script/requirements`),
  /** 历史解析版本列表 */
  listScriptVersions: (projectId: string) =>
    request<ScriptVersionInfo[]>(`/api/projects/${projectId}/script/versions`),
  /** 恢复历史剧本原文（写回编辑器原文存档：确认后重新解析即可） */
  restoreScriptVersion: (versionId: string) =>
    request<{ ok: boolean; project_id: string; text: string; chars: number }>(
      `/api/script/versions/${versionId}/restore`, {
      method: "POST",
    }),
  /** 一句话梗概扩写为标准剧本（LLM 优先，规则降级）；mode: llm|rule|passthrough */
  expandScript: (projectId: string, text: string) =>
    request<{ text: string; mode: string }>(`/api/projects/${projectId}/script/expand`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  /** 剧本优化：按审核问题清单外科手术式修复缺陷，返回修复后全文供确认 */
  /** 提交剧本优化长任务（按终审问题清单修复；与解析共用观测窗，可停止） */
  // ---- 逐集流水线（18 号 §52/§53/§54：每集独立成任务） ----
  /** 单集剧情审核（带上一集/下一集上下文；结果写 Episode.audit_json） */
  auditEpisode: (episodeId: string) =>
    request<{ task_id: string; status: string }>(
      `/api/episodes/${episodeId}/audit`, { method: "POST" }),
  /** 单集一键修复（补丁协议定点修复，**不复审**；issues 空 = 修全部 critical） */
  optimizeEpisode: (episodeId: string, issues: AuditIssue[]) =>
    request<{ task_id: string; status: string }>(
      `/api/episodes/${episodeId}/optimize`,
      { method: "POST", body: JSON.stringify({ issues }) }),
  /** 单集制作（拆解 → 立即落库 → 画面描述 → 回填） */
  buildEpisode: (episodeId: string) =>
    request<{ task_id: string; status: string }>(
      `/api/episodes/${episodeId}/build`, { method: "POST" }),
  /** 本集资产解析（把本集正文里的角色/场景/道具对到项目资产，只补新增；
   * node_id = `ep_assets_{集id}`，进度/轨迹/终态与审核修复制作同一条链路） */
  extractEpisodeAssets: (episodeId: string) =>
    request<{ task_id: string; status: string }>(
      `/api/episodes/${episodeId}/assets`, { method: "POST" }),
  /** 取当集正文（大字段按需查，不进分集列表） */
  getEpisodeScript: (episodeId: string) =>
    request<{ id: string; seq: number; title: string; body: string;
              char_start: number; char_end: number }>(
      `/api/episodes/${episodeId}/script`),
  /** 编辑分集计划（集名/梗概/当集正文——确定性写入，不触发重新分集） */
  patchEpisode: (episodeId: string,
                 body: { title?: string; synopsis?: string; body?: string }) =>
    request<EpisodeInfo>(`/api/episodes/${episodeId}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  /** 重新分集（两段式：confirm=false 先预检将重建几集、丢弃多少分镜） */
  replanEpisodes: (projectId: string, confirm: boolean) =>
    request<{ task_id?: string; status?: string;
              episodes?: number; shots?: number; chars?: number }>(
      `/api/projects/${projectId}/episodes/replan`,
      { method: "POST", body: JSON.stringify({ confirm }) }),

  getScript: (projectId: string) =>
    request<ScriptData>(`/api/projects/${projectId}/script`),
  // ---- 提示词在线编辑（系统配置·提示词页签） ----
  listPrompts: () =>
    request<{ ok: boolean; prompts: PromptMeta[] }>("/api/prompts"),
  getPrompt: (name: string) =>
    request<PromptDetail>(`/api/prompts/${name}`),
  savePrompt: (name: string, content: string) =>
    request<{ ok: boolean }>(`/api/prompts/${name}`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),
  restorePrompt: (name: string) =>
    request<{ ok: boolean }>(`/api/prompts/${name}/restore`, {
      method: "POST",
    }),
  /** 上传剧本文档（txt/md/docx/pdf）提取纯文本，填回编辑器确认后解析 */
  extractScriptDoc: (projectId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<ScriptExtractResult>(`/api/projects/${projectId}/script/extract`, {
      method: "POST",
      body: form,
    });
  },
  createCharacter: (
    projectId: string,
    body: { name: string; role?: string; persona?: string; appearance?: string; voice_desc?: string },
  ) =>
    request<CharacterInfo>(`/api/projects/${projectId}/characters`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateCharacter: (
    characterId: string,
    body: { name?: string; role?: string; persona?: string; appearance?: string; voice_desc?: string },
  ) =>
    request<CharacterInfo>(`/api/characters/${characterId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  generateCharacterImage: (
    characterId: string,
    body: { kind?: string; name?: string; appearance_desc?: string; model_id?: string; episodes?: number[];
      /** 抽卡目标形象记录（非空 = 不新建记录，在该形象下追加一个抽卡版本） */
      regenerate_image_id?: string },
  ) =>
    request<{ task_id: string; version_id: string | null; image_id: string; node_id: string }>(`/api/characters/${characterId}/images`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** 把形象记录指定版本设为生效图（version_id 空 = 回退跟随最新） */
  preferCharacterImage: (characterId: string, imageId: string, versionId: string | null) =>
    request<{ ok: boolean }>(
      `/api/characters/${characterId}/images/${imageId}/prefer`,
      { method: "POST", body: JSON.stringify({ version_id: versionId }) },
    ),
  /** 生成角色四视图：单张**横版**图内**一行四列**（正面/左45°/背面/右45°，以基础形象为参考图） */
  generateFourView: (characterId: string, body: { model_id?: string; regenerate_image_id?: string }) =>
    request<{ task_id: string; version_id: string | null; image_id: string; node_id: string }>(
      `/api/characters/${characterId}/images/fourview`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),
  /** 取消角色定妆图生成（排队即取消；生成中在步级回调处协作中止） */
  cancelCharacterImages: (characterId: string) =>
    request<{ ok: boolean; canceled: number }>(
      `/api/characters/${characterId}/images/cancel`,
      { method: "POST" },
    ),
  /** 删除角色形象（定妆图/妆造/四视图）：产物保留在资产库可找回 */
  deleteCharacterImage: (characterId: string, imageId: string) =>
    request<{ ok: boolean }>(
      `/api/characters/${characterId}/images/${imageId}`,
      { method: "DELETE" },
    ),
  /** 删除角色资产（两段式：confirm=false 预检映射不删除；true 删除并清除映射） */
  deleteCharacter: (characterId: string, confirm?: boolean) =>
    request<AssetDeleteResponse>(
      `/api/characters/${characterId}?confirm=${confirm ? "true" : "false"}`,
      { method: "DELETE" },
    ),
  /** 上传本地图片作为角色基础形象（新建/无形象角色的补图入口，落 uploaded 版本） */
  uploadCharacterImage: (characterId: string, file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<{ ok: boolean; image_id: string; artifact_id: string; version_id: string }>(
      `/api/characters/${characterId}/images/upload`,
      { method: "POST", body },
    );
  },
  /** 删除场景主图：场景回到未生成态（产物保留在资产库可找回） */
  deleteSceneImage: (sceneId: string) =>
    request<{ ok: boolean }>(`/api/scenes/${sceneId}/image`, {
      method: "DELETE",
    }),
  /** 删除场景资产（两段式：confirm=false 预检映射不删除；true 删除并清除映射） */
  deleteScene: (sceneId: string, confirm?: boolean) =>
    request<AssetDeleteResponse>(
      `/api/scenes/${sceneId}?confirm=${confirm ? "true" : "false"}`,
      { method: "DELETE" },
    ),
  /** 把场景图指定版本设为生效主图（version_id 空 = 回退跟随最新） */
  preferSceneImage: (sceneId: string, versionId: string | null) =>
    request<{ ok: boolean }>(`/api/scenes/${sceneId}/image/prefer`, {
      method: "POST",
      body: JSON.stringify({ version_id: versionId }),
    }),
  /** 妆造建议清单（解析提取的换装事件，按角色分组供角色卡确认） */
  listCostumeSuggestions: (projectId: string) =>
    request<CostumeSuggestionInfo[]>(
      `/api/projects/${projectId}/costume-suggestions`,
    ),
  /** 接受妆造建议：按建议生成妆造形象（可微调名称/描述/集数/模型） */
  acceptCostumeSuggestion: (
    projectId: string,
    suggestionId: string,
    body: {
      character_id: string;
      name?: string;
      appearance_desc?: string;
      episodes?: number[];
      model_id?: string;
    },
  ) =>
    request<{ task_id: string; episodes: number[] }>(
      `/api/projects/${projectId}/costume-suggestions/${suggestionId}/accept`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  /** 拒绝妆造建议 */
  rejectCostumeSuggestion: (projectId: string, suggestionId: string) =>
    request<{ ok: boolean }>(
      `/api/projects/${projectId}/costume-suggestions/${suggestionId}/reject`,
      { method: "POST" },
    ),
  /** 上传角色音色参考音频（生成视频时作配音音色参考，重复上传覆盖） */
  uploadVoiceRef: (characterId: string, file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<{ ok: boolean; asset_id: string }>(
      `/api/characters/${characterId}/voice-ref`,
      { method: "POST", body },
    );
  },
  /** 移除角色音色参考音频（仅清除引用） */
  deleteVoiceRef: (characterId: string) =>
    request<{ ok: boolean }>(`/api/characters/${characterId}/voice-ref`, {
      method: "DELETE",
    }),
  /** 妆造→分镜批量映射（6.4 第 2 条）：形象写入引用该角色的分镜并回写适用集数 */
  applyCostume: (characterId: string, body: { image_id: string; episode_ids?: string[] }) =>
    request<{ ok: boolean; updated: number; episodes: number[] }>(
      `/api/characters/${characterId}/costume/apply`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  listCharacters: (projectId: string) =>
    request<CharacterInfo[]>(`/api/projects/${projectId}/characters`),
  updateScene: (sceneId: string, body: { name?: string; description?: string; image_prompt?: string }) =>
    request<SceneInfo>(`/api/scenes/${sceneId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  /** 生成场景主图。返回 `prompt`＝**本次实际发送的完整提示词**（组装结果已写回
   *  服务端字段；前端据此同步提示词框——界面所见即所发，2026-09-30 用户规则）。 */
  generateScene: (sceneId: string, modelId?: string, count?: number) =>
    request<{ task_id: string; prompt: string; group_anchor_artifact?: string }>(
      `/api/scenes/${sceneId}/generate`, {
        method: "POST",
        body: JSON.stringify({ model_id: modelId ?? "", count: count ?? 1 }),
      }),
  /** 场景组详情（32 号，界面「场景组」弹窗）：当前组 + 同组成员（各带产物供选母版） */
  getSceneGroup: (sceneId: string) =>
    request<{
      ok: boolean;
      group: {
        id: string; name: string; env_setting: string;
        anchor_asset_id: string; anchor_artifact: string;
      } | null;
      members: { scene_id: string; name: string; artifact_id: string }[];
    }>(`/api/scenes/${sceneId}/scene-group`),
  /** 多选式编组（32 号交互优化）：勾一批场景一次保存——name 非空 = 全部归入
   *  该组（同名并入/新建），空 = 把勾选的移出所在组；anchor_asset_id 不传 = 不改、
   *  空串 = 回「自动」、id = 指定母版（须为勾选场景的已有产物）。返回 removed =
   *  本次被移出组的场景数（用于「无变化」提示）。 */
  applySceneGroup: (body: {
    project_id: string;
    scene_ids: string[];
    name: string;
    env_setting: string;
    anchor_asset_id?: string;
  }) =>
    request<{ ok: boolean; removed: number }>(`/api/scene-groups/apply`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** 场景补充视角（20 号批次 4）：反向视角 / 关键交互区特写，**以场景主图为
   *  I2I 参考**补全（官方口径：全景图是空间母版，后续视角优先由母版补全）。
   *  无主图时后端报错——不降级为纯文生图（会丢掉空间约束）。 */
  generateSceneExtraView: (
    sceneId: string,
    view: "reverse" | "interaction",
    modelId?: string,
  ) =>
    request<{ task_id: string; view: string }>(`/api/scenes/${sceneId}/extra-view`, {
      method: "POST",
      body: JSON.stringify({ view, model_id: modelId ?? "" }),
    }),
  /** 道具定妆图生成（白底物件图，跨镜头外观一致性锚点） */
  /** 生成道具定妆图。返回的 `prompt` 同上（组装结果写回字段，前端同步提示词框）。 */
  generateProp: (propId: string, modelId?: string) =>
    request<{ task_id: string; prompt: string }>(`/api/props/${propId}/generate`, {
      method: "POST",
      body: JSON.stringify({ model_id: modelId ?? "" }),
    }),
  /** 编辑道具名称/描述/生图提示词（与场景 PATCH 同模式） */
  updateProp: (propId: string, body: { name?: string; description?: string; image_prompt?: string }) =>
    request<{ ok: boolean }>(`/api/props/${propId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  /** 把道具图指定版本设为生效图（version_id 空 = 回退跟随最新） */
  preferPropImage: (propId: string, versionId: string | null) =>
    request<{ ok: boolean }>(`/api/props/${propId}/image/prefer`, {
      method: "POST",
      body: JSON.stringify({ version_id: versionId }),
    }),
  /** 取消道具图生成 */
  cancelProp: (propId: string) =>
    request<{ ok: boolean; canceled: number }>(`/api/props/${propId}/cancel`, {
      method: "POST",
    }),
  /** 删除道具资产（两段式：confirm=false 预检映射不删除；true 删除并清除映射） */
  deleteProp: (propId: string, confirm?: boolean) =>
    request<AssetDeleteResponse>(
      `/api/props/${propId}?confirm=${confirm ? "true" : "false"}`,
      { method: "DELETE" },
    ),
  /** 取消场景图生成 */
  cancelScene: (sceneId: string) =>
    request<{ ok: boolean; canceled: number }>(`/api/scenes/${sceneId}/cancel`, {
      method: "POST",
    }),
  listScenes: (projectId: string) =>
    request<SceneInfo[]>(`/api/projects/${projectId}/scenes`),
  /** 道具列表（故事板资产栏/提及候选数据源） */
  listProps: (projectId: string) =>
    request<PropInfo[]>(`/api/projects/${projectId}/props`),
  /** 手工新建道具（解析之外补充资产，与场景/角色手工新建同模式） */
  createProp: (projectId: string, body: { name: string; description?: string; image_prompt?: string }) =>
    request<PropInfo>(`/api/projects/${projectId}/props`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** 单镜生成参数排查（kind=ff 首帧 / v 视频；返回该节点最新任务的完整参数） */
  getGenerationParams: (shotId: string, kind: "ff" | "v") =>
    request<{ ok: boolean; task: GenerationParamsTask | null }>(
      `/api/shots/${shotId}/generation-params?kind=${kind}`,
    ),
  /** 分镜体检报告（解析期存档全量，白话说明直出） */
  getStoryboardQa: (episodeId: string) =>
    request<{ ok: boolean; items: QaIssueInfo[] }>(
      `/api/episodes/${episodeId}/storyboard-qa`,
    ),
  /** 自学习全局洞察：系统性弱项 + 提示词改进建议 */
  /** 模型调用日志（系统管理·调用日志页；仅管理员，支持分页） */
  listModelCalls: (
    opts?: {
      days?: number;
      limit?: number;
      /** 分页偏移（「加载更多」按已显示的条数递增） */
      offset?: number;
      onlyFailed?: boolean;
      q?: string;
      /** 自定义日期区间（YYYY-MM-DD，含结束当天；填了就忽略 days） */
      start?: string;
      end?: string;
    },
  ) => {
    const params = new URLSearchParams();
    // days=0 是有效取值（「全部」档，不限时间）——不能用真值判断
    if (opts?.days !== undefined) params.set("days", String(opts.days));
    if (opts?.limit) params.set("limit", String(opts.limit));
    if (opts?.offset) params.set("offset", String(opts.offset));
    if (opts?.onlyFailed) params.set("only_failed", "true");
    if (opts?.q) params.set("q", opts.q);
    if (opts?.start) params.set("start", opts.start);
    if (opts?.end) params.set("end", opts.end);
    const qs = params.toString();
    return request<{
      ok: boolean;
      window: { since: number; until: number; label: string };
      /** 当前过滤条件下的总条数（分页进度用） */
      total: number;
      offset: number;
      limit: number;
      items: ModelCallInfo[];
    }>(`/api/model-calls${qs ? `?${qs}` : ""}`);
  },
  /** 单次调用详情（含接口入参/响应留痕；仅管理员） */
  getModelCall: (callId: string) =>
    request<{ ok: boolean; call: ModelCallDetail }>(`/api/model-calls/${callId}`),
  /** 模型调用看板（按调用点/端点/模型聚合 + 按天趋势；仅管理员） */
  modelCallStats: (opts?: { days?: number; start?: string; end?: string }) => {
    const params = new URLSearchParams();
    params.set("days", String(opts?.days ?? 7));
    if (opts?.start) params.set("start", opts.start);
    if (opts?.end) params.set("end", opts.end);
    return request<ModelCallStats>(`/api/model-calls/stats?${params.toString()}`);
  },
  getLessonsInsights: () =>
    request<{ ok: boolean; patterns: LessonsPattern[]; suggestions: LessonsSuggestion[] }>(
      `/api/lessons-insights`,
    ),
  /** 应用改进建议（追加到目标提示词，快照兜底可恢复） */
  applyLessonsSuggestion: (rule: string) =>
    request<{ ok: boolean; already_applied?: boolean; target?: string }>(
      `/api/lessons-insights/apply`,
      { method: "POST", body: JSON.stringify({ rule }) },
    ),
  listEpisodes: (projectId: string) =>
    request<EpisodeInfo[]>(`/api/projects/${projectId}/episodes`),
  listShots: (episodeId: string) =>
    request<ShotInfo[]>(`/api/episodes/${episodeId}/shots`),
  addShot: (
    episodeId: string,
    body: { description: string; scene_ref_id?: string | null; duration?: number },
  ) =>
    request<ShotInfo>(`/api/episodes/${episodeId}/shots`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateShot: (shotId: string, body: ShotPatchBody) =>
    // duration 回传：手工编辑被服务端钳位时以此为准同步显示；
    // duration_note = 被自动抬升的原因（语速下限）；video_prompt/timing_ok =
    // 改时长后服务端**确定性重排时间小节**的结果（框里所见即实际发送的那份）
    request<{
      ok: boolean; duration?: number; duration_note?: string;
      video_prompt?: string; timing_ok?: boolean;
    }>(`/api/shots/${shotId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  generateShots: (
    episodeId: string,
    mode: "first_frames" | "videos" | "all",
    modelId?: string,
    /** 定向生成子集（审片重拍只重生成指定分镜；空 = 全部） */
    shotIds?: string[],
    /** 批次 3 §34：单变量记录/重拍备注/预算止损与体检硬门逃生口；
     *  25 号 2.4：`reason` 是**重摇原因**——提交含已有产出的镜时后端必填；
     *  `count` = 首帧一次抽几张（1-4，多张各落一个版本，可在卡片上挑） */
    opts?: {
      changed?: string;
      note?: string;
      reason?: string;
      force?: boolean;
      ignore_storyboard_qa?: boolean;
      count?: number;
    },
  ) =>
    request<{
      batch_id: string;
      task_ids: string[];
      skipped: { seq: number; reason: string }[];
      /** 出首帧前**自动补过人物朝向锚点**的镜号（后端在组装首帧提示词之前修的） */
      anchor_fixed?: number[];
    }>(
      `/api/episodes/${episodeId}/shots/generate`,
      {
        method: "POST",
        body: JSON.stringify({
          mode,
          model_id: modelId ?? "",
          shot_ids: shotIds ?? null,
          changed: opts?.changed ?? "seed",
          note: opts?.note ?? "",
          reason: opts?.reason ?? "",
          force: opts?.force ?? false,
          ignore_storyboard_qa: opts?.ignore_storyboard_qa ?? false,
          count: opts?.count ?? 1,
        }),
      },
    ),
  /** 设为首帧（2026-09-29）：在「一次抽 N 张」的候选里挑定这张；空 = 跟随最新 */
  preferFirstFrame: (shotId: string, versionId?: string) =>
    request<{ ok: boolean; version_id: string | null }>(
      `/api/shots/${shotId}/prefer-first-frame`,
      { method: "POST", body: JSON.stringify({ version_id: versionId ?? "" }) },
    ),
  /** 镜头五级裁决（批次 3 §34.1：keep 回写观测态） */
  reviewShot: (
    shotId: string,
    body: {
      verdict: "keep" | "post" | "edit" | "reroll" | "rewrite";
      evidence?: string;
      changed?: string;
      /** 重摇原因（25 号 2.4）：裁决是记录不是提交，故此处可选 */
      reason?: string;
    },
  ) =>
    request<{ ok: boolean; take_seq: number; observed: Record<string, unknown> }>(
      `/api/shots/${shotId}/verdict`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  /** 单镜裁决日志（Take N · changed · verdict · evidence） */
  shotReviews: (shotId: string) =>
    request<{ ok: boolean; reviews: ShotReviewInfo[] }>(`/api/shots/${shotId}/reviews`),
  /** 重摇原因分布（25 号 2.4：质量类重摇按原因聚合——「靶子在哪」的唯一答案） */
  rerollReasons: (projectId: string) =>
    request<{
      ok: boolean;
      quality_rerolls: number;
      unlabeled: number;
      label_rate: number;
      /** 只作参照、不进填写率：裁决意向 / AI 判级 / 旧版本记录 */
      other_origins: { verdict?: number; auto?: number; legacy?: number };
      by_reason: { reason: string; count: number }[];
      by_changed: { changed: string; count: number }[];
      samples: {
        shot_id: string;
        take_seq: number;
        verdict: string;
        changed: string;
        reason: string;
        evidence: string;
        created_at: number;
      }[];
    }>(`/api/projects/${projectId}/reroll-reasons`),
  /** 整集裁决日志（镜头日志时间线） */
  episodeReviews: (episodeId: string) =>
    request<{ ok: boolean; reviews: (ShotReviewInfo & { shot_id: string })[] }>(
      `/api/episodes/${episodeId}/reviews`,
    ),
  /** 视觉自动判级（批次 3.5：抽帧拼图 + 多模态 LLM 五级裁决，reviewer=auto） */
  autoReviewEpisode: (episodeId: string) =>
    request<{
      ok: boolean;
      results: { shot_id: string; seq: number; verdict: string; evidence?: string; error?: string }[];
      auto_count: number;
    }>(`/api/episodes/${episodeId}/auto-review`, { method: "POST" }),
  /** 生成清单预览（铁律 2 轻量版：数量/时长/模型/风险清单，确认后再提交） */
  generatePreview: (episodeId: string) =>
    request<{
      ok: boolean;
      shots: number;
      total_duration: number;
      model: string;
      missing_characters: string[];
      unresolved_error_count: number;
      unresolved_sample: string[];
      qa_errors: number;
      /** 端点健康警示（25 号 1.5：欠费/队列满——提交前先摆出来） */
      provider_warnings?: string[];
    }>(`/api/episodes/${episodeId}/generate-preview`),
  /** 成片预检报告（确定性检查不改数据：资产完备/段间衔接/景别层次/台词覆盖/轴线/时序/时长分布/模型建议） */
  precheckEpisode: (episodeId: string) =>
    request<{
      ok: boolean;
      summary: { shots: number; total_duration: number; avg_duration: number; short_shots: number };
      issues: { level: "error" | "warn" | "info"; category: string; seq: number | null; message: string }[];
      model_hints: { seq: number; hint: string }[];
    }>(`/api/episodes/${episodeId}/precheck`),
  /** 确定性审片（视频时长偏差/纯色黑帧/首帧缺失；重拍走 generateShots 定向子集） */
  reviewEpisode: (episodeId: string) =>
    request<{
      ok: boolean;
      results: {
        shot_id: string;
        seq: number;
        has_video: boolean;
        has_first_frame: boolean;
        video_duration?: number;
        expected_duration?: number;
        duration_ok?: boolean;
        solid_frames?: number;
        frame_ok?: boolean;
        probe_failed?: boolean;
      }[];
      failed_shot_ids: string[];
    }>(`/api/episodes/${episodeId}/review`),
  /** 单镜画面描述重算（两段式契约）：
   *  - 不传参：生成并落库（体检项一键修复走这条）
   *  - `dryRun`：只生成不落库，返回结果供预览
   *  - `prompt` 非空：直接应用这段文本（预览后确认/改过的），不再调 LLM */
  regenerateShotPrompt: (
    shotId: string,
    opts?: {
      dryRun?: boolean;
      prompt?: string;
      /** 姿态改写（取景语言版 position）：与 prompt 同源的两段式回传——
       *  应用路径不调 LLM，不回传就等于改写丢了 */
      statePosition?: string;
    },
  ) =>
    request<{
      ok: boolean;
      video_prompt: string;
      timing_ok: boolean;
      dry_run?: boolean;
      /** dry_run 预览返回的改写后 position（空 = 本镜无需改写） */
      state_position?: string;
    }>(`/api/shots/${shotId}/regenerate-video-prompt`, {
      method: "POST",
      body: JSON.stringify({
        dry_run: opts?.dryRun ?? false,
        prompt: opts?.prompt ?? "",
        state_position: opts?.statePosition ?? "",
      }),
    }),
  composeEpisode: (episodeId: string) =>
    request<{ task_id: string }>(`/api/episodes/${episodeId}/compose`, { method: "POST" }),
  /** 多集连发合成（6.6 第 6 条）：按传入顺序逐集排队，无视频分集跳过 */
  composeEpisodesBatch: (projectId: string, episodeIds: string[]) =>
    request<{ task_ids: string[]; skipped: number }>("/api/episodes/compose-batch", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId, episode_ids: episodeIds }),
    }),
  /** 全链首尾帧对齐（6.6 第 4 条）：分镜 n 尾帧 → 分镜 n+1 首帧版本（幂等，重复执行跳过已对齐对） */
  linkFrames: (episodeId: string) =>
    request<{
      ok: boolean;
      /** 新对齐的相邻对数 */
      linked: number;
      /** 幂等跳过数（源视频未变，对齐版本已存在） */
      duplicates: number;
      /** 前一分镜无视频的跳过数 */
      skipped: number;
      /** 抽到 mock 占位帧的分镜 seq（ffmpeg 不可用/抽帧失败） */
      fallback_seqs: number[];
      /** 首帧已更新但视频仍是旧首帧生成的分镜 seq（需重新生成视频生效） */
      regen_seqs: number[];
    }>(`/api/episodes/${episodeId}/link-frames`, { method: "POST" }),
};

/** 订阅项目任务事件（进度/状态）。断线自动重连，重连成功触发 reconnected 事件供调用方全量刷新兜底。 */
export function subscribeTasks(
  projectId: string,
  onEvent: (event: string, data: Record<string, unknown>) => void,
): () => void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let ws: WebSocket | null = null;
  let closed = false;
  let wasOpen = false;
  let retryTimer: number | undefined;

  const connect = () => {
    // 令牌走 query：浏览器 WebSocket 构造器不能自定义请求头，后端在握手时校验
    // 「令牌有效 + 该项目归当前用户」（2026-09-30 第 1 层）
    const token = getStoredToken();
    const qs = new URLSearchParams({ project_id: projectId });
    if (token) qs.set("token", token);
    ws = new WebSocket(`${proto}://${location.host}/ws/tasks?${qs.toString()}`);
    ws.onopen = () => {
      // 连接建立（含首次）：通知连接状态；断线重连时额外触发全量刷新兜底
      onEvent("connected", {});
      if (wasOpen) onEvent("reconnected", {});
      wasOpen = true;
    };
    ws.onmessage = (msg) => {
      try {
        const payload = JSON.parse(msg.data as string);
        if (payload.event && payload.event !== "ping") {
          onEvent(payload.event, payload.data);
        }
      } catch {
        /* 忽略非 JSON 消息 */
      }
    };
    ws.onclose = () => {
      if (closed) return;
      onEvent("disconnected", {});
      retryTimer = window.setTimeout(connect, 3000);
    };
  };
  connect();
  return () => {
    closed = true;
    if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    ws?.close();
  };
}

/** 产物访问地址（**签名优先**）。
 *
 * 方案 A（2026-09-30）：产物文件不能用登录令牌保护（媒体标签带不了
 * Authorization 头），改走签名临时链接。签名由 fileUrls.ts 缓存并在需要时
 * 申请；拿不到签名时回退未签名的旧地址（严格模式未开时行为与改造前一致）。
 *
 * 与 fileUrls.ts 是**循环依赖**（它需要本模块的 api），但两边都只在**函数体
 * 内**互相引用、不在模块顶层使用对方的值，所以 ESM 提升后调用期解析正常。
 */
export function fileUrl(artifactId: string): string {
  const signed = cachedSignedUrl(artifactId);
  if (signed) return signed;
  void primeSignedUrls([artifactId]);
  return `/api/artifacts/${artifactId}/file`;
}

/** /files 下相对路径的访问地址（签名优先；同上） */
export function rawFileUrl(relPath: string): string {
  const rel = (relPath || "").replace(/\\/g, "/").replace(/^\/+/, "");
  const signed = cachedSignedFileUrl(rel);
  if (signed) return signed;
  void primeSignedUrls([], [rel]);
  return `/files/${rel}`;
}
