/** 组框节点（4.10）：成员容器的可视外框，无端口不可连线，拖动时由画布同步移动成员。 */

import { NodeProps } from "@xyflow/react";
import { useCanvas } from "../stores/canvas";

export function GroupNode({ id }: NodeProps) {
  const group = useCanvas((s) => s.groups[id]);
  if (!group) return null;
  return (
    <div
      className="group-box"
      style={{ borderColor: group.color, background: `${group.color}12` }}
    >
      <div className="group-label" style={{ background: group.color }}>
        {group.name}
      </div>
    </div>
  );
}
