// GraphSpecListPage — spec list for the current workspace (GET /api/graphs/specs).
// Each row renders a MiniTopology thumbnail (parsed from the spec YAML) + spec
// name + version + node count / scheduler / trigger mode. Clicking a row opens
// the YAML editor; a secondary action opens the instance list. (PRD §6.3)

import { useEffect, useState, type FC } from "react";
import { ChevronRight, ListTree, Pencil } from "lucide-react";
import { getSpec, getSpecs, type GraphSpecSummary } from "../../lib/graphsApi";
import { parseGraphSpecYaml, type ParsedGraphTopology } from "./yaml/parseGraphSpec";
import { MiniTopology } from "./topology/MiniTopology";
import { useT } from "../../i18n";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { SectionLabel } from "../ui/SectionLabel";
import { formatGraphApiError } from "./shared";

export interface GraphSpecListPageProps {
  workspaceId: string;
  onEditSpec: (specId: string) => void;
  onOpenInstances: () => void;
}

/** Spec id → parsed topology (or null if parse failed). */
type TopologyMap = Record<string, ParsedGraphTopology | null>;

export const GraphSpecListPage: FC<GraphSpecListPageProps> = ({
  workspaceId,
  onEditSpec,
  onOpenInstances,
}) => {
  const t = useT();
  const [specs, setSpecs] = useState<GraphSpecSummary[]>([]);
  const [topologies, setTopologies] = useState<TopologyMap>({});
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    setTopologies({});
    getSpecs(workspaceId)
      .then((loaded) => {
        if (cancelled) return;
        setSpecs(loaded);
        // Parallel-fetch each spec's YAML and parse the topology. Parse
        // failures produce a null entry — MiniTopology is simply omitted for
        // that row, the list itself is never blocked.
        loaded.forEach((spec) => {
          getSpec(workspaceId, spec.spec_id)
            .then((resp) => {
              if (cancelled) return;
              let topo: ParsedGraphTopology | null;
              try {
                topo = parseGraphSpecYaml(resp.yaml_content);
              } catch {
                topo = null;
              }
              setTopologies((prev) => ({ ...prev, [spec.spec_id]: topo }));
            })
            .catch(() => {
              if (cancelled) return;
              setTopologies((prev) => ({ ...prev, [spec.spec_id]: null }));
            });
        });
      })
      .catch((err) => {
        if (!cancelled) setError(formatGraphApiError(err));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return (): void => {
      cancelled = true;
    };
  }, [workspaceId]);

  return (
    <div className="flex-1 overflow-y-auto px-6 py-6">
      <div className="mx-auto max-w-3xl">
        <div className="flex items-center justify-between">
          <SectionLabel>{t("graphs.specs")}</SectionLabel>
          <Button variant="secondary" size="sm" onClick={onOpenInstances}>
            <ListTree size={14} />
            {t("graphs.instances")}
          </Button>
        </div>

        <div className="mt-4 flex flex-col gap-2">
          {isLoading ? (
            <p className="text-base text-mute">{t("graphs.loading")}</p>
          ) : error ? (
            <p className="text-base text-danger">
              {t("graphs.loadFailed", { error })}
            </p>
          ) : specs.length === 0 ? (
            <p className="text-base text-mute">{t("graphs.noGraphsHint")}</p>
          ) : (
            specs.map((spec) => {
              const topo = topologies[spec.spec_id];
              const functionalNodes = topo
                ? topo.nodes.filter(
                    (n) =>
                      n.nodeType !== "__start__" && n.nodeType !== "__end__",
                  ).length
                : 0;
              return (
                <Card key={spec.spec_id} hoverable className="p-0">
                  <Button
                    variant="ghost"
                    size="md"
                    onClick={(): void => onEditSpec(spec.spec_id)}
                    title={t("graphs.editSpec")}
                    className="h-auto w-full justify-between gap-3 rounded-md px-4 py-3 text-left hover:bg-hairline-soft"
                  >
                    <span className="flex min-w-0 items-center gap-3">
                      {topo ? (
                        <MiniTopology topology={topo} className="shrink-0" />
                      ) : (
                        <span className="inline-block h-6 w-20 shrink-0" />
                      )}
                      <span className="flex min-w-0 flex-col gap-0.5">
                        <span className="flex items-baseline gap-2">
                          <span className="truncate text-base font-medium text-ink">
                            {spec.name}
                          </span>
                          <span className="shrink-0 font-mono text-xs text-faint">
                            {t("graphs.version", { version: spec.version })}
                          </span>
                        </span>
                        {topo ? (
                          <span className="font-mono text-xs text-mute">
                            {t("graphs.nodesCount", { count: functionalNodes })}
                            {" · "}
                            {topo.scheduler}
                            {" · "}
                            {topo.defaultTrigger}
                          </span>
                        ) : null}
                      </span>
                    </span>
                    <span className="flex shrink-0 items-center gap-1 text-mute">
                      <Pencil size={14} />
                      <ChevronRight size={16} />
                    </span>
                  </Button>
                </Card>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};
