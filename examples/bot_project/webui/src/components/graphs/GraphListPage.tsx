// GraphListPage — graph instance list with an optional status filter
// (GET /api/graphs/instances?status=…). Clicking a row opens the execution
// viewer.

import { useCallback, useEffect, useState, type FC } from "react";
import { ArrowLeft, ChevronRight, Hash } from "lucide-react";
import {
  GRAPH_INSTANCE_STATUSES,
  listInstances,
  type GraphInstance,
} from "../../lib/graphsApi";
import { useT } from "../../i18n";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { SectionLabel } from "../ui/SectionLabel";
import { SelectMenu } from "../ui/SelectMenu";
import { formatGraphApiError, GraphStatusBadge } from "./shared";
import { statusLabelKey } from "./GraphExecutionViewer";

export interface GraphListPageProps {
  workspaceId: string;
  onOpenInstance: (instanceId: string) => void;
  onBack: () => void;
}

export const GraphListPage: FC<GraphListPageProps> = ({
  workspaceId,
  onOpenInstance,
  onBack,
}) => {
  const t = useT();
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [instances, setInstances] = useState<GraphInstance[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback((): void => {
    setIsLoading(true);
    setError(null);
    listInstances(workspaceId, statusFilter || undefined)
      .then(setInstances)
      .catch((err) => setError(formatGraphApiError(err)))
      .finally(() => setIsLoading(false));
  }, [workspaceId, statusFilter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

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
            {instances.map((instance) => (
              <Card key={instance.graph_instance_id} hoverable className="p-0">
                <Button
                  variant="ghost"
                  size="md"
                  onClick={(): void => onOpenInstance(instance.graph_instance_id)}
                  className="h-auto w-full justify-between gap-2 rounded-md px-4 py-3 text-left hover:bg-hairline-soft"
                >
                  <span className="flex min-w-0 items-center gap-2.5">
                    <Hash size={14} className="shrink-0 text-mute" />
                    <span className="font-mono text-base text-ink">
                      {instance.graph_instance_id}
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
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
