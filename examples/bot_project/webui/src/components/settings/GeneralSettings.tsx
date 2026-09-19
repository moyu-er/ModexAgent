// GeneralSettings.tsx — the "General" settings page (PA-11): language,
// theme, default workspace, default assistant. Language and theme are
// frontend presentation preferences (localStorage); the two defaults are
// the personal_assistant preference domain (immediate save, no restart).

import { useCallback, useEffect, useMemo, useState, type FC } from "react";
import { Check } from "lucide-react";
import { useT } from "../../i18n";
import { useLocale } from "../../hooks/useLocale";
import { useTheme } from "../../hooks/useTheme";
import { Button } from "../ui/Button";
import { HelperText } from "../ui/HelperText";
import { SectionLabel } from "../ui/SectionLabel";
import {
  fetchPreferences,
  savePreferences,
  type PersonalAssistantPreferences,
} from "../../lib/preferencesApi";
import { fetchWorkspace } from "../../lib/api";
import { listPools } from "../../lib/poolApi";

interface Props {
  /** Notify the shell that a default workspace/pool changed (immediate). */
  onPreferencesChanged?: (prefs: PersonalAssistantPreferences) => void;
}

function Row({
  label,
  helper,
  children,
}: {
  label: string;
  helper?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-1 gap-2 border-b border-hairline py-4 last:border-b-0 md:grid-cols-[minmax(180px,240px)_1fr] md:gap-6">
      <div>
        <span className="text-base text-ink">{label}</span>
        {helper ? <HelperText>{helper}</HelperText> : null}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export const GeneralSettings: FC<Props> = ({ onPreferencesChanged }) => {
  const t = useT();
  const { locale, setLocale, available } = useLocale();
  const { theme, setTheme } = useTheme();
  const [prefs, setPrefs] = useState<PersonalAssistantPreferences | null>(null);
  const [recent, setRecent] = useState<string[]>([]);
  const [poolNames, setPoolNames] = useState<string[]>([]);
  const [loadError, setLoadError] = useState<string>("");
  const [saveError, setSaveError] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetchPreferences(),
      fetchWorkspace().catch(() => null),
      listPools().catch(() => []),
    ])
      .then(([p, wsInfo, pools]) => {
        if (cancelled) return;
        setPrefs(p);
        setRecent((wsInfo?.recent ?? []).map((r) => r.path));
        setPoolNames(pools.map((pl) => pl.name));
        setLoadError("");
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(String(e));
      });
    return (): void => {
      cancelled = true;
    };
  }, []);

  const persist = useCallback(
    async (next: PersonalAssistantPreferences): Promise<void> => {
      setSaveError("");
      try {
        const saved = await savePreferences(next);
        setPrefs(saved);
        onPreferencesChanged?.(saved);
      } catch (e) {
        setSaveError(t("settings.general.saveFailed", { detail: String(e) }));
      }
    },
    [onPreferencesChanged, t],
  );

  const pickDefaultWorkspace = useCallback(
    (path: string | null): void => {
      if (!prefs) return;
      // The server validates + canonicalizes the path on save; a raw string
      // typed from recents is already canonical.
      void persist({ ...prefs, defaultWorkspace: path });
    },
    [prefs, persist],
  );

  const defaultPoolMissing = useMemo(
    () =>
      prefs !== null &&
      poolNames.length > 0 &&
      prefs.defaultPool !== "" &&
      !poolNames.includes(prefs.defaultPool),
    [prefs, poolNames],
  );

  if (loadError) {
    return <p className="text-base text-error">{t("common.failedToLoad", { error: loadError })}</p>;
  }
  if (prefs === null) {
    return <p className="text-base text-mute">{t("common.loading")}</p>;
  }

  return (
    <div data-testid="settings-general" className="mx-auto max-w-[820px] space-y-6">
      <SectionLabel>{t("settings.general.title")}</SectionLabel>

      <Row label={t("settings.general.language")} helper={t("settings.general.languageHelper")}>
        <select
          value={locale}
          onChange={(e) => setLocale(e.target.value)}
          className="w-full max-w-[320px] rounded-sm border border-hairline bg-canvas px-2.5 py-2 text-base text-ink focus:border-brand focus:outline-none"
          aria-label={t("settings.general.language")}
        >
          {available.map((l) => (
            <option key={l.value} value={l.value}>
              {l.label}
            </option>
          ))}
        </select>
      </Row>

      <Row label={t("settings.general.theme")}>
        <div className="flex gap-2">
          {(
            [
              ["dark", t("settings.general.themeDark")],
              ["light", t("settings.general.themeLight")],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={theme === value}
              onClick={() => setTheme(value)}
              className={`rounded-pill border px-4 py-1.5 text-base transition-colors ${
                theme === value
                  ? "border-brand bg-hairline-soft text-ink"
                  : "border-hairline text-body hover:bg-hairline-soft"
              }`}
            >
              {theme === value ? <Check size={12} className="mr-1 inline" aria-hidden="true" /> : null}
              {label}
            </button>
          ))}
        </div>
      </Row>

      <Row
        label={t("settings.general.defaultWorkspace")}
        helper={t("settings.general.defaultWorkspaceHelper")}
      >
        <div className="space-y-2">
          <select
            value={prefs.defaultWorkspace ?? ""}
            onChange={(e) => pickDefaultWorkspace(e.target.value || null)}
            aria-label={t("settings.general.defaultWorkspace")}
            className="w-full max-w-[480px] rounded-sm border border-hairline bg-canvas px-2.5 py-2 font-mono text-sm text-ink focus:border-brand focus:outline-none"
          >
            <option value="">{t("settings.general.defaultWorkspaceNone")}</option>
            {recent.map((path) => (
              <option key={path} value={path}>
                {path}
              </option>
            ))}
            {/* Keep a saved default that fell out of recents selectable. */}
            {prefs.defaultWorkspace && !recent.includes(prefs.defaultWorkspace) && (
              <option value={prefs.defaultWorkspace}>{prefs.defaultWorkspace}</option>
            )}
          </select>
          {prefs.defaultWorkspace && (
            <Button variant="secondary" size="sm" onClick={() => pickDefaultWorkspace(null)}>
              {t("settings.general.defaultWorkspaceClear")}
            </Button>
          )}
        </div>
      </Row>

      <Row
        label={t("settings.general.defaultPool")}
        helper={t("settings.general.defaultPoolHelper")}
      >
        <div className="space-y-2">
          <select
            value={prefs.defaultPool}
            onChange={(e) => prefs && void persist({ ...prefs, defaultPool: e.target.value })}
            aria-label={t("settings.general.defaultPool")}
            className="w-full max-w-[320px] rounded-sm border border-hairline bg-canvas px-2.5 py-2 text-base text-ink focus:border-brand focus:outline-none"
          >
            {defaultPoolMissing && prefs.defaultPool && (
              <option value={prefs.defaultPool}>{prefs.defaultPool}</option>
            )}
            {poolNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          {defaultPoolMissing && (
            <HelperText>
              {t("settings.general.defaultPoolInvalid")}
            </HelperText>
          )}
          {!defaultPoolMissing && poolNames.length === 0 && (
            <HelperText>{t("settings.general.poolUnavailable")}</HelperText>
          )}
        </div>
      </Row>

      {saveError ? (
        <p className="text-base text-error" role="alert">
          {saveError}
        </p>
      ) : null}
    </div>
  );
};
