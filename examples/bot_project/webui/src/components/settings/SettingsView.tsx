import { useEffect, useState } from "react";
import type { ConfigPayload, RegistrySection } from "../../types/config";
import { fetchConfig, saveConfig } from "../../lib/api";
import { ConfigForm } from "./ConfigForm";
import { ModelEditor } from "./ModelEditor";
import { GlobalMcpView } from "./GlobalMcpView";
import { GlobalSkillsView } from "./GlobalSkillsView";
import { PoolsView } from "./PoolsView";
import { ConfirmDialog } from "./ConfirmDialog";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";

interface Props {
  onExit: () => void;
}

// Sidebar groups. The Configuration group holds the persisted-config domains
// (IM/Models) that share the dirty/save footer. The "Pools & Agents" group
// holds the new standalone views (each owns its own persistence + toasts).
type ViewKey = "im" | "model" | "pools" | "mcp" | "skills";

interface NavEntry {
  key: ViewKey;
  label: string;
}

const CONFIG_GROUP: NavEntry[] = [
  { key: "im", label: "IM Adapters" },
  { key: "model", label: "Models" },
];

const POOLS_GROUP: NavEntry[] = [
  { key: "pools", label: "Pools" },
  { key: "mcp", label: "Global MCP" },
  { key: "skills", label: "Global Skills" },
];

/** Domains backed by the /api/config persisted-config API (shared save footer). */
const PERSISTED_DOMAINS = new Set<ViewKey>(["im", "model"]);

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

export function SettingsView({ onExit }: Props) {
  const toast = useToast();
  const [view, setView] = useState<ViewKey>("im");
  const [original, setOriginal] = useState<ConfigPayload | null>(null);
  const [form, setForm] = useState<ConfigPayload | null>(null);
  const [error, setError] = useState<string>("");
  /** Open state for the discard-unsaved confirm when switching persisted views. */
  const [discardView, setDiscardView] = useState<ViewKey | null>(null);

  const isPersisted = PERSISTED_DOMAINS.has(view);

  const load = async (d: ViewKey): Promise<void> => {
    setError("");
    const payload = await fetchConfig(d);
    setOriginal(payload);
    setForm(clone(payload));
    if (payload.restart_required) toast.restart.setRestartNeeded(true);
  };

  useEffect(() => {
    if (!isPersisted) return;
    void load(view).catch((e: unknown) => setError(String(e)));
  }, [view, isPersisted]);

  // Loading state only applies to persisted-config views.
  if (isPersisted && (!form || !original)) {
    return (
      <div className="flex h-full items-center justify-center text-text-secondary">
        {error ? `Failed to load: ${error}` : "Loading…"}
      </div>
    );
  }

  const dirty = form && original ? JSON.stringify(form) !== JSON.stringify(original) : false;

  const switchView = (next: ViewKey): void => {
    if (next === view) return;
    // Only the persisted views carry local dirty state worth a discard prompt.
    if (isPersisted && dirty) {
      setDiscardView(next);
      return;
    }
    setView(next);
    setError("");
  };

  const assemblePayload = (): Record<string, unknown> => {
    if (!form) return {};
    if (form.flavor === "registry") {
      const out: Record<string, unknown> = {};
      for (const [key, sec] of Object.entries(form.sections ?? {})) {
        out[key] = (sec as RegistrySection).values;
      }
      return out;
    }
    return form.values ?? {};
  };

  const onSave = async (): Promise<void> => {
    if (!view || !isPersisted) return;
    setError("");
    try {
      const updated = await saveConfig(view, assemblePayload());
      setOriginal(updated);
      setForm(clone(updated));
      if (updated.restart_required) restartToast(toast);
    } catch (e) {
      setError(`Save failed: ${String(e)}`);
    }
  };

  const onCancel = (): void => {
    if (original) setForm(clone(original)); // server untouched
    setError("");
  };

  return (
    <div className="flex h-full">
      <aside className="w-52 shrink-0 border-r border-divider p-3">
        <button className="mb-4 text-sm text-ai-brand hover:underline" onClick={onExit}>
          ← Back to chat
        </button>
        <SidebarGroup
          title="Configuration"
          entries={CONFIG_GROUP}
          active={view}
          onSelect={switchView}
        />
        <div className="mt-4">
          <SidebarGroup
            title="Pools & Agents"
            entries={POOLS_GROUP}
            active={view}
            onSelect={switchView}
          />
        </div>
      </aside>

      <section className="flex-1 overflow-auto p-6">
        {view === "mcp" ? (
          <GlobalMcpView />
        ) : view === "skills" ? (
          <GlobalSkillsView />
        ) : view === "pools" ? (
          <PoolsView />
        ) : form && isPersisted ? (
          <PersistedDomain
            form={form}
            error={error}
            dirty={dirty}
            onChange={setForm}
            onSave={onSave}
            onCancel={onCancel}
          />
        ) : null}
      </section>

      {discardView !== null ? (
        <ConfirmDialog
          title="Discard unsaved changes?"
          message="Switching now will lose your edits to the current view."
          confirmLabel="Discard"
          tone="danger"
          onConfirm={() => {
            const next = discardView;
            setDiscardView(null);
            setView(next);
            setError("");
          }}
          onCancel={() => setDiscardView(null)}
        />
      ) : null}
    </div>
  );
}

function SidebarGroup({
  title,
  entries,
  active,
  onSelect,
}: {
  title: string;
  entries: NavEntry[];
  active: ViewKey;
  onSelect: (k: ViewKey) => void;
}) {
  return (
    <div>
      <h2 className="mb-1 px-2 text-[11px] font-semibold uppercase tracking-wide text-text-disabled">
        {title}
      </h2>
      <ul className="space-y-1">
        {entries.map((e) => (
          <li key={e.key}>
            <button
              className={`w-full rounded px-2 py-1 text-left text-sm hover:bg-sidebar-hover ${
                e.key === active
                  ? "bg-sidebar-hover font-semibold text-text-primary"
                  : "text-text-secondary"
              }`}
              onClick={() => onSelect(e.key)}
            >
              {e.label}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function PersistedDomain({
  form,
  error,
  dirty,
  onChange,
  onSave,
  onCancel,
}: {
  form: ConfigPayload;
  error: string;
  dirty: boolean;
  onChange: (next: ConfigPayload) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  return (
    <>
      <h1 className="mb-4 text-lg font-semibold text-text-primary">{form.label}</h1>

      {form.flavor === "registry" ? (
        <div className="space-y-6">
          {Object.entries(form.sections ?? {}).map(([key, sec]) => {
            const section = sec as RegistrySection;
            return (
              <fieldset key={key} className="rounded-lg border border-divider p-4">
                <legend className="px-1 text-sm font-semibold text-text-primary">
                  {section.label}
                </legend>
                <ConfigForm
                  fields={section.fields}
                  values={section.values}
                  onChange={(next) =>
                    onChange({
                      ...form,
                      sections: {
                        ...(form.sections ?? {}),
                        [key]: { ...section, values: next },
                      },
                    })
                  }
                />
              </fieldset>
            );
          })}
        </div>
      ) : form.domain === "model" ? (
        <ModelEditor
          values={form.values ?? {}}
          onChange={(next) => onChange({ ...form, values: next })}
        />
      ) : (
        <ConfigForm
          fields={form.fields ?? []}
          values={form.values ?? {}}
          onChange={(next) => onChange({ ...form, values: next })}
        />
      )}

      {error && <p className="mt-4 text-sm text-error">{error}</p>}

      <div className="mt-6 flex justify-end gap-2 border-t border-divider pt-4">
        <button
          className="rounded border border-divider px-4 py-1.5 text-sm text-text-primary hover:bg-sidebar-hover disabled:opacity-50"
          onClick={onCancel}
          disabled={!dirty}
        >
          Cancel
        </button>
        <button
          className="rounded bg-btn-primary px-4 py-1.5 text-sm text-btn-primary-text hover:opacity-90 disabled:opacity-50"
          onClick={onSave}
          disabled={!dirty}
        >
          Save
        </button>
      </div>
    </>
  );
}
