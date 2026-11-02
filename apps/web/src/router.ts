/** 极简 hash 路由：URL hash → 路径段（zustand 订阅），刷新后原地恢复。

路由表（App.tsx 分发）：
- #/                     首页（项目列表）
- #/login                登录页
- #/admin/users          账号管理（仅管理员）
- #/project/:id/canvas   画布工作台
- #/project/:id/script | assets | episodes   短剧工作台三个子页

选 hash 而非 history API：无服务端回退配置依赖（静态托管/dev 都可用），
刷新/直达/前进后退天然成立。
 */

import { create } from "zustand";

/** 路由存储：仅存原始 hash（解析在组件侧 memo，避免每次返回新数组） */
interface RouterState {
  hash: string;
}

export const useRouter = create<RouterState>(() => ({
  hash: window.location.hash,
}));

// hash 变化（含浏览器前进/后退）同步进 store
window.addEventListener("hashchange", () => {
  useRouter.setState({ hash: window.location.hash });
});

/** 解析 hash 为路径段：#/project/1/script → ["project","1","script"]；空 hash → [] */
export function parsePath(hash: string): string[] {
  const raw = hash.replace(/^#/, "");
  const path = raw.startsWith("/") ? raw : `/${raw}`;
  return path.split("/").filter(Boolean);
}

/** 编程跳转（to 形如 "/login"；replace = 不留历史记录的重定向） */
export function navigate(to: string, replace = false): void {
  const target = to.startsWith("#") ? to : `#${to.startsWith("/") ? to : `/${to}`}`;
  if (replace) {
    // replaceState 不触发 hashchange，手动同步 store
    history.replaceState(null, "", target);
    useRouter.setState({ hash: target });
  } else {
    window.location.hash = target;
  }
}
