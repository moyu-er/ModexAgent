import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import type { ConfigPayload, RegistrySection } from "../../types/config";
import { ApiError, fetchConfig, saveConfig } from "../../lib/api";
import { ConfigForm } from "./ConfigForm";
import { SectionLabel } from "../ui/SectionLabel";
import { ModelEditor } from "./ModelEditor";
import { GlobalMcpView } from "./GlobalMcpView";
import { GlobalSkillsView } from "./GlobalSkillsView";
import { ScopeView } from "./ScopeView";
import { PoolsConfigView } from "./pools/PoolsConfigView";
import { PromptsView } from "./PromptsView";
import { ConfirmDialog } from "./ConfirmDialog";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { Button } from "../ui/Button";
import { ActionBar } from "../ui/ActionBar";
import { IconButton } from "../ui/IconButton";
import { XIcon } from "../ui/icons";
import { CATEGORY, type ViewKey } from "./categoryMeta";
import { IM_BRAND_ICONS } from "./imBrands";
import { useT, type MessageKey, type TFn } from "../../i18n";
import { TERMS } from "../../i18n/terms";
import { validateModelValues } from "./modelValidation";

// Re-export so any existing `import { ViewKey } from "./SettingsView"` keeps
// resolving; categoryMeta.ts is now the canonical declaration.
export type { ViewKey };

interface Props {
  open: boolean;
  onClose: () => void;
}

// Sidebar groups. The Configuration group holds the persisted-config domains
// (IM/Models) that share the dirty/save footer. The "Pools & Agents" group
// holds the new standalone views (each owns its own persistence + toasts).
interface NavEntry {
  key: ViewKey;
  labelKey?: MessageKey;
  labelTerm?: string;
}

const CONFIG_GROUP: NavEntry[] = [
  { key: "im", labelKey: "settings.nav.imAdapters" },
  { key: "model", labelKey: "settings.nav.models" },
];

const POOLS_GROUP: NavEntry[] = [
  { key: "pools", labelKey: "settings.nav.pools" },
  { key: "scope", labelKey: "settings.nav.scope" },
  { key: "mcp", labelTerm: TERMS.mcp },
  { key: "skills", labelTerm: TERMS.skills },
  { key: "prompts", labelKey: "settings.nav.prompts" },
];

/** Domains backed by the /api/config persisted-config API (shared save footer). */
const PERSISTED_DOMAINS = new Set<ViewKey>(["im", "model"]);

/** All valid URL ?tab= values, in the canonical order they appear in the sidebar. */
const VALID_TABS: ReadonlySet<ViewKey> = new Set([
  "im",
  "model",
  "scope",
  "pools",
  "mcp",
  "skills",
  "prompts",
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

function formatSaveError(e: unknown, t: TFn): string {
  if (e instanceof ApiError) {
    try {
      const body = JSON.parse(e.detail) as {
        error?: string;
        fields?: Record<string, string[]>;
      };
      if (body.fields && Object.keys(body.fields).length > 0) {
        const parts = Object.entries(body.fields).map(
          ([field, msgs]) => `${field}: ${(msgs ?? []).join(", ")}`,
        );
        return t("settings.common.saveFailed", { detail: parts.join("; ") });
      }
      if (body.error) {
        return t("settings.common.saveFailed", { detail: body.error });
      }
    } catch {
      // detail is not JSON — fall through
    }
    return t("settings.common.saveFailed", { detail: `${e.status} ${e.detail}` });
  }
  return t("settings.common.saveFailed", { detail: String(e) });
}

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

export function SettingsModal({ open, onClose }: Props) {
  const toast = useToast();
  const t = useT();
  const [view, setView] = useState<ViewKey>(readInitialTab);
  const [original, setOriginal] = useState<ConfigPayload | null>(null);
  const [form, setForm] = useState<ConfigPayload | null>(null);
  const [error, setError] = useState<string>("");
  /** Open state for the discard-unsaved confirm when switching persisted views. */
  const [discardView, setDiscardView] = useState<ViewKey | null>(null);
  /** Open state for the discard-unsaved confirm when closing the modal. */
  const [discardForClose, setDiscardForClose] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);

  const isPersisted = PERSISTED_DOMAINS.has(view);
  const dirty = useDirty(form, original);

  // Close the modal. If the current persisted view has unsaved edits, prompt
  // for discard confirmation first; otherwise close immediately.
  const requestClose = useCallback((): void => {
    if (isPersisted && dirty) {
      setDiscardForClose(true);
    } else {
      onClose();
    }
  }, [isPersisted, dirty, onClose]);

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
    void load(view).catch((e: unknown) =>
      setError(t("common.failedToLoad", { error: String(e) })),
    );
  }, [view, isPersisted, t]);

  // Esc closes the modal (with the same dirty guard as the X button). When
  // the discard confirm is already open, let it handle its own Esc.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (discardView !== null || discardForClose) return;
      e.stopPropagation();
      requestClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, discardView, discardForClose, requestClose]);

  // Lock body scroll while the modal is open.
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  // Closed modal renders nothing. All hooks above must run on every render.
  if (!open) return null;

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
    const payload = assemblePayload();
    if (view === "model") {
      const errKey = validateModelValues(payload);
      if (errKey) {
        setError(t(errKey));
        return false;
      }
    }
    setSaving(true);
    setError("");
    try {
      const updated = await saveConfig(view, payload);
      setOriginal(updated);
      setForm(clone(updated));
      if (updated.restart_required) restartToast(toast, t);
      return true;
    } catch (e) {
      setError(formatSaveError(e, t));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const onCancel = (): void => {
    if (original) setForm(clone(original)); // server untouched
    setError("");
  };

  return createPortal(
    <div
      className="modal-scrim-enter fixed inset-0 z-50 flex items-center justify-center bg-overlay p-6"
      onClick={(e) => {
        if (e.target === e.currentTarget) requestClose();
      }}
    >
      <div
        className="modal-panel-enter flex h-[min(860px,92vh)] w-[min(1280px,95vw)] flex-col overflow-hidden rounded-lg border border-hairline bg-canvas-popover shadow-popover"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex flex-shrink-0 items-center gap-4 border-b border-hairline bg-canvas px-5 py-3.5">
          <h2 className="text-lg font-bold leading-tight tracking-tight text-bright">
            {t("settings.modal.title")}
          </h2>
          <div className="ml-auto flex items-center gap-2">
            <IconButton
              icon={<XIcon />}
              label={t("settings.modal.close")}
              variant="ghost"
              size="sm"
              onClick={requestClose}
            />
          </div>
        </div>

        <div
          data-testid="settings-shell"
          className="flex min-h-0 flex-1 flex-row"
        >
          <aside
            aria-label={t("settings.nav.settingsNavigation")}
            className="w-52 shrink-0 border-r border-hairline bg-canvas p-3"
          >
            <div className="space-y-4">
              <SidebarGroup
                title={t("settings.nav.configuration")}
                entries={CONFIG_GROUP}
                active={view}
                onSelect={switchView}
                t={t}
              />
              <SidebarGroup
                title={t("settings.nav.poolsAgents")}
                entries={POOLS_GROUP}
                active={view}
                onSelect={switchView}
                t={t}
              />
            </div>
          </aside>

          <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-canvas">
            <div className="flex h-full flex-col">
              <div className="flex-1 overflow-auto p-6">
                {isPersisted && (!form || !original) ? (
                  <div className="flex h-full items-center justify-center text-mute">
                    {error
                      ? t("common.failedToLoad", { error })
                      : t("common.loading")}
                  </div>
                ) : view === "mcp" ? (
                  <GlobalMcpView />
                ) : view === "skills" ? (
                  <GlobalSkillsView />
                ) : view === "scope" ? (
                  <ScopeView />
                ) : view === "pools" ? (
                  <PoolsConfigView />
                ) : view === "prompts" ? (
                  <PromptsView />
                ) : form && isPersisted ? (
                  <PersistedDomain
                    form={form}
                    error={error}
                    onChange={setForm}
                  />
                ) : null}
              </div>
              {isPersisted && form && (
                <ActionBar dirty={dirty}>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={onCancel}
                    disabled={!dirty || saving}
                  >
                    {t("common.cancel")}
                  </Button>
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={onSave}
                    disabled={!dirty || saving}
                    loading={saving}
                  >
                    {t("common.save")}
                  </Button>
                </ActionBar>
              )}
            </div>
          </section>
        </div>

        {discardView !== null || discardForClose ? (
          <ConfirmDialog
            title={t("settings.common.discardUnsavedTitle")}
            message={t("settings.common.discardSwitchView")}
            confirmLabel={t("settings.common.discard")}
            tone="danger"
            onConfirm={() => {
              if (discardForClose) {
                setDiscardForClose(false);
                onClose();
              } else if (discardView !== null) {
                const next = discardView;
                setDiscardView(null);
                setView(next);
              }
              setError("");
            }}
            onCancel={() => {
              setDiscardView(null);
              setDiscardForClose(false);
            }}
          />
        ) : null}
      </div>
    </div>,
    document.body,
  );
}

function SidebarGroup({
  title,
  entries,
  active,
  onSelect,
  t,
}: {
  title: string;
  entries: NavEntry[];
  active: ViewKey;
  onSelect: (k: ViewKey) => void;
  t: (key: MessageKey, params?: Record<string, string | number>) => string;
}) {
  return (
    <div className="rounded-lg border border-hairline bg-canvas-elevated p-2">
      <h2 className="mb-1.5 border-b border-hairline px-2 pb-1.5 text-xs font-semibold uppercase tracking-wide text-mute">
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
                {e.labelTerm ?? t(e.labelKey!)}
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
}: {
  form: ConfigPayload;
  error: string;
  onChange: (next: ConfigPayload) => void;
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
                      <span className="font-mono text-base font-semibold text-bright">
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
        />
      ) : (
        <ConfigForm
          fields={form.fields ?? []}
          values={form.values ?? {}}
          onChange={(next) => onChange({ ...form, values: next })}
        />
      )}

      {error && <p className="mt-4 text-base text-error">{error}</p>}
    </>
  );
}
