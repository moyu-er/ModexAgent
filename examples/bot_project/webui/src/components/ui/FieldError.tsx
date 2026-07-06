// FieldError.tsx — error copy under a form control.
// Renders with `role="alert"` so screen readers announce it as soon as it
// mounts. Use it instead of HelperText when a field fails validation.

import type { ReactNode } from "react";

export interface FieldErrorProps {
  children: ReactNode;
  className?: string;
  id?: string;
}

export function FieldError({ children, className, id }: FieldErrorProps) {
  const cls = ["mt-1 text-xs text-error", className]
    .filter(Boolean)
    .join(" ");
  return (
    <p id={id} className={cls} role="alert">
      {children}
    </p>
  );
}