// Small single-responsibility confirm dialog. Replaces window.confirm across the
// settings views (pool/subagent delete, discard-unsaved, prompt-discard) so the
// UI stays consistent and is testable via getByRole("dialog"). Pure
// presentational — the caller owns the open state and the onConfirm/onCancel
// callbacks.

import { useEffect } from "react";
import type { ReactNode } from "react";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";

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
  // Escape cancels — standard dialog keyboard contract.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const confirmVariant = tone === "danger" ? "danger" : "primary";

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
      <Card elevated className="w-full max-w-sm p-4">
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {message ? (
          <div className="mt-2 text-xs text-body">{message}</div>
        ) : null}
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={onCancel} autoFocus>
            {cancelLabel}
          </Button>
          <Button variant={confirmVariant} size="sm" onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </Card>
    </div>
  );
}