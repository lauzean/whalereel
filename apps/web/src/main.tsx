import { createRoot } from "react-dom/client";
import { ReactFlowProvider } from "@xyflow/react";
import { App } from "./App";
import { ErrorBoundary } from "./ui/ErrorBoundary";
import { FeedbackHost } from "./ui/FeedbackHost";
import "./styles.css";
// 移动端适配层：只含窄屏/触摸媒体查询，须在 styles.css 之后加载以覆盖其桌面规则
import "./styles-mobile.css";

createRoot(document.getElementById("root")!).render(
  // 全局错误边界（最外层）：渲染期异常不再变成白屏，给出可读报错与刷新入口
  <ErrorBoundary>
    <ReactFlowProvider>
      <App />
      {/* 全局反馈组件宿主：Toast 与 Confirm/Prompt 弹窗（命令式 API 渲染出口） */}
      <FeedbackHost />
    </ReactFlowProvider>
  </ErrorBoundary>,
);
