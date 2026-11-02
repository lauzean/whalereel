/** 画布视图：React Flow 宿主 + 左侧节点菜单 + Agent 对话面板 + 左下角导航栏（v0.2 4.2/4.11/4.12/5.1）。 */

import {
  Background,
  BackgroundVariant,
  MiniMap,
  ReactFlow,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type Edge,
  type IsValidConnection,
  type Node,
  type NodeRemoveChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, subscribeTasks, type CharacterInfo, type NodeVersionInfo, type SceneInfo } from "../api/client";
import { warmKnownModelIds } from "../api/modelOptions";
import { BILLING_KEYS } from "../billingKeys";
import { ChatPanel } from "../chat/ChatPanel";
import { useCanvas, GROUP_COLORS, type StudioNodeData } from "../stores/canvas";
import { useChat } from "../stores/chat";
import { creditsFor, useBilling, useCostTip } from "../stores/billing";
import { AssetsPanel } from "./AssetsPanel";
import { GroupNode } from "./GroupNode";
import { autoLayout } from "./layout";
import { runAll, runNode } from "./run";
import { StudioNode } from "./StudioNode";
import { NODE_CATEGORIES, NODE_SPECS } from "./specs";
import { confirmDialog, promptDialog, toast } from "../ui/feedback";
import { useIsPhone } from "../ui/useIsPhone";

const nodeTypes = { studio: StudioNode, group: GroupNode };

// 图片裁剪预设比例（与后端 CROP_RATIOS 一致，4.9 裁剪工具）
const CROP_RATIOS = ["1:1", "4:3", "3:4", "16:9", "9:16"];

// ---------- 触摸端手势与浮层常量 ----------

/** 长按判定时长（毫秒）：短于此会与单击混淆，长于此手感发滞 */
const LONG_PRESS_MS = 480;
/** 长按期间允许的手指位移（像素），超过即视为拖动（拖画布/拖节点）而取消长按。
    与下面的节点拖动阈值同源：长按容差必须大于拖动阈值，否则按下即被判成拖动。 */
const LONG_PRESS_MOVE_PX = 10;
/** 节点拖动阈值：鼠标（React Flow 默认 1px，精准指针不需要余量） */
const NODE_DRAG_THRESHOLD_MOUSE = 1;
/** 节点拖动阈值：触摸（手指按下天然有 1-2px 抖动，沿用 1px 会把「平移画布」判成拖节点） */
const NODE_DRAG_THRESHOLD_TOUCH = 8;
/** 右键菜单的估算尺寸与屏幕边距：用于把落点收敛在视口内（触摸端点按常在屏幕边缘） */
const MENU_ESTIMATE_W = 200;
const MENU_ESTIMATE_H = 300;
const MENU_VIEWPORT_MARGIN = 8;

/** 菜单落点收敛在视口内（原实现直接用 clientX/clientY，屏幕边缘处菜单会溢出点不到） */
function clampMenuPos(x: number, y: number): { x: number; y: number } {
  return {
    x: Math.max(MENU_VIEWPORT_MARGIN,
      Math.min(x, window.innerWidth - MENU_ESTIMATE_W - MENU_VIEWPORT_MARGIN)),
    y: Math.max(MENU_VIEWPORT_MARGIN,
      Math.min(y, window.innerHeight - MENU_ESTIMATE_H - MENU_VIEWPORT_MARGIN)),
  };
}

// ---------- 左侧节点菜单（新建 / 资产库 双 tab） ----------

/** 左侧节点面板。open 只在触摸端有意义：手机档它是底部弹层（.palette.m-open
    由 styles-mobile.css 展开），桌面端常驻左栏、该状态不产生任何样式。 */
function NodePalette({ open, onPick }: { open: boolean; onPick: () => void }) {
  const addNode = useCanvas((s) => s.addNode);
  const [tab, setTab] = useState<"nodes" | "assets">("nodes");
  const specs = Object.values(NODE_SPECS);
  return (
    <aside className={`palette${open ? " m-open" : ""}`}>
      <div className="palette-tabs">
        <button className={tab === "nodes" ? "on" : ""} onClick={() => setTab("nodes")}>
          新建节点
        </button>
        <button className={tab === "assets" ? "on" : ""} onClick={() => setTab("assets")}>
          资产库
        </button>
      </div>
      {tab === "assets" && <AssetsPanel />}
      {tab === "nodes" && NODE_CATEGORIES.map((cat) => {
        const items = specs.filter((s) => s.category === cat.key);
        if (!items.length) return null;
        return (
          <div key={cat.key} className="palette-group">
            <div className="palette-subtitle">{cat.title}</div>
            {items.map((s) => (
              <button
                key={s.type}
                className="palette-item"
                title={`新建${s.title}节点`}
                onClick={() => {
                  addNode(s.type, {
                    x: 200 + Math.random() * 300,
                    y: 150 + Math.random() * 200,
                  });
                  // 触摸端：建完节点即收起弹层，把画布还给用户
                  onPick();
                }}
              >
                <span className="palette-icon">{s.icon}</span>
                {s.title}
              </button>
            ))}
          </div>
        );
      })}
    </aside>
  );
}

// ---------- 顶栏 ----------

function Toolbar() {
  const { save, refreshTasks, dirty, nodes } = useCanvas();
  const balance = useBilling((s) => s.account?.balance);
  const hasExport = nodes.some((n) => n.data.specType === "export");
  // 「全部运行」按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const runAllCostTip = useCostTip(BILLING_KEYS.createTask);

  /** 全部运行（33 号方案：付费副作用确认前置）——每个可运行节点 = 一次
   *  POST /api/tasks = 扣一次费；确认框把「节点数 × 单价 × 合计」摆明白。 */
  const runAllWithConfirm = async () => {
    const runnable = nodes.filter(
      (n) => NODE_SPECS[n.data.specType]?.runnerHint).length;
    const per = creditsFor(BILLING_KEYS.createTask);
    const costText = per !== null && per > 0
      ? `\n\n共 ${runnable} 个可运行节点，按当前价目每个节点消耗 ${per} 积分`
        + `（合计约 ${runnable * per} 积分`
        + (balance !== undefined ? `，当前余额 ${balance}` : "") + "）。"
      : "";
    const ok = await confirmDialog({
      title: "全部运行",
      message: "将按拓扑顺序依次生成全部节点（失败节点会阻断其下游）。"
        + costText,
      confirmText: "开始运行",
    });
    if (!ok) return;
    await runAll().catch((e) => toast.error(`运行失败：${e.message}`));
  };

  return (
    <div className="toolbar">
      <button onClick={() => save().catch((e) => toast.error(`保存失败：${e.message}`))}>
        保存{dirty ? " *" : ""}
      </button>
      <button onClick={() => void runAllWithConfirm()}
              title={runAllCostTip || undefined}>
        全部运行
      </button>
      {hasExport && (
        <button
          onClick={async () => {
            const exportNode = nodes.find((n) => n.data.specType === "export");
            if (exportNode) await runNode(exportNode.id);
          }}
        >
          导出成片
        </button>
      )}
      <button onClick={() => refreshTasks()}>刷新状态</button>
    </div>
  );
}

// ---------- 左下角导航栏（复刻小云雀：重置/隐藏边/整理画布/小地图/网格吸附/缩放） ----------

function BottomNav() {
  const { hideEdges, snapToGrid, showMiniMap, toggleHideEdges, toggleSnapToGrid,
    toggleMiniMap, nodes, edges, setNodes, wsConnected } = useCanvas();
  const { fitView, zoomIn, zoomOut, getZoom } = useReactFlow();
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    const t = setInterval(() => setZoom(getZoom()), 500);
    return () => clearInterval(t);
  }, [getZoom]);

  const tidy = (mode: "horizontal" | "vertical") => {
    const laid = autoLayout(nodes, edges, mode);
    setNodes(laid);
    setTimeout(() => fitView({ duration: 400 }), 50);
  };

  return (
    <div className="bottom-nav">
      <span
        className={`ws-dot ${wsConnected ? "on" : "off"}`}
        title={wsConnected ? "实时通道已连接" : "实时通道已断开（正在自动重连，进度靠轮询兜底）"}
      />
      <button title="重置视角" onClick={() => fitView({ duration: 300 })}>重置</button>
      <button
        title={hideEdges ? "恢复连线" : "隐藏连线"}
        className={hideEdges ? "on" : ""}
        onClick={toggleHideEdges}
      >
        隐藏边
      </button>
      <button title="横向布局：分镜流从左到右" onClick={() => tidy("horizontal")}>
        整理画布
      </button>
      <button title="纵向布局：参考链从上到下" onClick={() => tidy("vertical")}>
        纵向整理
      </button>
      <button
        title="小地图"
        className={showMiniMap ? "on" : ""}
        onClick={toggleMiniMap}
      >
        小地图
      </button>
      <button
        title="网格吸附"
        className={snapToGrid ? "on" : ""}
        onClick={toggleSnapToGrid}
      >
        网格吸附
      </button>
      <span className="zoom-display">{Math.round(zoom * 100)}%</span>
      <button title="缩小" onClick={() => zoomOut({ duration: 200 })}>-</button>
      <button title="放大" onClick={() => zoomIn({ duration: 200 })}>+</button>
    </div>
  );
}

// ---------- 画布右键菜单 ----------

interface ContextMenu {
  x: number;
  y: number;
  flowX: number;
  flowY: number;
}

function ContextMenuOverlay({ menu, onClose }: { menu: ContextMenu; onClose: () => void }) {
  const addNode = useCanvas((s) => s.addNode);
  const items = ["text", "image", "video", "audio"];
  return (
    <>
      <div className="context-mask" onClick={onClose} onContextMenu={(e) => { e.preventDefault(); onClose(); }} />
      <div className="context-menu" style={{ left: menu.x, top: menu.y }}>
        {items.map((t) => {
          const spec = NODE_SPECS[t];
          return (
            <button
              key={t}
              onClick={() => {
                addNode(t, { x: menu.flowX, y: menu.flowY });
                onClose();
              }}
            >
              新建{spec.title}
            </button>
          );
        })}
      </div>
    </>
  );
}

// ---------- 节点右键菜单（删除 / 应用到角色或场景，4.9 P0） ----------

interface NodeMenu {
  x: number;
  y: number;
  nodeId: string;
  /** 是否由触摸端长按唤出：仅此类菜单提供「多选」（桌面端有 Shift 点选，手机没有） */
  touch?: boolean;
}

function NodeContextMenuOverlay({ menu, onClose }: { menu: NodeMenu; onClose: () => void }) {
  const node = useCanvas((s) => s.nodes.find((n) => n.id === menu.nodeId));
  const deleteNode = useCanvas((s) => s.deleteNode);
  const loadAssets = useCanvas((s) => s.loadAssets);
  const projectId = useCanvas((s) => s.projectId);
  const applyEdited = useCanvas((s) => s.applyEdited);
  const [chars, setChars] = useState<CharacterInfo[]>([]);
  const [scenes, setScenes] = useState<SceneInfo[]>([]);
  const [subOpen, setSubOpen] = useState<null | "char" | "scene" | "crop">(null);
  const [applying, setApplying] = useState(false);
  const [editing, setEditing] = useState(false);

  const isImage = node?.data.artifactKind === "image" && !!node?.data.artifactId;
  const isVideo = node?.data.artifactKind === "video" && !!node?.data.artifactId;
  /** 画布上已选中的普通节点数（触摸端打组入口的显示条件；数字稳定，不会引起多余渲染） */
  const selectedCount = useCanvas(
    (s) => s.nodes.filter((n) => n.selected && n.type !== "group").length,
  );

  // 编辑工具：产物追加为新版本并切为生效（4.9 编辑即新版本，可回退）
  const runEdit = async (fn: (pid: string) => Promise<NodeVersionInfo>) => {
    if (!projectId) return;
    setEditing(true);
    try {
      const version = await fn(projectId);
      applyEdited(menu.nodeId, version);
      onClose();
    } catch (e) {
      toast.error(`编辑失败：${(e as Error).message}`);
    } finally {
      setEditing(false);
    }
  };

  useEffect(() => {
    if (!isImage) return;
    const pid = useCanvas.getState().projectId;
    if (!pid) return;
    api.listCharacters(pid).then(setChars).catch(() => {});
    api.listScenes(pid).then(setScenes).catch(() => {});
  }, [isImage]);

  if (!node) return null;

  const apply = async (target: "char" | "scene", targetId: string) => {
    if (!node.data.artifactId) return;
    setApplying(true);
    try {
      if (target === "char") {
        await api.applyCharacterImage(targetId, node.data.artifactId);
      } else {
        await api.applySceneImage(targetId, node.data.artifactId);
      }
      await loadAssets();
      onClose();
    } catch (e) {
      toast.error(`应用失败：${(e as Error).message}`);
    } finally {
      setApplying(false);
    }
  };

  return (
    <>
      <div className="context-mask" onClick={onClose} onContextMenu={(e) => { e.preventDefault(); onClose(); }} />
      <div className="context-menu" style={{ left: menu.x, top: menu.y }}>
        {isImage && (
          <>
            <button disabled={editing} onClick={() => runEdit((pid) => api.rotateImage(menu.nodeId, pid, "ccw"))}>
              向左旋转 90°
            </button>
            <button disabled={editing} onClick={() => runEdit((pid) => api.rotateImage(menu.nodeId, pid, "cw"))}>
              向右旋转 90°
            </button>
            <button onClick={() => setSubOpen(subOpen === "crop" ? null : "crop")}>
              裁剪比例 ▸
            </button>
            {subOpen === "crop" && (
              <div className="context-submenu">
                {CROP_RATIOS.map((r) => (
                  <button key={r} disabled={editing} onClick={() => runEdit((pid) => api.cropImage(menu.nodeId, pid, r))}>
                    {r}
                  </button>
                ))}
              </div>
            )}
            <div className="context-sep" />
          </>
        )}
        {isVideo && (
          <>
            <button
              disabled={editing}
              onClick={async () => {
                const input = await promptDialog({
                  title: "剪辑区间（秒，格式：开始,结束）",
                  initial: "0,5",
                });
                if (!input) return;
                const parts = input.split(",").map((x) => parseFloat(x.trim()));
                if (parts.length !== 2 || parts.some(isNaN) || parts[1] <= parts[0]) {
                  toast.error("区间无效，请输入如：0,5");
                  return;
                }
                runEdit((pid) => api.trimVideo(menu.nodeId, pid, parts[0], parts[1]));
              }}
            >
              剪辑视频片段
            </button>
            <div className="context-sep" />
          </>
        )}
        {isImage && (
          <>
            <button onClick={() => setSubOpen(subOpen === "char" ? null : "char")}>
              应用为角色形象 {chars.length ? "▸" : ""}
            </button>
            {subOpen === "char" && (
              <div className="context-submenu">
                {chars.length === 0 && <span className="context-sub-empty">项目暂无角色</span>}
                {chars.map((c) => (
                  <button key={c.id} disabled={applying} onClick={() => apply("char", c.id)}>
                    {c.name}
                  </button>
                ))}
              </div>
            )}
            <button onClick={() => setSubOpen(subOpen === "scene" ? null : "scene")}>
              应用为场景主图 {scenes.length ? "▸" : ""}
            </button>
            {subOpen === "scene" && (
              <div className="context-submenu">
                {scenes.length === 0 && <span className="context-sub-empty">项目暂无场景</span>}
                {scenes.map((s) => (
                  <button key={s.id} disabled={applying} onClick={() => apply("scene", s.id)}>
                    {s.name}
                  </button>
                ))}
              </div>
            )}
            <div className="context-sep" />
          </>
        )}
        <button
          className="danger"
          onClick={() => {
            deleteNode(menu.nodeId);
            onClose();
          }}
        >
          删除节点
        </button>
        {menu.touch && (
          <>
            <div className="context-sep" />
            {/* 触摸端没有 Shift 点选：靠它凑齐「同类型 ≥2」后才会出现拼接浮动条 */}
            <button
              onClick={() => {
                const state = useCanvas.getState();
                state.setNodes(
                  state.nodes.map((n) =>
                    n.id === menu.nodeId ? { ...n, selected: !n.selected } : n),
                );
                onClose();
              }}
            >
              {node.selected ? "取消选中" : "选中（可拼接）"}
            </button>
            {/* 打组在桌面走 Ctrl+G 快捷键，手机没有键盘 → 选中 ≥2 个后在这里给入口 */}
            {selectedCount >= 2 && (
              <button
                onClick={() => {
                  const state = useCanvas.getState();
                  state.createGroup(
                    state.nodes.filter((n) => n.selected && n.type !== "group").map((n) => n.id),
                  );
                  onClose();
                }}
              >
                打组（{selectedCount} 个节点）
              </button>
            )}
          </>
        )}
      </div>
    </>
  );
}

// ---------- 组右键菜单（重命名/换色/批量运行/解散/删除成员，4.10） ----------

interface GroupMenu {
  x: number;
  y: number;
  groupId: string;
}

function GroupContextMenuOverlay({ menu, onClose }: { menu: GroupMenu; onClose: () => void }) {
  const group = useCanvas((s) => s.groups[menu.groupId]);
  const { renameGroup, setGroupColor, deleteGroupNodes, dissolveGroup } = useCanvas();

  if (!group) return null;

  const runBatch = async () => {
    onClose();
    const failed: string[] = [];
    for (const id of group.nodeIds) {
      try {
        await runNode(id);
      } catch {
        failed.push(id);
      }
    }
    if (failed.length) toast.warning(`组内 ${failed.length} 个节点无法运行（缺输入或类型不支持）`);
  };

  return (
    <>
      <div className="context-mask" onClick={onClose} onContextMenu={(e) => { e.preventDefault(); onClose(); }} />
      <div className="context-menu" style={{ left: menu.x, top: menu.y }}>
        <button
          onClick={() => {
            void promptDialog({ title: "分组名称", initial: group.name }).then((name) => {
              if (name && name.trim()) renameGroup(menu.groupId, name.trim());
            });
            onClose();
          }}
        >
          重命名分组
        </button>
        <div className="context-sep" />
        <div className="group-colors">
          {GROUP_COLORS.map((c) => (
            <button
              key={c}
              className={"color-dot" + (group.color === c ? " on" : "")}
              style={{ background: c }}
              title="设置分组底色"
              onClick={() => setGroupColor(menu.groupId, c)}
            />
          ))}
        </div>
        <div className="context-sep" />
        <button onClick={runBatch}>批量运行（{group.nodeIds.length} 个节点）</button>
        <button
          onClick={() => {
            dissolveGroup(menu.groupId);
            onClose();
          }}
        >
          解散分组
        </button>
        <button
          className="danger"
          onClick={() => {
            deleteGroupNodes(menu.groupId);
            onClose();
          }}
        >
          删除组内节点
        </button>
      </div>
    </>
  );
}

// ---------- 多选浮动条（同类型产物 ≥2 时可拼图/拼视频，4.9 拼接工具） ----------

function MultiSelectBar() {
  const nodes = useCanvas((s) => s.nodes);
  const projectId = useCanvas((s) => s.projectId);
  const [busy, setBusy] = useState(false);

  const picked = nodes.filter(
    (n) => n.selected && n.type !== "group" && !!n.data.artifactId,
  );
  const images = picked.filter((n) => n.data.artifactKind === "image");
  const videos = picked.filter((n) => n.data.artifactKind === "video");
  // 混合多选或同类型不足 2 个时不展示（拼接要求同类型）
  const canImage = !videos.length && images.length >= 2;
  const canVideo = !images.length && videos.length >= 2;
  if ((!canImage && !canVideo) || busy) return null;
  const kind: "image" | "video" = canImage ? "image" : "video";
  const items = canImage ? images : videos;

  const concat = async () => {
    const state = useCanvas.getState();
    if (!projectId) return;
    const artifactIds = items
      .map((n) => n.data.artifactId)
      .filter((x): x is string => !!x);
    if (artifactIds.length < 2) return;
    setBusy(true);
    let destId: string | null = null;
    try {
      // 新节点落点：选中节点包围盒下方；拼接产物直接挂为其新版本
      const maxX = Math.max(...items.map((n) => n.position.x));
      const maxY = Math.max(...items.map((n) => n.position.y));
      destId = state.addNode(kind, { x: maxX, y: maxY + 280 });
      const version = await api.concatAssets(projectId, kind, artifactIds, destId);
      useCanvas.getState().applyEdited(destId, version);
    } catch (e) {
      // 失败回滚：清掉空节点，不留残骸
      if (destId) useCanvas.getState().deleteNode(destId);
      toast.error(`拼接失败：${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="multiselect-bar">
      {canImage && (
        <button onClick={concat}>拼图（{images.length} 张）</button>
      )}
      {canVideo && (
        <button onClick={concat}>拼视频（{videos.length} 段）</button>
      )}
    </div>
  );
}

// ---------- 画布主体 ----------

export function CanvasView() {
  const {
    nodes, edges, setNodes, setEdges, onConnect, projectId, handleTaskEvent,
    hideEdges, snapToGrid, showMiniMap, save, groups,
  } = useCanvas();
  const paneSide = useChat((s) => s.paneSide);
  const { screenToFlowPosition } = useReactFlow();
  const [menu, setMenu] = useState<ContextMenu | null>(null);
  const [nodeMenu, setNodeMenu] = useState<NodeMenu | null>(null);
  const [groupMenu, setGroupMenu] = useState<GroupMenu | null>(null);
  /** 触摸端：节点面板浮层开合（桌面端常驻左栏，该状态不产生样式） */
  const [paletteOpen, setPaletteOpen] = useState(false);
  /** 长按定时器（触摸端长按 = 右键；命中节点开节点菜单，命中空白开画布菜单） */
  const longPressRef = useRef<{ timer: number; x: number; y: number } | null>(null);
  // 拖组框时记录成员原始位置，按位移差同步成员（4.10）
  const dragStateRef = useRef<{
    gid: string;
    start: { x: number; y: number };
    origins: Record<string, { x: number; y: number }>;
  } | null>(null);

  useEffect(() => {
    if (!projectId) return;
    const unsub = subscribeTasks(projectId, (event, data) => handleTaskEvent(event, data));
    return unsub;
  }, [projectId, handleTaskEvent]);

  // 预热已知模型 id 缓存（本地注册表 + provider 模型）：run.ts 旧节点模型
  // 迁移校验据此放行项目预设/provider 模型 id，避免被静默重置为默认模型；
  // 同时装载「模型是否有原生音频」能力表（视频节点徽标用，未知不显示断言）
  useEffect(() => {
    warmKnownModelIds().catch(() => {});
    void useCanvas.getState().loadAudioCaps();
  }, []);

  // 手机档：对话面板是全屏浮层，默认展开会把画布整块盖住——首屏以画布为主，
  // 需要对话时点右下角悬浮把手唤出（桌面端左右分栏，不适用）
  const isPhone = useIsPhone();
  useEffect(() => {
    if (isPhone && !useChat.getState().collapsed) useChat.getState().toggleCollapse();
  }, [isPhone]);

  // Ctrl+G：将当前多选的普通节点打组（4.10）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== "g") return;
      e.preventDefault();
      const state = useCanvas.getState();
      const selected = state.nodes.filter((n) => n.selected && n.type !== "group");
      if (selected.length >= 2) state.createGroup(selected.map((n) => n.id));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 画布改动自动保存（防抖 2 秒）
  //
  // **三处丢改动已修（2026-09-30）**：
  // ① `groups` 没进依赖数组——改分组底色/名字只动 groups，dirty=true 却等不到
  //    保存，刷新即丢；现在把 groups 一并作为触发条件。
  // ② 保存失败被 `.catch(() => {})` 吞掉——手动保存有 toast、自动保存没有，用户
  //    以为存上了。现在自动保存失败也提示，并保留 dirty 让下次改动重试。
  // ③ 卸载时 `clearTimeout` 直接放弃未落盘的改动——现在卸载前补一次「立刻保存」。
  useEffect(() => {
    if (!projectId || !useCanvas.getState().dirty) return;
    const t = setTimeout(() => {
      useCanvas.getState().save().catch((e: unknown) => {
        toast.error(`画布自动保存失败：${e instanceof Error ? e.message : String(e)}`
          + "（改动仍在本页，可手动保存重试）");
      });
    }, 2000);
    return () => clearTimeout(t);
  }, [nodes, edges, groups, projectId]);

  // 卸载兜底：防抖计时器被清掉时，未落盘的改动会永久丢失（实测复现路径：
  // 改完节点 → 2 秒内切走页面/关标签）。这里在组件卸载时补一次立即保存。
  useEffect(() => {
    return () => {
      if (useCanvas.getState().projectId && useCanvas.getState().dirty) {
        void useCanvas.getState().save().catch(() => {
          // 卸载阶段不再弹 toast（组件已走，弹窗宿主可能也在卸载中）
        });
      }
    };
  }, []);

  // 关标签/刷新兜底（2026-09-30 复查 F2 残留）：React 的卸载 cleanup 在
  // 「直接关标签/刷新」时**不会执行**（页面直接销毁），上面那条只在 SPA 内部
  // 切页时生效。听 pagehide / 页面转后台，并用 keepalive 让请求在页面销毁后
  // 仍有机会发出（能发出几 KB 就看浏览器；发不出去与现状等价，不倒退）。
  useEffect(() => {
    const flush = () => {
      const st = useCanvas.getState();
      if (st.projectId && st.dirty) {
        void st.save({ keepalive: true }).catch(() => {
          /* 页面正在销毁，无从提示 */
        });
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") flush();
    };
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  // 类型校验：source 输出类型必须被 target 参考端口接受
  const isValidConnection: IsValidConnection = useCallback((connection: Connection | Edge) => {
    const state = useCanvas.getState();
    const source = state.nodes.find((n) => n.id === connection.source);
    const target = state.nodes.find((n) => n.id === connection.target);
    if (!source || !target || !connection.sourceHandle || !connection.targetHandle) {
      return false;
    }
    if (source.data.specType === "group" || target.data.specType === "group") return false;
    const sourceSpec = NODE_SPECS[source.data.specType];
    const targetSpec = NODE_SPECS[target.data.specType];
    const out = sourceSpec?.outputs.find((p) => p.id === connection.sourceHandle);
    const inp = targetSpec?.inputs.find((p) => p.id === connection.targetHandle);
    if (!out || !inp) return false;
    const accepts = inp.accepts ?? [inp.type];
    return accepts.includes(out.type);
  }, []);

  const visibleEdges = hideEdges
    ? edges.map((e) => ({ ...e, hidden: true }))
    : edges;

  // ---------- 触摸端：长按 = 右键 ----------
  // 手机没有鼠标右键，而 React Flow 的 onPaneContextMenu/onNodeContextMenu 在 iOS
  // 上根本不触发（Android 也时机不稳）。这里统一补一个长按手势：命中节点 → 节点菜单，
  // 命中分组成员 → 分组菜单，命中空白 → 画布菜单；手指位移超过阈值（在拖画布/拖节点）
  // 即取消。菜单落点与鼠标右键同源，因此三处菜单的后续行为完全一致。
  /** 长按已经弹过菜单（用于吃掉浏览器补发的 click，见 onCanvasTouchEnd） */
  const longPressedRef = useRef(false);

  const cancelLongPress = useCallback(() => {
    const lp = longPressRef.current;
    if (lp) window.clearTimeout(lp.timer);
    longPressRef.current = null;
  }, []);

  const onCanvasTouchStart = (e: TouchEvent) => {
    // 只接管画布内的长按（工具条/面板按钮上的长按照旧）。
    // 判定用 Element 而不是 HTMLElement：画布背景层是 **SVG**（React Flow 的
    // 点阵 pattern），按在空白画布上时 target 就是它——用 HTMLElement 判定会把
    // 「空白处长按」整条路径挡掉，菜单永远弹不出来（实测）。
    if (!(e.target instanceof Element) || !e.target.closest(".react-flow")) return;
    const touch = e.touches[0];
    if (!touch) return;
    const startX = touch.clientX;
    const startY = touch.clientY;
    // 命中判定放在按下时就做（长按期间目标元素可能因拖动而变化）
    const nodeEl = e.target.closest<HTMLElement>(".react-flow__node");
    const nodeId = nodeEl?.dataset.id ?? null;
    cancelLongPress();
    longPressRef.current = {
      x: startX,
      y: startY,
      timer: window.setTimeout(() => {
        longPressRef.current = null;
        longPressedRef.current = true;      // 供 onCanvasTouchEnd 吃掉随后的 click
        const p = clampMenuPos(startX, startY);
        setMenu(null);
        setNodeMenu(null);
        setGroupMenu(null);
        if (!nodeId) {
          const flow = screenToFlowPosition({ x: startX, y: startY });
          setMenu({ ...p, flowX: flow.x, flowY: flow.y });
          return;
        }
        const node = useCanvas.getState().nodes.find((n) => n.id === nodeId);
        if (node?.type === "group") setGroupMenu({ ...p, groupId: nodeId });
        else setNodeMenu({ ...p, nodeId, touch: true });
      }, LONG_PRESS_MS),
    };
  };

  const onCanvasTouchEnd = (e: TouchEvent) => {
    // 长按弹出菜单后，浏览器仍会在 touchend 之后补一个 click；那一刻屏幕上正好是
    // 菜单的全屏遮罩（.context-mask），这个 click 会**立刻把菜单关掉**（实测时间轴：
    // 菜单在 480ms 弹出、click 在 ~735ms 落下）。touchend 监听不是 passive，
    // 在这里 preventDefault 即可把它吃掉。
    if (longPressedRef.current) {
      longPressedRef.current = false;
      e.preventDefault();
    }
    cancelLongPress();
  };

  const onCanvasTouchMove = (e: TouchEvent) => {
    const lp = longPressRef.current;
    if (!lp) return;
    const touch = e.touches[0];
    if (!touch) return;
    if (Math.abs(touch.clientX - lp.x) > LONG_PRESS_MOVE_PX
      || Math.abs(touch.clientY - lp.y) > LONG_PRESS_MOVE_PX) {
      cancelLongPress();
    }
  };

  /** 画布容器：长按手势的原生监听挂在这里（原因见下面的 effect） */
  const canvasMainRef = useRef<HTMLDivElement>(null);
  /** 处理函数引用：原生监听只注册一次，每次渲染刷新引用，避免闭包过期 */
  const touchHandlersRef = useRef({
    start: onCanvasTouchStart, move: onCanvasTouchMove, end: onCanvasTouchEnd,
  });
  touchHandlersRef.current = {
    start: onCanvasTouchStart, move: onCanvasTouchMove, end: onCanvasTouchEnd,
  };

  // 触摸端长按 = 右键。**必须用原生捕获阶段监听**：
  // React 的事件委托挂在根节点、走冒泡阶段，而 React Flow 的 d3 手势在 touchstart 上会
  // `stopImmediatePropagation()`（它要独占手势，实测空白处按下去事件根本冒泡不出去），
  // 于是 React 的 onTouchStart 收不到任何画布触摸事件——长按菜单永远弹不出来。
  // 捕获阶段在目标之前执行，d3 拦不住；passive 保证不干扰它自己的平移/缩放。
  useEffect(() => {
    const host = canvasMainRef.current;
    if (!host) return;
    const start = (e: TouchEvent) => touchHandlersRef.current.start(e);
    const move = (e: TouchEvent) => touchHandlersRef.current.move(e);
    const end = (e: TouchEvent) => touchHandlersRef.current.end(e);
    host.addEventListener("touchstart", start, { capture: true, passive: true });
    host.addEventListener("touchmove", move, { capture: true, passive: true });
    // touchend 不能 passive：长按后要靠它 preventDefault 吃掉浏览器补发的 click
    host.addEventListener("touchend", end, { capture: true });
    host.addEventListener("touchcancel", end, { capture: true });
    return () => {
      host.removeEventListener("touchstart", start, true);
      host.removeEventListener("touchmove", move, true);
      host.removeEventListener("touchend", end, true);
      host.removeEventListener("touchcancel", end, true);
    };
  }, []);

  return (
    <div className="canvas-layout">
      <NodePalette
        open={paletteOpen}
        onPick={() => setPaletteOpen(false)}
      />
      {paneSide === "left" && <ChatPanel />}
      <div className="canvas-main" ref={canvasMainRef}>
        <Toolbar />
        {/* 触摸端：节点面板以底部弹层唤出（按钮与遮罩在宽屏下由媒体查询隐藏，
            DOM 顺序放在 ReactFlow 之前，避免影响 .canvas-main 的 flex 布局） */}
        {paletteOpen && (
          <div className="m-backdrop" onClick={() => setPaletteOpen(false)} />
        )}
        <button
          className="canvas-fab"
          aria-label="打开节点面板"
          onClick={() => setPaletteOpen((v) => !v)}
        >
          节点
        </button>
        <ReactFlow
          nodes={nodes}
          edges={visibleEdges}
          nodeTypes={nodeTypes}
          onNodesChange={(changes) => {
            const state = useCanvas.getState();
            // 删除变更统一走 store：成员删除同步分组，组框删除转为解散（4.10）
            const removes = changes.filter((c) => c.type === "remove") as NodeRemoveChange[];
            if (removes.length) {
              for (const r of removes) {
                if (r.id.startsWith("group_")) state.dissolveGroup(r.id);
                else state.deleteNode(r.id);
              }
              const rest = changes.filter((c) => c.type !== "remove");
              if (rest.length) {
                state.setNodes(applyNodeChanges(rest, state.nodes) as Node<StudioNodeData>[]);
              }
              return;
            }
            state.setNodes(applyNodeChanges(changes, state.nodes) as Node<StudioNodeData>[]);
          }}
          onEdgesChange={(changes) => {
            const state = useCanvas.getState();
            state.setEdges(applyEdgeChanges(changes, state.edges));
          }}
          onConnect={(connection) =>
            onConnect({ ...connection, id: `e_${Date.now().toString(36)}` } as Edge)
          }
          isValidConnection={isValidConnection}
          onNodeDragStart={(_, node) => {
            if (node.type !== "group") return;
            const g = useCanvas.getState().groups[node.id];
            if (!g) return;
            const origins: Record<string, { x: number; y: number }> = {};
            for (const mid of g.nodeIds) {
              const m = useCanvas.getState().nodes.find((n) => n.id === mid);
              // 已选中的成员由 React Flow 多选拖动负责，避免双重位移
              if (m && !m.selected) origins[mid] = { ...m.position };
            }
            dragStateRef.current = { gid: node.id, start: { ...node.position }, origins };
          }}
          onNodeDrag={(_, node) => {
            const ds = dragStateRef.current;
            if (!ds || node.id !== ds.gid) return;
            const dx = node.position.x - ds.start.x;
            const dy = node.position.y - ds.start.y;
            if (!dx && !dy) return;
            const state = useCanvas.getState();
            state.setNodes(
              state.nodes.map((n) =>
                ds.origins[n.id]
                  ? { ...n, position: { x: ds.origins[n.id].x + dx, y: ds.origins[n.id].y + dy } }
                  : n,
              ),
            );
          }}
          onNodeDragStop={(_, _node, dragged) => {
            dragStateRef.current = null;
            const state = useCanvas.getState();
            const touched = new Set<string>();
            for (const n of dragged) {
              if (n.type === "group") continue;
              for (const g of Object.values(state.groups)) {
                if (g.nodeIds.includes(n.id)) touched.add(g.id);
              }
            }
            for (const gid of touched) state.refreshGroupBox(gid);
          }}
          onPaneContextMenu={(event) => {
            event.preventDefault();
            const pos = screenToFlowPosition({ x: event.clientX, y: event.clientY });
            setMenu({ ...clampMenuPos(event.clientX, event.clientY), flowX: pos.x, flowY: pos.y });
          }}
          onNodeContextMenu={(event, node) => {
            event.preventDefault();
            const p = clampMenuPos(event.clientX, event.clientY);
            setMenu(null);
            if (node.type === "group") {
              setNodeMenu(null);
              setGroupMenu({ ...p, groupId: node.id });
            } else {
              setGroupMenu(null);
              setNodeMenu({ ...p, nodeId: node.id });
            }
          }}
          onMoveStart={() => {
            setMenu(null);
            setNodeMenu(null);
            setGroupMenu(null);
          }}
          snapToGrid={snapToGrid}
          snapGrid={[20, 20]}
          nodeDragThreshold={isPhone ? NODE_DRAG_THRESHOLD_TOUCH : NODE_DRAG_THRESHOLD_MOUSE}
          fitView
          minZoom={0.2}
          maxZoom={2}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} color="#22263a" />
          {showMiniMap && (
            <MiniMap pannable zoomable bgColor="#141826" maskColor="rgba(10,12,20,0.7)" />
          )}
        </ReactFlow>
        <BottomNav />
        <MultiSelectBar />
        {menu && <ContextMenuOverlay menu={menu} onClose={() => setMenu(null)} />}
        {nodeMenu && <NodeContextMenuOverlay menu={nodeMenu} onClose={() => setNodeMenu(null)} />}
        {groupMenu && (
          <GroupContextMenuOverlay menu={groupMenu} onClose={() => setGroupMenu(null)} />
        )}
      </div>
      {paneSide === "right" && <ChatPanel />}
    </div>
  );
}
