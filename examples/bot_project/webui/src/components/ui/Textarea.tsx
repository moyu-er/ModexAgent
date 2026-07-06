// Textarea.tsx — labeled multiline text primitive.
//
// Same label/helper/error chrome as Input. Defaults to `font-mono` (most uses
// in this app are code-ish — system prompts, model names, tool descriptions)
// and an 80px min-height so the control feels like a textarea even when empty.

import type { TextareaHTMLAttributes } from "react";
import { forwardRef, useId } from "react";
import { Label } from "./Label";
import { HelperText } from "./HelperText";
import { FieldError } from "./FieldError";

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: string;
  helper?: string;
  error?: string;
  /** Force monospace font; defaults to true since most callsites pass code-like content. */
  mono?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { label, helper, error, mono = true, className, id, required, rows, ...rest },
  ref,
) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const hasError = Boolean(error);
  const cls = [
    "w-full rounded-xs border px-3 py-2 text-sm",
    "bg-canvas-elevated text-ink placeholder:text-faint",
    hasError
      ? "border-error focus:border-error focus:ring-error/30"
      : "border-hairline focus:border-link focus:ring-link/30",
    "focus:outline-none focus:ring-1",
    "disabled:cursor-not-allowed disabled:opacity-60",
    "min-h-[80px] resize-y",
    mono ? "font-mono" : "",
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
      <textarea
        ref={ref}
        id={inputId}
        className={cls}
        rows={rows ?? 4}
        required={required}
        aria-invalid={hasError || undefined}
        aria-describedby={error ? `${inputId}-error` : helper ? `${inputId}-helper` : undefined}
        {...rest}
      />
      {error ? (
        <FieldError id={`${inputId}-error`}>{error}</FieldError>
      ) : helper ? (
        <HelperText id={`${inputId}-helper`}>{helper}</HelperText>
      ) : null}
    </div>
  );
});
