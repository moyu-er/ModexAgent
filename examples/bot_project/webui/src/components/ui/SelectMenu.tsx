// SelectMenu.tsx — sidebar pool picker (inline pill variant of DropdownPanel).
//
// Thin adapter kept for the picker's call-site API ({ options, value,
// onChange }); all rendering and interaction logic lives in DropdownPanel
// (DESIGN.md §5.3 — one dropdown spec, two trigger variants). The picker uses
// the compact pill trigger stretched to the sidebar's full width.

import type { FC } from "react";
import { DropdownPanel } from "./DropdownPanel";

export interface SelectMenuOption {
  value: string;
  label: string;
}

export interface SelectMenuProps {
  options: SelectMenuOption[];
  value: string;
  onChange: (value: string) => void;
  className?: string;
  /** Accessible label for the trigger button. */
  ariaLabel?: string;
}

export const SelectMenu: FC<SelectMenuProps> = ({
  options,
  value,
  onChange,
  className = "",
  ariaLabel,
}) => (
  <DropdownPanel
    variant="pill"
    options={options}
    value={value}
    onChange={onChange}
    ariaLabel={ariaLabel}
    className={className}
    triggerClassName="w-full py-2.5 text-base font-medium"
  />
);
