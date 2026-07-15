import { useEffect, useMemo, useState } from "react";
import type { ConfigPayload, RegistrySection } from "../../types/config";
import { fetchConfig, saveConfig } from "../../lib/api";
import { ConfigForm } from "./ConfigForm";
import { SectionLabel } from "../ui/SectionLabel";
import { ModelEditor } from "./ModelEditor";
import { GlobalMcpView } from "./GlobalMcpView";
import { GlobalSkillsView } from "./GlobalSkillsView";
import { PoolsView } from "./PoolsView";
import { ConfirmDialog } from "./ConfirmDialog";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { Button } from "../ui/Button";
import { ActionBar } from "../ui/ActionBar";
import { ChevronLeft } from "lucide-react";
import { CATEGORY, type ViewKey } from "./categoryMeta";
import { IM_BRAND_ICONS } from "./imBrands";

// Re-export so any existing `import { ViewKey } from "./SettingsView"` keeps
// resolving; categoryMeta.ts is now the canonical declaration.
export type { ViewKey };

interface Props {
  onExit: () => void;
}

// Sidebar groups. The Configuration group holds the persisted-config domains
// (IM/Models) that share the dirty/save footer. The "Pools & Agents" group
// holds the new standalone views (each owns its own persistence + toasts).
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
  { key: "mcp", label: "MCP" },
  { key: "skills", label: "Skills" },
];

/** Domains backed by the /api/config persisted-config API (shared save footer). */
const PERSISTED_DOMAINS = new Set<ViewKey>(["im", "model"]);

/** All valid URL ?tab= values, in the canonical order they appear in the sidebar. */
const VALID_TABS: ReadonlySet<ViewKey> = new Set([
  "im",
  "model",
  "pools",
  "mcp",
  "skills",
]);

/** Read the initial tab from window.location.search without coupling to React Router. */
function readInitialTab(): ViewKey {
  if (typeof window === "undefined") return "im";
  const params = new URLSearchParams(window.location.search);
  const tab = params.get("tab");
  return tab && VALID_TABS.has(tab as ViewKey) ? (tab as ViewKey) : "im";
}

/** Write the current tab back to the URL without adding to the history stack. */
function writeTabToUrl(tab: ViewKey): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  params.set("tab", tab);
  const next = `${window.location.pathname}?${params.toString()}${window.location.hash}`;
  window.history.replaceState(window.history.state, "", next);
}

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

/**
 * Hook owning a single `dirty` boolean. The persisted-domain views derive it
 * from form/original; non-persisted views leave it false (each child manages
 * its own internal dirty state for now — see PoolEditor's onDirtyChange).
 */
function useDirty(form: ConfigPayload | null, original: ConfigPayload | null): boolean {
  return useMemo<boolean>(
    () =>
      form && original ? JSON.stringify(form) !== JSON.stringify(original) : false,
    [form, original],
  );
}

export function SettingsView({ onExit }: Props) {
  const toast = useToast();
  const [view, setView] = useState<ViewKey>(readInitialTab);
  const [original, setOriginal] = useState<ConfigPayload | null>(null);
  const [form, setForm] = useState<ConfigPayload | null>(null);
  const [error, setError] = useState<string>("");
  /** Open state for the discard-unsaved confirm when switching persisted views. */
  const [discardView, setDiscardView] = useState<ViewKey | null>(null);
  const [saving, setSaving] = useState<boolean>(false);

  const isPersisted = PERSISTED_DOMAINS.has(view);
  const dirty = useDirty(form, original);

  // Sync tab → URL whenever the active view changes. replaceState (not push)
  // avoids polluting history on every navigation, and since the effect only
  // writes (it never reads), it can't loop with the initial URL read.
  useEffect(() => {
    writeTabToUrl(view);
  }, [view]);

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
      <div className="flex h-full items-center justify-center text-mute">
        {error ? `Failed to load: ${error}` : "Loading…"}
      </div>
    );
  }

  const switchView = (next: ViewKey): void => {
    if (next === view) return;
    // Only the persisted views carry local dirty state worth a discard prompt;
    // the pool/mcp/skills views own their own dirty tracking internally.
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

  const onSave = async (): Promise<boolean> => {
    if (!view || !isPersisted) return false;
    setSaving(true);
    setError("");
    try {
      const updated = await saveConfig(view, assemblePayload());
      setOriginal(updated);
      setForm(clone(updated));
      if (updated.restart_required) restartToast(toast);
      return true;
    } catch (e) {
      setError(`Save failed: ${String(e)}`);
      return false;
    } finally {
      setSaving(false);
    }
  };

  const onCancel = (): void => {
    if (original) setForm(clone(original)); // server untouched
    setError("");
  };

  return (
    <div data-testid="settings-shell" className="flex h-full flex-col md:flex-row">
      <aside
        aria-label="Settings navigation"
        className="w-full shrink-0 border-b border-hairline bg-canvas p-3 md:w-52 md:border-b-0 md:border-r"
      >
        <Button
          variant="ghost"
          size="sm"
          onClick={onExit}
          className="mb-5 w-full justify-start gap-2 px-3 text-sm font-medium text-ink hover:bg-hairline-soft"
        >
          <ChevronLeft className="h-4 w-4" />
          Back
        </Button>
        <div className="space-y-4">
          <SidebarGroup
            title="Configuration"
            entries={CONFIG_GROUP}
            active={view}
            onSelect={switchView}
          />
          <SidebarGroup
            title="Pools & Agents"
            entries={POOLS_GROUP}
            active={view}
            onSelect={switchView}
          />
        </div>
      </aside>

      <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-canvas">
        <div className="flex h-full flex-col">
          <div className="flex-1 overflow-auto p-6">
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
                onChange={setForm}
                dirty={dirty}
                onSave={onSave}
              />
            ) : null}
          </div>
          {isPersisted && form && (
            <ActionBar>
              <Button
                variant="secondary"
                size="sm"
                onClick={onCancel}
                disabled={!dirty || saving}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={onSave}
                disabled={!dirty || saving}
                loading={saving}
              >
                Save
              </Button>
            </ActionBar>
          )}
        </div>
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
    <div className="rounded-lg border border-hairline bg-canvas-elevated p-2">
      <h2 className="mb-1.5 px-2 pb-1.5 text-[10px] font-semibold uppercase tracking-widest text-mute border-b border-hairline">
        {title}
      </h2>
      <ul className="mt-1.5 space-y-0.5">
        {entries.map((e) => {
          const meta = CATEGORY[e.key];
          const Icon = meta.icon;
          return (
            <li key={e.key}>
              <button
                type="button"
                className={`nav-item${e.key === active ? " active" : ""}`}
                style={{ ["--cat" as string]: meta.catVar }}
                onClick={() => onSelect(e.key)}
              >
                <span className="category-chip">
                  <Icon size={14} />
                </span>
                {e.label}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function PersistedDomain({
  form,
  error,
  onChange,
  dirty,
  onSave,
}: {
  form: ConfigPayload;
  error: string;
  onChange: (next: ConfigPayload) => void;
  dirty: boolean;
  onSave: () => Promise<boolean>;
}) {
  return (
    <>
      {form.flavor === "registry" ? (
        <div className="space-y-6">
          {Object.entries(form.sections ?? {}).map(([key, sec]) => {
            const section = sec as RegistrySection;
            return (
              <div key={key} className="rounded-lg border border-hairline bg-canvas-elevated p-5">
                {(() => {
                  const brand = IM_BRAND_ICONS[key];
                  if (!brand) return <SectionLabel>{section.label}</SectionLabel>;
                  const { Icon, color } = brand;
                  return (
                    <div className="mb-4 flex items-center gap-2.5">
                      <span
                        className="inline-flex h-8 w-8 items-center justify-center rounded-md"
                        style={{ color }}
                      >
                        <Icon className="h-5 w-5" />
                      </span>
                      <span className="font-mono text-sm font-semibold text-bright">
                        {section.label}
                      </span>
                    </div>
                  );
                })()}
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
              </div>
            );
          })}
        </div>
      ) : form.domain === "model" ? (
        <ModelEditor
          values={form.values ?? {}}
          onChange={(next) => onChange({ ...form, values: next })}
          dirty={dirty}
          onSave={onSave}
        />
      ) : (
        <ConfigForm
          fields={form.fields ?? []}
          values={form.values ?? {}}
          onChange={(next) => onChange({ ...form, values: next })}
        />
      )}

      {error && <p className="mt-4 text-sm text-error">{error}</p>}
    </>
  );
}
