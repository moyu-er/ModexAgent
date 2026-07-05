// Presentational toast. Single responsibility: render one toast. All timing,
// stacking, and dismissal logic lives in ToastContext.

import type { ReactNode } from "react";

export type ToastTone = "info" | "success" | "warning";

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

const TONE_BORDER: Record<ToastTone, string> = {
  info: "border-ai-brand",
  success: "border-success",
  warning: "border-warning",
};

const TONE_DOT: Record<ToastTone, string> = {
  info: "bg-ai-brand",
  success: "bg-success",
  warning: "bg-warning",
};

export function Toast({ message, tone = "info", action, onDismiss }: ToastProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={`pointer-events-auto flex w-80 items-start gap-2.5 rounded-lg border bg-content-bg px-3 py-2.5 shadow-lg ${TONE_BORDER[tone]}`}
    >
      <span aria-hidden="true" className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${TONE_DOT[tone]}`} />
      <p className="flex-1 break-words text-sm text-text-primary">{message}</p>
      {action ? (
        <button
          type="button"
          className="shrink-0 text-xs font-medium text-ai-brand hover:underline"
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
        aria-label="Dismiss"
        className="shrink-0 text-text-secondary hover:text-text-primary"
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
