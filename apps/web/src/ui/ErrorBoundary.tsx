/** 全局错误边界：把「白屏」变成「看得懂的报错 + 能自救的按钮」。
 *
 * 为什么必须有（2026-09-30）：此前全仓没有 ErrorBoundary（`ErrorBoundary` /
 * `componentDidCatch` / `getDerivedStateFromError` 零命中），而 App 的启动门禁是
 * `if (!ready) return null`——任何一个渲染期异常或启动数据拉取卡住，用户得到的
 * 就是一片纯色空白，既没有提示也没有重试入口，只能凭经验刷新。
 *
 * 这里兜住三类情况：
 * ① 渲染期抛异常 → 显示错误详情 + 「重试」；
 * ② 启动数据一直没回来 → App 侧自己超时兜底（见 App.tsx），不走本组件；
 * ③ chunk 加载失败（构建产物更新后旧页面继续点击）→ 提示刷新页面。
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
  info: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: "" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 控制台留全量堆栈，便于用户截图反馈；不额外上报（本项目无上报通道）
    console.error("[界面异常]", error, info.componentStack);
    this.setState({ info: info.componentStack ?? "" });
  }

  private reload = (): void => {
    window.location.reload();
  };

  private reset = (): void => {
    this.setState({ error: null, info: "" });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    const message = error.message || String(error);
    // 动态 import 失败（构建产物换版后旧页面继续点）单独给文案
    const isChunkError = /Loading chunk|Failed to fetch dynamically imported module/i
      .test(message);
    return (
      <div className="crash-screen">
        <div className="crash-card">
          <h2>{isChunkError ? "页面需要刷新" : "界面出错了"}</h2>
          <p className="crash-hint">
            {isChunkError
              ? "应用已更新，当前页面还在用旧版本的文件。刷新一下即可恢复。"
              : "这一步操作把界面打断了。可以先重试；若反复出现，把下面的信息截图反馈。"}
          </p>
          <pre className="crash-detail">{message}</pre>
          <div className="crash-actions">
            <button className="mm-btn-primary" onClick={this.reload}>刷新页面</button>
            <button className="mm-btn" onClick={this.reset}>返回继续</button>
          </div>
          {this.state.info && (
            <details className="crash-stack">
              <summary>技术细节（给开发排查用）</summary>
              <pre>{this.state.info}</pre>
            </details>
          )}
        </div>
      </div>
    );
  }
}
