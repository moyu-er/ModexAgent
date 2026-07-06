// Checkbox.tsx — labeled checkbox primitive.
//
// Renders a native <input type="checkbox"> paired with a <label> wrapping
// the visible text, so clicking anywhere on the label toggles the box. The
// checkbox square uses Geist canvas/hairline/link tokens and a link focus ring.

import type { InputHTMLAttributes } from "react";
import { forwardRef, useId } from "react";
import { HelperText } from "./HelperText";
import { FieldError } from "./FieldError";

export interface CheckboxProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size"> {
  label?: string;
  helper?: string;
  error?: string;
}

export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { label, helper, error, className, id, required, checked, defaultChecked, ...rest },
  ref,
) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const hasError = Boolean(error);
  const boxCls = [
    "h-4 w-4 shrink-0 rounded-sm border",
    hasError ? "border-error" : "border-hairline",
    "bg-canvas-elevated text-link",
    "focus:outline-none focus:ring-1 focus:ring-link/30",
    "disabled:cursor-not-allowed disabled:opacity-60",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="block">
      <label
        htmlFor={inputId}
        className="inline-flex cursor-pointer items-center gap-2 text-sm text-ink"
      >
        <input
          ref={ref}
          id={inputId}
          type="checkbox"
          className={boxCls}
          checked={checked}
          defaultChecked={defaultChecked}
          required={required}
          aria-invalid={hasError || undefined}
          aria-describedby={
            error ? `${inputId}-error` : helper ? `${inputId}-helper` : undefined
          }
          {...rest}
        />
        <span>
          {label}
          {required ? <span className="ml-0.5 text-error">*</span> : null}
        </span>
      </label>
      {error ? (
        <FieldError id={`${inputId}-error`}>{error}</FieldError>
      ) : helper ? (
        <HelperText id={`${inputId}-helper`}>{helper}</HelperText>
      ) : null}
    </div>
  );
});
