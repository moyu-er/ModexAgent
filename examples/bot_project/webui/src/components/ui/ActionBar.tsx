import type { ReactNode } from "react";
import { useT } from "../../i18n";

export interface ActionBarProps {
  children: ReactNode;
  className?: string;
  /**
   * When true, an ember dot is rendered at the leading edge of the bar to
   * signal unsaved changes (DESIGN.md §8). Wired to the parent view's dirty
   * state — no new state invented here.
   */
  dirty?: boolean;
}

export function ActionBar({ children, className = "", dirty = false }: ActionBarProps) {
  const t = useT();
  return (
    <div
      role="group"
      aria-label={t("ui.formActions")}
      className={["action-bar", className].join(" ").trim()}
    >
      {dirty && (
        <span
          className="unsaved-dot"
          role="status"
          aria-label={t("ui.unsavedChanges")}
          title={t("ui.unsavedChanges")}
        />
      )}
      <span className="flex items-center gap-2">{children}</span>
    </div>
  );
}
