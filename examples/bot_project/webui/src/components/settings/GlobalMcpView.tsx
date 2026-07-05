// Global MCP registry editor. Loads getMcp() on mount → list of server cards.
// Add/edit/delete entries; Save per-card via upsertMcp; Delete via deleteMcp
// (surfaces the used_by conflict list on 409). MCP writes always imply a
// restart, so successful save/delete shows a "Saved. Restart to apply." toast.

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { McpServerEntry, McpTransport } from "../../types/pool";
import {
  getMcp,
  upsertMcp,
  deleteMcp,
  McpInUseError,
} from "../../lib/mcpApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";

const INPUT =
  "w-full rounded border border-input-border bg-input-bg px-2.5 py-1.5 text-sm text-text-primary placeholder:text-text-disabled focus:border-input-focus focus:outline-none focus:ring-1 focus:ring-input-focus";
const LABEL = "mb-1 block text-xs font-medium text-text-secondary";

const TRANSPORTS: McpTransport[] = ["stdio", "sse", "streamableHttp"];

interface CardState {
  /** Stable id for React keys (not the server name, which can change). */
  id: number;
  /** Original name (null = newly added, not yet persisted). */
  originalName: string | null;
  /** Currently-edited name. */
  name: string;
  entry: McpServerEntry;
  /** Conflict list surfaced when delete hits 409. */
  conflict?: Array<[string, string]>;
}

const emptyEntry = (): McpServerEntry => ({
  transport: "stdio",
  command: "",
  args: [],
  env: {},
  cwd: "",
  url: "",
  headers: {},
  timeout: 30,
});

export function GlobalMcpView() {
  const toast = useToast();
  const [cards, setCards] = useState<CardState[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const _nextId = useRef<number>(1);

  const load = async (): Promise<void> => {
    setLoadError("");
    try {
      const map = await getMcp();
      setCards(
        Object.entries(map).map(([name, entry]) => ({
          id: _nextId.current++,
          originalName: name,
          name,
          entry,
        })),
      );
    } catch (e) {
      setLoadError(String(e));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  if (loadError) {
    return (
      <p className="text-sm text-error">Failed to load: {loadError}</p>
    );
  }
  if (!cards) {
    return (
      <p className="text-sm text-text-secondary">Loading…</p>
    );
  }

  const update = (i: number, patch: Partial<CardState>): void => {
    setCards((prev) => prev!.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  };

  const addCard = (): void => {
    setCards((prev) => [
      ...(prev ?? []),
      { id: _nextId.current++, originalName: null, name: "", entry: emptyEntry() },
    ]);
  };

  const onSave = async (i: number): Promise<void> => {
    const card = cards[i]!;
    const name = card.name.trim();
    if (!name) {
      toast.show({ message: "Server name is required.", tone: "warning" });
      return;
    }
    try {
      // Rename: the old server name (if any, and different) must be deleted so
      // the renamed entry replaces it rather than leaving an orphan. A 409
      // (in-use) on the old name blocks the rename — the user must unassign it.
      const renamed =
        card.originalName !== null && card.originalName !== name;
      if (renamed) {
        await deleteMcp(card.originalName!);
      }
      await upsertMcp(name, card.entry);
      update(i, { originalName: name, name, conflict: undefined });
      // MCP upsert/delete unconditionally mark the pool dirty (the registry is
      // read at pool boot, not hot-reloaded), so the restart toast fires
      // unconditionally.
      restartToast(toast);
    } catch (e) {
      if (e instanceof McpInUseError) {
        const where = e.usedBy.map(([p, a]) => `${p}/${a}`).join(", ");
        update(i, { conflict: e.usedBy });
        toast.show({
          message: `Rename blocked — "${card.originalName}" in use by ${where}. Unassign first.`,
          tone: "warning",
        });
      } else {
        toast.show({ message: `Save failed: ${errDetail(e)}`, tone: "warning" });
      }
    }
  };

  const onDelete = async (i: number): Promise<void> => {
    const card = cards[i]!;
    const name = card.originalName ?? card.name.trim();
    if (!card.originalName) {
      // Never persisted — just drop locally.
      setCards((prev) => prev!.filter((_, j) => j !== i));
      return;
    }
    try {
      await deleteMcp(name);
      setCards((prev) => prev!.filter((_, j) => j !== i));
      // Deletion also implies a restart; reuse the uniform toast (the message
      // already says "Saved", which is close enough for a delete→restart hint
      // and keeps every restart surface identical).
      restartToast(toast);
    } catch (e) {
      if (e instanceof McpInUseError) {
        const where = e.usedBy.map(([p, a]) => `${p}/${a}`).join(", ");
        update(i, { conflict: e.usedBy });
        toast.show({
          message: `In use by ${where}. Unassign first.`,
          tone: "warning",
        });
      } else {
        toast.show({
          message: `Delete failed: ${errDetail(e)}`,
          tone: "warning",
        });
      }
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-text-secondary">
          Global MCP servers available to every pool's agents.
        </p>
        <button
          type="button"
          className="rounded-md border border-input-border px-3 py-1.5 text-sm text-text-primary hover:bg-sidebar-hover"
          onClick={addCard}
        >
          + Add server
        </button>
      </div>

      {cards.length === 0 && (
        <p className="rounded-md border border-dashed border-input-border px-3 py-6 text-center text-sm text-text-secondary">
          No MCP servers configured.
        </p>
      )}

      <div className="space-y-2">
        {cards.map((card, i) => (
          <McpCard
            key={card.id}
            card={card}
            onChange={(patch) => update(i, patch)}
            onSave={() => onSave(i)}
            onDelete={() => onDelete(i)}
          />
        ))}
      </div>
    </div>
  );
}

function McpCard({
  card,
  onChange,
  onSave,
  onDelete,
}: {
  card: CardState;
  onChange: (patch: Partial<CardState>) => void;
  onSave: () => void;
  onDelete: () => void;
}) {
  const e = card.entry;
  const setEntry = (patch: Partial<McpServerEntry>): void =>
    onChange({ entry: { ...e, ...patch } });

  return (
    <div className="rounded-lg border border-card-border bg-content-bg p-4">
      <div className="mb-3 flex items-center gap-2">
        <span className="truncate text-sm font-medium text-text-primary">
          {card.originalName ?? (
            <span className="italic text-text-secondary">New server</span>
          )}
        </span>
        <span className="truncate font-mono text-xs text-text-secondary">
          {e.transport ?? "—"} · {e.command || e.url || "no command"}
        </span>
        <div className="ml-auto flex items-center gap-3">
          <button
            type="button"
            className="text-xs text-ai-brand hover:underline"
            onClick={onSave}
          >
            Save
          </button>
          <button
            type="button"
            aria-label="Delete server"
            className="text-text-secondary hover:text-error"
            onClick={onDelete}
          >
            <TrashIcon />
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Name" required>
          <input
            className={INPUT}
            value={card.name}
            onChange={(ev) => onChange({ name: ev.target.value })}
          />
        </Field>
        <Field label="Transport">
          <select
            className={INPUT}
            value={e.transport ?? ""}
            onChange={(ev) =>
              setEntry({
                transport: (ev.target.value || undefined) as
                  | McpTransport
                  | undefined,
              })
            }
          >
            <option value="">—</option>
            {TRANSPORTS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Command">
          <input
            className={INPUT}
            value={e.command ?? ""}
            onChange={(ev) => setEntry({ command: ev.target.value })}
            placeholder="npx"
          />
        </Field>
        <Field label="URL">
          <input
            className={INPUT}
            value={e.url ?? ""}
            onChange={(ev) => setEntry({ url: ev.target.value })}
            placeholder="https://…"
          />
        </Field>
        <Field label="Args (one per line or comma-separated)" className="sm:col-span-2">
          <textarea
            className={`${INPUT} min-h-[60px]`}
            value={kvListToText(e.args ?? [])}
            onChange={(ev) =>
              setEntry({ args: textToKvList(ev.target.value) })
            }
          />
        </Field>
        <Field label="Environment (KEY=value, one per line)" className="sm:col-span-2">
          <textarea
            className={`${INPUT} min-h-[60px] font-mono`}
            value={envToText(e.env ?? {})}
            onChange={(ev) => setEntry({ env: textToEnv(ev.target.value) })}
          />
        </Field>
        <Field label="Headers (KEY:value, one per line)" className="sm:col-span-2">
          <textarea
            className={`${INPUT} min-h-[48px] font-mono`}
            value={envToText(e.headers ?? {})}
            onChange={(ev) => setEntry({ headers: textToEnv(ev.target.value) })}
          />
        </Field>
        <Field label="Working directory">
          <input
            className={INPUT}
            value={e.cwd ?? ""}
            onChange={(ev) => setEntry({ cwd: ev.target.value })}
          />
        </Field>
        <Field label="Timeout (s)">
          <input
            type="number"
            className={INPUT}
            value={e.timeout ?? 30}
            onChange={(ev) => setEntry({ timeout: Number(ev.target.value) })}
          />
        </Field>
      </div>

      {card.conflict && card.conflict.length > 0 && (
        <p className="mt-3 rounded border border-warning bg-content-bg px-2 py-1.5 text-xs text-warning">
          In use by{" "}
          {card.conflict.map(([p, a]) => `${p}/${a}`).join(", ")}. Unassign from
          those agents before deleting.
        </p>
      )}
    </div>
  );
}

function Field({
  label,
  required,
  className,
  children,
}: {
  label: string;
  required?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={className}>
      <label className={LABEL}>
        {label}
        {required && <span className="text-error"> *</span>}
      </label>
      {children}
    </div>
  );
}

function TrashIcon() {
  return (
    <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3 4h10M6.5 4V2.5h3V4M5 4l.5 8.5a1 1 0 0 0 1 .9h3a1 1 0 0 0 1-.9L11 4M6.5 7v4M9.5 7v4"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// ─── list/env text helpers ───────────────────────────────────────────────────

function kvListToText(list: string[]): string {
  return list.join("\n");
}

function textToKvList(text: string): string[] {
  // Split on newlines OR commas; trim; drop empties.
  return text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function envToText(env: Record<string, string>): string {
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

function textToEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 0) continue;
    const k = trimmed.slice(0, eq).trim();
    const v = trimmed.slice(eq + 1).trim();
    if (k) out[k] = v;
  }
  return out;
}

function errDetail(e: unknown): string {
  if (e instanceof ApiError) return `${e.status} ${e.detail}`;
  return String(e);
}
