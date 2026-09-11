// CapabilityRow.tsx — one capability bundle row in the agent form (PRD Part
// C C1). Shows the bill-reported state (auto / declared / vetoed) with an
// auto-reason, the carried tools+hooks as read-only bundle chips, and the
// tri-state control (follow auto / force on / force off) — degrading to a
// plain on/off checkbox where the bill shows the capability never
// auto-applies for this agent.

import { useState } from "react";
import type { ScopeCapabilityBill, ScopeCapabilityBundle } from "../../../lib/scopeApi";
import { useT } from "../../../i18n";
import { Checkbox } from "../../ui/Checkbox";
import { DropdownPanel } from "../../ui/DropdownPanel";
import { Badge, Chip } from "./chips";
import type { CapabilityMode } from "./scopeModel";

const CHIP_PREVIEW_COUNT = 6;

interface Props {
  name: string;
  /** The bill entry for this capability on this agent, if it has one. */
  bill: ScopeCapabilityBill | null;
  bundle: ScopeCapabilityBundle | null;
  isRoot: boolean;
  mode: CapabilityMode;
  config: Record<string, unknown>;
  onModeChange: (mode: CapabilityMode) => void;
  onConfigFieldChange: (key: string, value: unknown) => void;
}

function shellModeLabel(mode: string, t: ReturnType<typeof useT>): string {
  if (mode === "subprocess") return t("settings.poolsPanel.shellModeSubprocess");
  if (mode === "persistent") return t("settings.poolsPanel.shellModePersistent");
  if (mode === "terminal") return t("settings.poolsPanel.shellModeTerminal");
  return mode;
}

export function CapabilityRow({
  name,
  bill,
  bundle,
  isRoot,
  mode,
  config,
  onModeChange,
  onConfigFieldChange,
}: Props) {
  const t = useT();
  const [expanded, setExpanded] = useState(false);

  const items = [
    ...(bundle?.tools ?? []).map((tool) => ({ kind: "tool" as const, name: tool })),
    ...(bundle?.hooks ?? []).map((hook) => ({ kind: "hook" as const, name: hook })),
  ];
  const visible = expanded ? items : items.slice(0, CHIP_PREVIEW_COUNT);
  const hiddenCount = items.length - visible.length;
  const modeField = name === "shell" ? bundle?.config_fields.mode : undefined;
  const configuredMode = typeof config.mode === "string" ? config.mode : null;
  const defaultMode = typeof modeField?.default === "string" ? modeField.default : "";
  const shellMode = configuredMode ?? defaultMode;
  const shellModes = (modeField?.choices ?? []).filter(
    (choice): choice is string => typeof choice === "string",
  );
  const visibleShellModes = shellModes.filter(
    (choice) => isRoot || choice !== "terminal" || shellMode === "terminal",
  );

  return (
    <div
      className="rounded-md border border-hairline p-3"
      data-testid={`capability-row-${name}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-medium text-ink">{name}</span>
        {bill?.state === "auto" ? (
          <>
            <Badge tone="brand">{t("settings.poolsPanel.capStateAuto")}</Badge>
            <span className="text-xs text-mute">
              {isRoot
                ? t("settings.poolsPanel.capAutoReasonRoot")
                : t("settings.poolsPanel.capAutoReasonSub")}
            </span>
          </>
        ) : bill?.state === "declared" ? (
          <Badge tone="brand">{t("settings.poolsPanel.capStateDeclared")}</Badge>
        ) : bill?.state === "vetoed" ? (
          <Badge tone="danger">{t("settings.poolsPanel.capStateVetoed")}</Badge>
        ) : null}
        <span className="ml-auto">
          {bill !== null ? (
            <DropdownPanel
              variant="pill"
              ariaLabel={t("settings.poolsPanel.capControl", { name })}
              value={mode}
              options={[
                { value: "auto", label: t("settings.poolsPanel.capFollowAuto") },
                { value: "on", label: t("settings.poolsPanel.capForceOn") },
                { value: "off", label: t("settings.poolsPanel.capForceOff") },
              ]}
              onChange={(v) => onModeChange(v as CapabilityMode)}
            />
          ) : (
            <Checkbox
              label={t("settings.poolsPanel.capEnable")}
              checked={mode === "on"}
              onChange={(e) => onModeChange(e.target.checked ? "on" : "auto")}
            />
          )}
        </span>
      </div>
      {items.length > 0 ? (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {visible.map((item) => (
            <Chip
              key={`${item.kind}:${item.name}`}
              title={
                item.kind === "tool"
                  ? t("settings.poolsPanel.bundleTool", { name: item.name })
                  : t("settings.poolsPanel.bundleHook", { name: item.name })
              }
            >
              {item.name}
              {item.kind === "hook" ? (
                <span className="text-faint">{t("settings.poolsPanel.hookTag")}</span>
              ) : null}
            </Chip>
          ))}
          {hiddenCount > 0 ? (
            <button
              type="button"
              onClick={() => setExpanded(true)}
              className="rounded-pill px-2 py-1 text-xs text-brand transition-colors duration-fast hover:bg-hairline-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
            >
              {t("settings.poolsPanel.moreItems", { count: hiddenCount })}
            </button>
          ) : null}
        </div>
      ) : null}
      {(bundle?.tool_groups.length ?? 0) > 0 ? (
        <div className="mt-2 space-y-1.5">
          {bundle!.tool_groups.map((group) => (
            <div
              key={group.anchor}
              data-testid={`capability-tool-group-${group.anchor}`}
              className="rounded-sm border border-hairline-soft px-2 py-1.5"
            >
              <div className="font-mono text-xs text-body">{group.anchor}</div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {group.variants.map((variant) => (
                  <Chip key={variant.name} title={t("settings.poolsPanel.groupCandidateTitle")}>
                    {variant.name}: {variant.tools.join(", ")}
                  </Chip>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : null}
      {modeField && mode !== "off" ? (
        <div className="mt-3 space-y-2 border-t border-hairline pt-3">
          <DropdownPanel
            label={t("settings.poolsPanel.shellMode")}
            ariaLabel={t("settings.poolsPanel.shellMode")}
            value={shellMode}
            options={visibleShellModes.map((value) => ({
              value,
              label: shellModeLabel(value, t),
            }))}
            onChange={(value) => onConfigFieldChange("mode", value)}
          />
          {!isRoot && shellMode === "terminal" ? (
            <p className="text-xs text-mute">{t("settings.poolsPanel.shellSubTerminalHint")}</p>
          ) : null}
          {isRoot && shellMode === "terminal" ? (
            <Checkbox
              label={t("settings.poolsPanel.terminalVisibility")}
              helper={t("settings.poolsPanel.terminalVisibilityHelper")}
              checked={config.terminal_visibility === true}
              onChange={(event) =>
                onConfigFieldChange("terminal_visibility", event.target.checked)
              }
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
