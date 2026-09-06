// RuntimeSection.tsx — the agent form's strategy-first runtime block (PRD
// Part C C3). `react` agents continue to the native sections; `external`
// agents get the provider panel restored from the pre-cutover
// ExternalMainAgentFields pattern: provider brand icon + provider_kind
// dropdown + the explicit "the provider CLI owns tools/approval/MCP/prompt"
// principle. Strategy and provider enumerations come from
// /api/scope/options (C4); brand icons + display labels are presentation
// assets (the PRD's one deliberate exception).

import type { ScopeOptions } from "../../../lib/scopeApi";
import { useT } from "../../../i18n";
import { DropdownPanel } from "../../ui/DropdownPanel";
import { resolveProvider } from "../../../types/externalProviders";
import type { ProviderKind } from "../../../types/pool";
import { PROVIDER_BRAND_ICONS } from "../externalBrands";
import { FormSection } from "./FormSection";

interface Props {
  strategy: string;
  providerKind: string;
  options: ScopeOptions;
  onStrategyChange: (value: string) => void;
  onProviderKindChange: (value: string) => void;
}

export function RuntimeSection({
  strategy,
  providerKind,
  options,
  onStrategyChange,
  onProviderKindChange,
}: Props) {
  const t = useT();
  const isExternal = strategy === "external";
  const descriptor = resolveProvider(providerKind as ProviderKind);
  const brand = PROVIDER_BRAND_ICONS[providerKind as ProviderKind];

  return (
    <FormSection title={t("settings.poolsPanel.sectionRuntime")}>
      <DropdownPanel
        label={t("settings.poolsPanel.executionStrategy")}
        value={strategy}
        options={[
          { value: "", label: t("settings.poolsPanel.strategyDefault") },
          ...options.execution_strategies.map((s) => ({ value: s, label: s })),
        ]}
        onChange={onStrategyChange}
      />
      {isExternal ? (
        <div
          role="group"
          aria-label={t("settings.poolsPanel.externalRuntime")}
          data-testid="external-runtime-panel"
          className="space-y-3 rounded-md border border-hairline bg-hairline-soft p-3"
        >
          {brand ? (
            <div className="flex items-center gap-2.5">
              <brand.Icon className="h-7 w-7 rounded-sm" />
              <span className="font-mono text-base font-semibold text-bright">
                {descriptor?.label ?? providerKind}
              </span>
            </div>
          ) : null}
          <DropdownPanel
            label={t("settings.poolsPanel.providerKind")}
            value={providerKind}
            options={[
              { value: "", label: t("settings.poolsPanel.providerKindNone") },
              ...options.provider_kinds.map((k) => ({
                value: k,
                label: resolveProvider(k as ProviderKind)?.label ?? k,
              })),
            ]}
            onChange={onProviderKindChange}
          />
          <div>
            <p className="text-xs font-medium text-ink">
              {t("settings.poolsPanel.managedByProvider")}
            </p>
            <p className="mt-1 text-xs text-body">
              {t("settings.poolsPanel.providerOwns", {
                cli: descriptor?.cliName ?? providerKind ?? "—",
              })}
            </p>
          </div>
        </div>
      ) : null}
    </FormSection>
  );
}
