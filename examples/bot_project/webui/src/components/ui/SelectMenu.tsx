// SelectMenu.tsx — themed popover select for inline (non-form) pickers.
//
// Why this exists: the sidebar pool picker used a native <select>. The closed
// state can be themed, but the open <option> list is rendered by the OS/browser
// — its shape, padding, highlight and scrollbar can't be styled, which read as
// "last-century Windows" next to an otherwise themed UI. This renders a real
// listbox on our themed surface so the open state matches the rest of the app.
//
// API mirrors SelectPrimitive ({ options, value, onChange }) for a drop-in
// swap. Full keyboard support: Enter/Space/↓ opens, ↑/↓ move, Enter selects,
// Esc closes, Home/End jump, type-ahead by first letter.

import { useEffect, useId, useRef, useState, type FC, type KeyboardEvent } from "react";
import { Check, ChevronDown } from "lucide-react";

export interface SelectMenuOption {
  value: string;
  label: string;
}

export interface SelectMenuProps {
  options: SelectMenuOption[];
  value: string;
  onChange: (value: string) => void;
  className?: string;
  /** Accessible label for the trigger button. */
  ariaLabel?: string;
}

export const SelectMenu: FC<SelectMenuProps> = ({
  options,
  value,
  onChange,
  className = "",
  ariaLabel,
}) => {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(() => Math.max(0, options.findIndex((o) => o.value === value)));
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const listId = useId();

  // Close on outside click / Escape is handled in onTriggerKeyDown for Esc;
  // outside click lives here.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent): void => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    return (): void => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  // Keep the highlighted option in view as it moves.
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  // Move focus to the listbox on open so arrow/Enter/Esc/type-ahead land on
  // onListKeyDown. The trigger only *opens*; it does not navigate while open.
  // Without this, the keys stay on the trigger and the list handler never runs.
  useEffect(() => {
    if (open) listRef.current?.focus();
  }, [open]);

  const select = (idx: number): void => {
    const opt = options[idx];
    if (!opt) return;
    onChange(opt.value);
    setOpen(false);
    triggerRef.current?.focus();
  };

  const onTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>): void => {
    switch (e.key) {
      case "ArrowDown":
      case "Enter":
      case " ":
        e.preventDefault();
        setActive(Math.max(0, options.findIndex((o) => o.value === value)));
        setOpen(true);
        break;
      case "ArrowUp":
        e.preventDefault();
        setActive(Math.max(0, options.length - 1, options.findIndex((o) => o.value === value)));
        setOpen(true);
        break;
      default:
        return;
    }
  };

  const onListKeyDown = (e: KeyboardEvent<HTMLUListElement>): void => {
    const n = options.length;
    if (n === 0) return;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setActive((a) => (a + 1) % n);
        break;
      case "ArrowUp":
        e.preventDefault();
        setActive((a) => (a - 1 + n) % n);
        break;
      case "Home":
        e.preventDefault();
        setActive(0);
        break;
      case "End":
        e.preventDefault();
        setActive(n - 1);
        break;
      case "Enter":
        e.preventDefault();
        select(active);
        break;
      case "Escape":
        e.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
        break;
      case "Tab":
        setOpen(false);
        break;
      default: {
        // Type-ahead: jump to the next option whose label starts with the key.
        const ch = e.key.toLowerCase();
        if (ch.length !== 1 || !/[a-z0-9]/.test(ch)) return;
        e.preventDefault();
        const from = active + 1;
        for (let i = 0; i < n; i++) {
          const idx = (from + i) % n;
          if (options[idx]?.label.toLowerCase().startsWith(ch)) {
            setActive(idx);
            break;
          }
        }
      }
    }
  };

  const selectedLabel = options.find((o) => o.value === value)?.label ?? value;

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={ariaLabel}
        onClick={(): void => {
          // Reset highlight to the current selection on each open so a stale
          // position from a previous interaction doesn't carry over.
          setActive(Math.max(0, options.findIndex((o) => o.value === value)));
          setOpen((o) => !o);
        }}
        onKeyDown={onTriggerKeyDown}
        className={[
          "flex w-full cursor-pointer items-center gap-2 rounded-md border border-hairline-strong bg-canvas-elevated",
          "py-2.5 pl-3 pr-9 text-sm font-medium text-ink shadow-card",
          "transition-colors duration-app ease-app",
          "focus:border-link focus:outline-none focus:ring-1 focus:ring-link/30",
          "hover:border-link/60 hover:bg-hairline-soft",
        ].join(" ")}
      >
        <span className="h-2 w-2 shrink-0 rounded-full bg-link/80" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate text-left">{selectedLabel}</span>
        <ChevronDown
          size={16}
          className={`pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-mute transition-transform duration-app ease-app ${open ? "rotate-180" : ""}`}
          aria-hidden="true"
        />
      </button>

      {open && (
        <ul
          ref={listRef}
          role="listbox"
          id={listId}
          tabIndex={-1}
          aria-activedescendant={open ? `${listId}-${active}` : undefined}
          onKeyDown={onListKeyDown}
          className="absolute z-40 mt-1 max-h-64 min-w-full overflow-auto rounded-md border border-hairline-strong bg-canvas-elevated p-1 shadow-popover"
        >
          {options.map((o, idx) => {
            const selected = o.value === value;
            const highlighted = idx === active;
            return (
              <li
                key={o.value}
                id={`${listId}-${idx}`}
                role="option"
                aria-selected={selected}
                data-idx={idx}
                onMouseEnter={(): void => setActive(idx)}
                onClick={(): void => select(idx)}
                className={[
                  "relative flex cursor-pointer items-center gap-2 rounded-sm px-3 py-2 text-sm",
                  "transition-colors duration-app ease-app",
                  selected ? "text-link" : "text-ink",
                  highlighted ? "bg-hairline-soft" : "",
                ].join(" ")}
              >
                {selected && (
                  <span
                    className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-link"
                    aria-hidden="true"
                  />
                )}
                <span className="min-w-0 flex-1 truncate">{o.label}</span>
                {selected && (
                  <Check size={14} className="shrink-0 text-link" aria-hidden="true" />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
};
