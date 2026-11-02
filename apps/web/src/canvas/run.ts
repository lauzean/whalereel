/** 节点执行编排：生成动作由参考端口自动判定（v0.2 4.8）。

动作矩阵：
  图片节点：无参考=t2i；图片参考=i2i
  视频节点：无参考=t2v；1张图=i2v(首帧)；2张图=fl2v(首尾帧)；视频参考/混合=ref2v
  音频节点：tts（台词=自身 prompt + 上游 text）
  拼接/导出：compose
*/

import { api, type TaskInfo } from "../api/client";
import { isEndpointModelId, peekKnownModelIds } from "../api/modelOptions";
import { useCanvas } from "../stores/canvas";
import { toast } from "../ui/feedback";
import { NODE_SPECS } from "./specs";

const TERMINAL = new Set(["done", "failed", "cached", "canceled"]);

interface ResolvedAction {
  runner: string;
  modelId: string;
  params: Record<string, unknown>;
  inputRefs: Record<string, unknown>;
}

/** 收集上游参考（连线）与 @引用 的产物，按类型分组。 */
function collectInputs(nodeId: string) {
  const state = useCanvas.getState();
  const node = state.nodes.find((n) => n.id === nodeId);
  if (!node) return null;

  const images: string[] = [];
  const videos: string[] = [];
  const audios: string[] = [];
  const texts: string[] = [];

  const pushArtifact = (sourceId: string) => {
    const source = state.nodes.find((n) => n.id === sourceId);
    if (!source) return;
    const kind = source.data.artifactKind;
    const artifact = source.data.artifactId;
    if (artifact && kind === "image") images.push(artifact);
    else if (artifact && kind === "video") videos.push(artifact);
    else if (artifact && kind === "audio") audios.push(artifact);
    else if (source.data.specType === "text") {
      const t = String(source.data.params.text ?? "");
      if (t) texts.push(t);
    }
  };

  // 连线：以该节点为 target 的边（左入 = 参考输入）
  const incoming = state.edges.filter((e) => e.target === nodeId);
  const ordered = incoming.sort((a, b) =>
    (state.nodes.find((n) => n.id === a.source)?.position.x ?? 0) -
    (state.nodes.find((n) => n.id === b.source)?.position.x ?? 0),
  );
  for (const edge of ordered) pushArtifact(edge.source);

  // @引用：节点 data.refs（ID 级，取 selected 版本 artifact）
  for (const ref of node.data.refs ?? []) pushArtifact(ref.nodeId);

  return { node, images, videos, audios, texts };
}

/** 收集节点规格中模型下拉的全部可选值（旧节点模型迁移校验用）。 */
function specModelOptions(spec: { params: { id: string; options?: { value: string }[] }[] }): Set<string> {
  const param = spec.params.find((p) => p.id === "model");
  return new Set((param?.options ?? []).map((o) => o.value));
}

/** 判定生成动作（v0.2 4.8 生成能力矩阵）。 */
function resolveAction(nodeId: string): ResolvedAction | null {
  const state = useCanvas.getState();
  const collected = collectInputs(nodeId);
  if (!collected) return null;
  const { node, images, videos, audios, texts } = collected;
  const spec = NODE_SPECS[node.data.specType];
  if (!spec) return null;

  const params: Record<string, unknown> = { ...node.data.params };
  delete params.model;
  // 旧节点可能残留已下架的模型 ID（如 flux-1/dev）：不在可选列表且不在已知
  // 模型集合（本地注册表 + provider 模型）中，才迁移为默认模型——provider/项目
  // 预设模型 id 与端点编码串（端点::模型）不在静态下拉表，放行不重置；已知
  // 集合未预热时也全部放行（宁可放弃迁移也不吞掉用户选择的模型，过期 id 由
  // 后端预检明确报错）
  const rawModel = String(node.data.params.model || spec.defaultModel || "");
  const allowed = specModelOptions(spec);
  const known = peekKnownModelIds();
  const modelId = allowed.size > 0 && !allowed.has(rawModel)
    && !isEndpointModelId(rawModel) && (!known || !known.has(rawModel))
    ? String(spec.defaultModel || "")
    : rawModel;
  // seed 留空 = 随机：空串不提交（后端按缺省随机处理）
  if (String(params.seed ?? "").trim() === "") delete params.seed;

  // 运镜片段注入提示词
  const cameraMoveId = String(params.camera_move || "");
  delete params.camera_move;
  if (cameraMoveId) {
    const move = state.cameraMoves.find((m) => m.id === cameraMoveId);
    if (move) params.prompt = `${params.prompt ?? ""}，${move.prompt}`;
  }

  // 上游 text 节点内容合入提示词（自身 prompt 优先）
  if (texts.length && spec.type !== "audio") {
    params.prompt = params.prompt || texts.join("\n");
  }

  const inputRefs: Record<string, unknown> = {};

  switch (spec.type) {
    case "image": {
      if (images.length === 0) {
        return { runner: "t2i", modelId, params, inputRefs };
      }
      inputRefs.image = images[0];
      if (images.length > 1) inputRefs.extra_images = images.slice(1);
      return { runner: "i2i", modelId, params, inputRefs };
    }
    case "video": {
      if (videos.length > 0) {
        // 参考生视频（H3 Ref2VA：角色一致性主力路径）
        inputRefs.videos = videos;
        if (images.length) inputRefs.images = images;
        return { runner: "ref2v", modelId, params, inputRefs };
      }
      if (images.length >= 2) {
        inputRefs.first = images[0];
        inputRefs.last = images[1];
        if (images.length > 2) inputRefs.extra_images = images.slice(2);
        return { runner: "fl2v", modelId, params, inputRefs };
      }
      if (images.length === 1) {
        inputRefs.image = images[0];
        return { runner: "i2v", modelId, params, inputRefs };
      }
      return { runner: "t2v", modelId, params, inputRefs };
    }
    case "audio": {
      params.text = params.prompt || texts.join("\n") || params.text;
      delete params.prompt;
      return { runner: "tts", modelId, params, inputRefs };
    }
    case "timeline": {
      if (videos.length) inputRefs.clips = videos;
      if (audios.length) inputRefs.audio = audios;
      return { runner: "compose", modelId, params, inputRefs };
    }
    case "export": {
      if (videos.length) inputRefs.video = videos[0];
      return { runner: "compose", modelId, params, inputRefs };
    }
    default:
      return null;
  }
}

/** 单任务轮询兜底：WS 实时通道异常时保证进度/产物仍能更新到界面。

  WS 正常时任务很快出现在 store（applyTask 由 WS 事件驱动）；若 2 次轮询后
  store 里仍无该任务状态，视为实时通道异常，转为 2s 轮询直至终态，终态时
  全量刷新任务与版本（产物落点）。 */
async function pollTaskUntilSettled(taskId: string, nodeId: string): Promise<void> {
  for (let i = 0; i < 600; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    let t: TaskInfo;
    try {
      t = await api.getTask(taskId);
    } catch {
      continue; // 网络抖动：下一轮重试
    }
    const store = useCanvas.getState();
    store.applyTask(t);
    if (TERMINAL.has(t.status)) {
      await store.refreshTasks().catch(() => {});
      await store.loadVersions(nodeId).catch(() => {});
      return;
    }
  }
}

/** 单节点执行：提交任务（生成 = 追加抽卡版本）。失败 Toast 提示，不再静默。 */
export async function runNode(nodeId: string): Promise<string | null> {
  const state = useCanvas.getState();
  if (!state.projectId) {
    toast.warning("请先打开/创建项目后再生成");
    return null;
  }
  const action = resolveAction(nodeId);
  if (!action) {
    toast.warning("无法确定生成动作：请检查节点类型与参数");
    return null;
  }

  try {
    const { task_id: taskId } = await api.createTask({
      project_id: state.projectId,
      node_id: nodeId,
      runner: action.runner,
      model_id: action.modelId,
      params: action.params,
      input_refs: action.inputRefs,
    });
    await state.refreshTasks();
    // 后台兜底轮询：无论 WS 是否正常，任务状态与产物都会更新到界面
    void pollTaskUntilSettled(taskId, nodeId);
    return taskId;
  } catch (e) {
    toast.error(`生成提交失败：${e instanceof Error ? e.message : String(e)}`);
    return null;
  }
}

async function waitForTask(taskId: string): Promise<TaskInfo> {
  for (;;) {
    const task = await api.getTask(taskId);
    if (TERMINAL.has(task.status)) return task;
    await new Promise((r) => setTimeout(r, 1500));
  }
}

/** 拓扑序全量运行：失败节点阻断其下游。 */
export async function runAll(): Promise<void> {
  const state = useCanvas.getState();
  const { nodes, edges } = state;

  // Kahn 拓扑排序
  const inDeg = new Map<string, number>();
  const adj = new Map<string, string[]>();
  for (const n of nodes) {
    inDeg.set(n.id, 0);
    adj.set(n.id, []);
  }
  for (const e of edges) {
    if (!adj.has(e.source) || !inDeg.has(e.target)) continue;
    adj.get(e.source)!.push(e.target);
    inDeg.set(e.target, (inDeg.get(e.target) ?? 0) + 1);
  }
  const queue = nodes.filter((n) => (inDeg.get(n.id) ?? 0) === 0).map((n) => n.id);
  const order: string[] = [];
  while (queue.length) {
    const id = queue.shift()!;
    order.push(id);
    for (const next of adj.get(id) ?? []) {
      const d = inDeg.get(next)! - 1;
      inDeg.set(next, d);
      if (d === 0) queue.push(next);
    }
  }

  const failed = new Set<string>();
  for (const nodeId of order) {
    if (failed.has(nodeId)) continue;
    const node = nodes.find((n) => n.id === nodeId);
    const spec = node ? NODE_SPECS[node.data.specType] : null;
    if (!spec?.runnerHint) continue;

    const taskId = await runNode(nodeId);
    if (!taskId) continue;
    const task = await waitForTask(taskId);
    await useCanvas.getState().refreshTasks();
    if (task.status === "failed" || task.status === "canceled") {
      failed.add(nodeId);
      for (const next of adj.get(nodeId) ?? []) failed.add(next);
    }
  }
}
