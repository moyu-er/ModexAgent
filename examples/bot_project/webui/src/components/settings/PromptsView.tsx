// Prompts tab — list every agents/*.md and edit one in a prompt editor pane.
//
// Two-pane layout: list on the left (selectable + "New prompt" button),
// PromptEditor on the right (editable — Save calls PUT /api/prompts/{name}).
// The "New prompt" button opens a name-entry dialog; submit calls
// POST /api/prompts and selects the newly created prompt. A duplicate name
// (HTTP 409) is surfaced inline in the dialog; other create failures surface
// as a toast. Both Save and Create set restart_required on the backend, so
// the restart toast fires on each successful path.

import { useEffect, useState } from "react";
import type { PromptSummary, PromptUsage } from "../../types/pool";
import { createPrompt, deletePrompt, listPrompts, savePrompt, PromptInUseError } from "../../lib/promptsApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { IconButton } from "../ui/IconButton";
import { ConfirmDialog } from "./ConfirmDialog";
import { CATEGORY } from "./categoryMeta";
import { PromptEditor } from "./PromptEditor";
import { useT } from "../../i18n";
import { Trash2 } from "lucide-react";

const NAME_RE = /^[a-z][a-z0-9_-]+$/;

export function PromptsView() {
  const toast = useToast();
  const t = useT();
  const [prompts, setPrompts] = useState<PromptSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState<boolean>(false);
  const [newName, setNewName] = useState<string>("");
  const [createError, setCreateError] = useState<string>("");
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<boolean>(false);
  const [inUseUsages, setInUseUsages] = useState<PromptUsage[] | null>(null);
  const [inUseName, setInUseName] = useState<string>("");

  const load = async (): Promise<void> => {
    setLoadError("");
    try {
      setPrompts(await listPrompts());
    } catch (e) {
      setLoadError(String(e));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const onSave = async (content: string): Promise<void> => {
    if (!selected) return;
    await savePrompt(selected, content);
    void load();
  };

  const openCreate = (): void => {
    setNewName("");
    setCreateError("");
    setCreating(true);
  };

  const cancelCreate = (): void => {
    setCreating(false);
    setNewName("");
    setCreateError("");
  };

  const confirmCreate = async (): Promise<void> => {
    const name = newName.trim();
    if (!NAME_RE.test(name)) {
      setCreateError(t("settings.prompts.invalidName"));
      return;
    }
    setSubmitting(true);
    try {
      await createPrompt(name);
      await load();
      setSelected(name);
      setCreating(false);
      setNewName("");
      setCreateError("");
      restartToast(toast, t);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setCreateError(t("settings.prompts.duplicateName", { name }));
      } else {
        toast.show({
          message: t("settings.prompts.createFailed", {
            detail: e instanceof ApiError ? `${e.status} ${e.detail}` : String(e),
          }),
          tone: "warning",
        });
      }
    } finally {
      setSubmitting(false);
    }
  };

  const onDeleteConfirmed = async (name: string): Promise<void> => {
    setDeleting(true);
    try {
      await deletePrompt(name);
      setPrompts((prev) => (prev ?? []).filter((p) => p.name !== name));
      if (selected === name) {
        setSelected(null);
      }
      toast.show({ message: t("settings.prompts.deleted", { name }), tone: "success" });
    } catch (e) {
      if (e instanceof PromptInUseError) {
        setInUseUsages(e.usages);
        setInUseName(name);
      } else {
        toast.show({
          message: t("settings.prompts.deleteFailed", {
            detail: e instanceof ApiError ? `${e.status} ${e.detail}` : String(e),
          }),
          tone: "warning",
        });
      }
    } finally {
      setDeleting(false);
      setPendingDelete(null);
    }
  };

  if (loadError) {
    return <p className="text-base text-error">{t("common.failedToLoad", { error: loadError })}</p>;
  }
  if (!prompts) {
    return <p className="text-base text-mute">{t("common.loading")}</p>;
  }

  const meta = CATEGORY.prompts;
  const PageHeadIcon = meta.icon;

  return (
    <div className="space-y-4">
      <div className="page-head">
        <span
          className="page-head-icon"
          style={{ ["--cat" as string]: meta.catVar }}
        >
          <PageHeadIcon size={18} />
        </span>
        <div className="flex-1">
          <div className="page-title">{t(meta.titleKey!)}</div>
          <div className="page-sub">{t(meta.subKey)}</div>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={openCreate}
          disabled={creating}
        >
          {t("settings.prompts.newPrompt")}
        </Button>
      </div>

      {prompts.length === 0 ? (
        <div className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-base text-mute">
          {t("settings.prompts.noPrompts")}
        </div>
      ) : (
        <div className="flex gap-4">
          <Card className="w-64 shrink-0">
            <div className="space-y-1 p-1">
              {prompts.map((p) => {
                const isSelected = selected === p.name;
                return (
                  <div
                    key={p.name}
                    className={[
                      "flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 transition-colors",
                      isSelected
                        ? "border-hairline bg-hairline-soft"
                        : "border-transparent hover:bg-hairline-soft",
                    ].join(" ")}
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelected(isSelected ? null : p.name)}
                    onKeyDown={(e): void => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelected(isSelected ? null : p.name);
                      }
                    }}
                  >
                    <span className="flex-1 truncate font-mono text-base font-medium text-ink">
                      {p.name}
                    </span>
                    <IconButton
                      icon={<Trash2 size={14} />}
                      label={t("settings.prompts.deletePrompt", { name: p.name })}
                      variant="danger"
                      size="sm"
                      disabled={deleting}
                      onClick={(e) => {
                        e.stopPropagation();
                        setPendingDelete(p.name);
                      }}
                    />
                  </div>
                );
              })}
            </div>
          </Card>

          <div className="min-w-0 flex-1">
            {selected ? (
              <Card className="h-full">
                <PromptEditor promptName={selected} onSave={onSave} />
              </Card>
            ) : (
              <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-base text-mute">
                {t("settings.prompts.selectToView")}
              </p>
            )}
          </div>
        </div>
      )}

      {creating ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={t("settings.prompts.newPrompt")}
          className="fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4"
          onClick={(e) => {
            if (e.target === e.currentTarget) cancelCreate();
          }}
        >
          <Card elevated className="w-full max-w-sm p-4">
            <h3 className="text-base font-semibold text-ink">
              {t("settings.prompts.newPrompt")}
            </h3>
            <div className="mt-3">
              <Input
                label={t("settings.prompts.newNameLabel")}
                placeholder={t("settings.prompts.newNamePlaceholder")}
                helper={t("settings.prompts.newNameHelper")}
                error={createError || undefined}
                value={newName}
                autoFocus
                disabled={submitting}
                onChange={(e) => {
                  setNewName(e.target.value);
                  if (createError) setCreateError("");
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void confirmCreate();
                  }
                  if (e.key === "Escape") {
                    e.stopPropagation();
                    cancelCreate();
                  }
                }}
              />
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="secondary"
                size="sm"
                onClick={cancelCreate}
                disabled={submitting}
              >
                {t("settings.prompts.cancel")}
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={() => void confirmCreate()}
                loading={submitting}
              >
                {submitting
                  ? t("settings.prompts.creating")
                  : t("settings.prompts.create")}
              </Button>
            </div>
          </Card>
        </div>
      ) : null}

      {pendingDelete ? (
        <ConfirmDialog
          title={t("settings.prompts.deleteTitle", { name: pendingDelete })}
          message={t("settings.prompts.deleteMessage")}
          confirmLabel={t("settings.prompts.delete")}
          tone="danger"
          onConfirm={() => void onDeleteConfirmed(pendingDelete)}
          onCancel={() => setPendingDelete(null)}
        />
      ) : null}

      {inUseUsages ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={t("settings.prompts.inUseTitle", { name: inUseName })}
          className="fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4"
          onClick={(e) => {
            if (e.target === e.currentTarget) {
              setInUseUsages(null);
              setInUseName("");
            }
          }}
        >
          <Card elevated className="w-full max-w-lg p-4">
            <h3 className="text-base font-semibold text-ink">
              {t("settings.prompts.inUseTitle", { name: inUseName })}
            </h3>
            <p className="mt-2 text-xs text-body">
              {t("settings.prompts.inUseMessage")}
            </p>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-hairline text-mute">
                    <th className="pb-1 pr-3 font-medium">
                      {t("settings.prompts.inUsePoolHeader")}
                    </th>
                    <th className="pb-1 pr-3 font-medium">
                      {t("settings.prompts.inUseKindHeader")}
                    </th>
                    <th className="pb-1 font-medium">
                      {t("settings.prompts.inUseAgentHeader")}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {inUseUsages.map((u, i) => (
                    <tr key={`${u.pool}-${u.agent_kind}-${u.agent_name}-${i}`} className="border-b border-hairline">
                      <td className="py-1.5 pr-3 font-mono text-ink">{u.pool}</td>
                      <td className="py-1.5 pr-3 text-body">{u.agent_kind}</td>
                      <td className="py-1.5 font-mono text-ink">{u.agent_name}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-4 flex justify-end">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setInUseUsages(null);
                  setInUseName("");
                }}
                autoFocus
              >
                {t("settings.prompts.inUseClose")}
              </Button>
            </div>
          </Card>
        </div>
      ) : null}
    </div>
  );
}
