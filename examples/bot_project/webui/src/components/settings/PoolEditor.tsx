// Pool tree editor. Loads getPool(name) → original + form (deep clones).
// Editing mutates `form`; Save calls savePool(name, form). Save is DEFERRED —
// the MCP selection (name list inside the pool tree), approval settings, tool
// presets and subagent fields all flow through the single Save button.
//
// Skill selection is EAGER (handled inside AgentSkillSelector — assigning a
// skill is a side-effecting disk link backed by dedicated REST routes).
//
// A successful Save with restart_required=true fires the uniform restart toast
// (via restartToast) and arms the persistent indicator. Validation errors
// (HTTP 400 with `{fields: {loc: [msgs]}}`) map onto inline field errors with
// red highlight. The loc keys are dot-joined pydantic paths:
//   main.agent_name, main.max_steps, subagents.0.agent_name, ...
//
// Switching to another pool while this editor is dirty is handled by the parent
// PoolsView (custom ConfirmDialog).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ApprovalConfig,
  ContextMode,
  MainAgentNode,
  PoolSummary,
  PoolTree,
  SubagentNode,
  SystemPromptMode,
  ToolPreset,
} from "../../types/pool";
import { getPool, savePool, addPeer, removePeer, listPools } from "../../lib/poolApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { AgentMcpSelector } from "./AgentMcpSelector";
import { AgentSkillSelector } from "./AgentSkillSelector";
import { PromptEditor } from "./PromptEditor";
import { ConfirmDialog } from "./ConfirmDialog";
import {
  ExternalMainAgentFields,
  IMPLEMENTATION_DEFS,
  type ImplementationChoice,
} from "./ExternalMainAgentFields";
import {
  DEFAULT_EXTERNAL_PROVIDER,
  descriptorFor,
  selectProvider,
} from "../../types/externalProviders";
import { Card } from "../ui/Card";
import { SectionLabel } from "../ui/SectionLabel";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Select } from "../ui/Select";
import { Checkbox } from "../ui/Checkbox";
import { Textarea } from "../ui/Textarea";
import { HelperText } from "../ui/HelperText";
import {
  ChevronDownIcon,
  PlusIcon,
  XIcon,
} from "../ui/icons";
import { Trash2 } from "lucide-react";
import { useT, type MessageKey } from "../../i18n";

interface Props {
  pool: string;
  /** Optional upward dirty signal — PoolsView uses it to guard pool switching. */
  onDirtyChange?: (dirty: boolean) => void;
  /** Optional callback that receives a trigger for persisting the current pool. */
  onSave?: (save: () => Promise<void>) => void;
  /** Optional callback that receives a trigger for reverting the current pool. */
  onCancel?: (cancel: () => void) => void;
}

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

// Normalizes an external pool's provider_kind to a catalog-enabled value so
// unsupported providers (e.g. a legacy pool whose provider isn't enabled in
// the catalog yet) do not silently persist through a Save. Native pools are
// passed through.
const normalizeTree = (tree: PoolTree): PoolTree => {
  if (tree.main.execution_strategy !== "external_coding") return tree;
  const normalized = selectProvider(tree.main.provider_kind);
  if (normalized === tree.main.provider_kind) return tree;
  return {
    ...tree,
    main: { ...tree.main, provider_kind: normalized },
  };
};

// Mirrors the backend regex in `bot/config/prompt_store.py` so the "Edit system
// prompt" button only opens the editor when the name we'd actually save under
// is a valid agent name. Empty / uppercase / trailing-space names produce a
// double-slash URL (`/api/pools/<pool>/agents//prompt`) that aiohttp can't
// route — gate here to avoid the misleading 404.
const AGENT_NAME_RE = /^[a-z][a-z0-9_-]+$/;
const isValidAgentName = (name: string): boolean =>
  typeof name === "string" && AGENT_NAME_RE.test(name);

const PRESETS: ToolPreset[] = ["full", "read_write", "read_only", "minimal", "none"];
const CONTEXT_MODES: ContextMode[] = ["fresh", "fork"];
const CONTEXT_MODE_HINT_KEY: Record<ContextMode, MessageKey> = {
  fresh: "settings.pools.contextModeFresh",
  fork: "settings.pools.contextModeFork",
};
const SYSTEM_PROMPT_MODES: SystemPromptMode[] = ["replace", "append"];
const SYSTEM_PROMPT_MODE_HINT_KEY: Record<SystemPromptMode, MessageKey> = {
  replace: "settings.pools.promptModeReplace",
  append: "settings.pools.promptModeAppend",
};
const FORK_MAX_DEFAULT = 80;
const FORK_MAX_MAX = 100;
const SUPPLEMENTS = ["ast_grep", "todo"] as const;

const PRESET_OPTIONS = PRESETS.map((p) => ({ value: p, label: p }));
const CONTEXT_MODE_OPTIONS = CONTEXT_MODES.map((m) => ({ value: m, label: m }));
const SYSTEM_PROMPT_MODE_OPTIONS = SYSTEM_PROMPT_MODES.map((m) => ({
  value: m,
  label: m,
}));

const defaultSubagent = (): SubagentNode => ({
  agent_name: "",
  description: "",
  max_steps: 80,
  tool_preset: "read_write",
  tool_supplements: [],
  context_mode: "fork",
  mcp: [],
});

type PromptTarget =
  | { kind: "main" }
  | { kind: "sub"; index: number }
  | null;

type FieldErrors = Record<string, string[]>;

export function PoolEditor({ pool, onDirtyChange, onSave, onCancel }: Props) {
  const toast = useToast();
  const t = useT();
  const [original, setOriginal] = useState<PoolTree | null>(null);
  const originalRef = useRef<PoolTree | null>(original);
  originalRef.current = original;
  const [form, setForm] = useState<PoolTree | null>(null);
  const formRef = useRef<PoolTree | null>(form);
  formRef.current = form;
  const [errors, setErrors] = useState<FieldErrors>({});
  const [loadError, setLoadError] = useState<string>("");
  const [allPools, setAllPools] = useState<PoolSummary[] | null>(null);
  const [peerError, setPeerError] = useState<string>("");
  const [peerToRemove, setPeerToRemove] = useState<string | null>(null);
  const [addingPeer, setAddingPeer] = useState<boolean>(false);
  const [newPeer, setNewPeer] = useState<string>("");
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const [promptTarget, setPromptTarget] = useState<PromptTarget>(null);
  const [confirmDeleteSub, setConfirmDeleteSub] = useState<number | null>(null);
  const [confirmSwitch, setConfirmSwitch] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    setLoadError("");
    setPeerError("");
    setErrors({});
    setExpanded(new Set());
    setPromptTarget(null);
    setPeerToRemove(null);
    setAddingPeer(false);
    setNewPeer("");
    Promise.all([getPool(pool), listPools()])
      .then(([tree, pools]) => {
        if (cancelled) return;
        // original preserves the server value verbatim so an unsupported
        // provider_kind (e.g. "pi") is not silently lost on a read+save
        // cycle. form is normalized to a catalog-enabled provider so the
        // UI never shows an unselectable value; the dirty flag then fires,
        // making the provider rewrite an explicit user action on Save.
        setOriginal(tree);
        setForm(clone(normalizeTree(tree)));
        setAllPools(pools);
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [pool]);

  const dirty = useMemo<boolean>(
    () =>
      !!original &&
      !!form &&
      JSON.stringify(original) !== JSON.stringify(form),
    [original, form],
  );

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // ── save ───────────────────────────────────────────────────────────────────
  const save = useCallback(async (): Promise<void> => {
    const currentForm = formRef.current;
    if (!currentForm) return;
    setErrors({});
    try {
      const saved = await savePool(pool, currentForm);
      setOriginal(saved);
      setForm(clone(saved));
      if (saved.restart_required) restartToast(toast, t);
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        try {
          const body = JSON.parse(e.detail) as {
            fields?: FieldErrors;
          };
          if (body.fields) setErrors(body.fields);
          else
            toast.show({
              message: t("settings.pools.saveFailed", { detail: e.detail }),
              tone: "warning",
            });
        } catch {
          toast.show({
            message: t("settings.pools.saveFailed", { detail: e.detail }),
            tone: "warning",
          });
        }
      } else {
        toast.show({
          message: t("settings.pools.saveFailedStatus", { status: e instanceof ApiError ? e.status : "", detail: e instanceof ApiError ? e.detail : String(e) }),
          tone: "warning",
        });
      }
    }
  }, [pool, toast, t]);

  const cancel = useCallback((): void => {
    setForm(clone(originalRef.current));
    setErrors({});
  }, []);

  useEffect(() => {
    onSave?.(save);
  }, [onSave, save]);

  useEffect(() => {
    onCancel?.(cancel);
  }, [onCancel, cancel]);

  const availablePeers = useMemo<PoolSummary[]>(() => {
    if (!Array.isArray(allPools)) return [];
    const current = new Set(form?.peers ?? []);
    return allPools.filter((p) => p.name !== pool && !current.has(p.name));
  }, [allPools, form?.peers, pool]);

  if (loadError) {
    return <p className="text-sm text-error">{t("common.failedToLoad", { error: loadError })}</p>;
  }
  if (!form || !original) {
    return <p className="text-sm text-body">{t("common.loading")}</p>;
  }

  // ── form mutation helpers ──────────────────────────────────────────────────
  const patch = (p: Partial<PoolTree>): void =>
    setForm((prev) => (prev ? { ...prev, ...p } : prev));
  const patchMain = (p: Partial<MainAgentNode>): void =>
    setForm((prev) =>
      prev ? { ...prev, main: { ...prev.main, ...p } } : prev,
    );
  const patchSub = (i: number, p: Partial<SubagentNode>): void =>
    setForm((prev) =>
      prev
        ? {
            ...prev,
            subagents: prev.subagents.map((s, j) =>
              j === i ? { ...s, ...p } : s,
            ),
          }
        : prev,
    );

  const addSubagent = (): void => {
    const next = [...form.subagents, defaultSubagent()];
    patch({ subagents: next });
    setExpanded((prev) => new Set(prev).add(next.length - 1));
  };

  const removeSubagent = (i: number): void => {
    patch({ subagents: form.subagents.filter((_, j) => j !== i) });
    setExpanded((prev) => {
      const s = new Set<number>();
      for (const k of prev) {
        if (k === i) continue;
        s.add(k > i ? k - 1 : k);
      }
      return s;
    });
    setConfirmDeleteSub(null);
  };

  const toggle = (i: number): void =>
    setExpanded((prev) => {
      const s = new Set(prev);
      if (s.has(i)) s.delete(i);
      else s.add(i);
      return s;
    });

  const errFor = (loc: string): string | undefined => {
    const msgs = errors[loc];
    return msgs && msgs.length > 0 ? msgs[0] : undefined;
  };

  const mainAgentNameOf = (name: string): string => {
    if (!allPools) return name;
    const found = allPools.find((p) => p.name === name);
    return found?.main_agent_name ?? name;
  };

  const isExternal = form.main.execution_strategy === "external_coding";
  const effectiveStrategy: ImplementationChoice = isExternal
    ? "external_coding"
    : "react";
  const IMPLEMENTATION_OPTIONS = IMPLEMENTATION_DEFS.map((d) => ({
    value: d.value,
    label: t(d.labelKey),
  }));

  // native→external is destructive to draft subagents, so it is gated behind a
  // confirm. external→native only re-points the runtime and is applied directly.
  const applyExternal = (): void => {
    setConfirmSwitch(false);
    setForm((prev) =>
      prev
        ? {
            ...prev,
            main: {
              ...prev.main,
              execution_strategy: "external_coding",
              provider_kind: DEFAULT_EXTERNAL_PROVIDER,
            },
            subagents: [],
          }
        : prev,
    );
  };

  const applyNative = (): void => {
    setForm((prev) =>
      prev
        ? {
            ...prev,
            main: {
              ...prev.main,
              execution_strategy: "react",
              provider_kind: null,
            },
          }
        : prev,
    );
  };

  const onImplementationChange = (next: ImplementationChoice): void => {
    if (next === "external_coding") {
      if (isExternal) return;
      setConfirmSwitch(true);
      return;
    }
    if (!isExternal) return;
    applyNative();
  };

  const handleAddPeer = async (): Promise<void> => {
    const peer = newPeer.trim();
    if (!peer) return;
    setPeerError("");
    setAddingPeer(false);
    setNewPeer("");
    try {
      const result = await addPeer(pool, peer);
      setOriginal(result.pool_a);
      setForm(clone(normalizeTree(result.pool_a)));
    } catch (e) {
      if (e instanceof ApiError) {
        try {
          const body = JSON.parse(e.detail ?? "") as {
            fields?: FieldErrors;
          };
          if (body.fields?.peer?.length) {
            setPeerError(body.fields.peer[0] ?? String(e));
          } else {
            setPeerError(e.detail ?? String(e));
          }
        } catch {
          setPeerError(e.detail ?? String(e));
        }
      } else {
        setPeerError(String(e));
      }
    }
  };

  const handleRemovePeer = async (peer: string): Promise<void> => {
    setPeerError("");
    setPeerToRemove(null);
    try {
      const result = await removePeer(pool, peer);
      setOriginal(result.pool_a);
      setForm(clone(normalizeTree(result.pool_a)));
    } catch (e) {
      if (e instanceof ApiError) {
        try {
          const body = JSON.parse(e.detail ?? "") as {
            fields?: FieldErrors;
          };
          if (body.fields?.peer?.length) {
            setPeerError(body.fields.peer[0] ?? String(e));
          } else {
            setPeerError(e.detail ?? String(e));
          }
        } catch {
          setPeerError(e.detail ?? String(e));
        }
      } else {
        setPeerError(String(e));
      }
    }
  };

  const editor = (
    <div className="space-y-5">
      <h1 className="text-lg font-semibold text-ink">
        {t("settings.pools.poolTitle", { name: pool })}
      </h1>

      <Card id="main-agent-section">
        <div className="border-b border-hairline px-4 py-2">
          <SectionLabel>{t("settings.pools.mainAgent")}</SectionLabel>
        </div>
        <div className="space-y-5 px-4 py-4">
          {isExternal ? (
            <ExternalMainAgentFields
              node={form.main}
              errFor={errFor}
              patch={patchMain}
              implementationValue={effectiveStrategy}
              onImplementationChange={onImplementationChange}
            />
          ) : (
            <>
              <Select
                label={t("settings.pools.implementation")}
                options={IMPLEMENTATION_OPTIONS}
                value={effectiveStrategy}
                onChange={(e) =>
                  onImplementationChange(e.target.value as ImplementationChoice)
                }
              />
              <MainAgentFields
                node={form.main}
                savedAgentName={original.main.agent_name}
                promptDisabled={!isValidAgentName(form.main.agent_name)}
                errFor={errFor}
                patch={patchMain}
                pool={pool}
                onEditPrompt={() => setPromptTarget({ kind: "main" })}
              />
            </>
          )}
        </div>
      </Card>

      {/* PEERS */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <SectionLabel>{t("settings.pools.peers")}</SectionLabel>
        </div>
        <div className="space-y-2">
          {form.peers.map((peer) => {
            const isConfirming = peerToRemove === peer;
            return (
              <Card key={peer}>
                <div className="flex items-center justify-between gap-3 px-3 py-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-ink">
                      {peer}
                    </div>
                    <div className="truncate text-xs text-body">
                      {t("settings.pools.mainAgentName", { name: mainAgentNameOf(peer) })}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {isConfirming ? (
                      <span className="flex items-center gap-2 text-xs">
                        <Button
                          variant="link"
                          size="sm"
                          className="text-error hover:underline"
                          onClick={() => void handleRemovePeer(peer)}
                        >
                          {t("settings.pools.remove")}
                        </Button>
                        <Button
                          variant="link"
                          size="sm"
                          className="text-body hover:underline"
                          onClick={() => setPeerToRemove(null)}
                        >
                          {t("common.cancel")}
                        </Button>
                      </span>
                    ) : (
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={t("settings.pools.removePeer", { name: peer })}
                        className="text-body hover:text-error"
                        onClick={() => setPeerToRemove(peer)}
                      >
                        <Trash2 size={16} />
                      </Button>
                    )}
                  </div>
                </div>
              </Card>
            );
          })}
          {form.peers.length === 0 && (
            <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-sm text-body">
              {t("settings.pools.noPeers")}
            </p>
          )}
          {addingPeer ? (
            <div className="flex items-center gap-2">
              <Select
                aria-label={t("settings.pools.newPeerPool")}
                options={[
                  { value: "", label: t("settings.pools.selectPool") },
                  ...availablePeers.map((p) => ({
                    value: p.name,
                    label: `${p.name} (${p.main_agent_name})`,
                  })),
                ]}
                value={newPeer}
                onChange={(e) => setNewPeer(e.target.value)}
                className="flex-1"
              />
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void handleAddPeer()}
                disabled={!newPeer}
              >
                {t("common.add")}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setAddingPeer(false);
                  setNewPeer("");
                  setPeerError("");
                }}
              >
                {t("common.cancel")}
              </Button>
            </div>
          ) : (
            <Button
              variant="ghost"
              className="w-full justify-center border border-dashed border-hairline text-body hover:border-ink hover:bg-hairline-soft hover:text-ink"
              onClick={() => setAddingPeer(true)}
              disabled={availablePeers.length === 0}
            >
              <PlusIcon /> {t("settings.pools.addPeer")}
            </Button>
          )}
          {peerError && (
            <p className="text-sm text-error">{peerError}</p>
          )}
        </div>
      </section>

      {/* SUBAGENTS — hidden in external runtime. */}
      {!isExternal ? (
      <section>
        <div className="mb-2 flex items-center justify-between">
          <SectionLabel>{t("settings.pools.subagents")}</SectionLabel>
        </div>
        <div className="space-y-2">
          {form.subagents.map((sub, i) => (
            <SubagentCard
              key={i}
              index={i}
              node={sub}
              savedAgentName={original.subagents[i]?.agent_name ?? sub.agent_name}
              promptDisabled={!isValidAgentName(sub.agent_name)}
              open={expanded.has(i)}
              errFor={errFor}
              confirmingDelete={confirmDeleteSub === i}
              onToggle={() => toggle(i)}
              onPatch={(p) => patchSub(i, p)}
              onRequestDelete={() => setConfirmDeleteSub(i)}
              onConfirmDelete={() => removeSubagent(i)}
              onCancelDelete={() => setConfirmDeleteSub(null)}
              pool={pool}
              onEditPrompt={() =>
                setPromptTarget({ kind: "sub", index: i })
              }
            />
          ))}
          {form.subagents.length === 0 && (
            <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-sm text-body">
              {t("settings.pools.noSubagents")}
            </p>
          )}
          <Button
            variant="ghost"
            className="w-full justify-center border border-dashed border-hairline text-body hover:border-ink hover:bg-hairline-soft hover:text-ink"
            onClick={addSubagent}
          >
            <PlusIcon /> {t("settings.pools.addSubagent")}
          </Button>
        </div>
      </section>
      ) : null}
    </div>
  );

  return (
    <>
      {editor}

      {/* Prompt slide-over — PoolEditor stays mounted underneath so unsaved
          edits to the pool tree are retained. */}
      {promptTarget ? (
        <aside
          className="fixed inset-y-0 right-0 z-50 w-full max-w-2xl overflow-y-auto border-l border-hairline bg-canvas-elevated shadow-floating"
          role="dialog"
          aria-label={t("settings.pools.systemPromptEditor")}
        >
          <PromptEditor
            pool={pool}
            agent={
              promptTarget.kind === "main"
                ? form.main.agent_name
                : form.subagents[promptTarget.index]?.agent_name ??
                  `subagent-${promptTarget.index}`
            }
            onClose={() => setPromptTarget(null)}
            slideOverHeader={
              <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
                <div>
                  <h2 className="text-sm font-semibold text-ink">
                    {t("settings.pools.systemPrompt")}
                  </h2>
                  <HelperText>
                    {promptTarget.kind === "main"
                      ? t("settings.pools.mainAgentLabel")
                      : t("settings.pools.subagentLabel", { index: promptTarget.index + 1 })}
                  </HelperText>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={t("settings.pools.closePromptEditor")}
                  onClick={() => setPromptTarget(null)}
                >
                  <XIcon /> {t("settings.pools.close")}
                </Button>
              </div>
            }
          />
        </aside>
      ) : null}

      {confirmSwitch ? (
        <ConfirmDialog
          title={t("settings.pools.switchExternalTitle")}
          message={t("settings.pools.switchExternalMessage", { provider: descriptorFor(null).label })}
          confirmLabel={t("settings.pools.switchExternal")}
          tone="danger"
          onConfirm={applyExternal}
          onCancel={() => setConfirmSwitch(false)}
        />
      ) : null}
    </>
  );
}

// ─── shared bits ──────────────────────────────────────────────────────────────

interface ErrFn {
  (loc: string): string | undefined;
}

function SupplementsChips({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {SUPPLEMENTS.map((s) => {
        const selected = value.includes(s);
        return (
          <button
            key={s}
            type="button"
            aria-pressed={selected}
            onClick={() =>
              onChange(
                selected ? value.filter((v) => v !== s) : [...value, s],
              )
            }
            className={
              selected
                ? "inline-flex items-center gap-1.5 rounded-full border border-link bg-link-soft px-2.5 py-1 text-xs font-medium text-link-deep"
                : "inline-flex items-center gap-1.5 rounded-full border border-hairline bg-canvas-elevated px-2.5 py-1 text-xs text-body hover:border-ink hover:text-ink"
            }
          >
            {s}
          </button>
        );
      })}
    </div>
  );
}

// ─── Main agent fields ─────────────────────────────────────────────────────

function MainAgentFields({
  node,
  savedAgentName,
  promptDisabled,
  errFor,
  patch,
  pool,
  onEditPrompt,
}: {
  node: MainAgentNode;
  savedAgentName: string;
  promptDisabled: boolean;
  errFor: ErrFn;
  patch: (p: Partial<MainAgentNode>) => void;
  pool: string;
  onEditPrompt: () => void;
}) {
  const t = useT();
  const approval: ApprovalConfig = node.approval ?? {
    enabled: false,
    tools: { write: { allowed_paths: [] }, edit: { allowed_paths: [] } },
  };
  const setApproval = (p: Partial<ApprovalConfig>): void =>
    patch({ approval: { ...approval, ...p } });

  const writePaths = (approval.tools.write?.allowed_paths ?? []).join("\n");
  const editPaths = (approval.tools.edit?.allowed_paths ?? []).join("\n");
  const setToolPaths = (tool: "write" | "edit", text: string): void => {
    const list = text
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    setApproval({
      tools: { ...approval.tools, [tool]: { allowed_paths: list } },
    });
  };

  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Input
          label={t("settings.pools.agentName")}
          required
          error={errFor("main.agent_name")}
          value={node.agent_name}
          onChange={(e) => patch({ agent_name: e.target.value })}
        />
        <Input
          label={t("settings.pools.maxSteps")}
          type="number"
          error={errFor("main.max_steps")}
          value={node.max_steps}
          onChange={(e) => patch({ max_steps: Number(e.target.value) })}
        />
        <Select
          label={t("settings.pools.toolPreset")}
          error={errFor("main.tool_preset")}
          options={PRESET_OPTIONS}
          value={node.tool_preset}
          onChange={(e) =>
            patch({ tool_preset: e.target.value as ToolPreset })
          }
        />
        <div>
          <span className="mb-1 block text-xs font-medium text-body">
            {t("settings.pools.terminal")}
          </span>
          <div className="flex flex-col gap-1.5">
            <Checkbox
              label={t("settings.pools.enableTerminal")}
              checked={node.use_terminal}
              onChange={(e) => patch({ use_terminal: e.target.checked })}
            />
            <Checkbox
              label={t("settings.pools.visibleWindow")}
              checked={node.terminal_visibility}
              onChange={(e) =>
                patch({ terminal_visibility: e.target.checked })
              }
            />
          </div>
        </div>
      </div>

      <Input
        label={t("settings.pools.description")}
        error={errFor("main.description")}
        helper={t("settings.pools.descriptionHelper")}
        value={node.description}
        onChange={(e) => patch({ description: e.target.value })}
      />

      <div>
        <span className="mb-1 block text-xs font-medium text-body">
          {t("settings.pools.toolSupplements")}
        </span>
        <SupplementsChips
          value={node.tool_supplements}
          onChange={(next) => patch({ tool_supplements: next })}
        />
      </div>

      {/* Approval sub-section */}
      <div className="rounded-md border border-hairline bg-hairline-soft p-3">
        <Checkbox
          label={t("settings.pools.approvalRequired")}
          checked={approval.enabled}
          onChange={(e) => setApproval({ enabled: e.target.checked })}
        />
        {approval.enabled && (
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Textarea
              label={t("settings.pools.writeAllowedPaths")}
              mono
              helper={t("settings.pools.onePathPerLine")}
              value={writePaths}
              onChange={(e) => setToolPaths("write", e.target.value)}
              style={{ minHeight: "60px" }}
            />
            <Textarea
              label={t("settings.pools.editAllowedPaths")}
              mono
              helper={t("settings.pools.onePathPerLine")}
              value={editPaths}
              onChange={(e) => setToolPaths("edit", e.target.value)}
              style={{ minHeight: "60px" }}
            />
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <AgentMcpSelector
          value={node.mcp}
          onChange={(next) => patch({ mcp: next })}
        />
        <div>
          <AgentSkillSelector pool={pool} agent={savedAgentName} />
          <p className="mt-1 text-xs italic text-body">
            {t("settings.pools.skillAssignmentsImmediate")}
          </p>
        </div>
      </div>

      <div>
        <Button
          variant="link"
          onClick={onEditPrompt}
          disabled={promptDisabled}
          title={
            promptDisabled ? t("settings.pools.provideAgentNameFirst") : undefined
          }
        >
          {t("settings.pools.systemPromptEdit")}
        </Button>
        {promptDisabled && (
          <HelperText>
            {t("settings.pools.provideAgentNameHelper")}
          </HelperText>
        )}
      </div>
    </>
  );
}

// ─── Subagent card ──────────────────────────────────────────────────────────

function SubagentCard({
  index,
  node,
  savedAgentName,
  promptDisabled,
  open,
  errFor,
  confirmingDelete,
  onToggle,
  onPatch,
  onRequestDelete,
  onConfirmDelete,
  onCancelDelete,
  pool,
  onEditPrompt,
}: {
  index: number;
  node: SubagentNode;
  savedAgentName: string;
  promptDisabled: boolean;
  open: boolean;
  errFor: ErrFn;
  confirmingDelete: boolean;
  onToggle: () => void;
  onPatch: (p: Partial<SubagentNode>) => void;
  onRequestDelete: () => void;
  onConfirmDelete: () => void;
  onCancelDelete: () => void;
  pool: string;
  onEditPrompt: () => void;
}) {
  const t = useT();
  const summary = `${node.tool_preset} · mcp·${node.mcp.length} · ${
    node.context_mode
  }`;
  return (
    <Card>
      <div className="flex items-center gap-2 px-3 py-2.5">
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2.5 rounded px-1 py-0.5 text-left hover:bg-hairline-soft"
          aria-expanded={open}
        >
          <ChevronDownIcon open={open} className="text-body" />
          <span className="truncate text-sm font-medium text-ink">
            {node.agent_name || (
              <span className="italic text-body">
                {t("settings.pools.untitledSubagent")}
              </span>
            )}
          </span>
          <span className="truncate text-xs text-body">
            {summary}
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-2">
          {confirmingDelete ? (
            <span className="flex items-center gap-2 text-xs">
              <Button
                variant="link"
                size="sm"
                className="text-error hover:underline"
                onClick={onConfirmDelete}
              >
                {t("common.delete")}
              </Button>
              <Button
                variant="link"
                size="sm"
                className="text-body hover:underline"
                onClick={onCancelDelete}
              >
                {t("common.cancel")}
              </Button>
            </span>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              aria-label={t("settings.pools.removeSubagent", { name: node.agent_name || String(index) })}
              className="text-body hover:text-error"
              onClick={onRequestDelete}
            >
              <Trash2 size={16} />
            </Button>
          )}
        </div>
      </div>
      {open && (
        <div className="space-y-4 border-t border-hairline px-4 py-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Input
              label={t("settings.pools.agentName")}
              required
              error={errFor(`subagents.${index}.agent_name`)}
              value={node.agent_name}
              onChange={(e) => onPatch({ agent_name: e.target.value })}
            />
            <Input
              label={t("settings.pools.maxSteps")}
              type="number"
              error={errFor(`subagents.${index}.max_steps`)}
              value={node.max_steps}
              onChange={(e) =>
                onPatch({ max_steps: Number(e.target.value) })
              }
            />
          </div>

          <Input
            label={t("settings.pools.description")}
            required
            error={errFor(`subagents.${index}.description`)}
            value={node.description}
            onChange={(e) => onPatch({ description: e.target.value })}
          />

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Select
              label={t("settings.pools.toolPreset")}
              error={errFor(`subagents.${index}.tool_preset`)}
              options={PRESET_OPTIONS}
              value={node.tool_preset}
              onChange={(e) =>
                onPatch({ tool_preset: e.target.value as ToolPreset })
              }
            />
            <div>
              <Select
                label={t("settings.pools.contextMode")}
                error={errFor(`subagents.${index}.context_mode`)}
                options={CONTEXT_MODE_OPTIONS}
                value={node.context_mode}
                onChange={(e) =>
                  onPatch({
                    context_mode: e.target.value as ContextMode,
                  })
                }
              />
              <HelperText>
                {t(CONTEXT_MODE_HINT_KEY[node.context_mode])}
              </HelperText>
            </div>
            <div>
              <Select
                label={t("settings.pools.systemPromptMode")}
                error={errFor(`subagents.${index}.system_prompt_mode`)}
                options={SYSTEM_PROMPT_MODE_OPTIONS}
                value={node.system_prompt_mode ?? "replace"}
                onChange={(e) =>
                  onPatch({
                    system_prompt_mode: e.target.value as SystemPromptMode,
                  })
                }
              />
              <HelperText>
                {t(SYSTEM_PROMPT_MODE_HINT_KEY[node.system_prompt_mode ?? "replace"])}
              </HelperText>
            </div>
            {node.context_mode === "fork" && (
              <div>
                <Input
                  label={t("settings.pools.forkMaxMessages")}
                  type="number"
                  min={1}
                  max={FORK_MAX_MAX}
                  error={errFor(`subagents.${index}.fork_max_messages`)}
                  value={node.fork_max_messages ?? FORK_MAX_DEFAULT}
                  onChange={(e) => {
                    const n = Number.parseInt(e.target.value, 10);
                    onPatch({
                      fork_max_messages: Number.isFinite(n)
                        ? Math.min(Math.max(n, 1), FORK_MAX_MAX)
                        : FORK_MAX_DEFAULT,
                    });
                  }}
                />
                <HelperText>
                  {t("settings.pools.forkMaxHelper", { max: FORK_MAX_MAX, default: FORK_MAX_DEFAULT })}
                </HelperText>
              </div>
            )}
          </div>

          <div>
            <span className="mb-1 block text-xs font-medium text-body">
              {t("settings.pools.toolSupplements")}
            </span>
            <SupplementsChips
              value={node.tool_supplements}
              onChange={(next) => onPatch({ tool_supplements: next })}
            />
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <AgentMcpSelector
              value={node.mcp}
              onChange={(next) => onPatch({ mcp: next })}
            />
            <div>
              <AgentSkillSelector pool={pool} agent={savedAgentName} />
              <p className="mt-1 text-xs italic text-body">
                {t("settings.pools.skillAssignmentsImmediate")}
              </p>
            </div>
          </div>

          <div>
            <Button
              variant="link"
              onClick={onEditPrompt}
              disabled={promptDisabled}
              title={
                promptDisabled
                  ? t("settings.pools.provideAgentNameFirst")
                  : undefined
              }
            >
              {t("settings.pools.systemPromptEdit")}
            </Button>
            {promptDisabled && (
              <HelperText>
                {t("settings.pools.provideAgentNameHelper")}
              </HelperText>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}