/** 积分计费状态：余额 + 价目缓存 + 402 全局充值引导（33 号方案）。
 *
 * 数据源约定：价格**唯一来源是后端价目**（GET /api/billing/pricing）——
 * 前端禁止硬编码任何价格；未在价目表里的端点即免费。余额在「进入页面 /
 * 消费操作后 / 任务终态」时主动刷新（不做新 WS 频道——账目无项目维度，
 * 主动拉取简单可靠；同 modelOptions 的 TTL 缓存思路）。
 */

import { create } from "zustand";
import {
  api,
  setInsufficientCreditsHandler,
  type BillingPricingItem,
  type CreditAccountInfo,
} from "../api/client";
import { navigate } from "../router";
import { confirmDialog } from "../ui/feedback";

/** 价目缓存有效期（毫秒；管理员改价后最多迟这么久在前端生效） */
const PRICING_TTL_MS = 60_000;

interface BillingState {
  /** 我的账户（null = 未加载） */
  account: CreditAccountInfo | null;
  /** 端点键（"METHOD /path"）→ 价目行 */
  pricing: Record<string, BillingPricingItem> | null;
  pricingAt: number;
  /** 刷新余额（失败静默——余额展示不该打扰用户） */
  refreshAccount: () => Promise<void>;
  /** 刷新价目（带 TTL；force = 忽略缓存） */
  refreshPricing: (force?: boolean) => Promise<void>;
}

export const useBilling = create<BillingState>((set, get) => ({
  account: null,
  pricing: null,
  pricingAt: 0,

  refreshAccount: async () => {
    try {
      set({ account: await api.myCredits() });
    } catch {
      /* 静默：余额展示失败不弹错（401 另有全局处理） */
    }
  },

  refreshPricing: async (force = false) => {
    if (!force && get().pricing
        && Date.now() - get().pricingAt < PRICING_TTL_MS) {
      return;
    }
    try {
      const r = await api.billingPricing();
      const map: Record<string, BillingPricingItem> = {};
      for (const item of r.rules) map[item.key] = item;
      set({ pricing: map, pricingAt: Date.now() });
    } catch {
      /* 静默：拿不到价目不阻塞功能（按钮只是少一个徽标） */
    }
  },
}));

/** 接口键 → 预计消耗积分（发起时按此额预扣；未加载返回 null）。
 *  实际扣费按「所用模型服务端点的费率 × 真实调用」结算、多退少补。 */
export function creditsFor(endpointKey: string): number | null {
  const item = useBilling.getState().pricing?.[endpointKey];
  if (!item) return null;
  return item.precharge;
}

/** 接口键 → 中文名（未登记返回空串）。 */
export function priceLabelOf(endpointKey: string): string {
  const item = useBilling.getState().pricing?.[endpointKey];
  return item?.label ?? "";
}

/** 按钮 tooltip 的计费说明（hook，订阅价目状态——加载/变更后自动更新）。
 *
 *  用户视角只讲「大概要花多少」，不暴露管理员侧的费率配置等概念
 *  （2026-10-09 用户反馈：不要单独徽标挂在按钮上，太丑——并进按钮 title；
 *  提示里也不许出现用户看不到的「模型管理」等管理概念）。
 *  不在计费清单/免费时返回空串（调用方 `title={tip || undefined}` 即可）。
 */
export function useCostTip(endpointKey: string): string {
  const item = useBilling((s) => s.pricing?.[endpointKey]);
  if (!item || item.precharge <= 0) return "";
  return `预计消耗约 ${item.precharge} 积分（按实际用量结算）`;
}

/** useCostTip + 原有 title 合并（两者都有用「；」拼接；都无则空串）。 */
export function useCostTipWith(baseTitle: string, endpointKey: string): string {
  const tip = useCostTip(endpointKey);
  if (!tip) return baseTitle;
  return baseTitle ? `${baseTitle}；${tip}` : tip;
}

/** 确认弹窗里的计费文案行（不在计费闸清单里返回空串——不打扰）。
 *  用户视角：说清「预计花多少 + 按实际结算 + 当前余额」即可。 */
export function costLine(endpointKey: string): string {
  const item = useBilling.getState().pricing?.[endpointKey];
  if (!item || item.precharge <= 0) return "";
  const balance = useBilling.getState().account?.balance;
  const balanceText = balance === undefined ? "" : `，当前余额 ${balance}`;
  return `本次预计消耗约 ${item.precharge} 积分（按实际用量结算，多退少补）`
    + `${balanceText}。`;
}

// 402 全局处理：任何接口返回「余额不足」→ 弹引导（去「我的积分」充值/兑换）。
// 结构化数据（需要/现有积分）由 client.ts 解析后传入——不能只靠错误字符串。
setInsufficientCreditsHandler((info) => {
  void (async () => {
    void useBilling.getState().refreshAccount(); // 顺手刷新余额，弹窗里数字才新
    const go = await confirmDialog({
      title: "积分余额不足",
      message: `${info.detail}\n\n可前往「我的积分」查看余额、使用兑换码充值，`
        + `或联系管理员充值。`,
      confirmText: "去充值",
      cancelText: "稍后再说",
    });
    if (go) navigate("#/credits");
  })();
});
