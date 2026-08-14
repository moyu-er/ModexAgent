// Shared bits for the graph views: status → filled-chip badge (PRD §6.4:
// .graph-badge-* classes carry text/border/bg from the --color-graph-status-*
// tokens), status → i18n label key, and ApiError detail formatting (backend
// 400s carry {"error": ..., "detail": ...}).

import type { FC } from "react";
import { ApiError } from "../../lib/api";
import type { MessageKey } from "../../i18n";
import type { GraphNodeStatus } from "../../lib/graphsApi";
import type { GraphNodeVisualStatus } from "./topology/GraphNode";

// API 状态 → 视觉 chip:paused→suspended(amber)、failed→crashed(red)、
// stopped→canceled(删除线灰);未知状态回落 pending 灰阶 chip。
const STATUS_CLS: Record<string, string> = {
  pending: "graph-badge-pending",
  running: "graph-badge-running",
  paused: "graph-badge-suspended",
  stopped: "graph-badge-canceled",
  crashed: "graph-badge-crashed",
  completed: "graph-badge-completed",
  failed: "graph-badge-crashed",
};

const STATUS_LABEL_KEYS: Record<string, MessageKey> = {
  pending: "graphs.statusPending",
  running: "graphs.statusRunning",
  paused: "graphs.statusPaused",
  stopped: "graphs.statusStopped",
  crashed: "graphs.statusCrashed",
  completed: "graphs.statusCompleted",
  failed: "graphs.statusFailed",
};

export function statusLabelKey(status: string): MessageKey {
  return STATUS_LABEL_KEYS[status] ?? "graphs.status";
}

export const GraphStatusBadge: FC<{ status: string; label: string }> = ({
  status,
  label,
}) => {
  const cls = STATUS_CLS[status] ?? "graph-badge-pending";
  return (
    <span
      className={`inline-flex items-center rounded-sm border px-1.5 py-0.5 font-mono text-xs ${cls}`}
    >
      {label}
    </span>
  );
};

// API 节点状态 → 视觉状态(TopologyCanvas/MiniTopology 共用的映射)。
function toVisualStatus(status: string): GraphNodeVisualStatus {
  switch (status) {
    case "running":
      return "running";
    case "completed":
      return "completed";
    case "crashed":
      return "crashed";
    case "canceled":
    case "cancelled":
      return "canceled";
    case "suspended":
      return "suspended";
    default:
      return "pending";
  }
}

/** node 名 → 视觉状态 map(MiniTopology 的 nodeStatuses 入参)。 */
export function buildNodeStatusMap(
  nodes: GraphNodeStatus[],
): Record<string, GraphNodeVisualStatus> {
  const map: Record<string, GraphNodeVisualStatus> = {};
  for (const n of nodes) {
    map[n.node_name] = toVisualStatus(n.status);
  }
  return map;
}

/**
 * Human-readable error line for graph REST failures. Backend validation 400s
 * return a JSON body {"error": ..., "detail": ...}; prefer those fields over
 * the raw body when parseable.
 */
export function formatGraphApiError(err: unknown): string {
  if (err instanceof ApiError && err.detail) {
    try {
      const body = JSON.parse(err.detail) as { error?: string; detail?: unknown };
      if (body.error) {
        const detail =
          typeof body.detail === "string"
            ? body.detail
            : body.detail !== undefined
              ? JSON.stringify(body.detail)
              : "";
        return detail ? `${body.error}: ${detail}` : body.error;
      }
    } catch {
      // Not JSON — fall through to the generic message.
    }
  }
  return err instanceof Error ? err.message : String(err);
}
