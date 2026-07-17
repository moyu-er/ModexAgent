import type { ReactNode } from "react";
import { useT } from "../../i18n";

export interface ActionBarProps {
  children: ReactNode;
  className?: string;
}

export function ActionBar({ children, className = "" }: ActionBarProps) {
  const t = useT();
  return (
    <div
      role="group"
      aria-label={t("ui.formActions")}
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
