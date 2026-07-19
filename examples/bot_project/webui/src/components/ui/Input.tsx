// Input.tsx — labeled text input primitive.
//
// Wraps a native <input> with optional label/helper/error chrome and left/right
// icon slots. The underlying input receives a generated `id` (or a caller-provided
// one) so the surrounding <label> can wire `htmlFor`. Forwarded ref points at the
// <input> so form libraries can grab the DOM node directly.

import type { InputHTMLAttributes, ReactNode } from "react";
import { forwardRef, useId } from "react";
import { Label } from "./Label";
import { HelperText } from "./HelperText";
import { FieldError } from "./FieldError";

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  label?: string;
  helper?: string;
  error?: string;
  /** Optional leading glyph (typically SearchIcon etc.). Reserved space on the left. */
  iconLeft?: ReactNode;
  /** Optional trailing glyph. Reserved space on the right. */
  iconRight?: ReactNode;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, helper, error, iconLeft, iconRight, className, id, required, ...rest },
  ref,
) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const hasError = Boolean(error);
  const hasIcon = Boolean(iconLeft || iconRight);
  const inputCls = [
    "h-9 w-full rounded-sm border px-3 text-base",
    "bg-canvas-elevated text-ink placeholder:text-faint",
    hasError
      ? "border-danger focus:border-danger focus:ring-danger"
      : "border-hairline focus:border-brand focus:ring-brand",
    "focus:outline-none focus:ring-2",
    "disabled:cursor-not-allowed disabled:opacity-45",
    iconLeft ? "pl-8" : "",
    iconRight ? "pr-8" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="block">
      {label ? (
        <Label htmlFor={inputId} required={required}>
          {label}
        </Label>
      ) : null}
      <div className={hasIcon ? "relative" : undefined}>
        {iconLeft ? (
          <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-mute">
            {iconLeft}
          </span>
        ) : null}
        <input
          ref={ref}
          id={inputId}
          className={inputCls}
          required={required}
          aria-invalid={hasError || undefined}
          aria-describedby={
            error
              ? `${inputId}-error`
              : helper
                ? `${inputId}-helper`
                : undefined
          }
          {...rest}
        />
        {iconRight ? (
          <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-mute">
            {iconRight}
          </span>
        ) : null}
      </div>
      {error ? (
        <FieldError id={`${inputId}-error`}>{error}</FieldError>
      ) : helper ? (
        <HelperText id={`${inputId}-helper`}>{helper}</HelperText>
      ) : null}
    </div>
  );
});
