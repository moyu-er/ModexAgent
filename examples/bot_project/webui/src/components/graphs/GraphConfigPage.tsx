// GraphConfigPage — spec list for the current workspace (GET /api/graphs/specs).
// Clicking a spec opens the YAML editor; a secondary action opens the
// instance list.

import { useEffect, useState, type FC } from "react";
import { ChevronRight, ListTree, Workflow } from "lucide-react";
import { getSpecs, type GraphSpecSummary } from "../../lib/graphsApi";
import { useT } from "../../i18n";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { SectionLabel } from "../ui/SectionLabel";
import { formatGraphApiError } from "./shared";

export interface GraphConfigPageProps {
  workspaceId: string;
  onEditSpec: (specId: string) => void;
  onOpenInstances: () => void;
}

export const GraphConfigPage: FC<GraphConfigPageProps> = ({
  workspaceId,
  onEditSpec,
  onOpenInstances,
}) => {
  const t = useT();
  const [specs, setSpecs] = useState<GraphSpecSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    getSpecs(workspaceId)
      .then((loaded) => {
        if (!cancelled) setSpecs(loaded);
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
            <p className="text-base text-mute">{t("graphs.noSpecs")}</p>
          ) : (
            specs.map((spec) => (
              <Card key={spec.spec_id} hoverable className="p-0">
                <Button
                  variant="ghost"
                  size="md"
                  onClick={(): void => onEditSpec(spec.spec_id)}
                  title={t("graphs.editSpec")}
                  className="h-auto w-full justify-between gap-2 rounded-md px-4 py-3 text-left hover:bg-hairline-soft"
                >
                  <span className="flex min-w-0 items-center gap-2.5">
                    <Workflow size={16} className="shrink-0 text-brand" />
                    <span className="truncate text-base text-ink">{spec.name}</span>
                    <span className="shrink-0 font-mono text-xs text-faint">
                      {t("graphs.version", { version: spec.version })}
                    </span>
                  </span>
                  <ChevronRight size={16} className="shrink-0 text-mute" />
                </Button>
              </Card>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
