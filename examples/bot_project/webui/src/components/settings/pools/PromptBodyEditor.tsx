// PromptBodyEditor.tsx — direct prompt-body editing inside the friendly
// agent form (PA-12). Reads/writes the referenced prompt THROUGH the
// original PromptStore REST owner (immediate save on explicit Save; NOT
// part of the scope draft). Shows the shared-reference impact notice; a
// failed save keeps the edited text.

import { useEffect, useState } from "react";
import type { FC } from "react";
import { getPrompt, savePrompt } from "../../../lib/promptsApi";
import { ApiError } from "../../../lib/api";
import { useToast } from "../../ToastContext";
import { restartToast } from "../restartToast";
import { Button } from "../../ui/Button";
import { HelperText } from "../../ui/HelperText";
import { Textarea } from "../../ui/Textarea";
import { useT } from "../../../i18n";
import { useRegisterSettingsEditor } from "../editorNavigation";

interface Props {
  promptName: string;
  /** Identity gate: hide the editor until the agent's identity is saved. */
  identityPersisted?: boolean;
}

export const PromptBodyEditor: FC<Props> = ({ promptName, identityPersisted = true }) => {
  const toast = useToast();
  const t = useT();
  const [original, setOriginal] = useState<string | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [loadError, setLoadError] = useState<string>("");
  const [saveError, setSaveError] = useState<string>("");
  const [saving, setSaving] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    setOriginal(null);
    setLoadError("");
    setSaveError("");
    getPrompt(promptName)
      .then((c) => {
        if (cancelled) return;
        setOriginal(c.content);
        setDraft(c.content);
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(String(e));
      });
    return (): void => {
      cancelled = true;
    };
  }, [promptName]);

  const dirty = original !== null && draft !== original;

  const save = async (): Promise<boolean> => {
    if (!identityPersisted || original === null || saving) return false;
    setSaving(true);
    setSaveError("");
    try {
      const saved = await savePrompt(promptName, draft);
      setOriginal(saved.content);
      restartToast(toast, t);
      return true;
    } catch (e) {
      // Keep the edited text — the user can retry or copy it out.
      setSaveError(
        e instanceof ApiError
          ? t("settings.poolsPanel.promptBodySaveFailed", {
              detail: `${e.status} ${e.detail}`,
            })
          : t("settings.poolsPanel.promptBodySaveFailed", { detail: String(e) }),
      );
      return false;
    } finally {
      setSaving(false);
    }
  };

  useRegisterSettingsEditor(() => ({
    isDirty: () => dirty,
    save,
    discard: () => { setDraft(original ?? ""); setSaveError(""); },
  }));
  if (!identityPersisted) return <HelperText>{t("settings.poolsPanel.identitySaveFirst")}</HelperText>;
  if (loadError) return <HelperText>{t("settings.promptEditor.failedToLoad", { error: loadError })}</HelperText>;
  if (original === null) return <HelperText>{t("settings.promptEditor.loading")}</HelperText>;

  return (
    <div className="space-y-2" data-testid="prompt-body-editor">
      <span className="block text-base text-ink">
        {t("settings.poolsPanel.promptBodySection")} — <span className="font-mono text-sm">{promptName}</span>
      </span>
      <HelperText>{t("settings.poolsPanel.promptBodyShared")}</HelperText>
      <Textarea
        mono
        rows={8}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        aria-label={t("settings.poolsPanel.promptBodySection")}
      />
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={!dirty || saving}
          loading={saving}
          onClick={() => void save()}
        >
          {t("common.save")}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={!dirty || saving}
          onClick={() => {
            setDraft(original);
            setSaveError("");
          }}
        >
          {t("common.cancel")}
        </Button>
        <span className="text-xs text-faint">
          {t("settings.poolsPanel.promptBodySaveImmediate")}
        </span>
      </div>
      {saveError ? (
        <p className="text-xs text-danger" role="alert">
          {saveError}
        </p>
      ) : null}
    </div>
  );
};
