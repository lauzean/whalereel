/** 资产管理页：三个水平 tab（角色 / 场景 / 道具，带数量角标），一次只显示一组。
 *
 * 布局依据 docs/prototype/drama-ui.html：卡片流本身不变（角色/场景/道具三套卡片
 * 及其全部操作原样保留），只是把「三组一起铺」改成 tab 切换——逐集后资产会积累到
 * 几十个，一屏铺三组反而找不到当前要维护的那一类。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  api,
  fileUrl,
  type AssetDeleteRefs,
  type CharacterImageInfo,
  type CharacterInfo,
  type CostumeSuggestionInfo,
  type ModelInfo,
  type NodeVersionInfo,
  type PropInfo,
  type SceneInfo,
} from "../api/client";
import { defaultEmptyLabel, fetchModelOptions } from "../api/modelOptions";
import { BILLING_KEYS } from "../billingKeys";
import { useDrama } from "../stores/drama";
import { useCostTip } from "../stores/billing";
import { ModelSelect } from "../ui/ModelSelect";
import { ImageLightbox } from "../ui/ImageLightbox";
import { PromptOptimizeDialog } from "../ui/PromptOptimizeDialog";
import { confirmDialog, promptDialog, toast } from "../ui/feedback";
import { CANDIDATE_COUNTS, DEFAULT_CANDIDATE_COUNT } from "./candidateCounts";

/** 场景补充视角（批次 4 · B4）：与后端 SCENE_EXTRA_VIEWS 对齐。
 *  只做这两种——左/右视角与中景可由主图 + 镜头机位声明覆盖，
 *  这两种是反打与关键动作镜真正需要的（不做满官方 6 视图以控成本）。 */
const EXTRA_VIEWS = [
  { key: "reverse", label: "反向视角" },
  { key: "interaction", label: "交互区特写" },
] as const;

/** 形象类型页签展示顺序（基础 → 四视图 → 妆造 → 三视图历史） */
const KIND_ORDER = ["fourview", "costume"];

/** 形象类型页签文案（basic/costume/triple/fourview） */
const KIND_LABEL: Record<string, string> = {
  // 只剩两种形象：角色设定图（= 角色的基础图）与妆造
  // （历史 basic/triple 记录已于 2026-09-30 迁移成 fourview，标签不再需要）
  costume: "妆造",
  fourview: "角色设定图",
};

/** 生成中状态文案（与画布文生图节点一致）：排队中 / 加载模型 N% / 生成中 N% */
function progressLabel(progress: { status: string; progress: number } | undefined): string {
  if (!progress || progress.status === "queued") return "排队中";
  const pct = Math.round(progress.progress * 100);
  if (progress.status === "loading_model") return `加载模型 ${pct}%`;
  return pct > 0 ? `生成中 ${pct}%` : "生成中";
}

/** 生成中跑马灯进度条（底部：已填充进度 + 流动光斑，与卡片呼吸灯配合） */
function ThumbMarquee({ progress }: { progress: { status: string; progress: number } | undefined }) {
  const pct = Math.max(4, Math.round((progress?.progress ?? 0) * 100));
  return (
    <div className="thumb-marquee">
      <div className="thumb-marquee-fill" style={{ width: `${pct}%` }} />
      <div className="thumb-marquee-light" />
    </div>
  );
}

/** 资产删除映射摘要（确认弹窗展示：分镜引用/妆造映射/形象绑定，角色专用项可缺省）。 */
function refsSummary(refs: AssetDeleteRefs): string {
  const parts: string[] = [];
  if (refs.shots > 0) {
    parts.push(
      `${refs.shots} 个分镜引用` +
        (refs.episodes.length > 0 ? `（第 ${refs.episodes.join("、")} 集）` : ""),
    );
  }
  if (refs.costume_maps) parts.push(`${refs.costume_maps} 个妆造映射`);
  if (refs.asset_links) parts.push(`${refs.asset_links} 条形象绑定`);
  return parts.length > 0
    ? `映射情况：${parts.join("；")}，删除后将自动清除。`
    : "未被任何分镜引用，可安全删除。";
}

/** 资产名称行内文本：双击进入编辑，Enter/失焦保存，Esc 取消（角色/场景/道具三卡共用）。 */
function AssetNameText({ name, onRename }: { name: string; onRename: (name: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  // Esc 取消标记：blur 统一作为提交入口，取消时由该标记拦截
  const cancelRef = useRef(false);

  // 提交改名：空白或未变化不提交
  const commit = async () => {
    setEditing(false);
    const next = draft.trim();
    if (!next || next === name) return;
    await onRename(next);
  };

  if (!editing) {
    return (
      <span
        className="asset-name-text"
        title="双击重命名"
        onDoubleClick={() => {
          setDraft(name);
          setEditing(true);
        }}
      >
        {name}
      </span>
    );
  }
  return (
    <input
      className="asset-name-input"
      value={draft}
      autoFocus
      title="回车保存，Esc 取消"
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        else if (e.key === "Escape") {
          cancelRef.current = true;
          (e.target as HTMLInputElement).blur();
        }
      }}
      onBlur={() => {
        if (cancelRef.current) {
          cancelRef.current = false;
          setEditing(false);
          return;
        }
        void commit();
      }}
    />
  );
}

/** 版本条公共交互参数（场景/道具/角色三卡共用） */
interface VersionStripProps {
  /** 抽卡版本列表（seq 倒序，仅含已有产物的版本） */
  versions: NodeVersionInfo[];
  /** 当前大图展示的产物 id */
  shownArtifact: string | null;
  /** 生效版本 id（用户「设为生效」的版本，未设置时为最新版本） */
  effectiveVersionId: string | null;
  /** 手动点选中的版本（null = 展示默认版本） */
  pickedVersion: NodeVersionInfo | null;
  /** 点选版本切换大图 */
  onPick: (versionId: string) => void;
  /** 把选中版本设为生效 */
  onPrefer: () => void;
  /** 重命名选中版本（弹输入框） */
  onRename: (version: NodeVersionInfo) => void;
  /** 版本默认名（版本未命名时展示，如「版本 2」「基础-版本 2」） */
  defaultName: (version: NodeVersionInfo) => string;
}

/** 版本条（场景/道具/角色三卡通用）：点选切换大图、生效版本带金色描边、
    选中非生效版本时出现「设为生效」、当前展示版本可重命名（单版本也显示，
    保证命名入口始终可见） */
function VersionStrip({
  versions, shownArtifact, effectiveVersionId, pickedVersion,
  onPick, onPrefer, onRename, defaultName,
}: VersionStripProps) {
  // 当前展示版本：手动点选 ?? 生效版本 ?? 最新版本（与调用方大图逻辑一致）
  const shownVersion =
    pickedVersion ??
    versions.find((v) => v.id === effectiveVersionId) ??
    versions[0] ??
    null;
  return (
    <div className="kind-thumbs">
      {versions.map((v) => {
        const label = v.name || defaultName(v);
        return (
          <button
            key={v.id}
            className={`kind-thumb${v.artifact_id === shownArtifact ? " active" : ""}${v.id === effectiveVersionId ? " effective" : ""}`}
            title={`${label}${v.id === effectiveVersionId ? "（生效）" : ""}`}
            onClick={() => onPick(v.id)}
          >
            <img src={fileUrl(v.artifact_id!)} alt={label} />
          </button>
        );
      })}
      {shownVersion && shownVersion.id !== effectiveVersionId && (
        <button
          className="kind-thumb-prefer"
          title={`把「${shownVersion.name || defaultName(shownVersion)}」设为生效`}
          onClick={onPrefer}
        >
          设为生效
        </button>
      )}
      {shownVersion && (
        <button
          className="kind-thumb-rename"
          title="重命名当前展示版本"
          onClick={() => onRename(shownVersion)}
        >
          改名
        </button>
      )}
    </div>
  );
}

export function CharacterCard({ character, models, emptyLabel }: { character: CharacterInfo; models: ModelInfo[]; emptyLabel?: string }) {
  const generateCharacterImage = useDrama((s) => s.generateCharacterImage);
  const generateFourView = useDrama((s) => s.generateFourView);
  const costumeSuggestions = useDrama((s) => s.costumeSuggestions);
  const acceptCostumeSuggestion = useDrama((s) => s.acceptCostumeSuggestion);
  const rejectCostumeSuggestion = useDrama((s) => s.rejectCostumeSuggestion);
  const cancelCharacterImage = useDrama((s) => s.cancelCharacterImage);
  const deleteCharacterImage = useDrama((s) => s.deleteCharacterImage);
  const uploadVoiceRef = useDrama((s) => s.uploadVoiceRef);
  const deleteVoiceRef = useDrama((s) => s.deleteVoiceRef);
  const applyCostume = useDrama((s) => s.applyCostume);
  const updateCharacter = useDrama((s) => s.updateCharacter);
  const deleteCharacter = useDrama((s) => s.deleteCharacter);
  const preferCharacterImage = useDrama((s) => s.preferCharacterImage);
  const renameNodeVersion = useDrama((s) => s.renameNodeVersion);
  // 花钱按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const acceptCostTip = useCostTip(BILLING_KEYS.costumeAccept);
  const fourViewCostTip = useCostTip(BILLING_KEYS.characterFourView);
  const projectId = useDrama((s) => s.projectId);
  const loadCharacters = useDrama((s) => s.loadCharacters);
  const episodes = useDrama((s) => s.episodes);
  const busy = character.generating;
  const progress = useDrama((s) => s.genProgress[`char_${character.id}`]);
  const [costumeOpen, setCostumeOpen] = useState(false);
  const [cname, setCname] = useState("");
  const [cdesc, setCdesc] = useState("");
  // 妆造映射面板：当前形象 id 与勾选的目标分集
  const [mapId, setMapId] = useState<string | null>(null);
  const [pickedEps, setPickedEps] = useState<string[]>([]);
  const [mapMsg, setMapMsg] = useState("");
  // 预览页签：按形象类型切换大图（只显示已存在的类型）；pickedId 为同类型
  // 多形象时手动点选的展示项
  const [tabKind, setTabKind] = useState<string | null>(null);
  const [pickedId, setPickedId] = useState<string | null>(null);
  // 图片放大预览（灯箱）
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  // 生图模型与生图提示词编辑（LLM 生成，可手动改）
  const [modelId, setModelId] = useState("");
  // 项目预设对齐（批次 4 补充）：项目设置里的 t2i 预设到达/变化时同步为默认
  // 选择；用户手动改过则不覆盖（手选优先）
  const presetT2i = useDrama((s) => s.modelPresets?.t2i ?? "");
  const userPicked = useRef(false);
  useEffect(() => {
    if (presetT2i && !userPicked.current) setModelId(presetT2i);
  }, [presetT2i]);
  const [promptEdit, setPromptEdit] = useState(false);
  /** AI 优化提示词弹窗（批次 3）：优化结果经「应用」后写回 */
  const [optOpen, setOptOpen] = useState(false);
  const [imagePrompt, setImagePrompt] = useState(character.image_prompt);
  useEffect(() => setImagePrompt(character.image_prompt), [character.image_prompt]);

  // 三视图 = **一张拼图覆盖四个角度**（正面/左45°/背面/右45°），系统自动裁成
  // 单角度图，挂到分镜时只挂该镜需要的那一张（占 1 个参考图名额）。
  // 界面把裁出来的四张摆出来（点开可预览），另给单张重做作为补救。
  const hasFourViewSheet = character.images.some(
    (im) => im.kind === "fourview" && im.artifact_id);
  // 三视图（一张图四个角度）：已有就抽卡重生成，没有就新建
  const genSheet = () => {
    const latest = [...character.images].reverse().find((im) => im.kind === "fourview");
    generateFourView(character.id, modelId, latest?.id).catch((e) =>
      toast.error(`三视图生成失败：${e instanceof Error ? e.message : e}`),
    );
  };

  // 预览页签状态推导：activeKind 兜底到第一个已存在类型；
  // 当前展示形象 = 手动点选项 ?? 该类型最新一张有产物的图
  // 页签只有「角色设定图」（= 角色的基础图）与「妆造」——历史 basic/triple 记录
  // 已全量迁移成 fourview（2026-09-30），这里不再有任何兼容分支
  const kinds = KIND_ORDER.filter((k) =>
    character.images.some((im) => im.kind === k),
  );
  const activeKind = tabKind && kinds.includes(tabKind) ? tabKind : (kinds[0] ?? "");
  const kindImages = character.images.filter((im) => im.kind === activeKind);
  const img =
    kindImages.find((im) => im.id === pickedId) ??
    [...kindImages].reverse().find((im) => im.artifact_id) ??
    null;

  // 当前形象的抽卡版本条（与场景/道具卡同语义）：对该形象多次抽卡成版本流，
  // 可设为生效、可单独命名；生成终态（busy true→false）重拉版本列表
  const [versions, setVersions] = useState<NodeVersionInfo[]>([]);
  const [pickedVersionId, setPickedVersionId] = useState<string | null>(null);
  const busyKey = !!progress;
  const imageNodeId = img ? `char_${character.id}_${img.id}` : "";
  const reloadVersions = useCallback(() => {
    if (!projectId || !imageNodeId || !img?.artifact_id) {
      setVersions([]);
      setPickedVersionId(null);
      return Promise.resolve();
    }
    return api
      .listVersions(projectId, imageNodeId)
      .then((list) => {
        const withArtifact = list
          .filter((v) => v.artifact_id)
          .sort((a, b) => b.seq - a.seq);
        setVersions(withArtifact);
        setPickedVersionId(null); // 刷新后回到最新版本
      })
      .catch(() => {});
  }, [projectId, imageNodeId, img?.artifact_id, busyKey]);
  useEffect(() => {
    reloadVersions();
  }, [reloadVersions]);
  // 大图产物：手动点选的版本 ?? 形象生效图（后端按「生效版本优先，否则最新」解析）
  const shownArtifact =
    versions.find((v) => v.id === pickedVersionId)?.artifact_id ??
    img?.artifact_id ??
    null;
  // 生效版本：用户「设为生效」的版本，未设置时为最新版本
  const effectiveVersionId =
    img?.preferred_version_id || versions[0]?.id || null;
  const pickedVersion = versions.find((v) => v.id === pickedVersionId) ?? null;
  // 版本默认名（用户示例：基础-版本1 / 基础-版本2）
  const versionDefaultName = (v: NodeVersionInfo) =>
    `${KIND_LABEL[img?.kind ?? "fourview"] ?? "形象"}-版本 ${v.seq}`;

  // 把预览选中的版本设为该形象的生效图（分镜参考图与卡片展示随之切换）
  const setPreferred = async () => {
    // 兜底：未手动点选（pickedVersion 为 null）时，按钮按 shownVersion 出现
    // （preferred 指向的版本不在条内时）——旧实现直接 return，按钮点了没反应
    // 也无提示（审计 13 号）
    const target =
      pickedVersion ??
      versions.find((v) => v.artifact_id === shownArtifact) ??
      null;
    if (!img || !target) return;
    try {
      await preferCharacterImage(character.id, img.id, target.id);
      toast.success(
        `已设为生效版本（${target.name || versionDefaultName(target)}）`,
      );
    } catch (e) {
      toast.error(`设置失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 版本重命名（输入框弹窗；留空恢复默认命名）
  const renameVersion = async (v: NodeVersionInfo) => {
    if (!imageNodeId) return;
    const name = await promptDialog({
      title: "版本重命名",
      message: `为「${v.name || versionDefaultName(v)}」设置显示名称（留空恢复默认命名）`,
      initial: v.name,
      confirmText: "保存",
    });
    if (name === null) return;
    try {
      await renameNodeVersion(imageNodeId, v.id, name.trim());
      void reloadVersions();
      toast.success("版本已命名");
    } catch (e) {
      toast.error(`命名失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 保存生图提示词（生成定妆图时优先生效）
  const savePrompt = async () => {
    try {
      await updateCharacter(character.id, { image_prompt: imagePrompt });
      setPromptEdit(false);
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 双击快速改名（分镜引用按 id 关联，改名不影响已绑定的引用）
  const renameCharacter = async (name: string) => {
    try {
      await updateCharacter(character.id, { name });
      toast.success("已重命名");
    } catch (e) {
      toast.error(`重命名失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 删除资产（两段式）：先预检映射情况弹确认框，确认后删除并清除全部映射
  // （角色/形象记录与版本删除，图片产物保留在资产库可找回）
  const removeAsset = async () => {
    try {
      const pre = await deleteCharacter(character.id, false);
      const ok = await confirmDialog({
        title: "删除角色",
        message: `确定删除「${character.name}」吗？${refsSummary(
          pre.references ?? { shots: 0, episodes: [] },
        )}图片产物保留在资产库可找回。`,
        confirmText: "删除",
        danger: true,
      });
      if (!ok) return;
      const res = await deleteCharacter(character.id, true);
      const cleared = res.cleared;
      const total =
        (cleared?.shots ?? 0) + (cleared?.costume_maps ?? 0) + (cleared?.asset_links ?? 0);
      toast.success(
        total > 0
          ? `已删除「${character.name}」并清除 ${total} 处映射`
          : `已删除「${character.name}」`,
      );
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 上传本地图片作为资产图：有展示形象 → 对该形象节点追加 uploaded 版本
  // （已设生效时把新版本设为生效，否则生效指针仍指旧图，上传图不展示）；
  // 无形象记录 → 走专用端点新建 basic 形象再挂图
  const uploadImage = async (file: File) => {
    if (!projectId) return;
    try {
      if (img) {
        const version = await api.uploadVersion(projectId, `char_${character.id}_${img.id}`, file);
        if (img.preferred_version_id) {
          await preferCharacterImage(character.id, img.id, version.id);
        } else {
          await loadCharacters();
        }
        await reloadVersions();
      } else {
        await api.uploadCharacterImage(character.id, file);
        await loadCharacters();
      }
      toast.success("已上传资产图");
    } catch (e) {
      toast.error(`上传失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 妆造形象：名称可留空（后端自动命名），外观描述必填
  const submitCostume = async () => {
    if (!cdesc.trim()) return;
    try {
      await generateCharacterImage(character.id, {
        kind: "costume",
        name: cname.trim() || undefined,
        appearance_desc: cdesc.trim(),
        model_id: modelId,
      });
      setCostumeOpen(false);
      setCname("");
      setCdesc("");
    } catch (e) {
      toast.error(`妆造形象生成失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 妆造→分镜批量映射（6.4 第 2 条）：不勾选分集 = 全部引用该角色的分集
  const openMap = (imageId: string) => {
    setMapId(imageId === mapId ? null : imageId);
    setPickedEps([]);
    setMapMsg("");
  };

  // 妆造建议（AI 从剧本提取的换装事件）：接受一键生成（描述可微调）、拒绝
  const mySuggestions = costumeSuggestions.filter(
    (s) => s.character === character.name,
  );
  const [editSugId, setEditSugId] = useState<string | null>(null);
  const [sugDesc, setSugDesc] = useState("");
  /** AI 优化妆造建议草稿（批次 3）：草稿尚无形象记录，故传角色 id + 草稿文本 */
  const [optSugId, setOptSugId] = useState<string | null>(null);
  const acceptSuggestion = async (s: CostumeSuggestionInfo, desc?: string) => {
    try {
      await acceptCostumeSuggestion(s, { appearance_desc: desc, model_id: modelId });
      setEditSugId(null);
      toast.success("妆造形象生成中");
    } catch (e) {
      toast.error(`生成失败：${e instanceof Error ? e.message : e}`);
    }
  };
  const rejectSuggestion = (s: CostumeSuggestionInfo) => {
    rejectCostumeSuggestion(s).catch((e) =>
      toast.error(`操作失败：${e instanceof Error ? e.message : e}`),
    );
  };

  // 删除按钮（两段式，2026-09-30 修用户报障）：
  // **点选过某个版本缩略图**时只删那一个版本（其余版本保留）——此前无论选没选，
  // 这个按钮都会把整个形象连同它的**全部版本**删掉，用户「选了个缩略图点删除，
  // 所有版本都没了」遇到的就是它；
  // **没点选版本**时才是「删除整个形象」（记录 + 全部版本 + 妆造映射清理、
  // 进行中任务取消），弹窗文案把两种目标都写明白。
  const removeImageRecord = async (target: CharacterImageInfo) => {
    const shownVersion = pickedVersion;
    if (shownVersion) {
      const label = shownVersion.name || versionDefaultName(shownVersion);
      const keepNote = versions.length > 1
        ? `其余 ${versions.length - 1} 个版本将保留。`
        : "这是该形象的最后一个版本，删除后形象将没有可用的图。";
      const ok = await confirmDialog({
        title: "删除所选版本",
        message: `确定删除「${target.name || "未命名形象"}」的 ${label} 吗？`
          + `${keepNote}图片文件保留在资产库可找回。`,
        confirmText: "删除",
        danger: true,
      });
      if (!ok) return;
      try {
        if (!projectId || !imageNodeId) throw new Error("项目未初始化");
        await api.deleteNodeVersion(projectId, imageNodeId, shownVersion.id);
        // 本地先移除被删版本（删非最新版本时不会触发重拉路径），再以服务端为准
        setVersions((prev) => prev.filter((v) => v.id !== shownVersion.id));
        setPickedVersionId(null);
        void reloadVersions();
        // 删的是生效版本时后端会清 preferred 指针，拉一次角色列表让徽标跟随
        void loadCharacters();
        toast.success("版本已删除");
      } catch (e) {
        toast.error(`删除失败：${e instanceof Error ? e.message : e}`);
      }
      return;
    }
    const ok = await confirmDialog({
      title: "删除整个形象",
      message: `确定删除「${target.name || "未命名形象"}」整个形象吗？`
        + `该形象的 ${versions.length || "全部"} 个版本会一起删除，`
        + "进行中的生成任务将取消，引用该形象的妆造映射会同步清除，"
        + "图片文件保留在资产库可找回。",
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await deleteCharacterImage(character.id, target.id);
      toast.success("形象已删除");
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : e}`);
    }
  };

  const toggleEp = (episodeId: string) => {
    setPickedEps((prev) =>
      prev.includes(episodeId) ? prev.filter((id) => id !== episodeId) : [...prev, episodeId],
    );
  };

  const runMap = async () => {
    if (!mapId) return;
    try {
      const res = await applyCostume(character.id, mapId, pickedEps);
      setMapMsg(
        `已映射 ${res.updated} 个分镜` +
          (res.episodes.length > 0 ? `（适用第 ${res.episodes.join("、")} 集）` : ""),
      );
      setMapId(null);
    } catch (e) {
      setMapMsg(e instanceof Error ? e.message : "映射失败");
    }
  };

  return (
    <div className={`asset-card${busy ? " generating" : ""}`}>
      <div className={`asset-thumb ${img ? "" : "blank"}`}>
        {shownArtifact ? (
          <img
            src={fileUrl(shownArtifact)}
            alt={img?.name || character.name}
            title="点击放大预览"
            className="clickable"
            onClick={() => setPreviewUrl(shownArtifact)}
          />
        ) : (
          <span>{busy ? "角色图生成中" : "暂无角色图"}</span>
        )}
        {busy && (
          <>
            <span className="gen-badge">{progressLabel(progress)}</span>
            <ThumbMarquee progress={progress} />
          </>
        )}
        {/* 右上角垂直操作组（映射 → 上传 → 删除；进度徽标已移左上不冲突） */}
        <div className="thumb-actions">
          {/* 当前展示形象 → 分镜映射入口 */}
          {img && (
            <button
              className={`thumb-map${mapId === img.id ? " active" : ""}`}
              title="把该形象批量映射到分镜"
              onClick={() => openMap(img.id)}
            >
              映射
            </button>
          )}
          {/* 上传本地图片（生成中隐藏）：有形象追加版本，无形象新建基础形象 */}
          {!busy && (
            <label className="thumb-upload" title="上传本地图片作为资产图">
              <input
                type="file"
                accept="image/png,image/jpeg,image/webp"
                hidden
                onChange={(e) => {
                  const picked = e.target.files?.[0];
                  e.target.value = ""; // 清空以便可重复选择同一文件
                  if (picked) void uploadImage(picked);
                }}
              />
              上传
            </label>
          )}
          {/* 删除（生成中隐藏）：点选过版本 → 只删那个版本；没点选 → 删整个形象 */}
          {img && !busy && (
            <button
              className="thumb-del"
              title={pickedVersion
                ? "删除选中的版本（其他版本保留）"
                : "删除该形象（含它的全部版本）"}
              onClick={() => void removeImageRecord(img)}
            >
              删除
            </button>
          )}
        </div>
      </div>
      {/* 形象类型页签：切换基础图/四视图/妆造图（只显示已存在的类型） */}
      {kinds.length > 0 && (
        <div className="preview-tabs">
          {kinds.map((k) => (
            <button
              key={k}
              className={`preview-tab${k === activeKind ? " active" : ""}`}
              onClick={() => {
                setTabKind(k);
                setPickedId(null);
              }}
            >
              {KIND_LABEL[k] ?? k}
            </button>
          ))}
        </div>
      )}
      {/* 形象抽卡版本条（有版本即显示，与场景/道具卡同款交互）：点选切换大图，
          生效版本带金色徽标；选中非生效版本时出现「设为生效」，可改名 */}
      {versions.length > 0 && (
        <VersionStrip
          versions={versions}
          shownArtifact={shownArtifact}
          effectiveVersionId={effectiveVersionId}
          pickedVersion={pickedVersion}
          onPick={setPickedVersionId}
          onPrefer={() => void setPreferred()}
          onRename={(v) => void renameVersion(v)}
          defaultName={versionDefaultName}
        />
      )}
      <div className="asset-name">
        <AssetNameText name={character.name} onRename={renameCharacter} />
        {/* 角色定位徽标（LLM 按剧本定义）：点击在主角/配角间切换，切换后列表重排 */}
        <button
          className={`role-tag ${character.role === "主角" ? "lead" : ""}`}
          title="点击切换主角/配角"
          onClick={() =>
            updateCharacter(character.id, {
              role: character.role === "主角" ? "配角" : "主角",
            }).catch((e) =>
              toast.error(`切换失败：${e instanceof Error ? e.message : e}`),
            )
          }
        >
          {character.role === "主角" ? "主角" : "配角"}
        </button>
        {img && <span className="freq-tag">{img.name}</span>}
        {img?.episodes && img.episodes.length > 0 && (
          <span className="eps-tag">第{img.episodes.join("/")}集</span>
        )}
      </div>
      {character.persona && (
        <div className="asset-field">
          <span className="asset-label">人设</span>
          <span className="asset-text">{character.persona}</span>
        </div>
      )}
      {/* 外观摘要（LLM 提取）：紧跟人设展示 */}
      {character.appearance && (
        <div className="asset-field">
          <span className="asset-label">外观</span>
          <span className="asset-text">{character.appearance}</span>
        </div>
      )}
      {/* 妆造建议（AI 从剧本提取的换装事件）：接受一键生成 / 微调描述 / 拒绝 */}
      {mySuggestions.length > 0 && (
        <div className="costume-suggestions">
          <span className="asset-label">妆造建议（AI 从剧本提取）</span>
          {mySuggestions.map((s) => (
            <div key={s.id} className="costume-suggestion">
              <div className="cs-head">
                <span className="cs-name">{s.name}</span>
                <span className="cs-eps">第{s.episodes.join("/")}集</span>
              </div>
              {editSugId === s.id ? (
                <textarea
                  className="cs-edit"
                  value={sugDesc}
                  onChange={(e) => setSugDesc(e.target.value)}
                />
              ) : (
                <span className="asset-text">{s.appearance_desc}</span>
              )}
              <div className="cs-evidence">依据：{s.evidence}</div>
              <div className="asset-footer">
                {editSugId === s.id ? (
                  <>
                    <button
                      className="gen-btn"
                      onClick={() => void acceptSuggestion(s, sugDesc)}
                    >
                      用此描述生成
                    </button>
                    <button
                      className="ghost-btn"
                      disabled={!sugDesc.trim()}
                      title="说清修改需求，由 AI 结合角色人设与基础定妆改写妆造描述"
                      onClick={() => setOptSugId(s.id)}
                    >
                      AI 优化
                    </button>
                    <button className="ghost-btn" onClick={() => setEditSugId(null)}>
                      取消
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      className="gen-btn"
                      disabled={busy}
                      onClick={() => void acceptSuggestion(s)}
                      title={acceptCostTip || undefined}
                    >
                      接受并生成
                    </button>
                    <button
                      className="ghost-btn"
                      onClick={() => {
                        setEditSugId(s.id);
                        setSugDesc(s.appearance_desc);
                      }}
                    >
                      微调描述
                    </button>
                    <button className="ghost-btn" onClick={() => rejectSuggestion(s)}>
                      拒绝
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      {/* AI 优化妆造描述草稿：应用后写回「微调描述」输入框（用户在弹窗里点应用才生效） */}
      {optSugId && projectId && (
        <PromptOptimizeDialog
          projectId={projectId}
          target={{
            target: "costume",
            id: character.id,
            label: `${character.name} · 妆造描述`,
            currentPrompt: sugDesc,
          }}
          onApply={(text) => {
            setSugDesc(text);
            setOptSugId(null);
            toast.success("已应用 AI 优化后的妆造描述");
          }}
          onClose={() => setOptSugId(null)}
        />
      )}
      {/* 生图提示词：LLM 基于剧本大背景生成，生成定妆图时优先生效 */}
      {promptEdit ? (
        <div className="asset-field edit-desc">
          <span className="asset-label">生图提示词</span>
          <textarea
            value={imagePrompt}
            placeholder="定妆图提示词（外貌/服装/气质/道具，AI 已按剧本预生成）"
            onChange={(e) => setImagePrompt(e.target.value)}
          />
          <div className="asset-footer">
            <button className="gen-btn" onClick={() => void savePrompt()}>
              保存
            </button>
            <button
              className="ghost-btn"
              onClick={() => {
                setPromptEdit(false);
                setImagePrompt(character.image_prompt);
              }}
            >
              取消
            </button>
          </div>
        </div>
      ) : (
        <>
        <div className="asset-field">
          <span className="asset-label">生图提示词</span>
          <span className="asset-text">
            {character.image_prompt || "（AI 解析后自动生成）"}
          </span>
          {/* 编辑 / AI 优化 并排一行（.asset-field 是 column，直接放会各占一行） */}
          <div className="asset-field-actions">
            <button className="vp-edit-btn" onClick={() => setPromptEdit(true)}>
              编辑
            </button>
            {character.image_prompt && projectId && (
              <button
                className="vp-edit-btn"
                title="说清修改需求，由 AI 结合剧本上下文改写提示词（可编辑后再应用）"
                onClick={() => setOptOpen(true)}
              >
                AI 优化
              </button>
            )}
          </div>
        </div>
        {optOpen && projectId && (
          <PromptOptimizeDialog
            projectId={projectId}
            target={{
              target: "character",
              id: character.id,
              label: "角色定妆图提示词",
              currentPrompt: character.image_prompt,
            }}
            onApply={async (text) => {
              setImagePrompt(text);
              await updateCharacter(character.id, { image_prompt: text });
              setOptOpen(false);
              toast.success("已应用 AI 优化后的提示词");
            }}
            onClose={() => setOptOpen(false)}
          />
        )}
        </>
      )}
      {/* 音色：LLM 按人设自动生成 + 可上传参考音频（生成视频时作配音音色参考） */}
      <div className="asset-field">
        <span className="asset-label">音色</span>
        <span className="asset-text">
          {character.voice_desc || "（AI 解析后自动生成）"}
        </span>
        {character.voice_ref_asset_id ? (
          <div className="voice-ref">
            <audio
              controls
              src={fileUrl(character.voice_ref_asset_id)}
              className="voice-audio"
            />
            <button
              className="ghost-btn voice-remove"
              disabled={busy}
              title="移除已上传的音色参考音频"
              onClick={() =>
                deleteVoiceRef(character.id).catch((e) =>
                  toast.error(`移除失败：${e instanceof Error ? e.message : e}`),
                )
              }
            >
              移除参考音频
            </button>
          </div>
        ) : (
          <label className="voice-upload">
            <input
              type="file"
              accept="audio/*"
              hidden
              onChange={(e) => {
                const picked = e.target.files?.[0];
                e.target.value = ""; // 清空以便可重复选择同一文件
                if (picked) {
                  uploadVoiceRef(character.id, picked).catch((err) =>
                    toast.error(`上传失败：${err instanceof Error ? err.message : err}`),
                  );
                }
              }}
            />
            上传参考音频
          </label>
        )}
      </div>
      {mapId && (
        <div className="costume-map-panel">
          <span className="asset-label">映射到分集（不勾选 = 全部）</span>
          <div className="map-eps">
            {episodes.map((ep) => (
              <label key={ep.id} className="map-ep">
                <input
                  type="checkbox"
                  checked={pickedEps.includes(ep.id)}
                  onChange={() => toggleEp(ep.id)}
                />
                第{ep.seq}集
              </label>
            ))}
            {episodes.length === 0 && <span className="map-empty">暂无分集（先解析剧本）</span>}
          </div>
          <div className="asset-footer">
            <button className="gen-btn" disabled={episodes.length === 0} onClick={runMap}>
              应用映射
            </button>
            <button className="ghost-btn" onClick={() => setMapId(null)}>
              取消
            </button>
          </div>
          {mapMsg && <div className="map-msg">{mapMsg}</div>}
        </div>
      )}
      {mapMsg && !mapId && <div className="map-msg">{mapMsg}</div>}
      {costumeOpen ? (
        <div className="asset-field costume-form">
          <span className="asset-label">形象名称</span>
          <input
            value={cname}
            onChange={(e) => setCname(e.target.value)}
            placeholder="如：当家主母造型（留空自动命名）"
          />
          <span className="asset-label">外观描述</span>
          <textarea
            value={cdesc}
            onChange={(e) => setCdesc(e.target.value)}
            placeholder="服装/发型/妆面等外观描述"
          />
          <div className="asset-footer">
            <button className="gen-btn" disabled={busy || !cdesc.trim()} onClick={submitCostume}>
              生成
            </button>
            <button
              className="ghost-btn"
              disabled={busy}
              onClick={() => {
                setCostumeOpen(false);
                setCname("");
                setCdesc("");
              }}
            >
              取消
            </button>
          </div>
        </div>
      ) : (
        <div className="asset-footer">
          {busy ? (
            <button
              className="ghost-btn"
              onClick={() =>
                cancelCharacterImage(character.id).catch((e) =>
                  toast.error(`取消失败：${e instanceof Error ? e.message : e}`),
                )
              }
            >
              取消生成
            </button>
          ) : (
            <>
              <ModelSelect models={models} value={modelId} onChange={(v) => { userPicked.current = true; setModelId(v); }} label="生图模型" emptyLabel={emptyLabel} />
              {/* 角色形象的**唯一入口**（2026-09-30 用户定稿）：一次出一张
                  **角色设定图**（左 1/3 正脸特写 + 右 2/3 正面/侧面/背面全身），
                  正面全身那格自动裁成基础形象（定妆图），正脸特写作身份大头照，
                  侧面/背面供背对/侧对镜头用——不用先出定妆图再补视角 */}
              <button className="gen-btn" onClick={genSheet}
                      title={fourViewCostTip || undefined}>
                {hasFourViewSheet ? "重新生成" : "生成设定图"}
              </button>
            </>
          )}
          <button className="ghost-btn" disabled={busy} onClick={() => setCostumeOpen(true)}>
            妆造形象
          </button>
          <button
            className="ghost-btn asset-del"
            disabled={busy}
            title="删除该角色并清除分镜引用/妆造映射（图片产物保留在资产库）"
            onClick={() => void removeAsset()}
          >
            删除资产
          </button>
        </div>
      )}
      {previewUrl && (
        <ImageLightbox artifactId={previewUrl} onClose={() => setPreviewUrl(null)} />
      )}
    </div>
  );
}

function NewCharacterCard() {
  const createCharacter = useDrama((s) => s.createCharacter);
  const loadCharacters = useDrama((s) => s.loadCharacters);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [persona, setPersona] = useState("");
  const [appearance, setAppearance] = useState("");
  const [voice, setVoice] = useState("");
  // 可选本地图片：角色创建成功后作为基础形象上传
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const created = await createCharacter({
        name: name.trim(),
        persona: persona.trim() || undefined,
        appearance: appearance.trim() || undefined,
        voice_desc: voice.trim() || undefined,
      });
      // 图片上传失败不回滚角色（资产已创建），仅提示错误
      if (created && imageFile) {
        try {
          await api.uploadCharacterImage(created.id, imageFile);
          await loadCharacters();
        } catch (e) {
          toast.error(`角色已创建，但图片上传失败：${e instanceof Error ? e.message : e}`);
        }
      }
      setOpen(false);
      setName("");
      setPersona("");
      setAppearance("");
      setVoice("");
      setImageFile(null);
    } catch (e) {
      toast.error(`新建角色失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button className="asset-card add-card" onClick={() => setOpen(true)}>
        + 新建角色
      </button>
    );
  }
  return (
    <div className="asset-card form-card">
      <div className="asset-name">新建角色</div>
      <div className="asset-field">
        <span className="asset-label">名称</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="角色名称" />
      </div>
      <div className="asset-field">
        <span className="asset-label">人设</span>
        <textarea value={persona} onChange={(e) => setPersona(e.target.value)} placeholder="性格与背景" />
      </div>
      <div className="asset-field">
        <span className="asset-label">外观</span>
        <textarea
          value={appearance}
          onChange={(e) => setAppearance(e.target.value)}
          placeholder="用于生成定妆图的外观描述"
        />
      </div>
      <div className="asset-field">
        <span className="asset-label">音色</span>
        <input value={voice} onChange={(e) => setVoice(e.target.value)} placeholder="音色描述" />
      </div>
      <div className="asset-field">
        <span className="asset-label">图片（可选）</span>
        <label className="file-upload">
          <input
            type="file"
            accept="image/png,image/jpeg,image/webp"
            hidden
            onChange={(e) => {
              setImageFile(e.target.files?.[0] ?? null);
              e.target.value = ""; // 清空以便可重复选择同一文件
            }}
          />
          {imageFile ? imageFile.name : "选择本地图片"}
        </label>
      </div>
      <div className="asset-footer">
        <button className="gen-btn" disabled={busy || !name.trim()} onClick={submit}>
          确定
        </button>
        <button className="ghost-btn" disabled={busy} onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </div>
  );
}

export function SceneCard({ scene, models, emptyLabel }: { scene: SceneInfo; models: ModelInfo[]; emptyLabel?: string }) {
  const generateScene = useDrama((s) => s.generateScene);
  const generateSceneExtraView = useDrama((s) => s.generateSceneExtraView);
  const cancelScene = useDrama((s) => s.cancelScene);
  const deleteSceneImage = useDrama((s) => s.deleteSceneImage);
  const deleteSceneImageVersion = useDrama((s) => s.deleteSceneImageVersion);
  const preferSceneImage = useDrama((s) => s.preferSceneImage);
  const updateScene = useDrama((s) => s.updateScene);
  const deleteScene = useDrama((s) => s.deleteScene);
  const renameNodeVersion = useDrama((s) => s.renameNodeVersion);
  const projectId = useDrama((s) => s.projectId);
  const loadScenes = useDrama((s) => s.loadScenes);
  // 花钱按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const extraViewCostTip = useCostTip(BILLING_KEYS.sceneExtraView);
  const sceneGenCostTip = useCostTip(BILLING_KEYS.sceneGenerate);
  const [editing, setEditing] = useState(false);
  const [desc, setDesc] = useState(scene.description);
  const busy = scene.generating;
  const progress = useDrama((s) => s.genProgress[`scene_${scene.id}`]);
  // 生成终态信号（busy true→false）：版本条与视角缩略图都靠它重拉
  const busyKey = !!progress;
  // 生图模型与生图提示词编辑（LLM 生成，可手动改）
  const [modelId, setModelId] = useState("");
  // 项目预设对齐（批次 4 补充）：项目 t2i 预设到达/变化时同步默认选择
  const presetT2i = useDrama((s) => s.modelPresets?.t2i ?? "");
  const userPicked = useRef(false);
  useEffect(() => {
    if (presetT2i && !userPicked.current) setModelId(presetT2i);
  }, [presetT2i]);
  const [promptEdit, setPromptEdit] = useState(false);
  /** AI 优化提示词弹窗（批次 3）：优化结果经「应用」后写回 */
  const [optOpen, setOptOpen] = useState(false);
  /** 场景组弹窗（32 号） */
  const [groupOpen, setGroupOpen] = useState(false);
  const [imagePrompt, setImagePrompt] = useState(scene.image_prompt);
  /** 场景补充视角（批次 4 · B4）：view → 产物 artifact_id（独立节点，
   *  不覆盖主图），反打镜头与关键动作镜的空间一致性锚点 */
  const [viewArts, setViewArts] = useState<Record<string, string>>({});
  // 生成状态作为刷新信号（同 reloadVersions）：视角任务是**独立节点**，完成
  // 后 `scene.artifact_id`（主图）不变 —— 只依赖 artifact_id 会导致缩略图
  // **永不出现**（实测缺陷：点了补反向视角，任务跑完图还是不出来）
  const reloadViews = useCallback(() => {
    if (!projectId) return;
    // 全量重建映射（而非只补新值）：重生成视角后旧产物失效，只补不删会留旧图
    Promise.all(EXTRA_VIEWS.map((v) =>
      api.listVersions(projectId, `scene_${scene.id}_view_${v.key}`)
        .then((vs) => [v.key, vs.find((x) => x.artifact_id)?.artifact_id ?? ""])
        .catch(() => [v.key, ""]),
    )).then((pairs) => setViewArts(Object.fromEntries(pairs)));
  }, [projectId, scene.id, busyKey]);
  // 主图换了（视角图可能对不上）或生成终态（busyKey 翻转）时重拉视角缩略图
  useEffect(() => reloadViews(), [reloadViews, scene.artifact_id]);
  useEffect(() => setImagePrompt(scene.image_prompt), [scene.image_prompt]);

  // 列表被任务事件刷新后同步编辑框
  useEffect(() => setDesc(scene.description), [scene.description]);

  // 一次抽几张（默认 1 = 与改造前一致，不多花钱）：场景图是「再抽一次」最频繁的
  // 资产，抽 N 张一次挑比反复点生成省往返；上限与后端 T2I_MAX_CANDIDATES 同源
  const [count, setCount] = useState<number>(DEFAULT_CANDIDATE_COUNT);

  // 场景图多版本（引擎对 scene_{id} 节点追加抽卡版本，与角色形象同语义）：
  // 按 seq 倒序展示版本条，点选切换大图
  const [versions, setVersions] = useState<NodeVersionInfo[]>([]);
  const [pickedId, setPickedId] = useState<string | null>(null);
  // 生成状态作为版本条刷新信号：任务终态（busy true→false）时重拉版本列表
  //（仅依赖 artifact_id 会漏掉「已设生效 + 再抽卡」场景——生效版不变时列表不刷新）
  const reloadVersions = useCallback(() => {
    if (!projectId || !scene.artifact_id) {
      setVersions([]);
      setPickedId(null);
      return Promise.resolve();
    }
    return api
      .listVersions(projectId, `scene_${scene.id}`)
      .then((list) => {
        const withArtifact = list
          .filter((v) => v.artifact_id)
          .sort((a, b) => b.seq - a.seq);
        setVersions(withArtifact);
        setPickedId(null); // 刷新后回到最新版本
      })
      .catch(() => {});
  }, [projectId, scene.id, scene.artifact_id, busyKey]);
  useEffect(() => {
    reloadVersions();
  }, [reloadVersions]);
  // 当前展示产物：手动点选的版本 ?? 最新版本（scene.artifact_id 由后端按
  // 「生效版本优先，否则最新」解析）
  const shownArtifact =
    versions.find((v) => v.id === pickedId)?.artifact_id ??
    scene.artifact_id ??
    null;
  // 生效版本：用户「设为生效」的版本，未设置时为最新版本
  const effectiveVersionId =
    scene.preferred_version_id || versions[0]?.id || null;
  const pickedVersion = versions.find((v) => v.id === pickedId) ?? null;
  // 图片放大预览（灯箱）
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  // 把预览选中的版本设为生效主图（分镜参考图与卡片展示随之切换）
  const setPreferred = async () => {
    if (!pickedVersion) return;
    try {
      await preferSceneImage(scene.id, pickedVersion.id);
      toast.success(`已设为生效版本（${pickedVersion.name || `版本 ${pickedVersion.seq}`}）`);
    } catch (e) {
      toast.error(`设置失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 版本重命名（输入框弹窗；留空恢复默认命名「版本N」）
  const renameVersion = async (v: NodeVersionInfo) => {
    const name = await promptDialog({
      title: "版本重命名",
      message: `为「${v.name || `版本 ${v.seq}`}」设置显示名称（留空恢复默认命名）`,
      initial: v.name,
      confirmText: "保存",
    });
    if (name === null) return;
    try {
      await renameNodeVersion(`scene_${scene.id}`, v.id, name.trim());
      void reloadVersions();
      toast.success("版本已命名");
    } catch (e) {
      toast.error(`命名失败：${e instanceof Error ? e.message : e}`);
    }
  };

  const gen = () => {
    generateScene(scene.id, modelId, count).catch((e) =>
      toast.error(`生成任务提交失败：${e instanceof Error ? e.message : e}`),
    );
  };

  // 保存生图提示词（生成场景图时优先生效）
  const savePrompt = async () => {
    try {
      await updateScene(scene.id, { image_prompt: imagePrompt });
      setPromptEdit(false);
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : e}`);
    }
  };

  const save = async () => {
    try {
      await updateScene(scene.id, { description: desc });
      setEditing(false);
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 双击快速改名（分镜引用按 id 关联，改名不影响已绑定的引用）
  const renameScene = async (name: string) => {
    try {
      await updateScene(scene.id, { name });
      toast.success("已重命名");
    } catch (e) {
      toast.error(`重命名失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 删除资产（两段式）：先预检引用情况弹确认框，确认后删除场景并清空
  // 引用分镜的 scene_ref_id（图片产物保留在资产库可找回）
  const removeAsset = async () => {
    try {
      const pre = await deleteScene(scene.id, false);
      const ok = await confirmDialog({
        title: "删除场景",
        message: `确定删除「${scene.name}」吗？${refsSummary(
          pre.references ?? { shots: 0, episodes: [] },
        )}图片产物保留在资产库可找回。`,
        confirmText: "删除",
        danger: true,
      });
      if (!ok) return;
      const res = await deleteScene(scene.id, true);
      const total = res.cleared?.shots ?? 0;
      toast.success(
        total > 0
          ? `已删除「${scene.name}」并清除 ${total} 处引用`
          : `已删除「${scene.name}」`,
      );
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 上传本地图片作为场景图：对 scene_{id} 节点追加 uploaded 版本；
  // 已设生效的版本存在时把新版本设为生效（否则生效指针仍指旧图不展示）
  const uploadImage = async (file: File) => {
    if (!projectId) return;
    try {
      const version = await api.uploadVersion(projectId, `scene_${scene.id}`, file);
      if (scene.preferred_version_id) {
        await preferSceneImage(scene.id, version.id);
      } else {
        await loadScenes();
      }
      await reloadVersions();
      toast.success("已上传场景图");
    } catch (e) {
      toast.error(`上传失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 删除当前展示的场景图版本（版本级删除，与角色卡删除选中形象同语义；
  // 无版本信息时兜底全删。产物保留资产库可找回）
  const removeSceneImage = async () => {
    const shownVersion = versions.find((v) => v.artifact_id === shownArtifact);
    const versionLabel = shownVersion
      ? (shownVersion.name || `版本 ${shownVersion.seq}`)
      : "当前场景图";
    const keepNote =
      versions.length > 1
        ? `其余 ${versions.length - 1} 个版本将保留。`
        : "这是唯一版本，删除后场景将回到未生成态。";
    const ok = await confirmDialog({
      title: "删除场景图",
      message: `确定删除「${scene.name}」的 ${versionLabel} 吗？${keepNote}图片文件保留在资产库可找回。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      if (shownVersion) {
        await deleteSceneImageVersion(scene.id, shownVersion.id);
        // 本地先移除被删版本（删非最新版本时 loadScenes 不会改变 artifact_id，
        // useEffect 不触发重拉——本地过滤保证列表即时正确，再以服务端为准重拉）
        setVersions((prev) => prev.filter((v) => v.id !== shownVersion.id));
        void reloadVersions();
      } else {
        await deleteSceneImage(scene.id);
        void reloadVersions();
      }
      setPickedId(null);
      toast.success("场景图已删除");
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <div className={`asset-card${busy ? " generating" : ""}`}>
      <div className={`asset-thumb ${shownArtifact ? "" : "blank"}`}>
        {shownArtifact ? (
          <img
            src={fileUrl(shownArtifact)}
            alt={scene.name}
            title="点击放大预览"
            className="clickable"
            onClick={() => setPreviewUrl(shownArtifact)}
          />
        ) : (
          <span>{busy ? "场景图生成中" : "暂无场景图"}</span>
        )}
        {busy && (
          <>
            <span className="gen-badge">{progressLabel(progress)}</span>
            <ThumbMarquee progress={progress} />
          </>
        )}
        {/* 右上角垂直操作组（上传 → 删除；进度徽标已移左上不冲突） */}
        <div className="thumb-actions">
          {/* 上传本地图片（生成中隐藏）：追加为该场景的 uploaded 版本 */}
          {!busy && (
            <label className="thumb-upload" title="上传本地图片作为场景图">
              <input
                type="file"
                accept="image/png,image/jpeg,image/webp"
                hidden
                onChange={(e) => {
                  const picked = e.target.files?.[0];
                  e.target.value = ""; // 清空以便可重复选择同一文件
                  if (picked) void uploadImage(picked);
                }}
              />
              上传
            </label>
          )}
          {/* 删除场景主图（生成中隐藏） */}
          {shownArtifact && !busy && (
            <button className="thumb-del" title="删除场景图" onClick={() => void removeSceneImage()}>
              删除
            </button>
          )}
        </div>
      </div>
      {/* 版本条（有版本即显示，单版本也可命名）：点选切换大图，
          生效版本带金色徽标；选中非生效版本时出现「设为生效」 */}
      {versions.length > 0 && (
        <VersionStrip
          versions={versions}
          shownArtifact={shownArtifact}
          effectiveVersionId={effectiveVersionId}
          pickedVersion={pickedVersion}
          onPick={setPickedId}
          onPrefer={() => void setPreferred()}
          onRename={(v) => void renameVersion(v)}
          defaultName={(v) => `版本 ${v.seq}`}
        />
      )}
      <div className="asset-name">
        <AssetNameText name={scene.name} onRename={renameScene} />
        {typeof scene.freq === "number" && <span className="freq-tag">出现 {scene.freq} 次</span>}
      </div>
      {editing ? (
        <div className="asset-field edit-desc">
          <span className="asset-label">描述</span>
          <textarea value={desc} onChange={(e) => setDesc(e.target.value)} />
          <div className="asset-footer">
            <button className="gen-btn" onClick={save}>
              保存
            </button>
            <button
              className="ghost-btn"
              onClick={() => {
                setEditing(false);
                setDesc(scene.description);
              }}
            >
              取消
            </button>
          </div>
        </div>
      ) : (
        <div className="asset-field">
          <span className="asset-label">描述</span>
          <span className="asset-text">{scene.description || "（未填写）"}</span>
          <button className="vp-edit-btn" onClick={() => setEditing(true)}>
            编辑
          </button>
        </div>
      )}
      {/* 生图提示词：LLM 基于剧本世界观生成，生成场景图时优先生效 */}
      {promptEdit ? (
        <div className="asset-field edit-desc">
          <span className="asset-label">生图提示词</span>
          <textarea
            value={imagePrompt}
            placeholder="场景主图提示词（环境/光线/氛围/构图，AI 已按剧本预生成）"
            onChange={(e) => setImagePrompt(e.target.value)}
          />
          <div className="asset-footer">
            <button className="gen-btn" onClick={() => void savePrompt()}>
              保存
            </button>
            <button
              className="ghost-btn"
              onClick={() => {
                setPromptEdit(false);
                setImagePrompt(scene.image_prompt);
              }}
            >
              取消
            </button>
          </div>
        </div>
      ) : (
        <>
        <div className="asset-field">
          <span className="asset-label">生图提示词</span>
          <span className="asset-text">
            {scene.image_prompt || "（AI 解析后自动生成）"}
          </span>
          {/* 编辑 / AI 优化 并排一行（.asset-field 是 column，直接放会各占一行） */}
          <div className="asset-field-actions">
            <button className="vp-edit-btn" onClick={() => setPromptEdit(true)}>
              编辑
            </button>
            {scene.image_prompt && projectId && (
              <button
                className="vp-edit-btn"
                title="说清修改需求，由 AI 结合剧本上下文改写提示词（可编辑后再应用）"
                onClick={() => setOptOpen(true)}
              >
                AI 优化
              </button>
            )}
          </div>
        </div>
        {/* 场景组（32 号）：同一真实空间（一套房/一家店）的场景互作环境参考 */}
        <div className="asset-field">
          <span className="asset-label">场景组</span>
          <span className="asset-text">
            {scene.group
              ? `${scene.group.name}（同组环境保持一致）`
              : "未分组（独立生成）"}
          </span>
          <div className="asset-field-actions">
            <button
              className="vp-edit-btn"
              title="把同一真实空间（一套房的不同房间 / 一家店的店内与门口）的场景分到一组，环境保持一致"
              onClick={() => setGroupOpen(true)}
            >
              {scene.group ? "设置" : "分组"}
            </button>
          </div>
        </div>
        {groupOpen && (
          <SceneGroupDialog scene={scene} onClose={() => setGroupOpen(false)} />
        )}
        {optOpen && projectId && (
          <PromptOptimizeDialog
            projectId={projectId}
            target={{
              target: "scene",
              id: scene.id,
              label: "场景主图提示词",
              currentPrompt: scene.image_prompt,
            }}
            onApply={async (text) => {
              setImagePrompt(text);
              await updateScene(scene.id, { image_prompt: text });
              setOptOpen(false);
              toast.success("已应用 AI 优化后的提示词");
            }}
            onClose={() => setOptOpen(false)}
          />
        )}
        </>
      )}
      <div className="asset-footer">
        {busy ? (
          <button
            className="ghost-btn"
            onClick={() =>
              cancelScene(scene.id).catch((e) =>
                toast.error(`取消失败：${e instanceof Error ? e.message : e}`),
              )
            }
          >
            取消生成
          </button>
        ) : (
          <>
            {/* 补充视角（批次 4 · B4）：以场景主图为 I2I 参考补全反向/交互区
                视角，供反打镜与关键动作镜作空间一致性锚点。**独立成块、排在
                生成设置之前**（资产内容在上、生成设置在下），且已移出 gen-btn
                内部——此前按钮套按钮，点它会连带重生成主图。 */}
            <div className="asset-extra-views">
              <span className="asset-label">补充视角</span>
              <div className="extra-view-grid">
                {EXTRA_VIEWS.map((v) => {
                  const art = viewArts[v.key];
                  const ready = Boolean(scene.artifact_id);
                  return (
                    <div className="extra-view-item" key={v.key}>
                      {art ? (
                        <img
                          className="extra-view-thumb"
                          src={fileUrl(art)}
                          alt={v.label}
                          title={`点击预览${v.label}`}
                          // 灯箱收 **artifact_id**（内部自己拼文件地址，见 ImageLightbox）。
                          // 这里原先把 fileUrl() 的结果又传进去，地址被套了两层
                          // （/api/artifacts//api/artifacts/<id>/file/file）→ 404 → 图裂
                          onClick={() => setPreviewUrl(art)}
                        />
                      ) : (
                        <div
                          className="extra-view-thumb empty"
                          title={ready ? "尚未生成" : "请先生成场景主图"}
                        >
                          未生成
                        </div>
                      )}
                      <button
                        className="vp-edit-btn"
                        disabled={busy || !ready}
                        title={ready
                          ? `以场景主图为参考生成${v.label}`
                            + (extraViewCostTip ? `；${extraViewCostTip}` : "")
                          : "请先生成场景主图——补充视角以主图为参考"}
                        onClick={() => {
                          // **必须走 store**（与 generateScene 一致）：组件内直调
                          // API 会绕开乐观标记与终态刷新 → 没有生成中反馈、
                          // 完成后缩略图也不出现（实测缺陷）
                          generateSceneExtraView(
                            scene.id, v.key, modelId || undefined)
                            .then(() => toast.success(`${v.label}生成中`))
                            .catch((e) => toast.error(
                              `生成失败：${e instanceof Error ? e.message : e}`));
                        }}
                      >
                        {art ? `重生成${v.label}` : `补${v.label}`}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
            <ModelSelect models={models} value={modelId} onChange={(v) => { userPicked.current = true; setModelId(v); }} label="生图模型" emptyLabel={emptyLabel} />
            <label className="cand-count" title="一次抽几张：多张各落一个版本，在版本条上挑一张「设为生效」">
              张数
              <select
                value={count}
                disabled={busy}
                onChange={(e) => setCount(Number(e.target.value))}
              >
                {CANDIDATE_COUNTS.map((n) => (
                  <option key={n} value={n}>{n} 张</option>
                ))}
              </select>
            </label>
            <button className="gen-btn" onClick={gen}
                    title={sceneGenCostTip || undefined}>
              {scene.artifact_id ? "重新生成" : "生成场景图"}
            </button>
          </>
        )}
        {/* 删除资产（生成中禁用）：预检引用 → 确认 → 删除并清引用 */}
        <button
          className="ghost-btn asset-del"
          disabled={busy}
          title="删除该场景并清空引用分镜的场景引用（图片产物保留在资产库）"
          onClick={() => void removeAsset()}
        >
          删除资产
        </button>
      </div>
      {previewUrl && <ImageLightbox artifactId={previewUrl} onClose={() => setPreviewUrl(null)} />}
    </div>
  );
}

/** 道具卡片（关键叙事道具：白底定妆图，跨镜头外观一致性的锚点）——
    与场景卡同款版本管理：多次抽卡成版本流、可设为生效、版本可命名 */
export function PropCard({ prop, models, emptyLabel }: { prop: PropInfo; models: ModelInfo[]; emptyLabel?: string }) {
  const generateProp = useDrama((s) => s.generateProp);
  const cancelProp = useDrama((s) => s.cancelProp);
  const deleteProp = useDrama((s) => s.deleteProp);
  const preferPropImage = useDrama((s) => s.preferPropImage);
  const updateProp = useDrama((s) => s.updateProp);
  const renameNodeVersion = useDrama((s) => s.renameNodeVersion);
  const projectId = useDrama((s) => s.projectId);
  const loadProps = useDrama((s) => s.loadProps);
  // 生成按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const propGenCostTip = useCostTip(BILLING_KEYS.propGenerate);
  const busy = prop.generating;
  const progress = useDrama((s) => s.genProgress[`prop_${prop.id}`]);
  const [modelId, setModelId] = useState("");
  // 项目预设对齐（批次 4 补充）：同 CharacterCard/SceneCard
  const presetT2i = useDrama((s) => s.modelPresets?.t2i ?? "");
  const userPicked = useRef(false);
  useEffect(() => {
    if (presetT2i && !userPicked.current) setModelId(presetT2i);
  }, [presetT2i]);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  // 生图提示词编辑（LLM 生成，可手动改）
  const [promptEdit, setPromptEdit] = useState(false);
  /** AI 优化提示词弹窗（批次 3）：优化结果经「应用」后写回 */
  const [optOpen, setOptOpen] = useState(false);
  const [imagePrompt, setImagePrompt] = useState(prop.image_prompt);
  useEffect(() => setImagePrompt(prop.image_prompt), [prop.image_prompt]);

  // 道具图多版本（引擎对 prop_{id} 节点追加抽卡版本，与场景卡同语义）：
  // 按 seq 倒序展示版本条，点选切换大图；生成终态（busy true→false）重拉
  const [versions, setVersions] = useState<NodeVersionInfo[]>([]);
  const [pickedId, setPickedId] = useState<string | null>(null);
  const busyKey = !!progress;
  const reloadVersions = useCallback(() => {
    if (!projectId || !prop.artifact_id) {
      setVersions([]);
      setPickedId(null);
      return Promise.resolve();
    }
    return api
      .listVersions(projectId, `prop_${prop.id}`)
      .then((list) => {
        const withArtifact = list
          .filter((v) => v.artifact_id)
          .sort((a, b) => b.seq - a.seq);
        setVersions(withArtifact);
        setPickedId(null); // 刷新后回到最新版本
      })
      .catch(() => {});
  }, [projectId, prop.id, prop.artifact_id, busyKey]);
  useEffect(() => {
    reloadVersions();
  }, [reloadVersions]);
  // 当前展示产物：手动点选的版本 ?? 最新版本（prop.artifact_id 由后端按
  // 「生效版本优先，否则最新」解析）
  const shownArtifact =
    versions.find((v) => v.id === pickedId)?.artifact_id ??
    prop.artifact_id ??
    null;
  // 生效版本：用户「设为生效」的版本，未设置时为最新版本
  const effectiveVersionId =
    prop.preferred_version_id || versions[0]?.id || null;
  const pickedVersion = versions.find((v) => v.id === pickedId) ?? null;

  const gen = () => {
    generateProp(prop.id, modelId || undefined).catch((e) =>
      toast.error(`道具图生成失败：${e instanceof Error ? e.message : e}`));
  };

  // 把预览选中的版本设为生效图（分镜参考图与卡片展示随之切换）
  const setPreferred = async () => {
    if (!pickedVersion) return;
    try {
      await preferPropImage(prop.id, pickedVersion.id);
      toast.success(`已设为生效版本（${pickedVersion.name || `版本 ${pickedVersion.seq}`}）`);
    } catch (e) {
      toast.error(`设置失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 版本重命名（输入框弹窗；留空恢复默认命名「版本N」）
  const renameVersion = async (v: NodeVersionInfo) => {
    const name = await promptDialog({
      title: "版本重命名",
      message: `为「${v.name || `版本 ${v.seq}`}」设置显示名称（留空恢复默认命名）`,
      initial: v.name,
      confirmText: "保存",
    });
    if (name === null) return;
    try {
      await renameNodeVersion(`prop_${prop.id}`, v.id, name.trim());
      void reloadVersions();
      toast.success("版本已命名");
    } catch (e) {
      toast.error(`命名失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 保存生图提示词（生成道具图时优先生效）
  const savePrompt = async () => {
    try {
      await updateProp(prop.id, { image_prompt: imagePrompt });
      setPromptEdit(false);
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 双击快速改名（分镜道具引用按 id 关联，改名不影响已绑定的引用）
  const renameProp = async (name: string) => {
    try {
      await updateProp(prop.id, { name });
      toast.success("已重命名");
    } catch (e) {
      toast.error(`重命名失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 删除资产（两段式）：先预检引用情况弹确认框，确认后删除道具并从
  // 引用分镜的 prop_refs 中移除（图片产物保留在资产库可找回）
  const removeAsset = async () => {
    try {
      const pre = await deleteProp(prop.id, false);
      const ok = await confirmDialog({
        title: "删除道具",
        message: `确定删除「${prop.name}」吗？${refsSummary(
          pre.references ?? { shots: 0, episodes: [] },
        )}图片产物保留在资产库可找回。`,
        confirmText: "删除",
        danger: true,
      });
      if (!ok) return;
      const res = await deleteProp(prop.id, true);
      const total = res.cleared?.shots ?? 0;
      toast.success(
        total > 0
          ? `已删除「${prop.name}」并清除 ${total} 处引用`
          : `已删除「${prop.name}」`,
      );
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 上传本地图片作为道具图：对 prop_{id} 节点追加 uploaded 版本；
  // 已设生效的版本存在时把新版本设为生效（否则生效指针仍指旧图不展示）
  const uploadImage = async (file: File) => {
    if (!projectId) return;
    try {
      const version = await api.uploadVersion(projectId, `prop_${prop.id}`, file);
      if (prop.preferred_version_id) {
        await preferPropImage(prop.id, version.id);
      } else {
        await loadProps();
      }
      await reloadVersions();
      toast.success("已上传道具图");
    } catch (e) {
      toast.error(`上传失败：${e instanceof Error ? e.message : e}`);
    }
  };

  // 删除当前展示的道具图版本（版本级删除；产物保留资产库可找回）
  const removePropImage = async () => {
    const shownVersion = versions.find((v) => v.artifact_id === shownArtifact);
    if (!shownVersion) return;
    const versionLabel = shownVersion.name || `版本 ${shownVersion.seq}`;
    const keepNote =
      versions.length > 1
        ? `其余 ${versions.length - 1} 个版本将保留。`
        : "这是唯一版本，删除后道具将回到未生成态。";
    const ok = await confirmDialog({
      title: "删除道具图",
      message: `确定删除「${prop.name}」的 ${versionLabel} 吗？${keepNote}图片文件保留在资产库可找回。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      if (!projectId) throw new Error("项目未初始化");
      await api.deleteNodeVersion(projectId, `prop_${prop.id}`, shownVersion.id);
      // 本地先移除被删版本，再以服务端为准重拉
      setVersions((prev) => prev.filter((v) => v.id !== shownVersion.id));
      void reloadVersions();
      setPickedId(null);
      toast.success("道具图已删除");
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <div className={`asset-card${busy ? " generating" : ""}`}>
      <div className={`asset-thumb ${shownArtifact ? "" : "blank"}`}>
        {shownArtifact ? (
          <img
            src={fileUrl(shownArtifact)}
            alt={prop.name}
            title="点击放大预览"
            className="clickable"
            onClick={() => setPreviewUrl(shownArtifact)}
          />
        ) : (
          <span>{busy ? "道具图生成中" : "暂无道具图"}</span>
        )}
        {busy && (
          <>
            <span className="gen-badge">{progressLabel(progress)}</span>
            <ThumbMarquee progress={progress} />
          </>
        )}
        {/* 右上角垂直操作组（上传 → 删除；进度徽标已移左上不冲突） */}
        <div className="thumb-actions">
          {/* 上传本地图片（生成中隐藏）：追加为该道具的 uploaded 版本 */}
          {!busy && (
            <label className="thumb-upload" title="上传本地图片作为道具图">
              <input
                type="file"
                accept="image/png,image/jpeg,image/webp"
                hidden
                onChange={(e) => {
                  const picked = e.target.files?.[0];
                  e.target.value = ""; // 清空以便可重复选择同一文件
                  if (picked) void uploadImage(picked);
                }}
              />
              上传
            </label>
          )}
          {/* 删除当前展示版本（生成中隐藏） */}
          {shownArtifact && !busy && (
            <button className="thumb-del" title="删除道具图" onClick={() => void removePropImage()}>
              删除
            </button>
          )}
        </div>
      </div>
      {/* 版本条（有版本即显示，与场景卡同款交互）：点选切换大图，
          生效版本带金色徽标；选中非生效版本时出现「设为生效」，可改名 */}
      {versions.length > 0 && (
        <VersionStrip
          versions={versions}
          shownArtifact={shownArtifact}
          effectiveVersionId={effectiveVersionId}
          pickedVersion={pickedVersion}
          onPick={setPickedId}
          onPrefer={() => void setPreferred()}
          onRename={(v) => void renameVersion(v)}
          defaultName={(v) => `版本 ${v.seq}`}
        />
      )}
      <div className="asset-name">
        <AssetNameText name={prop.name} onRename={renameProp} />
        {typeof prop.freq === "number" && prop.freq > 0 && (
          <span className="freq-tag">出现 {prop.freq} 次</span>
        )}
      </div>
      <div className="asset-field">
        <span className="asset-label">描述</span>
        <span className="asset-text">{prop.description || "（未填写）"}</span>
      </div>
      {/* 生图提示词：LLM 基于剧本生成（白底物件图），生成道具图时优先生效 */}
      {promptEdit ? (
        <div className="asset-field edit-desc">
          <span className="asset-label">生图提示词</span>
          <textarea
            value={imagePrompt}
            placeholder="道具定妆图提示词（全貌/材质/颜色/新旧，AI 已按剧本预生成）"
            onChange={(e) => setImagePrompt(e.target.value)}
          />
          <div className="asset-footer">
            <button className="gen-btn" onClick={() => void savePrompt()}>
              保存
            </button>
            <button
              className="ghost-btn"
              onClick={() => {
                setPromptEdit(false);
                setImagePrompt(prop.image_prompt);
              }}
            >
              取消
            </button>
          </div>
        </div>
      ) : (
        <>
        <div className="asset-field">
          <span className="asset-label">生图提示词</span>
          <span className="asset-text">
            {prop.image_prompt || "（AI 解析后自动生成）"}
          </span>
          {/* 编辑 / AI 优化 并排一行（.asset-field 是 column，直接放会各占一行） */}
          <div className="asset-field-actions">
            <button className="vp-edit-btn" onClick={() => setPromptEdit(true)}>
              编辑
            </button>
            {prop.image_prompt && projectId && (
              <button
                className="vp-edit-btn"
                title="说清修改需求，由 AI 结合剧本上下文改写提示词（可编辑后再应用）"
                onClick={() => setOptOpen(true)}
              >
                AI 优化
              </button>
            )}
          </div>
        </div>
        {optOpen && projectId && (
          <PromptOptimizeDialog
            projectId={projectId}
            target={{
              target: "prop",
              id: prop.id,
              label: "道具图提示词",
              currentPrompt: prop.image_prompt,
            }}
            onApply={async (text) => {
              setImagePrompt(text);
              await updateProp(prop.id, { image_prompt: text });
              setOptOpen(false);
              toast.success("已应用 AI 优化后的提示词");
            }}
            onClose={() => setOptOpen(false)}
          />
        )}
        </>
      )}
      <div className="asset-footer">
        {busy ? (
          <button
            className="ghost-btn"
            onClick={() =>
              cancelProp(prop.id).catch((e) =>
                toast.error(`取消失败：${e instanceof Error ? e.message : e}`),
              )
            }
          >
            取消生成
          </button>
        ) : (
          <>
            <ModelSelect models={models} value={modelId} onChange={(v) => { userPicked.current = true; setModelId(v); }} label="生图模型" emptyLabel={emptyLabel} />
            <button className="gen-btn" onClick={gen}
                    title={propGenCostTip || undefined}>
              {prop.artifact_id ? "重新生成" : "生成道具图"}
            </button>
          </>
        )}
        {/* 删除资产（生成中禁用）：预检引用 → 确认 → 删除并清引用 */}
        <button
          className="ghost-btn asset-del"
          disabled={busy}
          title="删除该道具并从引用分镜中移除（图片产物保留在资产库）"
          onClick={() => void removeAsset()}
        >
          删除资产
        </button>
      </div>
      {previewUrl && <ImageLightbox artifactId={previewUrl} onClose={() => setPreviewUrl(null)} />}
    </div>
  );
}

/** 道具资产区：关键叙事道具卡片流（数据随剧本解析/生图任务刷新） */
function PropSection({ models, emptyLabel }: { models: ModelInfo[]; emptyLabel?: string }) {
  // 数据源用 store.props（GET /props，生图任务终态与重连都会刷新）——
  // script.props 仅在解析完成时刷新，生图完成后卡片不会更新（刷新 bug 根因）
  const props = useDrama((s) => s.props);
  return (
    <section className="assets-section">
      <div className="assets-title">道具{props.length > 0 && `（${props.length}）`}</div>
      <div className="card-flow">
        {props.map((p) => (
          <PropCard key={p.id} prop={p} models={models} emptyLabel={emptyLabel} />
        ))}
        <NewPropCard />
      </div>
      {props.length === 0 && (
        <div className="assets-empty">
          解析剧本后自动提取关键道具（反复出现或承载剧情的物件，如画板/车辆/餐具），也可手动新建，作为跨镜头外观一致性锚点
        </div>
      )}
    </section>
  );
}

/** 手工新建场景卡片（解析之外补充资产，与新建角色同模式，支持附带本地图） */
function NewSceneCard() {
  const createScene = useDrama((s) => s.createScene);
  const loadScenes = useDrama((s) => s.loadScenes);
  const projectId = useDrama((s) => s.projectId);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  // 可选本地图片：场景创建成功后追加为 uploaded 版本（即当前主图）
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const created = await createScene({
        name: name.trim(),
        description: desc.trim() || undefined,
      });
      // 图片上传失败不回滚场景（资产已创建），仅提示错误
      if (created && imageFile && projectId) {
        try {
          await api.uploadVersion(projectId, `scene_${created.id}`, imageFile);
          await loadScenes();
        } catch (e) {
          toast.error(`场景已创建，但图片上传失败：${e instanceof Error ? e.message : e}`);
        }
      }
      setOpen(false);
      setName("");
      setDesc("");
      setImageFile(null);
    } catch (e) {
      toast.error(`新建场景失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button className="asset-card add-card" onClick={() => setOpen(true)}>
        + 新建场景
      </button>
    );
  }
  return (
    <div className="asset-card form-card">
      <div className="asset-name">新建场景</div>
      <div className="asset-field">
        <span className="asset-label">名称</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="场景名（地点+时间，如：拒北垣城头 风雪 夜）"
        />
      </div>
      <div className="asset-field">
        <span className="asset-label">描述</span>
        <textarea
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          placeholder="环境布局/时间光线/氛围等描述"
        />
      </div>
      <div className="asset-field">
        <span className="asset-label">图片（可选）</span>
        <label className="file-upload">
          <input
            type="file"
            accept="image/png,image/jpeg,image/webp"
            hidden
            onChange={(e) => {
              setImageFile(e.target.files?.[0] ?? null);
              e.target.value = ""; // 清空以便可重复选择同一文件
            }}
          />
          {imageFile ? imageFile.name : "选择本地图片"}
        </label>
      </div>
      <div className="asset-footer">
        <button className="gen-btn" disabled={busy || !name.trim()} onClick={submit}>
          确定
        </button>
        <button className="ghost-btn" disabled={busy} onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </div>
  );
}

/** 手工新建道具卡片（解析之外补充资产，与新建角色/场景同模式，支持附带本地图） */
function NewPropCard() {
  const createProp = useDrama((s) => s.createProp);
  const loadProps = useDrama((s) => s.loadProps);
  const projectId = useDrama((s) => s.projectId);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  // 可选本地图片：道具创建成功后追加为 uploaded 版本（即当前主图）
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const created = await createProp({
        name: name.trim(),
        description: desc.trim() || undefined,
      });
      // 图片上传失败不回滚道具（资产已创建），仅提示错误
      if (created && imageFile && projectId) {
        try {
          await api.uploadVersion(projectId, `prop_${created.id}`, imageFile);
          await loadProps();
        } catch (e) {
          toast.error(`道具已创建，但图片上传失败：${e instanceof Error ? e.message : e}`);
        }
      }
      setOpen(false);
      setName("");
      setDesc("");
      setImageFile(null);
    } catch (e) {
      toast.error(`新建道具失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button className="asset-card add-card" onClick={() => setOpen(true)}>
        + 新建道具
      </button>
    );
  }
  return (
    <div className="asset-card form-card">
      <div className="asset-name">新建道具</div>
      <div className="asset-field">
        <span className="asset-label">名称</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="道具名（如：青铜罗盘）"
        />
      </div>
      <div className="asset-field">
        <span className="asset-label">描述</span>
        <textarea
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          placeholder="全貌/材质/颜色/新旧等描述"
        />
      </div>
      <div className="asset-field">
        <span className="asset-label">图片（可选）</span>
        <label className="file-upload">
          <input
            type="file"
            accept="image/png,image/jpeg,image/webp"
            hidden
            onChange={(e) => {
              setImageFile(e.target.files?.[0] ?? null);
              e.target.value = ""; // 清空以便可重复选择同一文件
            }}
          />
          {imageFile ? imageFile.name : "选择本地图片"}
        </label>
      </div>
      <div className="asset-footer">
        <button className="gen-btn" disabled={busy || !name.trim()} onClick={submit}>
          确定
        </button>
        <button className="ghost-btn" disabled={busy} onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </div>
  );
}

/** 资产分组（水平 tab 的键） */
export type AssetSection = "characters" | "scenes" | "props" | "orphans";

/** 孤儿产物类别码 → 中文（后端只给码，文案在前端） */
const ORPHAN_KIND_LABEL: Record<string, string> = {
  shot_v: "分镜视频",
  shot_ff: "分镜首帧",
  char: "角色形象",
  scene: "场景主图",
  prop: "道具图",
};

/** 字节数 → 人类可读（孤儿产物要回答「占了多少磁盘」） */
function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** 秒级时间戳 → 本地「月-日 时:分」 */
function fmtTime(at: number): string {
  const d = new Date(at * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 分组 tab 定义：标题 + 该组的口径说明（原型 .tabs / .hint） */
const ASSET_TABS: { key: AssetSection; title: string; hint: string }[] = [
  {
    key: "characters",
    title: "角色",
    hint: "资产按集解析：解析第 N 集时会带上已有名单，只补新增角色，不会重复解析已有角色。"
      + "缺定妆图的角色生成视频时会被拦。",
  },
  {
    key: "scenes",
    title: "场景",
    hint: "场景按集解析并做归并（词干 → 相似度门禁 → LLM 分组），内外景永不合并。",
  },
  {
    key: "props",
    title: "道具",
    hint: "关键道具（反复出现或推动剧情且易漂移）会生成定妆图作为跨镜头外观锚点。",
  },
  {
    key: "orphans",
    title: "孤儿产物",
    hint: "重建分集/删除资产后，已生成的图与视频会失去全部引用（孤儿）——"
      + "它们没有被删，产物文件与版本记录都在，只是任何卡片都不再显示。"
      + "这里把它们列出来：看得见、能下载、能对账磁盘占用；**不做自动清理**"
      + "（重建后新旧镜头内容常常重叠，删掉就找不回来了）。",
  },
];

export function AssetsTab({ section: initialSection }: { section?: AssetSection }) {
  const characters = useDrama((s) => s.characters);
  const scenes = useDrama((s) => s.scenes);
  const props = useDrama((s) => s.props);
  /** 当前显示的分组（tab 切换；默认可由调用方指定） */
  const [section, setSection] = useState<AssetSection>(initialSection ?? "characters");
  // 生图模型选项 = 本地注册表 t2i 模型 + 模型服务端点模型（ark/agnes 等），挂载时拉取一次
  const [t2iModels, setT2iModels] = useState<ModelInfo[]>([]);
  // 空值文案显示全局默认端点名（未设置显示「默认」）
  const [t2iEmptyLabel, setT2iEmptyLabel] = useState<string | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    fetchModelOptions("t2i").then((models) => {
      if (alive) setT2iModels(models);
    });
    defaultEmptyLabel("t2i").then((label) => {
      if (alive && label) setT2iEmptyLabel(`默认：${label}`);
    });
    return () => {
      alive = false;
    };
  }, []);

  /** 孤儿产物（25 号 2.5 的可回溯入口）：进资产页时拉一次——角标要显示数量，
   *  而且用户「我生成过的东西去哪了」的问题大多从这里被问起。
   *  **依赖 projectId 而不是挂载时读一次**：资产页可能先于项目加载完成挂载
   *  （store 里 projectId 还是空），读一次会永远停在「加载中…」（实测）。
   *  失败要有**明确失败态**：否则用户看到的是永远转圈，比报错更糟。 */
  const pid = useDrama((s) => s.projectId);
  const [orphans, setOrphans] = useState<
    Awaited<ReturnType<typeof api.orphanVersions>> | null
  >(null);
  const [orphansFailed, setOrphansFailed] = useState(false);
  const [orphansTick, setOrphansTick] = useState(0);
  useEffect(() => {
    if (!pid) return;
    let alive = true;
    setOrphansFailed(false);
    api
      .orphanVersions(pid)
      .then((d) => {
        if (alive) setOrphans(d);
      })
      .catch(() => {
        if (alive) setOrphansFailed(true);   // 读不到不阻塞资产页，但要说明
      });
    return () => {
      alive = false;
    };
  }, [pid, orphansTick]);

  /** 各分组数量角标（孤儿数未拉到前显示「…」——**不显示 0**，那是在撒谎） */
  const counts: Record<AssetSection, number | string> = {
    characters: characters.length,
    scenes: scenes.length,
    props: props.length,
    orphans: orphansFailed ? "!" : orphans ? orphans.total : "…",
  };
  const tab = ASSET_TABS.find((t) => t.key === section) ?? ASSET_TABS[0];

  return (
    <div className="ast">
      <div className="stage-bar">
        资产
        <span className="stage-bar-sub">按集解析：每集只补新增，不重复解析已有</span>
      </div>
      <div className="ast-tabs">
        {ASSET_TABS.map((t) => (
          <button
            key={t.key}
            className={section === t.key ? "on" : ""}
            onClick={() => setSection(t.key)}
          >
            {t.title}
            <i>{counts[t.key]}</i>
          </button>
        ))}
      </div>
      <div className="ast-body">
        <div className="ast-hint">{tab.hint}</div>
        {section === "characters" && (
          <section className="assets-section">
            <div className="assets-title">
              角色{characters.length > 0 && `（${characters.length}）`}
            </div>
            <div className="card-flow">
              {characters.map((c) => (
                <CharacterCard key={c.id} character={c} models={t2iModels} emptyLabel={t2iEmptyLabel} />
              ))}
              <NewCharacterCard />
            </div>
            {characters.length === 0 && (
              <div className="assets-empty">解析剧本后自动提取角色，也可手动新建</div>
            )}
          </section>
        )}
        {section === "scenes" && (
          <section className="assets-section">
            <div className="assets-title">场景{scenes.length > 0 && `（${scenes.length}）`}</div>
            <div className="card-flow">
              {scenes.map((s) => (
                <SceneCard key={s.id} scene={s} models={t2iModels} emptyLabel={t2iEmptyLabel} />
              ))}
              <NewSceneCard />
            </div>
            {scenes.length === 0 && (
              <div className="assets-empty">解析剧本后自动提取场景，也可手动新建</div>
            )}
          </section>
        )}
        {section === "props" && (
          <PropSection models={t2iModels} emptyLabel={t2iEmptyLabel} />
        )}
        {section === "orphans" && (
          <section className="assets-section">
            <div className="assets-title">
              孤儿产物{orphans && orphans.total > 0 && `（${orphans.total}）`}
            </div>
            {orphansFailed ? (
              <div className="assets-empty">
                孤儿清单加载失败（不影响资产维护）
                <button
                  className="link-btn"
                  style={{ marginLeft: 8 }}
                  onClick={() => setOrphansTick((n) => n + 1)}
                >
                  重试
                </button>
              </div>
            ) : !orphans ? (
              <div className="assets-empty">加载中…</div>
            ) : orphans.total === 0 ? (
              <div className="assets-empty">
                没有孤儿产物——当前所有产出都还挂在分镜或资产上
              </div>
            ) : (
              <>
                <div className="orphan-sum">
                  已不再被任何分镜/资产引用：{orphans.total} 条 · 占用磁盘{" "}
                  {fmtBytes(orphans.bytes)}。产物文件保留在磁盘与资产库（可下载
                  找回），系统不会自动清理——重建分集后新旧镜头内容常常重叠。
                </div>
                <div className="orphan-kinds">
                  {orphans.by_kind.map((b) => (
                    <span key={b.kind} className="orphan-kind">
                      {ORPHAN_KIND_LABEL[b.kind] ?? b.kind}：{b.count} 条（
                      {fmtBytes(b.bytes)}）
                    </span>
                  ))}
                </div>
                <div className="orphan-list">
                  {orphans.items.map((it) => (
                    <div key={it.version_id} className="orphan-item">
                      {!it.artifact_id ? (
                        <div className="orphan-thumb" />   // 版本无产物（历史行）
                      ) : it.asset_kind === "video" ? (
                        <video
                          className="orphan-thumb"
                          src={fileUrl(it.artifact_id)}
                          muted
                          preload="metadata"
                        />
                      ) : (
                        <img
                          className="orphan-thumb"
                          src={fileUrl(it.artifact_id)}
                          alt=""
                          loading="lazy"
                        />
                      )}
                      <div className="orphan-meta">
                        <span className="orphan-name">
                          {ORPHAN_KIND_LABEL[it.kind] ?? it.kind} · 第 {it.seq} 版
                          {it.name ? ` · ${it.name}` : ""}
                        </span>
                        <span className="orphan-sub">
                          {fmtBytes(it.bytes)} · {fmtTime(it.created_at)}
                          {!it.on_disk && " · ⚠ 文件已不在磁盘"}
                        </span>
                        {it.prompt && (
                          <span className="orphan-prompt">{it.prompt.slice(0, 80)}</span>
                        )}
                      </div>
                      {it.artifact_id && (
                        <a
                          className="orphan-dl"
                          href={fileUrl(it.artifact_id)}
                          download
                        >
                          下载
                        </a>
                      )}
                    </div>
                  ))}
                </div>
                {orphans.items.length < orphans.total && (
                  <div className="assets-empty">
                    仅显示最近 {orphans.items.length} 条（共 {orphans.total} 条）
                  </div>
                )}
              </>
            )}
          </section>
        )}
      </div>
    </div>
  );
}

/** 场景组弹窗（32 号）：同一真实空间的分组、组环境设定与「环境母版」选择。
 *
 * 交互：组名留空保存 = 移出组；「环境母版」默认**自动**（组内首张生成好的场景图），
 * 也可从同组成员里点选一张。保存后服务端会对组内全部场景重组装提示词（组环境设定
 * 进提示词框，「所见即所发」），本组件只负责发请求与提示。
 */
/** 场景组弹窗（32 号，多选式交互）：一次勾选多个场景、一次保存。
 *
 * 交互要点：
 * - 从场景卡打开 = 以「本卡所在组」为起点（未分组时勾选里只有本卡）；
 * - 勾选列表 = 「这批场景同属一个真实空间」，保存时整批提交（服务端原子应用）；
 * - 点已有组 chip / 组名正好命中已有组 = 并入它：自动把它的成员并进勾选、
 *   带出它的环境设定与母版（母版只"带出来显示"，用户不动就不提交，防误清）；
 * - 组名留空 = 把勾选的场景移出各自的组。
 */
function SceneGroupDialog({ scene, onClose }: { scene: SceneInfo; onClose: () => void }) {
  const applySceneGroup = useDrama((s) => s.applySceneGroup);
  const allScenes = useDrama((s) => s.scenes);
  const projectId = useDrama((s) => s.projectId);
  const [name, setName] = useState(scene.group?.name ?? "");
  const [env, setEnv] = useState(scene.group?.env_setting ?? "");
  const [anchor, setAnchor] = useState("");              // 界面显示的母版选择（""=自动）
  const [anchorDirty, setAnchorDirty] = useState(false); // 用户动过母版才提交它
  const [checked, setChecked] = useState<Set<string>>(new Set([scene.id]));
  // 已联动过的组名：并入联动去重（改回同名不再重复并入用户已取消的成员）
  const syncedName = useRef(scene.group?.name ?? "");
  const [busy, setBusy] = useState(false);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  // 项目里已有的组（从场景列表归纳——组靠成员场景卡承载，没有独立入口）
  const groups = useMemo(() => {
    const m = new Map<string, { id: string; name: string; memberIds: string[] }>();
    for (const s of allScenes) {
      if (!s.group) continue;
      const g = m.get(s.group.id) ?? { id: s.group.id, name: s.group.name, memberIds: [] };
      g.memberIds.push(s.id);
      m.set(s.group.id, g);
    }
    return [...m.values()];
  }, [allScenes]);

  // 打开时：勾选 = 本卡所在组的全部成员；母版指针从组详情带出
  useEffect(() => {
    const g = scene.group ? groups.find((x) => x.id === scene.group!.id) : undefined;
    if (g) setChecked(new Set(g.memberIds));
    if (!scene.group) return;
    api.getSceneGroup(scene.id)
      .then((r) => {
        if (aliveRef.current && r.group) setAnchor(r.group.anchor_asset_id || "");
      })
      .catch(() => {});
    // 只在打开时联动一次（对话打开期间列表刷新不回灌，避免覆盖用户正在改的勾选）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scene.id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // 并入联动：把已有组的成员并进勾选、带出环境设定与母版指针
  const mergeGroup = (g: { id: string; name: string; memberIds: string[] }) => {
    setChecked((prev) => new Set([...prev, ...g.memberIds]));
    const firstMember = allScenes.find((s) => s.id === g.memberIds[0]);
    setEnv(firstMember?.group?.env_setting ?? "");
    setAnchorDirty(false);   // 母版显示换成目标组的，未再动就不提交
    if (g.memberIds[0]) {
      api.getSceneGroup(g.memberIds[0])
        .then((r) => {
          if (aliveRef.current && r.group) setAnchor(r.group.anchor_asset_id || "");
        })
        .catch(() => {});
    }
  };

  // 组名输入：正好命中项目里已有组名 = 并入它（与点 chip 等价）
  const onNameChange = (v: string) => {
    setName(v);
    const hit = groups.find((x) => x.name === v.trim());
    if (!hit || syncedName.current === hit.name) return;
    syncedName.current = hit.name;
    mergeGroup(hit);
  };

  const pickGroup = (g: { id: string; name: string; memberIds: string[] }) => {
    setName(g.name);
    syncedName.current = g.name;
    mergeGroup(g);
  };

  // 成员勾选：取消勾选正好是当前显示的母版图时，清回「自动」防指向非成员
  const toggleMember = (s: SceneInfo) => {
    const next = new Set(checked);
    if (next.has(s.id)) {
      next.delete(s.id);
      if (s.artifact_id && s.artifact_id === anchor) {
        setAnchor("");
        setAnchorDirty(true);
      }
    } else {
      next.add(s.id);
    }
    setChecked(next);
  };

  const save = async () => {
    if (busy || !projectId) return;
    const want = name.trim();
    const ids = [...checked];
    if (want && ids.length === 0) {
      toast.error("请至少勾选一个场景");
      return;
    }
    if (!want && ids.length === 0) {
      onClose();
      return;
    }
    setBusy(true);
    try {
      const res = await applySceneGroup({
        project_id: projectId, scene_ids: ids, name: want, env_setting: env,
        // 母版没动过就不提交（防"盲输已有组名"把别人组里已选的母版清掉）
        ...(anchorDirty ? { anchor_asset_id: anchor } : {}),
      });
      if (!want) {
        toast.success(res.removed > 0 ? "已移出场景组" : "这些场景本来就不在任何场景组");
      } else {
        toast.success("场景组已保存（组内场景的提示词已同步更新）");
      }
      onClose();
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const want = name.trim();
  // 母版候选 = 勾选中已有图的场景（artifact 非空才进来，供缩略图点选）
  const anchorOptions = allScenes.filter(
    (s): s is SceneInfo & { artifact_id: string } =>
      checked.has(s.id) && !!s.artifact_id);

  // 点遮罩不关闭（表单里有未保存的编辑，误触即丢）
  return createPortal(
    <div className="settings-mask">
      <div className="settings-dialog sg-dialog">
        <div className="settings-head">
          <span className="settings-title">场景组 · {scene.name}</span>
          <button className="provider-close" onClick={onClose}>关闭</button>
        </div>
        <div className="project-settings-body">
          <p className="sg-hint">
            把「同一个真实空间」的场景勾成一组（一套房的客厅/卧室/饭桌、一家店的店内/门口）：
            组环境设定会写进每个成员的生图提示词；生成时还会拿组内的「环境母版图」当参考图，
            让建材/色调/门窗/照明看起来是同一个地方。
          </p>
          <label className="sg-label">场景组名（留空 = 把勾选的场景移出场景组）
            <input className="sg-input" value={name} placeholder="如：林家老宅"
                   onChange={(e) => onNameChange(e.target.value)} />
          </label>
          {groups.length > 0 && (
            <div className="sg-label">并入已有组：
              <div className="sg-chips">
                {groups.map((g) => (
                  <button key={g.id}
                          className={`sg-chip${want === g.name ? " on" : ""}`}
                          onClick={() => pickGroup(g)}>
                    {g.name} · {g.memberIds.length}
                  </button>
                ))}
              </div>
            </div>
          )}
          <div className="sg-label">
            {want ? `成员（勾选要归入「${want}」的场景）` : "勾选的场景将移出各自所在的组"}
            <div className="sg-scenes">
              {allScenes.map((s) => {
                const on = checked.has(s.id);
                return (
                  <button key={s.id} type="button"
                          className={`sg-row${on ? " on" : ""}`}
                          onClick={() => toggleMember(s)}>
                    <span className="sg-check">{on ? "✓" : ""}</span>
                    {s.artifact_id
                      ? <img className="sg-thumb" src={fileUrl(s.artifact_id)} alt="" />
                      : <span className="sg-thumb sg-thumb-ph">无图</span>}
                    <span className="sg-name">{s.name}</span>
                    {s.id === scene.id && <em className="sg-tag">本卡</em>}
                    {s.group && s.group.name !== want && (
                      <em className="sg-tag muted">现属：{s.group.name}</em>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
          {want && (
            <>
              <label className="sg-label">
                组环境设定（一句话：建筑年代/装修档次/主材/墙地色调/门窗样式/照明）
                <textarea className="sg-input sg-textarea" rows={3} value={env}
                          placeholder="如：老式单位房，深色木地板，米白墙面，木框窗，暖黄吊灯"
                          onChange={(e) => setEnv(e.target.value)} />
              </label>
              <div className="sg-label">环境母版（生成同组其他场景时的参考图）
                <div className="sg-members">
                  <button className={`sg-member${anchor === "" ? " on" : ""}`}
                          onClick={() => { setAnchor(""); setAnchorDirty(true); }}>
                    <span className="sg-member-ph">自动</span>
                    <small>组内首张生成的场景图</small>
                  </button>
                  {anchorOptions.map((s) => (
                    <button key={s.id}
                            className={`sg-member${anchor === s.artifact_id ? " on" : ""}`}
                            title={`用「${s.name}」的这张图当环境母版`}
                            onClick={() => { setAnchor(s.artifact_id); setAnchorDirty(true); }}>
                      <img src={fileUrl(s.artifact_id)} alt={s.name} />
                      <small>{s.name}</small>
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
        <div className="prompt-opt-foot">
          <button className="fb-btn ghost" onClick={onClose}>取消</button>
          <button className="fb-btn primary" disabled={busy} onClick={() => void save()}>
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
