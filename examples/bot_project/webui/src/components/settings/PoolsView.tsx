// PoolsView — left/right layout: pool list + PoolEditor.
//
// Left list: listPools() summary; + Add pool (inline rename-style input, NOT
// window.prompt); per-pool inline rename + delete (delete uses the shared
// ConfirmDialog — NOT window.confirm). Deleting the default pool returns 409
// from the backend → surface as a toast.
//
// Switching to another pool while the current PoolEditor is dirty is guarded
// by a ConfirmDialog at this level (PoolEditor itself never sees the switch).

import { useEffect, useMemo, useRef, useState } from "react";
import type { PoolSummary } from "../../types/pool";
import {
  createPool,
  deletePool,
  listPools,
  renamePool,
} from "../../lib/poolApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { PoolEditor } from "./PoolEditor";
import { ConfirmDialog } from "./ConfirmDialog";
import { SectionLabel } from "../ui/SectionLabel";
import { ActionBar } from "../ui/ActionBar";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { Input } from "../ui/Input";
import {
  EditIcon,
  PlusIcon,
  SearchIcon,
} from "../ui/icons";
import { Trash2 } from "lucide-react";
import { CATEGORY } from "./categoryMeta";

type Confirm =
  | { kind: "delete"; name: string }
  | { kind: "switch"; name: string }
  | null;

interface RenameState {
  name: string;
  draft: string;
}

export function PoolsView() {
  const toast = useToast();
  const [pools, setPools] = useState<PoolSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState<boolean>(false);
  const [newName, setNewName] = useState<string>("");
  const [rename, setRename] = useState<RenameState | null>(null);
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
    return <p className="text-sm text-error">Failed to load: {loadError}</p>;
  }
  if (!pools) {
    return <p className="text-sm text-mute">Loading…</p>;
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
        message: `Create failed: ${e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)}`,
        tone: "warning",
      });
    }
  };

  const onRename = async (): Promise<void> => {
    if (!rename) return;
    const next = rename.draft.trim();
    const oldName = rename.name;
    if (!next || next === oldName) {
      setRename(null);
      return;
    }
    try {
      await renamePool(oldName, next);
      setRename(null);
      await load();
      if (selected === oldName) setSelected(next);
    } catch (e) {
      toast.show({
        message: `Rename failed: ${e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)}`,
        tone: "warning",
      });
      setRename(null);
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
      if (e instanceof ApiError && e.status === 409) {
        toast.show({
          message: `Cannot delete "${name}" (default pool or in use).`,
          tone: "warning",
        });
      } else {
        toast.show({
          message: `Delete failed: ${e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)}`,
          tone: "warning",
        });
      }
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
          <div className="page-title">{meta.title}</div>
          <div className="page-sub">{meta.sub}</div>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
      {/* Left: pool list */}
      <div className="flex w-64 shrink-0 flex-col gap-3 border-r border-hairline bg-canvas-elevated pr-3">
        <div className="flex items-center justify-between">
          <SectionLabel>Pools</SectionLabel>
          <IconButton
            icon={<PlusIcon />}
            label="Add pool"
            variant="ghost"
            size="sm"
            className="text-mute hover:text-link"
            onClick={() => setAdding(true)}
          />
        </div>

        <Input
          aria-label="Filter pools"
          placeholder="Filter pools…"
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
              placeholder="new-pool-name"
              className="mb-1 w-full rounded-sm border border-hairline bg-canvas-elevated px-2 py-1 text-sm text-ink focus:border-link focus:outline-none focus:ring-1 focus:ring-link"
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
              const isRenaming = rename?.name === p.name;
              return (
                <li key={p.name} className="space-y-1">
                  {isRenaming ? (
                    <input
                      autoFocus
                      className="w-full rounded-sm border border-hairline bg-canvas-elevated px-2 py-1 text-sm text-ink focus:border-link focus:outline-none focus:ring-1 focus:ring-link"
                      value={rename!.draft}
                      onChange={(e) =>
                        setRename({
                          name: rename!.name,
                          draft: e.target.value,
                        })
                      }
                      onBlur={() => void onRename()}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void onRename();
                        if (e.key === "Escape") setRename(null);
                      }}
                    />
                  ) : (
                    <div
                      className={`flex items-center gap-1 rounded-md border px-3 py-2 ${
                        isSel
                          ? "border-hairline bg-hairline-soft font-semibold text-ink"
                          : "border-transparent text-body hover:bg-hairline-soft"
                      }`}
                    >
                      <button
                        type="button"
                        className="min-w-0 flex-1 truncate text-left text-sm"
                        onClick={() => onSelect(p.name)}
                        onDoubleClick={() =>
                          setRename({ name: p.name, draft: p.name })
                        }
                        title={`${p.subagent_count} subagent(s)`}
                      >
                        {p.name}
                      </button>
                      <IconButton
                        icon={<EditIcon />}
                        label={`Rename ${p.name}`}
                        variant="ghost"
                        size="sm"
                        className="text-mute hover:text-link"
                        onClick={() =>
                          setRename({ name: p.name, draft: p.name })
                        }
                      />
                      <IconButton
                        icon={<Trash2 size={16} />}
                        label={`Delete ${p.name}`}
                        variant="ghost"
                        size="sm"
                        className="text-mute hover:text-error"
                        onClick={() =>
                          setConfirm({ kind: "delete", name: p.name })
                        }
                      />
                    </div>
                  )}
                </li>
              );
            })}
          </ul>

          {pools.length > 0 && visiblePools.length === 0 && (
            <p className="rounded-md border border-dashed border-hairline px-3 py-4 text-center text-xs text-mute">
              No pools match "{filter}".
            </p>
          )}

          {pools.length === 0 && !adding && (
            <p className="rounded-md border border-dashed border-hairline px-3 py-4 text-center text-xs text-mute">
              No pools. Click + to create one.
            </p>
          )}
        </div>
      </div>

      {/* Right: editor */}
      <div className="flex flex-1 flex-col rounded-lg border border-hairline bg-canvas-elevated p-4">
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
              />
            </div>
            <ActionBar>
              <Button
                variant="secondary"
                size="sm"
                onClick={handleCancel}
                disabled={!dirty || saving}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={handleSave}
                disabled={!dirty || saving}
                loading={saving}
              >
                Save
              </Button>
            </ActionBar>
          </>
        ) : (
          <p className="text-sm text-mute">
            Select a pool, or click + to create one.
          </p>
        )}
      </div>
      </div>

      {confirm?.kind === "delete" ? (
        <ConfirmDialog
          title={`Delete pool "${confirm.name}"?`}
          message="All agent configs, subagents and prompts in this pool will be removed."
          confirmLabel="Delete"
          tone="danger"
          onConfirm={() => void onDelete(confirm.name)}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
      {confirm?.kind === "switch" ? (
        <ConfirmDialog
          title="Discard unsaved changes?"
          message="Switching pools now will lose your edits to the current pool."
          confirmLabel="Discard"
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