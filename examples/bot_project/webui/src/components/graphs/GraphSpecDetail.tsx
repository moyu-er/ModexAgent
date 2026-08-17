// GraphSpecDetail — spec 详情视图(graph IA redesign T02 / Rev 3 T11)。
//
// 布局:header + 拓扑预览(主区,纯结构)+ instance 列表(右侧 300px 面板,
// 占满全高)。新建 instance 走主区右下角 FAB → 居中 New Instance modal(原
// 面板底部 composer 的独立版,modal 模式与 GraphInstanceDetail 的 Run Graph
// modal 同构:portal + focus trap + Esc/✕/backdrop 关闭)。Run 成功后立即跳转
// instance 详情,不在 spec 详情等待。

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FC,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, Code2, Play, Plus, X } from "lucide-react";
import {
  getInstance,
  getSpec,
  listInstances,
  runGraph,
  type GraphInstance,
  type GraphNodeStatus,
} from "../../lib/graphsApi";
import { useT } from "../../i18n";
import { useModalFocus } from "../../hooks/useModalFocus";
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
  const [modalOpen, setModalOpen] = useState(false);
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

  // Stable reference: the modal's focus effect depends on it — an inline
  // closure would tear down/re-add the keydown listener on every parent
  // re-render (per-instance detail fetches) and bounce focus off the textarea.
  const closeModal = useCallback((): void => setModalOpen(false), []);

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
        <div className="relative min-w-0 flex-1">
          {topology && <TopologyCanvas topology={topology} className="h-full" />}
          <button
            type="button"
            onClick={(): void => setModalOpen(true)}
            aria-label={t("graphs.newInstance")}
            aria-haspopup="dialog"
            className="btn-primary absolute bottom-6 right-6 z-10 flex h-14 w-14 items-center justify-center rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
          >
            <Plus size={22} />
          </button>
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
                  topology={topology}
                  onOpenInstance={onOpenInstance}
                />
              ))
            )}
          </div>
        </aside>
      </div>

      {modalOpen && (
        <NewInstanceModal
          workspaceId={workspaceId}
          specId={specId}
          specInfo={specInfo}
          onClose={closeModal}
          onOpenInstance={onOpenInstance}
        />
      )}
    </div>
  );
};

interface NewInstanceModalProps {
  workspaceId: string;
  specId: string;
  specInfo: { name: string; version: string } | null;
  onClose: () => void;
  onOpenInstance: (instanceId: string) => void;
}

const NewInstanceModal: FC<NewInstanceModalProps> = ({
  workspaceId,
  specId,
  specInfo,
  onClose,
  onOpenInstance,
}) => {
  const t = useT();
  const [input, setInput] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  // Focus management (via the shared useModalFocus hook): focus the textarea
  // on open, trap Tab inside, restore focus to the opener on close. Esc
  // closes the modal on the bubble phase so an Esc handled deeper does not
  // also close the modal.
  useModalFocus({ dialogRef, onClose, initialFocusRef: taRef });

  const handleRun = (): void => {
    if (isSubmitting || !input.trim()) return;
    const content = input.trim();
    setIsSubmitting(true);
    setRunError(null);
    runGraph(workspaceId, specId, content)
      .then((resp) => {
        setInput("");
        onOpenInstance(resp.graph_instance_id);
      })
      .catch((err) => {
        setRunError(formatGraphApiError(err));
        setIsSubmitting(false);
      });
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleRun();
    }
  };

  // Portal at document.body: the modal must escape any ancestor transform
  // (same containment hazard documented in WorkspaceBrowser).
  return createPortal(
    <div
      className="modal-scrim-enter fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4"
      data-testid="new-instance-backdrop"
      onClick={onClose}
      role="presentation"
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("graphs.newInstance")}
        tabIndex={-1}
        className="modal-panel-enter flex w-full max-w-[560px] flex-col overflow-hidden rounded-lg border border-hairline bg-canvas-popover shadow-card-hover focus:outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header: spec name · version chip · ✕ */}
        <div className="flex shrink-0 items-center gap-3 border-b border-hairline px-4 py-3">
          {specInfo ? (
            <>
              <span className="truncate text-base font-medium text-ink">
                {specInfo.name}
              </span>
              <span className="inline-flex items-center gap-1 rounded-sm border border-hairline px-1.5 py-0.5 font-mono text-xs text-ember">
                {t("graphs.specVersion", { version: specInfo.version })}
              </span>
            </>
          ) : (
            <span className="text-base text-mute">{t("graphs.loading")}</span>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label={t("common.close")}
            className="ml-auto rounded-sm p-1 text-mute hover:bg-hairline-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
          >
            <X size={18} />
          </button>
        </div>

        {runError ? (
          <pre className="mx-4 mt-3 whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger">
            {runError}
          </pre>
        ) : null}

        {/* Body: the old panel composer as a standalone floating input. */}
        <div className="p-4">
          <div className="composer">
            <div className="relative min-w-0 flex-1">
              <textarea
                ref={taRef}
                value={input}
                onChange={(e): void => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={t("graphs.triggerInstance")}
                rows={3}
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
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
};
