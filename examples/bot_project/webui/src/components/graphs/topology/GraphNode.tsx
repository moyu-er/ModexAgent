/**
 * GraphNode.tsx — 单个图节点的 SVG 渲染(graph PRD §5.2,Rev 4)。
 *
 * - **所有节点(含 START/END)统一为 140×44 圆角矩形** — 通过 glyph 和名称区分类型,
 *   不通过形状区分。START/END 显示友好标签 "START"/"END"(而非 `__start__`/`__end__`)。
 * - 功能节点内部: 左 glyph(20px, mono text-xs mute,附加 U+FE0E 强制文本呈现)、
 *   中 name(Inter 500 text-sm ink,超长截断 + <title> 完整名)+
 *   sub(类型 + pool, mono text-xs faint)、右 8px status dot。
 * - START/END 无 glyph、无 sub、无 status dot — 仅标签居中显示,brand fill 填充,
 *   字体 font-mono text-xs font-semibold(标签化视觉)。
 * - **agent 节点单击即跳转会话**(onOpen);非 agent 功能节点单击选中(onSelect)。
 * - 状态着色按 §5.2 表:running = 空心 dot + brand 边框 + 描边槽位;
 *   completed = 实心 dot + brand-soft 底色(双通道,动效降级下仍可分辨)。
 * - running 时渲染外扩 4px 同形圆角矩形描边槽位(data-ring-slot,无 stroke)
 *   — 几何由 ringSlotGeometry() 导出,G03 的 ActiveNodeRing 复用同一几何。
 */
import { useState, type FC, type KeyboardEvent, type MouseEvent } from "react";
import {
  GRAPH_NODE_END,
  GRAPH_NODE_START,
  type ParsedNode,
} from "../yaml/parseGraphSpec";
import type { LayoutNodeRect } from "./layout";

/** 节点运行状态(§5.2 状态着色表的键;由实例节点状态映射而来)。 */
export type GraphNodeVisualStatus =
  | "pending"
  | "running"
  | "completed"
  | "crashed"
  | "canceled"
  | "suspended";

/** --radius-md = 12px(SVG 几何属性不能用 var(),此处取 token 数值)。 */
export const GRAPH_NODE_RADIUS = 12;
/** 活跃描边外扩距离(§4.4)。 */
export const RING_SLOT_OUTSET = 4;

/** U+FE0E variation selector — 强制 glyph 文本呈现(§5.2 Rev 2 修正 4)。 */
const FE0E = "\uFE0E";

/** 功能节点类型 → glyph(§5.2 类型 glyph 表)。START/END 无 glyph。 */
export const NODE_TYPE_GLYPHS: Readonly<Record<string, string>> = {
  agent: `◉${FE0E}`,
  function: `ƒ${FE0E}`,
  delay: `◷${FE0E}`,
  human_input: `⏸${FE0E}`,
  graph: `⬕${FE0E}`,
};

/** 判断是否为结构性端点(START/END) — 统一形状但无 glyph/sub/dot。 */
export function isEndpointNode(nodeType: string): boolean {
  return nodeType === GRAPH_NODE_START || nodeType === GRAPH_NODE_END;
}

/** START/END 节点显示名映射(内部名 → 用户友好标签)。 */
const ENDPOINT_DISPLAY_NAMES: Readonly<Record<string, string>> = {
  [GRAPH_NODE_START]: "START",
  [GRAPH_NODE_END]: "END",
};

// 节点内部布局(局部坐标,节点中心为原点):padding 12,glyph 20,gap 4,
// status dot 8 + gap 8 → name 可用宽度 76px;text-sm(12px Inter)平均字宽
// 约 6.6px → 11 字符截断。sub 行与 dot 无垂直交叠,可用满宽 92px;
// text-xs(11px JetBrains Mono)字宽恒为 0.6em=6.6px → 14 字符截断。
// START/END 标签居中:可用宽度 = 140 - 2*12 = 116px → 17 字符截断。
const CONTENT_PAD = 12;
const GLYPH_WIDTH = 20;
const LABEL_GAP = 4;
const DOT_SIZE = 8;
const NAME_MAX_CHARS = 11;
const SUB_MAX_CHARS = 14;
const ENDPOINT_NAME_MAX_CHARS = 17;

interface StatusStyle {
  /** dot 填充(hollow 时忽略,改用描边)。 */
  readonly dotFill: string;
  readonly dotHollow: boolean;
  readonly bodyFill: string;
  readonly bodyStroke: string;
  readonly dashed: boolean;
}

/** §5.2 状态着色表(Rev 2 双通道版,含"节点底色"列)。 */
const STATUS_STYLES: Readonly<Record<GraphNodeVisualStatus, StatusStyle>> = {
  pending: {
    dotFill: "fill-faint",
    dotHollow: false,
    bodyFill: "fill-graph-node-fill",
    bodyStroke: "stroke-graph-node-border",
    dashed: false,
  },
  running: {
    dotFill: "",
    dotHollow: true,
    bodyFill: "fill-graph-node-fill",
    bodyStroke: "stroke-brand",
    dashed: false,
  },
  completed: {
    dotFill: "fill-success",
    dotHollow: false,
    bodyFill: "fill-graph-node-fill-done",
    bodyStroke: "stroke-graph-node-border",
    dashed: false,
  },
  crashed: {
    dotFill: "fill-danger",
    dotHollow: false,
    bodyFill: "fill-graph-node-fill",
    bodyStroke: "stroke-danger",
    dashed: false,
  },
  canceled: {
    dotFill: "fill-mute",
    dotHollow: false,
    bodyFill: "fill-graph-node-fill",
    bodyStroke: "stroke-graph-node-border",
    dashed: false,
  },
  suspended: {
    dotFill: "fill-warning",
    dotHollow: false,
    bodyFill: "fill-graph-node-fill",
    bodyStroke: "stroke-warning",
    dashed: true,
  },
};

/** START/END 固定样式:brand 填充,无状态着色。 */
const ENDPOINT_STYLE: StatusStyle = {
  dotFill: "",
  dotHollow: false,
  bodyFill: "fill-brand",
  bodyStroke: "stroke-brand-deep",
  dashed: false,
};

/** 超长截断(SVG text 不支持 CSS ellipsis,按字符数保守截断)。 */
export function truncateLabel(text: string, maxChars: number = NAME_MAX_CHARS): string {
  return text.length <= maxChars ? text : `${text.slice(0, maxChars - 1)}…`;
}

export interface RingSlotGeometry {
  x: number;
  y: number;
  width: number;
  height: number;
  rx: number;
}

/**
 * 活跃描边槽位几何(§4.4):节点 rect 外扩 4px、radius 取 --radius-md + 4
 * 的同形圆角矩形,节点中心为原点。G03 的 ActiveNodeRing 复用此几何。
 */
export function ringSlotGeometry(width: number, height: number): RingSlotGeometry {
  return {
    x: -width / 2 - RING_SLOT_OUTSET,
    y: -height / 2 - RING_SLOT_OUTSET,
    width: width + RING_SLOT_OUTSET * 2,
    height: height + RING_SLOT_OUTSET * 2,
    rx: GRAPH_NODE_RADIUS + RING_SLOT_OUTSET,
  };
}

export interface GraphNodeProps {
  node: ParsedNode;
  /** dagre 布局结果(中心点坐标 + 尺寸)。 */
  rect: LayoutNodeRect;
  status?: GraphNodeVisualStatus;
  selected?: boolean;
  /** 单击 / Enter 选中(非 agent 功能节点)。 */
  onSelect?: (nodeName: string) => void;
  /** 单击 agent 节点 → 跳转会话。 */
  onOpen?: (nodeName: string) => void;
}

export const GraphNode: FC<GraphNodeProps> = ({
  node,
  rect,
  status = "pending",
  selected = false,
  onSelect,
  onOpen,
}) => {
  const [focused, setFocused] = useState(false);

  const { width, height } = rect;
  const isEndpoint = isEndpointNode(node.nodeType);
  const isAgent = node.nodeType === "agent";
  const style = isEndpoint ? ENDPOINT_STYLE : STATUS_STYLES[status];
  // 选中高亮(§8.1:150ms border-color)与键盘 focus ring 共用 brand 描边。
  // agent 节点单击跳转,不会被选中;endpoint 不可交互。
  const highlighted = !isEndpoint && !isAgent && (selected || focused);
  const bodyStroke = highlighted
    ? "stroke-graph-node-border-active"
    : style.bodyStroke;

  const glyph = NODE_TYPE_GLYPHS[node.nodeType] ?? null;
  // START/END 显示友好标签 "START"/"END"
  const nameText = isEndpoint
    ? (ENDPOINT_DISPLAY_NAMES[node.name] ?? truncateLabel(node.name, ENDPOINT_NAME_MAX_CHARS))
    : truncateLabel(node.name, NAME_MAX_CHARS);
  const subText = isEndpoint
    ? null
    : truncateLabel(
        node.config.pool ? `${node.nodeType} · ${node.config.pool}` : node.nodeType,
        SUB_MAX_CHARS,
      );

  const labelX = -width / 2 + CONTENT_PAD + GLYPH_WIDTH + LABEL_GAP;
  const dotCx = width / 2 - CONTENT_PAD - DOT_SIZE / 2;
  const ring = ringSlotGeometry(width, height);

  // 单击:agent 节点 → onOpen(跳转会话);其他功能节点 → onSelect(选中)
  const handleClick = (e: MouseEvent<SVGGElement>) => {
    e.stopPropagation();
    if (isAgent) {
      onOpen?.(node.name);
    } else {
      onSelect?.(node.name);
    }
  };
  const handleKeyDown = (e: KeyboardEvent<SVGGElement>) => {
    if (e.key === "Enter") {
      e.stopPropagation();
      if (isAgent) {
        onOpen?.(node.name);
      } else {
        onSelect?.(node.name);
      }
    }
  };

  const interactiveProps = isEndpoint
    ? {}
    : {
        role: "button" as const,
        tabIndex: 0,
        "aria-pressed": selected,
        onClick: handleClick,
        onKeyDown: handleKeyDown,
        onFocus: () => setFocused(true),
        onBlur: () => setFocused(false),
      };

  // cursor: agent 节点有 onOpen 时 pointer;非 agent 有 onSelect 时 pointer
  const cursor = isEndpoint
    ? "default"
    : (isAgent && onOpen) || (!isAgent && onSelect)
      ? "pointer"
      : "default";

  return (
    <g
      transform={`translate(${rect.x} ${rect.y})`}
      aria-label={node.name}
      data-testid={`graph-node-${node.name}`}
      data-node-type={node.nodeType}
      data-status={isEndpoint ? "endpoint" : status}
      className="outline-none"
      style={{ cursor }}
      {...interactiveProps}
    >
      <title>{node.name}</title>
      {/* G03 活跃描边槽位(§4.4):外扩 4px 同形圆角矩形,仅 running 时出现。
          此处只占位(无 stroke),脉动描边由 G03 ActiveNodeRing 渲染。 */}
      {status === "running" && !isEndpoint && (
        <rect
          data-ring-slot=""
          x={ring.x}
          y={ring.y}
          width={ring.width}
          height={ring.height}
          rx={ring.rx}
          fill="none"
          stroke="none"
          pointerEvents="none"
        />
      )}
      <rect
        data-node-body=""
        x={-width / 2}
        y={-height / 2}
        width={width}
        height={height}
        rx={GRAPH_NODE_RADIUS}
        strokeWidth={highlighted ? 2 : 1.5}
        strokeDasharray={style.dashed ? "5 3" : undefined}
        className={`${style.bodyFill} ${bodyStroke} transition-colors duration-fast ease-out`}
      />
      {glyph !== null && (
        <text
          x={-width / 2 + CONTENT_PAD}
          y={0}
          dominantBaseline="central"
          className="fill-current font-mono text-xs text-mute"
        >
          {glyph}
        </text>
      )}
      {isEndpoint ? (
        // START/END:标签居中显示,mono font-semibold(标签化视觉)
        <text
          x={0}
          y={0}
          textAnchor="middle"
          dominantBaseline="central"
          className="fill-current font-mono text-xs font-semibold text-on-brand"
        >
          {nameText}
        </text>
      ) : (
        // 功能节点:名称左对齐 + sub 行 + status dot
        <>
          <text
            x={labelX}
            y={-4}
            className="fill-current font-sans text-sm font-medium text-ink"
          >
            {nameText}
          </text>
          {subText !== null && (
            <text
              x={labelX}
              y={11}
              className="fill-current font-mono text-xs text-faint"
            >
              {subText}
            </text>
          )}
          {style.dotHollow ? (
            <circle
              data-status-dot=""
              cx={dotCx}
              cy={0}
              r={DOT_SIZE / 2}
              strokeWidth={1.5}
              className={`${style.bodyFill} stroke-brand`}
            />
          ) : (
            <circle
              data-status-dot=""
              cx={dotCx}
              cy={0}
              r={DOT_SIZE / 2}
              className={style.dotFill}
            />
          )}
        </>
      )}
    </g>
  );
};
