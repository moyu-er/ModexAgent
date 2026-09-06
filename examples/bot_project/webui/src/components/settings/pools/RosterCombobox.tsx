// RosterCombobox.tsx — the pools panel's "add an entry" control: a trigger
// button opening a popover with a filter input on top of the candidate list
// (PRD Part C C2 — backend-fed candidates only, never free text). Styling
// mirrors DropdownPanel's popover (popover surface, hairline, 150ms enter).

import { useEffect, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { useT } from "../../../i18n";

interface Props {
  /** Trigger label, e.g. "Add hook". */
  label: string;
  /** Candidate names (already filtered by the caller). */
  candidates: string[];
  onPick: (name: string) => void;
  /** Shown in the panel when candidates is empty. */
  emptyText: string;
  disabled?: boolean;
}

export function RosterCombobox({ label, candidates, onPick, emptyText, disabled }: Props) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    const onPointerDown = (e: PointerEvent): void => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const matches = candidates.filter((c) =>
    c.toLowerCase().includes(filter.trim().toLowerCase()),
  );

  const pick = (name: string): void => {
    setOpen(false);
    setFilter("");
    onPick(name);
  };

  return (
    <div ref={rootRef} className="relative inline-block">
      <button
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={[
          "inline-flex h-9 items-center gap-1.5 rounded-md border border-hairline px-3 text-sm",
          "bg-canvas-elevated text-body transition-colors duration-fast ease-out",
          "hover:border-border-strong hover:text-ink",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand",
          "disabled:cursor-not-allowed disabled:opacity-45",
        ].join(" ")}
      >
        <Plus size={13} aria-hidden="true" />
        {label}
      </button>
      {open ? (
        <div className="dropdown-panel-enter absolute left-0 top-full z-50 mt-1 w-64 rounded-md border border-hairline bg-canvas-popover p-1 shadow-popover">
          <input
            ref={inputRef}
            value={filter}
            placeholder={t("settings.poolsPanel.filterPlaceholder")}
            aria-label={t("settings.poolsPanel.filterPlaceholder")}
            onChange={(e) => setFilter(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && matches[0]) pick(matches[0]);
              if (e.key === "Escape") setOpen(false);
            }}
            className="mb-1 h-8 w-full rounded-sm border border-hairline bg-canvas-elevated px-2 font-mono text-xs text-ink placeholder:text-faint focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand"
          />
          <ul role="listbox" aria-label={label} className="max-h-56 overflow-auto">
            {matches.length === 0 ? (
              <li className="px-3 py-2 text-xs text-mute">{emptyText}</li>
            ) : (
              matches.map((name) => (
                <li
                  key={name}
                  role="option"
                  aria-selected={false}
                  onClick={() => pick(name)}
                  className="flex h-8 cursor-pointer items-center rounded-sm px-3 font-mono text-sm text-ink transition-colors duration-fast ease-out hover:bg-accent"
                >
                  {name}
                </li>
              ))
            )}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
