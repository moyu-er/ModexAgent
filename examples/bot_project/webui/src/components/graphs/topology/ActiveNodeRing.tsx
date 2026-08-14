/**
 * ActiveNodeRing.tsx — running 节点的外扩同形圆角矩形描边脉动(§4.4)。
 *
 * - 形状: 节点外扩 4px 的同形圆角矩形(rect,不是圆环),
 *   几何由 ringSlotGeometry() 复用 G02 的计算。
 * - 描边: --color-graph-active-ring(brand 30%),stroke-width 2,fill none。
 * - 动画: 1.2s 循环(--dur-ring-pulse),ease-in-out,
 *   stroke-opacity 0.3 → 0.6 → 0.3(CSS @keyframes graph-ring-pulse)。
 * - 生命周期: 跟随节点 running 状态(TopologyCanvas 在节点 running 时渲染它,
 *   离开 running 时移除)。
 * - 降级: 静态 brand 40% alpha 描边(不脉动),由 CSS reduced-motion guard 处理。
 */
import { type FC } from "react";
import { ringSlotGeometry } from "./GraphNode";

export interface ActiveNodeRingProps {
  /** 节点宽度(140 for functional, 20 for start, 4 for end)。 */
  width: number;
  /** 节点高度(44 for functional, 20 for start/end)。 */
  height: number;
  /** 节点中心 x。 */
  cx: number;
  /** 节点中心 y。 */
  cy: number;
}

export const ActiveNodeRing: FC<ActiveNodeRingProps> = ({
  width,
  height,
  cx,
  cy,
}) => {
  const geo = ringSlotGeometry(width, height);
  return (
    <rect
      x={geo.x}
      y={geo.y}
      width={geo.width}
      height={geo.height}
      rx={geo.rx}
      fill="none"
      strokeWidth={2}
      stroke="var(--color-graph-active-ring)"
      className="graph-ring-pulse"
      transform={`translate(${cx} ${cy})`}
      data-testid="active-node-ring"
      pointerEvents="none"
    />
  );
};
