// ModelEditor.tsx — Vercel Geist redesign of the persisted-config Models
// view. Replaces local SVG glyph helpers and the legacy `bg-user-bubble` /
// `text-user-bubble-text` chip palette with the shared `ui/Card`,
// `ui/Select`, `ui/Input`, and `ui/icons` primitives plus Geist surface
// tokens. Save is owned by SettingsView; this component only mutates
// `values` via `onChange`.
//
// Behavior is unchanged: the onChange contract remains
// `(next: Record<string, unknown>) => void`. The set of providers, models,
// capability enums, default-model selection (by combo index), and confirm
// patterns are preserved verbatim.

import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { SecretMaskValue, SecretWrite } from "../../types/config";
import { SecretField } from "./SecretField";
import { FetchModelsModal } from "./FetchModelsModal";
import type { FetchedModel } from "../../lib/api";
import { Card } from "../ui/Card";
import { Select } from "../ui/Select";
import { Input } from "../ui/Input";
import { IconButton } from "../ui/IconButton";
import { Button } from "../ui/Button";
import { Label } from "../ui/Label";
import { SectionLabel } from "../ui/SectionLabel";
import {
  ChevronRightIcon,
  PlusIcon,
  DefaultStarIcon,
  TextIcon,
  ImageIcon,
  VideoIcon,
  AudioIcon,
} from "../ui/icons";
import { Trash2, Download } from "lucide-react";
import type { SelectOption } from "../ui/Select";
import { CATEGORY } from "./categoryMeta";

interface ModelEntry {
  name: string;
  model: string;
  capabilities: string[];
  temperature: number;
  max_output_tokens: number;
  reasoning_effort: string;
}

interface Provider {
  key: string;
  name: string;
  base_url: string;
  interface_format: string;
  api_key: SecretMaskValue | SecretWrite;
  models_url?: string | null;
  models: ModelEntry[];
}

interface Props {
  values: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  /** Whether the form has unsaved changes (controls fetch-button availability). */
  dirty: boolean;
  /** Save the form, then resolve with true on success / false on failure. */
  onSave: () => Promise<boolean>;
}

// Closed enum (backend Modality). Multi-select via chips — never free text,
// since the backend rejects unknown capability strings.
type CapabilityValue = "text" | "image" | "video" | "audio";

interface CapabilityDef {
  value: CapabilityValue;
  label: string;
  Icon: (props: { className?: string }) => ReactNode;
}

const CAPABILITIES: readonly CapabilityDef[] = [
  { value: "text", label: "Text", Icon: (p) => <TextIcon {...p} /> },
  { value: "image", label: "Image", Icon: (p) => <ImageIcon {...p} /> },
  { value: "video", label: "Video", Icon: (p) => <VideoIcon {...p} /> },
  { value: "audio", label: "Audio", Icon: (p) => <AudioIcon {...p} /> },
];

const REASONING_EFFORT_OPTIONS: SelectOption[] = [
  { value: "none", label: "none" },
  { value: "minimal", label: "minimal" },
  { value: "low", label: "low" },
  { value: "medium", label: "medium" },
  { value: "high", label: "high" },
  { value: "xhigh", label: "xhigh" },
];

const INTERFACE_FORMAT_OPTIONS: SelectOption[] = [
  { value: "openai_compatible", label: "OpenAI Compatible" },
  { value: "anthropic", label: "Anthropic" },
];

type Confirm =
  | { kind: "provider"; pi: number }
  | { kind: "model"; pi: number; mi: number }
  | null;

/** Find the first (provider.name, model.name) combo across providers. */
function pickFirstCombo(
  providers: Provider[],
): { pName: string; mName: string } | null {
  for (const p of providers) {
    for (const m of p.models) {
      if (p.name && m.name) return { pName: p.name, mName: m.name };
    }
  }
  return null;
}

export function ModelEditor({ values, onChange, dirty, onSave }: Props) {
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
  // Index of the provider card that the user just added (for auto-scroll).
  const [justAddedIdx, setJustAddedIdx] = useState<number | null>(null);
  // Fetch-modal state: which provider index is fetching, and its cached list.
  const [fetchTarget, setFetchTarget] = useState<number | null>(null);
  const [fetchedCache, setFetchedCache] = useState<
    Record<number, FetchedModel[]>
  >({});
  const [inlineOpen, setInlineOpen] = useState<number | null>(null);

  const update = (patch: Record<string, unknown>): void => {
    onChange({ ...values, ...patch });
  };
  const updateProviders = (next: Provider[]): void => update({ providers: next });

  const updateModel = (pi: number, mi: number, patch: Partial<ModelEntry>): void => {
    const nextProviders = providers.map((q, i) =>
      i === pi
        ? { ...q, models: q.models.map((mm, j) => (j === mi ? { ...mm, ...patch } : mm)) }
        : q,
    );
    const p = providers[pi];
    const m = p?.models[mi];
    const wasModelDefault =
      patch.name !== undefined &&
      p?.name === defaultProvider &&
      m?.name === defaultModel &&
      m!.name !== "";
    if (wasModelDefault) {
      update({ providers: nextProviders, default_model: patch.name as string });
    } else {
      update({ providers: nextProviders });
    }
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

  // Build the Select options for the default-model combo picker. We keep an
  // internal index-based value (so the backend contract never sees spaces or
  // special chars in dropdown keys), but render it through the new Select
  // primitive — never a raw <select>.
  const defaultSelectOptions: SelectOption[] = combos.map((c, i) => ({
    value: String(i),
    label: `${c.pName} / ${c.mName}`,
  }));
  // When no valid combo exists, lead with a disabled placeholder option.
  const placeholderValue = "__placeholder__";
  if (!comboExists) {
    defaultSelectOptions.unshift({ value: placeholderValue, label: "Select a model" });
  }
  const defaultSelectValue = comboExists ? String(currentComboIdx) : placeholderValue;

  const addProvider = (): void => {
    updateProviders([
      ...providers,
      {
        key: "",
        name: "",
        base_url: "",
        interface_format: "openai_compatible",
        api_key: { has_value: false },
        models: [],
      },
    ]);
    const newIdx = providers.length;
    setExpanded((prev) => new Set(prev).add(newIdx));
    setJustAddedIdx(newIdx);
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
    if (isDef) {
      const firstCombo = pickFirstCombo(next);
      update({
        providers: next,
        default_provider: firstCombo?.pName ?? "",
        default_model: firstCombo?.mName ?? "",
      });
    } else updateProviders(next);
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
                  reasoning_effort: "none",
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
    if (isDef) {
      const firstCombo = pickFirstCombo(next);
      update({
        providers: next,
        default_provider: firstCombo?.pName ?? "",
        default_model: firstCombo?.mName ?? "",
      });
    } else updateProviders(next);
    setConfirm(null);
  };

  // After a new provider is added, scroll its card into view in the next
  // frame. Setting `key={provider-${index}}` keeps the DOM id stable across
  // renders so we can target the freshly-added card without using refs.
  useEffect(() => {
    if (justAddedIdx === null) return;
    const id = `provider-${justAddedIdx}`;
    // Defer to the next frame so React has committed the new card.
    const handle = window.requestAnimationFrame(() => {
      const el = document.getElementById(id);
      if (el && typeof el.scrollIntoView === "function") {
        el.scrollIntoView({ block: "center" });
      }
      setJustAddedIdx(null);
    });
    return () => window.cancelAnimationFrame(handle);
  }, [justAddedIdx]);

  // Stable change handlers — these don't capture `values`/`providers` from
  // the closure (they always read via the latest render) so memoizing them
  // is unnecessary. The native browser input/select wiring is already cheap.
  const handleKeyChange = useCallback(
    (pi: number, v: string) =>
      updateProviders(providers.map((q, i) => (i === pi ? { ...q, key: v } : q))),
    [providers],
  );
  const handleNameChange = useCallback(
    (pi: number, v: string) => {
      const nextProviders = providers.map((q, i) =>
        i === pi ? { ...q, name: v } : q,
      );
      const wasDefault =
        providers[pi]?.name === defaultProvider && providers[pi]!.name !== "";
      if (wasDefault) {
        update({ providers: nextProviders, default_provider: v });
      } else {
        update({ providers: nextProviders });
      }
    },
    [providers, defaultProvider],
  );
  const handleBaseUrlChange = useCallback(
    (pi: number, v: string) =>
      updateProviders(providers.map((q, i) => (i === pi ? { ...q, base_url: v } : q))),
    [providers],
  );
  const handleInterfaceFormatChange = useCallback(
    (pi: number, v: string) =>
      updateProviders(
        providers.map((q, i) => (i === pi ? { ...q, interface_format: v } : q)),
      ),
    [providers],
  );
  const handleApiKeyChange = useCallback(
    (pi: number, next: SecretWrite | undefined) =>
      updateProviders(
        providers.map((q, i) =>
          i === pi ? { ...q, api_key: next ?? q.api_key } : q,
        ),
      ),
    [providers],
  );
  const handleModelsUrlChange = useCallback(
    (pi: number, v: string) =>
      updateProviders(providers.map((q, i) => (i === pi ? { ...q, models_url: v || null } : q))),
    [providers],
  );

  const handleFetchClick = useCallback(
    async (pi: number): Promise<void> => {
      const p = providers[pi];
      if (!p || !p.key) return;
      // If the form is dirty, save first so the backend has the real api_key.
      if (dirty) {
        const ok = await onSave();
        if (!ok) return;
      }
      setFetchTarget(pi);
    },
    [providers, dirty, onSave],
  );

  const handleFetchImport = useCallback(
    (pi: number, models: FetchedModel[]): void => {
      const p = providers[pi];
      if (!p) return;
      const existingIds = new Set(p.models.map((m) => m.model));
      const newModels = models
        .filter((fm) => !existingIds.has(fm.id))
        .map((fm) => ({
          name: fm.id,
          model: fm.id,
          capabilities: ["text"],
          temperature: 0.7,
          max_output_tokens: 50000,
          reasoning_effort: "none",
        }));
      if (newModels.length === 0) return;
      updateProviders(
        providers.map((q, i) =>
          i === pi ? { ...q, models: [...q.models, ...newModels] } : q,
        ),
      );
      setFetchedCache((prev) => ({ ...prev, [pi]: models }));
    },
    [providers],
  );

  const handleInlinePick = useCallback(
    (pi: number, mi: number, modelId: string): void => {
      updateModel(pi, mi, { model: modelId });
      setInlineOpen(null);
    },
    [providers],
  );

  const meta = CATEGORY.model;
  const PageHeadIcon = meta.icon;

  return (
    <div className="space-y-6">
      <div className="page-head">
        <span
          className="page-head-icon"
          style={{ ["--cat" as string]: meta.catVar }}
        >
          <PageHeadIcon size={18} />
        </span>
        <div>
          <div className="page-title">{meta.title}</div>
          <div className="page-sub">{meta.sub}</div>
        </div>
      </div>

      {/* Top: default model + max context tokens */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-[1fr_220px]">
        <Select
          label="Default model"
          required
          options={defaultSelectOptions}
          value={defaultSelectValue}
          disabled={!comboExists && defaultSelectOptions.length === 1}
          onChange={(e) => {
            const v = e.target.value;
            if (v === placeholderValue) return;
            const idx = Number(v);
            const c = combos[idx];
            if (!c) return;
            update({ default_provider: c.pName, default_model: c.mName });
          }}
        />
        <Input
          label="Max context tokens"
          type="number"
          value={Number.isFinite(maxContext) ? maxContext : 0}
          onChange={(e) => update({ max_context_tokens: Number(e.target.value) })}
        />
      </div>

      {/* Providers */}
      <div>
        <div className="mb-2 flex items-center justify-between">
          <SectionLabel>Providers</SectionLabel>
        </div>

        <div className="space-y-2">
          {providers.map((p, pi) => {
            const isDefault = p.name !== "" && p.name === defaultProvider;
            const isOpen = expanded.has(pi);
            const keySet = Boolean(
              (p.api_key as SecretMaskValue)?.has_value,
            );
            const confirmingThis =
              confirm?.kind === "provider" && confirm.pi === pi;

            return (
              <div key={pi} id={`provider-${pi}`}>
                <Card
                  className={`p-5 ${
                    isDefault ? "border-l-2 border-l-link" : ""
                  }`.trim()}
                >
                {/* Header row */}
                <div className="flex items-center gap-2 px-3 py-2.5">
                  <button
                    type="button"
                    onClick={() => toggle(pi)}
                    className="flex min-w-0 flex-1 items-center gap-2.5 rounded px-1 py-0.5 text-left hover:bg-hairline-soft"
                  >
                    <ChevronRightIcon
                      className={`transition-transform ${isOpen ? "rotate-90" : ""}`}
                    />
                    <StatusDot on={keySet} />
                    <span className="truncate text-sm font-medium text-ink">
                      {p.name || (
                        <span className="italic text-body">Untitled provider</span>
                      )}
                    </span>
                    <span className="truncate text-xs text-body">
                      key: {p.key || "none"} · {p.models.length} model
                      {p.models.length === 1 ? "" : "s"}
                    </span>
                  </button>
                  <div className="flex shrink-0 items-center gap-2">
                    {isDefault && <DefaultBadge />}
                    {confirmingThis ? (
                      <span className="flex items-center gap-2 text-xs">
                        <span className="text-body">
                          Delete provider and {p.models.length} model
                          {p.models.length === 1 ? "" : "s"}?
                        </span>
                        <Button
                          variant="link"
                          size="sm"
                          className="font-medium text-error hover:underline"
                          onClick={() => removeProvider(pi)}
                        >
                          Delete
                        </Button>
                        <Button
                          variant="link"
                          size="sm"
                          className="text-body hover:underline"
                          onClick={() => setConfirm(null)}
                        >
                          Cancel
                        </Button>
                      </span>
                    ) : (
                      <IconButton
                        icon={<Trash2 size={16} />}
                        label="Remove provider"
                        variant="ghost"
                        size="sm"
                        onClick={() => setConfirm({ kind: "provider", pi })}
                      />
                    )}
                  </div>
                </div>

                {/* Body */}
                {isOpen && (
                  <div className="space-y-5 border-t border-hairline px-4 py-4">
                    {/* Provider fields */}
                    <div>
                      <SectionLabel>Provider</SectionLabel>
                      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <Input
                          label="Provider key"
                          required
                          value={p.key}
                          onChange={(e) => handleKeyChange(pi, e.target.value)}
                        />
                        <Input
                          label="Display name"
                          required
                          value={p.name}
                          onChange={(e) => handleNameChange(pi, e.target.value)}
                        />
                        <div className="sm:col-span-2">
                          <Input
                            label="Base URL"
                            required
                            value={p.base_url}
                            onChange={(e) => handleBaseUrlChange(pi, e.target.value)}
                          />
                        </div>
                        <div className="sm:col-span-2">
                          <Select
                            label="Interface format"
                            options={INTERFACE_FORMAT_OPTIONS}
                            value={p.interface_format ?? "openai_compatible"}
                            onChange={(e) =>
                              handleInterfaceFormatChange(pi, e.target.value)
                            }
                          />
                        </div>
                        <div className="sm:col-span-2">
                          <Label required>API key</Label>
                          <SecretField
                            value={(p.api_key as SecretMaskValue) ?? { has_value: false }}
                            onChange={(next) => handleApiKeyChange(pi, next)}
                          />
                        </div>
                        <div className="sm:col-span-2">
                          <Input
                            label="Models URL (optional)"
                            value={p.models_url ?? ""}
                            placeholder="留空自动探测 /v1/models"
                            onChange={(e) => handleModelsUrlChange(pi, e.target.value)}
                          />
                        </div>
                      </div>
                    </div>

                    {/* Models */}
                    <div>
                      <div className="mb-2 flex items-center justify-between">
                        <SectionLabel>Models</SectionLabel>
                        {p.key && (
                          <Button
                            type="button"
                            variant="secondary"
                            size="sm"
                            className="gap-1.5"
                            onClick={() => handleFetchClick(pi)}
                            disabled={!p.key}
                          >
                            <Download size={14} />
                            {dirty ? "保存后拉取" : "拉取模型"}
                          </Button>
                        )}
                      </div>
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
                              className="rounded-md border border-hairline bg-canvas-elevated p-5"
                            >
                              {/* Title / actions row */}
                              <div className="mb-3 flex items-center gap-2">
                                <span className="truncate font-mono text-xs text-body">
                                  {m.model || "new model"}
                                </span>
                                <div className="ml-auto flex items-center gap-2">
                                  {isModelDefault ? (
                                    <DefaultBadge />
                                  ) : (
                                    m.name !== "" && (
                                      <Button
                                        variant="link"
                                        size="sm"
                                        className="text-body hover:text-link"
                                        onClick={() =>
                                          update({
                                            default_provider: p.name,
                                            default_model: m.name,
                                          })
                                        }
                                      >
                                        Set as default
                                      </Button>
                                    )
                                  )}
                                  {confirmingThisModel ? (
                                    <span className="flex items-center gap-2 text-xs">
                                      <Button
                                        variant="link"
                                        size="sm"
                                        className="font-medium text-error hover:underline"
                                        onClick={() => removeModel(pi, mi)}
                                      >
                                        Delete
                                      </Button>
                                      <Button
                                        variant="link"
                                        size="sm"
                                        className="text-body hover:underline"
                                        onClick={() => setConfirm(null)}
                                      >
                                        Cancel
                                      </Button>
                                    </span>
                                  ) : (
                                    <IconButton
                                      icon={<Trash2 size={16} />}
                                      label="Remove model"
                                      variant="ghost"
                                      size="sm"
                                      onClick={() =>
                                        setConfirm({ kind: "model", pi, mi })
                                      }
                                    />
                                  )}
                                </div>
                              </div>

                              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                                <Input
                                  label="Model key"
                                  required
                                  value={m.name}
                                  onChange={(e) =>
                                    updateModel(pi, mi, { name: e.target.value })
                                  }
                                />
                                <div className="relative">
                                  <Input
                                    label="Model identifier"
                                    required
                                    value={m.model}
                                    onChange={(e) =>
                                      updateModel(pi, mi, { model: e.target.value })
                                    }
                                  />
                                  {fetchedCache[pi] && fetchedCache[pi]!.length > 0 && (
                                    <button
                                      type="button"
                                      onClick={() =>
                                        setInlineOpen(inlineOpen === pi ? null : pi)
                                      }
                                      className="absolute right-2 top-7 flex h-5 w-5 items-center justify-center rounded text-faint hover:bg-hairline-soft hover:text-ink"
                                      aria-label="从已拉取列表选择"
                                    >
                                      <ChevronRightIcon
                                        className={`h-3 w-3 transition-transform ${inlineOpen === pi ? "rotate-90" : ""}`}
                                      />
                                    </button>
                                  )}
                                  {inlineOpen === pi && fetchedCache[pi] && (
                                    <div className="absolute z-10 mt-1 max-h-48 w-full overflow-y-auto rounded-md border border-hairline bg-canvas-elevated py-1 shadow-lg">
                                      {fetchedCache[pi]!.map((fm) => (
                                        <button
                                          key={fm.id}
                                          type="button"
                                          onClick={() => handleInlinePick(pi, mi, fm.id)}
                                          className="block w-full truncate px-3 py-1 text-left font-mono text-xs text-ink hover:bg-hairline-soft"
                                        >
                                          {fm.id}
                                        </button>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              </div>

                              {/* Capabilities (enum multi-select) */}
                              <div className="mt-3">
                                <Label>Capabilities</Label>
                                <CapabilityChips
                                  value={m.capabilities}
                                  onChange={(caps) =>
                                    updateModel(pi, mi, { capabilities: caps })
                                  }
                                />
                              </div>

                              {/* Optional numeric fields (no *) */}
                              <div className="mt-3 grid grid-cols-2 gap-3">
                                <Input
                                  label="Temperature"
                                  type="number"
                                  step="0.1"
                                  value={m.temperature}
                                  onChange={(e) =>
                                    updateModel(pi, mi, {
                                      temperature: Number(e.target.value),
                                    })
                                  }
                                />
                                <Input
                                  label="Max output tokens"
                                  type="number"
                                  value={m.max_output_tokens}
                                  onChange={(e) =>
                                    updateModel(pi, mi, {
                                      max_output_tokens: Number(e.target.value),
                                    })
                                  }
                                />
                              </div>

                              {/* Reasoning effort (closed enum) */}
                              <div className="mt-3">
                                <Select
                                  label="Reasoning effort"
                                  options={REASONING_EFFORT_OPTIONS}
                                  value={m.reasoning_effort ?? "none"}
                                  onChange={(e) =>
                                    updateModel(pi, mi, {
                                      reasoning_effort: e.target.value,
                                    })
                                  }
                                />
                              </div>
                            </div>
                          );
                        })}

                        {p.models.length === 0 && (
                          <p className="rounded-md border border-dashed border-hairline px-3 py-2 text-xs text-body">
                            No models in this provider yet.
                          </p>
                        )}

                        <button
                          type="button"
                          className="flex w-full items-center justify-center gap-1.5 rounded-md border border-dashed border-hairline py-1.5 text-xs text-body hover:border-ink hover:bg-hairline-soft hover:text-ink"
                          onClick={() => addModel(pi)}
                        >
                          <PlusIcon /> Add model
                        </button>
                      </div>
                    </div>
                  </div>
                )}
                </Card>
              </div>
            );
          })}

          {providers.length === 0 && (
            <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-sm text-body">
              No providers yet.
            </p>
          )}

          <button
            type="button"
            className="mt-1 flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-hairline py-2.5 text-sm text-body hover:border-ink hover:bg-hairline-soft hover:text-ink"
            onClick={addProvider}
          >
            <PlusIcon /> Add provider
          </button>
        </div>
      </div>

      {fetchTarget !== null && providers[fetchTarget] && (
        <FetchModelsModal
          open
          onClose={() => setFetchTarget(null)}
          providerKey={providers[fetchTarget]!.key}
          existingModelIds={
            new Set(providers[fetchTarget]!.models.map((m) => m.model))
          }
          onImport={(models) => handleFetchImport(fetchTarget, models)}
        />
      )}
    </div>
  );
}

/* --- small presentational helpers (locality: only this editor uses them) --- */

function DefaultBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-link px-2 py-0.5 text-[11px] font-medium text-link">
      <DefaultStarIcon className="h-3 w-3" /> Default
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
          : "h-2 w-2 shrink-0 rounded-full border border-faint"
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
        const Icon = c.Icon;
        return (
          <button
            key={c.value}
            type="button"
            aria-pressed={selected}
            aria-label={c.label}
            onClick={() =>
              onChange(
                selected
                  ? value.filter((v) => v !== c.value)
                  : [...value, c.value],
              )
            }
            className={
              selected
                ? "inline-flex items-center gap-1.5 rounded-full border border-link bg-canvas-elevated px-2.5 py-1 text-xs font-medium text-link"
                : "inline-flex items-center gap-1.5 rounded-full border border-hairline bg-canvas-elevated px-2.5 py-1 text-xs text-body hover:border-ink hover:text-ink"
            }
          >
            <Icon />
            <span>{c.label}</span>
          </button>
        );
      })}
    </div>
  );
}