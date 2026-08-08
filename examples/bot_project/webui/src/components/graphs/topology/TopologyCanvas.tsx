/**
 * TopologyCanvas.tsx — 拓扑画布容器(graph PRD §5.4 + §6.1 B 区)。
 *
 * - 无 SVG viewBox:用户坐标系 = 像素坐标系,只有 <g transform> 一层缩放,
 *   避免 viewBox meet 模式与 transform 叠加导致节点过大。
 * - 初始 fit-to-screen: 自动缩放到容器,但不超过 1.0x(节点不被放大)。
 * - 滚轮缩放 0.5x–2x(以光标为中心)+ 指针拖拽平移。
 * - 右上角叠加状态图例。
 * - agent 节点单击 → onOpenSession;非 agent 单击 → onSelectNode;空白点击取消选中。
 */
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FC,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useT } from "../../../i18n";
import type { ParsedGraphTopology } from "../yaml/parseGraphSpec";
import {
  edgeKey,
  layoutGraph,
  type LayoutPoint,
  type LayoutResult,
} from "./layout";
import { GraphEdge } from "./GraphEdge";
import { GraphNode, type GraphNodeVisualStatus } from "./GraphNode";
import { DeliverPulse } from "./DeliverPulse";
import { ActiveNodeRing } from "./ActiveNodeRing";

export interface ViewTransform {
  scale: number;
  tx: number;
  ty: number;
}

export const MIN_ZOOM = 0.5;
export const MAX_ZOOM = 2;
/** 区分点击与拖拽的位移阈值(屏幕 px)。 */
const DRAG_THRESHOLD_PX = 4;
/** 内容四周留白(px,用户坐标系)。 */
const CONTENT_PAD = 48;

export function clampZoom(scale: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, scale));
}

/** 以 (cx, cy)(像素坐标)为不动点缩放。 */
export function zoomAt(
  t: ViewTransform,
  cx: number,
  cy: number,
  nextScale: number,
): ViewTransform {
  const scale = clampZoom(nextScale);
  return {
    scale,
    tx: t.tx + cx * (t.scale - scale),
    ty: t.ty + cy * (t.scale - scale),
  };
}

/** 平移(d 为像素位移)。 */
export function panBy(t: ViewTransform, dx: number, dy: number): ViewTransform {
  return { ...t, tx: t.tx + dx, ty: t.ty + dy };
}

/** dagre 布局的内容包围盒(用户坐标)。 */
interface ContentBounds {
  minX: number;
  minY: number;
  width: number;
  height: number;
}

function computeContentBounds(layout: LayoutResult): ContentBounds | null {
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
  // Include edge points in bounds (loop edges may extend beyond nodes).
  for (const edge of layout.edges.values()) {
    for (const p of edge.points) {
      minX = Math.min(minX, p.x);
      minY = Math.min(minY, p.y);
      maxX = Math.max(maxX, p.x);
      maxY = Math.max(maxY, p.y);
    }
  }
  if (!Number.isFinite(minX)) return null;
  return {
    minX,
    minY,
    width: maxX - minX + CONTENT_PAD * 2,
    height: maxY - minY + CONTENT_PAD * 2,
  };
}

/** deliver 脉冲信号(§4.3)。 */
export interface PulseSignal {
  id: number;
  edgeKey: string;
  points: LayoutPoint[];
}

// 保留导出供测试(原 computeViewBox 已移除,用 computeContentBounds 替代)
export interface CanvasViewBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function computeViewBox(layout: LayoutResult): CanvasViewBox {
  const b = computeContentBounds(layout);
  if (!b) return { x: 0, y: 0, width: 100, height: 100 };
  return { x: b.minX - CONTENT_PAD, y: b.minY - CONTENT_PAD, width: b.width, height: b.height };
}

export interface TopologyCanvasProps {
  topology: ParsedGraphTopology;
  nodeStatuses?: Readonly<Record<string, GraphNodeVisualStatus>>;
  activeEdges?: ReadonlySet<string>;
  pulses?: ReadonlyArray<PulseSignal>;
  onPulseComplete?: (pulseId: number) => void;
  selectedNodeId?: string | null;
  onSelectNode?: (nodeName: string | null) => void;
  onOpenSession?: (nodeName: string) => void;
  className?: string;
}

export const TopologyCanvas: FC<TopologyCanvasProps> = ({
  topology,
  nodeStatuses,
  activeEdges,
  pulses,
  onPulseComplete,
  selectedNodeId = null,
  onSelectNode,
  onOpenSession,
  className,
}) => {
  const t = useT();
  const layout = useMemo(() => layoutGraph(topology), [topology]);
  const bounds = useMemo(() => computeContentBounds(layout), [layout]);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ lastX: number; lastY: number; moved: boolean } | null>(
    null,
  );

  // 初始 fit-to-screen: 只有一层 transform 缩放,不超过 1.0x(节点不被放大)。
  const [view, setView] = useState<ViewTransform>({ scale: 1, tx: 0, ty: 0 });
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg || !bounds) return;
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    // fit scale: 内容缩放到容器,但不超过 1.0(不放大节点)
    const fitScale = clampZoom(
      Math.min(1.0, rect.width / bounds.width, rect.height / bounds.height),
    );
    // 居中偏移: 使内容在容器中居中
    const tx = (rect.width - bounds.width * fitScale) / 2 - (bounds.minX - CONTENT_PAD) * fitScale;
    const ty = (rect.height - bounds.height * fitScale) / 2 - (bounds.minY - CONTENT_PAD) * fitScale;
    setView({ scale: fitScale, tx, ty });
  }, [bounds?.width, bounds?.height, bounds?.minX, bounds?.minY]);

  // 滚轮缩放: 光标位置 = 像素坐标,直接用于 zoomAt。
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = svg.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      const factor = Math.exp(-e.deltaY * 0.0015);
      setView((v) => zoomAt(v, cx, cy, v.scale * factor));
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, []);

  const handlePointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    dragRef.current = { lastX: e.clientX, lastY: e.clientY, moved: false };
    window.addEventListener("pointermove", handleWindowPointerMove);
    window.addEventListener("pointerup", handleWindowPointerUp, { once: true });
  };

  const handleWindowPointerMove = (e: PointerEvent) => {
    const drag = dragRef.current;
    if (!drag) return;
    const dx = e.clientX - drag.lastX;
    const dy = e.clientY - drag.lastY;
    if (!drag.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
    drag.moved = true;
    drag.lastX = e.clientX;
    drag.lastY = e.clientY;
    // dx/dy 是像素位移,直接用于 panBy(用户坐标系 = 像素坐标系)
    setView((v) => panBy(v, dx, dy));
  };

  const handleWindowPointerUp = (e: PointerEvent) => {
    const drag = dragRef.current;
    dragRef.current = null;
    window.removeEventListener("pointermove", handleWindowPointerMove);
    if (drag && !drag.moved && e.target === svgRef.current) {
      onSelectNode?.(null);
    }
  };

  return (
    <div
      className={`relative min-h-[400px] overflow-hidden ${className ?? ""}`}
      data-testid="topology-canvas"
      style={{
        backgroundImage:
          "radial-gradient(var(--color-hairline-soft) 1px, transparent 1px)",
        backgroundSize: "16px 16px",
      }}
    >
      {/* 无 viewBox: 用户坐标系 = 像素坐标系,只有 <g transform> 一层缩放 */}
      <svg
        ref={svgRef}
        className="absolute inset-0 h-full w-full touch-none select-none"
        role="group"
        aria-label={topology.name}
        onPointerDown={handlePointerDown}
      >
        <g
          data-testid="topology-viewport"
          transform={`translate(${view.tx} ${view.ty}) scale(${view.scale})`}
        >
          {topology.edges.map((edge) => {
            const laid = layout.edges.get(edgeKey(edge.source, edge.target));
            if (!laid) return null;
            const key = edgeKey(edge.source, edge.target);
            return (
              <GraphEdge
                key={key}
                source={edge.source}
                target={edge.target}
                points={laid.points}
                active={activeEdges?.has(key) ?? false}
              />
            );
          })}
          {pulses?.map((pulse) => (
            <DeliverPulse
              key={pulse.id}
              points={pulse.points}
              onComplete={() => onPulseComplete?.(pulse.id)}
            />
          ))}
          {topology.nodes.map((node) => {
            const rect = layout.nodes.get(node.name);
            if (!rect) return null;
            return (
              <GraphNode
                key={node.name}
                node={node}
                rect={rect}
                status={nodeStatuses?.[node.name] ?? "pending"}
                selected={selectedNodeId === node.name}
                onSelect={
                  onSelectNode ? (name) => onSelectNode(name) : undefined
                }
                onOpen={onOpenSession}
              />
            );
          })}
          {topology.nodes.map((node) => {
            if (nodeStatuses?.[node.name] !== "running") return null;
            const rect = layout.nodes.get(node.name);
            if (!rect) return null;
            return (
              <ActiveNodeRing
                key={`ring-${node.name}`}
                width={rect.width}
                height={rect.height}
                cx={rect.x}
                cy={rect.y}
              />
            );
          })}
        </g>
      </svg>
      <div
        className="pointer-events-none absolute right-3 top-3 font-mono text-xs text-faint"
        data-testid="graph-canvas-legend"
      >
        ● {t("graphs.legendCompleted")} · ◎ {t("graphs.legendRunning")} · ●{" "}
        {t("graphs.legendCrashed")} · ○ {t("graphs.legendPending")}
      </div>
    </div>
  );
};
