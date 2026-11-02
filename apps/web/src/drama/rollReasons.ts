/** 重摇原因（25 号 2.4）：用户重出一镜时**必须**说明不满意什么。
 *
 * 为什么必填：历史 188 条重摇记录里 evidence 全空、70% 只换了种子——等于只
 * 知道「又摇了一次」，不知道摇的靶子在哪。原因分布是后续优化的唯一依据。
 *
 * ⚠️ 取值必须与后端 `server/app/api/drama.py` 的 REROLL_REASONS **逐字一致**
 * （门禁 PASS 138 双向对账），改动两侧同时改。
 */
export const REROLL_REASON_POSE = "pose";
export const REROLL_REASON_IDENTITY = "identity";
export const REROLL_REASON_COMPOSITION = "composition";
export const REROLL_REASON_TEXT = "text";
export const REROLL_REASON_AUDIO = "audio";
export const REROLL_REASON_QUALITY = "quality";
export const REROLL_REASON_OTHER = "other";

/** 原因清单（顺序 = 界面展示顺序；label 给用户，hint 给判断） */
export const ROLL_REASONS = [
  { key: REROLL_REASON_IDENTITY, label: "人物不一致", hint: "串脸、双胞胎、换人、妆造不对" },
  { key: REROLL_REASON_POSE, label: "姿态/动作不对", hint: "站位、朝向、动作调度与描述不符" },
  { key: REROLL_REASON_COMPOSITION, label: "构图/景别", hint: "景别、机位、运镜、主体占比不对" },
  { key: REROLL_REASON_QUALITY, label: "画质缺陷", hint: "模糊、畸变、闪烁、糊脸、颜色崩" },
  { key: REROLL_REASON_AUDIO, label: "声音/口型", hint: "口型对不上、语速、音效缺失" },
  { key: REROLL_REASON_TEXT, label: "画面文字", hint: "招牌/字幕/乱码等穿帮文字" },
  { key: REROLL_REASON_OTHER, label: "其他", hint: "以上都不像——必须写一句话说明" },
] as const;

/** 原因码 → 展示名（未知码原样返回，便于排查后端新增了取值而前端没跟上）。
 *  2026-10-10 起原因是**多选**（逗号分隔存储如 "identity,pose"）：
 *  按「、」连接各段展示；单值行为不变。 */
export function rollReasonLabel(key: string): string {
  return (key || "")
    .split(",")
    .map((part) => {
      const p = part.trim();
      if (!p) return "";
      return ROLL_REASONS.find((r) => r.key === p)?.label ?? p;
    })
    .filter(Boolean)
    .join("、");
}
