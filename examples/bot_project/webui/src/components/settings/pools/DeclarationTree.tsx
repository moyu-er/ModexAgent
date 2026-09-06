// DeclarationTree.tsx — the pools panel's left column: workspace → pools →
// agents, expandable, selection-driven, with node-type affordances (add pool,
// add subagent, delete) and issue markers fed by the last rejected save.
//
// New nodes are named inline: the tree owns a small "pending name input"
// state and validates non-empty + unique-within-pool before emitting the
// create callback.

import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Plus, Trash2 } from "lucide-react";
import { useT } from "../../../i18n";
import { IconButton } from "../../ui/IconButton";
import {
  agentNodeId,
  poolNodeId,
  WORKSPACE_NODE_ID,
  type AgentTreeNode,
  type ModelView,
} from "./scopeModel";

export type PendingName =
  | { kind: "pool" }
  | { kind: "agent"; pool: string; parentPath: string[] };

interface Props {
  view: ModelView;
  selection: string | null;
  issueNodeIds: ReadonlySet<string>;
  onSelect: (nodeId: string) => void;
  onCreatePool: (name: string) => void;
  onCreateAgent: (pool: string, parentPath: string[], name: string) => void;
  onDelete: (nodeId: string) => void;
}

const ROW_CLS =
  "flex min-h-9 w-full items-center gap-1.5 rounded-sm px-2 py-1 text-left text-base transition-colors duration-fast ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand";

export function DeclarationTree({
  view,
  selection,
  issueNodeIds,
  onSelect,
  onCreatePool,
  onCreateAgent,
  onDelete,
}: Props) {
  const t = useT();
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [pending, setPending] = useState<PendingName | null>(null);
  const [nameError, setNameError] = useState<string>("");

  const toggle = (id: string): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const isExpanded = (id: string): boolean => !expanded.has(id); // default open

  const commitName = (raw: string, siblings: string[], create: (name: string) => void): void => {
    const name = raw.trim();
    if (!name) {
      setNameError(t("settings.poolsPanel.nameRequired"));
      return;
    }
    if (siblings.includes(name)) {
      setNameError(t("settings.poolsPanel.nameTaken", { name }));
      return;
    }
    setPending(null);
    setNameError("");
    create(name);
  };

  const nameInput = (placeholder: string, siblings: string[], create: (name: string) => void) => (
    <PendingNameInput
      placeholder={placeholder}
      error={nameError}
      onCommit={(raw) => commitName(raw, siblings, create)}
      onCancel={() => {
        setPending(null);
        setNameError("");
      }}
    />
  );

  const rowState = (id: string): string =>
    [
      ROW_CLS,
      id === selection ? "bg-accent text-ink" : "text-body hover:bg-hairline-soft",
    ].join(" ");

  const issueDot = (id: string) =>
    issueNodeIds.has(id) ? (
      <span
        className="h-1.5 w-1.5 shrink-0 rounded-full bg-danger"
        title={t("settings.poolsPanel.hasIssues")}
        aria-label={t("settings.poolsPanel.hasIssues")}
      />
    ) : null;

  const renderAgent = (pool: string, node: AgentTreeNode, depth: number) => {
    const id = agentNodeId(pool, node.path);
    const isRoot = node.path.length === 1;
    const open = isExpanded(id);
    return (
      <li key={id}>
        <div className="group flex items-center" style={{ paddingLeft: `${depth * 14}px` }}>
          {node.children.length > 0 ? (
            <button
              type="button"
              aria-label={open ? t("sessionTree.collapse") : t("sessionTree.expand")}
              aria-expanded={open}
              onClick={() => toggle(id)}
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm text-mute transition-colors duration-fast hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
            >
              {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            </button>
          ) : (
            <span className="h-6 w-6 shrink-0" aria-hidden="true" />
          )}
          <button type="button" className={rowState(id)} onClick={() => onSelect(id)}>
            <span className="min-w-0 flex-1 truncate font-mono text-sm">{node.name}</span>
            {isRoot ? (
              <span className="shrink-0 rounded-pill border border-hairline px-1.5 py-px text-xs text-mute">
                {t("settings.poolsPanel.rootBadge")}
              </span>
            ) : null}
            {issueDot(id)}
          </button>
          <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity duration-fast group-hover:opacity-100 group-focus-within:opacity-100">
            <IconButton
              icon={<Plus size={13} />}
              label={t("settings.poolsPanel.addSubagent", { name: node.name })}
              variant="ghost"
              size="sm"
              onClick={() => {
                setPending({ kind: "agent", pool, parentPath: node.path });
                setNameError("");
              }}
            />
            <IconButton
              icon={<Trash2 size={13} />}
              label={t("settings.poolsPanel.deleteNode", { name: node.name })}
              variant="ghost"
              size="sm"
              onClick={() => onDelete(id)}
            />
          </span>
        </div>
        {pending?.kind === "agent" &&
        pending.pool === pool &&
        pending.parentPath.join("/") === node.path.join("/") ? (
          <div style={{ paddingLeft: `${(depth + 1) * 14 + 24}px` }} className="py-1 pr-2">
            {nameInput(
              t("settings.poolsPanel.newAgentPlaceholder"),
              node.children.map((c) => c.name),
              (name) => onCreateAgent(pool, node.path, name),
            )}
          </div>
        ) : null}
        {open && node.children.length > 0 ? (
          <ul>{node.children.map((c) => renderAgent(pool, c, depth + 1))}</ul>
        ) : null}
      </li>
    );
  };

  const poolNames = view.pools.map((p) => p.name);

  return (
    <nav aria-label={t("settings.poolsPanel.treeLabel")} className="flex h-full flex-col">
      <ul className="min-h-0 flex-1 space-y-0.5 overflow-auto p-2">
        {view.workspaceBody ? (
          <li>
            <div className="group flex items-center">
              <span className="h-6 w-6 shrink-0" aria-hidden="true" />
              <button
                type="button"
                className={rowState(WORKSPACE_NODE_ID)}
                onClick={() => onSelect(WORKSPACE_NODE_ID)}
              >
                <span className="min-w-0 flex-1 truncate font-mono text-sm font-semibold">
                  {view.workspaceName ?? t("settings.poolsPanel.workspace")}
                </span>
                {issueDot(WORKSPACE_NODE_ID)}
              </button>
              {!view.poolAsRoot ? (
                <span className="shrink-0 opacity-0 transition-opacity duration-fast group-hover:opacity-100 group-focus-within:opacity-100">
                  <IconButton
                    icon={<Plus size={13} />}
                    label={t("settings.poolsPanel.addPool")}
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setPending({ kind: "pool" });
                      setNameError("");
                    }}
                  />
                </span>
              ) : null}
            </div>
            {pending?.kind === "pool" ? (
              <div className="py-1 pl-8 pr-2">
                {nameInput(t("settings.poolsPanel.newPoolPlaceholder"), poolNames, onCreatePool)}
              </div>
            ) : null}
          </li>
        ) : null}
        {view.pools.map((pool) => {
          const id = poolNodeId(pool.name);
          const open = isExpanded(id);
          return (
            <li key={id}>
              <div className="group flex items-center" style={{ paddingLeft: view.poolAsRoot ? 0 : 14 }}>
                <button
                  type="button"
                  aria-label={open ? t("sessionTree.collapse") : t("sessionTree.expand")}
                  aria-expanded={open}
                  onClick={() => toggle(id)}
                  className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm text-mute transition-colors duration-fast hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                >
                  {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                </button>
                <button type="button" className={rowState(id)} onClick={() => onSelect(id)}>
                  <span className="min-w-0 flex-1 truncate font-mono text-sm font-medium">
                    {pool.name}
                  </span>
                  {issueDot(id)}
                </button>
                {!view.poolAsRoot ? (
                  <span className="shrink-0 opacity-0 transition-opacity duration-fast group-hover:opacity-100 group-focus-within:opacity-100">
                    <IconButton
                      icon={<Trash2 size={13} />}
                      label={t("settings.poolsPanel.deleteNode", { name: pool.name })}
                      variant="ghost"
                      size="sm"
                      onClick={() => onDelete(id)}
                    />
                  </span>
                ) : null}
              </div>
              {open ? <ul>{pool.agents.map((a) => renderAgent(pool.name, a, 2))}</ul> : null}
            </li>
          );
        })}
        {view.pools.length === 0 ? (
          <li className="px-3 py-2 text-sm text-mute">{t("settings.poolsPanel.emptyTree")}</li>
        ) : null}
      </ul>
      {!view.poolAsRoot ? (
        <div className="border-t border-hairline p-2">
          <button
            type="button"
            onClick={() => {
              setPending({ kind: "pool" });
              setNameError("");
            }}
            className="flex min-h-9 w-full items-center gap-1.5 rounded-sm px-2 text-sm text-body transition-colors duration-fast ease-out hover:bg-hairline-soft hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
          >
            <Plus size={13} aria-hidden="true" />
            {t("settings.poolsPanel.addPool")}
          </button>
        </div>
      ) : null}
    </nav>
  );
}

function PendingNameInput({
  placeholder,
  error,
  onCommit,
  onCancel,
}: {
  placeholder: string;
  error: string;
  onCommit: (raw: string) => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState("");
  const t = useT();

  useEffect(() => {
    ref.current?.focus();
  }, []);

  return (
    <div>
      <input
        ref={ref}
        value={value}
        placeholder={placeholder}
        aria-label={placeholder}
        aria-invalid={Boolean(error) || undefined}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") onCommit(value);
          if (e.key === "Escape") onCancel();
        }}
        className={[
          "h-9 w-full rounded-sm border px-2 font-mono text-sm",
          "bg-canvas-elevated text-ink placeholder:text-faint",
          error
            ? "border-danger focus:border-danger focus:ring-danger"
            : "border-hairline focus:border-brand focus:ring-brand",
          "focus:outline-none focus:ring-2",
        ].join(" ")}
      />
      {error ? <p className="mt-1 text-xs text-danger">{error}</p> : null}
      <p className="mt-1 text-xs text-faint">{t("settings.poolsPanel.nameInputHelper")}</p>
    </div>
  );
}
