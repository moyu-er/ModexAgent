// Main-agent fields for external pools. External pools run their agent
// in a provider CLI, so the framework no longer owns max steps, terminal,
// tools, approval, MCP, skills or the system prompt — those are managed by
// the provider. The Implementation and Provider controls are grouped together
// at the top in one runtime-first panel, followed by the identity fields the
// pool needs for routing and inter-agent communication. All provider options,
// defaults and help copy derive from the externalProviders catalog.

import type { MainAgentNode, ProviderKind } from "../../types/pool";
import {
  PROVIDER_OPTIONS,
  descriptorFor,
  selectProvider,
} from "../../types/externalProviders";
import { Input } from "../ui/Input";
import { DropdownPanel } from "../ui/DropdownPanel";
import { PROVIDER_BRAND_ICONS } from "./externalBrands";
import { useT, type MessageKey } from "../../i18n";

interface ErrFn {
  (loc: string): string | undefined;
}

export type ImplementationChoice = "react" | "external";

const IMPLEMENTATION_DEFS: { value: ImplementationChoice; labelKey: MessageKey }[] = [
  { value: "react", labelKey: "settings.external.native" },
  { value: "external", labelKey: "settings.external.external" },
];

export { IMPLEMENTATION_DEFS };

export interface ExternalMainAgentFieldsProps {
  node: MainAgentNode;
  savedAgentName: string;
  errFor: ErrFn;
  patch: (p: Partial<MainAgentNode>) => void;
  implementationValue: ImplementationChoice;
  onImplementationChange: (next: ImplementationChoice) => void;
}

export function ExternalMainAgentFields({
  node,
  savedAgentName,
  errFor,
  patch,
  implementationValue,
  onImplementationChange,
}: ExternalMainAgentFieldsProps) {
  const t = useT();
  const descriptor = descriptorFor(node.provider_kind);
  const IMPLEMENTATION_OPTIONS = IMPLEMENTATION_DEFS.map((d) => ({
    value: d.value,
    label: t(d.labelKey),
  }));

  return (
    <>
      <div
        role="group"
        aria-label={t("settings.external.externalRuntime")}
        data-testid="external-runtime-panel"
        className="space-y-3 rounded-md border border-hairline bg-hairline-soft p-3"
      >
        {(() => {
          const brand = node.provider_kind ? PROVIDER_BRAND_ICONS[node.provider_kind] : undefined;
          if (!brand) return null;
          const { Icon } = brand;
          return (
            <div className="flex items-center gap-2.5">
              <Icon className="h-7 w-7 rounded-sm" />
              <span className="font-mono text-base font-semibold text-bright">
                {descriptor.label}
              </span>
            </div>
          );
        })()}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <DropdownPanel
            label={t("settings.external.implementation")}
            options={IMPLEMENTATION_OPTIONS}
            value={implementationValue}
            onChange={(v) =>
              onImplementationChange(v as ImplementationChoice)
            }
          />
          <DropdownPanel
            label={t("settings.external.provider")}
            options={PROVIDER_OPTIONS}
            value={selectProvider(node.provider_kind)}
            onChange={(v) =>
              patch({ provider_kind: v as ProviderKind })
            }
          />
        </div>
        <div>
          <p className="text-xs font-medium text-ink">
            {t("settings.external.managedByProvider")}
          </p>
          <p className="mt-1 text-xs text-body">
            {t("settings.external.providerRunHelper", { cli: descriptor.cliName })}
          </p>
        </div>
      </div>

      <Input
        label={t("settings.external.agentName")}
        required
        error={errFor("main.agent_name")}
        value={node.agent_name}
        onChange={(e) => patch({ agent_name: e.target.value })}
        disabled={savedAgentName !== ""}
        helper={savedAgentName !== "" ? t("settings.pools.agentNameLocked") : undefined}
      />

      <Input
        label={t("settings.external.description")}
        error={errFor("main.description")}
        helper={t("settings.external.descriptionHelper")}
        value={node.description}
        onChange={(e) => patch({ description: e.target.value })}
      />
    </>
  );
}
