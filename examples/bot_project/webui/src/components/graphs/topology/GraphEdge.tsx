/**
 * GraphEdge.tsx — 单条边的 SVG 渲染(graph PRD §5.3)。
 *
 * - 边 = --color-graph-edge(border-strong)1.5px stroke,路径取 dagre
 *   布局给出的 points(折线/曲线控制点顺序连接)。
 * - 箭头 <marker> 与边同色、6px,teal 保留给活跃/流动语义,不做常亮。
 * - 高亮态(active,deliver 脉冲经过时):stroke + 箭头切换为
 *   --color-graph-edge-active / --color-graph-arrow-active,过渡 --dur。
 *
 * 每条边自带 <defs> 内的 marker(useId 保证 id 唯一),因此可脱离
 * TopologyCanvas 单独渲染。
 */
import { useId, type FC } from "react";
import { edgeKey, type LayoutPoint } from "./layout";

export interface GraphEdgeProps {
  source: string;
  target: string;
  /** dagre 边路径点(含首尾,顺序 source → target)。 */
  points: LayoutPoint[];
  /** deliver 高亮态(§5.3):边与箭头切换为 brand 60%。 */
  active?: boolean;
}

/** points → SVG path d(折线;dagre 回环曲线同样以点序列给出)。 */
export function edgePathD(points: LayoutPoint[]): string {
  const round = (n: number): number => Math.round(n * 100) / 100;
  return points
    .map((p, i) => `${i === 0 ? "M" : "L"}${round(p.x)} ${round(p.y)}`)
    .join(" ");
}

export const GraphEdge: FC<GraphEdgeProps> = ({
  source,
  target,
  points,
  active = false,
}) => {
  // useId 含 ":"(如 ":r1:"),不能直接做 SVG id/SVG url(#) 引用,去掉。
  const markerId = `graph-arrow-${useId().replace(/:/g, "")}`;
  if (points.length < 2) {
    return null;
  }
  return (
    <g data-testid={`graph-edge-${edgeKey(source, target)}`}>
      <defs>
        <marker
          id={markerId}
          viewBox="0 0 6 6"
          markerWidth={6}
          markerHeight={6}
          refX={6}
          refY={3}
          orient="auto"
          markerUnits="userSpaceOnUse"
        >
          <path
            d="M0 0L6 3L0 6Z"
            className={`${active ? "fill-graph-arrow-active" : "fill-graph-arrow"} transition-colors duration-app ease-out`}
          />
        </marker>
      </defs>
      <path
        d={edgePathD(points)}
        fill="none"
        strokeWidth={1.5}
        markerEnd={`url(#${markerId})`}
        className={`${active ? "stroke-graph-edge-active" : "stroke-graph-edge"} transition-colors duration-app ease-out`}
      />
    </g>
  );
};
