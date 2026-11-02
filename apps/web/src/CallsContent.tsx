/** 调用日志与看板（系统管理·调用日志页签，25 号 2.3/2.6）。
 *
 * 这一页回答两个问题：
 * - **看板**：「一共花了多少、哪个端点最差」——总量、消费（出图张数 / 视频秒数 /
 *   token）、按「调用点 + 域名 + 模型」聚合的明细、按天趋势；
 * - **日志**：「具体哪一次失败了、为什么」——逐条记录（时间/调用点/域名/模型/
 *   结果/耗时/失败类别/错误摘要/消费），可只看失败、可按关键词过滤。
 *
 * 两者是同一张表（`model_calls`，每次对外模型调用一行）的两个视角：先在这里挑出
 * 可疑调用，再拿任务号去分镜卡的「查看参数」看那一次实际发出去的完整内容。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  api,
  type ModelCallDetail,
  type ModelCallInfo,
  type ModelCallStats,
} from "./api/client";
import { toast } from "./ui/feedback";

/** 可选时间窗（天）；0 = 全部（不限时间，配合「加载更多」翻历史） */
const WINDOW_DAYS = [1, 7, 30, 0] as const;
/** 调用明细每页条数（「加载更多」每次追加一页） */
const PAGE_SIZE = 200;

/** 失败类别 → 白话（与后端 FailClass 取值对齐；未知原样显示） */
const FAIL_CLASS_LABEL: Record<string, string> = {
  content_safe: "内容安全拦截",
  rate_limit: "限流",
  timeout: "超时",
  network: "连接失败",
  quota: "欠费/余额不足",
  param: "参数问题",
  server: "服务端错误",
  billed: "已计费（取回失败）",
  unknown: "未知",
};

/** 秒级时间戳 → 本地「月-日 时:分:秒」 */
function fmtTime(at: number): string {
  const d = new Date(at * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 毫秒 → 「1.2s」/「350ms」 */
function fmtMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

/** 大数字加千分位（消费量看着直观） */
function fmtNum(n: number): string {
  return n.toLocaleString("zh-CN");
}

/** 消费信息 → 一行中文（取不到就显示「—」） */
function usageText(u: Record<string, unknown>): string {
  const parts: string[] = [];
  const img = Number(u.images ?? 0);
  if (img) parts.push(`${img} 张图`);
  const tok = u.tokens as { total_tokens?: number } | undefined;
  if (tok && Number(tok.total_tokens ?? 0) > 0) {
    parts.push(`${fmtNum(Number(tok.total_tokens))} token`);
  }
  const sec = Number(u.req_seconds ?? u.req_duration ?? 0);
  if (sec) parts.push(`${sec}s 视频`);
  if (u.remote_task_id) parts.push(`远端任务 ${String(u.remote_task_id).slice(0, 10)}`);
  return parts.length ? parts.join(" · ") : "—";
}

export function CallsContent() {
  const [days, setDays] = useState<number>(7);
  /** 自定义日期区间（YYYY-MM-DD）：两个都填了才生效，优先于「近 N 天」 */
  const [rangeStart, setRangeStart] = useState("");
  const [rangeEnd, setRangeEnd] = useState("");
  const [stats, setStats] = useState<ModelCallStats | null>(null);
  const [logs, setLogs] = useState<ModelCallInfo[]>([]);
  const [onlyFailed, setOnlyFailed] = useState(false);
  /** 输入框里的关键词（**改它不查询**——逐字符发请求既浪费又闪） */
  const [keyword, setKeyword] = useState("");
  /** 真正用于查询的关键词（点「查询」或回车才同步） */
  const [appliedKeyword, setAppliedKeyword] = useState("");
  /** 手动触发一次重查（点「查询」时条件没变也得再查一次） */
  const [tick, setTick] = useState(0);
  const [loading, setLoading] = useState(false);
  /** 明细总条数（当前过滤条件下；分页进度「已显示 X / 共 Y 条」用） */
  const [total, setTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  /** 打开的调用详情（call id；null = 未打开） */
  const [detailId, setDetailId] = useState<string | null>(null);

  const custom = !!(rangeStart && rangeEnd);
  const rangeInvalid = custom && rangeStart > rangeEnd;
  /** 只填了一头：等另一头填完再查（避免"看着像筛了、其实没筛"） */
  const rangePartial = !rangeStart !== !rangeEnd;

  const load = useCallback(async () => {
    if (rangePartial) return;
    if (rangeInvalid) {
      toast.error("开始日期不能晚于结束日期");
      return;
    }
    setLoading(true);
    try {
      const win = custom ? { start: rangeStart, end: rangeEnd } : { days };
      const [s, l] = await Promise.all([
        api.modelCallStats({ days, ...win }),
        api.listModelCalls({
          ...win, limit: PAGE_SIZE, offset: 0, onlyFailed, q: appliedKeyword,
        }),
      ]);
      setStats(s);
      setLogs(l.items);
      setTotal(l.total ?? l.items.length);
    } catch (e) {
      toast.error(`调用数据加载失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setLoading(false);
    }
  }, [days, custom, rangePartial, rangeInvalid, rangeStart, rangeEnd,
      onlyFailed, appliedKeyword, tick]);

  /** 分页：把下一页追加到明细列表（筛选条件变化时由 load 重置回第一页） */
  const loadMore = useCallback(async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      const win = custom ? { start: rangeStart, end: rangeEnd } : { days };
      const l = await api.listModelCalls({
        ...win, limit: PAGE_SIZE, offset: logs.length,
        onlyFailed, q: appliedKeyword,
      });
      setLogs((prev) => [...prev, ...l.items]);
      setTotal(l.total ?? logs.length + l.items.length);
    } catch (e) {
      toast.error(`加载更多失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, custom, rangeStart, rangeEnd, days, logs.length,
      onlyFailed, appliedKeyword]);

  /** 点「查询」/ 回车：把输入框里的关键词生效并强制重查一次 */
  const applyFilters = () => {
    setAppliedKeyword(keyword.trim());
    setTick((n) => n + 1);
  };

  useEffect(() => {
    void load();
  }, [load]);

  /** 按天趋势的柱子高度：以窗口内最大调用量为基准（全 0 时给 4px 占位） */
  const dayMax = useMemo(
    () => Math.max(1, ...(stats?.by_day ?? []).map((d) => d.calls)),
    [stats],
  );

  return (
    <div className="calls-wrap">
      <div className="calls-head">
        <h3 className="calls-title">调用日志</h3>
        <div className="calls-controls">
          <span className="calls-label">时间范围</span>
          {WINDOW_DAYS.map((d) => (
            <button
              key={d}
              className={`calls-chip${!custom && days === d ? " on" : ""}`}
              title={d === 0
                ? "不限时间（看板与调用明细同时生效，明细可用「加载更多」翻历史）"
                : `最近 ${d} 天（看板与调用明细同时生效）`}
              onClick={() => {
                setDays(d);
                setRangeStart("");
                setRangeEnd("");
              }}
            >
              {d === 0 ? "全部" : `近 ${d} 天`}
            </button>
          ))}
          {/* 自定义区间：两端都填才生效（后端按「含结束当天」处理） */}
          <span className="calls-range">
            <input
              type="date"
              className={`calls-date${custom ? " on" : ""}`}
              value={rangeStart}
              max={rangeEnd || undefined}
              aria-label="开始日期"
              onChange={(e) => setRangeStart(e.target.value)}
            />
            <span className="calls-range-sep">至</span>
            <input
              type="date"
              className={`calls-date${custom ? " on" : ""}`}
              value={rangeEnd}
              min={rangeStart || undefined}
              aria-label="结束日期"
              onChange={(e) => setRangeEnd(e.target.value)}
            />
          </span>
          <button
            className="calls-chip"
            disabled={loading || rangeInvalid || rangePartial}
            title={
              rangeInvalid
                ? "开始日期不能晚于结束日期"
                : rangePartial
                  ? "自定义区间要填完开始与结束两个日期"
                  : "按当前条件重新查询"
            }
            onClick={applyFilters}
          >
            {loading ? "查询中…" : "查询"}
          </button>
        </div>
      </div>

      {stats && (
        <>
          <div className="calls-cards">
            <div className="calls-card">
              <span className="calls-card-num">{fmtNum(stats.total.calls)}</span>
              <span className="calls-card-label">调用总数</span>
            </div>
            <div className="calls-card">
              <span className="calls-card-num danger">{fmtNum(stats.total.failed)}</span>
              <span className="calls-card-label">
                失败（{stats.total.calls
                  ? Math.round((stats.total.failed / stats.total.calls) * 100)
                  : 0}
                %）
              </span>
            </div>
            <div className="calls-card">
              <span className="calls-card-num">{fmtNum(stats.consumption.images)}</span>
              <span className="calls-card-label">出图张数</span>
            </div>
            <div className="calls-card">
              <span className="calls-card-num">{fmtNum(stats.consumption.video_seconds)}</span>
              <span className="calls-card-label">视频秒数</span>
            </div>
            <div className="calls-card">
              <span className="calls-card-num">{fmtNum(stats.consumption.tokens)}</span>
              <span className="calls-card-label">token</span>
            </div>
          </div>

          {stats.by_day.length > 0 && (
            <div className="calls-section">
              <div className="calls-section-title">按天调用量（红＝其中失败）</div>
              <div className="calls-days">
                {stats.by_day.map((d) => (
                  <div key={d.date} className="calls-day" title={`${d.date}：${d.calls} 次，失败 ${d.failed}`}>
                    <div className="calls-day-bar" style={{ height: `${Math.max(4, Math.round((d.calls / dayMax) * 72))}px` }}>
                      <div
                        className="calls-day-fail"
                        style={{ height: `${d.calls ? Math.round((d.failed / d.calls) * 100) : 0}%` }}
                      />
                    </div>
                    <span className="calls-day-label">{d.date.slice(5)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="calls-section">
            <div className="calls-section-title">
              按调用点 / 端点 / 模型（点「只看失败」可筛日志）
            </div>
            <div className="calls-table-scroll">
              <table className="calls-table">
                <thead>
                  <tr>
                    <th>调用点</th><th>域名</th><th>模型</th>
                    <th>次数</th><th>失败</th><th>平均耗时</th>
                    <th>出图</th><th>视频秒</th><th>token</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.by_model.map((m) => (
                    <tr key={`${m.what}|${m.host}|${m.model}`}>
                      <td>{m.what || "—"}</td>
                      <td className="calls-mono">{m.host || "—"}</td>
                      <td className="calls-mono">{m.model || "—"}</td>
                      <td>{fmtNum(m.calls)}</td>
                      <td className={m.failed ? "calls-bad" : ""}>{fmtNum(m.failed)}</td>
                      <td>{fmtMs(m.avg_ms)}</td>
                      <td>{fmtNum(m.images)}</td>
                      <td>{fmtNum(m.video_seconds)}</td>
                      <td>{fmtNum(m.tokens)}</td>
                    </tr>
                  ))}
                  {stats.by_model.length === 0 && (
                    <tr><td colSpan={9} className="calls-empty">这段时间没有调用记录</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      <div className="calls-section">
        <div className="calls-section-title">
          调用明细（{stats?.window.label ?? ""}，新的在前；分页加载）
        </div>
        <div className="calls-filters">
          <label className="calls-filter">
            <input
              type="checkbox"
              checked={onlyFailed}
              onChange={(e) => setOnlyFailed(e.target.checked)}
            />
            只看失败
          </label>
          <input
            className="calls-search"
            placeholder="搜索：调用点 / 域名 / 模型 / 错误信息（回车查询）"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") applyFilters(); }}
          />
          <button className="calls-chip" disabled={loading} onClick={applyFilters}>
            查询
          </button>
        </div>
        <div className="calls-log">
          {logs.map((it) => (
            <div key={it.id} className={`calls-row${it.ok ? "" : " bad"}`}>
              <div className="calls-row-head">
                <span className="calls-time">{fmtTime(it.at)}</span>
                <span className="calls-what">{it.what || "—"}</span>
                <span className={`calls-badge${it.ok ? " ok" : " bad"}`}>
                  {it.ok ? "成功" : (FAIL_CLASS_LABEL[it.fail_class] ?? it.fail_class) || "失败"}
                </span>
                {!!it.http_status && !it.ok && (
                  <span className="calls-status">HTTP {it.http_status}</span>
                )}
                <span className="calls-ms">{fmtMs(it.elapsed_ms || 0)}</span>
                <button
                  className="calls-detail-btn"
                  title="查看这次调用的完整内容（接口入参 / 响应 / 错误）"
                  onClick={() => setDetailId(it.id)}
                >
                  查看详情
                </button>
              </div>
              <div className="calls-row-sub">
                <span className="calls-mono">{it.host || "—"}</span>
                <span className="calls-mono">{it.model || "—"}</span>
                <span>{usageText(it.usage)}</span>
              </div>
              {!it.ok && it.error && (
                <div className="calls-row-err" title={it.error}>{it.error}</div>
              )}
            </div>
          ))}
          {logs.length === 0 && (
            <div className="calls-empty">
              {loading ? "加载中…" : "没有符合条件的调用记录"}
            </div>
          )}
        </div>
        {logs.length > 0 && (
          <div className="calls-more">
            {logs.length < total ? (
              <button
                className="calls-chip"
                disabled={loadingMore}
                onClick={() => void loadMore()}
              >
                {loadingMore
                  ? "加载中…"
                  : `加载更多（已显示 ${fmtNum(logs.length)} / 共 ${fmtNum(total)} 条）`}
              </button>
            ) : (
              <span className="calls-more-end">已到末尾（共 {fmtNum(total)} 条）</span>
            )}
          </div>
        )}
      </div>
      {detailId && (
        <CallDetailDialog callId={detailId} onClose={() => setDetailId(null)} />
      )}
    </div>
  );
}

/** 调用详情弹窗（「查看详情」）：单次对外模型调用的全部留痕。
 *
 * 内容：基础信息（调用点/端点/模型/结果/耗时/消费/项目/任务/完整地址）+
 * **接口入参**、**响应**、错误信息三块长文本（各自可复制）。
 * 留痕是**按需拉取**的（列表接口不带这两个大字段，避免一屏 200 条把响应撑爆）；
 * 服务端写入时已打码敏感字段并截断超长内容（截断处带原文长度标记）。
 */
function CallDetailDialog({ callId, onClose }: { callId: string; onClose: () => void }) {
  const [call, setCall] = useState<ModelCallDetail | null>(null);
  const [failed, setFailed] = useState("");
  useEffect(() => {
    let alive = true;
    api.getModelCall(callId)
      .then((r) => { if (alive) setCall(r.call); })
      .catch((e) => {
        if (alive) setFailed(e instanceof Error ? e.message : String(e));
      });
    return () => { alive = false; };
  }, [callId]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  /** JSON 字符串 → 缩进美化（不是 JSON 就原样显示，不报错） */
  const pretty = (raw: string): string => {
    if (!raw) return "";
    try {
      return JSON.stringify(JSON.parse(raw), null, 2);
    } catch {
      return raw;
    }
  };
  const copy = (text: string, label: string) => {
    void navigator.clipboard.writeText(text)
      .then(() => toast.success(`${label}已复制`))
      .catch(() => toast.error("复制失败（浏览器未授权剪贴板）"));
  };

  // portal 到 body：调用方可能位于带 transform 的容器内（与提示词弹窗同因）
  return createPortal(
    <div className="settings-mask">
      <div className="settings-dialog call-detail-dialog">
        <div className="settings-head">
          <span className="settings-title">调用详情</span>
          <button className="provider-close" onClick={onClose}>关闭</button>
        </div>
        <div className="project-settings-body">
          {failed && <div className="calls-empty">加载失败：{failed}</div>}
          {!call && !failed && <div className="provider-loading">加载中…</div>}
          {call && (
            <>
              <div className="call-detail-meta">
                <span>时间</span><b>{fmtTime(call.at)}</b>
                <span>调用点</span><b>{call.what || "—"}</b>
                <span>端点</span><b className="calls-mono">{call.host || "—"}</b>
                <span>模型</span><b className="calls-mono">{call.model || "—"}</b>
                <span>结果</span>
                <b className={call.ok ? "" : "calls-bad"}>
                  {call.ok
                    ? "成功"
                    : `${FAIL_CLASS_LABEL[call.fail_class]
                        ?? (call.fail_class || "失败")}`
                      + (call.http_status ? `（HTTP ${call.http_status}）` : "")}
                </b>
                <span>耗时</span><b>{fmtMs(call.elapsed_ms || 0)}</b>
                <span>消费</span><b>{usageText(call.usage)}</b>
                <span>项目</span><b className="calls-mono">{call.project_id || "—"}</b>
                <span>任务</span><b className="calls-mono">{call.task_id || "—"}</b>
                <span>地址</span><b className="calls-mono call-detail-url">{call.url || "—"}</b>
              </div>
              {call.error && (
                <div className="call-detail-block">
                  <div className="call-detail-head">
                    <span>错误信息</span>
                    <button className="link-btn" onClick={() => copy(call.error, "错误信息")}>
                      复制
                    </button>
                  </div>
                  <pre className="call-detail-pre">{call.error}</pre>
                </div>
              )}
              <div className="call-detail-block">
                <div className="call-detail-head">
                  <span>接口入参</span>
                  <button
                    className="link-btn"
                    disabled={!call.request_json}
                    onClick={() => copy(pretty(call.request_json), "接口入参")}
                  >
                    复制
                  </button>
                </div>
                <pre className="call-detail-pre">
                  {pretty(call.request_json) || "（这次调用没有留痕入参）"}
                </pre>
              </div>
              <div className="call-detail-block">
                <div className="call-detail-head">
                  <span>响应</span>
                  <button
                    className="link-btn"
                    disabled={!call.response_json}
                    onClick={() => copy(pretty(call.response_json), "响应内容")}
                  >
                    复制
                  </button>
                </div>
                <pre className="call-detail-pre">
                  {pretty(call.response_json) || "（这次调用没有留痕响应）"}
                </pre>
              </div>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
