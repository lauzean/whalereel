/** 画布状态：节点/边 + 版本（抽卡）+ @引用 + 任务联动（v0.2 4.5/4.6）。 */

import type { Edge, Node } from "@xyflow/react";
import { create } from "zustand";
import { useBilling } from "./billing";
import {
  api,
  type CameraMove,
  type NodeVersionInfo,
  type ProjectAssetInfo,
  type TaskInfo,
} from "../api/client";
import { fetchAudioCaps } from "../api/modelOptions";

/** @引用：ID 级引用（提示词标签渲染，避免改名失效） */
export interface MentionRef {
  nodeId: string;
  label: string;
}

export interface StudioNodeData extends Record<string, unknown> {
  specType: string;
  label: string;
  params: Record<string, string | number>;
  /** @引用列表（生成时注入参考） */
  refs: MentionRef[];
  /** 当前生效版本（后续引用以它为准） */
  selectedVersionId?: string | null;
  /** 最近一次产物（= selected 版本的 artifact） */
  artifactId?: string | null;
  artifactKind?: string;
  /** 最近一次任务 */
  taskId?: string | null;
  /** 上游引用变更后标记「可更新」（不自动重跑） */
  stale?: boolean;
}

/** 画布打组（4.10）：组是成员容器，随 graph_json 持久化；组框为合成节点（id 以 group_ 开头）。 */
export interface GroupInfo {
  id: string;
  name: string;
  color: string;
  nodeIds: string[];
}

/** 分组底色预设（8 色） */
export const GROUP_COLORS = [
  "#ef4444", "#f97316", "#eab308", "#22c55e",
  "#06b6d4", "#3b82f6", "#8b5cf6", "#ec4899",
];

interface CanvasState {
  projectId: string | null;
  nodes: Node<StudioNodeData>[];
  edges: Edge[];
  tasks: Record<string, TaskInfo>; // node_id -> 最新任务
  versions: Record<string, NodeVersionInfo[]>; // node_id -> 抽卡记录
  cameraMoves: CameraMove[];
  /** 模型 id → 是否有原生音频（画布视频节点徽标）。**未列入 = 未知**，不显示断言 */
  audioCaps: Record<string, boolean>;
  assets: ProjectAssetInfo[]; // 项目产物资产库（含已删除节点的产物）
  groups: Record<string, GroupInfo>; // 画布分组（4.10）
  dirty: boolean;

  // 画布导航（左下角菜单栏）
  hideEdges: boolean;
  snapToGrid: boolean;
  showMiniMap: boolean;
  toggleHideEdges: () => void;
  toggleSnapToGrid: () => void;
  toggleMiniMap: () => void;

  setProject: (id: string) => Promise<void>;
  addNode: (specType: string, position: { x: number; y: number }, connectFrom?: string) => string;
  updateNodeParam: (nodeId: string, paramId: string, value: string | number) => void;
  updateNodeData: (nodeId: string, patch: Partial<StudioNodeData>) => void;
  onConnect: (edge: Edge) => void;
  deleteNode: (nodeId: string) => void;
  setNodes: (nodes: Node<StudioNodeData>[]) => void;
  setEdges: (edges: Edge[]) => void;
  save: (opts?: { keepalive?: boolean }) => Promise<void>;

  // 资产库（三方联动：找回产物 / 插入画布）
  loadAssets: () => Promise<void>;
  insertAssetNode: (asset: ProjectAssetInfo, position?: { x: number; y: number }) => Promise<void>;

  // 画布打组（4.10）
  createGroup: (nodeIds: string[]) => void;
  dissolveGroup: (groupId: string) => void;
  renameGroup: (groupId: string, name: string) => void;
  setGroupColor: (groupId: string, color: string) => void;
  deleteGroupNodes: (groupId: string) => void;
  refreshGroupBox: (groupId: string) => void;

  // 版本（抽卡）
  loadVersions: (nodeId: string) => Promise<NodeVersionInfo[]>;
  selectVersion: (nodeId: string, versionId: string) => void;
  uploadFile: (nodeId: string, file: File) => Promise<void>;
  /** 编辑工具产物落点：追加版本并切为当前生效（4.9 编辑即新版本） */
  applyEdited: (nodeId: string, version: NodeVersionInfo) => void;
  addRef: (nodeId: string, ref: MentionRef) => void;
  removeRef: (nodeId: string, refNodeId: string) => void;

  refreshTasks: () => Promise<void>;
  /** 单任务状态直写 store（WS 事件与轮询兜底共用的合并逻辑） */
  applyTask: (t: TaskInfo) => void;
  handleTaskEvent: (event: string, data: Record<string, unknown>) => void;
  /** WS 实时通道连接状态（bottom-nav 状态点展示，断线时用户可感知） */
  wsConnected: boolean;
  setWsConnected: (v: boolean) => void;
  loadCameraMoves: () => Promise<void>;
  loadAudioCaps: () => Promise<void>;
}

let seq = 1;

/** 组框合成节点：仅作成员容器的可视外框，保存时过滤。 */
function buildGroupNode(g: GroupInfo): Node<StudioNodeData> {
  return {
    id: g.id,
    type: "group",
    position: { x: 0, y: 0 },
    zIndex: -1,
    data: { specType: "group", label: g.name, params: {}, refs: [] },
  };
}

/** 依据成员包围盒推算组框位置与尺寸（带留白与标题高度）。 */
function groupBoxOf(members: Node<StudioNodeData>[]): {
  position: { x: number; y: number };
  style: { width: number; height: number };
} {
  const PAD = 28;
  const LABEL_H = 34;
  const minX = Math.min(...members.map((n) => n.position.x));
  const minY = Math.min(...members.map((n) => n.position.y));
  const maxX = Math.max(
    ...members.map((n) => n.position.x + (n.measured?.width ?? n.width ?? 260)),
  );
  const maxY = Math.max(
    ...members.map((n) => n.position.y + (n.measured?.height ?? n.height ?? 200)),
  );
  return {
    position: { x: minX - PAD, y: minY - PAD - LABEL_H },
    style: {
      width: maxX - minX + PAD * 2,
      height: maxY - minY + PAD * 2 + LABEL_H,
    },
  };
}

export const useCanvas = create<CanvasState>((set, get) => ({
  projectId: null,
  nodes: [],
  edges: [],
  tasks: {},
  versions: {},
  cameraMoves: [],
  audioCaps: {},
  assets: [],
  groups: {},
  dirty: false,
  hideEdges: false,
  snapToGrid: false,
  showMiniMap: true,

  toggleHideEdges: () => set({ hideEdges: !get().hideEdges }),
  toggleSnapToGrid: () => set({ snapToGrid: !get().snapToGrid }),
  toggleMiniMap: () => set({ showMiniMap: !get().showMiniMap }),

  setProject: async (id) => {
    set({
      projectId: id || null, nodes: [], edges: [], tasks: {}, versions: {},
      groups: {}, dirty: false,
    });
    if (!id) return;
    try {
      const { graph } = await api.getCanvas(id);
      const groups = (graph.groups as Record<string, GroupInfo>) || {};
      const nodes = ((graph.nodes as Node<StudioNodeData>[]) || []).map((n) => ({
        ...n,
        data: { ...n.data, refs: n.data?.refs ?? [] },
      }));
      // 重建组框合成节点并按成员包围盒定位
      const groupNodes: Node<StudioNodeData>[] = [];
      const nextNodes = [...nodes];
      for (const g of Object.values(groups)) {
        const members = nodes.filter((n) => g.nodeIds.includes(n.id));
        if (!members.length) continue;
        const box = groupBoxOf(members);
        groupNodes.push({ ...buildGroupNode(g), ...box });
      }
      set({
        nodes: [...nextNodes, ...groupNodes],
        edges: (graph.edges as Edge[]) || [],
        groups,
      });
      // 恢复各节点版本（抽卡记录）
      await Promise.all(
        nodes.map((n) => get().loadVersions(n.id).catch(() => [])),
      );
    } catch {
      /* 新项目空画布 */
    }
    await get().refreshTasks();
    await get().loadCameraMoves();
    await get().loadAssets();
  },

  addNode: (specType, position, connectFrom) => {
    const id = `n${seq++}_${Date.now().toString(36)}`;
    const node: Node<StudioNodeData> = {
      id,
      type: "studio",
      position,
      data: { specType, label: specType, params: {}, refs: [] },
    };
    set({ nodes: [...get().nodes, node], dirty: true });
    // 悬停「+」创建：自动连线（connectFrom 为参考输入的来源）
    if (connectFrom) {
      const edge: Edge = {
        id: `e_${Date.now().toString(36)}`,
        source: connectFrom,
        target: id,
        targetHandle: "ref",
        sourceHandle: "out",
      };
      set({ edges: [...get().edges, edge], dirty: true });
    }
    return id;
  },

  updateNodeParam: (nodeId, paramId, value) => {
    set({
      nodes: get().nodes.map((n) =>
        n.id === nodeId
          ? { ...n, data: { ...n.data, params: { ...n.data.params, [paramId]: value } } }
          : n,
      ),
      dirty: true,
    });
  },

  updateNodeData: (nodeId, patch) => {
    set({
      nodes: get().nodes.map((n) => (n.id === nodeId ? { ...n, data: { ...n.data, ...patch } } : n)),
      dirty: true,
    });
  },

  onConnect: (edge) => {
    set({ edges: [...get().edges, { ...edge }], dirty: true });
  },

  deleteNode: (nodeId) => {
    // 同步移出所在分组；组被删空则自动解散（4.10）
    const groups = { ...get().groups };
    let touched = false;
    for (const g of Object.values(groups)) {
      if (g.nodeIds.includes(nodeId)) {
        groups[g.id] = { ...g, nodeIds: g.nodeIds.filter((x) => x !== nodeId) };
        touched = true;
      }
    }
    set({
      nodes: get().nodes.filter((n) => n.id !== nodeId),
      edges: get().edges.filter((e) => e.source !== nodeId && e.target !== nodeId),
      groups,
      dirty: true,
    });
    if (touched) {
      for (const g of Object.values(groups)) get().refreshGroupBox(g.id);
    }
  },

  setNodes: (nodes) => set({ nodes }),
  setEdges: (edges) => set({ edges }),

  save: async (opts?: { keepalive?: boolean }) => {
    const { projectId, nodes, edges, groups } = get();
    if (!projectId) return;
    await api.saveCanvas(projectId, {
      // 组框是合成节点不入库；分组随 graph 顶层 groups 字段持久化
      nodes: nodes
        .filter((n) => !n.id.startsWith("group_"))
        .map((n) => ({ ...n, selected: false })),
      edges,
      groups,
    }, Boolean(opts?.keepalive));
    set({ dirty: false });
  },

  // ---------- 资产库（三方联动） ----------

  loadAssets: async () => {
    const projectId = get().projectId;
    if (!projectId) return;
    try {
      const assets = await api.listAssets(projectId);
      set({ assets });
    } catch {
      /* 资产库加载失败不阻塞画布 */
    }
  },

  insertAssetNode: async (asset, position) => {
    const projectId = get().projectId;
    if (!projectId) return;
    const specType = asset.kind === "video" ? "video" : asset.kind === "audio" ? "audio" : "image";
    let pos = position;
    if (!pos) {
      // 缺省落点：现有节点右下方偏移，避免重叠
      const maxX = Math.max(0, ...get().nodes.map((n) => n.position.x));
      const maxY = Math.max(0, ...get().nodes.map((n) => n.position.y));
      pos = { x: maxX + 80, y: maxY + 80 };
    }
    const id = `n${seq++}_${Date.now().toString(36)}`;
    set({
      nodes: [
        ...get().nodes,
        { id, type: "studio", position: pos, data: { specType, label: specType, params: {}, refs: [] } },
      ],
      dirty: true,
    });
    const version = await api.restoreAsset(projectId, id, asset.artifact_id);
    set({ versions: { ...get().versions, [id]: [version] } });
    if (version.artifact_id) {
      get().updateNodeData(id, {
        selectedVersionId: version.id,
        artifactId: version.artifact_id,
        artifactKind: version.kind ?? undefined,
      });
    }
  },

  // ---------- 画布打组（4.10） ----------

  createGroup: (nodeIds) => {
    const ids = nodeIds.filter((x) => !x.startsWith("group_"));
    if (ids.length < 2) return;
    const id = `group_${seq++}_${Date.now().toString(36)}`;
    const count = Object.keys(get().groups).length;
    const group: GroupInfo = {
      id,
      name: `分组 ${count + 1}`,
      color: GROUP_COLORS[count % GROUP_COLORS.length],
      nodeIds: ids,
    };
    set({
      nodes: [...get().nodes, buildGroupNode(group)],
      groups: { ...get().groups, [id]: group },
      dirty: true,
    });
    get().refreshGroupBox(id);
  },

  dissolveGroup: (groupId) => {
    set({
      nodes: get().nodes.filter((n) => n.id !== groupId),
      groups: Object.fromEntries(
        Object.entries(get().groups).filter(([k]) => k !== groupId),
      ),
      dirty: true,
    });
  },

  renameGroup: (groupId, name) => {
    const g = get().groups[groupId];
    if (!g) return;
    set({
      groups: { ...get().groups, [groupId]: { ...g, name } },
      nodes: get().nodes.map((n) => (n.id === groupId ? { ...n, data: { ...n.data, label: name } } : n)),
      dirty: true,
    });
  },

  setGroupColor: (groupId, color) => {
    const g = get().groups[groupId];
    if (!g) return;
    set({ groups: { ...get().groups, [groupId]: { ...g, color } }, dirty: true });
  },

  deleteGroupNodes: (groupId) => {
    const g = get().groups[groupId];
    if (!g) return;
    for (const id of [...g.nodeIds]) get().deleteNode(id);
    get().dissolveGroup(groupId);
  },

  refreshGroupBox: (groupId) => {
    const g = get().groups[groupId];
    if (!g) return;
    const members = get().nodes.filter((n) => g.nodeIds.includes(n.id));
    if (!members.length) {
      get().dissolveGroup(groupId);
      return;
    }
    const box = groupBoxOf(members);
    set({
      nodes: get().nodes.map((n) => (n.id === groupId ? { ...n, ...box } : n)),
      dirty: true,
    });
  },

  // ---------- 版本（抽卡） ----------

  loadVersions: async (nodeId) => {
    const projectId = get().projectId;
    if (!projectId) return [];
    const list = await api.listVersions(projectId, nodeId);
    set({ versions: { ...get().versions, [nodeId]: list } });
    // 若节点尚无 selected（新加载/上传后），选最新版本生效
    const node = get().nodes.find((n) => n.id === nodeId);
    if (node && list.length && !node.data.selectedVersionId) {
      const latest = list[list.length - 1];
      if (latest.artifact_id) {
        get().updateNodeData(nodeId, {
          selectedVersionId: latest.id,
          artifactId: latest.artifact_id,
          artifactKind: latest.kind ?? undefined,
        });
      }
    }
    return list;
  },

  selectVersion: (nodeId, versionId) => {
    const list = get().versions[nodeId] || [];
    const v = list.find((x) => x.id === versionId);
    if (!v) return;
    get().updateNodeData(nodeId, {
      selectedVersionId: versionId,
      artifactId: v.artifact_id,
      artifactKind: v.kind ?? undefined,
    });
    // 下游已生成节点标记 stale（不自动重跑，保护已有结果）
    set({
      nodes: get().nodes.map((n) => {
        const hasRef = n.data.refs?.some((r) => r.nodeId === nodeId);
        const isDownstream = get().edges.some(
          (e) => e.source === nodeId && e.target === n.id,
        );
        return hasRef || isDownstream ? { ...n, data: { ...n.data, stale: true } } : n;
      }),
    });
  },

  uploadFile: async (nodeId, file) => {
    const projectId = get().projectId;
    if (!projectId) return;
    const version = await api.uploadVersion(projectId, nodeId, file);
    set({ versions: { ...get().versions, [nodeId]: [...(get().versions[nodeId] || []), version] } });
    if (version.artifact_id) {
      get().updateNodeData(nodeId, {
        selectedVersionId: version.id,
        artifactId: version.artifact_id,
        artifactKind: version.kind ?? undefined,
      });
    }
  },

  applyEdited: (nodeId, version) => {
    set({
      versions: { ...get().versions, [nodeId]: [...(get().versions[nodeId] || []), version] },
      dirty: true,
    });
    if (version.artifact_id) {
      get().updateNodeData(nodeId, {
        selectedVersionId: version.id,
        artifactId: version.artifact_id,
        artifactKind: version.kind ?? undefined,
      });
    }
  },

  addRef: (nodeId, ref) => {
    const node = get().nodes.find((n) => n.id === nodeId);
    if (!node) return;
    if (node.data.refs.some((r) => r.nodeId === ref.nodeId)) return;
    get().updateNodeData(nodeId, { refs: [...node.data.refs, ref] });
  },

  removeRef: (nodeId, refNodeId) => {
    const node = get().nodes.find((n) => n.id === nodeId);
    if (!node) return;
    get().updateNodeData(nodeId, { refs: node.data.refs.filter((r) => r.nodeId !== refNodeId) });
  },

  // ---------- 任务 ----------

  refreshTasks: async () => {
    const projectId = get().projectId;
    if (!projectId) return;
    const tasks = await api.listTasks(projectId);
    const byNode: Record<string, TaskInfo> = {};
    // 按创建时间倒序，最新任务胜出
    for (const t of tasks) {
      if (!byNode[t.node_id]) byNode[t.node_id] = t;
    }
    set({ tasks: byNode });
  },

  wsConnected: false,
  setWsConnected: (v) => set({ wsConnected: v }),

  applyTask: (t) => {
    const nodeId = t.node_id;
    if (!nodeId) return;
    set((state) => ({
      tasks: {
        ...state.tasks,
        [nodeId]: {
          ...(state.tasks[nodeId] as TaskInfo),
          ...t,
          node_id: nodeId,
        },
      },
      // done 后产物以 selected 版本为准（version_created 事件会刷新）
      nodes: state.nodes.map((n) => {
        if (n.id !== nodeId) return n;
        const patch: Partial<StudioNodeData> = { taskId: t.id };
        if (t.status === "done" && t.version_id) {
          patch.selectedVersionId = t.version_id;
          patch.stale = false;
          if (t.artifact_id) patch.artifactId = t.artifact_id;
        }
        return { ...n, data: { ...n.data, ...patch } };
      }),
    }));
  },

  handleTaskEvent: (event, data) => {
    if (event === "version_created") {
      const nodeId = data.node_id as string;
      if (nodeId) {
        get().loadVersions(nodeId).catch(() => {});
      }
      return;
    }
    if (event === "connected" || event === "disconnected") {
      get().setWsConnected(event === "connected");
      if (event === "connected") void get().refreshTasks(); // 重连兜底：补拉断线期间错过的状态
      return;
    }
    const nodeId = data.node_id as string | undefined;
    // 计费（33 号方案）：任务到终态时余额可能变化（计费成立 / 失败自动退款），
    // 主动重拉一次余额（账目无项目维度、不开新 WS 频道）
    const status = data.status as string | undefined;
    if (status && ["done", "failed", "canceled", "cached"].includes(status)) {
      void useBilling.getState().refreshAccount();
    }
    if (!nodeId) {
      get().refreshTasks();
      return;
    }
    get().applyTask(data as unknown as TaskInfo);
  },

  loadCameraMoves: async () => {
    if (get().cameraMoves.length) return;
    try {
      const moves = await api.cameraMoves();
      set({ cameraMoves: moves });
    } catch {
      /* 运镜库不可用时仅禁用选择器 */
    }
  },

  loadAudioCaps: async () => {
    if (Object.keys(get().audioCaps).length) return;
    try {
      set({ audioCaps: await fetchAudioCaps() });
    } catch {
      /* 拿不到音频能力时徽标整体不显示（未知不声称，绝不当成「无音频」） */
    }
  },
}));
