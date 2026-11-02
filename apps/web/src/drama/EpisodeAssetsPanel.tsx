/** 本集资产面板（分集工作台步骤条第 2 步）：解析本集正文里的资产 + 本集资产概览。
 *
 * 卡片直接复用资产页的 CharacterCard / SceneCard / PropCard——同款样式与全部操作
 * （生成定妆图/妆造/版本/删除等），一行都不重写。
 *
 * 「本集涉及的资产」判据：逐集解析只补新增，所以**首见集号（origin_seq）等于本集**
 * 的资产就是本集的解析产出；其余是本集复用到的已有资产（不在这里重复展示，避免把
 * 全项目资产都铺一屏）。
 *
 * 后端契约：POST /api/episodes/{id}/assets（node_id = `ep_assets_{集id}`）；
 * 分集列表项带 asset_seqs（有值 = 本集资产已解析过）。
 */

import { useEffect, useMemo, useState } from "react";
import type { CharacterInfo, EpisodeInfo, ModelInfo, PropInfo, SceneInfo } from "../api/client";
import { defaultEmptyLabel, fetchModelOptions } from "../api/modelOptions";
import { BILLING_KEYS } from "../billingKeys";
import { useDrama } from "../stores/drama";
import { costLine, useCostTip } from "../stores/billing";
import { confirmDialog, toast } from "../ui/feedback";
import { CharacterCard, PropCard, SceneCard } from "./AssetsTab";

/** 本集资产集合（首见集号 = 本集） */
export function useEpisodeAssets(seq: number): {
  characters: CharacterInfo[];
  scenes: SceneInfo[];
  props: PropInfo[];
  /** 本集解析出的资产总数 */
  count: number;
} {
  const characters = useDrama((s) => s.characters);
  const scenes = useDrama((s) => s.scenes);
  const props = useDrama((s) => s.props);
  return useMemo(() => {
    const chars = characters.filter((c) => c.origin_seq === seq);
    const scns = scenes.filter((s) => s.origin_seq === seq);
    const prps = props.filter((p) => p.origin_seq === seq);
    return {
      characters: chars,
      scenes: scns,
      props: prps,
      count: chars.length + scns.length + prps.length,
    };
  }, [characters, scenes, props, seq]);
}

/** 本集资产是否已解析：分集资产标记优先，回退「有资产首见本集」 */
export function useEpisodeAssetsParsed(
  episode: EpisodeInfo, newCount: number,
): boolean {
  // 判据一：本集解析存档存在（`episode.assets` 非空对象）。这是**权威信号**——
  // 本集全部资产都命中复用、一个新增都没有时，「本集资产」也是已解析过。
  // 判据二：兜底数一遍 origin_seq 等于本集的资产（存档因故缺失时仍能显示）。
  // 注意：不能只看判据二——回调到「本集无新增」时会误报未解析。
  const rec = episode.assets;
  return (rec != null && Object.keys(rec).length > 0) || newCount > 0;
}

export function EpisodeAssetsPanel({ episode }: { episode: EpisodeInfo }) {
  const extractEpisodeAssets = useDrama((s) => s.extractEpisodeAssets);
  const epTask = useDrama((s) => s.epTask);
  const assets = useEpisodeAssets(episode.seq);
  const parsed = useEpisodeAssetsParsed(episode, assets.count);
  // 解析按钮的悬停提示里带计费说明（不要单独徽标挂按钮上）
  const extractCostTip = useCostTip(BILLING_KEYS.episodeAssets);

  // 生图模型选项（复用资产卡的下拉默认值）：与资产页同一份数据源
  const [t2iModels, setT2iModels] = useState<ModelInfo[]>([]);
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

  const busy = epTask?.episodeId === episode.id;
  const extracting = busy && epTask?.kind === "assets";
  // 缺定妆图的角色：生成视频时会被拦（与资产卡的红线一致）
  const noPoster = assets.characters.filter(
    (c) => !c.images.some((im) => im.artifact_id));

  const runExtract = async () => {
    const ok = await confirmDialog({
      title: `解析第 ${episode.seq} 集资产`,
      message: "AI 会读本集正文，把角色 / 场景 / 道具对到项目资产：已有名单一并提供，"
        + "只补新增，不会重复解析已有资产（正文里的别名会统一到名单名）。确定继续吗？"
        + (costLine(BILLING_KEYS.episodeAssets)
            ? `\n\n${costLine(BILLING_KEYS.episodeAssets)}` : ""),
      confirmText: "开始解析",
    });
    if (!ok) return;
    const res = await extractEpisodeAssets(episode.id);
    if (res) toast.success(`第 ${episode.seq} 集资产解析已提交`);
    else toast.error("资产解析任务提交失败，请查看提示");
  };

  return (
    <div className="ep-pane">
      <div className="ep-pane-lead">
        解析本集正文里的角色 / 场景 / 道具。会把已有资产名单一并交给 AI，只补新增的，
        不会重复解析已有角色；解析结果落在项目资产上（首见集号 = 本集）。
      </div>
      <div className="ep-pane-acts">
        <button
          className="gen-btn primary"
          disabled={busy}
          onClick={() => void runExtract()}
          title={extractCostTip || undefined}
        >
          {extracting ? "解析中…" : parsed ? "重新解析本集资产" : "解析本集资产"}
        </button>
        {parsed && (
          <span className="ep-pane-note">
            {assets.count > 0 ? `本集新增 ${assets.count} 个资产` : "本集未新增资产（正文都是已有名单里的）"}

          </span>
        )}
        <span className="ep-pane-note">解析在「分集」页逐集进行，资产页只管维护</span>
      </div>

      {/* 补充视角建议（2026-09-29）：资产解析时**只提示、不生成**——反打/过肩/
          交互区特写的镜需要场景主图看不到的那一面，是否生成由用户决定（去资产页）。
          放在卡片**上方**：放在下面等于没有（实测要滚过两张大卡才看得到）。 */}
      {parsed && (episode.assets?.view_hints?.length ?? 0) > 0 && (
        <div className="ep-view-hints">
          <div className="ep-view-hints-title">补充视角建议（只是建议，不会自动生成）</div>
          {episode.assets!.view_hints!.map((h) => (
            <div key={`${h.scene}-${h.view}`} className="ep-view-hint-row">
              <span className="ep-view-hint-scene">{h.scene}</span>
              <span className="ep-view-hint-view">
                {h.view === "reverse" ? "反打视角" : h.view === "interaction" ? "交互区特写" : h.view}
              </span>
              {h.reason && <span className="ep-view-hint-reason">{h.reason}</span>}
            </div>
          ))}
          <div className="ep-view-hints-foot">
            到「资产 → 场景」里点该场景的「补充视角」即可生成（以主图为基础、空间同源）。
            生成后，声明要用它的分镜会自动挂上。
          </div>
        </div>
      )}

      {parsed && assets.count > 0 && (
        <div className="card-flow">
          {assets.characters.map((c) => (
            <CharacterCard key={c.id} character={c} models={t2iModels} emptyLabel={t2iEmptyLabel} />
          ))}
          {assets.scenes.map((s) => (
            <SceneCard key={s.id} scene={s} models={t2iModels} emptyLabel={t2iEmptyLabel} />
          ))}
          {assets.props.map((p) => (
            <PropCard key={p.id} prop={p} models={t2iModels} emptyLabel={t2iEmptyLabel} />
          ))}
        </div>
      )}

      {parsed && noPoster.length > 0 && (
        <div className="ep-pane-warn">
          缺定妆图：{noPoster.map((c) => c.name).join("、")}
          ——生成视频会被拦，请在卡片上生成定妆图（也可到「资产」页统一维护）
        </div>
      )}
      {!parsed && (
        <div className="ep-pane-note">本集尚未解析资产：解析后本集新增的资产卡会显示在这里。</div>
      )}
    </div>
  );
}
