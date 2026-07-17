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
import { useT } from "../../i18n";

// Protocol choice shown only under the Remote HTTP category. The two share
// fields, so this is metadata only — no data reset on change.
const PROTOCOL_DEFS: { value: string; labelKey: "settings.mcp.protocolSse" | "settings.mcp.protocolStreamable" }[] = [
  { value: "sse", labelKey: "settings.mcp.protocolSse" },
  { value: "streamableHttp", labelKey: "settings.mcp.protocolStreamable" },
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
  const t = useT();
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
    return <p className="text-sm text-error">{t("common.failedToLoad", { error: loadError })}</p>;
  }
  if (!cards) {
    return <p className="text-sm text-mute">{t("common.loading")}</p>;
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
      toast.show({ message: t("settings.mcp.serverNameRequired"), tone: "warning" });
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
      restartToast(toast, t);
    } catch (e) {
      if (renamed) {
        update(i, { name: card.originalName! });
      }
      toast.show({ message: t("settings.mcp.saveFailed", { detail: errDetail(e) }), tone: "warning" });
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
      restartToast(toast, t);
    } catch (e) {
      toast.show({
        message: t("settings.mcp.deleteFailed", { detail: errDetail(e) }),
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
          <div className="page-title">{meta.titleTerm ?? t(meta.titleKey!)}</div>
          <div className="page-sub">{t(meta.subKey)}</div>
        </div>
      </div>

      <p className="text-xs text-mute">
        {t("settings.mcp.availableToAll")}
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
            {t("settings.mcp.noServers")}
          </p>
        )}

        <button
          type="button"
          className="mt-1 flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-hairline py-2.5 text-sm text-body hover:border-ink hover:bg-hairline-soft hover:text-ink"
          onClick={addCard}
        >
          <PlusIcon /> {t("settings.mcp.addServer")}
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
  const t = useT();
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
              title={t("settings.mcp.unsavedChanges")}
              className="h-2 w-2 shrink-0 rounded-full bg-warning"
            />
          )}
          <span className="truncate text-sm font-medium text-ink">
            {card.originalName ?? (
              <span className="italic text-body">{t("settings.mcp.newServer")}</span>
            )}
          </span>
          <TransportBadge remote={isHttp} label={transportLabel(resolvedTransport, t)} />
          <span className="truncate font-mono text-xs text-body">
            {e.command || e.url || t("settings.mcp.notConfigured")}
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="ghost" size="sm" onClick={onSave}>
            {t("settings.mcp.save")}
          </Button>
          {confirming ? (
            <span className="flex items-center gap-2 text-xs">
              <Button
                variant="link"
                size="sm"
                className="font-medium text-error hover:underline"
                onClick={onConfirmDelete}
              >
                {t("settings.mcp.delete")}
              </Button>
              <Button
                variant="link"
                size="sm"
                className="text-body hover:underline"
                onClick={onCancelDelete}
              >
                {t("settings.mcp.cancel")}
              </Button>
            </span>
          ) : (
            <IconButton
              icon={<Trash2 size={16} />}
              label={t("settings.mcp.deleteServer")}
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
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Input
              label={t("settings.mcp.name")}
              required
              value={card.name}
              onChange={(ev) => onChange({ name: ev.target.value })}
            />
          </div>

          <div>
            <SectionLabel>{t("settings.mcp.transport")}</SectionLabel>
            <div className="grid grid-cols-2 gap-2">
              <CategoryButton
                active={!isHttp}
                title={t("settings.mcp.stdioTitle")}
                desc={t("settings.mcp.stdioDesc")}
                onClick={() => setCategory(false)}
              />
              <CategoryButton
                active={isHttp}
                title={t("settings.mcp.remoteHttpTitle")}
                desc={t("settings.mcp.remoteHttpDesc")}
                onClick={() => setCategory(true)}
              />
            </div>
            {isHttp && (
              <div className="mt-3">
                <Label>{t("settings.mcp.protocol")}</Label>
                <div className="flex flex-wrap gap-1.5">
                  {PROTOCOL_DEFS.map((opt) => {
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
                        {t(opt.labelKey)}
                      </button>
                    );
                  })}
                </div>
                <HelperText>
                  {t("settings.mcp.protocolHelper")}
                </HelperText>
              </div>
            )}
          </div>

          <div>
            <SectionLabel>{isHttp ? t("settings.mcp.endpoint") : t("settings.mcp.process")}</SectionLabel>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {isHttp ? (
                <>
                  <div className="sm:col-span-2">
                    <Input
                      label={t("settings.mcp.url")}
                      required
                      value={e.url ?? ""}
                      onChange={(ev) => setEntry({ url: ev.target.value })}
                      placeholder={t("settings.mcp.urlPlaceholder")}
                    />
                  </div>
                  <Input
                    label={t("settings.mcp.timeout")}
                    type="number"
                    value={e.timeout ?? 30}
                    onChange={(ev) =>
                      setEntry({ timeout: Number(ev.target.value) })
                    }
                  />
                  <div className="sm:col-span-2">
                    <KeyValueEditor
                      label={t("settings.mcp.httpHeaders")}
                      helper={t("settings.mcp.httpHeadersHelper")}
                      entries={e.headers ?? {}}
                      onChange={(headers) => setEntry({ headers })}
                    />
                  </div>
                </>
              ) : (
                <>
                  <Input
                    label={t("settings.mcp.command")}
                    required
                    value={e.command ?? ""}
                    onChange={(ev) => setEntry({ command: ev.target.value })}
                    placeholder={t("settings.mcp.commandPlaceholder")}
                  />
                  <Input
                    label={t("settings.mcp.timeout")}
                    type="number"
                    value={e.timeout ?? 30}
                    onChange={(ev) =>
                      setEntry({ timeout: Number(ev.target.value) })
                    }
                  />
                  <div className="sm:col-span-2">
                    <Input
                      label={t("settings.mcp.workingDirectory")}
                      value={e.cwd ?? ""}
                      onChange={(ev) => setEntry({ cwd: ev.target.value })}
                    />
                  </div>
                  <div className="sm:col-span-2">
                    <Textarea
                      label={t("settings.mcp.args")}
                      helper={t("settings.mcp.argsHelper")}
                      mono={false}
                      value={kvListToText(e.args ?? [])}
                      onChange={(ev) =>
                        setEntry({ args: textToKvList(ev.target.value) })
                      }
                    />
                  </div>
                  <div className="sm:col-span-2">
                    <KeyValueEditor
                      label={t("settings.mcp.envVars")}
                      helper={t("settings.mcp.envVarsHelper")}
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

function transportLabel(transport: McpTransport, t: (key: "settings.mcp.stdioTitle" | "settings.mcp.protocolSse" | "settings.mcp.protocolStreamable") => string): string {
  if (transport === "stdio") return t("settings.mcp.stdioTitle");
  if (transport === "sse") return t("settings.mcp.protocolSse");
  return t("settings.mcp.protocolStreamable");
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
