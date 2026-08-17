/**
 * MiniTopology.tsx — 80×24px 固定拓扑缩略图(graph PRD §5.5)。
 *
 * - 节点:全部 6px 圆(统一形状,START/END 用 brand 色区分);边:1px hairline。
 * - 无文字、无交互 — 纯视觉签名(spec 列表 / 实例列表行内)。
 * - 可选状态着色(实例列表用):功能节点按 §5.2 状态色填充,无状态信息时
 *   回落 --color-graph-mini-node(faint);START/END 恒为 brand。
 * - >8 节点折叠:保留 START/END 与首末功能节点,中间链折叠为 `···`
 *   三点刻度(布局逻辑在 miniLayout.ts,纯函数可单测)。
 */
import { useMemo, type FC } from "react";
import type { ParsedGraphTopology } from "../yaml/parseGraphSpec";
import type { GraphNodeVisualStatus } from "./GraphNode";
import {
  computeMiniLayout,
  MINI_HEIGHT,
  MINI_NODE_R,
  MINI_WIDTH,
  type MiniNodePoint,
} from "./miniLayout";

/** 功能节点状态 → 填充色(§6.2 状态色系,缩略图无底色/边框通道)。 */
const MINI_STATUS_FILL: Readonly<Record<GraphNodeVisualStatus, string>> = {
  pending: "fill-graph-status-pending",
  running: "fill-graph-status-running",
  completed: "fill-graph-status-completed",
  crashed: "fill-graph-status-crashed",
  canceled: "fill-graph-status-canceled",
  suspended: "fill-graph-status-suspended",
};

export interface MiniTopologyProps {
  topology: ParsedGraphTopology;
  /** 节点名 → 运行状态(实例列表传入;spec 列表不传即纯结构)。 */
  nodeStatuses?: Readonly<Record<string, GraphNodeVisualStatus>>;
  className?: string;
}

function nodeFill(
  node: MiniNodePoint,
  nodeStatuses: Readonly<Record<string, GraphNodeVisualStatus>> | undefined,
): string {
  if (node.kind === "start") return "fill-graph-mini-start";
  if (node.kind === "end") return "fill-graph-mini-end";
  const status = nodeStatuses?.[node.name];
  return status ? MINI_STATUS_FILL[status] : "fill-graph-mini-node";
}

export const MiniTopology: FC<MiniTopologyProps> = ({
  topology,
  nodeStatuses,
  className,
}) => {
  const layout = useMemo(() => computeMiniLayout(topology), [topology]);
  return (
    <svg
      width={MINI_WIDTH}
      height={MINI_HEIGHT}
      viewBox={`0 0 ${MINI_WIDTH} ${MINI_HEIGHT}`}
      role="img"
      aria-label={topology.name}
      className={className}
      data-testid="mini-topology"
      data-folded={layout.foldDots !== null ? "true" : "false"}
    >
      {layout.segments.map((seg, i) => (
        <line
          key={`seg-${i}`}
          x1={seg.x1}
          y1={seg.y1}
          x2={seg.x2}
          y2={seg.y2}
          strokeWidth={1}
          className="stroke-graph-mini-edge"
        />
      ))}
      {layout.foldDots?.map((dot, i) => (
        <circle
          key={`fold-${i}`}
          data-fold-dot=""
          cx={dot.x}
          cy={dot.y}
          r={0.5}
          className="fill-graph-mini-edge"
        />
      ))}
      {layout.nodes.map((node) => (
        <circle
          key={node.name}
          data-mini-node={node.name}
          cx={node.x}
          cy={node.y}
          r={MINI_NODE_R}
          className={nodeFill(node, nodeStatuses)}
        />
      ))}
    </svg>
  );
};
