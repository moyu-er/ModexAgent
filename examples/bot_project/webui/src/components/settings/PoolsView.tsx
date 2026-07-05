// PoolsView — left/right layout: pool list + PoolEditor.
//
// Left list: listPools() summary; + Add pool (inline rename-style input, NOT
// window.prompt); per-pool inline rename + delete (delete uses the shared
// ConfirmDialog — NOT window.confirm). Deleting the default pool returns 409
// from the backend → surface as a toast.
//
// Switching to another pool while the current PoolEditor is dirty is guarded
// by a ConfirmDialog at this level (PoolEditor itself never sees the switch).

import { useEffect, useState } from "react";
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
import { PlusIcon, TrashIcon } from "./icons";

const INPUT =
  "w-full rounded border border-input-border bg-input-bg px-2 py-1 text-sm text-text-primary focus:border-input-focus focus:outline-none focus:ring-1 focus:ring-input-focus";

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

  if (loadError) {
    return <p className="text-sm text-error">Failed to load: {loadError}</p>;
  }
  if (!pools) {
    return <p className="text-sm text-text-secondary">Loading…</p>;
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
    if (!next || next === rename.name) {
      setRename(null);
      return;
    }
    try {
      await renamePool(rename.name, next);
      setRename(null);
      await load();
      if (selected === rename.name) setSelected(next);
    } catch (e) {
      toast.show({
        message: `Rename failed: ${e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)}`,
        tone: "warning",
      });
      setRename(null);
    }
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

  return (
    <div className="flex h-full">
      {/* Left: pool list */}
      <div className="w-56 shrink-0 space-y-2 border-r border-divider pr-3">
        <div className="flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-wide text-text-disabled">
            Pools
          </h2>
          <button
            type="button"
            aria-label="Add pool"
            className="text-text-secondary hover:text-ai-brand"
            onClick={() => setAdding(true)}
          >
            <PlusIcon />
          </button>
        </div>

        <ul className="space-y-1">
          {pools.map((p) => {
            const isSel = p.name === selected;
            const isRenaming = rename?.name === p.name;
            return (
              <li key={p.name} className="space-y-1">
                {isRenaming ? (
                  <input
                    autoFocus
                    className={INPUT}
                    value={rename!.draft}
                    onChange={(e) =>
                      setRename({ name: rename!.name, draft: e.target.value })
                    }
                    onBlur={() => void onRename()}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void onRename();
                      if (e.key === "Escape") setRename(null);
                    }}
                  />
                ) : (
                  <div
                    className={`flex items-center gap-1 rounded px-2 py-1 ${
                      isSel
                        ? "bg-sidebar-hover font-semibold text-text-primary"
                        : "text-text-secondary hover:bg-sidebar-hover"
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
                    <button
                      type="button"
                      aria-label={`Rename ${p.name}`}
                      className="text-text-secondary hover:text-ai-brand"
                      onClick={() =>
                        setRename({ name: p.name, draft: p.name })
                      }
                    >
                      ✎
                    </button>
                    <button
                      type="button"
                      aria-label={`Delete ${p.name}`}
                      className="text-text-secondary hover:text-error"
                      onClick={() => setConfirm({ kind: "delete", name: p.name })}
                    >
                      <TrashIcon />
                    </button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>

        {adding && (
          <input
            autoFocus
            placeholder="new-pool-name"
            className={INPUT}
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

        {pools.length === 0 && !adding && (
          <p className="rounded-md border border-dashed border-input-border px-3 py-4 text-center text-xs text-text-secondary">
            No pools. Click + to create one.
          </p>
        )}
      </div>

      {/* Right: editor */}
      <div className="flex-1 overflow-auto pl-4">
        {selected ? (
          <PoolEditor
            key={selected}
            pool={selected}
            onDirtyChange={setDirty}
          />
        ) : (
          <p className="text-sm text-text-secondary">
            Select a pool, or click + to create one.
          </p>
        )}
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
