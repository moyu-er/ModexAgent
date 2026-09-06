// PoolsConfigView.tsx — the `pools` settings tab (PRD Part C / tickets T4+T5):
// master/detail over the scope declaration. Left column is the declaration
// tree (workspace → pools → agents); the right column is the selected node's
// form. The whole declaration is ONE dirty-tracked document — edits mutate a
// cloned draft, one Save button PUTs /api/scope/model, and a successful save
// re-fetches the canonicalized model (the backend strips default-valued
// fields) before resetting the form state.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../../../lib/api";
import {
  getScopeBill,
  getScopeModel,
  getScopeOptions,
  previewScopeModel,
  saveScopeModel,
  type ScopeAgentBill,
  type ScopeModelIssue,
  type ScopeModelTree,
  type ScopeOptions,
} from "../../../lib/scopeApi";
import { listPrompts } from "../../../lib/promptsApi";
import { useToast } from "../../ToastContext";
import { useT, type TFn } from "../../../i18n";
import { ActionBar } from "../../ui/ActionBar";
import { Button } from "../../ui/Button";
import { CATEGORY } from "../categoryMeta";
import { ConfirmDialog } from "../ConfirmDialog";
import { restartToast } from "../restartToast";
import { DeclarationTree } from "./DeclarationTree";
import { AgentForm } from "./AgentForm";
import { PoolForm } from "./PoolForm";
import { WorkspaceForm } from "./WorkspaceForm";
import {
  addPool,
  addSubagent,
  agentBodyOf,
  agentNodeId,
  applyPermissionsToOtherPools,
  deleteAgent,
  deletePool,
  findAgent,
  nodeIdsByName,
  poolNodeId,
  setPeer,
  viewModel,
  WORKSPACE_NODE_ID,
  type AgentBody,
  type WorkspaceBody,
} from "./scopeModel";

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

/** Parse a tree node id back into its coordinates. */
function parseNodeId(
  id: string,
): { kind: "workspace" } | { kind: "pool"; pool: string } | { kind: "agent"; pool: string; path: string[] } | null {
  if (id === WORKSPACE_NODE_ID) return { kind: "workspace" };
  const parts = id.split("/");
  if (parts[0] === "pool" && parts.length === 2) return { kind: "pool", pool: parts[1]! };
  if (parts[0] === "agent" && parts.length >= 3) {
    return { kind: "agent", pool: parts[1]!, path: parts.slice(2) };
  }
  return null;
}

function parseIssues(e: unknown): ScopeModelIssue[] {
  if (!(e instanceof ApiError)) return [];
  try {
    const body = JSON.parse(e.detail) as { issues?: ScopeModelIssue[] };
    return Array.isArray(body.issues) ? body.issues : [];
  } catch {
    return [];
  }
}

function formatSaveError(e: unknown, t: TFn): string {
  if (e instanceof ApiError) {
    try {
      const body = JSON.parse(e.detail) as {
        error?: string;
        issues?: ScopeModelIssue[];
      };
      if (body.issues && body.issues.length > 0) {
        return t("settings.poolsPanel.saveFailed", {
          detail: body.issues.map((i) => `${i.rule} ${i.node}: ${i.message}`).join("; "),
        });
      }
      if (body.error) {
        return t("settings.poolsPanel.saveFailed", { detail: body.error });
      }
    } catch {
      // detail is not JSON — fall through
    }
    return t("settings.poolsPanel.saveFailed", { detail: `${e.status} ${e.detail}` });
  }
  return t("settings.poolsPanel.saveFailed", { detail: String(e) });
}

export function PoolsConfigView() {
  const toast = useToast();
  const t = useT();
  const [model, setModel] = useState<ScopeModelTree | null>(null);
  const [original, setOriginal] = useState<ScopeModelTree | null>(null);
  const [options, setOptions] = useState<ScopeOptions | null>(null);
  const [prompts, setPrompts] = useState<string[]>([]);
  /** Bill of the on-disk declaration (valid while the draft is clean). */
  const [diskBill, setDiskBill] = useState<ScopeAgentBill[] | null>(null);
  /** Bill-shaped preview of the dirty draft; null until the first success. */
  const [previewBill, setPreviewBill] = useState<ScopeAgentBill[] | null>(null);
  const [previewPending, setPreviewPending] = useState<boolean>(false);
  const [selection, setSelection] = useState<string | null>(null);
  const [issues, setIssues] = useState<ScopeModelIssue[]>([]);
  const [loadError, setLoadError] = useState<string>("");
  const [saveError, setSaveError] = useState<string>("");
  const [saving, setSaving] = useState<boolean>(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const formPanelRef = useRef<HTMLDivElement>(null);
  const previewSeq = useRef(0);

  const load = useCallback(async (): Promise<void> => {
    setLoadError("");
    const [tree, opts, bill] = await Promise.all([
      getScopeModel(),
      getScopeOptions(),
      getScopeBill(),
    ]);
    setModel(tree);
    setOriginal(tree);
    setOptions(opts);
    setDiskBill(bill);
    setPreviewBill(null);
    setIssues([]);
    setSaveError("");
    // Prompts feed one dropdown — a failure degrades to an empty roster with
    // a toast rather than taking down the whole panel.
    try {
      const list = await listPrompts();
      setPrompts(list.map((p) => p.name));
    } catch (e) {
      setPrompts([]);
      toast.show({
        message: t("settings.poolsPanel.promptsLoadFailed", { error: String(e) }),
        tone: "error",
      });
    }
  }, [toast, t]);

  useEffect(() => {
    void load().catch((e: unknown) => {
      const message = t("common.failedToLoad", { error: String(e) });
      setLoadError(message);
      toast.show({ message, tone: "error" });
    });
  }, [load, t, toast]);

  const view = useMemo(() => (model ? viewModel(model) : null), [model]);

  // Default + dangling selection repair: workspace first, then first pool.
  useEffect(() => {
    if (!view) return;
    const valid =
      selection !== null &&
      (selection === WORKSPACE_NODE_ID
        ? view.workspaceBody !== null
        : (() => {
            const parsed = parseNodeId(selection);
            if (!parsed) return false;
            if (parsed.kind === "pool") {
              return view.pools.some((p) => p.name === parsed.pool);
            }
            if (parsed.kind === "agent") {
              return findAgent(view, parsed.pool, parsed.path) !== null;
            }
            return false;
          })());
    if (valid) return;
    if (view.workspaceBody) setSelection(WORKSPACE_NODE_ID);
    else if (view.pools[0]) setSelection(poolNodeId(view.pools[0].name));
    else setSelection(null);
  }, [view, selection]);

  const dirty = model !== null && original !== null && JSON.stringify(model) !== JSON.stringify(original);

  // C0 — while the draft is dirty, the on-disk bill is stale w.r.t. the form:
  // debounce a POST /api/scope/preview so effective sections track the draft
  // live. On preview 400 the issues land in the same issue area as a failed
  // save; the last good bill stays on screen. A sequence guard discards
  // out-of-order responses.
  useEffect(() => {
    if (!dirty || model === null) return;
    const seq = ++previewSeq.current;
    setPreviewPending(true);
    const timer = setTimeout(() => {
      void previewScopeModel(model)
        .then((agents) => {
          if (previewSeq.current !== seq) return;
          setPreviewBill(agents);
          setIssues([]);
          setPreviewPending(false);
        })
        .catch((e: unknown) => {
          if (previewSeq.current !== seq) return;
          const found = parseIssues(e);
          if (found.length > 0) setIssues(found);
          setPreviewPending(false);
        });
    }, 400);
    return () => clearTimeout(timer);
  }, [model, dirty]);

  /** The bill the form renders: preview of the draft while dirty, else disk. */
  const effectiveBill = dirty ? (previewBill ?? diskBill) : diskBill;

  /** Clone-then-mutate: the single edit path for every form/tree action. */
  const update = useCallback((mut: (draft: ScopeModelTree) => void): void => {
    setModel((prev) => {
      if (prev === null) return prev;
      const draft = clone(prev);
      mut(draft);
      return draft;
    });
    setIssues([]);
    setSaveError("");
  }, []);

  // Map issue.node (a bare pool/agent name) onto tree node ids so the tree
  // can mark offenders and the form panel can show messages near the fields.
  const issuesByNode = useMemo(() => {
    const map = new Map<string, ScopeModelIssue[]>();
    if (!view) return map;
    for (const issue of issues) {
      const ids = nodeIdsByName(view, issue.node);
      const id = selection !== null && ids.includes(selection) ? selection : ids[0];
      if (!id) continue;
      map.set(id, [...(map.get(id) ?? []), issue]);
    }
    return map;
  }, [issues, view, selection]);

  const focusFirstInvalid = (found: ScopeModelIssue[]): void => {
    if (!view || found.length === 0) return;
    const ids = nodeIdsByName(view, found[0]!.node);
    if (ids[0]) setSelection(ids[0]);
    formPanelRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  };

  const save = async (): Promise<void> => {
    if (model === null) return;
    setSaving(true);
    setSaveError("");
    try {
      await saveScopeModel(model);
      // The backend canonicalizes on write (deviations only) — reset the
      // form state from what the file actually holds now, including the
      // fresh disk bill (the preview was of the pre-canonical draft).
      const [fresh, freshBill] = await Promise.all([getScopeModel(), getScopeBill()]);
      setModel(fresh);
      setOriginal(fresh);
      setDiskBill(freshBill);
      setPreviewBill(null);
      setIssues([]);
      restartToast(toast, t);
    } catch (e) {
      const found = parseIssues(e);
      if (found.length > 0) {
        setIssues(found);
        focusFirstInvalid(found);
      }
      setSaveError(formatSaveError(e, t));
    } finally {
      setSaving(false);
    }
  };

  const cancel = (): void => {
    if (original !== null) setModel(clone(original));
    setPreviewBill(null);
    setIssues([]);
    setSaveError("");
  };

  // ── Structure operations ────────────────────────────────────────────────

  const createPool = (name: string): void => {
    update((d) => addPool(d, name));
    setSelection(agentNodeId(name, [name]));
  };

  const createAgent = (pool: string, parentPath: string[], name: string): void => {
    update((d) => addSubagent(d, pool, parentPath, name));
    setSelection(agentNodeId(pool, [...parentPath, name]));
  };

  const confirmDelete = (): void => {
    if (deleteTarget === null) return;
    const parsed = parseNodeId(deleteTarget);
    if (parsed?.kind === "pool") {
      update((d) => deletePool(d, parsed.pool));
    } else if (parsed?.kind === "agent") {
      update((d) => deleteAgent(d, parsed.pool, parsed.path));
    }
    setDeleteTarget(null);
  };

  // Deleting a pool OR a pool's root agent removes the whole pool.
  const deleteTargetName = ((): string => {
    if (deleteTarget === null) return "";
    const parsed = parseNodeId(deleteTarget);
    if (parsed?.kind === "pool") return parsed.pool;
    if (parsed?.kind === "agent") return parsed.path[parsed.path.length - 1] ?? "";
    return "";
  })();
  const deleteIsPool = ((): boolean => {
    if (deleteTarget === null) return false;
    const parsed = parseNodeId(deleteTarget);
    return parsed?.kind === "pool" || (parsed?.kind === "agent" && parsed.path.length === 1);
  })();

  const applyToPools = (pool: string, path: string[]): void => {
    if (!view) return;
    update((d) => applyPermissionsToOtherPools(d, pool, path));
    toast.show({
      message: t("settings.poolsPanel.appliedToPools", { count: view.pools.length - 1 }),
      tone: "success",
    });
  };

  // ── Render ──────────────────────────────────────────────────────────────

  const meta = CATEGORY.pools;
  const PageHeadIcon = meta.icon;

  if (loadError) {
    return <p className="text-base text-error">{loadError}</p>;
  }
  if (model === null || view === null || options === null) {
    return (
      <div
        className="flex h-full items-center justify-center gap-2 text-mute"
        data-testid="pools-loading"
      >
        <svg className="h-4 w-4 animate-spin" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <circle cx="8" cy="8" r="6" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
          <path d="M14 8a6 6 0 0 0-6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
        {t("common.loading")}
      </div>
    );
  }

  const parsed = selection !== null ? parseNodeId(selection) : null;

  return (
    <div data-testid="pools-view" className="flex h-full flex-col space-y-4">
      <div className="page-head">
        <span className="page-head-icon" style={{ ["--cat" as string]: meta.catVar }}>
          <PageHeadIcon size={18} />
        </span>
        <div>
          <div className="page-title">{t(meta.titleKey!)}</div>
          <div className="page-sub">{t(meta.subKey)}</div>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 gap-4">
        <div className="w-64 shrink-0 overflow-hidden rounded-lg border border-hairline bg-canvas-elevated">
          <DeclarationTree
            view={view}
            selection={selection}
            issueNodeIds={new Set(issuesByNode.keys())}
            onSelect={setSelection}
            onCreatePool={createPool}
            onCreateAgent={createAgent}
            onDelete={setDeleteTarget}
          />
        </div>

        <div ref={formPanelRef} className="min-w-0 flex-1 overflow-auto">
          {previewPending ? (
            <p
              data-testid="pools-preview-pending"
              className="mb-2 animate-pulse text-xs text-mute"
            >
              {t("settings.poolsPanel.previewPending")}
            </p>
          ) : null}
          {saveError ? (
            <pre
              data-testid="pools-save-error"
              className="mb-4 whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger"
            >
              {saveError}
            </pre>
          ) : null}

          {parsed?.kind === "workspace" && view.workspaceBody ? (
            <WorkspaceForm
              workspace={view.workspaceBody}
              updateWorkspace={(mut: (b: WorkspaceBody) => void) =>
                update((d) => {
                  const v = viewModel(d);
                  if (v.workspaceBody) mut(v.workspaceBody);
                })
              }
            />
          ) : parsed?.kind === "pool" ? (
            (() => {
              const pool = view.pools.find((p) => p.name === parsed.pool);
              if (!pool) return null;
              return (
                <PoolForm
                  pool={pool}
                  otherPoolNames={view.pools.map((p) => p.name).filter((n) => n !== pool.name)}
                  onSetPeer={(other, on) => update((d) => setPeer(d, pool.name, other, on))}
                />
              );
            })()
          ) : parsed?.kind === "agent" ? (
            (() => {
              const node = findAgent(view, parsed.pool, parsed.path);
              if (!node) return null;
              return (
                <AgentForm
                  pool={parsed.pool}
                  node={node}
                  options={options}
                  prompts={prompts}
                  issues={issuesByNode.get(selection ?? "") ?? []}
                  bill={
                    effectiveBill?.find(
                      (a) => a.pool === parsed.pool && a.agent === node.name,
                    ) ?? null
                  }
                  updateAgent={(mut: (b: AgentBody) => void) =>
                    update((d) => {
                      const body = agentBodyOf(d, parsed.pool, parsed.path);
                      if (body) mut(body);
                    })
                  }
                  onApplyToPools={() => applyToPools(parsed.pool, parsed.path)}
                />
              );
            })()
          ) : (
            <p className="text-base text-mute">{t("settings.poolsPanel.selectNode")}</p>
          )}
        </div>
      </div>

      <ActionBar dirty={dirty}>
        <Button variant="secondary" size="sm" onClick={cancel} disabled={!dirty || saving}>
          {t("common.cancel")}
        </Button>
        <Button
          variant="primary"
          size="sm"
          onClick={() => void save()}
          disabled={!dirty || saving}
          loading={saving}
          data-testid="pools-save"
        >
          {t("common.save")}
        </Button>
      </ActionBar>

      {deleteTarget !== null ? (
        <ConfirmDialog
          title={
            deleteIsPool
              ? t("settings.poolsPanel.deletePoolTitle", { name: deleteTargetName })
              : t("settings.poolsPanel.deleteAgentTitle", { name: deleteTargetName })
          }
          message={
            deleteIsPool
              ? t("settings.poolsPanel.deletePoolMessage")
              : t("settings.poolsPanel.deleteAgentMessage")
          }
          confirmLabel={t("common.delete")}
          tone="danger"
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      ) : null}
    </div>
  );
}
