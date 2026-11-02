/** Agent 对话面板：多会话标签页 + 消息流 + 计划确认卡 + @引用输入（M2 5.1/5.2/5.5）。
 *  位置：画布右侧（可换边/可收起，收起后留展开把手），生成结果经 chat store 自动落画布。 */

import { useReactFlow, type Node } from "@xyflow/react";
import { useEffect, useRef, useState } from "react";
import type { ChatMessage, ChatPlan } from "../api/client";
import { BILLING_KEYS } from "../billingKeys";
import { MentionPicker } from "../canvas/MentionPicker";
import { NODE_SPECS } from "../canvas/specs";
import { useCanvas, type StudioNodeData } from "../stores/canvas";
import { creditsFor, useCostTip } from "../stores/billing";
import { useChat } from "../stores/chat";

/** 预计耗时格式化（秒 → 秒/分/时） */
function formatDuration(seconds: number): string {
  if (seconds < 60) return `约 ${seconds} 秒`;
  if (seconds < 3600) return `约 ${Math.round(seconds / 60)} 分钟`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return m > 0 ? `约 ${h} 小时 ${m} 分` : `约 ${h} 小时`;
}

/** 引用 chip 文案：优先画布节点当前名称，节点不存在时退回 ID */
function refLabelOf(nodeId: string, nodes: Node<StudioNodeData>[]) {
  const node = nodes.find((n) => n.id === nodeId);
  const spec = node ? NODE_SPECS[node.data.specType] : undefined;
  return String(node?.data.params.name || spec?.title || nodeId.slice(0, 8));
}

// ---------- 面板头：会话标签页 + 新建/换边/收起 ----------

function ChatHeader() {
  const sessions = useChat((s) => s.sessions);
  const activeSessionId = useChat((s) => s.activeSessionId);
  const paneSide = useChat((s) => s.paneSide);
  const switchSession = useChat((s) => s.switchSession);
  const createSession = useChat((s) => s.createSession);
  const setPaneSide = useChat((s) => s.setPaneSide);
  const toggleCollapse = useChat((s) => s.toggleCollapse);

  return (
    <div className="chat-header">
      <div className="chat-tabs">
        {sessions.map((s) => (
          <button
            key={s.id}
            className={`chat-tab ${s.id === activeSessionId ? "on" : ""}`}
            title={s.title || "新对话"}
            onClick={() => void switchSession(s.id)}
          >
            {s.title || "新对话"}
          </button>
        ))}
        {sessions.length === 0 && <span className="chat-tabs-empty">暂无会话</span>}
      </div>
      <div className="chat-header-actions">
        <button title="新建对话" onClick={() => void createSession()}>
          新建
        </button>
        {/* chat-side-toggle：手机档对话面板恒为全屏浮层，「靠左/靠右」无从体现，
            由 styles-mobile.css 隐藏该按钮（桌面端不受影响） */}
        <button
          className="chat-side-toggle"
          title="切换面板位置"
          onClick={() => setPaneSide(paneSide === "right" ? "left" : "right")}
        >
          {paneSide === "right" ? "靠左" : "靠右"}
        </button>
        <button title="收起对话面板" onClick={toggleCollapse}>
          收起
        </button>
      </div>
    </div>
  );
}

// ---------- 消息流 ----------

function MessageBubble({ message }: { message: ChatMessage }) {
  const nodes = useCanvas((s) => s.nodes);
  const { fitView } = useReactFlow();

  /** 点击引用 chip 定位画布节点 */
  const locate = (nodeId: string) => {
    void fitView({ nodes: [{ id: nodeId }], duration: 400, maxZoom: 1 });
  };

  return (
    <div className={`chat-msg ${message.role}`}>
      <div className="chat-bubble">{message.content}</div>
      {message.refs.length > 0 && (
        <div className="chat-msg-refs">
          {message.refs.map((r) => (
            <button
              key={r.node_id}
              className="chat-ref-chip"
              title={`定位画布节点 ${r.node_id}`}
              onClick={() => locate(r.node_id)}
            >
              @{refLabelOf(r.node_id, nodes)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** 计划确认卡：确认执行 / 调整（取消计划回输入框，v0.2 5.5） */
function PlanCard({ plan }: { plan: ChatPlan }) {
  const confirmPlan = useChat((s) => s.confirmPlan);
  const cancelPlan = useChat((s) => s.cancelPlan);
  const sending = useChat((s) => s.sending);
  // 积分预估（33 号方案）：确认执行 = 一次对话调用 + 每个节点一次生成任务，
  // 按当前价目把合计摆在卡上（价目未加载/全免费时为 0，不显示）
  const perChat = creditsFor(BILLING_KEYS.chat) ?? 0;
  const perTask = creditsFor(BILLING_KEYS.createTask) ?? 0;
  const creditEstimate = perChat + plan.node_count * perTask;

  return (
    <div className="chat-plan-card">
      <div className="chat-plan-title">创作计划</div>
      <div className="chat-plan-rows">
        <span>节点数：{plan.node_count}</span>
        <span>模型：{plan.model}</span>
        <span>预计耗时：{formatDuration(plan.estimated_seconds)}</span>
        {creditEstimate > 0 && (
          <span title="本次执行（对话与生成任务）按当前价目的预估消耗">
            预计积分：约 {creditEstimate}
          </span>
        )}
      </div>
      {plan.summary && <div className="chat-plan-summary">{plan.summary}</div>}
      <div className="chat-plan-actions">
        <button className="chat-plan-adjust" onClick={cancelPlan}>
          调整
        </button>
        <button
          className="chat-plan-confirm"
          disabled={sending}
          onClick={() => void confirmPlan()}
        >
          确认执行
        </button>
      </div>
    </div>
  );
}

function ChatMessages() {
  const messages = useChat((s) => s.messages);
  const sending = useChat((s) => s.sending);
  const error = useChat((s) => s.error);
  const planPending = useChat((s) => s.planPending);
  const listRef = useRef<HTMLDivElement>(null);

  // 新消息/状态变化时滚动到底部
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, sending, planPending]);

  return (
    <div className="chat-messages" ref={listRef}>
      {messages.length === 0 && !sending && !error && (
        <div className="chat-empty">与 Agent 对话，描述需求即可创作，生成内容会自动落到画布</div>
      )}
      {messages.map((m) => (
        <MessageBubble key={m.id} message={m} />
      ))}
      {sending && <div className="chat-typing">思考中…</div>}
      {error && <div className="chat-error">{error}</div>}
      {planPending && <PlanCard plan={planPending.plan} />}
    </div>
  );
}

// ---------- 输入区：@引用暂存 chips + textarea + 发送 ----------

function ChatInput() {
  const draft = useChat((s) => s.draft);
  const setDraft = useChat((s) => s.setDraft);
  const send = useChat((s) => s.send);
  const sending = useChat((s) => s.sending);
  const activeSessionId = useChat((s) => s.activeSessionId);
  const planPending = useChat((s) => s.planPending);
  const refInput = useChat((s) => s.refInput);
  const addRefInput = useChat((s) => s.addRefInput);
  const removeRefInput = useChat((s) => s.removeRefInput);
  const [showMention, setShowMention] = useState(false);
  // 发送按钮的悬停提示里带计费说明（用户反馈：不要单独徽标挂在按钮上）
  const sendCostTip = useCostTip(BILLING_KEYS.chat);

  const canSend = !!activeSessionId && !sending && draft.trim().length > 0;

  const onSend = () => {
    if (!canSend) return;
    const text = draft;
    setDraft("");
    void send(text);
  };

  return (
    <div className="chat-input-area">
      {refInput.length > 0 && (
        <div className="chat-input-refs">
          {refInput.map((r) => (
            <span key={r.nodeId} className="chat-ref-chip" title={`@${r.nodeId}`}>
              @{r.label}
              <button onClick={() => removeRefInput(r.nodeId)}>×</button>
            </span>
          ))}
        </div>
      )}
      <div className="chat-input-wrap">
        {showMention && (
          <MentionPicker
            onClose={() => setShowMention(false)}
            onPick={(nodeId, label) => addRefInput({ nodeId, label })}
          />
        )}
        <textarea
          rows={3}
          placeholder={
            planPending
              ? "发送即确认当前计划，或点「调整」修改需求"
              : "描述创作需求，Enter 发送，Shift+Enter 换行"
          }
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSend();
            }
          }}
        />
        <div className="chat-input-actions">
          <button
            className="chat-mention-btn"
            title="引用画布节点"
            onClick={() => setShowMention((v) => !v)}
          >
            @
          </button>
          <button className="chat-send-btn" disabled={!canSend} onClick={onSend}
                  title={sendCostTip || undefined}>
            {sending ? "发送中…" : "发送"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------- 面板主体 ----------

export function ChatPanel() {
  const projectId = useCanvas((s) => s.projectId);
  const collapsed = useChat((s) => s.collapsed);
  const paneSide = useChat((s) => s.paneSide);
  const toggleCollapse = useChat((s) => s.toggleCollapse);
  const loadSessions = useChat((s) => s.loadSessions);

  // 项目就绪后加载会话（接口未就绪时保持空态，不影响画布）
  useEffect(() => {
    if (projectId) void loadSessions(projectId);
  }, [projectId, loadSessions]);

  // 收起后：画布边缘留展开把手
  if (collapsed) {
    return (
      <button
        className={`chat-handle ${paneSide === "left" ? "side-left" : ""}`}
        title="展开对话面板"
        onClick={toggleCollapse}
      >
        对话
      </button>
    );
  }

  return (
    <aside className={`chat-panel ${paneSide === "left" ? "side-left" : ""}`}>
      <ChatHeader />
      <ChatMessages />
      <ChatInput />
    </aside>
  );
}
