// Global MCP registry editor. Loads getMcp() on mount → list of server cards.
// Add/edit/delete entries; Save per-card via upsertMcp; Delete via deleteMcp
// (surfaces the used_by conflict list on 409). MCP writes always imply a
// restart, so successful save/delete shows a "Saved. Restart to apply." toast.
//
// Each card is collapsible (first card / newly-added cards start expanded).
// Header row carries name, transport · command|URL summary, save link, trash
// icon, and a chevron that rotates by `open`. The body uses the standard
// form primitives (Input / Select / Textarea / HelperText).

import { useEffect, useRef, useState } from "react";
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
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { Input } from "../ui/Input";
import { Select } from "../ui/Select";
import { Textarea } from "../ui/Textarea";
import { KeyValueEditor } from "../ui/KeyValueEditor";
import { HelperText } from "../ui/HelperText";
import { IconButton } from "../ui/IconButton";
import { ChevronDownIcon, TrashIcon } from "../ui/icons";

const TRANSPORTS: McpTransport[] = ["stdio", "sse", "streamableHttp"];
const TRANSPORT_OPTIONS = [
  { value: "", label: "—" },
  ...TRANSPORTS.map((t) => ({ value: t, label: t })),
];

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
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const nextId = useRef<number>(1);

  const load = async (): Promise<void> => {
    setLoadError("");
    try {
      const map = await getMcp();
      const list = Object.entries(map).map(([name, entry]) => ({
        id: nextId.current++,
        originalName: name,
        name,
        entry,
      }));
      setCards(list);
      // Default: only the first persisted card is expanded.
      setExpanded(list.length > 0 ? new Set([list[0]!.id]) : new Set());
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
  if (!cards) {
    return <p className="text-sm text-mute">Loading…</p>;
  }

  const update = (i: number, patch: Partial<CardState>): void => {
    setCards((prev) => prev!.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  };

  const toggleExpanded = (id: number): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const collapse = (id: number): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  };

  const removeAndCollapse = (i: number): void => {
    setCards((prev) => {
      const removed = prev![i]!;
      collapse(removed.id);
      return prev!.filter((_, j) => j !== i);
    });
  };

  const addCard = (): void => {
    const newId = nextId.current++;
    setCards((prev) => [
      { id: newId, originalName: null, name: "", entry: emptyEntry() },
      ...(prev ?? []),
    ]);
    setExpanded((prev) => {
      const next = new Set(prev);
      next.add(newId);
      return next;
    });
  };

  const onSave = async (i: number): Promise<void> => {
    const card = cards[i]!;
    const name = card.name.trim();
    if (!name) {
      toast.show({ message: "Server name is required.", tone: "warning" });
      return;
    }

    const renamed =
      card.originalName !== null && card.originalName !== name;

    try {
      if (renamed) {
        await deleteMcp(card.originalName!);
      }
      await upsertMcp(name, card.entry);
      update(i, { originalName: name, name, conflict: undefined });
      restartToast(toast);
    } catch (e) {
      // If the delete succeeded but the upsert failed, the old config is gone.
      // Restore the original name locally so the user can retry without losing
      // the previous identity.
      if (renamed) {
        update(i, { name: card.originalName! });
      }
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
      removeAndCollapse(i);
      return;
    }
    try {
      await deleteMcp(name);
      removeAndCollapse(i);
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
        <p className="text-xs text-mute">
          Global MCP servers available to every pool's agents.
        </p>
        <Button variant="secondary" size="sm" onClick={addCard}>
          + Add server
        </Button>
      </div>

      {cards.length === 0 && (
        <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-sm text-mute">
          No MCP servers configured.
        </p>
      )}

      <div className="space-y-2">
        {cards.map((card, i) => (
          <McpCard
            key={card.id}
            card={card}
            open={expanded.has(card.id)}
            onToggle={() => toggleExpanded(card.id)}
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
  open,
  onToggle,
  onChange,
  onSave,
  onDelete,
}: {
  card: CardState;
  open: boolean;
  onToggle: () => void;
  onChange: (patch: Partial<CardState>) => void;
  onSave: () => void;
  onDelete: () => void;
}) {
  const e = card.entry;
  const setEntry = (patch: Partial<McpServerEntry>): void =>
    onChange({ entry: { ...e, ...patch } });

  // Dirty = has been edited and not yet persisted as the current name.
  const dirty = card.name.trim() !== (card.originalName ?? "");

  // Header is a clickable region (toggles the body), but it must also host
  // real <button> children (Save, Trash, Chevron) — so we render it as a
  // <div role="button"> rather than a <button> to avoid nested-button HTML.
  const headerClick = onToggle;

  return (
    <Card>
      <div
        role="button"
        tabIndex={0}
        aria-expanded={open}
        aria-controls={`mcp-card-${card.id}-body`}
        onClick={headerClick}
        onKeyDown={(ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            headerClick();
          }
        }}
        className="flex w-full cursor-pointer items-center gap-2 rounded text-left outline-none focus-visible:ring-2 focus-visible:ring-link/50"
      >
        {dirty && (
          <span
            aria-hidden="true"
            title="Unsaved changes"
            className="h-2 w-2 shrink-0 rounded-full bg-warning"
          />
        )}
        <span className="truncate text-sm font-medium text-ink">
          {card.originalName ?? (
            <span className="italic text-mute">New server</span>
          )}
        </span>
        <span className="truncate font-mono text-xs text-mute">
          {e.command || e.url || "no command"}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={(ev) => {
              ev.stopPropagation();
              onSave();
            }}
          >
            Save
          </Button>
          <IconButton
            label="Delete server"
            icon={<TrashIcon />}
            variant="ghost"
            size="sm"
            onClick={(ev) => {
              ev.stopPropagation();
              onDelete();
            }}
          />
          <IconButton
            label={open ? "Collapse" : "Expand"}
            icon={<ChevronDownIcon open={open} />}
            variant="ghost"
            size="sm"
            onClick={(ev) => {
              ev.stopPropagation();
              onToggle();
            }}
            tabIndex={-1}
          />
        </div>
      </div>

      {open && (
        <div
          id={`mcp-card-${card.id}-body`}
          className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2"
        >
          <Input
            label="Name"
            required
            value={card.name}
            onChange={(ev) => onChange({ name: ev.target.value })}
          />
          <Select
            label="Transport"
            value={e.transport ?? ""}
            onChange={(ev) =>
              setEntry({
                transport: (ev.target.value || undefined) as
                  | McpTransport
                  | undefined,
              })
            }
            options={TRANSPORT_OPTIONS}
          />
          <Input
            label="Command"
            value={e.command ?? ""}
            onChange={(ev) => setEntry({ command: ev.target.value })}
            placeholder="npx"
          />
          <Input
            label="URL"
            value={e.url ?? ""}
            onChange={(ev) => setEntry({ url: ev.target.value })}
            placeholder="https://…"
          />
          <div className="sm:col-span-2">
            <Textarea
              label="Args (one per line or comma-separated)"
              helper="Use newlines or commas to separate arguments."
              mono={false}
              value={kvListToText(e.args ?? [])}
              onChange={(ev) =>
                setEntry({ args: textToKvList(ev.target.value) })
              }
            />
          </div>
          <div className="sm:col-span-2">
            <KeyValueEditor
              label="Environment variables"
              helper="Variables available to the server process."
              entries={e.env ?? {}}
              onChange={(env) => setEntry({ env })}
            />
          </div>
          <div className="sm:col-span-2">
            <KeyValueEditor
              label="HTTP headers"
              helper="Custom headers sent with non-stdio transports."
              entries={e.headers ?? {}}
              onChange={(headers) => setEntry({ headers })}
            />
          </div>
          <Input
            label="Working directory"
            value={e.cwd ?? ""}
            onChange={(ev) => setEntry({ cwd: ev.target.value })}
          />
          <Input
            label="Timeout (s)"
            type="number"
            value={e.timeout ?? 30}
            onChange={(ev) => setEntry({ timeout: Number(ev.target.value) })}
          />

          {card.conflict && card.conflict.length > 0 && (
            <div className="sm:col-span-2">
              <HelperText className="text-warning">
                In use by{" "}
                {card.conflict.map(([p, a]) => `${p}/${a}`).join(", ")}. Unassign
                from those agents before deleting.
              </HelperText>
            </div>
          )}
        </div>
      )}
    </Card>
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

function errDetail(e: unknown): string {
  if (e instanceof ApiError) return `${e.status} ${e.detail}`;
  return String(e);
}