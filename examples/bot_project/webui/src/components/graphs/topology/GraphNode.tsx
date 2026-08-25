/**
 * GraphNode.tsx — 单个图节点的 SVG 渲染(graph PRD §5.2, §6 Rev 4)。
 *
 * - **状态只由节点内圆点颜色表达**(§6 Rev 4 dot-only):节点本体
 *   fill/描边恒定,不随状态变化 — 状态着色只出现在右侧 10px 实心圆点上,
 *   六态六色(灰/teal/绿/红/琥珀/紫),右上角图例同色映射。
 * - 功能节点 140×44 圆角矩形:左 lucide SVG 类型图标(14px, text-mute)、
 *   中 name(Inter 500 text-sm ink,超长截断 + <title> 完整名)、
 *   sub(类型 + pool, mono text-xs text-mute)、右状态圆点。
 * - START/END 为 76×30 幽灵药丸(brand 微 tint 填充 + brand 40% 描边 +
 *   brand 文字)— 终端节点视觉降权,功能节点才是主角。
 * - running 额外保留 ActiveNodeRing 外扩呼吸环(节点本体颜色不变,
 *   动效只是运行提示);crash flash 由 TopologyCanvas 渲染(瞬态)。
 * - **agent 节点单击即跳转会话**(onOpen);非 agent 功能节点单击选中(onSelect)。
 */
import { useState, type FC, type KeyboardEvent, type MouseEvent } from "react";
import { Bot, Boxes, Braces, Layers, Timer, User, Workflow, type LucideIcon } from "lucide-react";
import {
  GRAPH_NODE_END,
  GRAPH_NODE_START,
  type ParsedNode,
} from "../yaml/parseGraphSpec";
import type { LayoutNodeRect } from "./layout";

/** 节点运行状态(§5.2;由实例节点状态映射而来)。 */
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

/** 功能节点类型 → lucide 图标(§5.2 Rev 4:SVG 图标替代 unicode glyph,
 * 跨平台渲染稳定)。START/END 无图标。 */
export const NODE_TYPE_ICONS: Readonly<Record<string, LucideIcon>> = {
  agent: Bot,
  function: Braces,
  delay: Timer,
  human_input: User,
  graph: Workflow,
  // Scope 声明层级(票据16):workspace=层叠容器,pool=池(与设置导航同图标)。
  workspace: Layers,
  pool: Boxes,
};

/** 判断是否为结构性端点(START/END) — 小药丸,无图标/sub/dot。 */
export function isEndpointNode(nodeType: string): boolean {
  return nodeType === GRAPH_NODE_START || nodeType === GRAPH_NODE_END;
}

/** START/END 节点显示名映射(内部名 → 用户友好标签)。 */
const ENDPOINT_DISPLAY_NAMES: Readonly<Record<string, string>> = {
  [GRAPH_NODE_START]: "START",
  [GRAPH_NODE_END]: "END",
};

// 节点内部布局(局部坐标,节点中心为原点):padding 12,icon 14,gap 6,
// status dot 10 + gap 8 → name 可用宽度 80px;text-sm(12px Inter)平均字宽
// 约 6.6px → 12 字符截断。sub 行与 dot 无垂直交叠,可用满宽 96px;
// text-xs(11px JetBrains Mono)字宽恒为 0.6em=6.6px → 14 字符截断。
const CONTENT_PAD = 12;
const ICON_SIZE = 14;
const LABEL_GAP = 6;
const DOT_SIZE = 10;
const NAME_MAX_CHARS = 12;
const SUB_MAX_CHARS = 14;

/** §6.2 Rev 4 状态 → 圆点填充色(节点本体不随状态变化)。 */
const STATUS_DOTS: Readonly<Record<GraphNodeVisualStatus, string>> = {
  pending: "fill-graph-status-pending",
  running: "fill-graph-status-running",
  completed: "fill-graph-status-completed",
  crashed: "fill-graph-status-crashed",
  canceled: "fill-graph-status-canceled",
  suspended: "fill-graph-status-suspended",
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
  // 选中高亮(§8.1)与键盘 focus ring 共用 brand 描边。
  // agent 节点单击跳转,不会被选中;endpoint 不可交互。
  const highlighted = !isEndpoint && !isAgent && (selected || focused);

  const Icon = NODE_TYPE_ICONS[node.nodeType] ?? null;
  // START/END 显示友好标签 "START"/"END"
  const nameText = isEndpoint
    ? (ENDPOINT_DISPLAY_NAMES[node.name] ?? node.name)
    : truncateLabel(node.name);
  const subText = isEndpoint
    ? null
    : truncateLabel(
        node.config.pool ? `${node.nodeType} · ${node.config.pool}` : node.nodeType,
        SUB_MAX_CHARS,
      );

  const labelX = -width / 2 + CONTENT_PAD + ICON_SIZE + LABEL_GAP;
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
      {isEndpoint ? (
        // START/END 幽灵药丸(§5.2 Rev 4):全圆角,brand 微 tint + 描边。
        <rect
          data-node-body=""
          x={-width / 2}
          y={-height / 2}
          width={width}
          height={height}
          rx={height / 2}
          strokeWidth={1.25}
          className="fill-graph-endpoint-fill stroke-graph-endpoint-border transition-colors duration-fast ease-out"
        />
      ) : (
        // 功能节点:本体恒定(fill + border-strong + 阴影),状态只在 dot 上。
        <rect
          data-node-body=""
          x={-width / 2}
          y={-height / 2}
          width={width}
          height={height}
          rx={GRAPH_NODE_RADIUS}
          strokeWidth={highlighted ? 2 : 1.25}
          className={`fill-graph-node-fill ${highlighted ? "stroke-graph-node-border-active" : "stroke-graph-node-border"} transition-colors duration-fast ease-out`}
          style={{ filter: "var(--filter-graph-node)" }}
        />
      )}
      {Icon !== null && (
        <Icon
          x={-width / 2 + CONTENT_PAD}
          y={-ICON_SIZE / 2}
          width={ICON_SIZE}
          height={ICON_SIZE}
          strokeWidth={1.75}
          className="text-mute"
          aria-hidden="true"
        />
      )}
      {isEndpoint ? (
        // START/END:标签居中显示,mono font-semibold(标签化视觉)
        <text
          x={0}
          y={0}
          textAnchor="middle"
          dominantBaseline="central"
          className="fill-current font-mono text-xs font-semibold tracking-wide text-graph-endpoint-text"
        >
          {nameText}
        </text>
      ) : (
        // 功能节点:名称左对齐 + sub 行 + 状态圆点
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
              className="fill-current font-mono text-xs text-mute"
            >
              {subText}
            </text>
          )}
          <circle
            data-status-dot=""
            cx={dotCx}
            cy={0}
            r={DOT_SIZE / 2}
            className={STATUS_DOTS[status]}
          />
        </>
      )}
    </g>
  );
};
