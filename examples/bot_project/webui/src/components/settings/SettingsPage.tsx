// SettingsPage.tsx — the standalone settings page (PA-11), routed at
// `#/settings` / `#/settings/<section>`. Six searchable groups:
// General / Models / Assistants / Extensions / Messaging channels / Advanced.
//
// Navigation guard (local, no global draft transaction): the page asks the
// ACTIVE editor whether it is dirty before a nav change; the user chooses
// Save and leave / Discard / Keep editing. Editors plug in via the
// `SettingsEditorHandle` contract (dirty + optional save/discard); editors
// that own persistence internally (pools, mcp, skills, prompts) register
// themselves through `useRegisterSettingsEditor`.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FC,
} from "react";
import { ArrowLeft, Search } from "lucide-react";
import { useT, type MessageKey } from "../../i18n";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "./ConfirmDialog";
import { CATEGORY, EXTENSION_ICONS, type ViewKey } from "./categoryMeta";
import { TERMS } from "../../i18n/terms";
import { GeneralSettings } from "./GeneralSettings";
import { ModelEditor } from "./ModelEditor";
import { ConfigForm } from "./ConfigForm";
import { PoolsConfigView } from "./pools/PoolsConfigView";
import { GlobalMcpView } from "./GlobalMcpView";
import { GlobalSkillsView } from "./GlobalSkillsView";
import { PromptsView } from "./PromptsView";
import { ScopeView } from "./ScopeView";
import { fetchConfig, saveConfig, ApiError } from "../../lib/api";
import { IM_BRAND_ICONS } from "./imBrands";
import type { ConfigPayload, RegistrySection } from "../../types/config";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { validateModelValues } from "./modelValidation";
import type { NavigationGuard } from "../../hooks/useHashRoute";

import { EditorContext, useRegisterSettingsEditor, useSettingsNavigation, type EditorRegistration, type EditorThunk, type SettingsEditorHandle } from "./editorNavigation";

// ── Nav declaration ─────────────────────────────────────────────────────────

const GROUP_ORDER: ViewKey[] = [
  "general",
  "model",
  "assistants",
  "extensions",
  "im",
  "advanced",
];

const VALID_SECTIONS: ReadonlySet<string> = new Set(GROUP_ORDER as string[]);

/** The nav-rail label for each group (distinct from the page-head title). */
const GROUP_NAV_LABEL: Record<string, MessageKey> = {
  general: "settings.nav.general",
  model: "settings.nav.models",
  assistants: "settings.nav.assistants",
  extensions: "settings.nav.extensions",
  im: "settings.nav.channels",
  advanced: "settings.nav.advanced",
  scope: "settings.scope.title",
  pools: "settings.nav.pools",
  mcp: "settings.nav.scope",
  skills: "settings.nav.scope",
  prompts: "settings.nav.prompts",
};

/** Resolve a group's label at render time (i18n + proper nouns). */
function groupLabel(key: ViewKey, t: (k: MessageKey) => string): string {
  const navKey = GROUP_NAV_LABEL[key];
  if (navKey) return t(navKey);
  const meta = CATEGORY[key];
  return meta.titleTerm ?? t(meta.titleKey!);
}

function groupSub(key: ViewKey, t: (k: MessageKey) => string): string {
  return t(CATEGORY[key].subKey);
}

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

function formatSaveError(
  e: unknown,
  t: (k: MessageKey, p?: Record<string, string | number>) => string,
): string {
  if (e instanceof ApiError) {
    try {
      const body = JSON.parse(e.detail) as {
        error?: string;
        fields?: Record<string, string[]>;
      };
      if (body.fields && Object.keys(body.fields).length > 0) {
        const parts = Object.entries(body.fields).map(
          ([f, msgs]) => `${f}: ${(msgs ?? []).join(", ")}`,
        );
        return t("settings.common.saveFailed", { detail: parts.join("; ") });
      }
      if (body.error) return t("settings.common.saveFailed", { detail: body.error });
    } catch {
      // not JSON
    }
    return t("settings.common.saveFailed", { detail: `${e.status} ${e.detail}` });
  }
  return t("settings.common.saveFailed", { detail: String(e) });
}

// ── Persisted-domain editor (model / im) ────────────────────────────────────

function PersistedDomainEditor({ domain }: { domain: "model" | "im" }) {
  const toast = useToast();
  const t = useT();
  const [original, setOriginal] = useState<ConfigPayload | null>(null);
  const [form, setForm] = useState<ConfigPayload | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setOriginal(null);
    setForm(null);
    setError("");
    fetchConfig(domain)
      .then((payload) => {
        if (cancelled) return;
        setOriginal(payload);
        setForm(clone(payload));
        if (payload.restart_required) toast.restart.setRestartNeeded(true);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(t("common.failedToLoad", { error: String(e) }));
      });
    return (): void => {
      cancelled = true;
    };
  }, [domain, toast, t]);

  const dirty = useMemo(
    () => (form && original ? JSON.stringify(form) !== JSON.stringify(original) : false),
    [form, original],
  );

  const save = useCallback(async (): Promise<boolean> => {
    if (!form) return false;
    const payload =
      form.flavor === "registry"
        ? Object.fromEntries(
            Object.entries(form.sections ?? {}).map(([k, sec]) => [
              k,
              (sec as RegistrySection).values,
            ]),
          )
        : form.values ?? {};
    if (domain === "model") {
      const errKey = validateModelValues(payload);
      if (errKey) {
        setError(t(errKey));
        return false;
      }
    }
    setSaving(true);
    setError("");
    try {
      const updated = await saveConfig(domain, payload);
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
  }, [form, domain, toast, t]);

  const discard = useCallback(() => {
    if (original) setForm(clone(original));
    setError("");
  }, [original]);

  const handle = useMemo<SettingsEditorHandle>(
    () => ({ isDirty: () => dirty, save, discard }),
    [dirty, save, discard],
  );
  useRegisterSettingsEditor(() => handle);

  return (
    <div className="mx-auto max-w-[980px]">
      {!form ? (
        <div className="flex h-40 items-center justify-center text-mute">
          {error ? t("common.failedToLoad", { error }) : t("common.loading")}
        </div>
      ) : domain === "model" ? (
        <ModelEditor
          values={form.values ?? {}}
          onChange={(next) => setForm({ ...form, values: next })}
        />
      ) : (
        <div className="space-y-6">
          {Object.entries(form.sections ?? {}).map(([key, sec]) => {
            const section = sec as RegistrySection;
            return (
              <div
                key={key}
                className="rounded-lg border border-hairline bg-canvas-elevated p-5"
              >
                {(() => {
                  const brand = IM_BRAND_ICONS[key];
                  if (!brand) {
                    return (
                      <h3 className="mb-4 font-mono text-base font-semibold text-bright">
                        {section.label}
                      </h3>
                    );
                  }
                  const { Icon, color } = brand;
                  return (
                    <div className="mb-4 flex items-center gap-2.5">
                      <span
                        className="inline-flex h-8 w-8 items-center justify-center rounded-md"
                        style={{ backgroundColor: color }}
                      >
                        <Icon className="h-5 w-5 text-white" />
                      </span>
                      <h3 className="font-mono text-base font-semibold text-bright">
                        {section.label}
                      </h3>
                    </div>
                  );
                })()}
                <ConfigForm
                  fields={section.fields}
                  values={section.values}
                  onChange={(next) =>
                    setForm({
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
      )}
      {form ? (
        <>
          {dirty ? (
            <div className="mt-6 flex items-center gap-2 border-t border-hairline pt-4">
              <Button variant="secondary" size="sm" onClick={discard} disabled={saving}>
                {t("common.cancel")}
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={() => void save()}
                disabled={saving}
                loading={saving}
              >
                {t("common.save")}
              </Button>
            </div>
          ) : null}
          {error ? (
            <p className="mt-4 text-base text-error" role="alert">
              {error}
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

// ── Extensions group: MCP / Skills / Prompts tabs ───────────────────────────

function ExtensionsEditor() {
  const t = useT();
  const requestLeave = useSettingsNavigation();
  const [tab, setTab] = useState<"mcp" | "skills" | "prompts">("mcp");
  const tabs = [
    { key: "mcp" as const, label: TERMS.mcp, icon: EXTENSION_ICONS.mcp.icon },
    { key: "skills" as const, label: TERMS.skills, icon: EXTENSION_ICONS.skills.icon },
    {
      key: "prompts" as const,
      label: t("settings.nav.prompts"),
      icon: EXTENSION_ICONS.prompts.icon,
    },
  ];
  return (
    <div data-testid="settings-extensions" className="flex h-full min-h-0 flex-col">
      <div className="mb-4 flex gap-1 border-b border-hairline" role="tablist">
        {tabs.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            onClick={() => requestLeave(() => setTab(key))}
            className={`-mb-px flex items-center gap-2 border-b-2 px-4 py-2 text-base transition-colors ${
              tab === key ? "border-brand text-ink" : "border-transparent text-mute hover:text-body"
            }`}
          >
            <Icon size={14} />
            {label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {tab === "mcp" ? <GlobalMcpView /> : tab === "skills" ? <GlobalSkillsView /> : <PromptsView />}
      </div>
    </div>
  );
}

// ── The page ────────────────────────────────────────────────────────────────

export interface SettingsPageProps {
  setNavigationGuard?: (guard: NavigationGuard | null) => void;
  /** Initial section (from the route); defaults to general. */
  section?: string;
  /** Navigate to a settings section (hash): "/settings/model" etc. */
  navigate: (path: string) => void;
  /** Leave settings entirely (back to chat). */
  onExit: () => void;
  onPreferencesChanged?: (prefs: {
    defaultWorkspace: string | null;
    defaultPool: string;
  }) => void;
}

export const SettingsPage: FC<SettingsPageProps> = ({
  section,
  navigate,
  onExit,
  onPreferencesChanged,
  setNavigationGuard,
}) => {
  const t = useT();
  const active: ViewKey = VALID_SECTIONS.has(section ?? "")
    ? ((section ?? "general") as ViewKey)
    : "general";
  const [query, setQuery] = useState("");
  const [pendingNav, setPendingNav] = useState<{ to: string; exit: boolean; resume?: () => void; preserved?: SettingsEditorHandle } | null>(null);
  const [editors, setEditors] = useState<EditorThunk[]>([]);
  const register = useCallback((h: EditorThunk) => {
    setEditors((prev) => [...prev, h]);
    return () => setEditors((prev) => prev.filter((item) => item !== h));
  }, []);
  const editorThunk = useCallback((preserved?: SettingsEditorHandle): SettingsEditorHandle => ({
    isDirty: () => editors.some((read) => read() !== preserved && read().isDirty()),
    save: async () => {
      for (const read of editors) {
        const handle = read();
        if (handle !== preserved && handle.isDirty() && (!handle.save || !(await handle.save()))) return false;
      }
      return true;
    },
    discard: () => editors.forEach((read) => { const handle = read(); if (handle !== preserved && handle.isDirty()) handle.discard?.(); }),
  }), [editors]);
  const requestLeave = useCallback<EditorRegistration["requestLeave"]>((resume, preserved) => {
    if (editorThunk(preserved).isDirty()) setPendingNav({ to: "", exit: false, resume, preserved });
    else resume();
  }, [editorThunk]);
  useEffect(() => {
    setNavigationGuard?.(requestLeave);
    return () => setNavigationGuard?.(null);
  }, [setNavigationGuard, requestLeave]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent): void => {
      if (editorThunk().isDirty()) { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [editorThunk]);

  // The registered thunk reads the live editor state at call time.
  const editor = editorThunk(pendingNav?.preserved);

  // Search across group labels + keywords (local, no backend).
  const q = query.trim().toLowerCase();
  const searchResults = useMemo(() => {
    if (!q) return null;
    return GROUP_ORDER.filter((key) => {
      const label = groupLabel(key, t).toLowerCase();
      return label.includes(q) || (CATEGORY[key].keywords ?? []).some((k) => k.includes(q));
    });
  }, [q, t]);

  const pendingRef = useRef<typeof pendingNav>(null);
  pendingRef.current = pendingNav;

  const runPendingNav = useCallback((): void => {
    const p = pendingRef.current;
    pendingRef.current = null;
    setPendingNav(null);
    if (!p) return;
    if (p.resume) p.resume();
    else if (p.exit) onExit();
    else navigate(p.to);
  }, [navigate, onExit]);

  const requestNav = useCallback(
    (to: string, exit: boolean): void => {
      if (setNavigationGuard) {
        if (exit) onExit();
        else navigate(to);
        return;
      }
      // Read the registered handle AT CALL TIME — a deps-stale closure here
      // would freeze the dirty state at first render.
      const handle = typeof editorThunk === "function" ? editorThunk() : null;
      if (handle?.isDirty()) {
        setPendingNav({ to, exit });
        return;
      }
      if (exit) onExit();
      else navigate(to);
    },
    [navigate, onExit, editorThunk, setNavigationGuard],
  );

  const saveAndLeave = useCallback((): void => {
    const handle = editorThunk(pendingRef.current?.preserved);
    if (!handle?.save) return;
    const nav = pendingRef.current;
    setPendingNav(null);
    void handle
      .save()
      .then((ok) => {
        if (!ok) return; // save failed — stay (the editor shows its error)
        if (!nav) return;
        if (nav.resume) nav.resume();
        else if (nav.exit) onExit();
        else navigate(nav.to);
      })
      .catch(() => {
        /* stay — the editor surfaces the failure */
      });
  }, [navigate, onExit, editorThunk]);

  // Esc leaves the search field (not the page).
  const dirty = editor?.isDirty() ?? false;

  return (
    <EditorContext.Provider value={{ register, requestLeave }}>
      <div className="flex h-full min-h-0 flex-col bg-canvas">
        {/* Header: back + search */}
        <div className="flex h-14 shrink-0 items-center gap-3 border-b border-hairline px-4">
          <Button className="shrink-0 whitespace-nowrap" variant="ghost" size="sm" onClick={() => requestNav("", true)}>
            <span className="flex items-center gap-1.5">
              <ArrowLeft size={15} aria-hidden="true" />
              {t("settings.nav.back")}
            </span>
          </Button>
          <div className="relative ml-auto w-full max-w-[320px]">
            <Search
              size={14}
              className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint"
              aria-hidden="true"
            />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("settings.nav.searchPlaceholder")}
              aria-label={t("settings.nav.searchPlaceholder")}
              className="w-full rounded-pill border border-hairline bg-canvas-elevated py-1.5 pl-8 pr-3 text-sm text-ink placeholder:text-faint focus:border-brand focus:outline-none"
            />
          </div>
          {dirty ? (
            <span
              role="status"
              aria-label={t("ui.unsavedChanges")}
              title={t("ui.unsavedChanges")}
              className="h-2 w-2 shrink-0 rounded-full bg-warning"
            />
          ) : null}
        </div>

        <div className="flex min-h-0 flex-1 flex-col md:flex-row" data-testid="settings-shell">
          {/* Nav rail */}
          <aside
            aria-label={t("settings.nav.settingsNavigation")}
            className="w-full shrink-0 overflow-x-auto border-b border-hairline bg-canvas-sidebar p-3 md:w-56 md:border-b-0 md:border-r"
          >
            {searchResults ? (
              <ul className="space-y-0.5" data-testid="settings-search-results">
                {searchResults.length === 0 && (
                  <li className="px-2 py-1.5 text-sm text-mute">
                    {t("settings.nav.searchNoMatch", { query: query.trim() })}
                  </li>
                )}
                {searchResults.map((key) => {
                  const meta = CATEGORY[key];
                  const Icon = meta.icon;
                  return (
                    <li key={key}>
                      <button
                        type="button"
                        className="nav-item active"
                        style={{ ["--cat" as string]: meta.catVar }}
                        onClick={() => {
                          setQuery("");
                          requestNav(`/settings/${key}`, false);
                        }}
                      >
                        <span className="category-chip">
                          <Icon size={14} />
                        </span>
                        {groupLabel(key, t)}
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <ul className="flex gap-1 md:block md:space-y-0.5">
                {GROUP_ORDER.map((key) => {
                  const meta = CATEGORY[key];
                  const Icon = meta.icon;
                  return (
                    <li key={key} className="shrink-0 whitespace-nowrap">
                      <button
                        type="button"
                        className={`nav-item${key === active ? " active" : ""}`}
                        style={{ ["--cat" as string]: meta.catVar }}
                        onClick={() => requestNav(`/settings/${key}`, false)}
                      >
                        <span className="category-chip">
                          <Icon size={14} />
                        </span>
                        {groupLabel(key, t)}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </aside>

          {/* Content */}
          <section className="min-h-0 min-w-0 flex-1 overflow-auto p-4 md:p-6">
            {active === "im" ? (
              <div className="page-head mb-4">
                <span
                  className="page-head-icon"
                  style={{ ["--cat" as string]: CATEGORY[active].catVar }}
                >
                  {(() => {
                    const Icon = CATEGORY[active].icon;
                    return <Icon size={18} />;
                  })()}
                </span>
                <div>
                  <div className="page-title">{groupLabel(active, t)}</div>
                  <div className="page-sub">{groupSub(active, t)}</div>
                </div>
              </div>
            ) : null}

            {active === "general" ? (
              <GeneralSettings onPreferencesChanged={onPreferencesChanged} />
            ) : active === "model" ? (
              <PersistedDomainEditor domain="model" />
            ) : active === "assistants" ? (
              <PoolsConfigView />
            ) : active === "extensions" ? (
              <ExtensionsEditor />
            ) : active === "im" ? (
              <PersistedDomainEditor domain="im" />
            ) : (
              <ScopeView />
            )}
          </section>
        </div>

        {pendingNav ? (
          <ConfirmDialog
            title={t("settings.common.discardUnsavedTitle")}
            message={t("settings.common.discardSwitchView")}
            confirmLabel={t("settings.common.discard")}
            extraActions={
              editor?.save ? (
                <Button variant="primary" size="sm" onClick={saveAndLeave}>
                  {t("settings.common.saveAndLeave")}
                </Button>
              ) : undefined
            }
            tone="danger"
            onConfirm={() => {
              editor?.discard?.();
              runPendingNav();
            }}
            onCancel={() => setPendingNav(null)}
          />
        ) : null}
      </div>
    </EditorContext.Provider>
  );
};
