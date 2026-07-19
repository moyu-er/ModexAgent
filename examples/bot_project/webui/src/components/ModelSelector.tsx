// ModelSelector.tsx — composer model picker.
//
// Rebuilt on the shared DropdownPanel primitive (DESIGN.md §5.3): compact
// pill trigger, upward-opening panel, provider groups as sticky mono eyebrow
// headers, brand "Default" badge on the provider's default model. Keyboard
// nav, selection styling and the open animation all come from DropdownPanel.

import { useMemo, type FC } from "react";
import type { ModelChoice } from "../lib/api";
import { DropdownPanel, type DropdownOption } from "./ui/DropdownPanel";
import { useT } from "../i18n";

export interface ModelSelectorValue {
  provider: string;
  model: string;
}

export interface ModelSelectorProps {
  models: ModelChoice[];
  value: ModelSelectorValue;
  onChange: (value: ModelSelectorValue) => void;
  "aria-label"?: string;
}

const keyOf = (m: ModelChoice): string => `${m.provider_name}::${m.model_name}`;

export const ModelSelector: FC<ModelSelectorProps> = ({
  models,
  value,
  onChange,
  "aria-label": ariaLabel,
}) => {
  const t = useT();
  const resolvedAriaLabel = ariaLabel ?? t("composer.model");

  const options = useMemo<DropdownOption[]>(
    () =>
      models.map((m) => ({
        value: keyOf(m),
        label: m.model_name,
        group: m.provider_name,
        badge: m.default ? t("composer.default") : undefined,
      })),
    [models, t],
  );

  const current = models.find(
    (m) => m.provider_name === value.provider && m.model_name === value.model,
  );
  const triggerLabel = current
    ? `${current.provider_name} - ${current.model_name}`
    : resolvedAriaLabel;

  const handleChange = (optionValue: string): void => {
    const m = models.find((x) => keyOf(x) === optionValue);
    if (m) onChange({ provider: m.provider_name, model: m.model_name });
  };

  return (
    <DropdownPanel
      variant="pill"
      direction="up"
      align="end"
      options={options}
      value={current ? keyOf(current) : ""}
      onChange={handleChange}
      ariaLabel={current ? `${resolvedAriaLabel}: ${triggerLabel}` : resolvedAriaLabel}
      listboxLabel={resolvedAriaLabel}
      triggerLabel={triggerLabel}
      className="shrink-0"
      triggerClassName="max-w-[160px] text-mute hover:text-ink"
      panelClassName="min-w-[240px] max-h-[min(60vh,320px)]"
    />
  );
};
