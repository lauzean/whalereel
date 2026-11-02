/** 短剧工作台主视图：左侧三个主菜单（剧本 / 资产 / 分集）+ 右侧铺满的工作区。
 *
 * 布局依据 docs/prototype/drama-ui.html（已定稿交互原型）：
 * - 左栏 132px 主菜单，当前项左侧 2px 蓝条高亮；
 * - 剧本页 = 左编辑器（铺满）+ 右「剧本解析」（过程 + 分集计划）；
 * - 资产页 = 三个水平 tab（角色/场景/道具，带数量角标）+ 原有卡片流；
 * - 分集页 = 左分集列表 + 右当前集工作台（水平步骤条驱动）。
 *
 * 为什么合并入口：旧版把「角色与场景」「AI 过程」做成并列菜单，逐集流水线下用户
 * 要在四个入口之间来回找“现在该做什么”。逐集后资产按集解析（资产页只管维护）、
 * 观测并入剧本页的解析面板即可，三个入口正好对应「剧本 → 资产 → 分集」三步。
 */

import { useEffect, useState } from "react";
import {
  subscribeTasks,
  type EpisodeInfo,
  type ModelPresetMap,
  type ProjectInfo,
} from "../api/client";
import { useDrama } from "../stores/drama";
import { BILLING_KEYS } from "../billingKeys";
import { costLine } from "../stores/billing";
import { CreditBalance } from "../ui/CreditBalance";
import { confirmDialog, toast } from "../ui/feedback";
import { AssetsTab } from "./AssetsTab";
import { EpisodeList } from "./EpisodeList";
import { EpisodeWorkspace } from "./EpisodeWorkspace";
import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import { ScriptTab } from "./ScriptTab";

/** 画风预设（首页选择与工作台标识共用，M3 6.8） */
export const DRAMA_STYLES = [
  { key: "real", label: "写实电影感" },
  { key: "3d", label: "3D 动画" },
  { key: "anime", label: "二次元" },
  { key: "guofeng", label: "国风漫画" },
] as const;

export function styleLabel(key: string): string {
  return DRAMA_STYLES.find((s) => s.key === key)?.label ?? key;
}

/** 路由段：episodes（可带集号段 episodes/:seq）/ script / assets。
 *  observe 是 v1.0 的旧链接段（观测已并入剧本页），保留只为兼容旧 URL */
export type DramaSection = "episodes" | "script" | "assets" | "observe";

/** 左侧主菜单（顺序与原型一致：剧本 / 资产 / 分集） */
const MENU: { key: DramaSection; title: string }[] = [
  { key: "script", title: "剧本" },
  { key: "assets", title: "资产" },
  { key: "episodes", title: "分集" },
];

export function DramaView({
  project,
  section,
  epSeq,
  onNavigate,
  onBack,
  onProjectUpdate,
}: {
  project: {
    id: string;
    name: string;
    style_preset: string;
    /** 风格锚点（美学第二级，可空）：项目设置弹窗据此回显 */
    style_anchor?: string;
    model_presets?: ModelPresetMap;
  };
  /** 当前分区（路由驱动）：episodes | script | assets | observe */
  section: DramaSection;
  /** 当前集号（仅 episodes 分区有意义；null = 未指定，落第一集） */
  epSeq: number | null;
  /** 切换分区/集号（App 层写路由，刷新后原地恢复） */
  onNavigate: (section: DramaSection, seq?: number) => void;
  onBack: () => void;
  onProjectUpdate: (p: ProjectInfo) => void;
}) {
  const [showSettings, setShowSettings] = useState(false);
  const {
    loadAll, handleTaskEvent, script, episodes, setModelPresets, replanEpisodes,
    wsConnected,
  } = useDrama();

  // 旧链接（#/project/:id/observe）落到剧本页：观测已并入剧本页的「剧本解析」面板
  const page: DramaSection = section === "observe" ? "script" : section;

  // 项目模型预设同步进 store：资产卡片/故事板的模型选择器默认值对齐项目设置
  useEffect(() => {
    setModelPresets(project.model_presets ?? null);
  }, [project, setModelPresets]);

  useEffect(() => {
    loadAll(project.id).catch(() => {});
    const unsub = subscribeTasks(project.id, (event, data) => handleTaskEvent(event, data));
    return unsub;
  }, [project.id, loadAll, handleTaskEvent]);

  // 当前集：URL 指定则用它，否则落第一集（分集加载完成后自动补正 URL）
  const active: EpisodeInfo | null =
    episodes.find((e) => e.seq === epSeq) ?? episodes[0] ?? null;
  useEffect(() => {
    if (page === "episodes" && active && active.seq !== epSeq) {
      onNavigate("episodes", active.seq);
    }
  }, [page, active, epSeq, onNavigate]);

  // 重新分集：两段式（先预检代价——重切边界会让旧分镜不再对应）
  const runReplan = async () => {
    const pre = await replanEpisodes(false);
    if (!pre) return;
    const ok = await confirmDialog({
      title: "按剧情重新分集",
      message: `AI 将按剧情走向/爆点/钩子重新切分分集边界。`
        + `当前 ${pre.episodes ?? 0} 集 / ${pre.shots ?? 0} 个分镜会被清空重建`
        + `（角色、场景、道具等资产保留）。确定继续吗？`
        + (costLine(BILLING_KEYS.episodeReplan)
            ? `\n\n${costLine(BILLING_KEYS.episodeReplan)}` : ""),
      confirmText: "重新分集",
      danger: (pre.shots ?? 0) > 0,
    });
    if (!ok) return;
    const res = await replanEpisodes(true);
    if (res) toast.success("重新分集任务已提交，进度见「剧本」页的剧本解析面板");
  };

  return (
    <div className="drama">
      <header className="app-header">
        <button className="back" onClick={onBack}>
          返回
        </button>
        <span>{project.name}</span>
        {/* 当前生效解析版本号（随解析/保留/恢复历史版本联动） */}
        {script?.version ? (
          <span className="style-tag version-tag" title="当前生效解析结果版本">
            V{script.version}
          </span>
        ) : null}
        <span className="style-tag">{styleLabel(project.style_preset)}</span>
        {/* 实时通道状态点（2026-09-30 复查 F5：短剧工作台此前断线不可见——与画布
            同一枚样式；断线期间事件会丢，重连时自动补拉对齐） */}
        <span
          className={`ws-dot ${wsConnected ? "on" : "off"}`}
          title={wsConnected
            ? "实时通道已连接"
            : "实时通道已断开（正在自动重连；进度会在重连后自动对齐）"}
        />
        {/* 积分余额（33 号方案）：消费大多发生在工作台里，这里常显 */}
        <CreditBalance />
        <button className="ghost-btn header-right" onClick={() => setShowSettings(true)}>
          项目设置
        </button>
      </header>

      <div className="drama-main">
        {/* 左侧主菜单：当前项左侧 2px 蓝条（原型 .menu button.on） */}
        <nav className="drama-menu">
          {MENU.map((m) => (
            <button
              key={m.key}
              className={page === m.key ? "on" : ""}
              onClick={() => onNavigate(m.key, m.key === "episodes" ? active?.seq : undefined)}
            >
              {m.title}
            </button>
          ))}
        </nav>

        <main className="drama-stage">
          {page === "script" && <ScriptTab />}
          {page === "assets" && <AssetsTab />}

          {page === "episodes" && (
            active ? (
              <>
                <aside className="ep-side">
                  {/* 重新分集是全局动作（会重切所有集边界），由 EpisodeList 渲染在列表末尾 */}
                  <EpisodeList
                    activeSeq={active.seq}
                    onPick={(ep) => onNavigate("episodes", ep.seq)}
                    onReplan={() => void runReplan()}
                  />
                </aside>
                <EpisodeWorkspace episode={active} />
              </>
            ) : (
              <div className="drama-empty">
                <p>还没有分集。</p>
                <button className="gen-btn primary" onClick={() => onNavigate("script")}>
                  去「剧本」页 AI 解析
                </button>
              </div>
            )
          )}
        </main>
      </div>

      {showSettings && (
        <ProjectSettingsDialog
          project={project}
          onClose={() => setShowSettings(false)}
          onSaved={onProjectUpdate}
        />
      )}
    </div>
  );
}
