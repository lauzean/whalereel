/** 短剧工作台状态：剧本解析/角色/场景/分集/分镜 + 任务事件联动（M3 6.1-6.5）。 */

import { create } from "zustand";
import { useBilling } from "./billing";
import { toast } from "../ui/feedback";
import {
  api,
  type AssetDeleteResponse,
  type AuditIssue,
  type CharacterInfo,
  type CostumeSuggestionInfo,
  type EpisodeInfo,
  type ModelPresetMap,
  type ParseRequirements,
  type PropInfo,
  type SceneInfo,
  type ScriptData,
  type ScriptVersionInfo,
  type ShotInfo,
  type ShotPatchBody,
  type TaskTrace,
} from "../api/client";

/** 批量任务聚合进度（同一 batch_id 的任务由 WS 事件驱动更新，供故事板顶栏聚合条） */
export interface BatchProgress {
  batchId: string;
  /** 批次任务总数 */
  total: number;
  /** taskId → 最新状态（task_update 带 batch_id/状态/错误，task_progress 只有 task_id） */
  tasks: Record<string, { status: string; progress: number; node_id: string; error: string }>;
}

interface DramaState {
  projectId: string | null;
  /** 剧本原文（前端编辑态，解析入参） */
  scriptText: string;
  /** 解析结果（标题/梗概/角色/场景/分集） */
  script: ScriptData | null;
  characters: CharacterInfo[];
  scenes: SceneInfo[];
  /** 道具资产（故事板资产栏/提及候选数据源） */
  props: PropInfo[];
  episodes: EpisodeInfo[];
  /** episodeId -> 分镜列表 */
  shots: Record<string, ShotInfo[]>;
  activeEpisodeId: string | null;
  /** 生成进度映射：char_{cid} / scene_{sid} / shot_{镜id} → { status, progress }（任务事件驱动，终态清除） */
  genProgress: Record<string, { status: string; progress: number }>;
  /** 当前追踪的生成批次（批量首帧/视频提交后设置，顶栏聚合进度条展示） */
  batch: BatchProgress | null;
  /** 实时通道状态（2026-09-30 复查 F5：短剧工作台此前不显示 WS 断线——
   *  画布侧有状态点与重连补拉，这边断线后事件全丢却悄无声息） */
  wsConnected: boolean;
  /** 妆造建议清单（解析提取的换装事件，角色卡确认后生成） */
  costumeSuggestions: CostumeSuggestionInfo[];
  /** 分镜任务失败原因（shotId → error；该集重新提交生成时清除） */
  shotErrors: Record<string, string>;
  parsing: boolean;
  /** 解析长任务进度（0-1）与阶段说明（WS task_progress 实时驱动） */
  parseProgress: { progress: number; label: string };
  parseTaskId: string | null;
  /** 项目生成模型预设（t2i/t2v，来自项目设置）——资产卡片/故事板模型选择器的默认对齐源 */
  modelPresets: ModelPresetMap | null;
  /** 同步项目模型预设（项目设置保存/项目加载时调用） */
  setModelPresets: (presets: ModelPresetMap | null) => void;
  /** 更新实时通道状态（顶栏状态点用） */
  setWsConnected: (v: boolean) => void;
  /** 解析过程观测（持久化 + WS task_trace 实时驱动）：进度步骤 + 思考轮次 */
  parseTrace: TaskTrace;
  /** 当前流式思考块（WS stream_begin/delta/end 实时驱动，不落库） */
  parseLive: { title: string; text: string } | null;
  /** 用户主动停止了本轮解析（停止后旧解析数据保留） */
  parseStopped: boolean;
  /** 当前逐集任务（审核/修复/制作/资产解析/重新分集）：左栏徽标与按钮忙碌态的数据源 */
  epTask: {
    episodeId: string;
    /** audit | optimize | build | assets | replan */
    kind: string;
    status: string;
    progress: number;
    label: string;
  } | null;
  /** 逐集任务 id（停止按钮用） */
  epTaskId: string | null;
  /** 最近一次解析失败（观测窗头部显示失败态） */
  parseFailed: boolean;
  /** 历史剧本原文版本（只存原文，恢复＝回填编辑器后重新解析） */
  scriptVersions: ScriptVersionInfo[];
  error: string;

  loadAll: (projectId: string) => Promise<void>;
  loadCharacters: () => Promise<void>;
  loadScenes: () => Promise<void>;
  loadProps: () => Promise<void>;
  loadEpisodes: () => Promise<void>;
  loadShots: (episodeId: string) => Promise<void>;
  setActiveEpisode: (episodeId: string) => void;
  /** 历史剧本原文版本（v1.1 降级：只存原文，恢复 = 回填编辑器） */
  loadScriptVersions: () => Promise<void>;
  restoreScriptVersion: (versionId: string)
    => Promise<{ ok: boolean; text: string; chars: number }>;

  setScriptText: (text: string) => void;
  /**
   * 提交剧本解析长任务（立即返回 task_id）：进度/思考经 WS 推送，
   * 由 handleTaskEvent 更新 parsing/parseProgress/parseTrace 并在完成时刷新数据。
   * keepVersion：已有解析结果时先快照为历史版本。
   * 提交失败返回 undefined（原因由 error 展示）。
   */
  parseScript: (keepVersion?: boolean, requirements?: ParseRequirements)
    => Promise<{ task_id: string } | undefined>;
  /** 停止本轮解析：取消任务并恢复到之前的解析结果（旧数据在解析成功前不被覆盖） */
  stopParseScript: () => Promise<void>;
  // ---- 逐集流水线（18 号 §52/§53/§54：每集独立成任务，共用观测窗） ----
  /** 单集剧情审核（带上一集/下一集上下文；结果写 Episode.audit_json） */
  auditEpisode: (episodeId: string) => Promise<{ task_id: string } | undefined>;
  /** 单集一键修复（补丁协议定点修复，不复审；issues 空 = 修全部 critical） */
  optimizeEpisode: (episodeId: string, issues: AuditIssue[])
    => Promise<{ task_id: string } | undefined>;
  /** 单集制作（拆解 → 立即落库 → 画面描述 → 回填） */
  buildEpisode: (episodeId: string) => Promise<{ task_id: string } | undefined>;
  /** 本集资产解析（逐集补新增资产，与审核/修复/制作共用观测窗与进度链路） */
  extractEpisodeAssets: (episodeId: string) => Promise<{ task_id: string } | undefined>;
  /** 停止当前逐集任务 */
  stopEpisodeTask: () => Promise<void>;
  /** 重新分集（LLM 按剧情/爆点/钩子重切；confirm=false 先预检） */
  replanEpisodes: (confirm: boolean)
    => Promise<{ episodes?: number; shots?: number } | undefined>;
  /** 编辑分集计划（集名/梗概/当集正文） */
  patchEpisode: (episodeId: string,
                 body: { title?: string; synopsis?: string; body?: string })
    => Promise<void>;
  /** 一句话梗概扩写为标准剧本，返回文本供确认（6.2 第 3 条） */
  expandScript: (text: string) => Promise<{ text: string; mode: string }>;

  /** 手工新建角色（解析之外补充资产），返回创建后的角色（供新建卡继续上传图片） */
  createCharacter: (body: {
    name: string;
    persona?: string;
    appearance?: string;
    voice_desc?: string;
  }) => Promise<CharacterInfo | undefined>;
  updateCharacter: (
    characterId: string,
    body: { name?: string; role?: string; persona?: string; appearance?: string; voice_desc?: string; image_prompt?: string },
  ) => Promise<void>;
  /** 生成角色形象图：默认 basic 基础形象；opts 可指定妆造/三视图与生图模型；
   * regenerateImageId 非空 = 抽卡模式（不新建形象记录，在该形象下追加版本） */
  generateCharacterImage: (
    characterId: string,
    opts?: { kind?: string; name?: string; appearance_desc?: string; model_id?: string; episodes?: number[]; regenerateImageId?: string },
  ) => Promise<void>;
  /** 删除角色形象（定妆图/妆造/四视图，产物保留在资产库可找回） */
  deleteCharacterImage: (characterId: string, imageId: string) => Promise<void>;
  /** 删除角色资产（两段式：confirm=false 预检映射；true 删除并清映射，产物保留资产库） */
  deleteCharacter: (characterId: string, confirm: boolean) => Promise<AssetDeleteResponse>;
  /** 删除场景主图（场景回到未生成态，产物保留在资产库可找回） */
  deleteSceneImage: (sceneId: string) => Promise<void>;
  /** 删除场景资产（两段式：confirm=false 预检映射；true 删除并清引用，产物保留资产库） */
  deleteScene: (sceneId: string, confirm: boolean) => Promise<AssetDeleteResponse>;
  /** 删除场景图的单个版本（版本级删除，产物保留在资产库可找回） */
  deleteSceneImageVersion: (sceneId: string, versionId: string) => Promise<void>;
  /** 把场景图指定版本设为生效主图（空 = 回退跟随最新版本） */
  preferSceneImage: (sceneId: string, versionId: string | null) => Promise<void>;
  /** 拉取妆造建议清单 */
  loadCostumeSuggestions: () => Promise<void>;
  /** 接受妆造建议并生成妆造形象（可微调名称/描述/集数/模型） */
  acceptCostumeSuggestion: (
    sug: CostumeSuggestionInfo,
    opts?: { name?: string; appearance_desc?: string; episodes?: number[]; model_id?: string },
  ) => Promise<void>;
  /** 拒绝妆造建议 */
  rejectCostumeSuggestion: (sug: CostumeSuggestionInfo) => Promise<void>;
  /** 生成角色四视图（正面/左45°/背面/右45° 全身照，以基础形象为参考图）；
   * regenerateImageId 非空 = 对该四视图记录抽卡（追加版本，不新建记录） */
  generateFourView: (characterId: string, modelId?: string, regenerateImageId?: string) => Promise<void>;
  /** 取消角色定妆图生成（排队即取消；生成中在步级回调处协作中止） */
  cancelCharacterImage: (characterId: string) => Promise<void>;
  /** 上传/移除角色音色参考音频（生成视频时作配音音色参考） */
  uploadVoiceRef: (characterId: string, file: File) => Promise<void>;
  deleteVoiceRef: (characterId: string) => Promise<void>;
  /** 妆造→分镜批量映射：形象写入引用该角色的分镜并回写适用集数（6.4 第 2 条） */
  applyCostume: (
    characterId: string,
    imageId: string,
    episodeIds?: string[],
  ) => Promise<{ updated: number; episodes: number[] }>;

  updateScene: (sceneId: string, body: { name?: string; description?: string; image_prompt?: string }) => Promise<void>;
  /** 场景组（32 号，多选式）：一次提交一批场景的组归属（成员集合 = 期望状态），
   *  成功后刷新场景列表；返回 removed = 本次被移出组的场景数 */
  applySceneGroup: (body: {
    project_id: string;
    scene_ids: string[];
    name: string;
    env_setting: string;
    anchor_asset_id?: string;
  }) => Promise<{ ok: boolean; removed: number }>;
  /** 手工新建场景（解析之外补充资产，与角色手工新建同模式），返回创建后的场景 */
  createScene: (body: { name: string; description?: string; image_prompt?: string }) => Promise<SceneInfo | undefined>;
  /** 生成场景主图（可指定 t2i 模型；count = 一次抽几张，1-4） */
  generateScene: (sceneId: string, modelId?: string, count?: number) => Promise<void>;
  /**
   * 生成场景补充视角（reverse 反向视角 / interaction 关键交互区特写）。
   *
   * **必须走 store**：其它生成动作都在这里打乐观标记（generating + genProgress），
   * WS 终态事件再清进度并 loadScenes()。组件内直调 API 会绕开这套机制——
   * 表现为「点击后迟迟没有生成中反馈，且完成后缩略图永不出现」（实测缺陷）。
   */
  generateSceneExtraView: (
    sceneId: string,
    view: "reverse" | "interaction",
    modelId?: string,
  ) => Promise<void>;
  /** 手工新建道具（解析之外补充资产，与场景/角色手工新建同模式），返回创建后的道具 */
  createProp: (body: { name: string; description?: string; image_prompt?: string }) => Promise<PropInfo | undefined>;
  generateProp: (propId: string, modelId?: string) => Promise<void>;
  /** 编辑道具名称/描述/生图提示词（与场景 PATCH 同模式） */
  updateProp: (propId: string, body: { name?: string; description?: string; image_prompt?: string }) => Promise<void>;
  /** 把道具图指定版本设为生效图（空 = 回退跟随最新版本） */
  preferPropImage: (propId: string, versionId: string | null) => Promise<void>;
  /** 取消道具图生成 */
  cancelProp: (propId: string) => Promise<void>;
  /** 删除道具资产（两段式：confirm=false 预检映射；true 删除并清引用，产物保留资产库） */
  deleteProp: (propId: string, confirm: boolean) => Promise<AssetDeleteResponse>;
  /** 把形象指定版本设为生效图（空 = 回退跟随最新版本） */
  preferCharacterImage: (characterId: string, imageId: string, versionId: string | null) => Promise<void>;
  /** 版本命名（三卡通用；空名 = 恢复默认命名「版本N」） */
  renameNodeVersion: (nodeId: string, versionId: string, name: string) => Promise<void>;
  /** 取消场景图生成 */
  cancelScene: (sceneId: string) => Promise<void>;

  addShot: (
    episodeId: string,
    body: { description: string; scene_ref_id?: string | null; duration?: number },
  ) => Promise<void>;
  updateShot: (shotId: string, body: ShotPatchBody) => Promise<void>;
  /** 单镜画面描述重生成（时长编辑后时序失配重算 / 手动重抽），就地更新分镜 */
  regenerateShotPrompt: (shotId: string) => Promise<void>;
  /** 预演重算结果（dry-run，不落库不改状态）：供确认框展示 */
  previewShotPrompt: (shotId: string) => Promise<{ video_prompt: string;
    timing_ok: boolean; state_position: string }>;
  /** 应用确认过的重算结果（走重算端点落库，保留体检项联动） */
  applyShotPrompt: (shotId: string, prompt: string,
                   /** 姿态改写（取景语言版 position），随预览一起回传 */
                   statePosition?: string) => Promise<void>;
  generateShots: (
    episodeId: string,
    mode: "first_frames" | "videos" | "all",
    modelId?: string,
    /** 定向生成子集（审片重拍；空 = 全部） */
    shotIds?: string[],
    /** 批次 3 §34：单变量记录/重拍备注/止损与体检硬门逃生口；
     *  25 号 2.4：`reason` = 重摇原因（重出已有产出的镜时后端必填） */
    opts?: {
      changed?: string;
      note?: string;
      reason?: string;
      force?: boolean;
      ignore_storyboard_qa?: boolean;
      /** 首帧一次抽几张（1-4） */
      count?: number;
    },
  ) => Promise<{ batch_id: string; task_ids: string[]; anchor_fixed?: number[] }>;
  /** 记录待追踪的批量任务批次（顶栏聚合进度条，WS 事件驱动更新） */
  trackBatch: (batchId: string, taskIds: string[]) => void;
  /** 关闭批次聚合进度条 */
  clearBatch: () => void;
  composeEpisode: (episodeId: string) => Promise<{ task_id: string }>;
  /** 多集连发合成：逐集排队，无视频分集跳过（6.6 第 6 条） */
  composeBatch: (episodeIds: string[]) => Promise<{ task_ids: string[]; skipped: number }>;
  /** 全链首尾帧对齐：完成后刷新分镜列表 */
  linkFrames: (episodeId: string) => Promise<{
    ok: boolean;
    linked: number;
    duplicates: number;
    skipped: number;
    fallback_seqs: number[];
    regen_seqs: number[];
  }>;

  /** 任务事件：按 node_id 前缀（char_/scene_/shot_*_ff/shot_*_v）在终态刷新对应资源 */
  handleTaskEvent: (event: string, data: Record<string, unknown>) => void;
  reset: () => void;
}

/** 任务终态（进行中状态由列表接口的 generating 字段表达，避免高频刷新） */
const FINAL_STATUS = ["done", "failed", "canceled", "cached"];

/** 任务进行中状态（恢复解析任务时使用） */
const ACTIVE_STATUSES = ["queued", "loading_model", "running"];

/** 解析长任务 node_id（与后端 NODE_ID_PARSE 一致） */
const NODE_ID_PARSE = "script_parse";

/** 重新分集长任务 node_id（与后端 NODE_ID_REPLAN 一致） */
const NODE_ID_REPLAN = "script_replan";

/** 逐集任务 node_id 前缀（与后端 NODE_ID_EP_* 一致，形如 `ep_audit_{集id}`）
 *
 * 前缀归属到「当集」：审核/修复/制作/资产解析各成一个独立任务，进度/轨迹按集展示
 * （旧链路 10 集并发拆解共用一个进度条，用户反馈"乱糟糟看不懂"）。
 */
const EP_TASK_PREFIXES = ["ep_audit_", "ep_optimize_", "ep_build_", "ep_assets_"] as const;

/** 流式思考增量的合帧间隔（毫秒）：~12 次/秒，观感无差别、渲染量降一个数量级 */
const STREAM_FLUSH_MS = 80;

export const useDrama = create<DramaState>((set, get) => {
  // ---- 流式思考增量的合帧缓冲（模块级闭包，不参与渲染） ----
  // 为什么必须合帧：SSE 每秒可推 300+ 条增量（资产分析 3 并发流 + 拆解并发流），
  // 逐条 set 会让订阅整份 store 的剧本页（含 36KB 编辑器与全部面板）每秒重渲染
  // 数百次——用户实测一次 40 分钟解析后浏览器标签崩溃。合帧后最多 ~12 次/秒，
  // 观感无差别（思考窗本来就是逐字滚动），渲染量降一个数量级。
  let streamBuf = "";
  let streamTimer: ReturnType<typeof setTimeout> | null = null;
  const flushStream = () => {
    if (streamTimer !== null) {
      clearTimeout(streamTimer);
      streamTimer = null;
    }
    if (!streamBuf) return;
    // 展示层压缩：换行/多空白折叠为单空格（在合帧后做一次，省掉每条的正则开销）
    const piece = streamBuf.replace(/\s*\n+\s*/g, " ");
    streamBuf = "";
    set((s) => {
      const live = s.parseLive ?? { title: "AI 思考中", text: "" };
      return { parseLive: { ...live, text: (live.text + piece).slice(-4000) } };
    });
  };
  const dropStreamBuf = () => {
    if (streamTimer !== null) {
      clearTimeout(streamTimer);
      streamTimer = null;
    }
    streamBuf = "";
  };

  /**
   * 提交逐集任务（审核/修复/制作共用）：清空观测窗 → 提交 → 记任务 id。
   *
   * 三类任务与解析**共用同一观测窗**（轨迹/流式思考/进度）：逐集后同时只跑
   * 一个任务，混在一起不会乱；反而把「审核 → 修复 → 制作」串成一条可读的过程。
   */
  const submitEpTask = async (
    kind: string, episodeId: string, label: string,
    submit: () => Promise<{ task_id: string; status: string }>,
  ): Promise<{ task_id: string } | undefined> => {
    dropStreamBuf();
    set({
      epTask: { episodeId, kind, status: "queued", progress: 0, label },
      epTaskId: null,
      error: "",
      parseTrace: { stages: [], gates: [], thoughts: [] },
      parseLive: null,
      parseProgress: { progress: 0, label },
    });
    try {
      const res = await submit();
      set({ epTaskId: res.task_id });
      return { task_id: res.task_id };
    } catch (e) {
      set({
        epTask: null,
        error: e instanceof Error ? e.message : String(e),
      });
      return undefined;
    }
  };

  return {
  projectId: null,
  scriptText: "",
  script: null,
  characters: [],
  scenes: [],
  props: [],
  episodes: [],
  shots: {},
  activeEpisodeId: null,
  genProgress: {},
  batch: null,
  wsConnected: false,
  shotErrors: {},
  costumeSuggestions: [],
  modelPresets: null,
  parsing: false,
  parseProgress: { progress: 0, label: "" },
  parseTaskId: null,
  parseTrace: { stages: [], thoughts: [] },
  parseLive: null,
  parseStopped: false,
  epTask: null,
  epTaskId: null,
  parseFailed: false,
  scriptVersions: [],
  error: "",

  loadAll: async (projectId) => {
    set({ projectId, error: "" });
    // 新项目未解析时接口可能 404，静默保持空态
    const script = await api.getScript(projectId).catch(() => null);
    // 回填上次解析的剧本原文（服务端持久化），刷新/重进后可直接重新解析
    set({ script, scriptText: script?.text ?? "" });
    // 解析长任务（页面刷新/重进场景）：进行中恢复解析态；已完成恢复过程轨迹
    // （trace 随任务持久化，解析完成后仍可查看完整解析过程）
    const tasks = await api.listTasks(projectId).catch(() => [] as never[]);
    const parseTasks = tasks.filter((t) => t.node_id === NODE_ID_PARSE);
    // 逐集任务在途（刷新/重进）：恢复「任务中」态与观测窗轨迹。node_id 形如
    // `ep_audit_{集id}`——从后缀还原集 id，徽标才能挂到正确的集上
    const activeEp = tasks.find(
      (t) => EP_TASK_PREFIXES.some((p) => t.node_id.startsWith(p))
        && ACTIVE_STATUSES.includes(t.status));
    if (activeEp) {
      const prefix = EP_TASK_PREFIXES.find((p) => activeEp.node_id.startsWith(p))!;
      set({
        epTask: {
          episodeId: activeEp.node_id.slice(prefix.length),
          kind: prefix.replace(/^ep_|_$/g, ""),
          status: activeEp.status,
          progress: activeEp.progress,
          label: "逐集任务进行中…",
        },
        epTaskId: activeEp.id,
        parseProgress: { progress: activeEp.progress, label: "逐集任务进行中…" },
        parseTrace: { stages: [], gates: [], thoughts: [] },
      });
      api.getTask(activeEp.id).then((t) => {
        if (get().epTaskId !== activeEp.id) return;  // 期间已换任务
        set({ parseTrace: t.trace ?? { stages: [], gates: [], thoughts: [] } });
      }).catch(() => {});
    }
    const active = parseTasks.find((t) => ACTIVE_STATUSES.includes(t.status));
    if (active) {
      set({
        parsing: true,
        parseTaskId: active.id,
        parseProgress: { progress: active.progress, label: "解析进行中…" },
        // 列表接口只给轨迹摘要（省 2s 轮询流量），完整轨迹按任务号单独取详情
        // （刷新页面恢复观测窗所需；详情接口 include_trace=true）
        parseTrace: { stages: [], thoughts: [] },
        parseStopped: active.status === "canceled",
        parseFailed: active.status === "failed",
      });
      api.getTask(active.id).then((t) => {
        if (get().parseTaskId !== active.id) return;  // 期间已换任务
        set({ parseTrace: t.trace ?? { stages: [], thoughts: [] } });
      }).catch(() => {});
    } else {
      // 已完结解析的轨迹恢复优先级：失败/停止优先展示该任务自己的过程
      // （失败可观测、停止恢复的是任务当时的部分轨迹）；其余场景回退到
      // 当前生效解析结果的归档轨迹（成功解析时落 Setting，随版本快照/
      // 恢复同步），无归档时再按最近任务恢复（兼容旧数据）
      const latest = parseTasks[0];
      const latestTrace = latest?.trace;
      const hasTaskTrace =
        (latestTrace?.stages?.length ?? 0) > 0 ||
        (latestTrace?.thoughts?.length ?? 0) > 0;
      const EMPTY_TRACE: TaskTrace = { stages: [], thoughts: [] };
      if (latest?.status === "failed") {
        set({ parseTrace: latestTrace ?? EMPTY_TRACE, parseFailed: true });
      } else if (latest?.status === "canceled" && hasTaskTrace) {
        set({ parseTrace: latestTrace ?? EMPTY_TRACE, parseStopped: true });
      } else if (
        script?.trace &&
        ((script.trace.stages?.length ?? 0) > 0 ||
          (script.trace.thoughts?.length ?? 0) > 0)
      ) {
        // 归档轨迹恒对应已成功的解析结果，无失败/停止态
        set({ parseTrace: script.trace });
      } else if (latest && hasTaskTrace) {
        // 到达此分支时状态必为 done/cached（failed/canceled 已被上方分支
        // 拦截）：无失败/停止态标记
        set({
          parseTrace: latestTrace ?? EMPTY_TRACE,
          parseStopped: false,
          parseFailed: false,
        });
      }
    }
    await Promise.all([
      get().loadCharacters().catch(() => {}),
      get().loadScenes().catch(() => {}),
      get().loadProps().catch(() => {}),
      get().loadEpisodes().catch(() => {}),
      get().loadScriptVersions().catch(() => {}),
      get().loadCostumeSuggestions().catch(() => {}),
    ]);
  },

  /** 历史解析版本列表 */
  loadScriptVersions: async () => {
    const projectId = get().projectId;
    if (!projectId) return;
    set({ scriptVersions: await api.listScriptVersions(projectId).catch(() => []) });
  },

  /** 恢复历史版本（整包重放后全量刷新操作面板） */
  restoreScriptVersion: async (versionId: string) => {
    // v1.1：版本 = 剧本原文 → 服务端写回原文存档，前端把该版填回编辑器
    const res = await api.restoreScriptVersion(versionId);
    set({ scriptText: res.text });
    return res;
  },

  loadCharacters: async () => {
    const projectId = get().projectId;
    if (!projectId) return;
    set({ characters: await api.listCharacters(projectId) });
  },

  loadScenes: async () => {
    const projectId = get().projectId;
    if (!projectId) return;
    set({ scenes: await api.listScenes(projectId) });
  },

  loadProps: async () => {
    const projectId = get().projectId;
    if (!projectId) return;
    set({ props: await api.listProps(projectId).catch(() => []) });
  },

  loadEpisodes: async () => {
    const projectId = get().projectId;
    if (!projectId) return;
    const episodes = await api.listEpisodes(projectId);
    set({ episodes, activeEpisodeId: get().activeEpisodeId ?? episodes[0]?.id ?? null });
  },

  loadShots: async (episodeId) => {
    const list = await api.listShots(episodeId);
    set({ shots: { ...get().shots, [episodeId]: list } });
  },

  setActiveEpisode: (episodeId) => set({ activeEpisodeId: episodeId }),

  setScriptText: (text) => set({ scriptText: text }),

  stopParseScript: async () => {
    const { parseTaskId } = get();
    if (!parseTaskId) return;
    try {
      await api.cancelTask(parseTaskId);
    } finally {
      // 任务引擎按取消事件落 canceled 态；旧解析数据在成功前从未被覆盖，
      // 停止即等效「恢复到之前的解析结果」——刷新操作面板与其同步
      set({ parsing: false, parseTaskId: null, parseLive: null, parseStopped: true });
      const projectId = get().projectId;
      if (projectId) {
        api.getScript(projectId).then((script) => set({ script })).catch(() => {});
      }
    }
  },

  auditEpisode: async (episodeId) => {
    const res = await submitEpTask("audit", episodeId, "提交剧情审核任务…",
                                   () => api.auditEpisode(episodeId));
    return res;
  },

  optimizeEpisode: async (episodeId, issues) => {
    const res = await submitEpTask("optimize", episodeId, "提交一键修复任务…",
                                   () => api.optimizeEpisode(episodeId, issues));
    return res;
  },

  buildEpisode: async (episodeId) => {
    const res = await submitEpTask("build", episodeId, "提交分集制作任务…",
                                   () => api.buildEpisode(episodeId));
    return res;
  },

  extractEpisodeAssets: async (episodeId) => {
    const res = await submitEpTask("assets", episodeId, "提交本集资产解析任务…",
                                   () => api.extractEpisodeAssets(episodeId));
    return res;
  },

  stopEpisodeTask: async () => {
    const { epTaskId } = get();
    if (!epTaskId) return;
    try {
      await api.cancelTask(epTaskId);
    } finally {
      set({ epTask: null, epTaskId: null, parseLive: null });
    }
  },

  replanEpisodes: async (confirm) => {
    const { projectId } = get();
    if (!projectId) return undefined;
    try {
      const res = await api.replanEpisodes(projectId, confirm);
      if (confirm) {
        // 重新分集是长任务：进度经 WS 推送到观测窗
        set({
          epTask: { episodeId: "", kind: "replan", status: "queued",
                    progress: 0, label: "重新分集中…" },
          epTaskId: res.task_id ?? null,
          error: "",
          parseTrace: { stages: [], gates: [], thoughts: [] },
          parseLive: null,
          parseProgress: { progress: 0, label: "提交重新分集任务…" },
        });
      }
      return res;
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
      return undefined;
    }
  },

  patchEpisode: async (episodeId, body) => {
    try {
      const updated = await api.patchEpisode(episodeId, body);
      set((s) => ({
        episodes: s.episodes.map((e) => (e.id === episodeId ? updated : e)),
      }));
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
    }
  },

  expandScript: async (text) => {
    const projectId = get().projectId;
    if (!projectId) throw new Error("项目未初始化");
    return api.expandScript(projectId, text);
  },

  parseScript: async (keepVersion = false, requirements?: ParseRequirements) => {
    const { projectId, scriptText } = get();
    const text = scriptText.trim();
    if (!projectId || !text) return undefined;
    set({
      parsing: true,
      error: "",
      parseStopped: false,
      parseProgress: { progress: 0, label: "提交解析任务…" },
      parseTrace: { stages: [], thoughts: [] },
      parseLive: null,
    });
    try {
      // 提交长任务后立即返回；完成/失败由 handleTaskEvent 按 WS 事件收尾
      const res = await api.parseScript(projectId, text, keepVersion, requirements);
      set({ parseTaskId: res.task_id });
      return { task_id: res.task_id };
    } catch (e) {
      set({ parsing: false, error: e instanceof Error ? e.message : String(e) });
      return undefined;
    }
  },

  createCharacter: async (body) => {
    const projectId = get().projectId;
    if (!projectId) return undefined;
    const created = await api.createCharacter(projectId, body);
    set({ characters: [...get().characters, created] });
    return created;
  },

  updateCharacter: async (characterId, body) => {
    await api.updateCharacter(characterId, body);
    set({
      characters: get().characters.map((c) => (c.id === characterId ? { ...c, ...body } : c)),
    });
  },

  generateCharacterImage: async (characterId, opts) => {
    const character = get().characters.find((c) => c.id === characterId);
    if (!character) return;
    // 抽卡模式：只传抽卡目标与模型（提示词由后端按原形象续用/重组）
    if (opts?.regenerateImageId) {
      await api.generateCharacterImage(characterId, {
        regenerate_image_id: opts.regenerateImageId,
        model_id: opts?.model_id ?? "",
      });
    } else {
      const kind = opts?.kind ?? "basic";
      await api.generateCharacterImage(characterId, {
        kind,
        // basic 保持旧行为（固定名称「基础形象」）；其余形象名称可缺省由后端自动命名
        name: opts?.name ?? (kind === "basic" ? "基础形象" : ""),
        appearance_desc:
          opts?.appearance_desc || character.appearance || character.persona || character.name,
        model_id: opts?.model_id ?? "",
        episodes: opts?.episodes,
      });
    }
    // 本地先标记生成中，任务完成后由任务事件触发列表刷新
    set({
      characters: get().characters.map((c) =>
        c.id === characterId ? { ...c, generating: true } : c,
      ),
      genProgress: {
        ...get().genProgress,
        [`char_${characterId}`]: { status: "queued", progress: 0 },
      },
    });
  },

  cancelCharacterImage: async (characterId) => {
    await api.cancelCharacterImages(characterId);
    // 后端取消后 WS task_update（终态）会清进度并刷新列表；此处兜底清一次
    set((s) => {
      const genProgress = { ...s.genProgress };
      delete genProgress[`char_${characterId}`];
      return { genProgress };
    });
  },

  deleteCharacterImage: async (characterId, imageId) => {
    await api.deleteCharacterImage(characterId, imageId);
    // 删除后刷新角色列表（被删形象从页签/缩略图条消失，分镜映射已同步清理）
    await get().loadCharacters().catch(() => {});
  },

  /** 删除角色资产（两段式：预检不删除，确认后删除并清映射，产物保留资产库） */
  deleteCharacter: async (characterId, confirm) => {
    const res = await api.deleteCharacter(characterId, confirm);
    if (res.deleted) await get().loadCharacters().catch(() => {});
    return res;
  },

  deleteSceneImage: async (sceneId) => {
    await api.deleteSceneImage(sceneId);
    await get().loadScenes().catch(() => {});
  },

  /** 删除场景资产（两段式：预检不删除，确认后删除并清引用，产物保留资产库） */
  deleteScene: async (sceneId, confirm) => {
    const res = await api.deleteScene(sceneId, confirm);
    if (res.deleted) await get().loadScenes().catch(() => {});
    return res;
  },

  deleteSceneImageVersion: async (sceneId, versionId) => {
    const projectId = get().projectId;
    if (!projectId) throw new Error("项目未初始化");
    await api.deleteNodeVersion(projectId, `scene_${sceneId}`, versionId);
    await get().loadScenes().catch(() => {});
  },

  preferSceneImage: async (sceneId, versionId) => {
    await api.preferSceneImage(sceneId, versionId);
    await get().loadScenes().catch(() => {});
  },

  loadCostumeSuggestions: async () => {
    const projectId = get().projectId;
    if (!projectId) return;
    set({
      costumeSuggestions: await api
        .listCostumeSuggestions(projectId)
        .catch(() => []),
    });
  },

  acceptCostumeSuggestion: async (sug, opts) => {
    const projectId = get().projectId;
    if (!projectId) throw new Error("项目未初始化");
    const char = get().characters.find((c) => c.name === sug.character);
    if (!char) throw new Error(`角色「${sug.character}」不存在`);
    await api.acceptCostumeSuggestion(projectId, sug.id, {
      character_id: char.id,
      name: opts?.name,
      appearance_desc: opts?.appearance_desc,
      episodes: opts?.episodes ?? sug.episodes,
      model_id: opts?.model_id,
    });
    await Promise.all([
      get().loadCharacters().catch(() => {}),
      get().loadCostumeSuggestions().catch(() => {}),
    ]);
  },

  rejectCostumeSuggestion: async (sug) => {
    const projectId = get().projectId;
    if (!projectId) throw new Error("项目未初始化");
    await api.rejectCostumeSuggestion(projectId, sug.id);
    set({
      costumeSuggestions: get().costumeSuggestions.filter((s) => s.id !== sug.id),
    });
  },

  generateFourView: async (characterId, modelId, regenerateImageId) => {
    await api.generateFourView(characterId, {
      model_id: modelId ?? "",
      ...(regenerateImageId ? { regenerate_image_id: regenerateImageId } : {}),
    });
    // 本地先标记生成中，完成后由任务事件刷新列表
    set({
      characters: get().characters.map((c) =>
        c.id === characterId ? { ...c, generating: true } : c,
      ),
      genProgress: {
        ...get().genProgress,
        [`char_${characterId}`]: { status: "queued", progress: 0 },
      },
    });
  },

  uploadVoiceRef: async (characterId, file) => {
    await api.uploadVoiceRef(characterId, file);
    // 上传后刷新角色列表取最新 voice_ref_asset_id
    await get().loadCharacters().catch(() => {});
  },

  deleteVoiceRef: async (characterId) => {
    await api.deleteVoiceRef(characterId);
    await get().loadCharacters().catch(() => {});
  },

  applyCostume: async (characterId, imageId, episodeIds) => {
    const projectId = get().projectId;
    if (!projectId) throw new Error("项目未初始化");
    const result = await api.applyCostume(
      characterId,
      episodeIds?.length ? { image_id: imageId, episode_ids: episodeIds } : { image_id: imageId },
    );
    // 映射直接落库（无任务事件），主动刷新角色形象适用集数与当前集分镜
    await Promise.all([
      get().loadCharacters().catch(() => {}),
      get().activeEpisodeId ? get().loadShots(get().activeEpisodeId!).catch(() => {}) : null,
    ]);
    return result;
  },

  applySceneGroup: async (body) => {
    // 多选式编组（32 号交互优化）：一次提交成员集合；服务端重组装受影响场景的
    // 提示词（组环境设定进框），随后刷新列表让卡片上的「场景组」行跟上
    const res = await api.applySceneGroup(body);
    await get().loadScenes().catch(() => {});
    return res;
  },

  updateScene: async (sceneId, body) => {
    await api.updateScene(sceneId, body);
    set({
      scenes: get().scenes.map((s) => (s.id === sceneId ? { ...s, ...body } : s)),
    });
  },

  createScene: async (body) => {
    const projectId = get().projectId;
    if (!projectId) return undefined;
    const created = await api.createScene(projectId, body);
    set({ scenes: [...get().scenes, created] });
    return created;
  },

  createProp: async (body) => {
    const projectId = get().projectId;
    if (!projectId) return undefined;
    const created = await api.createProp(projectId, body);
    set({ props: [...get().props, created] });
    return created;
  },

  generateScene: async (sceneId, modelId, count) => {
    const res = await api.generateScene(sceneId, modelId, count);
    set({
      // 同步服务端写回的**完整提示词**（组装结果，界面所见即所发）；
      // 卡片里的编辑框由 useEffect 跟随 scene.image_prompt 刷新
      scenes: get().scenes.map((s) => (
        s.id === sceneId
          ? { ...s, generating: true, image_prompt: res.prompt || s.image_prompt }
          : s)),
      genProgress: { ...get().genProgress, [`scene_${sceneId}`]: { status: "queued", progress: 0 } },
    });
  },

  generateSceneExtraView: async (sceneId, view, modelId) => {
    await api.generateSceneExtraView(sceneId, view, modelId);
    // 与 generateScene 同款乐观标记。视角任务的 node_id 是
    // `scene_{id}_view_{view}`，而 WS 处理器按前两段归并到 `scene_{id}` 这个
    // 键，所以这里用同一个键即可——终态事件会清进度并 loadScenes()。
    set({
      scenes: get().scenes.map((s) => (s.id === sceneId ? { ...s, generating: true } : s)),
      genProgress: { ...get().genProgress, [`scene_${sceneId}`]: { status: "queued", progress: 0 } },
    });
  },

  generateProp: async (propId, modelId) => {
    const res = await api.generateProp(propId, modelId);
    // 本地先置 generating + 进度，任务完成后由任务事件触发列表刷新；
    // 同步服务端写回的完整提示词（组装结果，界面所见即所发）
    set({
      props: get().props.map((p) => (
        p.id === propId
          ? { ...p, generating: true, image_prompt: res.prompt || p.image_prompt }
          : p)),
      genProgress: { ...get().genProgress, [`prop_${propId}`]: { status: "queued", progress: 0 } },
    });
  },

  updateProp: async (propId, body) => {
    await api.updateProp(propId, body);
    set({
      props: get().props.map((p) => (p.id === propId ? { ...p, ...body } : p)),
    });
  },

  preferPropImage: async (propId, versionId) => {
    await api.preferPropImage(propId, versionId);
    await get().loadProps().catch(() => {});
  },

  cancelProp: async (propId) => {
    await api.cancelProp(propId);
    set((s) => {
      const genProgress = { ...s.genProgress };
      delete genProgress[`prop_${propId}`];
      return {
        genProgress,
        props: s.props.map((p) => (p.id === propId ? { ...p, generating: false } : p)),
      };
    });
  },

  /** 删除道具资产（两段式：预检不删除，确认后删除并清引用，产物保留资产库） */
  deleteProp: async (propId, confirm) => {
    const res = await api.deleteProp(propId, confirm);
    if (res.deleted) await get().loadProps().catch(() => {});
    return res;
  },

  preferCharacterImage: async (characterId, imageId, versionId) => {
    await api.preferCharacterImage(characterId, imageId, versionId);
    await get().loadCharacters().catch(() => {});
  },

  renameNodeVersion: async (nodeId, versionId, name) => {
    const projectId = get().projectId;
    if (!projectId) throw new Error("项目未初始化");
    await api.renameNodeVersion(projectId, nodeId, versionId, name);
  },

  cancelScene: async (sceneId) => {
    await api.cancelScene(sceneId);
    set((s) => {
      const genProgress = { ...s.genProgress };
      delete genProgress[`scene_${sceneId}`];
      return { genProgress };
    });
  },

  addShot: async (episodeId, body) => {
    const shot = await api.addShot(episodeId, body);
    set({ shots: { ...get().shots, [episodeId]: [...(get().shots[episodeId] ?? []), shot] } });
  },

  updateShot: async (shotId, body) => {
    const res = await api.updateShot(shotId, body);
    const shots = { ...get().shots };
    for (const eid of Object.keys(shots)) {
      const idx = shots[eid].findIndex((s) => s.id === shotId);
      if (idx >= 0) {
        const list = [...shots[eid]];
        // 时长以服务端回传为准（输入越界时会被钳位到安全区间）；
        // 改时长后服务端会确定性重排时间小节，回传的 video_prompt/timing_ok
        // 一并落到本地——框里立即显示「实际会发送的那份」（所见即所发）
        list[idx] = { ...list[idx], ...body,
          ...(res?.duration != null ? { duration: res.duration } : {}),
          ...(res?.video_prompt != null && res.video_prompt !== ""
            ? { video_prompt: res.video_prompt } : {}),
          ...(res?.timing_ok != null ? { timing_ok: res.timing_ok } : {}) };
        shots[eid] = list;
        break;
      }
    }
    set({ shots });
    // 时长被自动抬升（台词语速下限）必须让用户知道为什么——此前后端回了原因、
    // 前端从没消费，用户只看到数字变了
    if (res?.duration_note) toast.info(res.duration_note);
  },

  regenerateShotPrompt: async (shotId) => {
    const res = await api.regenerateShotPrompt(shotId);
    const shots = { ...get().shots };
    for (const eid of Object.keys(shots)) {
      const idx = shots[eid].findIndex((s) => s.id === shotId);
      if (idx >= 0) {
        const list = [...shots[eid]];
        list[idx] = { ...list[idx], video_prompt: res.video_prompt, timing_ok: res.timing_ok };
        shots[eid] = list;
        break;
      }
    }
    set({ shots });
  },

  /** 预演重算结果（dry-run）：**不落库、不改本地状态**，只把文本拿回来给用户看。
   *  「重算」按钮走这条 + 确认框，避免点一下就直接覆盖手改过的描述（抽盲盒） */
  previewShotPrompt: async (shotId) => {
    const res = await api.regenerateShotPrompt(shotId, { dryRun: true });
    return { video_prompt: res.video_prompt, timing_ok: res.timing_ok,
             state_position: res.state_position ?? "" };
  },

  /** 应用（确认过、可能被改过的）重算结果：仍走重算端点落库，以便联动清除
   *  该镜的「内容类」体检项（suspended/state_chain/overload）——改走通用
   *  updateShot 会丢掉这个副作用 */
  applyShotPrompt: async (shotId, prompt, statePosition = "") => {
    const text = prompt.trim();
    if (!text) return;
    const res = await api.regenerateShotPrompt(shotId, {
      prompt: text, statePosition });
    const shots = { ...get().shots };
    for (const eid of Object.keys(shots)) {
      const idx = shots[eid].findIndex((s) => s.id === shotId);
      if (idx >= 0) {
        const list = [...shots[eid]];
        list[idx] = { ...list[idx], video_prompt: res.video_prompt, timing_ok: res.timing_ok };
        shots[eid] = list;
        break;
      }
    }
    set({ shots });
  },

  setModelPresets: (presets) => set({ modelPresets: presets }),
  setWsConnected: (v) => set({ wsConnected: v }),

  generateShots: async (episodeId, mode, modelId, shotIds, opts) => {
    const result = await api.generateShots(episodeId, mode, modelId, shotIds, opts);
    // 记录批次（顶栏聚合进度条），并清除该集上次的分镜失败标记
    get().trackBatch(result.batch_id, result.task_ids);
    const episodeShots = get().shots[episodeId] ?? [];
    // 定向重拍：只标记子集内分镜为生成中
    const target = shotIds ? new Set(shotIds) : null;
    set({
      shots: {
        ...get().shots,
        [episodeId]: episodeShots.map((s) =>
          (!target || target.has(s.id)) ? { ...s, generating: true } : s,
        ),
      },
      shotErrors: Object.fromEntries(
        Object.entries(get().shotErrors).filter(
          ([sid]) => !episodeShots.some((s) => s.id === sid),
        ),
      ),
    });
    return result;
  },

  trackBatch: (batchId, taskIds) => {
    // 全部跳过（如无首帧可生成视频）时不建空批次
    if (taskIds.length === 0) {
      set({ batch: null });
      return;
    }
    const tasks: BatchProgress["tasks"] = {};
    for (const id of taskIds) {
      tasks[id] = { status: "queued", progress: 0, node_id: "", error: "" };
    }
    set({ batch: { batchId, total: taskIds.length, tasks } });
  },

  clearBatch: () => set({ batch: null }),

  composeEpisode: async (episodeId) => {
    return api.composeEpisode(episodeId);
  },

  composeBatch: async (episodeIds) => {
    const projectId = get().projectId;
    if (!projectId) throw new Error("项目未初始化");
    return api.composeEpisodesBatch(projectId, episodeIds);
  },

  linkFrames: async (episodeId) => {
    const result = await api.linkFrames(episodeId);
    // 尾帧版本直接追加落库（无任务事件），主动刷新本集分镜
    await get().loadShots(episodeId).catch(() => {});
    return result;
  },

  handleTaskEvent: (event, data) => {
    // 通道状态点（2026-09-30 复查 F5）：断线置灰、连上点亮。**首次连接**（此前
    // 从没连上、wasOpen=false 只会发 connected）同样走下面的补拉——旧实现只认
    // reconnected，首屏握手失败后即便后来连上也永远不刷新，短剧工作台会一直
    // 停在旧数据上。
    if (event === "connected" || event === "disconnected") {
      get().setWsConnected(event === "connected");
      if (event === "disconnected") return;
    }
    // 重连兜底：断线期间的事件已丢失，全量刷新各分区；解析任务补查一次最新状态
    if (event === "reconnected" || event === "connected") {
      const episodeId = get().activeEpisodeId;
      const projectId = get().projectId;
      get().loadCharacters().catch(() => {});
      get().loadScenes().catch(() => {});
      get().loadProps().catch(() => {});
      get().loadEpisodes().catch(() => {});
      if (episodeId) get().loadShots(episodeId).catch(() => {});
      // 批次与进度按 REST 对账重建（审计 13 号 O-4/O-5）：WS 断线期间任务进入
      // 终态时事件会丢（后端队列满即丢、无重放），旧实现只刷列表——批次聚合条
      // 永久停在「N/M 完成」且无关闭入口，分镜卡挂着假进度条（列表已不 generating）
      const tracked = get().batch;
      if (projectId && tracked?.batchId) {
        api.listTasks(projectId, tracked.batchId).then((tasks) => {
          if (!tasks.length) return;
          const cur = get().batch;
          if (!cur || cur.batchId !== tracked.batchId) return;  // 期间已换批
          const next = { ...cur.tasks };
          const gen: Record<string, { status: string; progress: number }> = {};
          for (const t of tasks) {
            if (next[t.id]) {
              next[t.id] = { ...next[t.id], status: t.status,
                             error: t.error ?? next[t.id].error };
            }
            if (!FINAL_STATUS.includes(t.status)) {
              gen[t.node_id] = { status: t.status, progress: t.progress };
            }
          }
          set({ batch: { ...cur, tasks: next }, genProgress: gen });
        }).catch(() => {});
      } else if (projectId) {
        // 无批次追踪：至少把残留的假进度条清掉（列表已刷新为真实 generating）
        set({ genProgress: {} });
      }
      if (get().parsing && get().parseTaskId) {
        const taskId = get().parseTaskId!;
        api.getTask(taskId).then((t) => {
          get().handleTaskEvent("task_update", t as unknown as Record<string, unknown>);
        }).catch(() => {});
      }
      return;
    }
    const status = data.status as string | undefined;
    const nodeId = (data.node_id as string) || "";
    // 计费（33 号方案）：任务到终态时余额可能变化（计费成立 / 失败自动退款），
    // 主动重拉一次余额——账目没有项目维度、不开新 WS 频道，主动拉取最可靠
    if (event === "task_update" && status && FINAL_STATUS.includes(status)) {
      void useBilling.getState().refreshAccount();
    }
    // 批次聚合进度：task_update 带 batch_id/状态/错误（data.id），task_progress
    // 只有 task_id——两者都按 taskId 归账到当前追踪的批次
    const tracked = get().batch;
    const tid = (data.task_id as string) || (data.id as string) || "";
    if (tracked && tid && tracked.tasks[tid]) {
      const prev = tracked.tasks[tid];
      set({
        batch: {
          ...tracked,
          tasks: {
            ...tracked.tasks,
            [tid]: {
              status: (status as string) || prev.status,
              progress: typeof data.progress === "number" ? data.progress : prev.progress,
              node_id: (data.node_id as string) || prev.node_id,
              error: (data.error as string) || prev.error,
            },
          },
        },
      });
    }
    // 观测窗写入（轨迹/流式思考/进度）：解析与剧本优化**共用同一份展示状态**
    // ——用户要的正是「在同一个 AI 解析过程里看到优化过程」。返回 true = 已消费
    const applyObservability = (): boolean => {
      if (event === "task_trace") {
        const traceKind = data.kind as string;
        // 流式思考增量：begin 开新块 / delta 追加（仅保留尾部 4000 字防膨胀）。
        // delta 走合帧缓冲（见 flushStream 注释）：高频逐条 set 会把整个剧本页
        // 拖进每秒数百次重渲染——这正是浏览器标签被拖死的原因
        if (traceKind === "stream_begin") {
          dropStreamBuf();
          set({ parseLive: { title: (data.title as string) || "AI 思考中", text: "" } });
          return true;
        }
        if (traceKind === "stream") {
          streamBuf += String(data.text ?? "");
          if (streamBuf.length > 20000) streamBuf = streamBuf.slice(-20000); // 缓冲上限
          if (streamTimer === null) {
            streamTimer = setTimeout(flushStream, STREAM_FLUSH_MS);
          }
          return true;
        }
        if (traceKind === "stream_end") {
          flushStream(); // 收尾即冲刷：最后一段增量不得丢
          return true;
        }
        flushStream(); // stage/thought 到达前先冲刷，保证 live 块内容与归档对齐
        // 过程轨迹实时追加（stage→步骤条，thought→思考观测窗）
        const title = (data.title as string) || "";
        const at = (data.at as number) || Math.floor(Date.now() / 1000);
        set((s) => {
          const trace: TaskTrace = {
            stages: [...(s.parseTrace?.stages ?? [])],
            thoughts: [...(s.parseTrace?.thoughts ?? [])],
          };
          if (traceKind === "stage") {
            trace.stages = [...trace.stages, { label: title, progress: null, at }];
          } else if (traceKind === "thought") {
            trace.thoughts = [...trace.thoughts, {
              title, content: (data.content as string) || "", at,
            }];
          }
          // 其余未知 kind 忽略（防止未知事件灌入空条目）
          // 流式思考块结束时后端会归档为同标题 thought 条目：清掉 live 块
          // 避免同一内容重复展示（折叠条目接替流式块，内容连续）
          const liveCleared =
            traceKind === "thought" && !!title && title === s.parseLive?.title;
          return {
            parseTrace: trace,
            ...(liveCleared ? { parseLive: null } : {}),
          };
        });
        return true;
      }
      if (event === "task_progress") {
        const progress = typeof data.progress === "number" ? data.progress : 0;
        set({ parseProgress: { progress, label: (data.label as string) || "解析中…" } });
        return true;
      }
      return false;
    };

    // 解析长任务：进度实时更新；终态收尾（完成刷新全部解析数据，失败/取消透出原因）
    if (nodeId === NODE_ID_PARSE) {
      // 只认当前追踪的解析任务（审计 13 号：此前只比对 node_id，两个解析任务
      // 并存时——双击拍板/重复提交——进度、轨迹与终态会互相覆写，任一方的
      // done 就把 parsing 置 false，UI 提前宣称「解析完成」而另一方仍在跑）
      const trackedParseId = get().parseTaskId;
      const evtTaskId = (data.task_id as string) || (data.id as string) || "";
      if (trackedParseId && evtTaskId && evtTaskId !== trackedParseId) return;
      if (applyObservability()) return;
      if (status && FINAL_STATUS.includes(status)) {
        if (status === "done") {
          // parseLive 必须随终态清空（审计 13 号：流式块靠「同标题 thought 归档」
          // 清理，而 stream_label（审读剧本（通读全文））与归档标题（剧本审读
          // 结论）从不相遇 → 解析结束后观测窗底部长期残留上一轮流式文本）
          dropStreamBuf(); // 在途增量作废，别让终态之后又冒出一帧
          set({ parsing: false, parseTaskId: null, parseLive: null,
                parseProgress: { progress: 1, label: "解析完成" } });
          // 解析结果已落库：回查结构化数据（fire-and-forget，与既有事件处理一致）
          const pid = get().projectId;
          if (pid) {
            api.getScript(pid).then((script) => set({ script })).catch(() => {});
          }
          get().loadCharacters().catch(() => {});
          get().loadScenes().catch(() => {});
          get().loadProps().catch(() => {});
          get().loadEpisodes().catch(() => {});
          get().loadScriptVersions().catch(() => {});
          get().loadCostumeSuggestions().catch(() => {});
          set({ parseFailed: false });
        } else if (status === "canceled" && get().parseStopped) {
          // 用户主动停止：静默收尾（旧解析数据完好），不打扰
          dropStreamBuf();
          set({ parsing: false, parseTaskId: null, parseFailed: false,
                parseLive: null });
        } else {
          dropStreamBuf();
          set({ parsing: false, parseTaskId: null, parseFailed: true,
                parseLive: null,
                error: (data.error as string) || "解析任务失败，请重试" });
        }
      }
      return;
    }
    // 逐集任务（审核/修复/制作，node_id = `ep_{阶段}_{集id}`）与重新分集：
    // 与解析共用观测窗（轨迹/流式/进度同一份展示状态）——逐集后同时只跑一个
    // 任务，混在一起不会乱，反而把「审核 → 修复 → 制作」串成一条可读的过程。
    // 终态刷新分集列表（徽标/清单/分镜数都随之更新）。
    if (EP_TASK_PREFIXES.some((p) => nodeId.startsWith(p))
        || nodeId === NODE_ID_REPLAN) {
      const trackedEpId = get().epTaskId;
      const evtEpId = (data.task_id as string) || (data.id as string) || "";
      if (trackedEpId && evtEpId && evtEpId !== trackedEpId) return;
      const prevEp = get().epTask;
      if (event === "task_progress" && prevEp) {
        // 同步写 parseProgress：观测窗的进度条读的是它（逐集任务与解析共用同一份
        // 展示状态），只更 epTask 会让进度条一直停在提交时的 0%
        const p = typeof data.progress === "number" ? data.progress : prevEp.progress;
        const label = (data.label as string) || prevEp.label;
        set({
          epTask: { ...prevEp, status: "running", progress: p, label },
          parseProgress: { progress: p, label },
        });
      } else if (event === "task_update" && prevEp && status) {
        set({ epTask: { ...prevEp, status } });
      }
      if (applyObservability()) return;
      if (status && FINAL_STATUS.includes(status)) {
        dropStreamBuf();
        const pid = get().projectId;
        if (status === "done") {
          set({ epTask: null, epTaskId: null, parseLive: null,
                parseProgress: { progress: 1, label: "任务完成" },
                parseStopped: false, error: "", parseFailed: false });
        } else if (status === "canceled") {
          // parseStopped 要置位：观测窗据此显示「已停止」而不是「已完成」
          set({ epTask: null, epTaskId: null, parseLive: null,
                parseStopped: true, parseFailed: false });
        } else {
          set({ epTask: null, epTaskId: null, parseLive: null,
                parseStopped: false, parseFailed: true,
                error: (data.error as string) || "任务失败，请重试" });
        }
        // 终态刷新：分集徽标/审核清单/分镜数都随之更新；修复还会改当集正文，
        // 制作会改分镜，故一并刷新当前集的分镜与脚本原文；资产解析会新建/更新
        // 项目资产（origin_seq = 首见集号），三份资产列表也要重拉
        get().loadEpisodes().catch(() => {});
        const activeId = get().activeEpisodeId;
        if (activeId) get().loadShots(activeId).catch(() => {});
        if (pid) get().loadScriptVersions().catch(() => {});
        if (prevEp?.kind === "assets") {
          get().loadCharacters().catch(() => {});
          get().loadScenes().catch(() => {});
          get().loadProps().catch(() => {});
        }
      }
      return;
    }
    // 角色/场景/道具生成进度：task_update（含 status+progress）与 task_progress
    // （含 progress+node_id）都写入进度映射供卡片展示；终态清除并刷新列表
    if (nodeId.startsWith("char_") || nodeId.startsWith("scene_")
        || nodeId.startsWith("prop_")) {
      const key = nodeId.split("_").slice(0, 2).join("_"); // char_{cid} / scene_{sid} / prop_{pid}
      const prev = get().genProgress[key];
      if (status && FINAL_STATUS.includes(status)) {
        set((s) => {
          const genProgress = { ...s.genProgress };
          delete genProgress[key];
          return { genProgress };
        });
        if (nodeId.startsWith("char_")) get().loadCharacters().catch(() => {});
        else if (nodeId.startsWith("scene_")) get().loadScenes().catch(() => {});
        else get().loadProps().catch(() => {});
        return;
      }
      const progress = typeof data.progress === "number" ? data.progress : prev?.progress ?? 0;
      if (status || prev) {
        set((s) => ({
          genProgress: {
            ...s.genProgress,
            [key]: { status: status ?? prev?.status ?? "running", progress },
          },
        }));
      }
      return;
    }
    // 分镜任务：进度写入 genProgress（卡片内进度条），终态记录失败原因并刷新列表
    if (nodeId.startsWith("shot_")) {
      const shotKey = nodeId.split("_").slice(0, 2).join("_"); // shot_{镜id}
      const shotId = shotKey.slice("shot_".length);
      const prev = get().genProgress[shotKey];
      if (status && FINAL_STATUS.includes(status)) {
        set((s) => {
          const genProgress = { ...s.genProgress };
          delete genProgress[shotKey];
          const shotErrors = { ...s.shotErrors };
          if (status === "failed" && data.error) shotErrors[shotId] = String(data.error);
          else delete shotErrors[shotId];
          return { genProgress, shotErrors };
        });
        // 分镜首帧（shot_*_ff）与视频（shot_*_v）统一刷新当前集分镜
        const episodeId = get().activeEpisodeId;
        if (episodeId) get().loadShots(episodeId).catch(() => {});
        return;
      }
      const progress = typeof data.progress === "number" ? data.progress : prev?.progress ?? 0;
      if (status || prev) {
        set((s) => ({
          genProgress: {
            ...s.genProgress,
            [shotKey]: { status: status ?? prev?.status ?? "running", progress },
          },
        }));
      }
      return;
    }
    // 其余任务（合成等）仅终态刷新，避免高频进度事件反复拉取列表
    if (!status || !FINAL_STATUS.includes(status)) return;
    get().loadEpisodes().catch(() => {});
  },

  reset: () =>
    set({
      projectId: null,
      scriptText: "",
      script: null,
      characters: [],
      scenes: [],
      // props 必须一并清空：漏清会让下一个项目在 loadProps 覆盖前残留上一项目
      props: [],
      episodes: [],
      shots: {},
      activeEpisodeId: null,
      genProgress: {},
      batch: null,
      shotErrors: {},
      costumeSuggestions: [],
      modelPresets: null,
      parsing: false,
      parseProgress: { progress: 0, label: "" },
      parseTaskId: null,
      parseTrace: { stages: [], thoughts: [] },
      parseLive: null,
      parseStopped: false,
      parseFailed: false,
      scriptVersions: [],
      error: "",
    }),
  };
});
