/** 认证状态：登录用户 + 令牌持久化 + 401 全局登出。

令牌存 localStorage（客户端持久会话）；用户信息启动时经 /api/auth/me 恢复。
401 全局处理：会话过期/失效时清空本地状态并跳登录页，登录成功后回到原页。
 */

import { create } from "zustand";
import {
  api, getStoredToken, setStoredToken, setUnauthorizedHandler,
  type AuthUserInfo,
} from "../api/client";
import { navigate } from "../router";

interface AuthState {
  /** 当前登录用户（null = 未登录） */
  user: AuthUserInfo | null;
  /** 启动恢复是否完成（避免登录前闪现首页） */
  ready: boolean;
  /** 登录：成功后写入令牌与用户并返回；失败抛错（调用方 toast 提示） */
  login: (username: string, password: string) => Promise<void>;
  /** 登出：调后端吊销令牌（失败也照样清本地） */
  logout: () => Promise<void>;
  /** 启动恢复：有令牌则拉当前用户，失效则清空 */
  bootstrap: () => Promise<void>;
  /** 改密（成功后后端吊销其他端令牌，本端令牌保留，所以只需刷新用户信息） */
  changePassword: (oldPassword: string, newPassword: string) => Promise<void>;
}

/** 登录前原路径的 sessionStorage 键（登录成功后跳回） */
const REDIRECT_KEY = "avs_login_redirect";

/** 记录登录前想去的路径（401 跳登录时调用） */
export function rememberRedirect(): void {
  const hash = window.location.hash;
  if (hash && hash !== "#/login") {
    sessionStorage.setItem(REDIRECT_KEY, hash);
  }
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  ready: false,

  login: async (username, password) => {
    const result = await api.login(username, password);
    setStoredToken(result.token);
    set({ user: result.user });
    // 登录成功：回到登录前想去的页面（无记录则回首页）
    const redirect = sessionStorage.getItem(REDIRECT_KEY);
    sessionStorage.removeItem(REDIRECT_KEY);
    navigate(redirect || "#/");
  },

  logout: async () => {
    try {
      await api.logout();
    } catch {
      // 后端登出失败不阻断本地登出（令牌可能已过期）
    }
    setStoredToken("");
    set({ user: null });
    navigate("#/login");
  },

  bootstrap: async () => {
    if (!getStoredToken()) {
      set({ ready: true, user: null });
      return;
    }
    try {
      const user = await api.me();
      set({ user, ready: true });
    } catch {
      // 401 已由全局处理器处理（跳登录）；其余错误同样按未登录处理
      setStoredToken("");
      set({ user: null, ready: true });
    }
  },

  changePassword: async (oldPassword, newPassword) => {
    await api.changePassword(oldPassword, newPassword);
    // 改密后重新拉用户信息：must_change_password 由「是否仍等于默认口令」推导，
    // 改完自然变假——不重新拉的话界面会一直卡在强制改密页
    const user = await api.me();
    set({ user });
  },
}));

// 401 全局处理：清会话 → 记录原路径 → 跳登录页
setUnauthorizedHandler(() => {
  setStoredToken("");
  if (useAuth.getState().user) {
    useAuth.setState({ user: null });
    rememberRedirect();
    navigate("#/login");
  }
});
