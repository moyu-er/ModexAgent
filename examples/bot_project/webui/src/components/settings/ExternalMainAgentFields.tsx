// Main-agent fields for external_coding pools. External pools run their agent
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
import { Select } from "../ui/Select";
import { PROVIDER_BRAND_ICONS } from "./externalBrands";

interface ErrFn {
  (loc: string): string | undefined;
}

export type ImplementationChoice = "react" | "external_coding";

export const IMPLEMENTATION_OPTIONS: {
  value: ImplementationChoice;
  label: string;
}[] = [
  { value: "react", label: "Native" },
  { value: "external_coding", label: "External" },
];

export interface ExternalMainAgentFieldsProps {
  node: MainAgentNode;
  errFor: ErrFn;
  patch: (p: Partial<MainAgentNode>) => void;
  implementationValue: ImplementationChoice;
  onImplementationChange: (next: ImplementationChoice) => void;
}

export function ExternalMainAgentFields({
  node,
  errFor,
  patch,
  implementationValue,
  onImplementationChange,
}: ExternalMainAgentFieldsProps) {
  const descriptor = descriptorFor(node.provider_kind);

  return (
    <>
      <div
        role="group"
        aria-label="External runtime"
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
              <span className="font-mono text-sm font-semibold text-bright">
                {descriptor.label}
              </span>
            </div>
          );
        })()}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Select
            label="Implementation"
            options={IMPLEMENTATION_OPTIONS}
            value={implementationValue}
            onChange={(e) =>
              onImplementationChange(e.target.value as ImplementationChoice)
            }
          />
          <Select
            label="Provider"
            options={PROVIDER_OPTIONS}
            value={selectProvider(node.provider_kind)}
            onChange={(e) =>
              patch({ provider_kind: e.target.value as ProviderKind })
            }
          />
        </div>
        <div>
          <p className="text-xs font-medium text-ink">
            Managed by the provider runtime
          </p>
          <p className="mt-1 text-xs text-body">
            Max steps, terminal, tool preset, approval, MCP, skills and the
            system prompt are controlled by the {descriptor.cliName} CLI. This
            agent receives work and replies through{" "}
            <code className="font-mono">send_to_agent</code>.
          </p>
        </div>
      </div>

      <Input
        label="Agent name"
        required
        error={errFor("main.agent_name")}
        value={node.agent_name}
        onChange={(e) => patch({ agent_name: e.target.value })}
      />

      <Input
        label="Description"
        error={errFor("main.description")}
        helper="Used for agent discovery and inter-agent communication via send_to_agent."
        value={node.description}
        onChange={(e) => patch({ description: e.target.value })}
      />
    </>
  );
}
