/** 「我的积分」页（33 号方案）：余额卡 + 兑换码自助充值 + 流水分页明细。
 *
 * 数据源：账户与流水来自 /api/billing/*（只涉及当前用户）；操作完成后主动
 * 刷新余额与明细（不做 WS 频道——账目无项目维度）。
 */

import { useCallback, useEffect, useState } from "react";
import {
  api,
  CREDIT_KIND_LABEL,
  type CreditTransactionInfo,
} from "../api/client";
import { navigate } from "../router";
import { useBilling } from "../stores/billing";
import { toast } from "../ui/feedback";

/** 流水分页大小 */
const PAGE_SIZE = 30;

/** 流水状态中文标签（空串状态不展示——充值/调整类无状态机） */
const STATUS_LABEL: Record<string, string> = {
  pending: "处理中",
  consumed: "",
  refunded: "已退款",
};

/** 秒级时间戳 → 「YYYY-MM-DD HH:mm」 */
function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function CreditsPage() {
  const account = useBilling((s) => s.account);
  const refreshAccount = useBilling((s) => s.refreshAccount);
  const [items, setItems] = useState<CreditTransactionInfo[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [code, setCode] = useState("");
  const [redeeming, setRedeeming] = useState(false);

  const load = useCallback(async (offset: number) => {
    setLoading(true);
    try {
      const r = await api.myCreditTransactions({ offset, limit: PAGE_SIZE });
      setTotal(r.total);
      setItems((prev) => (offset === 0 ? r.items : [...prev, ...r.items]));
    } catch (e) {
      toast.error(
        `加载积分明细失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshAccount();
    void load(0);
  }, [refreshAccount, load]);

  /** 兑换码核销：成功后刷新余额与明细（错误已被 402/通用路径 toast） */
  const doRedeem = async () => {
    const c = code.trim();
    if (!c) {
      toast.info("请输入兑换码");
      return;
    }
    setRedeeming(true);
    try {
      const r = await api.redeemCreditCode(c);
      toast.success(`兑换成功：+${r.credits} 积分（当前余额 ${r.balance}）`);
      setCode("");
      void refreshAccount();
      void load(0);
    } catch (e) {
      toast.error(`兑换失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setRedeeming(false);
    }
  };

  return (
    <div className="app credits-page">
      <header className="app-header">
        <button className="back" onClick={() => navigate("#/")}>
          返回
        </button>
        <span>我的积分</span>
      </header>
      <main className="credits-main">
        <section className="credits-balance-card">
          <p className="credits-balance-num">{account ? account.balance : "—"}</p>
          <p className="credits-balance-label">当前积分余额</p>
          <p className="credits-balance-sub">
            累计充值 {account?.total_recharged ?? 0} · 累计消费{" "}
            {account?.total_consumed ?? 0}
          </p>
        </section>

        <section className="credits-redeem">
          <h3>兑换码充值</h3>
          <div className="credits-redeem-row">
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="输入兑换码"
              disabled={redeeming}
              onKeyDown={(e) => {
                if (e.key === "Enter") void doRedeem();
              }}
            />
            <button
              className="gen-btn"
              onClick={() => void doRedeem()}
              disabled={redeeming}
            >
              {redeeming ? "兑换中…" : "兑换"}
            </button>
          </div>
          <p className="credits-hint">没有兑换码？请联系管理员充值。</p>
        </section>

        <section className="credits-txns">
          <h3>积分明细{total ? `（共 ${total} 笔）` : ""}</h3>
          {items.length === 0 && !loading && (
            <p className="credits-hint">暂无积分记录</p>
          )}
          <ul className="credits-txn-list">
            {items.map((t) => (
              <li key={t.id} className="credits-txn">
                <div className="credits-txn-main">
                  <span className="credits-txn-note">
                    {t.note || CREDIT_KIND_LABEL[t.kind] || t.kind}
                  </span>
                  {STATUS_LABEL[t.status] ? (
                    <span className="credits-txn-status">
                      {STATUS_LABEL[t.status]}
                    </span>
                  ) : null}
                </div>
                <div className="credits-txn-side">
                  <span
                    className={`credits-txn-amount ${t.amount >= 0 ? "in" : "out"}`}
                  >
                    {t.amount >= 0 ? `+${t.amount}` : t.amount}
                  </span>
                  <span className="credits-txn-time">
                    {fmtTime(t.created_at)} · 余额 {t.balance_after}
                  </span>
                </div>
              </li>
            ))}
          </ul>
          {items.length < total && (
            <button
              className="ghost-btn credits-more"
              disabled={loading}
              onClick={() => void load(items.length)}
            >
              {loading ? "加载中…" : "加载更多"}
            </button>
          )}
        </section>
      </main>
    </div>
  );
}
