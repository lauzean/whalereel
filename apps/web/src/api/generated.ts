/**
 * 后端契约生成物 —— 请勿手改。
 *
 * 由 `python scripts/gen_api_types.py` 从后端 OpenAPI 生成；
 * 改了后端请求 DTO 后必须重跑（验证脚本会对账，过期会失败）。
 *
 * 覆盖范围：请求体 Pydantic 模型 + 操作清单（METHOD /path → 请求类型）。
 * 响应体未标注 `response_model`，OpenAPI 里没有结构信息，故未生成。
 *
 * schema 指纹：662e3f9ae46fb85d
 */


export interface AdjustIn {
  user_id: string;
  amount: number;
  note?: string;
}

export interface ApplyImageIn {
  artifact_id: string;
}

export interface AssetLinkIn {
  character_id: string;
  image_id: string;
  scope?: string;
  episode_id?: string | null;
  shot_id?: string | null;
  note?: string;
}

export interface Body_extract_script_doc_api_projects__project_id__script_extract_post {
  file: string;
}

export interface Body_upload_character_image_api_characters__character_id__images_upload_post {
  file: string;
}

export interface Body_upload_version_api_nodes__node_id__upload_post {
  file: string;
}

export interface Body_upload_voice_ref_api_characters__character_id__voice_ref_post {
  file: string;
}

export interface CanvasIn {
  graph: Record<string, unknown>;
}

export interface CharacterImageIn {
  kind?: string;
  name?: string;
  appearance_desc?: string;
  model_id?: string;
  episodes?: number[] | null;
  regenerate_image_id?: string;
}

export interface CharacterImagePreferIn {
  version_id?: string | null;
}

export interface CharacterIn {
  name: string;
  role?: string;
  persona?: string;
  appearance?: string;
  voice_desc?: string;
  image_prompt?: string;
}

export interface CharacterPatchIn {
  name?: string | null;
  role?: string | null;
  persona?: string | null;
  appearance?: string | null;
  voice_desc?: string | null;
  image_prompt?: string | null;
}

export interface ChatIn {
  text: string;
  refs?: ChatRef[];
  confirmed?: boolean;
}

export interface ChatRef {
  node_id: string;
  version_id?: string | null;
}

export interface ComposeBatchIn {
  project_id: string;
  episode_ids: string[];
}

export interface ConcatIn {
  kind: string;
  artifact_ids: string[];
  dest_node_id: string;
}

export interface CostumeApplyIn {
  image_id: string;
  episode_ids?: string[] | null;
}

export interface CostumeSuggestionAcceptIn {
  character_id: string;
  name?: string | null;
  appearance_desc?: string | null;
  episodes?: number[] | null;
  model_id?: string;
}

export interface CropIn {
  ratio?: string;
}

export interface DefaultsIn {
  defaults: Record<string, string | null>;
}

export interface EndpointIn {
  name: string;
  kind: string;
  provider: string;
  base_url: string;
  key_id?: string | null;
  model?: string;
  multimodal?: boolean;
  credits_per_1k_tokens?: number;
  credits_per_image?: number;
  credits_per_video_second?: number;
  bill_enabled?: boolean;
}

export interface EndpointPatchIn {
  name?: string | null;
  kind?: string | null;
  provider?: string | null;
  base_url?: string | null;
  key_id?: string | null;
  model?: string | null;
  multimodal?: boolean | null;
  credits_per_1k_tokens?: number | null;
  credits_per_image?: number | null;
  credits_per_video_second?: number | null;
  bill_enabled?: boolean | null;
}

export interface EpisodeOptimizeIn {
  issues?: Record<string, unknown>[];
}

export interface EpisodePatchIn {
  title?: string | null;
  synopsis?: string | null;
  body?: string | null;
}

export interface EpisodeReplanIn {
  confirm?: boolean;
}

export interface ExtractFrameIn {
  position?: string;
  time_sec?: number | null;
}

export interface FirstFramePreferIn {
  version_id?: string | null;
}

export interface FourViewIn {
  model_id?: string;
  regenerate_image_id?: string;
}

export interface KeyIn {
  name: string;
  api_key: string;
  remark?: string;
}

export interface KeyPatchIn {
  name?: string | null;
  api_key?: string | null;
  remark?: string | null;
}

export interface LessonApplyIn {
  rule: string;
}

export interface LoginIn {
  username: string;
  password: string;
}

export interface PasswordChangeIn {
  old_password: string;
  new_password: string;
}

export interface ProbeIn {
  kind: string;
  provider: string;
  base_url: string;
  api_key?: string | null;
}

export interface ProjectIn {
  name: string;
  mode?: string;
  style_preset?: string;
  style_anchor?: string;
  aspect_ratio?: string;
  video_resolution?: string;
  country?: string;
  description?: string;
  model_presets?: Record<string, string>;
}

export interface ProjectPatchIn {
  model_presets?: Record<string, string> | null;
  country?: string | null;
  aspect_ratio?: string | null;
  video_resolution?: string | null;
  description?: string | null;
  style_anchor?: string | null;
  refine_first_frame?: boolean | null;
  scene_group_lock?: boolean | null;
}

export interface PromptOptimizeIn {
  target: string;
  id: string;
  instruction: string;
  current_prompt?: string;
}

export interface PromptSaveIn {
  content: string;
}

export interface PropIn {
  name: string;
  description?: string;
  image_prompt?: string;
}

export interface PropPatchIn {
  name?: string | null;
  description?: string | null;
  image_prompt?: string | null;
}

export interface PropPreferIn {
  version_id?: string | null;
}

export interface ProviderIn {
  kind: string;
  provider: string;
  base_url?: string | null;
  api_key?: string | null;
  model?: string | null;
}

export interface RechargeIn {
  user_id: string;
  amount: number;
  note?: string;
}

export interface RedeemCodesIn {
  credits: number;
  count: number;
  note?: string;
}

export interface RedeemIn {
  code: string;
}

export interface RegeneratePromptIn {
  dry_run?: boolean;
  prompt?: string;
  state_position?: string;
}

export interface RestoreIn {
  artifact_id: string;
}

export interface RotateIn {
  direction?: string;
}

export interface SceneExtraViewIn {
  view: string;
  model_id?: string;
}

export interface SceneGenerateIn {
  model_id?: string;
  count?: number;
}

export interface SceneGroupApplyIn {
  project_id: string;
  scene_ids?: string[];
  name?: string;
  env_setting?: string;
  anchor_asset_id?: string | null;
}

export interface SceneGroupPatchIn {
  name?: string | null;
  env_setting?: string | null;
  anchor_asset_id?: string | null;
}

export interface SceneIn {
  name: string;
  description?: string;
  image_prompt?: string;
}

export interface ScenePatchIn {
  name?: string | null;
  description?: string | null;
  image_prompt?: string | null;
  group_name?: string | null;
}

export interface ScenePreferIn {
  version_id?: string | null;
}

export interface ScriptExpandIn {
  text: string;
}

export interface ScriptParseIn {
  text: string;
  keep_version?: boolean;
  requirements?: Record<string, unknown> | null;
}

export interface SessionIn {
  title?: string | null;
}

export interface SessionPatchIn {
  title?: string | null;
  status?: string | null;
  pane_side?: string | null;
}

export interface SettingsPatchIn {
  enabled?: boolean | null;
  signup_grant?: number | null;
  auto_refund?: boolean | null;
  admin_free?: boolean | null;
}

export interface ShotIn {
  description: string;
  scene_ref_id?: string | null;
  duration?: number;
}

export interface ShotPatchIn {
  description?: string | null;
  video_prompt?: string | null;
  duration?: number | null;
  scene_ref_id?: string | null;
  character_ref_ids?: string[] | null;
  camera?: string | null;
  transition_in?: string | null;
  costume_map?: Record<string, string> | null;
}

export interface ShotVerdictIn {
  verdict: string;
  evidence?: string;
  changed?: string;
  reason?: string;
}

export interface ShotsGenerateIn {
  mode?: string;
  model_id?: string;
  ignore_missing_assets?: boolean;
  shot_ids?: string[] | null;
  changed?: string;
  note?: string;
  reason?: string;
  force?: boolean;
  ignore_storyboard_qa?: boolean;
  count?: number;
}

export interface SignIn {
  artifact_ids?: string[];
  files?: string[];
}

export interface SkillCreateIn {
  file: string;
}

export interface SkillSaveIn {
  content: string;
}

export interface TaskIn {
  project_id: string;
  node_id: string;
  runner: string;
  model_id?: string;
  params?: Record<string, unknown>;
  input_refs?: Record<string, unknown>;
}

export interface TrimIn {
  start: number;
  end: number;
}

export interface UserCreateIn {
  username: string;
  password: string;
  is_admin?: boolean;
}

export interface UserPatchIn {
  password?: string | null;
  is_admin?: boolean | null;
  disabled?: boolean | null;
}

export interface VersionRenameIn {
  name?: string;
}

// 操作清单：冒号右侧为该操作的请求体类型（null = 无请求体）
export interface ApiRequestMap {
  "POST /api/artifacts/sign": SignIn;
  "GET /api/artifacts/{artifact_id}": null;
  "GET /api/artifacts/{artifact_id}/file": null;
  "POST /api/auth/change-password": PasswordChangeIn;
  "POST /api/auth/login": LoginIn;
  "POST /api/auth/logout": null;
  "GET /api/auth/me": null;
  "GET /api/auth/users": null;
  "POST /api/auth/users": UserCreateIn;
  "DELETE /api/auth/users/{user_id}": null;
  "PATCH /api/auth/users/{user_id}": UserPatchIn;
  "GET /api/billing/account": null;
  "GET /api/billing/accounts": null;
  "POST /api/billing/adjust": AdjustIn;
  "POST /api/billing/audit": null;
  "GET /api/billing/pricing": null;
  "POST /api/billing/recharge": RechargeIn;
  "POST /api/billing/redeem": RedeemIn;
  "GET /api/billing/redeem-codes": null;
  "POST /api/billing/redeem-codes": RedeemCodesIn;
  "GET /api/billing/settings": null;
  "PATCH /api/billing/settings": SettingsPatchIn;
  "GET /api/billing/transactions": null;
  "GET /api/billing/transactions/all": null;
  "GET /api/camera-moves": null;
  "DELETE /api/characters/{character_id}": null;
  "PATCH /api/characters/{character_id}": CharacterPatchIn;
  "POST /api/characters/{character_id}/apply-image": ApplyImageIn;
  "POST /api/characters/{character_id}/costume/apply": CostumeApplyIn;
  "POST /api/characters/{character_id}/images": CharacterImageIn;
  "POST /api/characters/{character_id}/images/cancel": null;
  "POST /api/characters/{character_id}/images/fourview": FourViewIn;
  "POST /api/characters/{character_id}/images/upload": FormData;
  "DELETE /api/characters/{character_id}/images/{image_id}": null;
  "POST /api/characters/{character_id}/images/{image_id}/prefer": CharacterImagePreferIn;
  "DELETE /api/characters/{character_id}/voice-ref": null;
  "POST /api/characters/{character_id}/voice-ref": FormData;
  "GET /api/defaults": null;
  "PUT /api/defaults": DefaultsIn;
  "GET /api/endpoints": null;
  "POST /api/endpoints": EndpointIn;
  "POST /api/endpoints/apply-suggested-prices": Record<string, unknown> | null;
  "POST /api/endpoints/probe-models": ProbeIn;
  "DELETE /api/endpoints/{endpoint_id}": null;
  "PATCH /api/endpoints/{endpoint_id}": EndpointPatchIn;
  "POST /api/episodes/compose-batch": ComposeBatchIn;
  "PATCH /api/episodes/{episode_id}": EpisodePatchIn;
  "POST /api/episodes/{episode_id}/assets": null;
  "POST /api/episodes/{episode_id}/audit": null;
  "POST /api/episodes/{episode_id}/auto-review": null;
  "POST /api/episodes/{episode_id}/build": null;
  "POST /api/episodes/{episode_id}/compose": null;
  "GET /api/episodes/{episode_id}/generate-preview": null;
  "POST /api/episodes/{episode_id}/link-frames": null;
  "POST /api/episodes/{episode_id}/optimize": EpisodeOptimizeIn;
  "GET /api/episodes/{episode_id}/precheck": null;
  "GET /api/episodes/{episode_id}/review": null;
  "GET /api/episodes/{episode_id}/reviews": null;
  "GET /api/episodes/{episode_id}/script": null;
  "GET /api/episodes/{episode_id}/shots": null;
  "POST /api/episodes/{episode_id}/shots": ShotIn;
  "GET /api/episodes/{episode_id}/shots/download": null;
  "POST /api/episodes/{episode_id}/shots/generate": ShotsGenerateIn;
  "GET /api/episodes/{episode_id}/storyboard-qa": null;
  "GET /api/episodes/{episode_id}/video-readiness": null;
  "GET /api/health": null;
  "GET /api/keys": null;
  "POST /api/keys": KeyIn;
  "DELETE /api/keys/{key_id}": null;
  "PATCH /api/keys/{key_id}": KeyPatchIn;
  "GET /api/lessons-insights": null;
  "POST /api/lessons-insights/apply": LessonApplyIn;
  "GET /api/model-calls": null;
  "GET /api/model-calls/stats": null;
  "GET /api/model-calls/{call_id}": null;
  "GET /api/model-options": null;
  "GET /api/models": null;
  "POST /api/models/cancel": null;
  "GET /api/models/detail": null;
  "GET /api/models/install": null;
  "POST /api/models/install": null;
  "POST /api/models/uninstall": null;
  "POST /api/nodes/{node_id}/crop": CropIn;
  "POST /api/nodes/{node_id}/extract-frame": ExtractFrameIn;
  "POST /api/nodes/{node_id}/rotate": RotateIn;
  "POST /api/nodes/{node_id}/trim": TrimIn;
  "POST /api/nodes/{node_id}/upload": FormData;
  "GET /api/nodes/{node_id}/versions": null;
  "DELETE /api/nodes/{node_id}/versions/{version_id}": null;
  "PATCH /api/nodes/{node_id}/versions/{version_id}": VersionRenameIn;
  "GET /api/projects": null;
  "POST /api/projects": ProjectIn;
  "GET /api/projects/style-anchors": null;
  "DELETE /api/projects/{project_id}": null;
  "GET /api/projects/{project_id}": null;
  "PATCH /api/projects/{project_id}": ProjectPatchIn;
  "DELETE /api/projects/{project_id}/asset-links": null;
  "GET /api/projects/{project_id}/asset-links": null;
  "PUT /api/projects/{project_id}/asset-links": AssetLinkIn;
  "GET /api/projects/{project_id}/assets": null;
  "GET /api/projects/{project_id}/canvas": null;
  "PUT /api/projects/{project_id}/canvas": CanvasIn;
  "GET /api/projects/{project_id}/characters": null;
  "POST /api/projects/{project_id}/characters": CharacterIn;
  "POST /api/projects/{project_id}/concat": ConcatIn;
  "GET /api/projects/{project_id}/costume-suggestions": null;
  "POST /api/projects/{project_id}/costume-suggestions/{suggestion_id}/accept": CostumeSuggestionAcceptIn;
  "POST /api/projects/{project_id}/costume-suggestions/{suggestion_id}/reject": null;
  "GET /api/projects/{project_id}/episodes": null;
  "POST /api/projects/{project_id}/episodes/replan": EpisodeReplanIn;
  "GET /api/projects/{project_id}/gate-stats": null;
  "POST /api/projects/{project_id}/nodes/{node_id}/restore": RestoreIn;
  "GET /api/projects/{project_id}/orphan-versions": null;
  "POST /api/projects/{project_id}/prompts/optimize": PromptOptimizeIn;
  "GET /api/projects/{project_id}/props": null;
  "POST /api/projects/{project_id}/props": PropIn;
  "GET /api/projects/{project_id}/reroll-reasons": null;
  "GET /api/projects/{project_id}/scenes": null;
  "POST /api/projects/{project_id}/scenes": SceneIn;
  "GET /api/projects/{project_id}/script": null;
  "POST /api/projects/{project_id}/script/expand": ScriptExpandIn;
  "POST /api/projects/{project_id}/script/extract": FormData;
  "POST /api/projects/{project_id}/script/parse": ScriptParseIn;
  "GET /api/projects/{project_id}/script/requirements": null;
  "GET /api/projects/{project_id}/script/versions": null;
  "GET /api/projects/{project_id}/sessions": null;
  "POST /api/projects/{project_id}/sessions": SessionIn;
  "GET /api/prompts": null;
  "GET /api/prompts/{name}": null;
  "PUT /api/prompts/{name}": PromptSaveIn;
  "POST /api/prompts/{name}/restore": null;
  "DELETE /api/props/{prop_id}": null;
  "PATCH /api/props/{prop_id}": PropPatchIn;
  "POST /api/props/{prop_id}/cancel": null;
  "POST /api/props/{prop_id}/generate": SceneGenerateIn;
  "POST /api/props/{prop_id}/image/prefer": PropPreferIn;
  "GET /api/providers": null;
  "PUT /api/providers": ProviderIn;
  "GET /api/providers/models": null;
  "POST /api/scene-groups/apply": SceneGroupApplyIn;
  "PATCH /api/scene-groups/{group_id}": SceneGroupPatchIn;
  "DELETE /api/scenes/{scene_id}": null;
  "PATCH /api/scenes/{scene_id}": ScenePatchIn;
  "POST /api/scenes/{scene_id}/apply-image": ApplyImageIn;
  "POST /api/scenes/{scene_id}/cancel": null;
  "POST /api/scenes/{scene_id}/extra-view": SceneExtraViewIn;
  "POST /api/scenes/{scene_id}/generate": SceneGenerateIn;
  "DELETE /api/scenes/{scene_id}/image": null;
  "POST /api/scenes/{scene_id}/image/prefer": ScenePreferIn;
  "GET /api/scenes/{scene_id}/scene-group": null;
  "POST /api/script/versions/{version_id}/restore": null;
  "PATCH /api/sessions/{session_id}": SessionPatchIn;
  "POST /api/sessions/{session_id}/chat": ChatIn;
  "GET /api/sessions/{session_id}/messages": null;
  "PATCH /api/shots/{shot_id}": ShotPatchIn;
  "GET /api/shots/{shot_id}/generation-params": null;
  "POST /api/shots/{shot_id}/prefer-first-frame": FirstFramePreferIn;
  "POST /api/shots/{shot_id}/regenerate-video-prompt": RegeneratePromptIn | null;
  "GET /api/shots/{shot_id}/reviews": null;
  "POST /api/shots/{shot_id}/verdict": ShotVerdictIn;
  "GET /api/skills": null;
  "POST /api/skills": SkillCreateIn;
  "DELETE /api/skills/{filename}": null;
  "GET /api/skills/{filename}": null;
  "PUT /api/skills/{filename}": SkillSaveIn;
  "POST /api/skills/{filename}/restore": null;
  "GET /api/system/deps": null;
  "POST /api/system/deps/install": null;
  "GET /api/system/gpu": null;
  "GET /api/system/info": null;
  "GET /api/tasks": null;
  "POST /api/tasks": TaskIn;
  "GET /api/tasks/{task_id}": null;
  "POST /api/tasks/{task_id}/cancel": null;
  "GET /files/{rel_path}": null;
}

