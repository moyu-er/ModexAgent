// MCP selector for a single agent node (main or subagent). Renders the global
// MCP registry as a collapsible checkbox list; toggling a server adds/removes
// its name from the agent's `mcp` array. Pure UI — the parent PoolEditor owns
// persistence (mcp is just a name list stored in the pool tree, saved with the
// rest of the form via the deferred Save button).

import { useEffect, useState } from "react";
import type { McpServerEntry } from "../../types/pool";
import { getMcp } from "../../lib/mcpApi";
import { Chevron } from "./icons";

interface Props {
  /** Currently selected server names (the agent's `mcp` array). */
  value: string[];
  onChange: (next: string[]) => void;
}

export function AgentMcpSelector({ value, onChange }: Props) {
  const [open, setOpen] = useState<boolean>(false);
  const [servers, setServers] = useState<Record<string, McpServerEntry> | null>(
    null,
  );
  const [loadError, setLoadError] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    getMcp()
      .then((map) => {
        if (!cancelled) setServers(map);
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const toggle = (name: string): void => {
    onChange(
      value.includes(name)
        ? value.filter((n) => n !== name)
        : [...value, name],
    );
  };

  const header = `MCP servers (${value.length} selected)`;

  return (
    <div className="rounded-md border border-divider bg-sidebar-bg">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-sidebar-hover"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Chevron open={open} />
        <span className="text-xs font-medium text-text-primary">{header}</span>
      </button>
      {open ? (
        <div className="border-t border-divider px-3 py-2">
          {loadError ? (
            <p className="text-xs text-error">Failed to load: {loadError}</p>
          ) : !servers ? (
            <p className="text-xs text-text-secondary">Loading…</p>
          ) : Object.keys(servers).length === 0 ? (
            <p className="text-xs text-text-secondary">
              No global MCP servers configured.
            </p>
          ) : (
            <ul className="space-y-1">
              {Object.entries(servers).map(([name, entry]) => {
                const checked = value.includes(name);
                return (
                  <li key={name}>
                    <label className="flex cursor-pointer items-center gap-2 text-xs text-text-primary">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggle(name)}
                        aria-label={name}
                        className="h-3.5 w-3.5"
                      />
                      <span className="truncate font-medium">{name}</span>
                      <span className="truncate text-text-secondary">
                        {entry.transport ?? "—"}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
