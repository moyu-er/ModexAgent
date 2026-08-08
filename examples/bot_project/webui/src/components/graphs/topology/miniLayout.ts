/**
 * miniLayout.ts — MiniTopology 的纯布局逻辑(graph PRD §5.5),与 SVG 渲染分离。
 *
 * - ≤8 节点:复用 dagre 布局(layoutGraph),坐标等比折算进 80×24 盒子。
 * - >8 节点(折叠):保留 START/END 与首末功能节点,中间链折叠为 `···`
 *   三点刻度 — 等距水平排布,fold 位置由 foldDots 表达,段为相邻展示项
 *   的直线连接。结构可读性优先于完整性。
 */
import {
  GRAPH_NODE_END,
  GRAPH_NODE_START,
  type ParsedGraphTopology,
  type ParsedNode,
} from "../yaml/parseGraphSpec";
import { layoutGraph } from "./layout";

export const MINI_WIDTH = 80;
export const MINI_HEIGHT = 24;
/** 功能节点 6px 圆(§5.5)。 */
export const MINI_NODE_R = 3;
/** 节点数超过该值时走折叠规则(§5.5 Rev 2 修正 7)。 */
export const MINI_FOLD_THRESHOLD = 8;

export type MiniNodeKind = "start" | "end" | "functional";

export interface MiniNodePoint {
  name: string;
  kind: MiniNodeKind;
  x: number;
  y: number;
}

export interface MiniSegment {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface MiniLayoutResult {
  nodes: MiniNodePoint[];
  /** 折叠三点刻度坐标(仅 >8 节点时非空)。 */
  foldDots: { x: number; y: number }[] | null;
  segments: MiniSegment[];
}

function kindOf(node: ParsedNode): MiniNodeKind {
  if (node.nodeType === GRAPH_NODE_START) return "start";
  if (node.nodeType === GRAPH_NODE_END) return "end";
  return "functional";
}

// ≤8 节点时的折算留白:圆点 r=3,x/y 各留 6/6 → x∈[6,74],y∈[6,18]。
const PAD_X = 6;
const PAD_Y = 6;
// 折叠模式水平排布范围。
const FOLD_MIN_X = 10;
const FOLD_MAX_X = 70;
/** 折叠三点刻度的点间距。 */
const FOLD_DOT_GAP = 5;

function layoutFull(topology: ParsedGraphTopology): MiniLayoutResult {
  const layout = layoutGraph(topology);
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const rect of layout.nodes.values()) {
    minX = Math.min(minX, rect.x - rect.width / 2);
    minY = Math.min(minY, rect.y - rect.height / 2);
    maxX = Math.max(maxX, rect.x + rect.width / 2);
    maxY = Math.max(maxY, rect.y + rect.height / 2);
  }
  if (!Number.isFinite(minX)) {
    return { nodes: [], foldDots: null, segments: [] };
  }
  const spanX = maxX - minX;
  const spanY = maxY - minY;
  const scaleX = (v: number): number =>
    spanX > 0
      ? PAD_X + ((v - minX) / spanX) * (MINI_WIDTH - PAD_X * 2)
      : MINI_WIDTH / 2;
  const scaleY = (v: number): number =>
    spanY > 0
      ? PAD_Y + ((v - minY) / spanY) * (MINI_HEIGHT - PAD_Y * 2)
      : MINI_HEIGHT / 2;

  const points = new Map<string, MiniNodePoint>();
  for (const node of topology.nodes) {
    const rect = layout.nodes.get(node.name);
    if (!rect) continue;
    points.set(node.name, {
      name: node.name,
      kind: kindOf(node),
      x: Math.round(scaleX(rect.x) * 100) / 100,
      y: Math.round(scaleY(rect.y) * 100) / 100,
    });
  }

  const segments: MiniSegment[] = [];
  for (const edge of topology.edges) {
    const a = points.get(edge.source);
    const b = points.get(edge.target);
    if (!a || !b) continue;
    segments.push({ x1: a.x, y1: a.y, x2: b.x, y2: b.y });
  }
  return { nodes: [...points.values()], foldDots: null, segments };
}

function layoutFolded(topology: ParsedGraphTopology): MiniLayoutResult {
  const start = topology.nodes.find((n) => n.nodeType === GRAPH_NODE_START);
  const end = topology.nodes.find((n) => n.nodeType === GRAPH_NODE_END);
  const functional = topology.nodes.filter(
    (n) => kindOf(n) === "functional",
  );
  const first = functional[0];
  const last = functional[functional.length - 1];

  // 展示项:start? + 首个功能节点 + fold + 末个功能节点 + end?
  // (首末为同一节点时不重复,也不折叠 — >8 节点时不会出现,此处仅防御。)
  type Item = { kind: "node"; node: ParsedNode } | { kind: "fold" };
  const items: Item[] = [];
  if (start) items.push({ kind: "node", node: start });
  if (first) items.push({ kind: "node", node: first });
  if (first && last && first.name !== last.name) {
    items.push({ kind: "fold" }, { kind: "node", node: last });
  }
  if (end) items.push({ kind: "node", node: end });

  const midY = MINI_HEIGHT / 2;
  const step =
    items.length > 1 ? (FOLD_MAX_X - FOLD_MIN_X) / (items.length - 1) : 0;
  const nodes: MiniNodePoint[] = [];
  let foldDots: { x: number; y: number }[] | null = null;
  const centers: { x: number; y: number }[] = [];
  items.forEach((item, i) => {
    const x = Math.round((FOLD_MIN_X + step * i) * 100) / 100;
    centers.push({ x, y: midY });
    if (item.kind === "fold") {
      foldDots = [-FOLD_DOT_GAP, 0, FOLD_DOT_GAP].map((dx) => ({
        x: x + dx,
        y: midY,
      }));
    } else {
      nodes.push({ name: item.node.name, kind: kindOf(item.node), x, y: midY });
    }
  });

  const segments: MiniSegment[] = [];
  for (let i = 0; i + 1 < centers.length; i += 1) {
    const a = centers[i];
    const b = centers[i + 1];
    if (a && b) {
      segments.push({ x1: a.x, y1: a.y, x2: b.x, y2: b.y });
    }
  }
  return { nodes, foldDots, segments };
}

/** 计算 80×24 缩略图布局(节点数 > 8 时按 §5.5 折叠)。 */
export function computeMiniLayout(
  topology: ParsedGraphTopology,
): MiniLayoutResult {
  if (topology.nodes.length > MINI_FOLD_THRESHOLD) {
    return layoutFolded(topology);
  }
  return layoutFull(topology);
}
