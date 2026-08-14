/**
 * layout.ts — dagre 布局封装(graph PRD §5.4/§9.2, §5.4 Rev 4)。
 *
 * 输入 `ParsedGraphTopology`(parseGraphSpec.ts),输出节点坐标与边路径。
 * 布局是数据,渲染是设计 — 本模块只产出坐标,不涉及任何视觉。
 *
 * 约定:
 * - 方向 TB(top-to-bottom),nodesep 56(水平),ranksep 76(垂直),
 *   edgesep 24 — Rev 4 放宽间距,给边曲线与节点呼吸空间。
 * - 功能节点统一 140×44;START/END 为 76×30 小药丸(终端节点降权,
 *   不再占用与功能节点相同的视觉体量)。
 * - 节点坐标为**中心点**(dagre 原生约定),渲染时按 width/height 自减一半。
 * - 回环边(如 reviewer→implementer)由 dagre 自然路由,无需特殊处理。
 * - 边 Map 的 key = `${source}-${target}`。
 */
import dagre from "@dagrejs/dagre";

import {
  GRAPH_NODE_END,
  GRAPH_NODE_START,
  type ParsedGraphTopology,
} from "../yaml/parseGraphSpec";

export interface LayoutPoint {
  x: number;
  y: number;
}

export interface LayoutNodeRect {
  /** 中心点坐标(dagre 约定)。 */
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LayoutEdge {
  points: LayoutPoint[];
}

export interface LayoutResult {
  nodes: Map<string, LayoutNodeRect>;
  /** key = `${source}-${target}` */
  edges: Map<string, LayoutEdge>;
}

export const GRAPH_NODE_SIZE = { width: 140, height: 44 } as const;
export const GRAPH_ENDPOINT_SIZE = { width: 76, height: 30 } as const;

export function edgeKey(source: string, target: string): string {
  return `${source}-${target}`;
}

function isEndpoint(nodeType: string): boolean {
  return nodeType === GRAPH_NODE_START || nodeType === GRAPH_NODE_END;
}

/** 计算拓扑的 dagre TB 布局。 */
export function layoutGraph(topology: ParsedGraphTopology): LayoutResult {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 56, ranksep: 76, edgesep: 24 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const node of topology.nodes) {
    const size = isEndpoint(node.nodeType)
      ? GRAPH_ENDPOINT_SIZE
      : GRAPH_NODE_SIZE;
    g.setNode(node.name, { width: size.width, height: size.height });
  }
  for (const edge of topology.edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  const nodes = new Map<string, LayoutNodeRect>();
  for (const node of topology.nodes) {
    const laid = g.node(node.name);
    nodes.set(node.name, {
      x: laid.x,
      y: laid.y,
      width: laid.width,
      height: laid.height,
    });
  }

  const edges = new Map<string, LayoutEdge>();
  for (const edge of topology.edges) {
    const laid = g.edge(edge.source, edge.target) as { points: LayoutPoint[] };
    edges.set(edgeKey(edge.source, edge.target), {
      points: laid.points.map((p) => ({ x: p.x, y: p.y })),
    });
  }

  return { nodes, edges };
}
