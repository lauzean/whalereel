/** 强制改密页：还在用出厂默认口令时，登录后必须先把密码换掉。
 *
 * 为什么做成**独立页面**而不是弹窗（2026-09-30 首登强制改密）：默认口令是公开的
 * （`admin/admin123`），只要没改，任何能访问端口的人都能登录。弹窗可以被误关，
 * 独立页则明确挡住后续操作——改完才放进工作台。
 */

import { useState } from "react";
import { useAuth } from "../stores/auth";
import { toast } from "../ui/feedback";
import { BrandMark } from "../ui/BrandMark";

export function ForcePasswordPage() {
  const [oldPassword, setOldPassword] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!oldPassword || !next) {
      toast.warning("请填写当前密码与新密码");
      return;
    }
    if (next.length < 6) {
      toast.warning("新密码至少 6 位");
      return;
    }
    if (next !== confirm) {
      toast.warning("两次输入的新密码不一致");
      return;
    }
    if (next === oldPassword) {
      toast.warning("新密码不能与当前密码相同");
      return;
    }
    setSubmitting(true);
    try {
      await useAuth.getState().changePassword(oldPassword, next);
      toast.success("密码已更新");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-veil" aria-hidden="true" />
      <div className="login-card">
        <div className="login-brand">
          <span className="login-logo" aria-hidden="true"><BrandMark /></span>
          <span>鲸影</span>
        </div>
        <p className="login-sub">首次使用：请先修改默认密码</p>
        <p className="login-hint">
          当前账号仍在使用出厂默认口令。默认口令是公开的，不修改则任何能访问本机
          端口的人都能登录，请先设置一个新密码。
        </p>
        <div className="login-form">
          <input
            type="password"
            placeholder="当前密码（默认 admin123）"
            autoComplete="current-password"
            value={oldPassword}
            onChange={(e) => setOldPassword(e.target.value)}
          />
          <input
            type="password"
            placeholder="新密码（至少 6 位）"
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
          <input
            type="password"
            placeholder="再输入一次新密码"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
          />
          <button disabled={submitting} onClick={() => void submit()}>
            {submitting ? "提交中…" : "设置新密码"}
          </button>
          <button
            className="login-skip"
            disabled={submitting}
            onClick={() => void useAuth.getState().logout()}
          >
            退出登录
          </button>
        </div>
      </div>
    </div>
  );
}
