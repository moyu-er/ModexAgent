// Global MCP registry editor. Loads getMcp() on mount → list of server cards.
// Add/edit/delete entries; Save per-card via upsertMcp; Delete via deleteMcp.
// MCP writes always imply a restart, so successful save/delete shows a
// "Saved. Restart to apply." toast.
//
// Card chrome mirrors ModelEditor: a chevron-right header with a status dot,
// transport badge, and a two-step (confirm) delete, over a sectioned body.
//
// Transport UX: there are TWO categories — stdio and "Remote HTTP"
// (sse / streamableHttp). The category toggle only flips which field group is
// shown; it NEVER clears what was typed, so toggling back and forth restores
// the previous input (until Save). sse and streamableHttp share the same
// Remote fields, so switching protocol between them is pure metadata.

import { useEffect, useRef, useState } from "react";
import type { McpServerEntry, McpTransport } from "../../types/pool";
import { getMcp, upsertMcp, deleteMcp } from "../../lib/mcpApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { Input } from "../ui/Input";
import { Textarea } from "../ui/Textarea";
import { KeyValueEditor } from "../ui/KeyValueEditor";
import { Label } from "../ui/Label";
import { HelperText } from "../ui/HelperText";
import { IconButton } from "../ui/IconButton";
import { SectionLabel } from "../ui/SectionLabel";
import { Trash2 } from "lucide-react";
import { ChevronRightIcon, PlusIcon } from "../ui/icons";
import { CATEGORY } from "./categoryMeta";

// Protocol choice shown only under the Remote HTTP category. The two share
// fields, so this is metadata only — no data reset on change.
const PROTOCOL_OPTIONS = [
  { value: "sse", label: "SSE" },
  { value: "streamableHttp", label: "Streamable HTTP" },
];

interface CardState {
  /** Stable id for React keys (not the server name, which can change). */
  id: number;
  /** Original name (null = newly added, not yet persisted). */
  originalName: string | null;
  /** Currently-edited name. */
  name: string;
  entry: McpServerEntry;
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
  /** Card id currently showing its delete-confirm row (null = none). */
  const [confirmId, setConfirmId] = useState<number | null>(null);
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
      update(i, { originalName: name, name });
      restartToast(toast);
    } catch (e) {
      // If the delete succeeded but the upsert failed, the old config is gone.
      // Restore the original name locally so the user can retry without losing
      // the previous identity.
      if (renamed) {
        update(i, { name: card.originalName! });
      }
      toast.show({ message: `Save failed: ${errDetail(e)}`, tone: "warning" });
    }
  };

  const onDelete = async (i: number): Promise<void> => {
    const card = cards[i]!;
    const name = card.originalName ?? card.name.trim();
    if (!card.originalName) {
      // Never persisted — just drop locally.
      removeAndCollapse(i);
      setConfirmId(null);
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
      toast.show({
        message: `Delete failed: ${errDetail(e)}`,
        tone: "warning",
      });
    } finally {
      setConfirmId(null);
    }
  };

  const meta = CATEGORY.mcp;
  const PageHeadIcon = meta.icon;

  return (
    <div className="space-y-6">
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

      <p className="text-xs text-mute">
        MCP servers available to every pool's agents.
      </p>

      <div className="space-y-2">
        {cards.map((card, i) => (
          <McpCard
            key={card.id}
            card={card}
            open={expanded.has(card.id)}
            confirming={confirmId === card.id}
            onToggle={() => toggleExpanded(card.id)}
            onChange={(patch) => update(i, patch)}
            onSave={() => onSave(i)}
            onRequestDelete={() => setConfirmId(card.id)}
            onConfirmDelete={() => void onDelete(i)}
            onCancelDelete={() => setConfirmId(null)}
          />
        ))}

        {cards.length === 0 && (
          <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-sm text-mute">
            No MCP servers configured.
          </p>
        )}

        <button
          type="button"
          className="mt-1 flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-hairline py-2.5 text-sm text-body hover:border-ink hover:bg-hairline-soft hover:text-ink"
          onClick={addCard}
        >
          <PlusIcon /> Add server
        </button>
      </div>
    </div>
  );
}

function McpCard({
  card,
  open,
  confirming,
  onToggle,
  onChange,
  onSave,
  onRequestDelete,
  onConfirmDelete,
  onCancelDelete,
}: {
  card: CardState;
  open: boolean;
  confirming: boolean;
  onToggle: () => void;
  onChange: (patch: Partial<CardState>) => void;
  onSave: () => void;
  onRequestDelete: () => void;
  onConfirmDelete: () => void;
  onCancelDelete: () => void;
}) {
  const e = card.entry;
  const setEntry = (patch: Partial<McpServerEntry>): void =>
    onChange({ entry: { ...e, ...patch } });

  // Resolve the transport that governs which field group is shown. An entry
  // with no explicit transport (legacy auto-detect) falls back to url→HTTP,
  // else stdio.
  const resolvedTransport: McpTransport =
    e.transport ?? (e.url ? "streamableHttp" : "stdio");
  const isHttp =
    resolvedTransport === "sse" || resolvedTransport === "streamableHttp";

  // Switching category/protocol changes ONLY `transport`. Both field sets stay
  // cached on the entry, so the user can toggle Local↔Remote and recover what
  // they typed. sse↔streamableHttp is metadata-only (shared Remote fields).
  const setCategory = (http: boolean): void => {
    if (http) {
      setEntry({ transport: isHttp ? resolvedTransport : "streamableHttp" });
    } else {
      setEntry({ transport: "stdio" });
    }
  };
  const setProtocol = (t: McpTransport): void => setEntry({ transport: t });

  const configured = Boolean(isHttp ? e.url : e.command);
  const dirty = card.name.trim() !== (card.originalName ?? "");

  return (
    <Card className="p-5">
      {/* Header row */}
      <div className="flex items-center gap-2 px-3 py-2.5">
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2.5 rounded px-1 py-0.5 text-left hover:bg-hairline-soft"
        >
          <ChevronRightIcon
            className={`transition-transform ${open ? "rotate-90" : ""}`}
          />
          <StatusDot on={configured} />
          {dirty && (
            <span
              aria-hidden="true"
              title="Unsaved changes"
              className="h-2 w-2 shrink-0 rounded-full bg-warning"
            />
          )}
          <span className="truncate text-sm font-medium text-ink">
            {card.originalName ?? (
              <span className="italic text-body">New server</span>
            )}
          </span>
          <TransportBadge remote={isHttp} label={transportLabel(resolvedTransport)} />
          <span className="truncate font-mono text-xs text-body">
            {e.command || e.url || "not configured"}
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="ghost" size="sm" onClick={onSave}>
            Save
          </Button>
          {confirming ? (
            <span className="flex items-center gap-2 text-xs">
              <Button
                variant="link"
                size="sm"
                className="font-medium text-error hover:underline"
                onClick={onConfirmDelete}
              >
                Delete
              </Button>
              <Button
                variant="link"
                size="sm"
                className="text-body hover:underline"
                onClick={onCancelDelete}
              >
                Cancel
              </Button>
            </span>
          ) : (
            <IconButton
              icon={<Trash2 size={16} />}
              label="Delete server"
              variant="ghost"
              size="sm"
              onClick={onRequestDelete}
            />
          )}
        </div>
      </div>

      {/* Body */}
      {open && (
        <div
          id={`mcp-card-${card.id}-body`}
          className="space-y-5 border-t border-hairline px-4 py-4"
        >
          {/* Identity */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Input
              label="Name"
              required
              value={card.name}
              onChange={(ev) => onChange({ name: ev.target.value })}
            />
          </div>

          {/* Transport category */}
          <div>
            <SectionLabel>Transport</SectionLabel>
            <div className="grid grid-cols-2 gap-2">
              <CategoryButton
                active={!isHttp}
                title="stdio"
                desc="Run a subprocess on this machine"
                onClick={() => setCategory(false)}
              />
              <CategoryButton
                active={isHttp}
                title="Remote HTTP"
                desc="Connect to a network endpoint"
                onClick={() => setCategory(true)}
              />
            </div>
            {isHttp && (
              <div className="mt-3">
                <Label>Protocol</Label>
                <div className="flex flex-wrap gap-1.5">
                  {PROTOCOL_OPTIONS.map((opt) => {
                    const active = resolvedTransport === opt.value;
                    return (
                      <button
                        key={opt.value}
                        type="button"
                        aria-pressed={active}
                        onClick={() => setProtocol(opt.value as McpTransport)}
                        className={
                          active
                            ? "inline-flex items-center rounded-full border border-link bg-canvas-elevated px-3 py-1 text-xs font-medium text-link"
                            : "inline-flex items-center rounded-full border border-hairline bg-canvas-elevated px-3 py-1 text-xs text-body hover:border-ink hover:text-ink"
                        }
                      >
                        {opt.label}
                      </button>
                    );
                  })}
                </div>
                <HelperText>
                  SSE and Streamable HTTP share the same fields — switching
                  never clears your input.
                </HelperText>
              </div>
            )}
          </div>

          {/* Configuration fields */}
          <div>
            <SectionLabel>{isHttp ? "Endpoint" : "Process"}</SectionLabel>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {isHttp ? (
                <>
                  <div className="sm:col-span-2">
                    <Input
                      label="URL"
                      required
                      value={e.url ?? ""}
                      onChange={(ev) => setEntry({ url: ev.target.value })}
                      placeholder="https://…"
                    />
                  </div>
                  <Input
                    label="Timeout (s)"
                    type="number"
                    value={e.timeout ?? 30}
                    onChange={(ev) =>
                      setEntry({ timeout: Number(ev.target.value) })
                    }
                  />
                  <div className="sm:col-span-2">
                    <KeyValueEditor
                      label="HTTP headers"
                      helper="Custom headers sent with each request (e.g. Authorization)."
                      entries={e.headers ?? {}}
                      onChange={(headers) => setEntry({ headers })}
                    />
                  </div>
                </>
              ) : (
                <>
                  <Input
                    label="Command"
                    required
                    value={e.command ?? ""}
                    onChange={(ev) => setEntry({ command: ev.target.value })}
                    placeholder="npx"
                  />
                  <Input
                    label="Timeout (s)"
                    type="number"
                    value={e.timeout ?? 30}
                    onChange={(ev) =>
                      setEntry({ timeout: Number(ev.target.value) })
                    }
                  />
                  <div className="sm:col-span-2">
                    <Input
                      label="Working directory"
                      value={e.cwd ?? ""}
                      onChange={(ev) => setEntry({ cwd: ev.target.value })}
                    />
                  </div>
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
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

// ─── small presentational helpers (locality: only this editor uses them) ─────

function transportLabel(t: McpTransport): string {
  if (t === "stdio") return "stdio";
  if (t === "sse") return "SSE";
  return "Streamable HTTP";
}

function StatusDot({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={
        on
          ? "h-2 w-2 shrink-0 rounded-full bg-success"
          : "h-2 w-2 shrink-0 rounded-full border border-faint"
      }
    />
  );
}

function TransportBadge({
  remote,
  label,
}: {
  remote: boolean;
  label: string;
}) {
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${
        remote ? "border-link text-link" : "border-hairline text-mute"
      }`}
    >
      {label}
    </span>
  );
}

function CategoryButton({
  active,
  title,
  desc,
  onClick,
}: {
  active: boolean;
  title: string;
  desc: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={
        active
          ? "rounded-md border border-link bg-canvas-elevated px-3 py-2 text-left"
          : "rounded-md border border-hairline bg-canvas-elevated px-3 py-2 text-left hover:border-ink"
      }
    >
      <div
        className={`text-sm font-medium ${
          active ? "text-link" : "text-ink"
        }`}
      >
        {title}
      </div>
      <div className="text-xs text-mute">{desc}</div>
    </button>
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
