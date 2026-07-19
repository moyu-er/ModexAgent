// Checkbox.tsx — labeled checkbox primitive.
//
// Renders a native <input type="checkbox"> restyled with the `.checkbox-custom`
// class (index.css): xs (6px) corners, hairline border, brand fill with a
// check-draw animation when checked (DESIGN.md §5.4). The label wraps the
// visible text so clicking anywhere on the label toggles the box.

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
    "checkbox-custom",
    hasError ? "border-danger" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="block">
      <label
        htmlFor={inputId}
        className="inline-flex cursor-pointer items-center gap-2 text-base text-ink"
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
          {required ? <span className="ml-0.5 text-danger">*</span> : null}
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
