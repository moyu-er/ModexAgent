// RenameDialog — the one shared rename interaction (PA-02 DESIGN §2.6).
//
// Pure presentational dialog: the caller owns the open state and the save
// path (useSessions.renameSession). Untitled sessions open with an empty
// input (sessionId placeholder). IME-safe Enter (isComposing / keyCode 229),
// Esc cancels, failures preserve the input and surface an error so the user
// can retry, and background title prop changes never rewrite a field that
// is already being edited (re-seeds only when a different session opens).

import { useRef, useState, type FC, type KeyboardEvent } from "react";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { useModalFocus } from "../hooks/useModalFocus";
import { useT } from "../i18n";

export interface RenameDialogProps {
  /** Session being renamed — full id shows as the untitled placeholder. */
  sessionId: string;
  /** Current persisted title (undefined / blank = untitled). */
  title: string | undefined;
  /** Persist the new title. Reject on failure so the dialog keeps the text. */
  onSave: (title: string) => Promise<void>;
  onCancel: () => void;
}

export const RenameDialog: FC<RenameDialogProps> = ({
  sessionId,
  title,
  onSave,
  onCancel,
}) => {
  const t = useT();
  const [value, setValue] = useState(() => title ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Seed once per target session. Later title prop changes (a refresh landing
  // mid-edit) deliberately do NOT clobber the user's in-progress edit — the
  // dialog is unmounted between targets, so mounting IS the seed point.
  const seededSessionRef = useRef(sessionId);
  if (seededSessionRef.current !== sessionId) {
    seededSessionRef.current = sessionId;
    setValue(title ?? "");
    setError(null);
  }

  useModalFocus({ dialogRef, onClose: onCancel, initialFocusRef: inputRef });

  const trimmed = value.trim();
  const canSave = trimmed.length > 0 && !submitting;

  const submit = (): void => {
    if (!canSave) return;
    const next = trimmed;
    setSubmitting(true);
    setError(null);
    onSave(next)
      .then(() => {
        setSubmitting(false);
      })
      .catch(() => {
        // Preserve the input verbatim — the user can correct and retry.
        setSubmitting(false);
        setError(t("sessions.renameError"));
      });
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>): void => {
    // IME guard: Enter confirming a composition (CJK input) must not save.
    // isComposing is the standard flag; keyCode 229 covers legacy engines.
    if (e.key === "Enter" && (e.nativeEvent.isComposing || e.keyCode === 229)) {
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t("sessions.renameTitle")}
      className="modal-scrim-enter fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !submitting) onCancel();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape" && submitting) e.stopPropagation();
      }}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="modal-panel-enter w-full max-w-sm rounded-lg border border-hairline bg-canvas-popover p-4 shadow-popover focus:outline-none"
      >
        <h3 className="text-base font-semibold text-ink">
          {t("sessions.renameTitle")}
        </h3>
        <div className="mt-3">
          <Input
            ref={inputRef}
            value={value}
            onChange={(e): void => {
              setValue(e.target.value);
              if (error) setError(null);
            }}
            onKeyDown={handleKeyDown}
            placeholder={sessionId}
            disabled={submitting}
            error={error ?? undefined}
            data-testid="rename-input"
          />
        </div>
        <p className="mt-1.5 text-xs text-faint">
          {t("sessions.renameHelper")}
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={onCancel}
            disabled={submitting}
          >
            {t("sessions.renameCancel")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={!canSave}
            loading={submitting}
          >
            {t("sessions.renameSave")}
          </Button>
        </div>
      </div>
    </div>
  );
};
