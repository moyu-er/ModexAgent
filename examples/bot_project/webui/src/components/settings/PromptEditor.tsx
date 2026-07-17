// System-prompt editor for a single agent (main or subagent). Loads the
// markdown body via getPrompt(), edits in a monospace textarea, and saves with
// savePrompt(). Save is DEFERRED (explicit Save button) — unlike skill toggles,
// the prompt is plain text and edits shouldn't fire one REST call per keystroke.
//
// A successful save implies a restart, so we surface the uniform restart toast
// (with "Restart now" action) and arm the persistent indicator.
//
// Discarding unsaved edits uses the shared ConfirmDialog (no window.confirm).
//
// Rendered as a sibling inside the PoolEditor's slide-over — the pool editor
// stays mounted behind it so unsaved pool edits aren't lost. The optional
// `slideOverHeader` slot lets the caller inject a Close button into the
// slide-over's header strip.

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { PromptContent } from "../../types/pool";
import { getPrompt, savePrompt } from "../../lib/poolApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { ConfirmDialog } from "./ConfirmDialog";
import { Button } from "../ui/Button";
import { Textarea } from "../ui/Textarea";
import { HelperText } from "../ui/HelperText";
import { useT } from "../../i18n";

interface Props {
  pool: string;
  agent: string;
  onClose: () => void;
  /** Optional header rendered at the top (used by the slide-over variant). */
  slideOverHeader?: ReactNode;
}

export function PromptEditor({ pool, agent, onClose, slideOverHeader }: Props) {
  const toast = useToast();
  const t = useT();
  const [original, setOriginal] = useState<string | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [loadError, setLoadError] = useState<string>("");
  const [saving, setSaving] = useState<boolean>(false);
  const [confirmDiscard, setConfirmDiscard] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    getPrompt(pool, agent)
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
  }, [pool, agent]);

  if (loadError) {
    return (
      <div className="space-y-3 p-4">
        <p className="text-sm text-error">{t("settings.promptEditor.failedToLoad", { error: loadError })}</p>
        <Button variant="link" onClick={onClose}>
          {t("settings.promptEditor.back")}
        </Button>
      </div>
    );
  }

  if (original === null) {
    return <p className="p-4 text-sm text-mute">{t("settings.promptEditor.loading")}</p>;
  }

  const dirty = draft !== original;
  const requestClose = (): void => {
    if (dirty) setConfirmDiscard(true);
    else onClose();
  };

  const onSave = async (): Promise<void> => {
    setSaving(true);
    try {
      const saved = await savePrompt(pool, agent, draft);
      setOriginal(saved.content);
      setDraft(saved.content);
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
              <h2 className="text-sm font-semibold text-ink">
                {t("settings.promptEditor.systemPromptAgent", { agent })}
              </h2>
              <HelperText>
                {t("settings.promptEditor.basePromptHelper")}
              </HelperText>
            </div>
            <Button variant="link" onClick={requestClose}>
              {t("settings.promptEditor.back")}
            </Button>
          </div>
        ) : (
          <div>
            <h3 className="text-sm font-medium text-ink">
              {t("settings.promptEditor.agentLabel", { agent })}
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
          className="text-sm"
        />
      </div>

      <div className="sticky bottom-0 z-10 mt-auto flex justify-end gap-2 border-t border-hairline bg-canvas-elevated px-4 pb-3 pt-3">
        <Button
          variant="secondary"
          onClick={requestClose}
        >
          {t("settings.promptEditor.cancel")}
        </Button>
        <Button
          variant="primary"
          onClick={() => void onSave()}
          disabled={!dirty || saving}
          loading={saving}
        >
          {saving ? t("settings.promptEditor.saving") : t("settings.promptEditor.save")}
        </Button>
      </div>

      {confirmDiscard ? (
        <ConfirmDialog
          title={t("settings.promptEditor.discardTitle")}
          message={t("settings.promptEditor.discardMessage")}
          confirmLabel={t("settings.promptEditor.discard")}
          tone="danger"
          onConfirm={() => {
            setConfirmDiscard(false);
            onClose();
          }}
          onCancel={() => setConfirmDiscard(false)}
        />
      ) : null}
    </div>
  );
}