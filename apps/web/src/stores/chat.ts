/** Agent 会话状态：多会话 + 消息流 + 计划确认 + 生成结果自动落画布（M2 5.1/5.2/5.5）。 */

import type { Node } from "@xyflow/react";
import { create } from "zustand";
import {
  api,
  type ChatAction,
  type ChatMessage,
  type ChatMessageRef,
  type ChatPlan,
  type SessionInfo,
} from "../api/client";
import { useCanvas, type StudioNodeData } from "./canvas";

/** 输入引用暂存（@选择器选中后暂存于输入区，发送时随消息提交） */
export interface ChatRefInput {
  nodeId: string;
  label: string;
  versionId?: string;
}

/** 待确认计划（携带触发原文，「调整」时回填输入框） */
export interface PendingPlan {
  plan: ChatPlan;
  text: string;
}

interface ChatState {
  projectId: string | null;
  sessions: SessionInfo[];
  activeSessionId: string | null;
  messages: ChatMessage[];
  sending: boolean;
  error: string;
  planPending: PendingPlan | null;
  refInput: ChatRefInput[];
  paneSide: "left" | "right";
  collapsed: boolean;
  /** 输入框草稿（会话切换清空，取消计划时回填） */
  draft: string;

  loadSessions: (projectId: string) => Promise<void>;
  createSession: (title?: string) => Promise<void>;
  switchSession: (sessionId: string) => Promise<void>;
  /** 发送消息；planPending 存在时发送等于确认当前计划 */
  send: (text: string) => Promise<void>;
  confirmPlan: () => Promise<void>;
  /** 调整：取消待确认计划，触发原文回填输入框 */
  cancelPlan: () => void;
  addRefInput: (ref: ChatRefInput) => void;
  removeRefInput: (nodeId: string) => void;
  toggleCollapse: () => void;
  setPaneSide: (side: "left" | "right") => void;
  setDraft: (text: string) => void;
}

/** Agent 返回的 actions 写入画布：节点结构与手工创建一致（type studio，v0.2 4.2/5.2） */
function applyActions(actions: ChatAction[]) {
  if (!actions.length) return;
  for (const action of actions) {
    const payload = action.node;
    if (!payload) continue;
    const data: StudioNodeData = {
      // node.type 是 React Flow 类型（studio），真实节点类型在 data.specType
      specType: payload.data?.specType ?? payload.type,
      label: payload.data?.label ?? payload.data?.specType ?? payload.type,
      params: payload.data?.params ?? {},
      refs: payload.data?.refs ?? [],
      taskId: action.task_id ?? null,
    };
    const existing = useCanvas.getState().nodes.find((n) => n.id === payload.id);
    if (existing) {
      // 节点已存在（Agent 更新场景）合并数据
      useCanvas.getState().updateNodeData(payload.id, data);
    } else {
      const node: Node<StudioNodeData> = {
        id: payload.id,
        type: "studio",
        position: payload.position,
        data,
      };
      useCanvas.setState((state) => ({ nodes: [...state.nodes, node], dirty: true }));
    }
  }
  // 已排队任务走画布既有任务联动（WebSocket 进度 + refreshTasks）
  if (actions.some((a) => a.task_id)) {
    useCanvas.getState().refreshTasks().catch(() => {});
  }
}

/** 发送消息并处理响应（本地回显/计划确认/动作落画布），send 与 confirmPlan 共用 */
async function postChat(text: string, refs: ChatMessageRef[], confirmed: boolean) {
  const state = useChat.getState();
  const sessionId = state.activeSessionId;
  if (!sessionId || state.sending) return;
  useChat.setState({ sending: true, error: "" });
  try {
    const res = await api.sendChat(sessionId, { text, refs, confirmed });
    const now = Date.now();
    // 本地回显用户消息与助手回复（会话历史以 listMessages 为准）
    useChat.setState({
      messages: [
        ...useChat.getState().messages,
        {
          id: `local_${now}`,
          session_id: sessionId,
          role: "user",
          content: text,
          refs,
          tool_calls: [],
          created_at: now,
        },
        {
          id: res.message_id,
          session_id: sessionId,
          role: "assistant",
          content: res.reply,
          refs: [],
          tool_calls: [],
          created_at: now + 1,
        },
      ],
      refInput: [],
      // 返回计划且无动作 = 待确认；否则视为已执行（5.5）
      planPending:
        res.plan && res.actions.length === 0 ? { plan: res.plan, text } : null,
    });
    if (res.actions.length) applyActions(res.actions);
  } catch (e) {
    useChat.setState({ error: e instanceof Error ? e.message : String(e) });
  } finally {
    useChat.setState({ sending: false });
  }
}

export const useChat = create<ChatState>((set, get) => ({
  projectId: null,
  sessions: [],
  activeSessionId: null,
  messages: [],
  sending: false,
  error: "",
  planPending: null,
  refInput: [],
  paneSide: "right",
  collapsed: false,
  draft: "",

  loadSessions: async (projectId) => {
    set({
      projectId,
      sessions: [],
      activeSessionId: null,
      messages: [],
      planPending: null,
      refInput: [],
      draft: "",
      error: "",
    });
    if (!projectId) return;
    try {
      const sessions = await api.listSessions(projectId);
      set({ sessions });
      const first = sessions.find((s) => s.status === "active") ?? sessions[0];
      if (first) {
        await get().switchSession(first.id);
      } else {
        // 项目尚无会话：自动创建首个会话，保证输入即用（5.1）
        await get().createSession();
      }
    } catch {
      /* 会话接口未就绪时保持空态（后端并行开发） */
    }
  },

  createSession: async (title) => {
    const projectId = get().projectId;
    if (!projectId) return;
    try {
      const session = await api.createSession(projectId, title);
      set({ sessions: [...get().sessions, session] });
      await get().switchSession(session.id);
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
    }
  },

  switchSession: async (sessionId) => {
    set({
      activeSessionId: sessionId,
      messages: [],
      planPending: null,
      refInput: [],
      draft: "",
      error: "",
    });
    // 会话自带面板位置偏好（sessions.pane_side，v0.2 5.1）
    const session = get().sessions.find((s) => s.id === sessionId);
    if (session) set({ paneSide: session.pane_side });
    try {
      set({ messages: await api.listMessages(sessionId) });
    } catch {
      /* 新会话无历史消息 */
    }
  },

  send: async (text) => {
    const { activeSessionId, planPending, refInput, sending } = get();
    if (!activeSessionId || !text.trim() || sending) return;
    const refs: ChatMessageRef[] = refInput.map((r) => ({
      node_id: r.nodeId,
      version_id: r.versionId,
    }));
    // 计划待确认时，发送等于确认（5.5）
    await postChat(text, refs, !!planPending);
  },

  confirmPlan: async () => {
    if (!get().planPending || get().sending) return;
    await postChat("确认执行", [], true);
  },

  cancelPlan: () => {
    const pending = get().planPending;
    set({ planPending: null, draft: pending?.text ?? get().draft });
  },

  addRefInput: (ref) => {
    if (get().refInput.some((r) => r.nodeId === ref.nodeId)) return;
    set({ refInput: [...get().refInput, ref] });
  },

  removeRefInput: (nodeId) => {
    set({ refInput: get().refInput.filter((r) => r.nodeId !== nodeId) });
  },

  toggleCollapse: () => set({ collapsed: !get().collapsed }),

  setPaneSide: (side) => {
    set({ paneSide: side });
    // 位置偏好随会话持久化（失败静默，接口未就绪不影响本地切换）
    const sessionId = get().activeSessionId;
    if (sessionId) {
      api.updateSession(sessionId, { pane_side: side }).catch(() => {});
    }
  },

  setDraft: (text) => set({ draft: text }),
}));
