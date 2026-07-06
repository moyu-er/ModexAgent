// SelectPrimitive.tsx — lightweight styled <select> for non-form inline use.
//
// Used by the Sidebar pool picker: same surface treatment as the form Select
// but with larger text/padding to match the sidebar's visual weight.

import type { SelectHTMLAttributes } from "react";
import { forwardRef } from "react";
import { ChevronDownIcon } from "./icons";

export interface SelectPrimitiveOption {
  value: string;
  label: string;
}

export interface SelectPrimitiveProps
  extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "size"> {
  options: SelectPrimitiveOption[];
}

export const SelectPrimitive = forwardRef<
  HTMLSelectElement,
  SelectPrimitiveProps
>(function SelectPrimitive({ options, className = "", ...rest }, ref) {
  return (
    <div className="relative">
      <select
        ref={ref}
        className={[
          "w-full cursor-pointer appearance-none rounded-sm border border-hairline bg-canvas-elevated",
          "py-3 pl-7 pr-10 text-base font-semibold text-ink",
          "focus:border-link focus:outline-none focus:ring-1 focus:ring-link/30",
          "hover:bg-hairline-soft",
          className,
        ].join(" ")}
        {...rest}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <span className="pointer-events-none absolute left-2 top-1/2 h-2.5 w-2.5 -translate-y-1/2 rounded-full bg-link" />
      <ChevronDownIcon className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-mute" />
    </div>
  );
});
