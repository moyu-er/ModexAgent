import { useState } from "react";
import type { ReactNode } from "react";
import type { SecretMaskValue, SecretWrite } from "../../types/config";
import { SecretField } from "./SecretField";

interface ModelEntry {
  name: string;
  model: string;
  capabilities: string[];
  temperature: number;
  max_output_tokens: number;
}

interface Provider {
  key: string;
  name: string;
  url: string;
  api_key: unknown; // SecretMaskValue when read from backend
  models: ModelEntry[];
}

interface Props {
  values: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}

// Closed enum (backend Modality). Multi-select via chips — never free text,
// since the backend rejects unknown capability strings.
const CAPABILITIES = [
  { value: "text", label: "Text", glyph: "Aa" },
  { value: "image", label: "Image", glyph: "◫" },
  { value: "video", label: "Video", glyph: "▶" },
  { value: "audio", label: "Audio", glyph: "♪" },
] as const;

const INPUT =
  "w-full rounded border border-input-border bg-input-bg px-2.5 py-1.5 text-sm text-text-primary placeholder:text-text-disabled focus:border-input-focus focus:outline-none focus:ring-1 focus:ring-input-focus";
const LABEL = "mb-1 block text-xs font-medium text-text-secondary";

type Confirm =
  | { kind: "provider"; pi: number }
  | { kind: "model"; pi: number; mi: number }
  | null;

export function ModelEditor({ values, onChange }: Props) {
  const defaultProvider = String(values.default_provider ?? "");
  const defaultModel = String(values.default_model ?? "");
  const maxContext = Number(values.max_context_tokens ?? 0);
  const providers = (values.providers as Provider[] | undefined) ?? [];

  const [expanded, setExpanded] = useState<Set<number>>(() => {
    const s = new Set<number>();
    const idx = providers.findIndex(
      (p) => defaultProvider !== "" && p.name === defaultProvider,
    );
    if (idx >= 0) s.add(idx);
    else if (providers.length > 0) s.add(0);
    return s;
  });
  const [confirm, setConfirm] = useState<Confirm>(null);

  const update = (patch: Record<string, unknown>): void => {
    onChange({ ...values, ...patch });
  };
  const updateProviders = (next: Provider[]): void => update({ providers: next });

  const updateModel = (pi: number, mi: number, patch: Partial<ModelEntry>): void => {
    updateProviders(
      providers.map((q, i) =>
        i === pi
          ? { ...q, models: q.models.map((mm, j) => (j === mi ? { ...mm, ...patch } : mm)) }
          : q,
      ),
    );
  };

  const toggle = (pi: number): void => {
    setExpanded((prev) => {
      const s = new Set(prev);
      if (s.has(pi)) s.delete(pi);
      else s.add(pi);
      return s;
    });
  };

  // All (provider.name, model.name) combos across every provider. The dropdown
  // keys options by index into this array (not by name) so provider/model names
  // containing spaces or any other character cannot corrupt the selection.
  const combos: { pName: string; mName: string }[] = providers.flatMap((p) =>
    (p.models ?? []).map((m) => ({ pName: p.name, mName: m.name })),
  );
  const currentComboIdx = combos.findIndex(
    (c) => c.pName === defaultProvider && c.mName === defaultModel,
  );
  const comboExists = currentComboIdx >= 0 && defaultProvider !== "";

  const addProvider = (): void => {
    updateProviders([
      ...providers,
      { key: "", name: "", url: "", api_key: { has_value: false }, models: [] },
    ]);
    setExpanded((prev) => new Set(prev).add(providers.length));
  };

  const removeProvider = (pi: number): void => {
    const p = providers[pi];
    const isDef = p?.name === defaultProvider && p.name !== "";
    const next = providers.filter((_, i) => i !== pi);
    setExpanded((prev) => {
      const s = new Set(prev);
      s.delete(pi);
      return s;
    });
    if (isDef) update({ providers: next, default_provider: "", default_model: "" });
    else updateProviders(next);
    setConfirm(null);
  };

  const addModel = (pi: number): void => {
    updateProviders(
      providers.map((q, i) =>
        i === pi
          ? {
              ...q,
              models: [
                ...q.models,
                {
                  name: "",
                  model: "",
                  capabilities: ["text"],
                  temperature: 0.7,
                  max_output_tokens: 50000,
                },
              ],
            }
          : q,
      ),
    );
  };

  const removeModel = (pi: number, mi: number): void => {
    const p = providers[pi];
    const m = p?.models[mi];
    const isDef =
      p?.name === defaultProvider && m?.name === defaultModel && m?.name !== "";
    const next = providers.map((q, i) =>
      i === pi ? { ...q, models: q.models.filter((_, j) => j !== mi) } : q,
    );
    if (isDef) update({ providers: next, default_provider: "", default_model: "" });
    else updateProviders(next);
    setConfirm(null);
  };

  return (
    <div className="space-y-6">
      {/* Top: default model + max context tokens */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-[1fr_220px]">
        <div>
          <label className={LABEL}>
            Default model<span className="text-error"> *</span>
          </label>
          <select
            className={INPUT}
            value={comboExists ? String(currentComboIdx) : ""}
            onChange={(e) => {
              const idx = Number(e.target.value);
              const c = combos[idx];
              if (!c) return;
              update({ default_provider: c.pName, default_model: c.mName });
            }}
          >
            {!comboExists && (
              <option value="" disabled>
                Select a model
              </option>
            )}
            {combos.map((c, i) => (
              <option key={i} value={String(i)}>
                {`${c.pName} / ${c.mName}`}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className={LABEL}>Max context tokens</label>
          <input
            type="number"
            className={INPUT}
            value={maxContext}
            onChange={(e) => update({ max_context_tokens: Number(e.target.value) })}
          />
        </div>
      </div>

      {/* Providers */}
      <div>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-wide text-text-disabled">
            Providers
          </h2>
        </div>

        <div className="space-y-2">
          {providers.map((p, pi) => {
            const isDefault = p.name !== "" && p.name === defaultProvider;
            const isOpen = expanded.has(pi);
            const keySet = Boolean(
              (p.api_key as SecretMaskValue | undefined)?.has_value,
            );
            const confirmingThis =
              confirm?.kind === "provider" && confirm.pi === pi;

            return (
              <div
                key={pi}
                className={`rounded-lg border bg-content-bg ${
                  isDefault
                    ? "border-card-border border-l-2 border-l-ai-brand"
                    : "border-card-border"
                }`}
              >
                {/* Header row */}
                <div className="flex items-center gap-2 px-3 py-2.5">
                  <button
                    type="button"
                    onClick={() => toggle(pi)}
                    className="flex min-w-0 flex-1 items-center gap-2.5 rounded px-1 py-0.5 text-left hover:bg-sidebar-hover"
                  >
                    <Chevron open={isOpen} />
                    <StatusDot on={keySet} />
                    <span className="truncate text-sm font-medium text-text-primary">
                      {p.name || (
                        <span className="italic text-text-secondary">Untitled provider</span>
                      )}
                    </span>
                    <span className="truncate text-xs text-text-secondary">
                      {p.key || "no key"} · {p.models.length} model
                      {p.models.length === 1 ? "" : "s"}
                    </span>
                  </button>
                  <div className="flex shrink-0 items-center gap-2">
                    {isDefault && <DefaultBadge />}
                    {confirmingThis ? (
                      <span className="flex items-center gap-2 text-xs">
                        <span className="text-text-secondary">
                          Delete provider and {p.models.length} model
                          {p.models.length === 1 ? "" : "s"}?
                        </span>
                        <button
                          className="font-medium text-error hover:underline"
                          onClick={() => removeProvider(pi)}
                        >
                          Delete
                        </button>
                        <button
                          className="text-text-secondary hover:underline"
                          onClick={() => setConfirm(null)}
                        >
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        aria-label="Remove provider"
                        className="text-text-secondary hover:text-error"
                        onClick={() => setConfirm({ kind: "provider", pi })}
                      >
                        <TrashIcon />
                      </button>
                    )}
                  </div>
                </div>

                {/* Body */}
                {isOpen && (
                  <div className="space-y-5 border-t border-divider px-4 py-4">
                    {/* Provider fields */}
                    <div>
                      <SectionLabel>Provider</SectionLabel>
                      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <Field label="Key" required>
                          <input
                            className={INPUT}
                            value={p.key}
                            onChange={(e) =>
                              updateProviders(
                                providers.map((q, i) =>
                                  i === pi ? { ...q, key: e.target.value } : q,
                                ),
                              )
                            }
                          />
                        </Field>
                        <Field label="Name" required>
                          <input
                            className={INPUT}
                            value={p.name}
                            onChange={(e) =>
                              updateProviders(
                                providers.map((q, i) =>
                                  i === pi ? { ...q, name: e.target.value } : q,
                                ),
                              )
                            }
                          />
                        </Field>
                        <Field label="URL" required className="sm:col-span-2">
                          <input
                            className={INPUT}
                            value={p.url}
                            onChange={(e) =>
                              updateProviders(
                                providers.map((q, i) =>
                                  i === pi ? { ...q, url: e.target.value } : q,
                                ),
                              )
                            }
                          />
                        </Field>
                        <Field label="API key" required className="sm:col-span-2">
                          <SecretField
                            value={(p.api_key as SecretMaskValue) ?? { has_value: false }}
                            onChange={(next: SecretWrite | undefined) =>
                              updateProviders(
                                providers.map((q, i) =>
                                  i === pi ? { ...q, api_key: next ?? q.api_key } : q,
                                ),
                              )
                            }
                          />
                        </Field>
                      </div>
                    </div>

                    {/* Models */}
                    <div>
                      <SectionLabel>Models</SectionLabel>
                      <div className="space-y-2.5">
                        {(p.models ?? []).map((m, mi) => {
                          const isModelDefault =
                            p.name === defaultProvider &&
                            m.name === defaultModel &&
                            m.name !== "";
                          const confirmingThisModel =
                            confirm?.kind === "model" &&
                            confirm.pi === pi &&
                            confirm.mi === mi;

                          return (
                            <div
                              key={mi}
                              className="rounded-md border border-divider bg-sidebar-bg p-3"
                            >
                              {/* Title / actions row */}
                              <div className="mb-3 flex items-center gap-2">
                                <span className="truncate font-mono text-xs text-text-secondary">
                                  {m.model || "new model"}
                                </span>
                                <div className="ml-auto flex items-center gap-2">
                                  {isModelDefault ? (
                                    <DefaultBadge />
                                  ) : (
                                    m.name !== "" && (
                                      <button
                                        type="button"
                                        className="text-xs text-text-secondary hover:text-ai-brand"
                                        onClick={() =>
                                          update({
                                            default_provider: p.name,
                                            default_model: m.name,
                                          })
                                        }
                                      >
                                        Set as default
                                      </button>
                                    )
                                  )}
                                  {confirmingThisModel ? (
                                    <span className="flex items-center gap-2 text-xs">
                                      <button
                                        className="font-medium text-error hover:underline"
                                        onClick={() => removeModel(pi, mi)}
                                      >
                                        Delete
                                      </button>
                                      <button
                                        className="text-text-secondary hover:underline"
                                        onClick={() => setConfirm(null)}
                                      >
                                        Cancel
                                      </button>
                                    </span>
                                  ) : (
                                    <button
                                      type="button"
                                      aria-label="Remove model"
                                      className="text-text-secondary hover:text-error"
                                      onClick={() =>
                                        setConfirm({ kind: "model", pi, mi })
                                      }
                                    >
                                      <TrashIcon />
                                    </button>
                                  )}
                                </div>
                              </div>

                              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                                <Field label="Model name" required>
                                  <input
                                    className={INPUT}
                                    value={m.name}
                                    onChange={(e) =>
                                      updateModel(pi, mi, { name: e.target.value })
                                    }
                                  />
                                </Field>
                                <Field label="Model routing" required>
                                  <input
                                    className={INPUT}
                                    value={m.model}
                                    onChange={(e) =>
                                      updateModel(pi, mi, { model: e.target.value })
                                    }
                                  />
                                </Field>
                              </div>

                              {/* Capabilities (enum multi-select) */}
                              <div className="mt-3">
                                <label className={LABEL}>Capabilities</label>
                                <CapabilityChips
                                  value={m.capabilities}
                                  onChange={(caps) =>
                                    updateModel(pi, mi, { capabilities: caps })
                                  }
                                />
                              </div>

                              {/* Optional numeric fields (no *) */}
                              <div className="mt-3 grid grid-cols-2 gap-3">
                                <Field label="Temperature">
                                  <input
                                    type="number"
                                    step="0.1"
                                    className={INPUT}
                                    value={m.temperature}
                                    onChange={(e) =>
                                      updateModel(pi, mi, {
                                        temperature: Number(e.target.value),
                                      })
                                    }
                                  />
                                </Field>
                                <Field label="Max output tokens">
                                  <input
                                    type="number"
                                    className={INPUT}
                                    value={m.max_output_tokens}
                                    onChange={(e) =>
                                      updateModel(pi, mi, {
                                        max_output_tokens: Number(e.target.value),
                                      })
                                    }
                                  />
                                </Field>
                              </div>
                            </div>
                          );
                        })}

                        {p.models.length === 0 && (
                          <p className="rounded-md border border-dashed border-input-border px-3 py-2 text-xs text-text-secondary">
                            No models in this provider yet.
                          </p>
                        )}

                        <button
                          type="button"
                          className="flex w-full items-center justify-center gap-1.5 rounded-md border border-dashed border-input-border py-1.5 text-xs text-text-secondary hover:border-text-secondary hover:bg-sidebar-hover hover:text-text-primary"
                          onClick={() => addModel(pi)}
                        >
                          <PlusIcon /> Add model
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            );
          })}

          {providers.length === 0 && (
            <p className="rounded-md border border-dashed border-input-border px-3 py-6 text-center text-sm text-text-secondary">
              No providers yet.
            </p>
          )}

          <button
            type="button"
            className="mt-1 flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-input-border py-2.5 text-sm text-text-secondary hover:border-text-secondary hover:bg-sidebar-hover hover:text-text-primary"
            onClick={addProvider}
          >
            <PlusIcon /> Add provider
          </button>
        </div>
      </div>
    </div>
  );
}

/* --- small presentational helpers (locality: only this editor uses them) --- */

function Field({
  label,
  required,
  className,
  children,
}: {
  label: string;
  required?: boolean;
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
    </div>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-text-disabled">
      {children}
    </div>
  );
}

function DefaultBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-ai-brand px-2 py-0.5 text-[11px] font-medium text-ai-brand">
      ★ Default
    </span>
  );
}

function StatusDot({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={
        on
          ? "h-2 w-2 shrink-0 rounded-full bg-success"
          : "h-2 w-2 shrink-0 rounded-full border border-text-disabled"
      }
    />
  );
}

function CapabilityChips({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {CAPABILITIES.map((c) => {
        const selected = value.includes(c.value);
        return (
          <button
            key={c.value}
            type="button"
            aria-pressed={selected}
            onClick={() =>
              onChange(
                selected
                  ? value.filter((v) => v !== c.value)
                  : [...value, c.value],
              )
            }
            className={
              selected
                ? "inline-flex items-center gap-1.5 rounded-full border border-ai-brand bg-user-bubble px-2.5 py-1 text-xs font-medium text-user-bubble-text"
                : "inline-flex items-center gap-1.5 rounded-full border border-input-border bg-input-bg px-2.5 py-1 text-xs text-text-secondary hover:border-text-secondary hover:text-text-primary"
            }
          >
            <span aria-hidden="true">{c.glyph}</span>
            <span>{c.label}</span>
          </button>
        );
      })}
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 text-text-secondary transition-transform ${
        open ? "rotate-90" : ""
      }`}
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M6 4l4 4-4 4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M8 3v10M3 8h10"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3 4h10M6.5 4V2.5h3V4M5 4l.5 8.5a1 1 0 0 0 1 .9h3a1 1 0 0 0 1-.9L11 4M6.5 7v4M9.5 7v4"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
