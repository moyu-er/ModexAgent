/**
 * GraphEdge.tsx — 单条边的 SVG 渲染(graph PRD §5.3, §5.3 Rev 4)。
 *
 * - 边 = --color-graph-edge(节点描边的 70% 混合)1.5px stroke — Rev 4
 *   层级翻转:边退到节点之下,节点本体才是视觉主角。
 * - 路径取 dagre 布局 points,经 roundedPathD 做**圆角折线**处理
 *   (每个拐角以 radius 半径的二次曲线过渡,直角硬拐不再出现)。
 * - 箭头 <marker> 与边同色、7px,teal 保留给活跃/流动语义,不做常亮。
 * - 高亮态(active,deliver 脉冲经过时):stroke 2px + 箭头切换为
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
  /** deliver 高亮态(§5.3):边与箭头切换为 brand 70%。 */
  active?: boolean;
}

const round = (n: number): number => Math.round(n * 100) / 100;

/**
 * points → 圆角折线 SVG path d(Rev 4)。
 *
 * 内部拐角以二次贝塞尔(Q)圆角过渡:进入拐点前沿直线走到 `radius` 前,
 * 以拐点为控制点弯到离开方向 `radius` 处。radius 被相邻段长的一半钳制,
 * 保证短段不被吃光;共线拐角退化为直线点。
 */
export function roundedPathD(points: LayoutPoint[], radius = 10): string {
  if (points.length < 2) return "";
  const first = points[0]!;
  let d = `M${round(first.x)} ${round(first.y)}`;
  for (let i = 1; i < points.length - 1; i++) {
    const prev = points[i - 1]!;
    const curr = points[i]!;
    const next = points[i + 1]!;
    const vInX = curr.x - prev.x;
    const vInY = curr.y - prev.y;
    const vOutX = next.x - curr.x;
    const vOutY = next.y - curr.y;
    const lenIn = Math.hypot(vInX, vInY);
    const lenOut = Math.hypot(vOutX, vOutY);
    if (lenIn === 0 || lenOut === 0) continue;
    const cross = vInX * vOutY - vInY * vOutX;
    if (Math.abs(cross) < 0.01) {
      d += ` L${round(curr.x)} ${round(curr.y)}`;
      continue;
    }
    const r = Math.min(radius, lenIn / 2, lenOut / 2);
    const pInX = curr.x - (vInX / lenIn) * r;
    const pInY = curr.y - (vInY / lenIn) * r;
    const pOutX = curr.x + (vOutX / lenOut) * r;
    const pOutY = curr.y + (vOutY / lenOut) * r;
    d += ` L${round(pInX)} ${round(pInY)} Q${round(curr.x)} ${round(curr.y)} ${round(pOutX)} ${round(pOutY)}`;
  }
  const last = points[points.length - 1]!;
  d += ` L${round(last.x)} ${round(last.y)}`;
  return d;
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
          viewBox="0 0 7 7"
          markerWidth={7}
          markerHeight={7}
          refX={6}
          refY={3.5}
          orient="auto"
          markerUnits="userSpaceOnUse"
        >
          <path
            d="M0 0.5L6.5 3.5L0 6.5Z"
            className={`${active ? "fill-graph-arrow-active" : "fill-graph-arrow"} transition-colors duration-app ease-out`}
          />
        </marker>
      </defs>
      <path
        d={roundedPathD(points)}
        fill="none"
        strokeWidth={active ? 2 : 1.5}
        markerEnd={`url(#${markerId})`}
        className={`${active ? "stroke-graph-edge-active" : "stroke-graph-edge"} transition-colors duration-app ease-out`}
      />
    </g>
  );
};
