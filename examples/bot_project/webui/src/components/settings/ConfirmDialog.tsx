// Small single-responsibility confirm dialog. Replaces window.confirm across the
// settings views (pool/subagent delete, discard-unsaved, prompt-discard) so the
// UI stays consistent and is testable via getByRole("dialog"). Pure
// presentational — the caller owns the open state and the onConfirm/onCancel
// callbacks.

import type { ReactNode } from "react";

export interface ConfirmDialogProps {
  /** Dialog title / heading. */
  title: string;
  /** Body text shown under the title. */
  message?: ReactNode;
  /** Confirm button label (e.g. "Delete", "Discard"). */
  confirmLabel?: string;
  /** Cancel button label. */
  cancelLabel?: string;
  /** Tone of the confirm button. "danger" renders red text/border. */
  tone?: "default" | "danger";
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "default",
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const confirmCls =
    tone === "danger"
      ? "border-error text-error hover:bg-sidebar-hover"
      : "border-ai-brand text-ai-brand hover:bg-sidebar-hover";
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4"
      onClick={(e) => {
        // Click on the backdrop (not its children) cancels.
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="w-full max-w-sm rounded-lg border border-card-border bg-content-bg p-4 shadow-lg">
        <h3 className="text-sm font-semibold text-text-primary">{title}</h3>
        {message ? (
          <div className="mt-2 text-xs text-text-secondary">{message}</div>
        ) : null}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            className="rounded border border-divider px-3 py-1.5 text-xs text-text-primary hover:bg-sidebar-hover"
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`rounded border px-3 py-1.5 text-xs font-medium ${confirmCls}`}
            onClick={onConfirm}
            autoFocus
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
