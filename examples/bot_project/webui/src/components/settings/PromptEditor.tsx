// System-prompt editor for a single agent (main or subagent). Loads the
// markdown body via getPrompt(), edits in a monospace textarea, and saves with
// savePrompt(). Save is DEFERRED (explicit Save button) — unlike skill toggles,
// the prompt is plain text and edits shouldn't fire one REST call per keystroke.
//
// A successful save implies a restart, so we surface the uniform restart toast
// (with "Restart now" action) and arm the persistent indicator.
//
// Discarding unsaved edits uses the shared ConfirmDialog (no window.confirm).

import { useEffect, useState } from "react";
import type { PromptContent } from "../../types/pool";
import { getPrompt, savePrompt } from "../../lib/poolApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { ConfirmDialog } from "./ConfirmDialog";

interface Props {
  pool: string;
  agent: string;
  onClose: () => void;
}

const TEXTAREA =
  "w-full rounded border border-input-border bg-input-bg px-3 py-2 font-mono text-sm text-text-primary focus:border-input-focus focus:outline-none focus:ring-1 focus:ring-input-focus";

export function PromptEditor({ pool, agent, onClose }: Props) {
  const toast = useToast();
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
      <div>
        <p className="text-sm text-error">Failed to load: {loadError}</p>
        <button
          type="button"
          className="mt-2 text-sm text-ai-brand hover:underline"
          onClick={onClose}
        >
          ← Back
        </button>
      </div>
    );
  }

  if (original === null) {
    return <p className="text-sm text-text-secondary">Loading…</p>;
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
      restartToast(toast);
    } catch (e) {
      toast.show({
        message: `Save failed: ${e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)}`,
        tone: "warning",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-text-primary">
            System prompt — <span className="font-mono">{agent}</span>
          </h2>
          <p className="text-xs text-text-secondary">
            This is the base prompt; the runtime pipeline injects skills/memory
            on top.
          </p>
        </div>
        <button
          type="button"
          className="text-sm text-ai-brand hover:underline"
          onClick={requestClose}
        >
          ← Back
        </button>
      </div>

      <textarea
        className={`${TEXTAREA} min-h-[420px]`}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        spellCheck={false}
      />

      <div className="flex justify-end gap-2 border-t border-divider pt-3">
        <button
          type="button"
          className="rounded border border-divider px-4 py-1.5 text-sm text-text-primary hover:bg-sidebar-hover disabled:opacity-50"
          onClick={requestClose}
        >
          Cancel
        </button>
        <button
          type="button"
          className="rounded bg-btn-primary px-4 py-1.5 text-sm text-btn-primary-text hover:opacity-90 disabled:opacity-50"
          onClick={() => void onSave()}
          disabled={!dirty || saving}
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>

      {confirmDiscard ? (
        <ConfirmDialog
          title="Discard unsaved changes?"
          message="Your edits to this system prompt will be lost."
          confirmLabel="Discard"
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
