// GraphSpecDetail — spec 详情视图(graph IA redesign T02)。
//
// 替换 GraphConversation 伪会话视图:拓扑预览(主区,纯结构)+ instance
// 列表(右侧 300px 面板)+ 新建 instance composer(面板底部)。Run 成功后
// 立即跳转 instance 详情,不在 spec 详情等待。

import {
  useEffect,
  useState,
  type FC,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { ArrowLeft, Code2, Play } from "lucide-react";
import {
  getInstance,
  getSpec,
  listInstances,
  runGraph,
  type GraphInstance,
  type GraphNodeStatus,
} from "../../lib/graphsApi";
import { useT } from "../../i18n";
import { Button } from "../ui/Button";
import { SectionLabel } from "../ui/SectionLabel";
import { formatGraphApiError } from "./shared";
import { GraphSpecInstanceRow } from "./GraphSpecInstanceRow";
import { TopologyCanvas } from "./topology/TopologyCanvas";
import {
  parseGraphSpecYaml,
  type ParsedGraphTopology,
} from "./yaml/parseGraphSpec";

export interface GraphSpecDetailProps {
  workspaceId: string;
  specId: string;
  onBack: () => void;
  onEditYaml: () => void;
  onOpenInstance: (instanceId: string) => void;
}

/** Instance id → node statuses (list endpoint returns nodes: []). */
type NodeStatusMap = Record<string, GraphNodeStatus[]>;

export const GraphSpecDetail: FC<GraphSpecDetailProps> = ({
  workspaceId,
  specId,
  onBack,
  onEditYaml,
  onOpenInstance,
}) => {
  const t = useT();
  const [specInfo, setSpecInfo] = useState<{
    name: string;
    version: string;
  } | null>(null);
  const [topology, setTopology] = useState<ParsedGraphTopology | null>(null);
  const [instances, setInstances] = useState<GraphInstance[]>([]);
  const [nodeStatuses, setNodeStatuses] = useState<NodeStatusMap>({});
  const [input, setInput] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    setSpecInfo(null);
    setTopology(null);
    setInstances([]);
    setNodeStatuses({});
    Promise.all([
      getSpec(workspaceId, specId),
      listInstances(workspaceId, undefined, specId),
    ])
      .then(([spec, loadedInstances]) => {
        if (cancelled) return;
        setSpecInfo({ name: spec.name, version: spec.version });
        try {
          setTopology(parseGraphSpecYaml(spec.yaml_content));
        } catch {
          setTopology(null);
        }
        setInstances(loadedInstances);
        // Per-instance detail fetch for progress (completed/total nodes).
        loadedInstances.forEach((inst) => {
          getInstance(workspaceId, inst.graph_instance_id)
            .then((detail) => {
              if (cancelled) return;
              setNodeStatuses((prev) => ({
                ...prev,
                [inst.graph_instance_id]: detail.nodes,
              }));
            })
            .catch(() => {
              // Detail unavailable — the row renders without progress.
            });
        });
      })
      .catch((err) => {
        if (!cancelled) setError(formatGraphApiError(err));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId, specId]);

  const handleRun = (): void => {
    if (isSubmitting || !input.trim()) return;
    const content = input.trim();
    setIsSubmitting(true);
    runGraph(workspaceId, specId, content)
      .then((resp) => {
        setInput("");
        onOpenInstance(resp.graph_instance_id);
      })
      .catch((err) => {
        setError(formatGraphApiError(err));
        setIsSubmitting(false);
      });
  };

  const handleSubmit = (e: FormEvent<HTMLFormElement>): void => {
    e.preventDefault();
    handleRun();
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleRun();
    }
  };

  return (
    <div
      className="flex flex-1 flex-col bg-canvas"
      data-testid="graph-spec-detail"
    >
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-hairline px-4">
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            size="sm"
            onClick={onBack}
            className="gap-1.5 -ml-2"
          >
            <ArrowLeft size={14} />
            {t("graphs.back")}
          </Button>
          {specInfo && (
            <>
              <span className="text-base font-medium text-ink">
                {specInfo.name}
              </span>
              <span className="font-mono text-xs text-faint">
                {t("graphs.version", { version: specInfo.version })}
              </span>
            </>
          )}
        </div>
        <Button variant="secondary" size="sm" onClick={onEditYaml}>
          <Code2 size={14} />
          {t("graphs.editYaml")}
        </Button>
      </header>

      {error && (
        <pre className="mx-4 mt-3 whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger">
          {error}
        </pre>
      )}

      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1">
          {topology && <TopologyCanvas topology={topology} className="h-full" />}
        </div>

        <aside className="flex w-[300px] shrink-0 flex-col border-l border-hairline bg-canvas-sidebar">
          <div className="border-b border-hairline px-4 pb-1 pt-3">
            <SectionLabel>{t("graphs.instances")}</SectionLabel>
          </div>

          <div className="flex-1 overflow-y-auto p-2">
            {isLoading ? (
              <p className="px-2 py-1 text-base text-mute">
                {t("graphs.loading")}
              </p>
            ) : instances.length === 0 ? (
              <p className="px-2 py-1 text-base text-mute">
                {t("graphs.noInstances")}
              </p>
            ) : (
              instances.map((inst) => (
                <GraphSpecInstanceRow
                  key={inst.graph_instance_id}
                  instance={inst}
                  nodes={nodeStatuses[inst.graph_instance_id]}
                  onOpenInstance={onOpenInstance}
                />
              ))
            )}
          </div>

          <div className="shrink-0 border-t border-hairline p-3">
            <form className="composer" onSubmit={handleSubmit}>
              <div className="relative min-w-0 flex-1">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={t("graphs.triggerInstance")}
                  rows={2}
                  disabled={isSubmitting}
                  className="w-full resize-none overflow-y-auto bg-transparent text-md leading-relaxed text-ink outline-none placeholder:text-faint disabled:opacity-50"
                />
              </div>
              <Button
                variant="primary"
                size="sm"
                disabled={!input.trim() || isSubmitting}
                onClick={handleRun}
              >
                <Play size={14} />
                {t("graphs.run")}
              </Button>
            </form>
          </div>
        </aside>
      </div>
    </div>
  );
};
