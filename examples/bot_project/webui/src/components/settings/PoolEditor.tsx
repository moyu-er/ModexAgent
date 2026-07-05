// Pool tree editor. Loads getPool(name) → original + form (deep clones).
// Editing mutates `form`; Save calls savePool(name, form). Save is DEFERRED —
// the MCP selection (name list inside the pool tree), approval settings, tool
// presets and subagent fields all flow through the single Save button.
//
// Skill selection is EAGER (handled inside AgentSkillSelector — assigning a
// skill is a side-effecting disk copy backed by dedicated REST routes).
//
// A successful Save with restart_required=true fires the uniform restart toast
// (via restartToast) and arms the persistent indicator. Validation errors
// (HTTP 400 with `{fields: {loc: [msgs]}}`) map onto inline field errors with
// red highlight. The loc keys are dot-joined pydantic paths:
//   main.agent_name, main.max_steps, subagents.0.agent_name, ...
//
// Switching to another pool while this editor is dirty is handled by the parent
// PoolsView (custom ConfirmDialog).

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
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
import { Chevron, PlusIcon, TrashIcon } from "./icons";

interface Props {
  pool: string;
  /** Optional upward dirty signal — PoolsView uses it to guard pool switching. */
  onDirtyChange?: (dirty: boolean) => void;
}

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

const INPUT =
  "w-full rounded border border-input-border bg-input-bg px-2.5 py-1.5 text-sm text-text-primary placeholder:text-text-disabled focus:border-input-focus focus:outline-none focus:ring-1 focus:ring-input-focus";
const LABEL = "mb-1 block text-xs font-medium text-text-secondary";
const INPUT_ERR =
  "w-full rounded border border-error bg-input-bg px-2.5 py-1.5 text-sm text-text-primary focus:border-error focus:outline-none focus:ring-1 focus:ring-error";

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
const SUPPLEMENTS = ["ast_grep"] as const;

const defaultSubagent = (): SubagentNode => ({
  agent_name: "",
  description: "",
  max_steps: 16,
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

export function PoolEditor({ pool, onDirtyChange }: Props) {
  const toast = useToast();
  const [original, setOriginal] = useState<PoolTree | null>(null);
  const [form, setForm] = useState<PoolTree | null>(null);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [loadError, setLoadError] = useState<string>("");
  const [saving, setSaving] = useState<boolean>(false);
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

  if (loadError) {
    return <p className="text-sm text-error">Failed to load: {loadError}</p>;
  }
  if (!form || !original) {
    return <p className="text-sm text-text-secondary">Loading…</p>;
  }

  // ── form mutation helpers ──────────────────────────────────────────────────
  const patch = (p: Partial<PoolTree>): void =>
    setForm((prev) => (prev ? { ...prev, ...p } : prev));
  const patchMain = (p: Partial<MainAgentNode>): void =>
    setForm((prev) => (prev ? { ...prev, main: { ...prev.main, ...p } } : prev));
  const patchSub = (i: number, p: Partial<SubagentNode>): void =>
    setForm((prev) =>
      prev
        ? {
            ...prev,
            subagents: prev.subagents.map((s, j) => (j === i ? { ...s, ...p } : s)),
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

  // ── save ───────────────────────────────────────────────────────────────────
  const onSave = async (): Promise<void> => {
    setSaving(true);
    setErrors({});
    try {
      const saved = await savePool(pool, form);
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
          else toast.show({ message: `Save failed: ${e.detail}`, tone: "warning" });
        } catch {
          toast.show({ message: `Save failed: ${e.detail}`, tone: "warning" });
        }
      } else {
        toast.show({
          message: `Save failed: ${e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)}`,
          tone: "warning",
        });
      }
    } finally {
      setSaving(false);
    }
  };

  const onCancel = (): void => {
    setForm(clone(original));
    setErrors({});
  };

  // ── prompt editing overlay ─────────────────────────────────────────────────
  if (promptTarget) {
    const agentName =
      promptTarget.kind === "main"
        ? form.main.agent_name
        : form.subagents[promptTarget.index]?.agent_name ?? `subagent-${promptTarget.index}`;
    return (
      <PromptEditor
        pool={pool}
        agent={agentName}
        onClose={() => setPromptTarget(null)}
      />
    );
  }

  const errFor = (loc: string): string | undefined => {
    const msgs = errors[loc];
    return msgs && msgs.length > 0 ? msgs[0] : undefined;
  };

  return (
    <div className="space-y-5">
      <h1 className="text-lg font-semibold text-text-primary">Pool: {pool}</h1>

      {/* MAIN AGENT (fixed-expanded, not collapsible) */}
      <section className="rounded-lg border border-card-border bg-content-bg">
        <div className="border-b border-divider px-4 py-2">
          <h2 className="text-[11px] font-semibold uppercase tracking-wide text-text-disabled">
            Main agent
          </h2>
        </div>
        <div className="space-y-5 px-4 py-4">
          <MainAgentFields
            node={form.main}
            errors={errors}
            errFor={errFor}
            patch={patchMain}
            pool={pool}
            onEditPrompt={() => setPromptTarget({ kind: "main" })}
          />
        </div>
      </section>

      {/* SUBAGENTS */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-wide text-text-disabled">
            Subagents
          </h2>
        </div>
        <div className="space-y-2">
          {form.subagents.map((sub, i) => (
            <SubagentCard
              key={i}
              index={i}
              node={sub}
              open={expanded.has(i)}
              errors={errors}
              errFor={errFor}
              confirmingDelete={confirmDeleteSub === i}
              onToggle={() => toggle(i)}
              onPatch={(p) => patchSub(i, p)}
              onRequestDelete={() => setConfirmDeleteSub(i)}
              onConfirmDelete={() => removeSubagent(i)}
              onCancelDelete={() => setConfirmDeleteSub(null)}
              pool={pool}
              onEditPrompt={() => setPromptTarget({ kind: "sub", index: i })}
            />
          ))}
          {form.subagents.length === 0 && (
            <p className="rounded-md border border-dashed border-input-border px-3 py-6 text-center text-sm text-text-secondary">
              No subagents in this pool.
            </p>
          )}
          <button
            type="button"
            className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-input-border py-2.5 text-sm text-text-secondary hover:border-text-secondary hover:bg-sidebar-hover hover:text-text-primary"
            onClick={addSubagent}
          >
            <PlusIcon /> Add subagent
          </button>
        </div>
      </section>

      {/* Footer: Save / Cancel */}
      <div className="flex justify-end gap-2 border-t border-divider pt-4">
        <button
          type="button"
          className="rounded border border-divider px-4 py-1.5 text-sm text-text-primary hover:bg-sidebar-hover disabled:opacity-50"
          onClick={onCancel}
          disabled={!dirty || saving}
        >
          Cancel
        </button>
        <button
          type="button"
          className="rounded bg-btn-primary px-4 py-1.5 text-sm text-btn-primary-text hover:opacity-90 disabled:opacity-50"
          onClick={() => void onSave()}
          disabled={!dirty || saving}
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}

// ─── shared bits ──────────────────────────────────────────────────────────────

interface ErrFn {
  (loc: string): string | undefined;
}

function ErrorNote({ msg }: { msg?: string }): ReactNode {
  if (!msg) return null;
  return <p className="mt-1 text-xs text-error">{msg}</p>;
}

function Field({
  label,
  required,
  error,
  className,
  children,
}: {
  label: string;
  required?: boolean;
  error?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={className}>
      <label className={LABEL}>
        {label}
        {required && <span className="text-error"> *</span>}
      </label>
      {children}
      <ErrorNote msg={error} />
    </div>
  );
}

function PresetSelect({
  value,
  onChange,
  error,
  id,
}: {
  value: ToolPreset;
  onChange: (v: ToolPreset) => void;
  /** When true, render the select with error styling. */
  error?: boolean;
  id?: string;
}) {
  return (
    <select
      id={id}
      className={error ? INPUT_ERR : INPUT}
      value={value}
      onChange={(e) => onChange(e.target.value as ToolPreset)}
    >
      {PRESETS.map((p) => (
        <option key={p} value={p}>
          {p}
        </option>
      ))}
    </select>
  );
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
                ? "inline-flex items-center gap-1.5 rounded-full border border-ai-brand bg-user-bubble px-2.5 py-1 text-xs font-medium text-user-bubble-text"
                : "inline-flex items-center gap-1.5 rounded-full border border-input-border bg-input-bg px-2.5 py-1 text-xs text-text-secondary hover:border-text-secondary hover:text-text-primary"
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
  errors,
  errFor,
  patch,
  pool,
  onEditPrompt,
}: {
  node: MainAgentNode;
  errors: FieldErrors;
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
        <Field label="Agent name" required error={errFor("main.agent_name")}>
          <input
            className={errors["main.agent_name"] ? INPUT_ERR : INPUT}
            value={node.agent_name}
            onChange={(e) => patch({ agent_name: e.target.value })}
          />
        </Field>
        <Field label="Max steps" error={errFor("main.max_steps")}>
          <input
            type="number"
            className={errors["main.max_steps"] ? INPUT_ERR : INPUT}
            value={node.max_steps}
            onChange={(e) => patch({ max_steps: Number(e.target.value) })}
          />
        </Field>
        <Field label="Tool preset" error={errFor("main.tool_preset")}>
          <PresetSelect
            value={node.tool_preset}
            onChange={(v) => patch({ tool_preset: v })}
            error={!!errors["main.tool_preset"]}
          />
        </Field>
        <div>
          <span className={LABEL}>Terminal</span>
          <div className="flex flex-col gap-1.5">
            <label className="flex items-center gap-2 text-xs text-text-primary">
              <input
                type="checkbox"
                checked={node.use_terminal}
                onChange={(e) => patch({ use_terminal: e.target.checked })}
                className="h-3.5 w-3.5"
              />
              Enable terminal
            </label>
            <label className="flex items-center gap-2 text-xs text-text-primary">
              <input
                type="checkbox"
                checked={node.terminal_visibility}
                onChange={(e) => patch({ terminal_visibility: e.target.checked })}
                className="h-3.5 w-3.5"
              />
              Visible window
            </label>
          </div>
        </div>
      </div>

      <div>
        <label className={LABEL}>Tool supplements</label>
        <SupplementsChips
          value={node.tool_supplements}
          onChange={(next) => patch({ tool_supplements: next })}
        />
      </div>

      {/* Approval sub-section */}
      <div className="rounded-md border border-divider bg-sidebar-bg p-3">
        <label className="flex items-center gap-2 text-xs font-medium text-text-primary">
          <input
            type="checkbox"
            checked={approval.enabled}
            onChange={(e) => setApproval({ enabled: e.target.checked })}
            className="h-3.5 w-3.5"
          />
          Approval required for write/edit tools
        </label>
        {approval.enabled && (
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Write allowed paths (one per line)">
              <textarea
                className={`${INPUT} min-h-[60px] font-mono`}
                value={writePaths}
                onChange={(e) => setToolPaths("write", e.target.value)}
              />
            </Field>
            <Field label="Edit allowed paths (one per line)">
              <textarea
                className={`${INPUT} min-h-[60px] font-mono`}
                value={editPaths}
                onChange={(e) => setToolPaths("edit", e.target.value)}
              />
            </Field>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <AgentMcpSelector
          value={node.mcp}
          onChange={(next) => patch({ mcp: next })}
        />
        <AgentSkillSelector
          pool={pool}
          agent={node.agent_name}
        />
      </div>

      <div>
        <button
          type="button"
          className="text-xs text-ai-brand hover:underline"
          onClick={onEditPrompt}
        >
          System prompt [Edit]
        </button>
      </div>
    </>
  );
}

// ─── Subagent card ──────────────────────────────────────────────────────────

function SubagentCard({
  index,
  node,
  open,
  errors,
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
  open: boolean;
  errors: FieldErrors;
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
  const summary = `${node.tool_preset} · mcp·${node.mcp.length}`;
  return (
    <div className="rounded-lg border border-card-border bg-content-bg">
      <div className="flex items-center gap-2 px-3 py-2.5">
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2.5 rounded px-1 py-0.5 text-left hover:bg-sidebar-hover"
        >
          <Chevron open={open} />
          <span className="truncate text-sm font-medium text-text-primary">
            {node.agent_name || (
              <span className="italic text-text-secondary">Untitled subagent</span>
            )}
          </span>
          <span className="truncate text-xs text-text-secondary">{summary}</span>
        </button>
        <div className="flex shrink-0 items-center gap-2">
          {confirmingDelete ? (
            <span className="flex items-center gap-2 text-xs">
              <button
                type="button"
                className="font-medium text-error hover:underline"
                onClick={onConfirmDelete}
              >
                Delete
              </button>
              <button
                type="button"
                className="text-text-secondary hover:underline"
                onClick={onCancelDelete}
              >
                Cancel
              </button>
            </span>
          ) : (
            <button
              type="button"
              aria-label={`Remove subagent ${node.agent_name || index}`}
              className="text-text-secondary hover:text-error"
              onClick={onRequestDelete}
            >
              <TrashIcon />
            </button>
          )}
        </div>
      </div>
      {open && (
        <div className="space-y-4 border-t border-divider px-4 py-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field
              label="Agent name"
              required
              error={errFor(`subagents.${index}.agent_name`)}
            >
              <input
                className={errors[`subagents.${index}.agent_name`] ? INPUT_ERR : INPUT}
                value={node.agent_name}
                onChange={(e) => onPatch({ agent_name: e.target.value })}
              />
            </Field>
            <Field
              label="Max steps"
              error={errFor(`subagents.${index}.max_steps`)}
            >
              <input
                type="number"
                className={errors[`subagents.${index}.max_steps`] ? INPUT_ERR : INPUT}
                value={node.max_steps}
                onChange={(e) => onPatch({ max_steps: Number(e.target.value) })}
              />
            </Field>
          </div>

          <Field
            label="Description"
            required
            error={errFor(`subagents.${index}.description`)}
          >
            <input
              className={errors[`subagents.${index}.description`] ? INPUT_ERR : INPUT}
              value={node.description}
              onChange={(e) => onPatch({ description: e.target.value })}
            />
          </Field>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field
              label="Tool preset"
              error={errFor(`subagents.${index}.tool_preset`)}
            >
              <PresetSelect
                value={node.tool_preset}
                onChange={(v) => onPatch({ tool_preset: v })}
                error={!!errors[`subagents.${index}.tool_preset`]}
              />
            </Field>
            <Field
              label="Context mode"
              error={errFor(`subagents.${index}.context_mode`)}
            >
              <select
                className={errors[`subagents.${index}.context_mode`] ? INPUT_ERR : INPUT}
                value={node.context_mode}
                onChange={(e) => onPatch({ context_mode: e.target.value as ContextMode })}
              >
                {CONTEXT_MODES.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[11px] text-text-secondary">
                {CONTEXT_MODE_HINT[node.context_mode]}
              </p>
            </Field>
            <Field
              label="System prompt mode"
              error={errFor(`subagents.${index}.system_prompt_mode`)}
            >
              <select
                className={
                  errors[`subagents.${index}.system_prompt_mode`] ? INPUT_ERR : INPUT
                }
                value={node.system_prompt_mode ?? "replace"}
                onChange={(e) =>
                  onPatch({
                    system_prompt_mode: e.target.value as SystemPromptMode,
                  })
                }
              >
                {SYSTEM_PROMPT_MODES.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[11px] text-text-secondary">
                {SYSTEM_PROMPT_MODE_HINT[node.system_prompt_mode ?? "replace"]}
              </p>
            </Field>
            {node.context_mode === "fork" && (
              <Field
                label="Fork max messages"
                error={errFor(`subagents.${index}.fork_max_messages`)}
              >
                <input
                  type="number"
                  min={1}
                  max={FORK_MAX_MAX}
                  className={
                    errors[`subagents.${index}.fork_max_messages`] ? INPUT_ERR : INPUT
                  }
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
                <p className="mt-1 text-[11px] text-text-secondary">
                  Parent-message cap (1–{FORK_MAX_MAX}). Default {FORK_MAX_DEFAULT}.
                </p>
              </Field>
            )}
          </div>

          <div>
            <label className={LABEL}>Tool supplements</label>
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
            <AgentSkillSelector
              pool={pool}
              agent={node.agent_name}
            />
          </div>

          <div>
            <button
              type="button"
              className="text-xs text-ai-brand hover:underline"
              onClick={onEditPrompt}
            >
              System prompt [Edit]
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
