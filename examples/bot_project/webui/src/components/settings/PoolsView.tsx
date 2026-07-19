// PoolsView — left/right layout: pool list + PoolEditor.
//
// Left list: listPools() summary; + Add pool (inline input, NOT
// window.prompt); per-pool delete (delete uses the shared ConfirmDialog — NOT
// window.confirm). Deleting the default pool returns 409 from the backend →
// surface as a toast.
//
// Switching to another pool while the current PoolEditor is dirty is guarded
// by a ConfirmDialog at this level (PoolEditor itself never sees the switch).

import { useEffect, useMemo, useRef, useState } from "react";
import type { PoolSummary } from "../../types/pool";
import {
  createPool,
  deletePool,
  listPools,
} from "../../lib/poolApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { PoolEditor } from "./PoolEditor";
import { ConfirmDialog } from "./ConfirmDialog";
import { ActionBar } from "../ui/ActionBar";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { Input } from "../ui/Input";
import {
  PlusIcon,
  SearchIcon,
} from "../ui/icons";
import { Trash2 } from "lucide-react";
import { CATEGORY } from "./categoryMeta";
import { useT } from "../../i18n";

type Confirm =
  | { kind: "delete"; name: string }
  | { kind: "switch"; name: string }
  | null;

export function PoolsView({ onNavigateToPrompts }: { onNavigateToPrompts: () => void }) {
  const toast = useToast();
  const t = useT();
  const [pools, setPools] = useState<PoolSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState<boolean>(false);
  const [newName, setNewName] = useState<string>("");
  const [confirm, setConfirm] = useState<Confirm>(null);
  /** Dirty signal: PoolEditor flips this via onDirtyChange. */
  const [dirty, setDirty] = useState<boolean>(false);
  const [filter, setFilter] = useState<string>("");
  /** Loading state for the pool Save button. */
  const [saving, setSaving] = useState<boolean>(false);
  /** Triggers received from PoolEditor to persist / revert the current pool. */
  const saveRef = useRef<(() => Promise<void>) | null>(null);
  const cancelRef = useRef<(() => void) | null>(null);

  const load = async (): Promise<void> => {
    setLoadError("");
    try {
      const list = await listPools();
      setPools(list);
      setSelected((cur) => cur ?? list[0]?.name ?? null);
    } catch (e) {
      setLoadError(String(e));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const visiblePools = useMemo<PoolSummary[]>(() => {
    if (!pools) return [];
    const q = filter.trim().toLowerCase();
    if (!q) return pools;
    return pools.filter((p) => p.name.toLowerCase().includes(q));
  }, [pools, filter]);

  if (loadError) {
    return <p className="text-base text-error">{t("common.failedToLoad", { error: loadError })}</p>;
  }
  if (!pools) {
    return <p className="text-base text-mute">{t("common.loading")}</p>;
  }

  const onSelect = (name: string): void => {
    if (name === selected) return;
    if (dirty) setConfirm({ kind: "switch", name });
    else setSelected(name);
  };

  const onAdd = async (): Promise<void> => {
    const name = newName.trim();
    setAdding(false);
    setNewName("");
    if (!name) return;
    try {
      await createPool(name);
      await load();
      setSelected(name);
    } catch (e) {
      toast.show({
        message: t("settings.pools.createFailed", { detail: e instanceof ApiError ? `${e.status} ${e.detail}` : String(e) }),
        tone: "warning",
      });
    }
  };

  const handleSave = async (): Promise<void> => {
    const save = saveRef.current;
    if (!save) return;
    setSaving(true);
    try {
      await save();
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = (): void => {
    cancelRef.current?.();
  };

  const onDelete = async (name: string): Promise<void> => {
    try {
      await deletePool(name);
      setConfirm(null);
      await load();
      if (selected === name) {
        setSelected(null);
      }
    } catch (e) {
      setConfirm(null);
      toast.show({
        message: t("settings.pools.deleteFailed", { detail: e instanceof ApiError ? `${e.status} ${e.detail}` : String(e) }),
        tone: "warning",
      });
    }
  };

  const meta = CATEGORY.pools;
  const PageHeadIcon = meta.icon;

  return (
    <div className="flex h-full flex-col">
      <div className="page-head">
        <span
          className="page-head-icon"
          style={{ ["--cat" as string]: meta.catVar }}
        >
          <PageHeadIcon size={18} />
        </span>
        <div>
          <div className="page-title">{meta.titleTerm ?? t(meta.titleKey!)}</div>
          <div className="page-sub">{t(meta.subKey)}</div>
        </div>
      </div>

      <div
        data-testid="pools-layout"
        className="flex min-h-0 flex-1 flex-col lg:flex-row"
      >
      {/* Left: pool list */}
      <aside
        aria-label={t("settings.pools.poolList")}
        className="flex max-h-48 w-full shrink-0 flex-col gap-3 border-b border-hairline bg-canvas-elevated pb-3 lg:max-h-none lg:w-64 lg:border-b-0 lg:border-r lg:pb-0 lg:pr-3"
      >
        <Input
          aria-label={t("settings.pools.filterPools")}
          placeholder={t("settings.pools.filterPoolsPlaceholder")}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !adding) {
              setAdding(true);
            }
          }}
          iconLeft={<SearchIcon />}
          className="text-xs"
        />

        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          {adding && (
            <input
              autoFocus
              placeholder={t("settings.pools.newPoolName")}
              className="mb-1 w-full rounded-sm border border-hairline bg-canvas-elevated px-2 py-1 text-base text-ink focus:border-link focus:outline-none focus:ring-1 focus:ring-link"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onBlur={() => void onAdd()}
              onKeyDown={(e) => {
                if (e.key === "Enter") void onAdd();
                if (e.key === "Escape") {
                  setAdding(false);
                  setNewName("");
                }
              }}
            />
          )}

          <ul className="space-y-1">
            {visiblePools.map((p) => {
              const isSel = p.name === selected;
              return (
                <li key={p.name} className="space-y-1">
                  <div
                    className={`flex items-center gap-1 rounded-md border px-3 py-2 ${
                      isSel
                        ? "border-hairline bg-hairline-soft font-semibold text-ink"
                        : "border-transparent text-body hover:bg-hairline-soft"
                    }`}
                  >
                    <button
                      type="button"
                      className="min-w-0 flex-1 truncate text-left text-base"
                      onClick={() => onSelect(p.name)}
                      title={t("settings.pools.subagentCount", { count: p.subagent_count })}
                    >
                      {p.name}
                    </button>
                    <IconButton
                      icon={<Trash2 size={16} />}
                      label={t("settings.pools.deleteName", { name: p.name })}
                      variant="ghost"
                      size="sm"
                      className="text-mute hover:text-error"
                      onClick={() =>
                        setConfirm({ kind: "delete", name: p.name })
                      }
                    />
                  </div>
                </li>
              );
            })}

            {/* Add-pool action — sits at the end of the list as a dashed
                + row, so the list reads as the single source of truth for
                pools and the affordance is contextual to the items. */}
            {!adding && (
              <li>
                <button
                  type="button"
                  onClick={() => setAdding(true)}
                  aria-label={t("settings.pools.addPool")}
                  className="flex w-full items-center justify-center gap-1.5 rounded-md border border-dashed border-hairline px-3 py-2 text-base text-mute transition-colors hover:border-border-strong hover:bg-hairline-soft hover:text-link"
                >
                  <PlusIcon />
                  {t("settings.pools.addPool")}
                </button>
              </li>
            )}
          </ul>

          {pools.length > 0 && visiblePools.length === 0 && (
            <p className="rounded-md border border-dashed border-hairline px-3 py-4 text-center text-xs text-mute">
              {t("settings.pools.noMatch", { filter })}
            </p>
          )}

          {pools.length === 0 && !adding && (
            <p className="rounded-md border border-dashed border-hairline px-3 py-4 text-center text-xs text-mute">
              {t("settings.pools.noPools")}
            </p>
          )}
        </div>
      </aside>

      {/* Right: editor */}
      <section
        aria-label={t("settings.pools.selectedPoolEditor")}
        className="flex min-w-0 flex-1 flex-col rounded-lg border border-hairline bg-canvas-elevated p-4"
      >
        {selected ? (
          <>
            <div className="flex-1 overflow-auto">
              <PoolEditor
                key={selected}
                pool={selected}
                onDirtyChange={setDirty}
                onSave={(save) => {
                  saveRef.current = save;
                }}
                onCancel={(cancel) => {
                  cancelRef.current = cancel;
                }}
                onNavigateToPrompts={onNavigateToPrompts}
              />
            </div>
            <ActionBar dirty={dirty}>
              <Button
                variant="secondary"
                size="sm"
                onClick={handleCancel}
                disabled={!dirty || saving}
              >
                {t("settings.pools.cancel")}
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={handleSave}
                disabled={!dirty || saving}
                loading={saving}
              >
                {t("settings.pools.save")}
              </Button>
            </ActionBar>
          </>
        ) : (
          <p className="text-base text-mute">
            {t("settings.pools.selectOrCreate")}
          </p>
        )}
      </section>
      </div>

      {confirm?.kind === "delete" ? (
        <ConfirmDialog
          title={t("settings.pools.deletePoolTitle", { name: confirm.name })}
          message={t("settings.pools.deletePoolMessage")}
          confirmLabel={t("settings.pools.delete")}
          tone="danger"
          onConfirm={() => void onDelete(confirm.name)}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
      {confirm?.kind === "switch" ? (
        <ConfirmDialog
          title={t("settings.pools.discardSwitchTitle")}
          message={t("settings.pools.discardSwitchMessage")}
          confirmLabel={t("settings.pools.discard")}
          tone="danger"
          onConfirm={() => {
            setSelected(confirm.name);
            setConfirm(null);
          }}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
    </div>
  );
}
