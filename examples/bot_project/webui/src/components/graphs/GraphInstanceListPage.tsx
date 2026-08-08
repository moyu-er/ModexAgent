// GraphInstanceListPage — graph instance list with an optional status filter
// (GET /api/graphs/instances?status=…). Each row renders a MiniTopology
// (status-colored from instance node states) + instance ID + spec name +
// status badge + progress (completed/total nodes). Clicking a row opens the
// execution viewer. (PRD §6.4)

import { useCallback, useEffect, useMemo, useState, type FC } from "react";
import { ArrowLeft, ChevronRight, Hash } from "lucide-react";
import {
  GRAPH_INSTANCE_STATUSES,
  getInstance,
  getSpec,
  listInstances,
  type GraphInstance,
  type GraphNodeStatus,
} from "../../lib/graphsApi";
import { parseGraphSpecYaml, type ParsedGraphTopology } from "./yaml/parseGraphSpec";
import { MiniTopology } from "./topology/MiniTopology";
import type { GraphNodeVisualStatus } from "./topology/GraphNode";
import { useT } from "../../i18n";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { SectionLabel } from "../ui/SectionLabel";
import { SelectMenu } from "../ui/SelectMenu";
import { formatGraphApiError, GraphStatusBadge } from "./shared";
import { statusLabelKey } from "./GraphExecutionViewer";

export interface GraphInstanceListPageProps {
  workspaceId: string;
  onOpenInstance: (instanceId: string) => void;
  onBack: () => void;
}

/** Spec id → parsed topology (shared across instances of the same spec). */
type TopologyMap = Record<string, ParsedGraphTopology | null>;

/** Instance id → node statuses (fetched from the detail endpoint). */
type NodeStatusMap = Record<string, GraphNodeStatus[]>;

/** Spec id → spec name (for display). */
type SpecNameMap = Record<string, string>;

/** Map backend node status string → GraphNodeVisualStatus for MiniTopology. */
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

function buildNodeStatusMap(
  nodes: GraphNodeStatus[],
): Record<string, GraphNodeVisualStatus> {
  const map: Record<string, GraphNodeVisualStatus> = {};
  for (const n of nodes) {
    map[n.node_name] = toVisualStatus(n.status);
  }
  return map;
}

export const GraphInstanceListPage: FC<GraphInstanceListPageProps> = ({
  workspaceId,
  onOpenInstance,
  onBack,
}) => {
  const t = useT();
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [instances, setInstances] = useState<GraphInstance[]>([]);
  const [topologies, setTopologies] = useState<TopologyMap>({});
  const [nodeStatuses, setNodeStatuses] = useState<NodeStatusMap>({});
  const [specNames, setSpecNames] = useState<SpecNameMap>({});
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback((): void => {
    setIsLoading(true);
    setError(null);
    setTopologies({});
    setNodeStatuses({});
    setSpecNames({});
    listInstances(workspaceId, statusFilter || undefined)
      .then((loaded) => {
        setInstances(loaded);

        // For each instance: fetch detail (for node statuses) + fetch the
        // associated spec's YAML (for topology). Spec fetches are de-duplicated
        // by spec_id — multiple instances may share the same spec.
        const seenSpecIds = new Set<string>();
        loaded.forEach((inst) => {
          // Fetch instance detail for node states (list endpoint returns
          // nodes: []).
          getInstance(workspaceId, inst.graph_instance_id)
            .then((detail) => {
              setNodeStatuses((prev) => ({
                ...prev,
                [inst.graph_instance_id]: detail.nodes,
              }));
            })
            .catch(() => {});

          // Fetch spec topology (de-duplicated by spec_id).
          const sid = inst.spec_id;
          if (sid && !seenSpecIds.has(sid)) {
            seenSpecIds.add(sid);
            getSpec(workspaceId, sid)
              .then((spec) => {
                let topo: ParsedGraphTopology | null;
                try {
                  topo = parseGraphSpecYaml(spec.yaml_content);
                } catch {
                  topo = null;
                }
                setTopologies((prev) => ({ ...prev, [sid]: topo }));
                setSpecNames((prev) => ({ ...prev, [sid]: spec.name }));
              })
              .catch(() => {
                setTopologies((prev) => ({ ...prev, [sid]: null }));
              });
          }
        });
      })
      .catch((err) => setError(formatGraphApiError(err)))
      .finally(() => setIsLoading(false));
  }, [workspaceId, statusFilter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Pre-compute progress (completed / total) per instance.
  const progressMap = useMemo(() => {
    const map: Record<string, { completed: number; total: number }> = {};
    for (const inst of instances) {
      const nodes = nodeStatuses[inst.graph_instance_id];
      if (nodes && nodes.length > 0) {
        const completed = nodes.filter((n) => n.status === "completed").length;
        map[inst.graph_instance_id] = { completed, total: nodes.length };
      } else {
        map[inst.graph_instance_id] = { completed: 0, total: 0 };
      }
    }
    return map;
  }, [instances, nodeStatuses]);

  return (
    <div className="flex-1 overflow-y-auto px-6 py-6">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <div className="flex items-center justify-between">
          <Button variant="ghost" size="sm" onClick={onBack} className="gap-1.5 -ml-2">
            <ArrowLeft size={14} />
            {t("graphs.back")}
          </Button>
          <div className="w-44">
            <SelectMenu
              ariaLabel={t("graphs.filterStatus")}
              value={statusFilter}
              onChange={setStatusFilter}
              options={[
                { value: "", label: t("graphs.allStatuses") },
                ...GRAPH_INSTANCE_STATUSES.map((s) => ({
                  value: s,
                  label: t(statusLabelKey(s)),
                })),
              ]}
            />
          </div>
        </div>

        <SectionLabel>{t("graphs.instances")}</SectionLabel>

        {isLoading ? (
          <p className="text-base text-mute">{t("graphs.loading")}</p>
        ) : error ? (
          <p className="text-base text-danger">
            {t("graphs.loadFailed", { error })}
          </p>
        ) : instances.length === 0 ? (
          <p className="text-base text-mute">{t("graphs.noInstances")}</p>
        ) : (
          <div className="flex flex-col gap-2">
            {instances.map((instance) => {
              const topo = topologies[instance.spec_id] ?? null;
              const nodes = nodeStatuses[instance.graph_instance_id];
              const statusMap = nodes ? buildNodeStatusMap(nodes) : undefined;
              const prog = progressMap[instance.graph_instance_id];
              const specName = specNames[instance.spec_id];
              return (
                <Card key={instance.graph_instance_id} hoverable className="p-0">
                  <Button
                    variant="ghost"
                    size="md"
                    onClick={(): void => onOpenInstance(instance.graph_instance_id)}
                    className="h-auto w-full justify-between gap-3 rounded-md px-4 py-3 text-left hover:bg-hairline-soft"
                  >
                    <span className="flex min-w-0 items-center gap-3">
                      {topo ? (
                        <MiniTopology
                          topology={topo}
                          nodeStatuses={statusMap}
                          className="shrink-0"
                        />
                      ) : (
                        <span className="inline-block h-6 w-20 shrink-0" />
                      )}
                      <span className="flex min-w-0 flex-col gap-0.5">
                        <span className="flex items-baseline gap-2">
                          <Hash size={12} className="shrink-0 text-mute" />
                          <span className="font-mono text-base text-ink">
                            {instance.graph_instance_id}
                          </span>
                          {specName ? (
                            <span className="truncate text-sm text-body">
                              {specName}
                            </span>
                          ) : null}
                        </span>
                        <span className="font-mono text-xs text-mute">
                          {prog && prog.total > 0
                            ? t("graphs.progress", {
                                completed: prog.completed,
                                total: prog.total,
                              })
                            : "—"}
                        </span>
                      </span>
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      <GraphStatusBadge
                        status={instance.status}
                        label={t(statusLabelKey(instance.status))}
                      />
                      <ChevronRight size={16} className="text-mute" />
                    </span>
                  </Button>
                </Card>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
};
