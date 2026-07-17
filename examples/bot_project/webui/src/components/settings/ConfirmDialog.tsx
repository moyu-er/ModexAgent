// Small single-responsibility confirm dialog. Replaces window.confirm across the
// settings views (pool/subagent delete, discard-unsaved, prompt-discard) so the
// UI stays consistent and is testable via getByRole("dialog"). Pure
// presentational — the caller owns the open state and the onConfirm/onCancel
// callbacks.

import { useEffect } from "react";
import type { ReactNode } from "react";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { useT } from "../../i18n";

export interface ConfirmDialogProps {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel,
  cancelLabel,
  tone = "default",
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const t = useT();
  const confirm = confirmLabel ?? t("settings.confirmDialog.confirm");
  const cancel = cancelLabel ?? t("settings.confirmDialog.cancel");
  const confirmVariant = tone === "danger" ? "danger" : "primary";

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
            {cancel}
          </Button>
          <Button variant={confirmVariant} size="sm" onClick={onConfirm}>
            {confirm}
          </Button>
        </div>
      </Card>
    </div>
  );
}