import type { ReactNode } from "react";

export interface ActionBarProps {
  children: ReactNode;
  className?: string;
}

export function ActionBar({ children, className = "" }: ActionBarProps) {
  return (
    <div
      role="group"
      aria-label="Form actions"
      className={[
        "sticky bottom-0 z-20 flex items-center justify-end gap-2",
        "border-t border-hairline bg-canvas px-6 py-3",
        className,
      ].join(" ")}
    >
      {children}
    </div>
  );
}
