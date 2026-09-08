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
  onModeChange: (mode: CapabilityMode) => void;
}

export function CapabilityRow({ name, bill, bundle, isRoot, mode, onModeChange }: Props) {
  const t = useT();
  const [expanded, setExpanded] = useState(false);

  const items = [
    ...(bundle?.tools ?? []).map((tool) => ({ kind: "tool" as const, name: tool })),
    ...(bundle?.hooks ?? []).map((hook) => ({ kind: "hook" as const, name: hook })),
  ];
  const visible = expanded ? items : items.slice(0, CHIP_PREVIEW_COUNT);
  const hiddenCount = items.length - visible.length;

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
    </div>
  );
}
