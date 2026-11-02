/** 系统管理页（仅管理员）：左侧菜单 + 右侧内容区，路由化子页。

子路由：#/admin/users 账号管理；#/admin/settings/{endpoints|models|defaults|prompts}
系统设置四页签（原系统配置弹窗的内容区整体迁入，独立路由刷新可恢复）。
 */

import { useEffect } from "react";
import { AdminUsersContent } from "./AdminUsersPage";
import { BillingContent } from "../BillingContent";
import { DefaultConfigContent } from "../DefaultConfigContent";
import { EndpointsContent } from "../EndpointsContent";
import { ModelManagerContent } from "../ModelManagerContent";
import { PromptsContent } from "../PromptsContent";
import { CallsContent } from "../CallsContent";
import { SkillsContent } from "../SkillsContent";
import { navigate } from "../router";
import { useAuth } from "../stores/auth";
import {
  IconBlocks, IconCalls, IconChip, IconCoins, IconDefaults, IconEndpoints,
  IconPrompts, IconUsers,
} from "../ui/icons";

/** 系统设置页签（key 即路由段） */
const SETTINGS_TABS = [
  { key: "endpoints", label: "模型管理", icon: IconEndpoints },
  { key: "models", label: "本地模型", icon: IconChip },
  { key: "defaults", label: "默认配置", icon: IconDefaults },
  { key: "skills", label: "技能包", icon: IconBlocks },
  { key: "prompts", label: "提示词", icon: IconPrompts },
  { key: "calls", label: "调用日志", icon: IconCalls },
  // 积分计费（33 号方案）：价目 / 全局配置 / 兑换码 / 流水对账
  { key: "billing", label: "积分计费", icon: IconCoins },
] as const;

type SettingsTabKey = (typeof SETTINGS_TABS)[number]["key"];

/** 判定字符串是否为合法设置页签路由段 */
function isSettingsTab(v: string | undefined): v is SettingsTabKey {
  return !!v && SETTINGS_TABS.some((t) => t.key === v);
}

/** 左侧菜单项定义（账号管理 + 系统设置分组） */
const MENU_USERS = { key: "users", label: "账号管理", icon: IconUsers } as const;

export function SystemPage({ sub, tab }: { sub: string; tab: string }) {
  const user = useAuth((s) => s.user);

  // 非法子路由归一：#/admin → #/admin/users（replace 不留历史）
  useEffect(() => {
    const valid =
      (sub === "users" && !tab) || (sub === "settings" && isSettingsTab(tab));
    if (!valid) navigate("#/admin/users", true);
  }, [sub, tab]);

  // 非管理员越权直达：回首页（按钮已隐藏，仅防御手输 URL）
  useEffect(() => {
    if (user && !user.is_admin) navigate("#/");
  }, [user]);

  if (!user?.is_admin) return null;
  if (sub === "settings" && isSettingsTab(tab)) {
    return <SystemShell active={`settings/${tab}`} />;
  }
  if (sub === "users") {
    return <SystemShell active="users" />;
  }
  return null; // 归一 effect 跳转中
}

/** 页面壳：顶部栏 + 左侧菜单 + 右侧内容（active 形如 users / settings/endpoints） */
function SystemShell({ active }: { active: string }) {
  return (
    <div className="system-page">
      <header className="app-header">
        <button className="back" onClick={() => navigate("#/")}>
          返回
        </button>
        <span>系统管理</span>
      </header>
      <div className="system-layout">
        <aside className="system-side">
          <p className="system-group">账号</p>
          <button
            className={`system-nav${active === "users" ? " on" : ""}`}
            onClick={() => navigate("#/admin/users")}
          >
            <MENU_USERS.icon />
            {MENU_USERS.label}
          </button>
          <p className="system-group">系统设置</p>
          {SETTINGS_TABS.map((t) => (
            <button
              key={t.key}
              className={`system-nav${active === `settings/${t.key}` ? " on" : ""}`}
              onClick={() => navigate(`#/admin/settings/${t.key}`)}
            >
              <t.icon />
              {t.label}
            </button>
          ))}
        </aside>
        <main className="system-body">
          {active === "users" && <AdminUsersContent />}
          {active === "settings/endpoints" && <EndpointsContent />}
          {active === "settings/models" && <ModelManagerContent />}
          {active === "settings/defaults" && <DefaultConfigContent />}
          {active === "settings/skills" && <SkillsContent />}
          {active === "settings/prompts" && <PromptsContent />}
          {active === "settings/calls" && <CallsContent />}
          {active === "settings/billing" && <BillingContent />}
        </main>
      </div>
    </div>
  );
}
