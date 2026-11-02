/** 账号管理内容区（仅管理员，嵌于系统管理页）：用户列表 + 新建用户 + 重置密码 / 管理员标记 / 禁用 / 删除 / 积分充值（33 号方案）。 */

import { useEffect, useState } from "react";
import { api, type AuthUserInfo } from "../api/client";
import { useAuth } from "../stores/auth";
import { confirmDialog, promptDialog, toast } from "../ui/feedback";

/** 时间戳（秒）→ 本地时间串（空值显示 —） */
function fmtTime(t: number | null): string {
  return t ? new Date(t * 1000).toLocaleString() : "—";
}

export function AdminUsersContent() {
  const [users, setUsers] = useState<AuthUserInfo[]>([]);
  /** 用户 id → 积分余额（计费账户概览；没发生过积分往来的显示 0） */
  const [balances, setBalances] = useState<Record<string, number>>({});
  const [newName, setNewName] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [newIsAdmin, setNewIsAdmin] = useState(false);
  const [creating, setCreating] = useState(false);
  const me = useAuth((s) => s.user);

  useEffect(() => {
    void reload();
  }, []);

  /** 拉取用户列表 + 积分概览（操作后统一走此刷新） */
  const reload = async () => {
    try {
      const [us, accts] = await Promise.all([
        api.listUsers(),
        api.billingAccounts(),
      ]);
      setUsers(us);
      const m: Record<string, number> = {};
      for (const a of accts.accounts) m[a.user_id] = a.balance;
      setBalances(m);
    } catch (e) {
      toast.error(`加载用户失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  /** 新建用户：用户名/密码必填，成功后刷新列表 */
  const create = async () => {
    if (!newName.trim() || !newPwd) {
      toast.warning("请填写用户名和初始密码");
      return;
    }
    setCreating(true);
    try {
      await api.createUser(newName.trim(), newPwd, newIsAdmin);
      toast.success(`已创建用户「${newName.trim()}」`);
      setNewName("");
      setNewPwd("");
      setNewIsAdmin(false);
      await reload();
    } catch (e) {
      toast.error(`创建失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setCreating(false);
    }
  };

  /** 重置密码：输入弹窗收集新密码（重置后该用户全部登录态失效） */
  const resetPwd = async (u: AuthUserInfo) => {
    const pwd = await promptDialog({
      title: `重置「${u.username}」的密码`,
      message: "输入新密码（至少 6 位）。重置后该用户所有端需重新登录。",
      placeholder: "新密码",
    });
    if (pwd === null) return;
    try {
      await api.updateUser(u.id, { password: pwd });
      toast.success(`已重置「${u.username}」的密码`);
    } catch (e) {
      toast.error(`重置失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  /** 切换管理员标记（自我降权已被后端拒绝，前端对自己的行隐藏该操作） */
  const toggleAdmin = async (u: AuthUserInfo) => {
    const next = !u.is_admin;
    const ok = await confirmDialog({
      title: next ? "设为管理员" : "取消管理员",
      message: next
        ? `「${u.username}」将获得系统设置与账号管理权限。`
        : `「${u.username}」将失去系统设置与账号管理权限。`,
      confirmText: next ? "设为管理员" : "取消管理员",
    });
    if (!ok) return;
    try {
      await api.updateUser(u.id, { is_admin: next });
      await reload();
    } catch (e) {
      toast.error(`操作失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  /** 启用/禁用（禁用后该用户不可登录且已有登录态失效） */
  const toggleDisabled = async (u: AuthUserInfo) => {
    const next = !u.disabled;
    const ok = await confirmDialog({
      title: next ? "禁用账号" : "启用账号",
      message: next
        ? `禁用后「${u.username}」将无法登录，已有登录态一并失效。`
        : `启用后「${u.username}」可重新登录。`,
      confirmText: next ? "禁用" : "启用",
      danger: next,
    });
    if (!ok) return;
    try {
      await api.updateUser(u.id, { disabled: next });
      await reload();
    } catch (e) {
      toast.error(`操作失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  /** 充值 / 调整积分（33 号方案）：正数充值、负数扣减（调整）；
   *  两步确认防误触，操作全部记入积分流水（后端）。 */
  const creditAdjust = async (u: AuthUserInfo) => {
    const current = balances[u.id] ?? 0;
    const raw = await promptDialog({
      title: `「${u.username}」的积分（当前 ${current}）`,
      message: "输入要充值的积分数；输入负数表示扣减（调整）。操作会记入积分流水。",
      placeholder: "如 100 或 -50",
      confirmText: "下一步",
    });
    if (raw === null) return;
    const amount = Number(raw.trim());
    if (!Number.isInteger(amount) || amount === 0) {
      toast.error("请输入非零整数");
      return;
    }
    const ok = await confirmDialog({
      title: amount > 0 ? "确认充值" : "确认扣减",
      message: `${amount > 0 ? "为" : "从"}「${u.username}」`
        + `${amount > 0 ? "充值" : "扣减"} ${Math.abs(amount)} 积分`
        + `（${current} → ${current + amount}）。`,
      confirmText: amount > 0 ? "确认充值" : "确认扣减",
      danger: amount < 0,
    });
    if (!ok) return;
    try {
      if (amount > 0) {
        await api.adminRecharge({ user_id: u.id, amount });
        toast.success(`已为「${u.username}」充值 ${amount} 积分`);
      } else {
        await api.adminAdjustCredit({ user_id: u.id, amount });
        toast.success(`已扣减「${u.username}」${-amount} 积分`);
      }
      await reload();
    } catch (e) {
      toast.error(`操作失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  /** 删除用户（不可恢复；自己的账号后端亦会拒绝） */
  const remove = async (u: AuthUserInfo) => {
    const ok = await confirmDialog({
      title: "删除用户",
      message: `确定删除用户「${u.username}」？该操作不可恢复。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteUser(u.id);
      toast.success(`已删除用户「${u.username}」`);
      await reload();
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <div className="admin-content">
        <div className="admin-create">
          <input
            placeholder="用户名（2-32 位中文/字母/数字）"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <input
            type="password"
            placeholder="初始密码（至少 6 位）"
            value={newPwd}
            onChange={(e) => setNewPwd(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void create()}
          />
          <label className="admin-check">
            <input
              type="checkbox"
              checked={newIsAdmin}
              onChange={(e) => setNewIsAdmin(e.target.checked)}
            />
            管理员
          </label>
          <button disabled={creating} onClick={() => void create()}>
            {creating ? "创建中…" : "新建用户"}
          </button>
        </div>
        <div className="admin-table">
          <div className="admin-row admin-head">
            <span>用户名</span>
            <span>角色</span>
            <span>状态</span>
            <span>积分</span>
            <span>最近登录</span>
            <span>操作</span>
          </div>
          {users.map((u) => (
            <div key={u.id} className={`admin-row${u.disabled ? " off" : ""}`}>
              <span className="admin-username">{u.username}</span>
              <span>
                {u.is_admin ? (
                  <span className="admin-badge admin">管理员</span>
                ) : (
                  <span className="admin-badge">成员</span>
                )}
              </span>
              <span>
                {u.disabled ? (
                  <span className="admin-badge danger">已禁用</span>
                ) : (
                  <span className="admin-badge ok">正常</span>
                )}
              </span>
              {/* 移动端卡片流没有表头，带「积分」字样自解释 */}
              <span className="admin-credits" title="积分余额">
                {balances[u.id] ?? 0} 积分
              </span>
              <span className="admin-time">{fmtTime(u.last_login_at)}</span>
              <span className="admin-actions">
                <button onClick={() => void resetPwd(u)}>重置密码</button>
                <button onClick={() => void creditAdjust(u)}>充值/调整</button>
                {u.id !== me?.id && (
                  <>
                    <button onClick={() => void toggleAdmin(u)}>
                      {u.is_admin ? "取消管理员" : "设为管理员"}
                    </button>
                    <button onClick={() => void toggleDisabled(u)}>
                      {u.disabled ? "启用" : "禁用"}
                    </button>
                    <button
                      className="danger"
                      onClick={() => void remove(u)}
                    >
                      删除
                    </button>
                  </>
                )}
              </span>
            </div>
          ))}
          {users.length === 0 && (
            <p className="admin-empty">暂无用户</p>
          )}
        </div>
      </div>
  );
}
