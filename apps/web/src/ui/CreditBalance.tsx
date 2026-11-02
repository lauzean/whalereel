/** 顶栏积分余额（33 号方案）：点击进入「我的积分」页。
 *
 * 挂三处（首页顶栏 + 短剧工作台 header + 画布 header）——消费大多发生在
 * 工作台里，只挂首页会看不到余额。挂载时自动拉一次余额（未加载不占位）。
 */

import { useEffect } from "react";
import { navigate } from "../router";
import { useBilling } from "../stores/billing";

export function CreditBalance() {
  const account = useBilling((s) => s.account);
  const refreshAccount = useBilling((s) => s.refreshAccount);

  useEffect(() => {
    if (!account) void refreshAccount();
    // 只在首次挂载时拉取；后续由消费操作/积分页触发刷新
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!account) return null;
  return (
    <button
      className="credit-balance"
      title="我的积分（余额 / 兑换码 / 明细）"
      aria-label="我的积分"
      onClick={() => navigate("#/credits")}
    >
      {account.balance} 积分
    </button>
  );
}
