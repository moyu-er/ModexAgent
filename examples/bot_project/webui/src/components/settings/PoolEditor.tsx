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
  PoolTree,
  SubagentNode,
  SystemPromptMode,
  ToolPreset,
} from "../../types/pool";
import { getPool, savePool } from "../../lib/poolApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { AgentMcpSelector } from "./AgentMcpSelector";
import { AgentSkillSelector } from "./AgentSkillSelector";
import { PromptEditor } from "./PromptEditor";
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
const CONTEXT_MODE_HINT: Record<ContextMode, string> = {
  fresh: "Start each turn from an empty context window.",
  fork: "Fork the parent's recent message history as the starting context.",
};
const SYSTEM_PROMPT_MODES: SystemPromptMode[] = ["replace", "append"];
const SYSTEM_PROMPT_MODE_HINT: Record<SystemPromptMode, string> = {
  replace: "Use the subagent's own system prompt only.",
  append: "Append the subagent's prompt after the parent's system prompt.",
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
  const [original, setOriginal] = useState<PoolTree | null>(null);
  const originalRef = useRef<PoolTree | null>(original);
  originalRef.current = original;
  const [form, setForm] = useState<PoolTree | null>(null);
  const formRef = useRef<PoolTree | null>(form);
  formRef.current = form;
  const [errors, setErrors] = useState<FieldErrors>({});
  const [loadError, setLoadError] = useState<string>("");
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const [promptTarget, setPromptTarget] = useState<PromptTarget>(null);
  const [confirmDeleteSub, setConfirmDeleteSub] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoadError("");
    setErrors({});
    setExpanded(new Set());
    setPromptTarget(null);
    getPool(pool)
      .then((tree) => {
        if (cancelled) return;
        setOriginal(tree);
        setForm(clone(tree));
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
      if (saved.restart_required) restartToast(toast);
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        try {
          const body = JSON.parse(e.detail) as {
            fields?: FieldErrors;
          };
          if (body.fields) setErrors(body.fields);
          else
            toast.show({
              message: `Save failed: ${e.detail}`,
              tone: "warning",
            });
        } catch {
          toast.show({
            message: `Save failed: ${e.detail}`,
            tone: "warning",
          });
        }
      } else {
        toast.show({
          message: `Save failed: ${e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)}`,
          tone: "warning",
        });
      }
    }
  }, [pool, toast]);

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

  if (loadError) {
    return <p className="text-sm text-error">Failed to load: {loadError}</p>;
  }
  if (!form || !original) {
    return <p className="text-sm text-body">Loading…</p>;
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

  const editor = (
    <div className="space-y-5">
      <h1 className="text-lg font-semibold text-ink">
        Pool: {pool}
      </h1>

      {/* MAIN AGENT (fixed-expanded, not collapsible) */}
      <Card id="main-agent-section">
        <div className="border-b border-hairline px-4 py-2">
          <SectionLabel>Main agent</SectionLabel>
        </div>
        <div className="space-y-5 px-4 py-4">
          <MainAgentFields
            node={form.main}
            savedAgentName={original.main.agent_name}
            promptDisabled={!isValidAgentName(form.main.agent_name)}
            errFor={errFor}
            patch={patchMain}
            pool={pool}
            onEditPrompt={() => setPromptTarget({ kind: "main" })}
          />
        </div>
      </Card>

      {/* SUBAGENTS */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <SectionLabel>Subagents</SectionLabel>
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
              No subagents in this pool.
            </p>
          )}
          <Button
            variant="ghost"
            className="w-full justify-center border border-dashed border-hairline text-body hover:border-ink hover:bg-hairline-soft hover:text-ink"
            onClick={addSubagent}
          >
            <PlusIcon /> Add subagent
          </Button>
        </div>
      </section>
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
          aria-label="System prompt editor"
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
                    System prompt
                  </h2>
                  <HelperText>
                    {promptTarget.kind === "main"
                      ? "Main agent"
                      : `Subagent #${promptTarget.index + 1}`}
                  </HelperText>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label="Close prompt editor"
                  onClick={() => setPromptTarget(null)}
                >
                  <XIcon /> Close
                </Button>
              </div>
            }
          />
        </aside>
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
          label="Agent name"
          required
          error={errFor("main.agent_name")}
          value={node.agent_name}
          onChange={(e) => patch({ agent_name: e.target.value })}
        />
        <Input
          label="Max steps"
          type="number"
          error={errFor("main.max_steps")}
          value={node.max_steps}
          onChange={(e) => patch({ max_steps: Number(e.target.value) })}
        />
        <Select
          label="Tool preset"
          error={errFor("main.tool_preset")}
          options={PRESET_OPTIONS}
          value={node.tool_preset}
          onChange={(e) =>
            patch({ tool_preset: e.target.value as ToolPreset })
          }
        />
        <div>
          <span className="mb-1 block text-xs font-medium text-body">
            Terminal
          </span>
          <div className="flex flex-col gap-1.5">
            <Checkbox
              label="Enable terminal"
              checked={node.use_terminal}
              onChange={(e) => patch({ use_terminal: e.target.checked })}
            />
            <Checkbox
              label="Visible window"
              checked={node.terminal_visibility}
              onChange={(e) =>
                patch({ terminal_visibility: e.target.checked })
              }
            />
          </div>
        </div>
      </div>

      <div>
        <span className="mb-1 block text-xs font-medium text-body">
          Tool supplements
        </span>
        <SupplementsChips
          value={node.tool_supplements}
          onChange={(next) => patch({ tool_supplements: next })}
        />
      </div>

      {/* Approval sub-section */}
      <div className="rounded-md border border-hairline bg-hairline-soft p-3">
        <Checkbox
          label="Approval required for write/edit tools"
          checked={approval.enabled}
          onChange={(e) => setApproval({ enabled: e.target.checked })}
        />
        {approval.enabled && (
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Textarea
              label="Write allowed paths (one per line)"
              mono
              helper="One path per line."
              value={writePaths}
              onChange={(e) => setToolPaths("write", e.target.value)}
              style={{ minHeight: "60px" }}
            />
            <Textarea
              label="Edit allowed paths (one per line)"
              mono
              helper="One path per line."
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
            Skill assignments save immediately.
          </p>
        </div>
      </div>

      <div>
        <Button
          variant="link"
          onClick={onEditPrompt}
          disabled={promptDisabled}
          title={
            promptDisabled ? "Provide an agent name first" : undefined
          }
        >
          System prompt [Edit]
        </Button>
        {promptDisabled && (
          <HelperText>
            Provide an agent name (lowercase, letters/digits/_/-) to edit its
            system prompt.
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
                Untitled subagent
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
                Delete
              </Button>
              <Button
                variant="link"
                size="sm"
                className="text-body hover:underline"
                onClick={onCancelDelete}
              >
                Cancel
              </Button>
            </span>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              aria-label={`Remove subagent ${node.agent_name || index}`}
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
              label="Agent name"
              required
              error={errFor(`subagents.${index}.agent_name`)}
              value={node.agent_name}
              onChange={(e) => onPatch({ agent_name: e.target.value })}
            />
            <Input
              label="Max steps"
              type="number"
              error={errFor(`subagents.${index}.max_steps`)}
              value={node.max_steps}
              onChange={(e) =>
                onPatch({ max_steps: Number(e.target.value) })
              }
            />
          </div>

          <Input
            label="Description"
            required
            error={errFor(`subagents.${index}.description`)}
            value={node.description}
            onChange={(e) => onPatch({ description: e.target.value })}
          />

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Select
              label="Tool preset"
              error={errFor(`subagents.${index}.tool_preset`)}
              options={PRESET_OPTIONS}
              value={node.tool_preset}
              onChange={(e) =>
                onPatch({ tool_preset: e.target.value as ToolPreset })
              }
            />
            <div>
              <Select
                label="Context mode"
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
                {CONTEXT_MODE_HINT[node.context_mode]}
              </HelperText>
            </div>
            <div>
              <Select
                label="System prompt mode"
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
                {
                  SYSTEM_PROMPT_MODE_HINT[
                    node.system_prompt_mode ?? "replace"
                  ]
                }
              </HelperText>
            </div>
            {node.context_mode === "fork" && (
              <div>
                <Input
                  label="Fork max messages"
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
                  Parent-message cap (1–{FORK_MAX_MAX}). Default{" "}
                  {FORK_MAX_DEFAULT}.
                </HelperText>
              </div>
            )}
          </div>

          <div>
            <span className="mb-1 block text-xs font-medium text-body">
              Tool supplements
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
                Skill assignments save immediately.
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
                  ? "Provide an agent name first"
                  : undefined
              }
            >
              System prompt [Edit]
            </Button>
            {promptDisabled && (
              <HelperText>
                Provide an agent name (lowercase, letters/digits/_/-) to edit
                its system prompt.
              </HelperText>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}