// Select.tsx — labeled <select> primitive.
//
// Same label/helper/error chrome as Input. Renders a native select with the
// same focus/error treatment. Chevron is rendered automatically as a trailing
// SVG.

import type { SelectHTMLAttributes } from "react";
import { forwardRef, useId } from "react";
import { Label } from "./Label";
import { HelperText } from "./HelperText";
import { FieldError } from "./FieldError";

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "size"> {
  label?: string;
  helper?: string;
  error?: string;
  options: SelectOption[];
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { label, helper, error, options, className, id, required, ...rest },
  ref,
) {
  const generatedId = useId();
  const selectId = id ?? generatedId;
  const hasError = Boolean(error);
  const selectCls = [
    "w-full appearance-none rounded-xs border px-3 py-2 pr-8 text-sm",
    "bg-canvas-elevated text-ink",
    hasError
      ? "border-error focus:border-error focus:ring-error/30"
      : "border-hairline focus:border-link focus:ring-link/30",
    "focus:outline-none focus:ring-1",
    "disabled:cursor-not-allowed disabled:opacity-60",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="block">
      {label ? (
        <Label htmlFor={selectId} required={required}>
          {label}
        </Label>
      ) : null}
      <div className="relative">
        <select
          ref={ref}
          id={selectId}
          className={selectCls}
          required={required}
          aria-invalid={hasError || undefined}
          aria-describedby={error ? `${selectId}-error` : helper ? `${selectId}-helper` : undefined}
          {...rest}
        >
          {options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <svg
          className="pointer-events-none absolute right-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-mute"
          viewBox="0 0 16 16"
          fill="none"
          aria-hidden="true"
        >
          <path
            d="M4 6l4 4 4-4"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      {error ? (
        <FieldError id={`${selectId}-error`}>{error}</FieldError>
      ) : helper ? (
        <HelperText id={`${selectId}-helper`}>{helper}</HelperText>
      ) : null}
    </div>
  );
});
