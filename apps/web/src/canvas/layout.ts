/** 整理画布：Kahn 分层 + 横向/纵向布局（v0.2 4.11）。

横向展开：层级从左到右（分镜流，适合成片流程）
纵向展开：层级从上到下（参考链，适合资产打磨）
*/

import type { Edge, Node } from "@xyflow/react";
import type { StudioNodeData } from "../stores/canvas";

const NODE_W = 300;
const NODE_H = 360;
const GAP_X = 120;
const GAP_Y = 80;

/** 按连线层级重排节点位置（返回新数组，不改连线）。 */
export function autoLayout(
  nodes: Node<StudioNodeData>[],
  edges: Edge[],
  mode: "horizontal" | "vertical",
): Node<StudioNodeData>[] {
  // Kahn 分层：level = 最长上游路径
  const inDeg = new Map<string, number>();
  const adj = new Map<string, string[]>();
  const level = new Map<string, number>();
  for (const n of nodes) {
    inDeg.set(n.id, 0);
    adj.set(n.id, []);
    level.set(n.id, 0);
  }
  for (const e of edges) {
    if (!adj.has(e.source) || !inDeg.has(e.target)) continue;
    adj.get(e.source)!.push(e.target);
    inDeg.set(e.target, (inDeg.get(e.target) ?? 0) + 1);
  }
  const queue = nodes.filter((n) => (inDeg.get(n.id) ?? 0) === 0).map((n) => n.id);
  while (queue.length) {
    const id = queue.shift()!;
    const lv = level.get(id) ?? 0;
    for (const next of adj.get(id) ?? []) {
      level.set(next, Math.max(level.get(next) ?? 0, lv + 1));
      const d = (inDeg.get(next) ?? 1) - 1;
      inDeg.set(next, d);
      if (d === 0) queue.push(next);
    }
  }

  // 同层节点按当前相对顺序排列
  const byLevel = new Map<number, Node<StudioNodeData>[]>();
  const sorted = [...nodes].sort((a, b) => (a.position.y ?? 0) - (b.position.y ?? 0));
  for (const n of sorted) {
    const lv = level.get(n.id) ?? 0;
    if (!byLevel.has(lv)) byLevel.set(lv, []);
    byLevel.get(lv)!.push(n);
  }

  const result: Node<StudioNodeData>[] = [];
  for (const lv of [...byLevel.keys()].sort((a, b) => a - b)) {
    const group = byLevel.get(lv)!;
    group.forEach((n, i) => {
      if (mode === "horizontal") {
        result.push({
          ...n,
          position: { x: lv * (NODE_W + GAP_X), y: i * (NODE_H + GAP_Y) },
        });
      } else {
        result.push({
          ...n,
          position: { x: i * (NODE_W + GAP_X), y: lv * (NODE_H + GAP_Y) },
        });
      }
    });
  }
  return result;
}
