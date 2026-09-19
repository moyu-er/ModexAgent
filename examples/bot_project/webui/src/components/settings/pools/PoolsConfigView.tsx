// PoolsConfigView.tsx — the `assistants` settings page (user UI corrections
// 3+4): a friendly assistants list (left) + the selected root/collaborator
// AgentForm (right). The whole declaration is ONE dirty-tracked document —
// edits mutate a cloned draft, one Save button PUTs /api/scope/model, and a
// successful save re-fetches the canonicalized model (the backend strips
// default-valued fields) before resetting the form state. Experts edit the
// raw declaration through Settings → Advanced (ScopeView), never here.

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
import { fetchPreferences } from "../../../lib/preferencesApi";
import { useToast } from "../../ToastContext";
import { useT, type TFn } from "../../../i18n";
import { useRegisterSettingsEditor, useSettingsNavigation, type SettingsEditorHandle } from "../editorNavigation";
import { ActionBar } from "../../ui/ActionBar";
import { Button } from "../../ui/Button";
import { Input } from "../../ui/Input";
import { CATEGORY } from "../categoryMeta";
import { ConfirmDialog } from "../ConfirmDialog";
import { restartToast } from "../restartToast";
import { AgentForm } from "./AgentForm";
import {
  addPool,
  agentBodyOf,
  agentNodeId,
  deleteAgent,
  deletePool,
  findAgent,
  nodeIdsByName,
  poolNodeId,
  viewModel,
  type AgentBody,
} from "./scopeModel";

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

/** Parse a tree node id back into its coordinates. */
function parseNodeId(
  id: string,
): { kind: "pool"; pool: string } | { kind: "agent"; pool: string; path: string[] } | null {
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
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [nameError, setNameError] = useState("");
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
  /** The user's default-assistant preference (PA-15): deleting that pool
   * requires saving another default first (two owners, honest ordering). */
  const [defaultPool, setDefaultPool] = useState<string | null>(null);
  const formPanelRef = useRef<HTMLDivElement>(null);
  const previewSeq = useRef(0);

  useEffect(() => {
    let cancelled = false;
    fetchPreferences()
      .then((p) => {
        if (!cancelled) setDefaultPool(p.defaultPool || null);
      })
      .catch(() => {});
    return (): void => {
      cancelled = true;
    };
  }, []);

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

  const dirty = model !== null && original !== null && JSON.stringify(model) !== JSON.stringify(original);

  // Register with the settings page's navigation guard (PA-11): expose this
  // editor's dirty/save/discard contract through live refs.
  const saveRef = useRef<() => Promise<boolean>>(() => Promise.resolve(false));
  const cancelRef = useRef<() => void>(() => undefined);
  const dirtyRef = useRef<boolean>(dirty);
  dirtyRef.current = dirty;
  const guardHandle = useMemo<SettingsEditorHandle>(
    () => ({
      isDirty: () => dirtyRef.current,
      save: async () => {
        try {
          return await saveRef.current();
        } catch {
          return false;
        }
      },
      discard: () => cancelRef.current(),
    }),
    [],
  );
  useRegisterSettingsEditor(() => guardHandle);
  const requestLeave = useSettingsNavigation(guardHandle);

  // Default + dangling selection repair: default pool's root first.
  useEffect(() => {
    if (!view) return;
    const valid =
      selection !== null &&
      (() => {
        const parsed = parseNodeId(selection);
        if (!parsed) return false;
        if (parsed.kind === "pool") {
          return view.pools.some((p) => p.name === parsed.pool);
        }
        if (parsed.kind === "agent") {
          return findAgent(view, parsed.pool, parsed.path) !== null;
        }
        return false;
      })();
    if (valid) return;
    const preferred = view.pools.find((pool) => pool.name === defaultPool) ?? view.pools[0];
    if (preferred?.agents[0]) setSelection(agentNodeId(preferred.name, preferred.agents[0].path));
    else setSelection(null);
  }, [view, selection, defaultPool]);

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

  const focusFirstInvalidRef = useRef<(found: ScopeModelIssue[]) => void>(() => undefined);
  focusFirstInvalidRef.current = (found: ScopeModelIssue[]): void => {
    if (!view || found.length === 0) return;
    const ids = nodeIdsByName(view, found[0]!.node);
    if (ids[0]) setSelection(ids[0]);
    formPanelRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  };

  const save = useCallback(async (): Promise<boolean> => {
    if (model === null || saving) return false;
    setSaving(true);
    setSaveError("");
    try {
      const saved = await saveScopeModel(model);
      // The backend canonicalizes on write (deviations only) — reset the
      // form state from what the file actually holds now, including the
      // fresh disk bill (the preview was of the pre-canonical draft).
      const [fresh, freshBill] = await Promise.all([getScopeModel(), getScopeBill()]);
      setModel(fresh);
      setOriginal(fresh);
      setDiskBill(freshBill);
      setPreviewBill(null);
      setIssues([]);
      if (saved.restart_required) restartToast(toast, t);
      return true;
    } catch (e) {
      const found = parseIssues(e);
      if (found.length > 0) {
        setIssues(found);
        focusFirstInvalidRef.current(found);
      }
      setSaveError(formatSaveError(e, t));
      return false;
    } finally {
      setSaving(false);
    }
  }, [model, saving, toast, t]);

  const cancel = useCallback((): void => {
    if (original !== null) setModel(clone(original));
    setPreviewBill(null);
    setIssues([]);
    setSaveError("");
  }, [original]);

  saveRef.current = () => save();
  cancelRef.current = cancel;

  // ── Structure operations ────────────────────────────────────────────────

  const createPool = (name: string): void => {
    update((d) => {
      addPool(d, name);
      const body = agentBodyOf(d, name, [name]);
      if (body && options?.hooks.includes("session_title")) body.hooks = ["+session_title"];
    });
    setSelection(agentNodeId(name, [name]));
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
  /** Deleting the preferred default assistant is blocked (PA-15): choose
   * and save another default in General first. */
  const deleteTargetPoolName = ((): string => {
    if (deleteTarget === null) return "";
    const parsed = parseNodeId(deleteTarget);
    if (parsed?.kind === "pool") return parsed.pool;
    if (parsed?.kind === "agent" && parsed.path.length === 1) return parsed.pool;
    return "";
  })();
  const deletingDefaultPool =
    deleteTargetPoolName !== "" &&
    defaultPool !== null &&
    deleteTargetPoolName === defaultPool;

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

      <div className="flex min-h-0 flex-1 flex-col gap-4 lg:flex-row">
        <div className="w-full shrink-0 overflow-auto rounded-lg border border-hairline bg-canvas-elevated lg:w-64">
          <nav aria-label={t("settings.nav.assistants")} className="space-y-1 p-3">
            {[...view.pools].sort((a, b) => Number(b.name === defaultPool) - Number(a.name === defaultPool)).map((pool) => {
              const root = pool.agents[0];
              return <div key={pool.name} className="flex items-center gap-1 border-b border-hairline py-2">
                <button type="button" className="min-w-0 flex-1 rounded px-2 py-2 text-left hover:bg-hairline-soft" onClick={() => requestLeave(() => setSelection(root ? agentNodeId(pool.name, root.path) : poolNodeId(pool.name)))}>
                  <span className="block truncate font-medium text-ink">{pool.name} {pool.name === defaultPool ? `· ${t("composer.default")}` : ""}</span>
                  <span className="line-clamp-2 text-xs text-mute">{typeof root?.body.description === "string" ? root.body.description : ""}</span>
                </button>
                <Button variant="ghost" size="sm" aria-label={t("settings.poolsPanel.deleteNode", { name: pool.name })} onClick={() => setDeleteTarget(poolNodeId(pool.name))}>×</Button>
              </div>;
            })}
            {creating ? <form className="space-y-2 pt-3" onSubmit={(event) => {
              event.preventDefault();
              const name = newName.trim();
              if (!/^[a-z][a-z0-9-]*$/.test(name)) { setNameError(t("settings.poolsPanel.newAgentInvalidKey")); return; }
              if (view.pools.some((pool) => pool.name === name)) { setNameError(t("settings.poolsPanel.nameTaken", { name })); return; }
              requestLeave(() => { createPool(name); setCreating(false); setNewName(""); setNameError(""); });
            }}>
              <Input autoFocus label={t("settings.poolsPanel.keyLabel")} value={newName} onChange={(event) => setNewName(event.target.value)} />
              {nameError && <p role="alert" className="text-xs text-danger">{nameError}</p>}
              <Button type="submit" size="sm">{t("common.add")}</Button>
              <Button variant="ghost" size="sm" onClick={() => setCreating(false)}>{t("common.cancel")}</Button>
            </form> : !view.poolAsRoot && <Button variant="secondary" size="sm" onClick={() => setCreating(true)}>{t("settings.poolsPanel.newAgentHeading")}</Button>}
          </nav>
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

          {parsed?.kind === "agent" ? (
            (() => {
              const node = findAgent(view, parsed.pool, parsed.path);
              if (!node) return null;
              // Identity gating (PLAN §3.3): an agent that exists only in the
              // dirty draft (absent from the on-disk original) has no
              // persisted identity yet — identity-dependent actions (Skills
              // assignment, MCP, prompt body) wait for a save.
              const identityPersisted =
                original !== null &&
                findAgent(viewModel(original), parsed.pool, parsed.path) !== null;
              return (
                <AgentForm
                  key={selection}
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
                  updateModel={update}
                  agentPath={parsed.path}
                  identityPersisted={identityPersisted}
                  persistedSkillsEnabled={diskBill?.find(
                    (agent) => agent.pool === parsed.pool && agent.agent === node.name,
                  )?.capabilities.some(
                    (capability) => capability.capability === "skills" &&
                      (capability.state === "auto" || capability.state === "declared"),
                  ) ?? false}
                  declaredPools={view.pools.map((p) => p.name)}
                  onChangeInstructions={requestLeave}
                  onSelectAgent={(path) => requestLeave(() => setSelection(agentNodeId(parsed.pool, path)))}
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

      {deleteTarget !== null && deletingDefaultPool ? (
        <ConfirmDialog
          title={t("settings.poolsPanel.newPoolDefaultDeletionTitle")}
          message={t("settings.poolsPanel.newPoolDefaultDeletionMessage", { pool: deleteTargetPoolName })}
          confirmLabel={t("settings.common.stay")}
          onConfirm={() => setDeleteTarget(null)}
          onCancel={() => setDeleteTarget(null)}
        />
      ) : deleteTarget !== null ? (
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
