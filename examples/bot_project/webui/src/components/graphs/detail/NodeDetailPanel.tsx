// NodeDetailPanel — sidebar panel for a selected graph node (PRD §6.1 D).
//
// Shows the node's name, type + pool, status badge, node_id (from the
// instance's runtime node list), and an "Open session" button for agent
// nodes. When ``result`` is present (completed nodes with output, §11.4),
// the node's deliver content summary is shown in a mono text block.

import { ExternalLink } from "lucide-react";
import type { FC } from "react";
import { useT } from "../../../i18n";
import { Button } from "../../ui/Button";
import { GraphStatusBadge } from "../shared";
import type { GraphNodeVisualStatus } from "../topology/GraphNode";

export interface NodeDetailPanelProps {
  nodeName: string;
  nodeType: string;
  pool?: string;
  status: GraphNodeVisualStatus;
  statusLabel: string;
  nodeId?: string;
  /** Node output summary (§11.4) — populated for completed nodes with result. */
  result?: { content: string } | null;
  /** Whether this node is an agent node (can open session). */
  isAgent: boolean;
  onOpenSession: () => void;
}

export const NodeDetailPanel: FC<NodeDetailPanelProps> = ({
  nodeName,
  nodeType,
  pool,
  status,
  statusLabel,
  nodeId,
  result,
  isAgent,
  onOpenSession,
}) => {
  const t = useT();
  const sub = pool ? `${nodeType} · ${pool}` : nodeType;

  return (
    <div data-testid="node-detail-panel" className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-base font-medium text-ink">{nodeName}</span>
        <GraphStatusBadge status={status} label={statusLabel} />
      </div>
      <div className="font-mono text-xs text-faint">{sub}</div>
      {nodeId ? (
        <div className="flex items-center gap-1.5 font-mono text-xs text-faint">
          <span>{t("graphs.invocationId")}:</span>
          <span>{nodeId}</span>
        </div>
      ) : null}
      {result ? (
        <div className="flex flex-col gap-1">
          <span className="font-mono text-xs text-faint">{t("graphs.resultLabel")}</span>
          <pre
            data-testid="node-result"
            className="max-h-32 overflow-y-auto whitespace-pre-wrap break-words rounded-md bg-canvas-elevated p-2 font-mono text-xs text-body"
          >
            {result.content}
          </pre>
        </div>
      ) : null}
      {isAgent ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={onOpenSession}
          className="gap-1.5 -ml-2 w-fit"
        >
          <ExternalLink size={14} />
          {t("graphs.openSession")}
        </Button>
      ) : null}
    </div>
  );
};
