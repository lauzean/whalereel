/** 分镜故事板：卡片流 ↔ 画布双视图（分镜=节点、转场=连线）+ 左侧资产栏 + 右侧预览（M3 6.6/6.7）。 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  api,
  fileUrl,
  type CharacterInfo,
  type EpisodeInfo,
  type GenerationParamsTask,
  type QaIssueInfo,
  type ModelInfo,
  type PropInfo,
  type SceneInfo,
  type ShotInfo,
  type ShotPatchBody,
  type NodeVersionInfo,
  type ShotReviewInfo,
} from "../api/client";
import { defaultEmptyLabel, fetchModelOptions } from "../api/modelOptions";
import { ModelSelect } from "../ui/ModelSelect";
import { ImageLightbox } from "../ui/ImageLightbox";
import { PromptOptimizeDialog } from "../ui/PromptOptimizeDialog";
import { PromptPreviewDialog } from "../ui/PromptPreviewDialog";
import { RollReasonDialog } from "../ui/RollReasonDialog";
import { REROLL_REASON_OTHER, ROLL_REASONS, rollReasonLabel } from "./rollReasons";
import {  } from "../ui/icons";
import { BILLING_KEYS } from "../billingKeys";
import { useDrama } from "../stores/drama";
import { costLine, useCostTip, useCostTipWith } from "../stores/billing";
import { useIsPhone } from "../ui/useIsPhone";
import { useThumbHover } from "../ui/ThumbHover";
import { confirmDialog, toast } from "../ui/feedback";
import { CANDIDATE_COUNTS } from "./candidateCounts";

/** 任务终态（与后端一致，进入终态后停止轮询） */
const FINAL_STATUS = ["done", "failed", "canceled", "cached"];
/** 轮询间隔（毫秒） */
const POLL_MS = 2000;
/** 轮询次数上限（约 30 分钟，防御长任务与任务列表截断） */
const POLL_MAX = 900;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** 取角色最新一张有产物的定妆图 */
function mainImage(c: CharacterInfo) {
  return [...c.images].reverse().find((img) => img.artifact_id) ?? null;
}

/** 分镜角色头像：优先妆造映射指定形象，无映射回退最新有产物图（6.4 第 2 条） */
function charImage(c: CharacterInfo, mappedId?: string) {
  if (mappedId) {
    const hit = c.images.find((im) => im.id === mappedId && im.artifact_id);
    if (hit) return hit;
  }
  return mainImage(c);
}

/** 生成中状态文案（与资产卡一致）：排队中 / 加载模型 N% / 生成中 N% */
function progressLabel(progress: { status: string; progress: number } | undefined): string {
  if (!progress || progress.status === "queued") return "排队中";
  const pct = Math.round(progress.progress * 100);
  if (progress.status === "loading_model") return `加载模型 ${pct}%`;
  return pct > 0 ? `生成中 ${pct}%` : "生成中";
}

/** @提及目标：角色/场景/道具统一索引（名称长度降序，优先长名匹配） */
type MentionTarget = {
  kind: "char" | "scene" | "prop";
  id: string;
  name: string;
  artifactId: string | null;
};

/** 提及类型元数据：前缀语法/悬浮标题/占位字（三类共用提及机制） */
const MENTION_KIND_META: Record<MentionTarget["kind"], { prefix: string; label: string; blank: string }> = {
  char: { prefix: "角色@", label: "角色", blank: "人" },
  scene: { prefix: "场景@", label: "场景", blank: "景" },
  prop: { prefix: "道具@", label: "道具", blank: "物" },
};

/** 构建画面描述的 @提及索引：角色取妆造映射优先的头像，场景取主图，道具取定妆图 */
function buildMentionIndex(
  characters: CharacterInfo[],
  scenes: SceneInfo[],
  props: PropInfo[] = [],
  costumeMap?: Record<string, string>,
): MentionTarget[] {
  const targets: MentionTarget[] = [];
  for (const c of characters) {
    const img = charImage(c, costumeMap?.[c.id]);
    targets.push({ kind: "char", id: c.id, name: c.name, artifactId: img?.artifact_id ?? null });
  }
  for (const s of scenes) {
    targets.push({ kind: "scene", id: s.id, name: s.name, artifactId: s.artifact_id ?? null });
  }
  for (const p of props) {
    targets.push({ kind: "prop", id: p.id, name: p.name, artifactId: p.artifact_id ?? null });
  }
  return targets.sort((a, b) => b.name.length - a.name.length);
}

/** 文本拆分为普通片段与 @提及片段。
 * 提及语法分两档：`角色@名称` / `场景@名称` / `道具@名称`（类型前缀，新数据
 * ——同名资产不会混淆）；兼容纯 `@名称`（旧数据，全索引最长名优先）。
 * 未知名原样保留。 */
function splitMentions(text: string, index: MentionTarget[]) {
  const parts: Array<{ text: string; target: MentionTarget | null }> = [];
  // 前缀语法在前缀限定的同类型子集内最长匹配（索引已按名称长度降序）
  const scoped: Record<MentionTarget["kind"], MentionTarget[]> = {
    char: index.filter((t) => t.kind === "char"),
    scene: index.filter((t) => t.kind === "scene"),
    prop: index.filter((t) => t.kind === "prop"),
  };
  let buf = "";
  const pushPart = (target: MentionTarget | null, from: number, to: number) => {
    if (buf) {
      parts.push({ text: buf, target: null });
      buf = "";
    }
    parts.push({ text: text.slice(from, to), target });
  };
  let i = 0;
  while (i < text.length) {
    // 带类型前缀：「角色@」「场景@」「道具@」固定 3 字符（2 字前缀 + @）
    let kind: MentionTarget["kind"] | null = null;
    for (const k of ["char", "scene", "prop"] as const) {
      if (text.startsWith(MENTION_KIND_META[k].prefix, i)) {
        kind = k;
        break;
      }
    }
    if (kind) {
      const hit = scoped[kind].find((t) => t.name && text.startsWith(t.name, i + 3));
      if (hit) {
        pushPart(hit, i, i + 3 + hit.name.length);
        i += 3 + hit.name.length;
        continue;
      }
    }
    // 兼容纯 @名称（旧数据）
    if (text[i] === "@") {
      const hit = index.find((t) => t.name && text.startsWith(t.name, i + 1));
      if (hit) {
        pushPart(hit, i, i + 1 + hit.name.length);
        i += 1 + hit.name.length;
        continue;
      }
    }
    buf += text[i];
    i += 1;
  }
  if (buf) parts.push({ text: buf, target: null });
  return parts;
}

/** @提及富文本渲染：命中角色/场景/道具渲染为带缩略图的引用块（实时预览素材） */
function MentionText({
  text,
  characters,
  scenes,
  props,
  costumeMap,
}: {
  text: string;
  characters: CharacterInfo[];
  scenes: SceneInfo[];
  props: PropInfo[];
  costumeMap?: Record<string, string>;
}) {
  const index = useMemo(
    () => buildMentionIndex(characters, scenes, props, costumeMap),
    [characters, scenes, props, costumeMap],
  );
  const parts = useMemo(() => splitMentions(text, index), [text, index]);
  const { hoverProps, hoverNode } = useThumbHover();
  return (
    <>
      {hoverNode}
      {parts.map((p, i) =>
        p.target ? (
          <span
            key={i}
            className={`mention-chip${p.target.artifactId ? "" : " empty"}`}
            title={`${MENTION_KIND_META[p.target.kind].label} ${p.target.name}`}
            {...hoverProps(p.target.artifactId, p.target.name)}
          >
            {p.target.artifactId ? (
              <img src={fileUrl(p.target.artifactId)} alt="" />
            ) : (
              <span className="mention-blank">{MENTION_KIND_META[p.target.kind].blank}</span>
            )}
            <span>{p.target.name}</span>
          </span>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </>
  );
}

/** 分镜状态徽标（生成中优先于产物状态） */
function shotBadge(shot: ShotInfo): { text: string; cls: string } {
  if (shot.generating) return { text: "生成中", cls: "generating" };
  if (shot.status === "done") return { text: "视频完成", cls: "done" };
  if (shot.status === "first_frame") return { text: "首帧已生成", cls: "first-frame" };
  return { text: "待生成", cls: "pending" };
}

/** 多切徽标（批次 4 §35.2）：关键场次段内 2 切镜的标识（含切点秒数） */
function cutBadge(shot: ShotInfo): string | null {
  const cuts = shot.cuts ?? [];
  if (cuts.length < 2) return null;
  return `2 切 · 第 ${cuts[1].offset}s`;
}

/** 风险标签中文化（解析期 risks 枚举 → 面向用户的白话短标） */
const RISK_LABELS: Record<string, string> = {
  face: "人脸漂移",
  prop: "道具易漂",
  text: "文字画面",
  motion: "大幅动作",
  crowd: "多人同框",
  fluid: "流体动作",
  mirror: "镜面反射",
  hands: "手部交互",
  collision: "碰撞",
  transform: "物体变形",
};

/** 风险标签的判据（鼠标悬停可见）——标签是模型自评，**与角色/道具名单对过账**
 *  才显示：对不上的一律剔除（用户报障：空镜挂着「多人同框」） */
const RISK_HINTS: Record<string, string> = {
  crowd: "画面里有两名及以上已登记角色同框——最易串脸/分身，生成后重点核对人数与身份",
  face: "画面里有人脸特写——人脸最易漂移，生成后核对是否还是同一个人的脸",
  prop: "画面里用到了已登记道具——道具外观易漂，生成后核对与定妆图是否一致",
  text: "画面里有文字（招牌/写字板/信纸）——文字易糊，必要时后期补字",
  motion: "画面里有大幅动作——动作易走形，必要时拆成两镜",
  hands: "画面里有手部精细交互——手指最易画错，生成后重点看手",
};

/** 高风险动作类风险（解析期按降级拍法写结果态，生成时优先看这些镜） */
const HARD_ACTION_RISKS = ["fluid", "mirror", "hands", "collision", "transform"];

/** 镜头风险徽标（批次 2b §32.5 落地：解析期 risks 展示在分镜卡上）。
 *
 * 此前 risks 只进 precheck 模型建议、前端零展示——用户在生成前看不到「这镜
 * 是高风险动作镜」，无法提前换模型或拆镜（审计 13 号 #6）。
 * 样式：常规风险冷灰底、高风险动作暖橙底 + 左侧圆点强调（深色卡片上比白底和谐）。
 */
function RisksBadges({ risks }: { risks?: string[] }) {
  const list = (risks ?? []).filter((r) => RISK_LABELS[r]);
  if (list.length === 0) return null;
  return (
    <div className="shot-foot-row shot-risks">
      {list.map((r) => (
        <span
          key={r}
          className={`shot-risk-badge${HARD_ACTION_RISKS.includes(r) ? " hard" : ""}`}
          title={
            HARD_ACTION_RISKS.includes(r)
              ? `${RISK_LABELS[r]}：高风险动作，已按降级拍法写结果态；建议用动作遵循强的高档模型，必要时拆镜`
              : (RISK_HINTS[r] ?? `${RISK_LABELS[r]}：生成后请重点核对该项`)
          }
        >
          <i className="shot-risk-dot" aria-hidden="true" />
          {RISK_LABELS[r]}
        </span>
      ))}
    </div>
  );
}

/** 等待一批任务全部进入终态（轮询任务列表）。
 *
 * 按 batch_id 精准查询（后端 limit 放宽到 500）：此前用不带 batch 的全量列表
 * （limit 200），批次任务数接近或超过 200 时 watched 永远凑不齐 → 空转
 * POLL_MAX×2s ≈ 30 分钟，期间 busy 让顶栏全部按钮（含合成/审片）禁用。
 */
async function waitTasks(projectId: string, taskIds: string[],
                         batchId?: string): Promise<void> {
  for (let i = 0; i < POLL_MAX && taskIds.length > 0; i++) {
    await sleep(POLL_MS);
    const tasks = await api.listTasks(projectId, batchId)
      .catch(() => [] as never[]);
    const watched = tasks.filter((t) => taskIds.includes(t.id));
    if (
      watched.length >= taskIds.length &&
      watched.every((t) => FINAL_STATUS.includes(t.status))
    ) {
      return;
    }
  }
}

/** 单张分镜卡：场景/角色引用 + 可编辑描述与时长 + 台词 + 导演级画面描述 + 首帧缩略图。 */
function ShotCard({
  shot,
  scene,
  scenes,
  characters,
  props,
  selected,
  busy,
  onSelect,
  onFirstFrame,
  onVideo,
  onUpdate,
  accordion,
  open,
  onToggle,
  onModalOpen,
}: {
  shot: ShotInfo;
  scene: SceneInfo | null;
  /** 全量场景列表（画面描述 @场景 提及的缩略图解析用） */
  scenes: SceneInfo[];
  characters: CharacterInfo[];
  /** 全量道具列表（画面描述 @道具 提及的缩略图解析用） */
  props: PropInfo[];
  selected: boolean;
  busy: boolean;
  onSelect: () => void;
  /** 生成本镜首帧（携带 shot id 定向提交——历史上误绑全集入口致整集批量生成） */
  onFirstFrame: (shotId: string) => void;
  /** 单镜生成/重新生成视频（定向提交，沿用现有首帧；不影响其他分镜） */
  onVideo: (shotId: string) => void;
  onUpdate: (body: ShotPatchBody) => void;
  /** 窄屏手风琴模式：卡片折成一行，点行展开（桌面恒为 false，卡片始终全展开） */
  accordion: boolean;
  /** 手风琴模式下本卡是否展开 */
  open: boolean;
  /** 切换展开态（点头部） */
  onToggle: () => void;
  /** 本卡打开了整屏弹层（放大编辑 / AI 优化）：通知外层先收起视频播放器 */
  onModalOpen: () => void;
}) {
  // 花钱按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const regenCostTip = useCostTipWith(
    "按当前时长、资产与前后镜衔接重新生成画面描述——先弹窗给你看，确认后才替换",
    BILLING_KEYS.regenerateShotPrompt);
  const shotGenCostTip = useCostTip(BILLING_KEYS.shotsGenerate);
  const [desc, setDesc] = useState(shot.description);
  const [duration, setDuration] = useState(String(shot.duration));
  // 导演级视频画面描述：展示态渲染 @提及缩略图，点击进入编辑（@ 弹候选）
  const [vp, setVp] = useState(shot.video_prompt);
  const [vpEdit, setVpEdit] = useState(false);
  /** @候选查询词（null = 关闭候选菜单）与类型限定（null = 纯 @ 触发，全类型列） */
  const [mentionQ, setMentionQ] = useState<string | null>(null);
  const [mentionKind, setMentionKind] = useState<MentionTarget["kind"] | null>(null);
  const vpRef = useRef<HTMLTextAreaElement>(null);
  // 本镜任务进度与失败原因（WS 事件驱动：genProgress / shotErrors）
  const shotProgress = useDrama((s) => s.genProgress[`shot_${shot.id}`]);
  const shotError = useDrama((s) => s.shotErrors[shot.id]);
  const previewShotPrompt = useDrama((s) => s.previewShotPrompt);
  const applyShotPrompt = useDrama((s) => s.applyShotPrompt);
  /** 重算走**两段式**：先 dry-run 拿结果 → 确认框预览（可改）→ 应用才落库。
   *  此前是点一下直接覆盖落库，用户看不到结果也没法拒绝（反馈「像抽盲盒」） */
  const [regenPreview, setRegenPreview] =
    useState<{ text: string; timingOk: boolean; statePos: string } | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const startRegenerate = async () => {
    setRegenerating(true);
    try {
      const r = await previewShotPrompt(shot.id);
      setRegenPreview({ text: r.video_prompt, timingOk: r.timing_ok,
                        statePos: r.state_position });
    } catch (e) {
      toast.error(`重算失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setRegenerating(false);
    }
  };
  const projectId = useDrama((s) => s.projectId);
  /** AI 优化提示词弹窗开关（批次 3）：优化结果经「应用」后走既有 onUpdate 写回 */
  const [optOpen, setOptOpen] = useState(false);
  /** 缩略图悬浮预览（场景缩略图 / 角色头像共用一份状态） */
  const { hoverProps, hoverNode } = useThumbHover();
  // 首帧图放大预览（灯箱）
  const [preview, setPreview] = useState<string | null>(null);
  /** 首帧候选挑选弹窗（配合「一次抽 N 张」） */
  const [candOpen, setCandOpen] = useState(false);
  /** 刷新分镜列表（挑定候选首帧后卡片要显示新选中的那张） */
  const reloadShots = useDrama((st) => st.loadShots);

  // 列表被任务事件刷新后同步编辑框
  useEffect(() => setDesc(shot.description), [shot.description]);
  useEffect(() => setDuration(String(shot.duration)), [shot.duration]);
  useEffect(() => setVp(shot.video_prompt), [shot.video_prompt]);

  const badge = shotBadge(shot);
  const chars = characters.filter((c) => shot.character_ref_ids.includes(c.id));

  // 失焦保存：描述与时长（值未变化时不提交）
  const saveDesc = () => {
    if (desc.trim() && desc !== shot.description) onUpdate({ description: desc });
  };
  const saveDuration = () => {
    const n = parseInt(duration, 10);
    if (Number.isFinite(n) && n > 0 && n !== shot.duration) onUpdate({ duration: n });
    else setDuration(String(shot.duration));
  };

  // 画面描述 @候选：按触发类型限定池（场景@ 只列场景、角色@ 只列角色、
  // 道具@ 只列道具），再按查询词过滤；纯 @ 触发不限定类型
  const mentionIndex = useMemo(
    () => buildMentionIndex(characters, scenes, props, shot.costume_map),
    [characters, scenes, props, shot.costume_map],
  );
  const mentionCandidates = useMemo(() => {
    if (mentionQ === null) return [];
    const pool = mentionKind
      ? mentionIndex.filter((t) => t.kind === mentionKind)
      : mentionIndex;
    const q = mentionQ.toLowerCase();
    return pool.filter((t) => t.name.toLowerCase().includes(q)).slice(0, 8);
  }, [mentionQ, mentionKind, mentionIndex]);

  // textarea 输入：检测光标前的提及触发——「场景@」「角色@」「道具@」限定
  // 候选类型，纯「@」不限定；查询词取 @ 后到空白/下一 @ 的文本
  const onVpChange = (value: string, caret: number) => {
    setVp(value);
    const before = value.slice(0, caret);
    const mTyped = /(角色|场景|道具)@([^\s@]{0,12})$/.exec(before);
    if (mTyped) {
      setMentionKind(
        mTyped[1] === "场景" ? "scene" : mTyped[1] === "道具" ? "prop" : "char");
      setMentionQ(mTyped[2]);
      return;
    }
    const m = /@([^\s@]{0,12})$/.exec(before);
    setMentionKind(null);
    setMentionQ(m ? m[1] : null);
  };

  // 选中候选：整段触发文本替换为「类型前缀@完整名称」（统一写入带前缀语法，
  // 展示与生成转换都无同名歧义），光标落在名称后
  const pickMention = (t: MentionTarget) => {
    const ta = vpRef.current;
    setMentionQ(null);
    setMentionKind(null);
    if (!ta) return;
    const caret = ta.selectionStart ?? vp.length;
    const before = vp.slice(0, caret);
    const mTyped = /(角色|场景|道具)@([^\s@]{0,12})$/.exec(before);
    const m = mTyped ?? /@([^\s@]{0,12})$/.exec(before);
    if (!m) return;
    const start = caret - m[0].length;
    const full = mTyped
      ? `${mTyped[1]}@${t.name}`
      : `${MENTION_KIND_META[t.kind].prefix}${t.name}`;
    const next = vp.slice(0, start) + full + vp.slice(caret);
    setVp(next);
    requestAnimationFrame(() => {
      ta.focus();
      const pos = start + full.length;
      ta.setSelectionRange(pos, pos);
    });
  };

  // 失焦保存画面描述并退出编辑态
  const saveVp = () => {
    setMentionQ(null);
    setMentionKind(null);
    setVpEdit(false);
    if (vp !== shot.video_prompt) onUpdate({ video_prompt: vp });
  };

  /** 放大编辑：画面描述实测 600-1400 字，窄卡片里只能看到几行。弹窗复用同一份
   * vp 状态与同一套 @ 提及（vpRef 指向弹窗 textarea），编辑行为一致、画布大。 */
  const [vpZoom, setVpZoom] = useState(false);

  // 本卡打开的整屏弹层（放大编辑 / AI 优化）与视频播放器互斥：手机浏览器的视频是独立
  // 硬件合成层，会画在弹窗之上（弹窗 z-index 再高也盖不住）。统一上报给外层收起播放器。
  useEffect(() => {
    if (vpZoom || optOpen) onModalOpen();
  }, [vpZoom, optOpen, onModalOpen]);
  const closeVpZoom = () => {
    saveVp();          // 复用失焦保存：写库 + 退出编辑态
    setVpZoom(false);
  };

  return (
    <div
      id={`shot-${shot.id}`}
      className={`shot-card${selected ? " selected" : ""}`
        + `${accordion ? " acc" : ""}${accordion && open ? " acc-open" : ""}`}
      onClick={onSelect}
    >
      {/* 窄屏手风琴头：缩略图 + 镜号 + 场景 + 状态 + 展开箭头。
          整集 30+ 张卡全展开是上万像素的滚动，折成一行后一屏能扫完、直接点到要改的那一镜。
          点头部**只切换展开态**（stopPropagation 拦掉卡片根的选中）：否则每展开一镜都会
          选中它、右侧预览弹层跟着弹开占掉半屏，扫分镜时非常打扰。要看视频就在展开后的
          卡里点「预览视频」——那才是明确的「我要看」意图。 */}
      {accordion && (
        <button
          className="shot-acc"
          onClick={(e) => {
            e.stopPropagation();
            onToggle();
          }}
          aria-expanded={open}
        >
          <span className="shot-acc-thumb">
            {shot.first_frame_artifact_id ? (
              <img src={fileUrl(shot.first_frame_artifact_id)} alt="" />
            ) : scene?.artifact_id ? (
              <img src={fileUrl(scene.artifact_id)} alt="" />
            ) : (
              <span>无图</span>
            )}
          </span>
          <span className="shot-acc-main">
            <span className="shot-acc-title">镜头 {shot.seq}</span>
            <span className="shot-acc-sub">
              {scene?.name || "未引用场景"}
              {shot.duration ? ` · ${shot.duration}s` : ""}
            </span>
          </span>
          <span className={`sb-badge ${badge.cls}`}>{badge.text}</span>
          <span className="shot-acc-chevron">{open ? "▾" : "▸"}</span>
        </button>
      )}
      <div className="shot-acc-body">
      <div className="shot-head">
        <span className="shot-seq">镜头 {shot.seq}</span>
        {/* 任务失败原因徽标（悬浮看完整错误；下次提交生成时清除） */}
        {shotError && (
          <span className="sb-badge failed" title={shotError}>
            失败
          </span>
        )}
        <span className={`sb-badge ${badge.cls}`}>{badge.text}</span>
        {cutBadge(shot) && (
          <span className="sb-badge multicut" title="关键场次多切：每切一张关键帧控制构图">
            {cutBadge(shot)}
          </span>
        )}
      </div>
      <div className="shot-scene">
        {scene ? (
          <>
            <span className="shot-scene-thumb" {...hoverProps(scene.artifact_id, scene.name)}>
              {scene.artifact_id ? (
                <img src={fileUrl(scene.artifact_id)} alt={scene.name} />
              ) : (
                <span>无图</span>
              )}
            </span>
            <span className="shot-scene-name" title={scene.name}>
              {scene.name}
            </span>
          </>
        ) : (
          <span className="shot-scene-none">未引用场景</span>
        )}
      </div>
      {chars.length > 0 && (
        <div className="shot-chars">
          {chars.map((c) => {
            const mappedId = shot.costume_map?.[c.id];
            const img = charImage(c, mappedId);
            const mapped = mappedId ? c.images.find((im) => im.id === mappedId) : null;
            return (
              <span
                key={c.id}
                className={`shot-char${mappedId ? " mapped" : ""}`}
                title={mapped ? `${c.name} · ${mapped.name}` : c.name}
                {...hoverProps(img?.artifact_id, c.name)}
              >
                {img?.artifact_id ? (
                  <img src={fileUrl(img.artifact_id)} alt={c.name} />
                ) : (
                  <span>{c.name.slice(0, 1)}</span>
                )}
              </span>
            );
          })}
        </div>
      )}
      {chars.some((c) => c.images.length > 1) && (
        <div
          className="shot-costumes nodrag"
          onClick={(e) => e.stopPropagation()}
        >
          {chars
            .filter((c) => c.images.length > 1)
            .map((c) => (
              <label key={c.id} className="shot-costume">
                <span className="shot-costume-name">{c.name}</span>
                <select
                  value={shot.costume_map?.[c.id] ?? ""}
                  onChange={(e) => {
                    const map = { ...(shot.costume_map ?? {}) };
                    if (e.target.value) map[c.id] = e.target.value;
                    else delete map[c.id];
                    onUpdate({ costume_map: map });
                  }}
                >
                  <option value="">默认形象</option>
                  {c.images.map((im) => (
                    <option key={im.id} value={im.id}>
                      {im.name || "未命名形象"}
                    </option>
                  ))}
                </select>
              </label>
            ))}
        </div>
      )}
      <span className="shot-label">画面描述</span>
      <textarea
        className="shot-desc nodrag"
        value={desc}
        placeholder="机位/景别/动作描述"
        onChange={(e) => setDesc(e.target.value)}
        onBlur={saveDesc}
      />
      <div className="shot-vp nodrag" onClick={(e) => e.stopPropagation()}>
        <span className="shot-label">
          视频画面描述（导演级）
          {/* 时序对账失配（时长被编辑过或时间小节不符）：提示重算 */}
          {shot.video_prompt && shot.timing_ok === false && (
            <span className="vp-stale-badge" title="时长与画面描述的时间小节不一致（时长可能被编辑过），建议重算">
              时长待重算
            </span>
          )}
        </span>
        {vpEdit ? (
          <div className="vp-editor">
            <textarea
              ref={vpRef}
              value={vp}
              placeholder="导演级提示词：景别/运镜/光影/色调/动作调度/特效…输入 场景@ / 角色@ / 道具@ 引用素材"
              onChange={(e) => onVpChange(e.target.value, e.target.selectionStart ?? 0)}
              onBlur={saveVp}
            />
            {mentionQ !== null && mentionCandidates.length > 0 && (
              <div className="mention-menu">
                {mentionCandidates.map((t) => (
                  <button
                    key={`${t.kind}-${t.id}`}
                    className="mention-item"
                    onMouseDown={(e) => {
                      // mousedown 先于 blur：阻止失焦以保持编辑焦点
                      e.preventDefault();
                      pickMention(t);
                    }}
                  >
                    <span className="mention-thumb">
                      {t.artifactId ? (
                        <img src={fileUrl(t.artifactId)} alt="" />
                      ) : (
                        <span>{t.name.slice(0, 1)}</span>
                      )}
                    </span>
                    <span className="mention-name">{t.name}</span>
                    <span className="mention-kind">{MENTION_KIND_META[t.kind].label}</span>
                  </button>
                ))}
              </div>
            )}
            <div className="vp-hint">
              输入「场景@」「角色@」「道具@」弹对应候选（插入为 类型@名称），
              生成视频时自动转为参考图引用 · 失焦自动保存
            </div>
          </div>
        ) : (
          <div className="vp-view" onClick={() => setVpEdit(true)}>
            {/* 编辑入口在下方按钮行（原先悬浮在文字框右上角，为此文字框右侧要多留
                22px 内边距——每一行都短一截，看着左右不对称，用户反馈）。挪走后文字框
                左右内边距对称，入口也常驻可见、更好找。
                滚动下沉到本层：外层 `.vp-view` 只做定位基准，滚动归这里。 */}
            <div className="vp-view-body">
              {shot.video_prompt ? (
                <MentionText
                  text={shot.video_prompt}
                  characters={characters}
                  scenes={scenes}
                  props={props}
                  costumeMap={shot.costume_map}
                />
              ) : (
                <span className="vp-empty">
                  AI 将按项目画风与导演技巧生成专业提示词，点击此处手动编辑
                </span>
              )}
            </div>
          </div>
        )}
        {/* 操作按钮排在内容**下方**：先读内容再决定操作，与其它卡片
            「内容在上、操作在下」一致（此前挤在标题行里，标签一长就换行） */}
        {!vpEdit && (
          <div className="shot-vp-actions">
            {/* 编辑走弹窗（画面描述实测 600-1400 字，窄卡片里只能看到几行）。
                行内编辑入口保留在「点描述框本身」——按钮只留这一个，避免两个「编辑」
                让人猜区别（用户反馈：去掉行内那个，弹窗这个直接叫「编辑」）。 */}
            <button
              className="vp-edit-btn"
              title="编辑视频画面描述（长文在弹窗里更好改）"
              onClick={() => {
                setVpEdit(false);      // 让 vpRef 绑到弹窗的 textarea
                setVpZoom(true);
              }}
            >
              编辑
            </button>
            {shot.video_prompt && projectId && (
              <button
                className="vp-edit-btn"
                title="说清修改需求，由 AI 结合本镜上下文改写提示词（可编辑后再应用）"
                onClick={() => setOptOpen(true)}
              >
                AI 优化
              </button>
            )}
            <button
              className="vp-edit-btn"
              disabled={busy || regenerating}
              title={regenCostTip}
              onClick={() => void startRegenerate()}
            >
              {regenerating ? "重算中…" : "重算"}
            </button>
          </div>
        )}
      </div>
      {hoverNode}
      {regenPreview && (
        <PromptPreviewDialog
          title={`重算画面描述 · 镜头 ${shot.seq}`}
          current={shot.video_prompt}
          next={regenPreview.text}
          hint={[
            regenPreview.timingOk
              ? "时间小节与时长一致"
              : "时间小节与时长仍不一致，建议手工调整",
            regenPreview.statePos ? "姿态已改写为取景语言" : "",
          ].filter(Boolean).join("；")}
          onApply={async (text) => {
            await applyShotPrompt(shot.id, text, regenPreview.statePos);
            setRegenPreview(null);
            toast.success("已应用重算结果");
          }}
          onClose={() => setRegenPreview(null)}
        />
      )}
      {shot.dialogue.length > 0 && (
        <div className="shot-dialogue">
          <span className="shot-label">台词</span>
          {shot.dialogue.map((d, i) => (
            <p key={i} className="shot-line">
              <span className="shot-speaker">
                {d.speaker}
                {d.os ? "（OS）" : ""}：
              </span>
              <span className="shot-text">{d.text}</span>
            </p>
          ))}
        </div>
      )}
      {/* 首帧图：标题/按钮样式与上方「视频画面描述」保持一致（shot-label 标题 +
          shot-vp-actions 按钮行 + vp-edit-btn 按钮；2026-09-30 用户要求：
          不再把按钮悬浮在图片上，图下常显、与其它卡片同一套观感） */}
      <div className="shot-ff nodrag" onClick={(e) => e.stopPropagation()}>
        <span className="shot-label">首帧图</span>
        <div className="shot-ff-media">
          {shot.first_frame_artifact_id ? (
            <img
              src={fileUrl(shot.first_frame_artifact_id)}
              alt={`镜头${shot.seq}首帧`}
              title="点击放大预览"
              className="clickable"
              onClick={(e) => {
                e.stopPropagation();
                setPreview(shot.first_frame_artifact_id);
              }}
            />
          ) : (
            <span className="shot-ff-empty">尚无首帧</span>
          )}
        </div>
        <div className="shot-vp-actions">
          <button
            className="vp-edit-btn"
            disabled={busy || shot.generating}
            title={(shot.first_frame_artifact_id
              ? "重新生成本镜首帧（单独抽卡，不影响其他分镜）"
              : "生成本镜首帧")
              + (shotGenCostTip ? `；${shotGenCostTip}` : "")}
            onClick={(e) => {
              e.stopPropagation();
              onFirstFrame(shot.id);
            }}
          >
            {busy || shot.generating
              ? "生成中"
              : shot.first_frame_artifact_id ? "重新生成" : "生成首帧"}
          </button>
          {/* 首帧候选挑选（配合「一次抽 N 张」）：挑定后出视频就按这张起像 */}
          {projectId && shot.first_frame_artifact_id && (
            <button
              className="vp-edit-btn"
              disabled={busy}
              title="在多次抽卡的首帧里挑一张「生效」（出视频按它起像；只改指针不删产物）"
              onClick={(e) => {
                e.stopPropagation();
                setCandOpen(true);
              }}
            >
              候选
            </button>
          )}
        </div>
      </div>
      {/* 本镜任务进度（首帧/视频共用槽位，任务事件实时驱动，终态消失） */}
      {shotProgress && (
        <div className="shot-progress">
          <span className="shot-progress-label">{progressLabel(shotProgress)}</span>
          <div className="progress-track">
            <div
              className="progress-bar"
              style={{ width: `${Math.round(shotProgress.progress * 100)}%` }}
            />
          </div>
        </div>
      )}
      {/* 镜头信息区：1 列 3 行（时长 / 景别 / 风险徽标）——横向单行在窄卡片里
          会挤压换行，拆行后层级清晰、徽标也不再被挤到边缘 */}
      <div className="shot-foot">
        <div className="shot-foot-row">
          <span className="shot-duration">
            时长
            {/* 2026-09-30 用户：**时长修改暂不需要，先注释掉**——改时长会与画面
                描述的时间小节脱节（后端虽已支持「改时长即确定性重排」，但用户判定
                该需求暂不必开）。需要时放开下面这段输入框即可：saveDuration /
                duration 状态 / 同步 effect 都原样保留着。
            <input
              className="nodrag"
              type="number"
              min={5}
              max={12}
              value={duration}
              onChange={(e) => setDuration(e.target.value)}
              onBlur={saveDuration}
            />
            */}
            {shot.duration} 秒
          </span>
        </div>
        {shot.camera && (
          <div className="shot-foot-row">
            <span className="shot-camera">{shot.camera}</span>
          </div>
        )}
        <RisksBadges risks={shot.risks} />
      </div>
      {/* 视频行：预览 + 单镜生成/重新生成。
          单镜重出此前只有「审片报告」里的重拍表单可达（要先跑审片、再展开报告），
          日常「这一镜不满意，重出一条」没有入口——这里补上。 */}
      <div className="shot-video-row">
        <button
          className="shot-video-btn nodrag"
          disabled={!shot.video_artifact_id}
          onClick={(e) => {
            e.stopPropagation();
            onSelect();
          }}
        >
          {shot.video_artifact_id ? "预览视频" : "暂无视频"}
        </button>
        {shot.video_artifact_id ? (
          <button
            className="shot-video-regen nodrag"
            disabled={busy || shot.generating}
            title={"重新生成本镜视频（沿用现有首帧，单变量重摇；不影响其他分镜）"
              + (shotGenCostTip ? `；${shotGenCostTip}` : "")}
            onClick={(e) => {
              e.stopPropagation();
              onVideo(shot.id);
            }}
          >
            {shot.generating ? "生成中" : "重出"}
          </button>
        ) : shot.first_frame_artifact_id ? (
          <button
            className="shot-video-regen nodrag"
            disabled={busy || shot.generating}
            title={"用本镜现有首帧生成视频（不影响其他分镜）"
              + (shotGenCostTip ? `；${shotGenCostTip}` : "")}
            onClick={(e) => {
              e.stopPropagation();
              onVideo(shot.id);
            }}
          >
            {shot.generating ? "生成中" : "出视频"}
          </button>
        ) : (
          <button
            className="shot-video-regen nodrag"
            disabled
            title="本镜还没有首帧——先点上方「生成首帧」"
          >
            出视频
          </button>
        )}
      </div>
      </div>
      {preview && <ImageLightbox artifactId={preview} onClose={() => setPreview(null)} />}
      {candOpen && projectId && (
        <FirstFrameCandidates
          shotId={shot.id}
          seq={shot.seq}
          projectId={projectId}
          current={shot.first_frame_artifact_id ?? undefined}
          onPicked={() => {
            const eid = useDrama.getState().activeEpisodeId;
            if (eid) void reloadShots(eid);
          }}
          onClose={() => setCandOpen(false)}
        />
      )}
      {/* AI 优化提示词（批次 3）：结果由用户点「应用」后才写回——未应用零副作用 */}
      {optOpen && projectId && (
        <PromptOptimizeDialog
          projectId={projectId}
          target={{
            target: "shot",
            id: shot.id,
            label: `第 ${shot.seq} 镜视频提示词`,
            currentPrompt: shot.video_prompt,
          }}
          onApply={(text) => {
            setVp(text);
            onUpdate({ video_prompt: text });
            setOptOpen(false);
            toast.success("已应用 AI 优化后的提示词");
          }}
          onClose={() => setOptOpen(false)}
        />
      )}
      {vpZoom && createPortal(
        <div className="vp-zoom-mask" onClick={closeVpZoom}>
          <div className="vp-zoom-modal" onClick={(e) => e.stopPropagation()}>
            <div className="vp-zoom-head">
              <span className="vp-zoom-title">镜头 {shot.seq} · 视频画面描述</span>
              {shot.timing_ok === false && (
                <span className="vp-stale-badge" title="时长与画面描述的时间小节不一致，建议重算">
                  时长待重算
                </span>
              )}
              {shot.video_prompt && projectId && (
                <button className="ghost-btn" disabled={busy}
                        title="说清修改需求，由 AI 结合本镜上下文改写提示词（可编辑后再应用）"
                        onClick={() => setOptOpen(true)}>
                  AI 优化
                </button>
              )}
              <button className="ghost-btn" disabled={busy || regenerating}
                      title={regenCostTip}
                      onClick={() => void startRegenerate()}>
                {regenerating ? "重算中…" : "重算"}
              </button>
              <button className="ghost-btn" onClick={closeVpZoom}>完成</button>
            </div>
            <textarea
              className="vp-zoom-area"
              ref={vpRef}
              autoFocus
              value={vp}
              placeholder="导演级提示词：景别/运镜/光影/色调/动作调度/特效…输入 场景@ / 角色@ / 道具@ 引用素材"
              onChange={(e) => onVpChange(e.target.value, e.target.selectionStart ?? 0)}
              onKeyDown={(e) => { if (e.key === "Escape") closeVpZoom(); }}
            />
            {mentionQ !== null && mentionCandidates.length > 0 && (
              <div className="mention-menu vp-zoom-mention">
                {mentionCandidates.map((t) => (
                  <button
                    key={`${t.kind}-${t.id}`}
                    className="mention-item"
                    onMouseDown={(e) => {
                      // mousedown 先于 blur：阻止失焦以保持编辑焦点
                      e.preventDefault();
                      pickMention(t);
                    }}
                  >
                    <span className="mention-thumb">
                      {t.artifactId ? (
                        <img src={fileUrl(t.artifactId)} alt="" />
                      ) : (
                        <span>{t.name.slice(0, 1)}</span>
                      )}
                    </span>
                    <span className="mention-name">{t.name}</span>
                    <span className="mention-kind">{MENTION_KIND_META[t.kind].label}</span>
                  </button>
                ))}
              </div>
            )}
            <div className="vp-hint">
              输入「场景@」「角色@」「道具@」弹对应候选 · 点遮罩或按 Esc 保存并关闭
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

/** 生成参数排查弹窗：该镜首帧/视频最新任务的最终 prompt + 参考图缩略图
    （按编号排列，与提示词「参考图N」逐一对账）+ 模型与截断告警 */
function GenerationParamsModal({ shotId, seq, onClose }: {
  shotId: string;
  seq: number;
  onClose: () => void;
}) {
  const [kind, setKind] = useState<"ff" | "v">("ff");
  const [task, setTask] = useState<GenerationParamsTask | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    // 乱序守卫（审计 13 号：快速切换「首帧/视频」时两个 GET 乱序返回，
    // 视频参数会渲染在「首帧」页签下——正好用于排查串脸的弹窗给错结论）
    let stale = false;
    setLoading(true);
    api
      .getGenerationParams(shotId, kind)
      .then((r) => { if (!stale) setTask(r.task); })
      .catch((e) => {
        if (!stale) toast.error(`参数查询失败：${e instanceof Error ? e.message : e}`);
      })
      .finally(() => { if (!stale) setLoading(false); });
    return () => { stale = true; };
  }, [shotId, kind]);

  // 参考图展示顺序：主图（视频首帧）在前 + extra_images（数组序即编号）
  const mainRef = kind === "v" ? task?.input_refs?.image ?? null : null;
  const extraRefs = task?.input_refs?.extra_images ?? [];
  const dropped = task?.generation?.refs_dropped ?? 0;
  // **模型实际收到的原文**＝runner 侧记录（含首帧 LLM 精炼与国家外貌前缀）；
  // 提交时的组装稿（task.prompt）拿不到这些——2026-09-30 修复：此前把组装稿
  // 标成「发给模型的原文」，对账（串脸/构图排查）对的是错的这份。
  const finalPrompt = task?.generation?.prompt || task?.prompt || "";
  return createPortal(
    <div className="gen-params-mask" onClick={onClose}>
      <div className="gen-params-modal" onClick={(e) => e.stopPropagation()}>
        <div className="gen-params-head">
          <span className="gen-params-title">分镜 {seq} · 生成参数</span>
          <div className="gen-params-tabs">
            <button
              className={`preview-tab${kind === "ff" ? " active" : ""}`}
              onClick={() => setKind("ff")}
            >
              首帧
            </button>
            <button
              className={`preview-tab${kind === "v" ? " active" : ""}`}
              onClick={() => setKind("v")}
            >
              视频
            </button>
          </div>
          <button className="ghost-btn" onClick={onClose}>关闭</button>
        </div>
        {loading ? (
          <div className="gen-params-empty">加载中…</div>
        ) : !task ? (
          <div className="gen-params-empty">
            该镜还没有{kind === "ff" ? "首帧" : "视频"}生成记录
          </div>
        ) : (
          <div className="gen-params-body">
            <div className="gen-params-meta">
              <span>模型：{task.model_id || "默认"}</span>
              <span>runner：{task.runner}</span>
              <span>状态：{task.status}</span>
            </div>
            {dropped > 0 && (
              <div className="gen-params-warn">
                ⚠ 参考图超出上限被截断：{task.generation?.refs_in} →{" "}
                {task.generation?.refs_used} 张（被裁参考图对应的提示词标注已失效，
                可能导致角色串脸/场景错误）
              </div>
            )}
            {(task.generation?.prompt_notes?.length ?? 0) > 0 && (
              <div className="gen-params-warn">
                ⚠ 提交前提示词已按本端点能力调整：
                {task.generation?.prompt_notes?.join("；")}
              </div>
            )}
            <div className="gen-params-refs">
              {mainRef && (
                <figure className="gen-params-ref">
                  <img src={fileUrl(mainRef)} alt="首帧图" />
                  <figcaption>首帧图</figcaption>
                </figure>
              )}
              {extraRefs.map((aid, i) => (
                <figure key={`${aid}-${i}`} className="gen-params-ref">
                  <img src={fileUrl(aid)} alt={`参考图${i + 1}`} />
                  <figcaption>参考图 {i + 1}</figcaption>
                </figure>
              ))}
              {extraRefs.length === 0 && !mainRef && (
                <span className="gen-params-empty">无参考图</span>
              )}
            </div>
            <div className="gen-params-prompt-block">
              <div className="gen-params-prompt-head">
                <span className="shot-label">最终提示词（模型实际收到的原文）</span>
                <button
                  className="ghost-btn"
                  onClick={() => {
                    void navigator.clipboard.writeText(finalPrompt);
                    toast.success("提示词已复制");
                  }}
                >
                  复制
                </button>
              </div>
              <pre className="gen-params-prompt">{finalPrompt}</pre>
            </div>
            {finalPrompt !== task.prompt && (
              <div className="gen-params-prompt-block">
                <div className="gen-params-prompt-head">
                  <span className="shot-label">
                    提交时的组装稿（精炼 / 国家外貌前缀之前）
                  </span>
                  <button
                    className="ghost-btn"
                    onClick={() => {
                      void navigator.clipboard.writeText(task.prompt);
                      toast.success("组装稿已复制");
                    }}
                  >
                    复制
                  </button>
                </div>
                <pre className="gen-params-prompt">{task.prompt}</pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

/** 底部「+ 增加分镜」卡片（默认时长 5 秒）。 */
function AddShotCard({ episodeId, scenes }: { episodeId: string; scenes: SceneInfo[] }) {
  const addShot = useDrama((s) => s.addShot);
  const [open, setOpen] = useState(false);
  const [desc, setDesc] = useState("");
  const [sceneId, setSceneId] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!desc.trim()) return;
    setBusy(true);
    try {
      await addShot(episodeId, {
        description: desc.trim(),
        scene_ref_id: sceneId || null,
        duration: 5,
      });
      setOpen(false);
      setDesc("");
      setSceneId("");
    } catch (e) {
      toast.error(`新增分镜失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button className="shot-add-card" onClick={() => setOpen(true)}>
        + 增加分镜
      </button>
    );
  }
  return (
    <div className="shot-card shot-add-form">
      <div className="shot-head">
        <span className="shot-seq">新增分镜</span>
      </div>
      <span className="shot-label">画面描述</span>
      <textarea
        value={desc}
        placeholder="机位/景别/动作描述"
        onChange={(e) => setDesc(e.target.value)}
      />
      <span className="shot-label">场景引用</span>
      <select value={sceneId} onChange={(e) => setSceneId(e.target.value)}>
        <option value="">不引用场景</option>
        {scenes.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}
          </option>
        ))}
      </select>
      <div className="asset-footer">
        <button className="sb-btn primary" disabled={busy || !desc.trim()} onClick={submit}>
          确定
        </button>
        <button className="ghost-btn" disabled={busy} onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </div>
  );
}

/** 画布视图节点数据：复用 ShotCard，编辑经 updateShot 落库与卡片流同步 */
type ShotFlowData = {
  shot: ShotInfo;
  scene: SceneInfo | null;
  scenes: SceneInfo[];
  characters: CharacterInfo[];
  props: PropInfo[];
  selected: boolean;
  busy: boolean;
  onSelect: () => void;
  onFirstFrame: (shotId: string) => void;
  onVideo: (shotId: string) => void;
  onUpdate: (body: ShotPatchBody) => void;
};

/** 画布视图相邻分镜横向间距（卡片宽 238 + 呼吸空间） */
const SHOT_STEP = 300;

/** 画布视图节点：分镜卡 + 左右转场连线端点 */
function ShotFlowNode({ data }: NodeProps) {
  const d = data as ShotFlowData;
  return (
    <div className="shot-flow-node">
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <ShotCard
        shot={d.shot}
        scene={d.scene}
        scenes={d.scenes}
        characters={d.characters}
        props={d.props}
        selected={d.selected}
        busy={d.busy}
        onSelect={d.onSelect}
        onFirstFrame={d.onFirstFrame}
        onVideo={d.onVideo}
        onUpdate={d.onUpdate}
        accordion={false}
        open
        onToggle={() => {}}
        onModalOpen={() => {}}
      />
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

const shotNodeTypes = { shot: ShotFlowNode };

/** 审片报告数据（api 返回类型推导，与后端契约同源） */
type ReviewReportData = Awaited<ReturnType<typeof api.reviewEpisode>>;

/** 分镜体检报告面板（解析期存档 QA）：白话解释直出 + 定位镜头 + 一键修复
 *（重算描述 / 修正衔接类型——修复后自动重拉报告，已处理项即消失） */
function QaPanel({
  report,
  shots,
  busy,
  onLocate,
  onRecalc,
  onFixTransition,
}: {
  report: QaIssueInfo[];
  shots: ShotInfo[];
  busy: boolean;
  onLocate: (shotId: string) => void;
  /** 重算该镜画面描述（清除 suspended/state_chain/overload 类问题） */
  onRecalc: (seq: number) => void;
  /** 修正衔接类型（transition 类问题按建议方向一键改） */
  onFixTransition: (seq: number, transitionIn: string) => void;
}) {
  const errors = report.filter((i) => i.level === "error");
  const warns = report.filter((i) => i.level !== "error");
  const shotIdBySeq = new Map(shots.map((s) => [s.seq, s.id]));
  return (
    <div className="sb-report-body">
      <div className="sb-report-stat">
        共 {report.length} 项 · 拦截 {errors.length} 项 / 提示 {warns.length} 项
        （拦截项处理前无法批量生成视频）
      </div>
      {report.length === 0 && (
        <div className="sb-report-empty">全部检查通过，可以批量生成</div>
      )}
      {report.map((it, idx) => {
        // 修复意图由后端结构化给出（it.fix：{field, value, label}），前端逐字
        // 应用——此前用「场景变化」中文子串猜方向，首镜 continue 的文案不含该
        // 词，按钮变成「改为连续动作」点了等于没点、报告永不清零、还提示已修正
        const fixIntent = it.fix ?? null;
        const shotId = it.seq != null ? shotIdBySeq.get(it.seq) : undefined;
        const canRecalc =
          it.seq != null &&
          ["suspended", "state_chain", "overload"].includes(it.rule) &&
          !!shotId;
        return (
          <div key={idx} className={`sb-report-issue ${it.level}`}>
            <span className="issue-level">
              {it.level === "error" ? "拦截" : "提示"}
            </span>
            <div className="issue-main">
              <div className="issue-title">
                {it.title || it.rule}
                {it.seq != null ? ` · 镜 ${it.seq}` : ""}
              </div>
              <div className="issue-plain">{it.plain || it.message}</div>
              {it.message && it.plain && (
                <div className="issue-msg">{it.message}</div>
              )}
            </div>
            <div className="issue-actions">
              {shotId && it.seq != null && (
                <button
                  className="ghost-btn"
                  disabled={busy}
                  onClick={() => onLocate(shotId)}
                >
                  定位
                </button>
              )}
              {canRecalc && (
                <button
                  className="gen-btn"
                  disabled={busy}
                  title="AI 按当前剧本与台词重写该镜画面描述（问题项自动消除）"
                  onClick={() => onRecalc(it.seq!)}
                >
                  重算描述
                </button>
              )}
              {fixIntent && fixIntent.field === "transition_in" && shotId && it.seq != null && (
                <button
                  className="gen-btn"
                  disabled={busy}
                  title={`把该镜衔接类型改为 ${fixIntent.label}（后端给出的目标值 ${fixIntent.value}）`}
                  onClick={() => onFixTransition(it.seq!, fixIntent.value)}
                >
                  改为{fixIntent.label}
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** 预检报告面板：时长分布 + 问题清单（阻断/建议） + 模型路由建议
 *
 * 已随工具栏「预检」按钮一并撤下（2026-09-23）：同一份信息在生成清单预览与
 * 硬门 400 明细里各出现一次，常驻按钮是第三条路径。后端端点与 api 方法保留，
 * 需要时按「自动出现」的方式重新接入（参考 submitWithQaEscape 展开体检报告）。
 */

/** 审片报告面板（批次 3 §34：不符镜头清单 + 五级裁决按钮组 + 单变量重拍表单
 * + 镜头日志）。裁决语义：keep=锁定继续 / post=后期修复 / edit=只换一层 /
 * reroll=同参数重摇 / rewrite=回溯改分镜。 */
const VERDICT_META: { key: string; label: string; hint: string }[] = [
  { key: "keep", label: "保留", hint: "主要支出达成且无致命伤——锁定并继续" },
  { key: "post", label: "后期", hint: "缺陷属后期域（颜色/字幕/混音），不重生成" },
  { key: "edit", label: "编辑", hint: "只错一层且平台可编辑，保住这条作源片段" },
  { key: "reroll", label: "重摇", hint: "同参数换种子重抽（同缺陷两次后将被止损拦截）" },
  { key: "rewrite", label: "重写", hint: "系统性问题——回溯改分镜/提示词再拍" },
];

function ReviewPanel({
  report,
  onRetake,
  onAutoReview,
  busy,
  projectId,
}: {
  report: ReviewReportData;
  projectId: string;
  onRetake: (
    ids: string[],
    opts: { changed: string; note: string; force: boolean; reason: string },
  ) => void;
  onAutoReview: () => void;
  busy: boolean;
}) {
  // AI 审片按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const autoReviewCostTip = useCostTipWith(
    "逐镜抽帧拼图发给多模态模型判级（需在模型管理勾选「支持多模态」），结果以 AI 建议落镜头日志",
    BILLING_KEYS.autoReview);
  const failed = report.results.filter((r) =>
    report.failed_shot_ids.includes(r.shot_id),
  );
  /** 重拍表单：单变量选择 + 重摇原因（25 号 2.4 必填；2026-10-10 起多选）
   *  + 备注 + 止损强提 */
  const [changed, setChanged] = useState("seed");
  /** 已勾选的原因码（多选，保持勾选顺序；提交时逗号连接） */
  const [reasons, setReasons] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [force, setForce] = useState(false);
  /** 重出已有视频的镜才算重摇（缺视频的镜是首拍，不要求填原因） */
  const needReason = failed.some((r) => r.has_video);
  /** 勾选/取消一个重摇原因（多选） */
  const toggleReason = (key: string) => {
    setReasons((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  };
  /** 选了「其他」却没写备注：后端会 400，这里先拦住（否则报告面板已关、
   *  用户要重新走「合成本集」才能回到这个表单） */
  const otherWithoutNote = reasons.includes(REROLL_REASON_OTHER) && !note.trim();
  /** 展开的镜头日志（shot_id → 条目） */
  const [logs, setLogs] = useState<Record<string, ShotReviewInfo[]>>({});
  const [logFor, setLogFor] = useState<string>("");
  /** 重摇原因分布（25 号 2.4）：展开时才拉取（零成本静默面板） */
  const [reasonStats, setReasonStats] = useState<
    Awaited<ReturnType<typeof api.rerollReasons>> | null
  >(null);

  const toggleReasonStats = async () => {
    if (reasonStats) {
      setReasonStats(null);
      return;
    }
    if (!projectId) return;   // 项目还没加载完：别去请求 /api/projects//reroll-reasons
    try {
      setReasonStats(await api.rerollReasons(projectId));
    } catch (e) {
      toast.error(`原因分布加载失败：${e instanceof Error ? e.message : e}`);
    }
  };

  const toggleLog = async (shotId: string) => {
    if (logFor === shotId) {
      setLogFor("");
      return;
    }
    setLogFor(shotId);
    try {
      const r = await api.shotReviews(shotId);
      setLogs((m) => ({ ...m, [shotId]: r.reviews }));
    } catch (e) {
      toast.error(`镜头日志加载失败：${e instanceof Error ? e.message : e}`);
    }
  };

  const judge = async (
    shotId: string,
    verdict: "keep" | "post" | "edit" | "reroll" | "rewrite",
  ) => {
    try {
      // 不带 reason：裁决是「意向记录」，而表单里选的原因是给**提交**用的——
      // 同一份原因被记到别镜的裁决日志上会污染分布（25 号复查前端 #5）
      await api.reviewShot(shotId, { verdict, evidence: note, changed });
      toast.success(
        verdict === "keep" ? "已保留（keep）并回写观测态" : `已裁决：${verdict}`,
      );
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="sb-report-body">
      <div className="sb-report-stat">
        {report.results.length} 镜：{report.results.length - failed.length} 通过 ·{" "}
        {failed.length} 不符（时长偏差 &gt;1s / 近纯色帧 / 缺产物 / 探针失败）
        <button
          className="sb-verdict-btn"
          style={{ marginLeft: 8 }}
          disabled={busy}
          title={autoReviewCostTip}
          onClick={onAutoReview}
        >
          AI 审片（多模态判级）
        </button>
      </div>
      {failed.length === 0 && (
        <div className="sb-report-empty">全部镜头通过确定性审片</div>
      )}
      {/* 重摇原因分布（25 号 2.4）：先有靶子，再谈优化——「我到底在反复重出
          什么」是下一步改提示词/改流程的唯一依据 */}
      <div className="sb-reason-stat">
        <button className="link-btn" onClick={() => void toggleReasonStats()}>
          {reasonStats ? "▾" : "▸"} 重摇原因分布（本项目）
        </button>
        {reasonStats && (
          <div className="sb-reason-dist">
            {reasonStats.quality_rerolls === 0 ? (
              <div className="sb-shotlog-empty">暂无质量类重摇记录</div>
            ) : (
              <>
                <div className="sb-reason-sum">
                  已提交的重摇 {reasonStats.quality_rerolls} 次 · 原因填写率{" "}
                  {Math.round(reasonStats.label_rate * 100)}%
                  {reasonStats.unlabeled > 0 && (
                    <span className="sb-reason-warn">
                      （{reasonStats.unlabeled} 次没填——说明还有旁路，或来自
                      必填机制上线前的旧版本）
                    </span>
                  )}
                </div>
                {/* 只作参照、不进填写率：裁决意向 / AI 判级 / 历史行都不是
                    用户真的摇过（25 号复查：混在一起会让填写率永远到不了 100%） */}
                {((reasonStats.other_origins?.verdict ?? 0) > 0
                  || (reasonStats.other_origins?.auto ?? 0) > 0
                  || (reasonStats.other_origins?.legacy ?? 0) > 0) && (
                  <div className="sb-reason-note">
                    另有（不计入填写率）：人工裁决 {reasonStats.other_origins?.verdict ?? 0} ·
                    AI 审片 {reasonStats.other_origins?.auto ?? 0} ·
                    旧版本记录 {reasonStats.other_origins?.legacy ?? 0}
                  </div>
                )}
                {reasonStats.by_reason.map((x) => (
                  <div key={x.reason} className="sb-reason-row">
                    <span>{rollReasonLabel(x.reason)}</span>
                    <i>{x.count}</i>
                  </div>
                ))}
                {reasonStats.by_reason.length === 0 && (
                  <div className="sb-shotlog-empty">
                    还没有带原因的重摇记录（新提交会开始累积）
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
      {failed.map((r) => (
        <div key={r.shot_id} className="sb-report-issue error sb-review-row">
          <span className="issue-level">不符</span>
          <span className="issue-cat">镜{r.seq}</span>
          <span className="issue-msg">
            {!r.has_video
              ? "无视频产物"
              : r.probe_failed
                ? "视频无法读取（探针失败，请检查文件）"
                : !r.duration_ok
                  ? `时长偏差：视频 ${r.video_duration}s ≠ 设定 ${r.expected_duration}s`
                  : !r.frame_ok
                    ? `检出 ${r.solid_frames} 帧近纯色（黑帧/占位帧嫌疑）`
                    : "首帧缺失"}
          </span>
          <span className="sb-verdicts">
            {VERDICT_META.map((v) => (
              <button
                key={v.key}
                className="sb-verdict-btn"
                title={v.hint}
                disabled={busy}
                onClick={() =>
                  judge(r.shot_id, v.key as "keep" | "post" | "edit" | "reroll" | "rewrite")
                }
              >
                {v.label}
              </button>
            ))}
            <button
              className="sb-verdict-btn"
              disabled={busy}
              onClick={() => toggleLog(r.shot_id)}
            >
              {logFor === r.shot_id ? "收起日志" : "日志"}
            </button>
          </span>
          {logFor === r.shot_id && (
            <div className="sb-shotlog">
              {(logs[r.shot_id] ?? []).length === 0 ? (
                <div className="sb-shotlog-empty">暂无裁决记录</div>
              ) : (
                (logs[r.shot_id] ?? []).map((it) => (
                  <div key={`${r.shot_id}-${it.take_seq}`} className="sb-shotlog-line">
                    Take {it.take_seq} · {it.changed} · {it.verdict}
                    {it.reason ? ` · 原因：${rollReasonLabel(it.reason)}` : ""}
                    {it.evidence ? ` · ${it.evidence}` : ""}
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      ))}
      {failed.length > 0 && (
        <div className="sb-retake-form">
          <div className="sb-retake-row">
            <span className="sb-retake-label">
              原因{needReason ? "（必填，可多选）" : "（首拍可不选，可多选）"}：
            </span>
            {/* 多选（2026-10-10 用户要求）：常不满一项——如同时「人物不一致」+「构图」 */}
            <div className="sb-retake-reasons" role="group" aria-label="重摇原因（可多选）">
              {ROLL_REASONS.map((r) => (
                <button
                  key={r.key}
                  type="button"
                  className={`sb-retake-chip${reasons.includes(r.key) ? " on" : ""}`}
                  title={r.hint}
                  onClick={() => toggleReason(r.key)}
                >
                  {r.label}
                </button>
              ))}
            </div>
            <span className="sb-retake-label">单变量：</span>
            <select
              className="sb-retake-select"
              value={changed}
              onChange={(e) => setChanged(e.target.value)}
            >
              <option value="seed">换种子（同参数重摇）</option>
              <option value="prompt">改提示词（重写）</option>
              <option value="refs">换参考图</option>
              <option value="model">换模型</option>
              <option value="params">改参数</option>
              <option value="both">多项同时改（无学习价值，慎选）</option>
            </select>
            <input
              className="sb-retake-note"
              placeholder="一句话依据（缺陷描述，进镜头日志）"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <label className="sb-retake-force">
              <input
                type="checkbox"
                checked={force}
                onChange={(e) => setForce(e.target.checked)}
              />
              强提（绕过止损）
            </label>
          </div>
          <button
            className="sb-btn primary"
            disabled={busy || (needReason && reasons.length === 0) || otherWithoutNote}
            title={
              otherWithoutNote
                ? "原因选了「其他」——请先写一句备注（否则这条记录无从分析，后端也会拒收）"
                : "只重拍视频（沿用现有首帧）。要换首帧请用「生成首帧」按钮。重出已有视频的镜必须选原因（可多选）"
            }
            onClick={() => onRetake(report.failed_shot_ids,
                                    { changed, note, force, reason: reasons.join(",") })}
          >
            重拍视频·不符镜头（{failed.length}）
          </button>
          {otherWithoutNote && (
            <div className="sb-retake-warn">
              原因选了「其他」，请在备注里写一句说明（后端要求必填）
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** 首帧候选挑选（2026-09-29：「一次抽 N 张」配套的挑选入口）。
 *
 * 首帧比视频便宜得多，而「背对镜头」「两人同框」这类构图模型本身不稳定
 * （实测同一提示词两次抽卡一正一反）——多抽几张、在这里挑定「生效首帧」，
 * 下游（出视频/卡片展示/就绪检查）都按这个指针取。**只改指针、不删产物**，
 * 挑错可随时改回（点「跟随最新」即清除指针）。 */
function FirstFrameCandidates({ shotId, seq, projectId, current, onPicked, onClose }: {
  shotId: string;
  seq: number;
  projectId: string;
  /** 当前生效的首帧产物 id（用于打勾） */
  current?: string;
  onPicked: () => void;
  onClose: () => void;
}) {
  const [items, setItems] = useState<NodeVersionInfo[] | null>(null);
  const [busy, setBusy] = useState("");
  useEffect(() => {
    let alive = true;
    api
      .listVersions(projectId, `shot_${shotId}_ff`)
      .then((v) => {
        if (alive) setItems([...v].reverse());   // 新的在前
      })
      .catch((e) => {
        if (alive) {
          toast.error(`候选加载失败：${e instanceof Error ? e.message : e}`);
          setItems([]);
        }
      });
    return () => {
      alive = false;
    };
  }, [projectId, shotId]);

  const pick = async (versionId?: string) => {
    setBusy(versionId ?? "latest");
    try {
      await api.preferFirstFrame(shotId, versionId);
      toast.success(versionId ? "已设为首帧" : "已改为跟随最新版本");
      onPicked();
      onClose();
    } catch (e) {
      toast.error(`设置失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy("");
    }
  };

  return createPortal(
    <div className="settings-mask" onClick={onClose}>
      <div className="settings-dialog ff-cand-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="settings-head">
          <span className="settings-title">镜头 {seq} · 首帧候选</span>
          <button className="provider-close" onClick={onClose}>关闭</button>
        </div>
        <div className="project-settings-body">
          <div className="ff-cand-hint">
            点一张设为首帧——出视频、卡片展示与就绪检查都按它取。
            「跟随最新」= 不指定，永远用最新抽的那张。
          </div>
          {!items ? (
            <div className="assets-empty">加载中…</div>
          ) : items.length === 0 ? (
            <div className="assets-empty">这一镜还没有首帧候选</div>
          ) : (
            <div className="ff-cand-grid">
              {items.map((v) => (
                <button
                  key={v.id}
                  className={`ff-cand-item${v.artifact_id && v.artifact_id === current ? " on" : ""}`}
                  disabled={!v.artifact_id || !!busy}
                  onClick={() => void pick(v.id)}
                >
                  {v.artifact_id ? (
                    <img src={fileUrl(v.artifact_id)} alt="" loading="lazy" />
                  ) : (
                    <span className="ff-cand-none">无产物</span>
                  )}
                  <span className="ff-cand-tag">
                    {v.artifact_id && v.artifact_id === current ? "当前生效" : `版本 ${v.seq}`}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="prompt-opt-foot">
          <button className="fb-btn ghost" disabled={!!busy} onClick={() => void pick(undefined)}>
            跟随最新
          </button>
          <button className="fb-btn ghost" onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** 画布视图：分镜=节点、相邻分镜连线表达转场（6.6 第 7 条），与卡片流同源互同步。 */
function ShotCanvas({
  shots,
  scenes,
  characters,
  props,
  selectedId,
  busy,
  onSelect,
  onUpdateShot,
  onFirstFrame,
  onVideo,
}: {
  shots: ShotInfo[];
  scenes: SceneInfo[];
  characters: CharacterInfo[];
  props: PropInfo[];
  selectedId: string | null;
  busy: boolean;
  onSelect: (shotId: string) => void;
  onUpdateShot: (shotId: string, body: ShotPatchBody) => void;
  onFirstFrame: (shotId: string) => void;
  onVideo: (shotId: string) => void;
}) {
  /** 拖拽后的节点位置（视图本地态；未拖拽的分镜按 seq 横向排布） */
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});

  const nodes = useMemo<Node[]>(
    () =>
      shots.map((shot, i) => ({
        id: shot.id,
        type: "shot",
        position: positions[shot.id] ?? { x: i * SHOT_STEP, y: 0 },
        data: {
          shot,
          scene: scenes.find((s) => s.id === shot.scene_ref_id) ?? null,
          scenes,
          characters,
          props,
          selected: shot.id === selectedId,
          busy,
          onSelect: () => onSelect(shot.id),
          onFirstFrame,
          onVideo,
          onUpdate: (body: ShotPatchBody) =>
            onUpdateShot(shot.id, body),
        },
      })),
    [shots, scenes, characters, props, selectedId, busy, positions, onSelect, onUpdateShot, onFirstFrame, onVideo],
  );

  const edges = useMemo<Edge[]>(
    () =>
      shots.slice(1).map((shot, i) => ({
        id: `t-${shots[i].id}-${shot.id}`,
        source: shots[i].id,
        target: shot.id,
        type: "smoothstep",
        label: "转场",
        animated: shot.generating,
        style: { stroke: "#3a4160" },
        labelStyle: { fill: "#8b93a7", fontSize: 11 },
        labelBgStyle: { fill: "#171b2b" },
        labelBgPadding: [6, 3],
        labelBgBorderRadius: 4,
      })),
    [shots],
  );

  // 仅应用位置变化（拖拽排布）；选中态由 ShotCard 点击经 onSelect 统一处理
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    for (const c of changes) {
      if (c.type === "position" && c.position) {
        const pos = c.position;
        setPositions((prev) => ({ ...prev, [c.id]: pos }));
      }
    }
  }, []);

  return (
    <div className="sb-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={shotNodeTypes}
        onNodesChange={onNodesChange}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        minZoom={0.15}
      >
        <Background gap={20} color="#20263c" />
        <Controls />
      </ReactFlow>
    </div>
  );
}

export function Storyboard({
  episode,
  onBack,
}: {
  episode: EpisodeInfo;
  /** 返回分集列表；不传则不渲染返回按钮（嵌入右侧工作区时由外层导航代替） */
  onBack?: () => void;
}) {
  const projectId = useDrama((s) => s.projectId);
  const shotsMap = useDrama((s) => s.shots);
  const characters = useDrama((s) => s.characters);
  const scenes = useDrama((s) => s.scenes);
  const props = useDrama((s) => s.props);
  const setActiveEpisode = useDrama((s) => s.setActiveEpisode);
  const loadShots = useDrama((s) => s.loadShots);
  const generateShots = useDrama((s) => s.generateShots);
  const composeEpisode = useDrama((s) => s.composeEpisode);
  const updateShot = useDrama((s) => s.updateShot);
  const regenerateShotPrompt = useDrama((s) => s.regenerateShotPrompt);
  // linkFrames（首尾帧对齐）已从工具栏撤下：批量出片时上一镜视频还没好，
  // 生成器内的「机会式链式衔接」会跳过，只有单镜重生成时才真正接得上——
  // 那条路径已自动生效，不再需要手动按钮。store 方法保留备用。
  const batch = useDrama((s) => s.batch);
  const clearBatch = useDrama((s) => s.clearBatch);
  const shotErrors = useDrama((s) => s.shotErrors);
  // 批量生成按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const batchFfCostTip = useCostTip(BILLING_KEYS.shotsGenerate);
  const batchVideoCostTip = useCostTipWith(
    "批量出视频；缺首帧的分镜会先自动补首帧（只补缺的）",
    BILLING_KEYS.shotsGenerate);

  const shots = shotsMap[episode.id] ?? [];
  const [selectedId, setSelectedId] = useState<string | null>(null);
  /** 视图模式：卡片流 / 画布（6.6 第 7 条，两视图数据同源） */
  const [view, setView] = useState<"cards" | "canvas">("cards");
  /** 批量动作进行中：ff 首帧 / video 视频 / compose 合成 / retake 重拍 / qa 体检报告 */
  const [busy, setBusy] = useState<"" | "ff" | "video" | "compose" | "retake" | "qa" | "review">("");
  /** 首帧一次抽几张（顶栏可选；只影响首帧提交） */
  const [ffCount, setFfCount] = useState<number>(1);
  /** 批次明细展开（失败/取消任务列表） */
  const [batchOpen, setBatchOpen] = useState(false);
  const [notice, setNotice] = useState("");
  /** 预检/审片报告面板（确定性检查结果，6.10.2 / E2） */
  const [report, setReport] = useState<
    { kind: "review" | "qa"; data: ReviewReportData | QaIssueInfo[] } | null
  >(null);
  /** 本集合成成片产物（初始化自分集数据持久入口，合成完成后更新） */
  const [composeArtifactId, setComposeArtifactId] = useState<string | null>(
    episode.compose_artifact_id ?? null,
  );
  /** 下载下拉展开态（合成本集旁的下载入口；点外部关闭） */
  const [dlOpen, setDlOpen] = useState(false);
  /** 手机档：预览播放器弹层的展开态。
      null = 跟随默认（有视频就展开，由 CSS 的 :has(video) 决定）；true/false = 用户手动指定。
      此前只有「强制收起」一档，**手动展开根本没接**（.m-open 从没被设置过），
      于是点标题条上的「展开」毫无反应。 */
  const [previewOpen, setPreviewOpen] = useState<boolean | null>(null);
  /** 手机档判定：预览面板整条标题可点、行为与桌面不同（见下方标题行注释） */
  const isPhone = useIsPhone();
  /** 左侧「定位」条上的缩略图悬浮预览（角色/场景/道具共用一份状态） */
  const { hoverProps: railHoverProps, hoverNode: railHoverNode } = useThumbHover();
  /** 窄屏手风琴：当前展开的分镜（null = 全收起）。
      单开而不是多开——手机上目标就是「一眼扫完本集分镜、直接点到要改的那一镜」，
      同时摊开多张会把列表重新拉长。桌面恒全展开（accordion=false）。 */
  const [openShotId, setOpenShotId] = useState<string | null>(null);
  const dlWrapRef = useRef<HTMLDivElement>(null);
  /** 项目名（下载文件名用：项目名_第n集_标题） */
  const [projectName, setProjectName] = useState("");
  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    api.getProject(projectId).then((p) => {
      if (alive) setProjectName(p.name);
    }).catch(() => {});
    return () => { alive = false; };
  }, [projectId]);
  // 下载文件基名：项目名_第n集_分集标题（Windows 非法字符消毒）
  const episodeFileBase = [
    projectName.trim(), `第${episode.seq}集`, (episode.title || "").trim(),
  ].filter(Boolean).join("_").replace(/[\\/:*?"<>|]/g, "_");
  useEffect(() => {
    if (!dlOpen) return;
    const onDown = (e: MouseEvent) => {
      // e.target instanceof Node：项目内 Node 被 @xyflow 类型遮蔽，不能用 as 断言
      if (dlWrapRef.current && !dlWrapRef.current.contains(e.target instanceof Node ? e.target : null)) {
        setDlOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [dlOpen]);
  /** 生成参数排查弹窗（选中分镜 id；null = 关闭） */
  const [paramsShot, setParamsShot] = useState<{ id: string; seq: number } | null>(null);
  /** 卡片流容器：鼠标纵向滚轮 → 横向滚动（上下滚动即左右浏览分镜） */
  const cardsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // **依赖 `view` 不可省**：切到画布视图时卡片流容器被卸载，切回来是**新建的
    // DOM 节点**；依赖为空则 effect 不重跑，监听器仍挂在已脱离文档的旧节点上，
    // 新容器没有监听 → 滚轮横向滚动失效（用户报障）。
    if (view !== "cards") return;   // 画布视图下容器不存在，不挂
    const el = cardsRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const target = e.target instanceof Element ? e.target : null;
      // 内容区优先：光标在「视频画面描述」查看/编辑框内时，滚轮先滚它自己的
      // 内容。该区域本就有 overflow-y:auto，但旧实现无条件把纵向滚动量转成
      // 卡片流的横向滚动并 preventDefault，于是它永远滚不动（报障：光标放进
      // 描述框上下滚动，整个卡片流横向平移，内容看不到底）。
      if (target?.closest(".vp-view, .vp-editor, textarea")) return;
      // 通用兜底：target 到卡片容器之间若有「可纵向滚动且真的滚得动」的祖先，
      // 同样交还原生滚动，不再劫持（覆盖今后新增的任何内部滚动区）
      for (let node = target; node && node !== el; node = node.parentElement) {
        if (node.scrollHeight > node.clientHeight
            && getComputedStyle(node).overflowY !== "visible") {
          return;
        }
      }
      // 纵向滚动量转横向；触控板的横向 delta 直接放行
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        el.scrollLeft += e.deltaY;
        e.preventDefault();
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [view]);
  /** 视频生成模型选项：本地注册表 t2v 模型 + 模型服务端点模型（ark/agnes 等） */
  const [videoModels, setVideoModels] = useState<ModelInfo[]>([]);
  const [videoModelId, setVideoModelId] = useState("");
  // 项目预设对齐（批次 4 补充）：项目 t2v 预设到达/变化时同步默认选择，
  // 用户手选后不覆盖（手选优先）
  const presetT2v = useDrama((s) => s.modelPresets?.t2v ?? "");
  const videoTouched = useRef(false);
  useEffect(() => {
    if (presetT2v && !videoTouched.current) setVideoModelId(presetT2v);
  }, [presetT2v]);
  // 空值文案显示全局默认端点名（未设置显示「默认」）
  const [emptyLabel, setEmptyLabel] = useState<string | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    fetchModelOptions("t2v").then((models) => {
      if (alive) setVideoModels(models);
    });
    defaultEmptyLabel("t2v").then((label) => {
      if (alive && label) setEmptyLabel(`默认：${label}`);
    });
    return () => {
      alive = false;
    };
  }, []);

  // 进入故事板即设为当前集：任务事件据此刷新分镜列表
  useEffect(() => {
    setActiveEpisode(episode.id);
    loadShots(episode.id).catch(() => {});
  }, [episode.id, setActiveEpisode, loadShots]);

  // 提示条自动消隐
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(""), 6000);
    return () => clearTimeout(t);
  }, [notice]);

  // 批次聚合统计（WS 事件驱动 batch.tasks）：任务按 node_id 映射回分镜 seq，
  // 终态归账；全部终态后展示关闭按钮，失败/取消可展开明细
  const batchStats = useMemo(() => {
    if (!batch) return null;
    // 兼容多切二切关键帧节点（shot_{id}_ff_c2，批次 4 §35.2）：漏掉该形态时
    // 二切任务失败会在明细里显示「镜 ?」无法定位（审计 13 号）
    const shotIdOf = (nodeId: string) => {
      const m = /^shot_(.+)_(ff|v)(_c\d+)?$/.exec(nodeId);
      return m ? m[1] : "";
    };
    const entries = Object.entries(batch.tasks).map(([taskId, t]) => {
      const sid = shotIdOf(t.node_id);
      return {
        taskId, ...t,
        stage: t.node_id.endsWith("_v") ? "视频" : "首帧",
        seq: shots.find((s) => s.id === sid)?.seq ?? 0,
      };
    });
    const done = entries.filter((t) => t.status === "done" || t.status === "cached").length;
    const failed = entries.filter((t) => t.status === "failed");
    const canceled = entries.filter((t) => t.status === "canceled");
    const settled = entries.filter((t) => FINAL_STATUS.includes(t.status)).length;
    return {
      stage: entries.some((t) => t.stage === "视频") ? "视频批次" : "首帧批次",
      total: entries.length,
      done,
      settled,
      // 失败与取消**分开计**：两者的用户动作完全不同（失败=要排查重试，
      // 取消=你自己停的），合成一个「失败/取消 N」读不出该做什么
      failedCount: failed.length,
      canceledCount: canceled.length,
      problemCount: failed.length + canceled.length,
      problems: [...failed, ...canceled],
      percent: entries.length ? Math.round((settled / entries.length) * 100) : 0,
      allSettled: entries.length > 0 && settled === entries.length,
    };
  }, [batch, shots]);

  const selected = shots.find((s) => s.id === selectedId) ?? null;

  // 生成清单预览（批次 3 §34.4，报告铁律 2 轻量版）：提交前展示数量/时长/
  // 模型/风险清单，用户确认后才生成——付费副作用确认前置
  const confirmGenerate = async (): Promise<boolean> => {
    try {
      const p = await api.generatePreview(episode.id);
      const lines = [
        `镜头 ${p.shots} 个 · 总时长 ${p.total_duration}s · 视频模型 ${p.model || "默认"}`,
        p.missing_characters.length
          ? `⚠ 缺定妆角色：${p.missing_characters.join("、")}（提交会被拦截）`
          : "角色参考图完备",
        p.qa_errors > 0
          ? `⚠ 分镜体检 error ${p.qa_errors} 项（提交将被拦截）`
          : "分镜体检无 error 残留",
        p.unresolved_error_count > 0
          ? `⚠ 未绑定参考图引用 ${p.unresolved_error_count} 处：${p.unresolved_sample.join("；")}`
          : "参考图引用全部可绑定",
      ];
      // 端点健康（25 号 1.5）：欠费/队列满**在确认框里**就说清楚——提交前知道
      // 端点接不了单，而不是让每条任务各失败一次
      for (const w of p.provider_warnings ?? []) lines.push(`⚠ ${w}`);
      // 积分预估（33 号方案）：按次计费，把这次提交的消耗也摆进确认框
      const cost = costLine(BILLING_KEYS.shotsGenerate);
      if (cost) lines.push(`· ${cost}`);
      return await confirmDialog({ title: "生成清单预览", message: lines.join("\n") });
    } catch (e) {
      toast.error(`生成预览失败：${e instanceof Error ? e.message : e}`);
      return false;
    }
  };

  /** 重摇原因弹窗（25 号 2.4）：`run` 收到已校验的 (原因, 备注) 后真正提交。
   *  所有「重出已有产出」的入口都先过这里——没有原因的重摇只是继续盲摇。 */
  const [rerollAsk, setRerollAsk] = useState<{
    title: string;
    subtitle: string;
    confirmText: string;
    run: (reason: string, note: string) => Promise<void>;
  } | null>(null);

  /** 首帧**实际提交**（原因已定或本镜是首拍；批量走全集清单确认）。
   *
   * 走 submitWithQaEscape：首帧链路同样会被「端点不可用」硬门拦住，而它的
   * 逃生口（force）只有这个包装会弹确认框——不接的话用户充值后仍被拦死、
   * 提示里说的「勾选强提」在首帧路径上根本不存在（25 号复查：后端/前端/回归
   * 三方各自独立报到同一处）。 */
  const submitFirstFrames = async (
    shotId: string | undefined,
    opts: { reason?: string; note?: string },
  ) => {
    if (!shotId && !(await confirmGenerate())) return;
    setBusy("ff");
    try {
      await submitWithQaEscape(async (esc) => {
        const res = await generateShots(episode.id, "first_frames", undefined,
          shotId ? [shotId] : undefined,
          { reason: opts.reason, note: opts.note,
            ignore_storyboard_qa: esc.ignore, force: esc.force,
            count: ffCount });
        noteAnchorFix(res.anchor_fixed);
      });
    } finally {
      setBusy("");
    }
  };

  /** 出首帧时若后端自动补过人物朝向锚点，明确告诉用户（否则这次修正无声无息） */
  const noteAnchorFix = (seqs?: number[]) => {
    if (!seqs || seqs.length === 0) return;
    toast.success(
      `已自动修正 ${seqs.length} 镜的人物朝向（第 ${seqs.join("、")} 镜）——` +
      "原来只写了「侧脸」没说朝哪边，生图会画反；本次首帧已按修正后的姿态生成",
    );
  };

  const runFirstFrames = async (shotId?: string) => {
    // 单镜生成：定向提交该镜（后端按 shot_ids 过滤），无需全集清单确认
    if (shotId) {
      const shot = shots.find((s) => s.id === shotId);
      if (shot?.first_frame_artifact_id) {
        // 重出已有首帧 = 重摇：**先选原因**（后端同样会拦，前端先问更省事）
        setRerollAsk({
          title: `重新生成镜 ${shot.seq} 的首帧`,
          subtitle: "这一镜已有首帧，本次提交是重摇——请说明哪里不满意",
          confirmText: "提交重出",
          run: (reason, note) => submitFirstFrames(shotId, { reason, note }),
        });
        return;
      }
      await submitFirstFrames(shotId, {});
      return;
    }
    const rerollCount = shots.filter((s) => s.first_frame_artifact_id).length;
    if (rerollCount > 0) {
      setRerollAsk({
        title: "批量生成首帧",
        subtitle: `本集有 ${rerollCount} 镜已有首帧，本次会重出这些镜头——原因会记入每一条重摇日志`,
        confirmText: "提交批量生成",
        run: (reason, note) => submitFirstFrames(undefined, { reason, note }),
      });
      return;
    }
    await submitFirstFrames(undefined, {});
  };

  /** 体检 error 硬门的逃生口：确认后带 ignore_storyboard_qa 重提交。
   *
   * 后端对 videos/all 模式在体检 error 未清零时返回 400（带逐镜清单），
   * 此前前端没有对应开关——遇上无法一键修复的 error 就整集卡死（审计 13 号
   * H）。这里把 400 转成明确的「忽略并生成」选择，仍以清单为先。
   *
   * 同时**自动展开体检报告面板**：被门禁拦下正是用户最需要看到「哪几镜有问题、
   * 怎么修」的时刻，原先只弹一句错误文本，用户还得自己想起去点「体检报告」
   * 按钮（该按钮已从工具栏撤下，改为在这里按需出现）。
   */
  const submitWithQaEscape = async (
    run: (opts: { ignore: boolean; force: boolean }) => Promise<void>,
  ): Promise<void> => {
    /** 问一次「端点不可用仍要强提吗」（25 号 1.5：账本可能滞后于刚充值的事实） */
    const askForce = async (msg: string): Promise<boolean> =>
      confirmDialog({
        title: "端点当前不可用",
        message: `${msg}\n\n该端点最近一次调用失败（欠费/队列满）。若已充值成功或确认平台已恢复，可强制提交一次（成功即自动解除限制）；否则建议先充值或稍后重试。`,
        confirmText: "强制提交",
        cancelText: "取消",
        danger: true,
      });
    const attempt = async (opts: { ignore: boolean; force: boolean }) => {
      try {
        await run(opts);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        // 端点不可用：标记文案由后端常量给出（ENDPOINT_UNAVAILABLE_TAG），
        // 不靠对整句文案的匹配——改文案不该让逃生口静默失效
        if (!opts.force && msg.includes("[端点不可用]")) {
          if (await askForce(msg)) await attempt({ ...opts, force: true });
          return;
        }
        // 盲摇止损（REROLL_BUDGET_TAG）：后端文案让用户"强提"，而单镜/批量
        // 入口此前没有强提通道——被堵死且不知为何（2026-10-10 实测）。把
        // 标记转成明确的「仍然重摇」确认，确认即带 force 重提交。
        if (!opts.force && msg.includes("[重摇预算]")) {
          const ok = await confirmDialog({
            title: "这一镜已经盲摇过两次",
            message: `${msg}\n\n同参数再摇一次大概率读不出新信息（这就是止损的用意所在）。确认放弃学习价值、仍然再摇一次吗？`,
            confirmText: "仍然重摇",
            cancelText: "取消",
            danger: true,
          });
          if (ok) await attempt({ ...opts, force: true });
          return;
        }
        throw e;
      }
    };
    try {
      await attempt({ ignore: false, force: false });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // 匹配「体检存在」而非固定整句：后端文案会随白话化调整（如「分镜体检
      // 存在」→「本镜体检存在」），钉死整句会让逃生口在改文案时静默失效
      if (!msg.includes("体检存在")) {
        toast.error(msg);
        return;
      }
      await runQaReport();   // 先展开报告（含定位/修复动作），再问是否忽略
      const ok = await confirmDialog({
        title: "分镜体检未通过",
        message: `${msg}\n\n这些镜头生成前建议先修复（报告已展开，可定位镜头并一键修复）。确定要忽略体检错误继续生成吗？（会记录在案）`,
        confirmText: "忽略并生成",
        cancelText: "返回修复",
        danger: true,
      });
      if (!ok) return;
      try {
        await attempt({ ignore: true, force: false });
      } catch (e2) {
        toast.error(e2 instanceof Error ? e2.message : String(e2));
      }
    }
  };

  /** 批量出视频。缺首帧的镜先补首帧再出视频——吸收原「全部生成」的编排能力。
   *
   * 只补**缺的**：后端 submit_first_frame 没有「已有首帧则跳过」的判断，全集
   * 重提交只能靠任务缓存兜底（提示词/参考图一变就是 cache miss，真金白银重出图）。
   * 原「全部生成」无差别重提交全集首帧，首帧齐了再点就是白跑一轮。
   */
  /** 单镜生成/重新生成视频：定向提交本镜（后端按 shot_ids 过滤，不影响其他分镜）。
   *
   * 首次出片（本镜还没有视频）不计入「盲摇」预算——后端按有无既有视频区分
   * 首拍与重拍，首拍记 pending、不参与止损；只有对已有视频的定向重出才受
   * 「同缺陷两次后强制改写」约束。
   */
  const submitShotVideo = async (
    shotId: string,
    opts: { reason?: string; note?: string },
  ) => {
    const seq = shots.find((s) => s.id === shotId)?.seq;
    setBusy("video");
    try {
      await submitWithQaEscape(async (esc) => {
        const res = await generateShots(episode.id, "videos", videoModelId,
          [shotId], { ignore_storyboard_qa: esc.ignore, force: esc.force,
                      reason: opts.reason, note: opts.note });
        setNotice(
          res.task_ids.length > 0
            ? `已提交镜${seq ?? ""}的视频生成`
            : `镜${seq ?? ""}无法生成：缺首帧或已被门禁拦截（见报告）`,
        );
      });
    } finally {
      setBusy("");
    }
  };

  const runShotVideo = async (shotId: string) => {
    const shot = shots.find((s) => s.id === shotId);
    if (shot?.video_artifact_id) {
      // 重出已有视频 = 重摇：**先选原因**（25 号 2.4）
      setRerollAsk({
        title: `重出镜 ${shot.seq} 的视频`,
        subtitle: "这一镜已有视频，本次提交是重摇——请说明哪里不满意",
        confirmText: "提交重出",
        run: (reason, note) => submitShotVideo(shotId, { reason, note }),
      });
      return;
    }
    await submitShotVideo(shotId, {});
  };

  /** 批量出视频的**实际提交**（原因已定；缺首帧的先补齐——那些是首拍不填原因） */
  const submitVideos = async (opts: { reason?: string; note?: string }) => {
    if (!projectId) return;
    if (!(await confirmGenerate())) return;
    setBusy("video");
    try {
      const missing = shots.filter((s) => !s.first_frame_artifact_id);
      if (missing.length > 0) {
        setNotice(`第 ${missing.map((s) => s.seq).join("、")} 镜缺首帧，先补首帧…`);
        // 补首帧这一步也会被「端点不可用」拦（生图端点与视频端点可能不同）——
        // 同样给它逃生口，否则整条「批量出视频」会在第一步卡死
        await submitWithQaEscape(async (esc) => {
          const first = await generateShots(episode.id, "first_frames", undefined,
            missing.map((s) => s.id), { force: esc.force });
          await waitTasks(projectId, first.task_ids, first.batch_id);
        });
      }
      await submitWithQaEscape(async (esc) => {
        const res = await generateShots(episode.id, "videos", videoModelId, undefined, {
          ignore_storyboard_qa: esc.ignore,
          force: esc.force,
          reason: opts.reason, note: opts.note,
        });
        setNotice(
          res.task_ids.length > 0
            ? `已提交 ${res.task_ids.length} 个视频任务`
            : "没有可生成视频的分镜（首帧可能未产出）",
        );
      });
    } catch (e) {
      toast.error(`生成视频提交失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy("");
    }
  };

  const runVideos = async () => {
    if (!projectId) return;
    const rerollCount = shots.filter((s) => s.video_artifact_id).length;
    if (rerollCount > 0) {
      setRerollAsk({
        title: "批量生成视频",
        subtitle: `本集有 ${rerollCount} 镜已有视频，本次会重出这些镜头——请选择原因（记入每条重摇日志）`,
        confirmText: "提交批量生成",
        run: (reason, note) => submitVideos({ reason, note }),
      });
      return;
    }
    await submitVideos({});
  };

  /** 合成本集：**先自动审片**，有拍坏的镜头则弹报告让用户决定；无问题才提交合成。
   *
   * compose 端点本身没有任何质量闸——出完几十条视频直接拼，黑帧/时长跑偏的
   * 镜头会静默进成片。审片此前是工具栏上的手动按钮，很容易忘（该按钮已撤下，
   * 改为在合成这一步自动发生）。
   */
  const runCompose = async () => {
    setBusy("compose");
    try {
      const review = await api.reviewEpisode(episode.id);
      if (review.failed_shot_ids.length > 0) {
        setReport({ kind: "review", data: review });
        const ok = await confirmDialog({
          title: "合成前审片发现问题",
          message: `${review.failed_shot_ids.length} 个镜头的视频有确定性缺陷（时长偏差/纯色黑帧/缺视频）。`
            + "报告已展开，可直接重拍不符镜头。\n\n仍要现在合成吗？（这些镜头会照原样进成片）",
          confirmText: "仍要合成",
          cancelText: "先处理",
          danger: true,
        });
        if (!ok) {
          setBusy("");
          return;
        }
      }
    } catch (e) {
      // 审片失败不阻断合成（它是辅助质检，不是硬门）
      toast.warning(`审片跳过：${e instanceof Error ? e.message : e}`);
    }
    // 记住旧成片：本次合成失败/取消时回填，避免「已有成片在故事板不可回看」
    // （审计 13 号：旧实现先置 null 且仅在 done 回填，失败后播放器与「播放本集
    // 成片」入口一起消失，须返回分集列表重进才恢复）
    const prevCompose = composeArtifactId ?? episode.compose_artifact_id ?? null;
    setComposeArtifactId(null);
    try {
      const { task_id: taskId } = await composeEpisode(episode.id);
      for (let i = 0; i < POLL_MAX; i++) {
        await sleep(POLL_MS);
        const task = await api.getTask(taskId).catch(() => null);
        if (!task || !FINAL_STATUS.includes(task.status)) continue;
        if (task.status === "done" && task.artifact_id) {
          setComposeArtifactId(task.artifact_id);
          setSelectedId(null);
          setNotice("本集合成完成，成片已在右侧播放器展示");
        } else {
          setComposeArtifactId(prevCompose);  // 失败回填旧成片
          toast.warning(`本集合成未完成：${task.error || task.status}`);
        }
        return;
      }
      setComposeArtifactId(prevCompose);  // 轮询超时同样回填
    } catch (e) {
      setComposeArtifactId(prevCompose);  // 提交异常同样回填旧成片
      toast.error(`合成任务提交失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy("");
    }
  };

  // 下载入口：分镜视频打包（后端 zip）与成片直下；无产物对应项禁用
  const shotVideoCount = shots.filter((s) => s.video_artifact_id).length;
  const composeId = composeArtifactId ?? episode.compose_artifact_id ?? null;
  const triggerDownload = (url: string, name?: string) => {
    const a = document.createElement("a");
    a.href = url;
    if (name) a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  const downloadShotVideos = () => {
    setDlOpen(false);
    toast.info("正在打包分镜视频，完成后浏览器自动下载…");
    triggerDownload(`/api/episodes/${episode.id}/shots/download`);
  };
  const downloadCompose = () => {
    setDlOpen(false);
    if (!composeId) return;
    triggerDownload(fileUrl(composeId), `${episodeFileBase}.mp4`);
  };

  // 成片预检（确定性检查不改数据，6.10.2）：资产完备/段间衔接/景别层次/
  // 台词覆盖/轴线抽查/时序对账 + 时长分布与模型建议
  //
  // 已从工具栏撤下（2026-09-23）：它展示的信息在每次批量生成的「生成清单
  // 预览」里已经逐条弹过一遍（镜头数/时长/模型/缺定妆角色/体检 error/未绑
  // 引用），提交被硬门拦截时后端还会再报一次明细。保留按钮 = 第三条路径看
  // 同一份信息。api.precheckEpisode 与后端端点保留，供后续按需复用。

  // 分镜体检报告（解析期存档 QA 全量，白话解释）：报告项可定位镜头/
  // 一键重算描述/一键修正衔接
  const runQaReport = async () => {
    setBusy("qa");
    try {
      const r = await api.getStoryboardQa(episode.id);
      setReport({ kind: "qa", data: r.items });
    } catch (e) {
      toast.error(`体检报告加载失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy("");
    }
  };

  // 体检项一键修复——重算描述：重写该镜画面描述，内容类问题项自动消除；
  // 修正衔接：PATCH transition_in 后重拉报告
  const fixQaByRecalc = async (seq: number) => {
    const shot = shots.find((s) => s.seq === seq);
    if (!shot) return;
    setBusy("qa");
    try {
      await regenerateShotPrompt(shot.id);
      const r = await api.getStoryboardQa(episode.id);
      setReport({ kind: "qa", data: r.items });
      toast.success(`镜 ${seq} 画面描述已重算`);
    } catch (e) {
      toast.error(`重算失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy("");
    }
  };

  const fixQaTransition = async (seq: number, transitionIn: string) => {
    const shot = shots.find((s) => s.seq === seq);
    if (!shot) return;
    setBusy("qa");
    try {
      await updateShot(shot.id, { transition_in: transitionIn });
      const r = await api.getStoryboardQa(episode.id);
      setReport({ kind: "qa", data: r.items });
      toast.success(`镜 ${seq} 衔接类型已修正`);
    } catch (e) {
      toast.error(`修正失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy("");
    }
  };

  // 确定性审片（E2）：视频时长偏差/纯色黑帧/缺产物 → 报告 + 定向重拍
  // 已从工具栏撤下（2026-09-23）：改为**合成本集时自动跑**（见 runCompose），
  // 手动按钮容易忘——而它是合成前唯一的质量闸（compose 端点本身没有质量门）。

  // 视觉自动判级（批次 3.5）：逐镜抽帧拼图 → 多模态 LLM 五级裁决（AI 建议
  // 落镜头日志，人工可覆判——auto 是建议不是终裁）
  const runAutoReview = async () => {
    setBusy("review");
    try {
      const r = await api.autoReviewEpisode(episode.id);
      if (r.auto_count > 0) {
        toast.success(`AI 审片完成：${r.auto_count} 镜已出判级建议（见镜头日志）`);
        const data = await api.reviewEpisode(episode.id);
        setReport({ kind: "review", data });
      } else {
        toast.warning("没有可判级的镜头（缺视频产物或判级失败，详见后端日志）");
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  // 重拍不符镜头（批次 3 §34：带单变量记录/备注/止损强提——同缺陷两次
  // 盲摇会被后端止损规则拦截，改提示词走 rewrite 不受限）
  //
  // 只重拍【视频】（审计 13 号 I）：此前用 "all" 会先全量提交新首帧、紧接着
  // 提交视频，而视频取的是已落库的旧首帧——白付一整套首帧费用且重拍「没效果」
  // （改参考图后视频仍从旧首帧起像）。首帧要重出请用「生成首帧」按钮，语义分离。
  const runRetake = async (
    shotIds: string[],
    opts: { changed: string; note: string; force: boolean; reason: string },
  ) => {
    setBusy("retake");
    setReport(null);
    try {
      const res = await generateShots(episode.id, "videos", videoModelId, shotIds, {
        changed: opts.changed,
        note: opts.note,
        // 重摇原因（25 号 2.4）：重出已有视频的镜必填，后端同样会拦
        reason: opts.reason,
        force: opts.force,
        ignore_storyboard_qa: true,
      });
      setNotice(
        res.task_ids.length > 0
          ? `已提交 ${res.task_ids.length} 个重拍任务（只重拍视频，单变量：${opts.changed}）`
          : "没有可重拍的任务（镜头可能缺首帧引用），请查看报告明细",
      );
    } catch (e) {
      toast.error(`重拍提交失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy("");
    }
  };

  // 资产栏点击定位：选中分镜；卡片流滚动到目标卡，画布视图仅选中高亮
  const locate = (predicate: (s: ShotInfo) => boolean) => {
    const target = shots.find(predicate);
    if (!target) return;
    setSelectedId(target.id);
    if (view === "canvas") return;
    document
      .getElementById(`shot-${target.id}`)
      ?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  };

  const videoSrc = selected
    ? selected.video_artifact_id
      ? fileUrl(selected.video_artifact_id)
      : null
    : composeArtifactId
      ? fileUrl(composeArtifactId)
      : null;

  /** 预览面板当前是否展开：用户手动指定优先，否则「有视频就自动展开」。
      这个判断放在 JS 里而不是只靠 CSS 的 `:has(video)`——夸克等老内核没有 `:has()`，
      光靠 CSS 会出现「状态说有视频该展开、界面却没展开」的不一致。 */
  const previewShown = previewOpen ?? !!videoSrc;
  const playerRef = useRef<HTMLVideoElement>(null);

  // 收起播放器时暂停：面板收成一条后视频还在放（只剩声音），体验很怪
  useEffect(() => {
    if (!previewShown) playerRef.current?.pause();
  }, [previewShown]);

  // 打开参数弹窗时先收起播放器。**手机浏览器的视频是独立硬件合成层**：
  // <video> 会画在普通 DOM 之上，弹窗 z-index 再高也盖不住（实测用户反馈
  // 「点参数的弹窗层级低于预览弹窗」）。收起后视频移出视口，冲突自然消失。
  useEffect(() => {
    if (paramsShot) setPreviewOpen(false);
  }, [paramsShot]);


  return (
    <div className="storyboard">
      <div className="sb-topbar">
        {onBack && (
          <button className="ghost-btn" onClick={onBack}>
            返回分集列表
          </button>
        )}
        {/* 集号与集名不在这里重复：故事板正上方的集头部（.ep-ws-head）已经写明，
            这里只放操作条（原来标题与操作按钮同排，标题是同一页里的第三次重复） */}
        <div className="sb-actions">
          <button
            className="sb-btn"
            onClick={() => setView(view === "cards" ? "canvas" : "cards")}
          >
            {view === "cards" ? "画布视图" : "卡片流"}
          </button>
          <ModelSelect models={videoModels} value={videoModelId} onChange={(v) => { videoTouched.current = true; setVideoModelId(v); }} emptyLabel={emptyLabel} />
          {/* 首帧抽卡张数（2026-09-29）：首帧便宜，而「背对镜头/两人同框」这类
              构图模型不稳定——多抽几张再在卡片上挑，比反复改措辞务实 */}
          <label className="sb-btn sb-ffcount" title="一次抽几张首帧（多张各落一个版本，在卡片「候选」里挑）">
            首帧
            <select
              value={ffCount}
              onChange={(e) => setFfCount(Number(e.target.value))}
              disabled={!!busy}
            >
              {CANDIDATE_COUNTS.map((n) => (
                <option key={n} value={n}>{n} 张</option>
              ))}
            </select>
          </label>
          <button className="sb-btn" disabled={!!busy}
                  onClick={() => void runFirstFrames()}
                  title={batchFfCostTip || undefined}>
            {busy === "ff" ? "提交中…" : "批量生成首帧"}
          </button>
          <button
            className="sb-btn"
            disabled={!!busy}
            title={batchVideoCostTip}
            onClick={runVideos}
          >
            {busy === "video" ? "提交中…" : "批量生成视频"}
          </button>
          <button
            className="sb-btn primary"
            disabled={!!busy}
            title="合成前自动审片：有时长偏差/黑帧的镜头会先弹出报告"
            onClick={runCompose}
          >
            {busy === "compose" ? "合成中…" : "合成本集"}
          </button>
          {/* 下载入口：下拉选「所有分镜（后端打包）/ 本集成片（直下）」，
              对应产物未生成时禁用该下拉项 */}
          <div className="sb-dl-wrap" ref={dlWrapRef}>
            <button
              className="sb-btn"
              disabled={!!busy}
              title="下载分镜视频或本集成片"
              onClick={() => setDlOpen((v) => !v)}
            >
              下载 ▾
            </button>
            {dlOpen && (
              <div className="sb-dl-menu">
                <button
                  disabled={shotVideoCount === 0}
                  title={shotVideoCount === 0
                    ? "尚无已完成的分镜视频"
                    : "打包下载全部已完成的分镜视频"}
                  onClick={downloadShotVideos}
                >
                  下载所有分镜{shotVideoCount > 0 ? `（${shotVideoCount} 个）` : "（暂无）"}
                </button>
                <button
                  disabled={!composeId}
                  title={composeId ? "下载已合成的本集成片" : "尚未合成本集"}
                  onClick={downloadCompose}
                >
                  下载本集成片{composeId ? "" : "（未合成）"}
                </button>
              </div>
            )}
          </div>
          {/* 「播放本集成片」紧挨合成本集：有产物时才出现，点击切到成片预览 */}
          {composeArtifactId && (
            <button
              className="sb-btn"
              disabled={!!busy}
              title="在右侧播放器查看本集已合成的成片"
              onClick={() => setSelectedId(null)}
            >
              播放本集成片
            </button>
          )}
        </div>
      </div>
      {notice && <div className="sb-notice">{notice}</div>}
      {/* 体检/审片报告面板（确定性检查结果）——两者都已改为「按需自动出现」：
          体检报告在被生成门禁拦下时展开（submitWithQaEscape），审片在点合成
          时自动跑并展开。工具栏不再常驻这两个按钮。 */}
      {report && (
        <div className="sb-report">
          <div className="sb-report-head">
            <span className="sb-report-title">
              {report.kind === "qa"
                ? "分镜体检报告（白话说明，可定位镜头并一键修复）"
                : "审片报告（确定性检查，重拍走原参数单变量）"}
            </span>
            <button className="sb-batch-close" title="关闭" onClick={() => setReport(null)}>
              ✕
            </button>
          </div>
          {report.kind === "qa" ? (
            <QaPanel
              report={report.data as QaIssueInfo[]}
              shots={shots}
              busy={!!busy}
              onLocate={(shotId) => {
                setSelectedId(shotId);
                setReport(null);
              }}
              onRecalc={(seq) => void fixQaByRecalc(seq)}
              onFixTransition={(seq, t) => void fixQaTransition(seq, t)}
            />
          ) : (
            <ReviewPanel
              report={report.data as ReviewReportData}
              projectId={projectId ?? ""}
              onRetake={runRetake}
              onAutoReview={runAutoReview}
              busy={!!busy}
            />
          )}
        </div>
      )}
      {/* 批次聚合进度条：N/M 完成 + 失败明细（方案 B，任务事件实时驱动）。
          has-fail 时整条转为失败配色（失败文本 + 进度条转红）——「有任务真
          失败了」和「只是我自己取消了」需要一眼区分开。 */}
      {batchStats && batchStats.total > 0 && (
        <div className={`sb-batch${batchStats.allSettled ? " settled" : ""}`
          + `${batchStats.failedCount > 0 ? " has-fail" : ""}`}>
          <div className="sb-batch-main">
            <span className="sb-batch-label">
              {batchStats.stage} {batchStats.settled}/{batchStats.total} 完成
            </span>
            {/* 失败与取消分开显示，各自的处理动作不同（失败→排查重试 / 取消→无需处理） */}
            {batchStats.failedCount > 0 && (
              <span className="sb-batch-pill fail">失败 {batchStats.failedCount}</span>
            )}
            {batchStats.canceledCount > 0 && (
              <span className="sb-batch-pill cancel">已取消 {batchStats.canceledCount}</span>
            )}
            <div className="sb-batch-track">
              <div className="sb-batch-bar" style={{ width: `${batchStats.percent}%` }} />
            </div>
            {batchStats.problemCount > 0 && (
              <button className="sb-batch-toggle" onClick={() => setBatchOpen((v) => !v)}>
                {batchOpen ? "收起明细" : "查看明细"}
              </button>
            )}
            {batchStats.allSettled && (
              <button className="sb-batch-close" title="关闭" onClick={clearBatch}>
                ✕
              </button>
            )}
          </div>
          {batchOpen && batchStats.problemCount > 0 && (
            <div className="sb-batch-detail">
              {batchStats.problems.map((t) => (
                <div key={t.taskId}
                     className={`sb-batch-row${t.status === "failed" ? " fail" : ""}`}>
                  <span className="sb-batch-loc">镜 {t.seq || "?"}（{t.stage}）</span>
                  <span className="sb-batch-status">{t.status === "failed" ? "失败" : "已取消"}</span>
                  <span className="sb-batch-err" title={t.error}>{t.error || "-"}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <div className="sb-main">
        <aside className="sb-rail">
          <div className="sb-rail-title">角色（{characters.length}）</div>
          {characters.map((c) => {
            const img = mainImage(c);
            return (
              <button
                key={c.id}
                className="rail-item"
                title={`定位 ${c.name} 出现的分镜`}
                onClick={() => locate((s) => s.character_ref_ids.includes(c.id))}
                {...railHoverProps(img?.artifact_id, c.name)}
              >
                <span className="rail-thumb">
                  {img?.artifact_id ? (
                    <img src={fileUrl(img.artifact_id)} alt={c.name} />
                  ) : (
                    <span>{c.name.slice(0, 1)}</span>
                  )}
                </span>
                <span className="rail-name">{c.name}</span>
              </button>
            );
          })}
          {characters.length === 0 && <div className="rail-empty">暂无角色</div>}
          <div className="sb-rail-title">场景（{scenes.length}）</div>
          {scenes.map((s) => (
            <button
              key={s.id}
              className="rail-item"
              title={`定位引用 ${s.name} 的分镜`}
              onClick={() => locate((shot) => shot.scene_ref_id === s.id)}
              {...railHoverProps(s.artifact_id, s.name)}
            >
              <span className="rail-thumb">
                {s.artifact_id ? (
                  <img src={fileUrl(s.artifact_id)} alt={s.name} />
                ) : (
                  <span>{s.name.slice(0, 1)}</span>
                )}
              </span>
              <span className="rail-name">{s.name}</span>
            </button>
          ))}
          {scenes.length === 0 && <div className="rail-empty">暂无场景</div>}
          <div className="sb-rail-title">道具（{props.length}）</div>
          {props.map((p) => (
            <button
              key={p.id}
              className="rail-item"
              title={`定位引用 ${p.name} 的分镜`}
              onClick={() => locate((shot) => shot.prop_ref_ids?.includes(p.id))}
              {...railHoverProps(p.artifact_id, p.name)}
            >
              <span className="rail-thumb">
                {p.artifact_id ? (
                  <img src={fileUrl(p.artifact_id)} alt={p.name} />
                ) : (
                  <span>{p.name.slice(0, 1)}</span>
                )}
              </span>
              <span className="rail-name">{p.name}</span>
            </button>
          ))}
          {props.length === 0 && <div className="rail-empty">暂无道具</div>}
        </aside>
        {view === "canvas" ? (
          <ShotCanvas
            shots={shots}
            scenes={scenes}
            characters={characters}
            props={props}
            selectedId={selectedId}
            busy={!!busy}
            onSelect={setSelectedId}
            onUpdateShot={(shotId, body) =>
              updateShot(shotId, body).catch((e) =>
                toast.error(`保存失败：${e instanceof Error ? e.message : e}`),
              )
            }
            onFirstFrame={(sid) => void runFirstFrames(sid)}
            onVideo={(sid) => void runShotVideo(sid)}
          />
        ) : (
          <div className="sb-cards" ref={cardsRef}>
            {shots.length === 0 && (
              <div className="sb-cards-empty">本集暂无分镜，点击右侧卡片新增</div>
            )}
            {shots.map((shot) => (
              <ShotCard
                key={shot.id}
                shot={shot}
                scene={scenes.find((s) => s.id === shot.scene_ref_id) ?? null}
                scenes={scenes}
                characters={characters}
                props={props}
                selected={shot.id === selectedId}
                busy={!!busy}
                onSelect={() => setSelectedId(shot.id)}
                onFirstFrame={(sid) => void runFirstFrames(sid)}
            onVideo={(sid) => void runShotVideo(sid)}
                onUpdate={(body) =>
                  updateShot(shot.id, body).catch((e) =>
                    toast.error(`保存失败：${e instanceof Error ? e.message : e}`),
                  )
                }
                accordion={isPhone}
                open={openShotId === shot.id}
                onToggle={() => setOpenShotId((cur) => (cur === shot.id ? null : shot.id))}
                onModalOpen={() => setPreviewOpen(false)}
              />
            ))}
            <AddShotCard episodeId={episode.id} scenes={scenes} />
          </div>
        )}
        <aside className={`sb-preview${previewOpen === false ? " m-closed" : previewShown ? " m-open" : ""}`}>
          {/* 手机档：整条标题就是「展开/收起」的开关——原来是标题右侧一个 36px 的小按钮，
              手指点不中；而且收起时面板位移动画还没走完，浏览器补发的 click 会落到面板下面的
              卡片上（正好点开「画面描述」放大弹窗），看起来像层级错乱。整条做大目标后，
              点哪儿都在同一条上。桌面端这行不可点（右栏常驻，无需展开/收起）。 */}
          <div
            className={`sb-preview-title${isPhone ? " tappable" : ""}`}
            onClick={isPhone ? () => setPreviewOpen(!previewShown) : undefined}
          >
            {selected ? `分镜 ${selected.seq} 视频` : composeArtifactId ? "本集成片" : "预览播放器"}
            {selected && (
              <button
                className="ghost-btn"
                title="查看该镜发给模型的最终提示词与参考图清单（排查串脸/构图/方向问题）"
                onClick={(e) => {
                  e.stopPropagation();      // 别把「查看参数」当成「展开/收起」
                  setParamsShot({ id: selected.id, seq: selected.seq });
                }}
              >
                查看参数
              </button>
            )}
            <span className="ghost-btn m-only sb-preview-toggle">
              {previewShown ? "收起" : "展开"}
            </span>
          </div>
          <div className="sb-player">
            {videoSrc ? (
              <video ref={playerRef} key={videoSrc} src={videoSrc} controls />
            ) : busy === "compose" ? (
              <span className="sb-player-hint">本集合成中…</span>
            ) : selected ? (
              <span className="sb-player-hint">当前分镜暂无视频，生成后在此预览</span>
            ) : (
              <span className="sb-player-hint">点击分镜卡预览视频</span>
            )}
          </div>
          {selected && (
            <div className="sb-preview-desc">
              <span className="shot-label">画面描述</span>
              <p>{selected.description}</p>
              {selected.video_prompt && (
                <>
                  <span className="shot-label">视频画面描述（导演级）</span>
                  <p className="vp-preview">
                    <MentionText
                      text={selected.video_prompt}
                      characters={characters}
                      scenes={scenes}
                      props={props}
                      costumeMap={selected.costume_map}
                    />
                  </p>
                </>
              )}
            </div>
          )}
        </aside>
      </div>
      {railHoverNode}
      {paramsShot && (
        <GenerationParamsModal
          shotId={paramsShot.id}
          seq={paramsShot.seq}
          onClose={() => setParamsShot(null)}
        />
      )}
      {/* 重摇原因（25 号 2.4）：重出已有产出的入口统一先过这一框——原因进
          镜头日志，是「质量类重摇的原因分布」唯一的来源 */}
      {rerollAsk && (
        <RollReasonDialog
          title={rerollAsk.title}
          subtitle={rerollAsk.subtitle}
          confirmText={rerollAsk.confirmText}
          busy={!!busy}
          onClose={() => setRerollAsk(null)}
          onSubmit={async (reason, note) => {
            const run = rerollAsk.run;
            setRerollAsk(null);
            await run(reason, note);
          }}
        />
      )}
    </div>
  );
}
