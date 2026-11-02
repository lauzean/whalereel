/** 模型管理内容区：列表 + 下载进度 + 安装/卸载 + 推理依赖引导（嵌入系统配置页） */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type InstallStatus, type ModelInfo } from "./api/client";
import { confirmDialog, toast } from "./ui/feedback";

/** 任务类别标签（与后端 task_type 对齐） */
const TASK_LABELS: Record<string, string> = {
  t2v: "文生视频",
  i2v: "图生视频",
  fl2v: "首尾帧",
  ref2v: "参考生视频",
  t2i: "文生图",
  edit: "编辑",
  tts: "配音",
  llm: "对话模型",
};

/** 模型管理内容区（系统配置页右侧）：模型列表与下载管理 */
export function ModelManagerContent() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [deps, setDeps] = useState<{ missing: string[]; install: { status: string; log: string; error: string } } | null>(null);
  const [depsInstalling, setDepsInstalling] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const depsTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const refreshingRef = useRef(false);

  /** 轮询模型列表（下载中需持续刷新进度）；上一次请求未完成时跳过本轮，避免堆积。
   *  轮询 interval 的启停在此处统一收口：挂载、点击安装/取消/卸载后的刷新
   *  都经过本函数，依据最新任务状态自动续上或停掉定时器——否则「先开弹窗
   *  后点安装」时无人启动轮询，进度会永远停留在点击瞬间的快照。 */
  const refresh = useCallback(async (): Promise<boolean | null> => {
    if (refreshingRef.current) return null;
    refreshingRef.current = true;
    try {
      const data = await api.listModels();
      setModels(data);
      const busy = data.some((m) =>
        ["queued", "downloading", "canceling"].includes(m.install?.status ?? "idle"),
      );
      if (busy && !timerRef.current) {
        timerRef.current = setInterval(() => void refresh(), 1500);
      } else if (!busy && timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      return busy;
    } catch {
      return false;
    } finally {
      refreshingRef.current = false;
    }
  }, []);

  useEffect(() => {
    api.depsStatus().then(setDeps).catch(() => {});
    void refresh().finally(() => setLoading(false));
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      if (depsTimerRef.current) clearInterval(depsTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const stopDepsPoll = () => {
    if (depsTimerRef.current) {
      clearInterval(depsTimerRef.current);
      depsTimerRef.current = null;
    }
  };

  const installDeps = async () => {
    stopDepsPoll();
    setDepsInstalling(true);
    try {
      await api.installDeps();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
      setDepsInstalling(false);
      return;
    }
    let misses = 0;
    depsTimerRef.current = setInterval(() => {
      void (async () => {
        try {
          const d = await api.depsStatus();
          setDeps(d);
          misses = 0;
          const st = d.install.status;
          if (st === "done" || st === "failed" || st === "idle") {
            stopDepsPoll();
            setDepsInstalling(false);
          }
        } catch {
          misses += 1;
          if (misses >= 5) {
            stopDepsPoll();
            setDepsInstalling(false);
          }
        }
      })();
    }, 2000);
  };

  const renderStatus = (m: ModelInfo) => {
    const st = m.install?.status ?? "idle";
    // 活跃下载（含取消中）优先于 installed 展示：下载中的目录也非空，
    // installed 的毛刺不能盖过进行中的任务
    if (st === "queued" || st === "downloading") {
      return (
        <div className="mm-progress">
          <div className="mm-bar">
            <div className="mm-bar-fill" style={{ width: `${(m.install.progress ?? 0) * 100}%` }} />
          </div>
          <span className="mm-bar-text">
            {((m.install.progress ?? 0) * 100).toFixed(1)}% · {m.install.message}
          </span>
        </div>
      );
    }
    if (st === "canceling") {
      return <span className="mm-status mm-warn">正在取消…</span>;
    }
    if (st === "done" || m.installed) {
      return <span className="mm-status mm-ok">已安装</span>;
    }
    if (st === "canceled") {
      return <span className="mm-status mm-warn">已取消（可续传）</span>;
    }
    if (st === "failed") {
      return <span className="mm-status mm-err" title={m.install.error}>下载失败</span>;
    }
    return <span className="mm-status mm-idle">未安装</span>;
  };

  const renderAction = (m: ModelInfo) => {
    const st = m.install?.status ?? "idle";
    if (st === "queued" || st === "downloading" || st === "canceling") {
      return (
        <button className="mm-btn" onClick={() => act(() => api.cancelModel(m.id))}>
          取消
        </button>
      );
    }
    if (st === "done" || m.installed) {
      return (
        <button
          className="mm-btn mm-btn-ghost"
          onClick={() => {
            void confirmDialog({
              title: "卸载模型",
              message: `卸载模型「${m.display_name}」？将删除本地权重。`,
              confirmText: "卸载",
              danger: true,
            }).then((ok) => {
              if (ok) void act(() => api.uninstallModel(m.id));
            });
          }}
        >
          卸载
        </button>
      );
    }
    return (
      <button
        className="mm-btn mm-btn-primary"
        onClick={() => void act(() => api.installModel(m.id))}
      >
        安装
      </button>
    );
  };

  const depsInstallingFlag =
    depsInstalling || deps?.install.status === "installing";

  return (
    <div className="provider-body">
      {deps && (deps.missing.length > 0 || deps.install.status === "installing") && (
        <div className="mm-deps">
          <div className="mm-deps-title">
            推理依赖缺失：{deps.missing.join(" / ")}
          </div>
          {depsInstallingFlag ? (
            <div className="mm-deps-running">正在安装（pip install -e .[inference]）…</div>
          ) : (
            <button className="mm-btn mm-btn-primary" onClick={() => void installDeps()}>
              一键安装推理依赖
            </button>
          )}
          {deps.install.status === "done" && (
            <div className="mm-deps-ok">推理依赖安装完成，请重启后端生效</div>
          )}
          {deps.install.status === "failed" && (
            <div className="mm-deps-err">{deps.install.error}</div>
          )}
        </div>
      )}
      {loading ? (
        <div className="provider-loading">加载中...</div>
      ) : (
        <div className="mm-list">
          {models.map((m) => (
            <div key={m.id} className="mm-item">
              <div className="mm-info">
                <div className="mm-name">{m.display_name}</div>
                <div className="mm-meta">
                  {TASK_LABELS[m.task_type] ?? m.task_type} · 显存约{" "}
                  {m.vram_mb > 0 ? `${Math.round(m.vram_mb / 1024)}GB` : "无需"} ·{" "}
                  {m.quant || "标准版"} · 下载源{" "}
                  {m.ms_repo ? "ModelScope 官方" : "HF"}
                </div>
                {m.notes && <div className="mm-notes">{m.notes}</div>}
              </div>
              <div className="mm-right">{renderStatus(m)}</div>
              <div className="mm-right">{renderAction(m)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
