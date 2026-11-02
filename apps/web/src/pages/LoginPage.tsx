/** 登录页：账号密码登录，回车提交；已登录自动回首页。

全屏背景为 React Bits Aurora 极光（WebGL），品牌三色缓慢流光；
系统「减少动态效果」偏好下回退为静态品牌色渐变。
 */

import { useEffect, useMemo, useState } from "react";
import { useAuth } from "../stores/auth";
import { navigate } from "../router";
import { toast } from "../ui/feedback";
import Aurora from "../ui/Aurora";
import { BrandMark } from "../ui/BrandMark";

/** 极光三段色标（品牌色的发光变体：亮蓝 → 亮紫 → 亮青） */
const AURORA_COLORS = ["#8ab6ff", "#b18cff", "#54e6cf"];
/** 极光形态参数：起伏幅度 / 过渡柔和度 / 流速倍率（慢速电影感） */
const AURORA_AMPLITUDE = 1.4;
const AURORA_BLEND = 0.45;
const AURORA_SPEED = 0.5;

export function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const user = useAuth((s) => s.user);
  // 「减少动态效果」偏好：静态渐变回退（渲染期读取一次即可）
  const prefersReduced = useMemo(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches, []);

  // 已登录访问登录页（如登录后回退）：回首页
  useEffect(() => {
    if (user) navigate("#/", true);
  }, [user]);

  /** 提交登录：账密非空校验，失败 toast 后端错误信息 */
  const submit = async () => {
    if (!username.trim() || !password) {
      toast.warning("请输入用户名和密码");
      return;
    }
    setSubmitting(true);
    try {
      await useAuth.getState().login(username.trim(), password);
      // 成功后由 login 内部跳转；此处无需处理
    } catch (e) {
      // client 层已把错误解析为人类可读信息（如「用户名或密码错误」）
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-page">
      {prefersReduced ? (
        <div className="login-aurora login-aurora-static" aria-hidden="true" />
      ) : (
        <div className="login-aurora" aria-hidden="true">
          <Aurora
            colorStops={AURORA_COLORS}
            amplitude={AURORA_AMPLITUDE}
            blend={AURORA_BLEND}
            speed={AURORA_SPEED}
          />
        </div>
      )}
      <div className="login-veil" aria-hidden="true" />
      <div className="login-card">
        <div className="login-brand">
          <span className="login-logo" aria-hidden="true"><BrandMark /></span>
          <span>鲸影</span>
        </div>
        <p className="login-sub">鲸纳万象 · 影叙人间</p>
        <div className="login-form">
          <input
            placeholder="用户名"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
          />
          <input
            type="password"
            placeholder="密码"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
          />
          <button disabled={submitting} onClick={() => void submit()}>
            {submitting ? "登录中…" : "登 录"}
          </button>
        </div>
      </div>
    </div>
  );
}
