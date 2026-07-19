// Presentational toast (DESIGN.md §5.4). Single responsibility: render one
// toast. All timing, stacking, and dismissal logic lives in ToastContext.
// Popover surface + hairline; severity is carried by the dot (brand=info/
// success, ember=warning, danger=error) — never by a full-bleed color wash.

import type { ReactNode } from "react";
import { useT } from "../i18n";

export type ToastTone = "info" | "success" | "warning" | "error";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastProps {
  message: string;
  tone?: ToastTone;
  action?: ToastAction;
  onDismiss: () => void;
}

const TONE_DOT: Record<ToastTone, string> = {
  info: "bg-brand",
  success: "bg-success",
  warning: "bg-warning",
  error: "bg-danger",
};

export function Toast({ message, tone = "info", action, onDismiss }: ToastProps) {
  const t = useT();
  return (
    <div
      role="status"
      aria-live="polite"
      className="toast-enter pointer-events-auto flex w-80 items-start gap-2.5 rounded-md border border-hairline bg-canvas-popover px-3.5 py-3 shadow-popover"
    >
      <span
        aria-hidden="true"
        className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${TONE_DOT[tone]}`}
      />
      <p className="flex-1 break-words text-base text-ink">{message}</p>
      {action ? (
        <button
          type="button"
          className="shrink-0 text-xs font-medium text-brand hover:underline"
          onClick={() => {
            action.onClick();
            onDismiss();
          }}
        >
          {action.label}
        </button>
      ) : null}
      <button
        type="button"
        aria-label={t("toast.dismiss")}
        className="shrink-0 text-mute hover:text-ink"
        onClick={onDismiss}
      >
        <DismissIcon />
      </button>
    </div>
  );
}

function DismissIcon(): ReactNode {
  return (
    <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M4 4l8 8M12 4l-8 8"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}
