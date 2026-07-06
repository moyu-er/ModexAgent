// MCP selector for a single agent node (main or subagent). Renders the global
// MCP registry as a compact popover checklist; toggling a server adds/removes
// its name from the agent's `mcp` array. Pure UI — the parent PoolEditor owns
// persistence (mcp is just a name list stored in the pool tree, saved with the
// rest of the form via the deferred Save button).

import { useEffect, useRef, useState } from "react";
import type { McpServerEntry } from "../../types/pool";
import { getMcp } from "../../lib/mcpApi";
import { Card } from "../ui/Card";
import { Checkbox } from "../ui/Checkbox";
import { IconButton } from "../ui/IconButton";
import { ChevronDownIcon } from "../ui/icons";

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
  const containerRef = useRef<HTMLDivElement>(null);

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

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const toggle = (name: string): void => {
    onChange(
      value.includes(name)
        ? value.filter((n) => n !== name)
        : [...value, name],
    );
  };

  const header = `MCP servers (${value.length} selected)`;

  return (
    <div ref={containerRef} className="relative">
      <Card className="p-0">
        <div
          role="button"
          tabIndex={0}
          className="flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left hover:bg-hairline-soft"
          onClick={() => setOpen((v) => !v)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              setOpen((v) => !v);
            }
          }}
          aria-expanded={open}
        >
          <IconButton
            label={open ? "Collapse" : "Expand"}
            icon={<ChevronDownIcon open={open} />}
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
          />
          <span className="text-xs font-medium text-ink">{header}</span>
        </div>
      </Card>
      {open && (
        <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-64 overflow-y-auto rounded-md border border-hairline bg-canvas-elevated shadow-floating">
          <div className="px-3 py-2">
            {loadError ? (
              <p className="text-xs text-error">Failed to load: {loadError}</p>
            ) : !servers ? (
              <p className="text-xs text-mute">Loading…</p>
            ) : Object.keys(servers).length === 0 ? (
              <p className="text-xs text-mute">No global MCP servers configured.</p>
            ) : (
              <ul className="space-y-1">
                {Object.keys(servers).map((name) => (
                  <li key={name}>
                    <Checkbox
                      label={name}
                      checked={value.includes(name)}
                      onChange={() => toggle(name)}
                      aria-label={name}
                    />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
