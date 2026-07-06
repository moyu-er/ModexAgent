// HelperText.tsx — neutral helper copy under a form control.
// Kept tiny and dependency-free so Input/Textarea/Select/Checkbox can share it.

import type { ReactNode } from "react";

export interface HelperTextProps {
  children: ReactNode;
  className?: string;
  id?: string;
}

export function HelperText({ children, className, id }: HelperTextProps) {
  const cls = ["mt-1 text-xs text-mute", className]
    .filter(Boolean)
    .join(" ");
  return (
    <p id={id} className={cls}>
      {children}
    </p>
  );
}