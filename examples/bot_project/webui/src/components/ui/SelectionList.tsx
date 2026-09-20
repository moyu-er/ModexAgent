// SelectionList.tsx — bounded one-column checkbox selection list.
//
// Shared surface for the settings views' enable/assign lists (Skills, MCP
// servers, capability bundles): checkbox LEFT, label + description RIGHT,
// one vertical column with a fixed scroll ceiling (~20rem) so long rosters
// never grow the page. An optional filter input collapses long lists.
// Deliberately NOT a responsive grid, table, or card-per-item layout.
//
// Disabled rows render as REAL disabled checkboxes (checked or not) — never
// a fake empty square — for installed-but-not-toggleable entries and
// in-flight requests (`busyIds`) while the whole list can also be locked
// (`disabled`) during loads.

import { useMemo, useState } from "react";
import { Checkbox } from "./Checkbox";

export interface SelectionItem {
  id: string;
  label: string;
  /** Secondary line under the label. */
  description?: string;
  /** Row-level lock: rendered as a real disabled checkbox. */
  disabled?: boolean;
  /** Right-aligned tag for disabled rows (e.g. "local"). */
  note?: string;
  /** Tooltip for the row. */
  title?: string;
}

interface Props {
  items: SelectionItem[];
  checked: ReadonlySet<string>;
  onToggle: (id: string, next: boolean) => void;
  ariaLabel: string;
  /** Label for the optional filter input; omit to hide filtering. */
  searchLabel?: string;
  /** Shown when there are no items at all. */
  emptyText?: string;
  /** Lock every checkbox (e.g. while a load is in flight). */
  disabled?: boolean;
  /** Per-row in-flight requests (rendered disabled until they settle). */
  busyIds?: ReadonlySet<string>;
}

export function SelectionList({
  items,
  checked,
  onToggle,
  ariaLabel,
  searchLabel,
  emptyText,
  disabled = false,
  busyIds,
}: Props) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const visible = useMemo(
    () =>
      needle
        ? items.filter((item) => item.label.toLowerCase().includes(needle))
        : items,
    [items, needle],
  );

  if (items.length === 0 && emptyText) {
    return <p className="text-base text-mute">{emptyText}</p>;
  }

  return (
    <div>
      {searchLabel ? (
        <input
          type="search"
          value={query}
          placeholder={searchLabel}
          aria-label={searchLabel}
          onChange={(e) => setQuery(e.target.value)}
          className="mb-2 h-8 w-full rounded-sm border border-hairline bg-canvas-elevated px-2 text-sm text-ink placeholder:text-faint focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand"
        />
      ) : null}
      <ul
        aria-label={ariaLabel}
        data-testid="selection-list"
        className="max-h-80 space-y-1.5 overflow-y-auto pr-1"
      >
        {visible.map((item) => {
          const rowDisabled = disabled || item.disabled || (busyIds?.has(item.id) ?? false);
          return (
            <li
              key={item.id}
              title={item.title}
              className="flex items-center gap-3 rounded-sm border border-hairline bg-hairline-soft px-3 py-2"
            >
              <div className="min-w-0 flex-1 [overflow-wrap:anywhere] [&_p]:pl-6">
                <Checkbox
                  label={item.label}
                  helper={item.description}
                  checked={checked.has(item.id)}
                  disabled={rowDisabled}
                  onChange={(e) => onToggle(item.id, e.target.checked)}
                />
              </div>
              {item.note ? (
                <span className="ml-auto shrink-0 rounded-full border border-hairline px-1.5 py-0.5 text-xs text-mute">
                  {item.note}
                </span>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
