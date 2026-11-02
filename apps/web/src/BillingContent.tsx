/** 系统管理 · 积分计费页签（33 号方案，仅管理员）。
 *
 * 口径（2026-10-09 用户定稿）：**费率在「模型管理」页按模型服务端点配置**
 * （方舟/Agnes/… 每千 token / 每张图 / 每秒视频各多少积分）；积分按实际
 * 调用情况结算。本页只管：全局配置 / 兑换码 / 全局流水与对账——
 * 不再有任何「接口级」配置。系统平台接口一律免费。
 */

import { useCallback, useEffect, useState } from "react";
import {
  api,
  CREDIT_KIND_LABEL,
  type BillingSettingsInfo,
  type CreditTransactionInfo,
  type RedeemCodeInfo,
} from "./api/client";
import { navigate } from "./router";
import { toast } from "./ui/feedback";

/** 秒级时间戳 → 「MM-DD HH:mm」 */
function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function BillingContent() {
  const [settings, setSettings] = useState<BillingSettingsInfo | null>(null);
  const [savingSettings, setSavingSettings] = useState(false);
  const [auditMsg, setAuditMsg] = useState("");
  /** 初始赠送输入草稿（失焦才提交——避免每敲一键发一次请求） */
  const [grantDraft, setGrantDraft] = useState("");

  // 兑换码：生成表单 + 最近生成的明文码 + 列表
  const [genCredits, setGenCredits] = useState("100");
  const [genCount, setGenCount] = useState("10");
  const [genNote, setGenNote] = useState("");
  const [generating, setGenerating] = useState(false);
  const [generated, setGenerated] = useState<string[]>([]);
  const [codes, setCodes] = useState<RedeemCodeInfo[]>([]);

  // 全局流水（分页）
  const [txns, setTxns] = useState<CreditTransactionInfo[]>([]);
  const [txnTotal, setTxnTotal] = useState(0);
  const [txnLoading, setTxnLoading] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([
        api.billingSettings(),
        api.listRedeemCodes({ limit: 50 }),
      ]);
      setSettings(s.settings);
      setCodes(c.items);
    } catch (e) {
      toast.error(`加载计费配置失败：${e instanceof Error ? e.message : e}`);
    }
  }, []);

  const loadTxns = useCallback(async (offset: number) => {
    setTxnLoading(true);
    try {
      const r = await api.allCreditTransactions({ offset, limit: 50 });
      setTxnTotal(r.total);
      setTxns((prev) => (offset === 0 ? r.items : [...prev, ...r.items]));
    } catch (e) {
      toast.error(`加载流水失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setTxnLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    void loadTxns(0);
  }, [load, loadTxns]);

  // 初始赠送草稿跟随服务端值（初次加载与保存后同步）
  useEffect(() => {
    if (settings) setGrantDraft(String(settings.signup_grant));
  }, [settings?.signup_grant]);  // eslint-disable-line react-hooks/exhaustive-deps

  /** 计费开关（总开关/失败退款/管理员免费/初始赠送）保存 */
  const saveSettings = async (patch: Partial<BillingSettingsInfo>) => {
    setSavingSettings(true);
    try {
      const r = await api.updateBillingSettings(patch);
      setSettings(r.settings);
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setSavingSettings(false);
    }
  };

  /** 批量生成兑换码 */
  const generateCodes = async () => {
    const credits = Number(genCredits);
    const count = Number(genCount);
    if (!Number.isInteger(credits) || credits <= 0) {
      toast.error("面额必须为正整数");
      return;
    }
    if (!Number.isInteger(count) || count < 1 || count > 500) {
      toast.error("数量需在 1-500 之间");
      return;
    }
    setGenerating(true);
    try {
      const r = await api.createRedeemCodes({ credits, count, note: genNote });
      setGenerated(r.codes);
      toast.success(`已生成 ${r.codes.length} 张兑换码——请立即复制保存（页面刷新后不再展示明文）`);
      const c = await api.listRedeemCodes({ limit: 50 });
      setCodes(c.items);
    } catch (e) {
      toast.error(`生成失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setGenerating(false);
    }
  };

  const copyGenerated = async () => {
    try {
      await navigator.clipboard.writeText(generated.join("\n"));
      toast.success("已复制全部兑换码");
    } catch {
      toast.error("复制失败，请手动选择复制");
    }
  };

  /** 手动对账 */
  const runAudit = async () => {
    try {
      const r = await api.billingAudit();
      if (r.bad.length === 0) {
        setAuditMsg(`对账通过：${r.checked} 个账户余额与流水汇总全部一致`);
        toast.success("对账通过");
      } else {
        setAuditMsg(`发现 ${r.bad.length} 个账户不平（余额与流水汇总不一致），请查看后端日志`);
        toast.error("对账发现问题");
      }
    } catch (e) {
      toast.error(`对账失败：${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <div className="provider-body billing-body">
      <h3 className="billing-h3">费率与计费说明</h3>
      <p className="billing-note">
        积分计费按<strong>实际模型调用</strong>结算：每次调用（对话模型按
        token、生图按张、生视频按秒）按<strong>所用模型服务端点</strong>的
        费率扣积分。<strong>费率在「模型管理」页每个服务端点里配置</strong>；
        发起操作时系统按内置额度先预扣一笔押金，完成后按实际调用多退少补。
        <strong>系统平台功能（保存、编辑、查询、任务管理等）一律免费</strong>。
      </p>
      <button className="provider-save" onClick={() => navigate("#/admin/settings/endpoints")}>
        去「模型管理」配置服务费率
      </button>

      <h3 className="billing-h3">全局配置</h3>
      {settings && (
        <div className="billing-settings">
          <label className="billing-setting-row">
            <input type="checkbox" checked={settings.enabled}
                   disabled={savingSettings}
                   onChange={(e) => void saveSettings({ enabled: e.target.checked })} />
            计费总开关（关闭后不预扣、不结算，全部免费）
          </label>
          <label className="billing-setting-row">
            <input type="checkbox" checked={settings.auto_refund}
                   disabled={savingSettings}
                   onChange={(e) => void saveSettings({ auto_refund: e.target.checked })} />
            失败自动退款（任务失败/取消退回预扣）
          </label>
          <label className="billing-setting-row">
            <input type="checkbox" checked={settings.admin_free}
                   disabled={savingSettings}
                   onChange={(e) => void saveSettings({ admin_free: e.target.checked })} />
            管理员免计费（勾选后管理员调用不扣分）
          </label>
          <label className="billing-setting-row">
            新用户初始赠送
            <input className="billing-credit-input" type="number" min={0}
                   value={grantDraft}
                   disabled={savingSettings}
                   onChange={(e) => setGrantDraft(e.target.value)}
                   onBlur={() => {
                     const n = Number(grantDraft) || 0;
                     if (settings && n !== settings.signup_grant) {
                       void saveSettings({ signup_grant: n });
                     }
                   }} />
            积分
          </label>
        </div>
      )}

      <h3 className="billing-h3">兑换码</h3>
      <p className="billing-note">
        生成后请立即复制发给用户（明文只在生成时展示一次）；用户在「我的积分」页输入兑换。
      </p>
      <div className="billing-toolbar">
        <label>面额
          <input className="billing-credit-input" type="number" min={1}
                 value={genCredits} onChange={(e) => setGenCredits(e.target.value)} />
        </label>
        <label>数量
          <input className="billing-credit-input" type="number" min={1} max={500}
                 value={genCount} onChange={(e) => setGenCount(e.target.value)} />
        </label>
        <label>备注
          <input className="billing-note-input" value={genNote}
                 placeholder="如：内测第一批"
                 onChange={(e) => setGenNote(e.target.value)} />
        </label>
        <button className="provider-save" disabled={generating}
                onClick={() => void generateCodes()}>
          {generating ? "生成中…" : "生成兑换码"}
        </button>
      </div>
      {generated.length > 0 && (
        <div className="billing-generated">
          <div className="billing-generated-head">
            <span>本次生成的兑换码（{generated.length} 张）</span>
            <button className="ghost-btn" onClick={() => void copyGenerated()}>
              复制全部
            </button>
          </div>
          <div className="billing-generated-list">
            {generated.map((c) => <code key={c}>{c}</code>)}
          </div>
        </div>
      )}
      {codes.length > 0 && (
        <div className="billing-table-wrap">
          <table className="billing-table">
            <thead>
              <tr><th>兑换码</th><th>面额</th><th>状态</th><th>使用者</th><th>时间</th></tr>
            </thead>
            <tbody>
              {codes.map((c) => (
                <tr key={c.id}>
                  <td><code>{c.code}</code></td>
                  <td>{c.credits}</td>
                  <td>{c.used_by_user_id ? "已使用" : "未使用"}</td>
                  <td>{c.used_by_username || "—"}</td>
                  <td>{fmtTime(c.used_at ?? c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="billing-h3">全局流水与对账</h3>
      <div className="billing-toolbar">
        <button className="ghost-btn" onClick={() => void runAudit()}>
          立即对账（余额 == 流水汇总）
        </button>
        {auditMsg && <span className="billing-note">{auditMsg}</span>}
      </div>
      <div className="billing-table-wrap">
        <table className="billing-table">
          <thead>
            <tr>
              <th>时间</th><th>用户</th><th>类型</th><th>变动</th>
              <th>余额</th><th>说明</th>
            </tr>
          </thead>
          <tbody>
            {txns.map((t) => (
              <tr key={t.id}>
                <td>{fmtTime(t.created_at)}</td>
                <td>{t.username || t.user_id.slice(0, 8)}</td>
                <td>{CREDIT_KIND_LABEL[t.kind] || t.kind}{t.status === "refunded" ? "（已退）" : ""}</td>
                <td className={t.amount >= 0 ? "billing-in" : "billing-out"}>
                  {t.amount >= 0 ? `+${t.amount}` : t.amount}
                </td>
                <td>{t.balance_after}</td>
                <td className="billing-note-cell" title={t.note}>{t.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {txns.length < txnTotal && (
        <button className="ghost-btn billing-more" disabled={txnLoading}
                onClick={() => void loadTxns(txns.length)}>
          {txnLoading ? "加载中…" : `加载更多（已显示 ${txns.length}/${txnTotal}）`}
        </button>
      )}
    </div>
  );
}
