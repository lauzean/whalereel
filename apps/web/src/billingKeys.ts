/** 计费接口键（33 号方案）：前端按钮 → 后端计费闸清单 key 的**集中映射**。
 *
 * 每个 key 必须与后端计费闸清单（core/billing.py 的 `GATED_ENDPOINTS`）的
 * 「METHOD + 路由模板」**逐字一致**——键对不上 = 该按钮悬停时不显示计费提示。
 * 用法（2026-10-09 用户反馈：不要单独徽标挂按钮上，并进 title）：
 * - 按钮悬停提示：`title={useCostTip(BILLING_KEYS.xxx) || undefined}`；
 *   按钮已有 title 时用 `useCostTipWith("原说明", BILLING_KEYS.xxx)` 合并；
 * - 确认弹窗文案行：`costLine(BILLING_KEYS.xxx)`；
 * - **禁止在组件里散落端点字符串**；预扣清单本身来自 /api/billing/pricing
 *   （唯一数据源），这里只放键名映射。
 */

export const BILLING_KEYS = {
  /** 剧本解析（长任务，LLM 多次） */
  scriptParse: "POST /api/projects/{project_id}/script/parse",
  /** 一句话梗概扩写（LLM 一次） */
  scriptExpand: "POST /api/projects/{project_id}/script/expand",
  /** AI 优化提示词（LLM 一两次） */
  promptsOptimize: "POST /api/projects/{project_id}/prompts/optimize",
  /** 单集剧情审核 */
  episodeAudit: "POST /api/episodes/{episode_id}/audit",
  /** 本集资产解析 */
  episodeAssets: "POST /api/episodes/{episode_id}/assets",
  /** 单集一键修复 */
  episodeOptimize: "POST /api/episodes/{episode_id}/optimize",
  /** 单集制作（拆解+画面描述） */
  episodeBuild: "POST /api/episodes/{episode_id}/build",
  /** 按剧情重新分集 */
  episodeReplan: "POST /api/projects/{project_id}/episodes/replan",
  /** 分镜首帧/视频批量生成（含单镜与重摇） */
  shotsGenerate: "POST /api/episodes/{episode_id}/shots/generate",
  /** 视觉自动判级（多模态 LLM 逐镜） */
  autoReview: "POST /api/episodes/{episode_id}/auto-review",
  /** 角色定妆图/妆造生成 */
  characterImage: "POST /api/characters/{character_id}/images",
  /** 角色三视图生成 */
  characterFourView: "POST /api/characters/{character_id}/images/fourview",
  /** 接受妆造建议并生成 */
  costumeAccept:
    "POST /api/projects/{project_id}/costume-suggestions/{suggestion_id}/accept",
  /** 场景主图生成 */
  sceneGenerate: "POST /api/scenes/{scene_id}/generate",
  /** 场景补充视角（LLM+生图） */
  sceneExtraView: "POST /api/scenes/{scene_id}/extra-view",
  /** 道具定妆图生成 */
  propGenerate: "POST /api/props/{prop_id}/generate",
  /** 画布生成任务（t2i/t2v/tts 统一入口） */
  createTask: "POST /api/tasks",
  /** 单镜画面描述重算（LLM） */
  regenerateShotPrompt: "POST /api/shots/{shot_id}/regenerate-video-prompt",
  /** Agent 对话（每次调用） */
  chat: "POST /api/sessions/{session_id}/chat",
} as const;
