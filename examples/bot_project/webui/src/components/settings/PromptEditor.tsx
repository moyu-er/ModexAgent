// System-prompt editor for a single prompt. Loads the markdown body via
// getPrompt(promptName) from the global prompts API, edits in a monospace
// textarea, and saves via the caller-provided onSave callback.
//
// Save is DEFERRED (explicit Save button) — the prompt is plain text and
// edits shouldn't fire one REST call per keystroke.
//
// A successful save implies a restart, so we surface the uniform restart toast
// (with "Restart now" action) and arm the persistent indicator.
//
// Discarding unsaved edits uses the shared ConfirmDialog (no window.confirm).
// The optional `onClose` / `slideOverHeader` slots support a slide-over host;
// when absent (the PromptsView case), Discard resets the draft in place.

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { PromptContent } from "../../types/pool";
import { getPrompt } from "../../lib/promptsApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { ConfirmDialog } from "./ConfirmDialog";
import { Button } from "../ui/Button";
import { Textarea } from "../ui/Textarea";
import { HelperText } from "../ui/HelperText";
import { useT } from "../../i18n";

interface Props {
  promptName: string;
  onClose?: () => void;
  /** When provided, the Save button is shown and persistence is delegated here.
   * When absent, the editor is read-only (no Save button, textarea disabled). */
  onSave?: (content: string) => Promise<void>;
  /** Optional header rendered at the top (used by the slide-over variant). */
  slideOverHeader?: ReactNode;
}

export function PromptEditor({ promptName, onClose, onSave, slideOverHeader }: Props) {
  const toast = useToast();
  const t = useT();
  const [original, setOriginal] = useState<string | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [loadError, setLoadError] = useState<string>("");
  const [saving, setSaving] = useState<boolean>(false);
  const [confirmDiscard, setConfirmDiscard] = useState<boolean>(false);

  const readOnly = onSave === undefined;

  useEffect(() => {
    let cancelled = false;
    getPrompt(promptName)
      .then((c: PromptContent) => {
        if (cancelled) return;
        setOriginal(c.content);
        setDraft(c.content);
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [promptName]);

  if (loadError) {
    return (
      <div className="space-y-3 p-4">
        <p className="text-base text-error">{t("settings.promptEditor.failedToLoad", { error: loadError })}</p>
        {onClose && (
          <Button variant="link" onClick={onClose}>
            {t("settings.promptEditor.back")}
          </Button>
        )}
      </div>
    );
  }

  if (original === null) {
    return <p className="p-4 text-base text-mute">{t("settings.promptEditor.loading")}</p>;
  }

  const dirty = draft !== original;
  const requestClose = (): void => {
    if (dirty) setConfirmDiscard(true);
    else onClose?.();
  };

  const doSave = async (): Promise<void> => {
    if (!onSave) return;
    setSaving(true);
    try {
      await onSave(draft);
      setOriginal(draft);
      // Prompt writes unconditionally mark the pool dirty (no hot-reload path
      // yet), so the restart toast fires unconditionally.
      restartToast(toast, t);
    } catch (e) {
      toast.show({
        message: t("settings.promptEditor.saveFailed", { detail: e instanceof ApiError ? `${e.status} ${e.detail}` : String(e) }),
        tone: "warning",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      {slideOverHeader}

      <div className="space-y-3 px-4 py-4">
        {!slideOverHeader ? (
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-base font-semibold text-ink">
                {t("settings.promptEditor.systemPromptAgent", { agent: promptName })}
              </h2>
              <HelperText>
                {t("settings.promptEditor.basePromptHelper")}
              </HelperText>
            </div>
            {onClose && (
              <Button variant="link" onClick={requestClose}>
                {t("settings.promptEditor.back")}
              </Button>
            )}
          </div>
        ) : (
          <div>
            <h3 className="text-base font-medium text-ink">
              {t("settings.promptEditor.agentLabel", { agent: promptName })}
            </h3>
            <HelperText>
              {t("settings.promptEditor.basePromptHelper")}
            </HelperText>
          </div>
        )}

        <Textarea
          aria-label={t("settings.promptEditor.promptBody")}
          mono
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
          style={{ minHeight: "420px" }}
          className="text-base"
          disabled={readOnly}
        />
      </div>

      {!readOnly && (
        <div className="sticky bottom-0 z-10 mt-auto flex justify-end gap-2 border-t border-hairline bg-canvas-elevated px-4 pb-3 pt-3">
          <Button
            variant="secondary"
            onClick={requestClose}
          >
            {t("settings.promptEditor.cancel")}
          </Button>
          <Button
            variant="primary"
            onClick={() => void doSave()}
            disabled={!dirty || saving}
            loading={saving}
          >
            {saving ? t("settings.promptEditor.saving") : t("settings.promptEditor.save")}
          </Button>
        </div>
      )}

      {confirmDiscard ? (
        <ConfirmDialog
          title={t("settings.promptEditor.discardTitle")}
          message={t("settings.promptEditor.discardMessage")}
          confirmLabel={t("settings.promptEditor.discard")}
          tone="danger"
          onConfirm={() => {
            setConfirmDiscard(false);
            setDraft(original);
            onClose?.();
          }}
          onCancel={() => setConfirmDiscard(false)}
        />
      ) : null}
    </div>
  );
}
