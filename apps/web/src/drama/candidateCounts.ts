/**
 * 一次抽卡张数的可选值（生图侧通用：首帧 / 场景主图，后续角色、道具同语义）。
 *
 * **与后端同源**：上限 4 由 `server/app/api/drama.py` 的 `T2I_MAX_CANDIDATES`
 * 决定（runner 侧同值截断），这里只暴露到 4——前端给不出后端不接的值。
 * 抽卡的用途：资产图/首帧这类"好不好看"没有确定答案的产物，一次生成多张、
 * 用户挑一张，比反复点「重新生成」省时省钱（每次点击都是一次完整调用）。
 */
export const CANDIDATE_COUNTS = [1, 2, 3, 4] as const;

/** 默认张数（不额外花钱：与改造前行为一致） */
export const DEFAULT_CANDIDATE_COUNT = 1;
