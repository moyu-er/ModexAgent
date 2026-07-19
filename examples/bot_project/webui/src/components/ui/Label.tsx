// Label.tsx — small presentational <label> used above form controls.
// 13px medium per DESIGN.md §5.2. Renders the red `*` only when `required`
// is set so callers can toggle it without forking the component. Pairs with
// Input/Textarea/Select/Checkbox.

import type { ReactNode } from "react";

export interface LabelProps {
  children: ReactNode;
  required?: boolean;
  className?: string;
  htmlFor?: string;
}

export function Label({ children, required = false, className, htmlFor }: LabelProps) {
  const cls = ["text-base font-medium text-body mb-1 block", className]
    .filter(Boolean)
    .join(" ");
  return (
    <label className={cls} htmlFor={htmlFor}>
      {children}
      {required ? <span className="ml-0.5 text-danger">*</span> : null}
    </label>
  );
}