// DropdownPanel.tsx — the one dropdown primitive (DESIGN.md §5.3).
//
// All dropdowns in the app (form selects, sidebar pool picker, model
// selector) render through this component: a shared popover panel (popover
// surface, teal-shimmer hairline, 150ms fade + 4px slide from the trigger
// direction, 32px rows, brand-8% hover, 2px brand bar + check selection) plus
// one of two triggers — "form" (36px field with label/helper/error chrome,
// chevron rotates 180° on open) or "pill" (compact inline rounded-pill).
//
// Interaction model (standardized from the old SelectMenu): focus moves to
// the listbox on open and stays there; the active option is exposed via
// aria-activedescendant. Full keyboard support: Enter/Space/↓/↑ opens,
// ↑/↓ move (wrapping), Home/End jump, Enter selects, Esc closes and returns
// focus to the trigger, Tab closes, type-ahead by first letter. Click
// outside closes. Options may carry a `group` (sticky mono eyebrow header,
// insertion-ordered) and a `badge` (trailing brand mark, e.g. "Default").

import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type FC,
  type KeyboardEvent,
  type ReactElement,
} from "react";
import { Check, ChevronDown } from "lucide-react";
import { Label } from "./Label";
import { HelperText } from "./HelperText";
import { FieldError } from "./FieldError";

export interface DropdownOption {
  value: string;
  label: string;
  /** Optional group header (sticky eyebrow). Grouped by insertion order. */
  group?: string;
  /** Optional trailing brand mark (e.g. the "Default" model badge). */
  badge?: string;
}

export interface DropdownPanelProps {
  options: DropdownOption[];
  value: string;
  onChange: (value: string) => void;
  /** Trigger variant: form field (default) or compact inline pill. */
  variant?: "form" | "pill";
  /** Panel opens below (default) or above the trigger. */
  direction?: "up" | "down";
  /** Panel horizontal alignment against the trigger. */
  align?: "start" | "end";
  /** Accessible name for the trigger when there is no visible label. */
  ariaLabel?: string;
  /** Accessible name for the listbox. */
  listboxLabel?: string;
  /** Visible label (form variant). */
  label?: string;
  helper?: string;
  error?: string;
  required?: boolean;
  disabled?: boolean;
  /** Overrides the displayed selection text on the trigger. */
  triggerLabel?: string;
  /** Extra classes on the root wrapper. */
  className?: string;
  /** Extra classes on the trigger button. */
  triggerClassName?: string;
  /** Extra classes on the panel. */
  panelClassName?: string;
}

interface FlatItem {
  option: DropdownOption;
  index: number;
}

interface OptionGroup {
  name: string | null;
  items: FlatItem[];
}

export const DropdownPanel: FC<DropdownPanelProps> = ({
  options,
  value,
  onChange,
  variant = "form",
  direction = "down",
  align = "start",
  ariaLabel,
  listboxLabel,
  label,
  helper,
  error,
  required,
  disabled,
  triggerLabel,
  className = "",
  triggerClassName = "",
  panelClassName = "",
}) => {
  const selectedIndex = options.findIndex((o) => o.value === value);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(() => Math.max(0, selectedIndex));
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const baseId = useId();
  const listId = `${baseId}-listbox`;
  const triggerId = `${baseId}-trigger`;
  const hasError = Boolean(error);

  // Typeahead buffer: accumulates printable chars within a ~500ms window so
  // multi-letter prefixes (and CJK labels, where one keystroke = one char)
  // match correctly. Resets on open, on navigation, and after the window
  // elapses with no new key.
  const typeaheadRef = useRef<{ buffer: string; timer: ReturnType<typeof setTimeout> | null }>({
    buffer: "",
    timer: null,
  });
  const TYPEAHEAD_WINDOW_MS = 500;

  // Group consecutive options by their `group` field, preserving insertion
  // order (un-grouped options render flat under a null-named group).
  const groups = useMemo<OptionGroup[]>(() => {
    const map = new Map<string | null, FlatItem[]>();
    options.forEach((option, index) => {
      const key = option.group ?? null;
      const list = map.get(key) ?? [];
      list.push({ option, index });
      map.set(key, list);
    });
    return Array.from(map.entries()).map(([name, items]) => ({ name, items }));
  }, [options]);

  // Close on outside pointer down.
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

  // Move focus to the listbox on open so arrow/Enter/Esc/type-ahead land on
  // onListKeyDown. The trigger only *opens*; it does not navigate while open.
  // Also reset the typeahead buffer on each open so a stale prefix from a
  // prior session doesn't carry over.
  useEffect(() => {
    if (open) {
      listRef.current?.focus();
      const buf = typeaheadRef.current;
      buf.buffer = "";
      if (buf.timer) {
        clearTimeout(buf.timer);
        buf.timer = null;
      }
    }
  }, [open]);

  // Keep the highlighted option in view as it moves.
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const select = (idx: number): void => {
    const opt = options[idx];
    if (!opt) return;
    onChange(opt.value);
    setOpen(false);
    triggerRef.current?.focus();
  };

  const openPanel = (fromEnd: boolean): void => {
    // Reset highlight to the current selection on each open so a stale
    // position from a previous interaction doesn't carry over.
    const target =
      selectedIndex >= 0 ? selectedIndex : fromEnd ? options.length - 1 : 0;
    setActive(Math.max(0, target));
    setOpen(true);
  };

  const onTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>): void => {
    switch (e.key) {
      case "ArrowDown":
      case "Enter":
      case " ":
        e.preventDefault();
        openPanel(false);
        break;
      case "ArrowUp":
        e.preventDefault();
        openPanel(true);
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
        // Typeahead: accept any single printable char (e.key.length === 1,
        // no ctrl/meta) so CJK labels — where one keystroke is one character —
        // work, not just [a-z0-9]. Chars accumulate in a ~500ms buffer so a
        // multi-letter prefix (e.g. "ab" → "abort") matches; the buffer resets
        // after the window elapses. Search wraps from the active index.
        if (e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) return;
        e.preventDefault();
        const lower = e.key.toLowerCase();
        const buf = typeaheadRef.current;
        buf.buffer = (buf.buffer + lower).slice(-32);
        if (buf.timer) clearTimeout(buf.timer);
        buf.timer = setTimeout(() => {
          buf.buffer = "";
          buf.timer = null;
        }, TYPEAHEAD_WINDOW_MS);
        const needle = buf.buffer;
        const n = options.length;
        if (n === 0) return;
        // Single-char: cycle from the active index forward (repeated taps of
        // the same key advance to the next match). Multi-char prefix: narrow
        // by searching from index 0 so the accumulated prefix lands on the
        // FIRST matching option, not the one after the last single-char hit.
        const from = needle.length > 1 ? 0 : active + 1;
        for (let i = 0; i < n; i++) {
          const idx = (from + i) % n;
          if (options[idx]?.label.toLowerCase().startsWith(needle)) {
            setActive(idx);
            break;
          }
        }
      }
    }
  };

  const selectedLabel =
    triggerLabel ?? options.find((o) => o.value === value)?.label ?? value;

  const isForm = variant === "form";
  const triggerCls = [
    "flex items-center gap-1.5 border text-left",
    "transition-colors duration-fast ease-out focus:outline-none",
    isForm
      ? [
          "relative w-full rounded-sm px-3 py-2 pr-8 text-base",
          "bg-canvas-elevated text-ink",
          hasError
            ? "border-error focus:border-error focus:ring-1 focus:ring-error"
            : "border-hairline focus:border-link focus:ring-1 focus:ring-link",
          "disabled:cursor-not-allowed disabled:opacity-45",
        ].join(" ")
      : [
          "rounded-pill border-hairline bg-canvas-elevated px-3 py-1.5 text-xs text-ink",
          "hover:bg-accent focus-visible:ring-2 focus-visible:ring-link",
          "disabled:cursor-not-allowed disabled:opacity-45",
        ].join(" "),
    triggerClassName,
  ]
    .filter(Boolean)
    .join(" ");

  const chevron = (
    <ChevronDown
      size={isForm ? 14 : 12}
      aria-hidden="true"
      className={[
        "shrink-0 text-mute transition-transform duration-fast ease-out",
        open ? "rotate-180" : "",
        isForm ? "pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2" : "",
      ].join(" ")}
    />
  );

  const trigger = (
    <button
      ref={triggerRef}
      type="button"
      id={triggerId}
      disabled={disabled}
      aria-haspopup="listbox"
      aria-expanded={open}
      aria-controls={open ? listId : undefined}
      aria-label={ariaLabel}
      aria-invalid={isForm && hasError ? true : undefined}
      aria-describedby={
        isForm ? (error ? `${triggerId}-error` : helper ? `${triggerId}-helper` : undefined) : undefined
      }
      onClick={(): void => {
        if (open) {
          setOpen(false);
        } else {
          openPanel(false);
        }
      }}
      onKeyDown={onTriggerKeyDown}
      className={triggerCls}
    >
      <span className="min-w-0 flex-1 truncate">{selectedLabel}</span>
      {chevron}
    </button>
  );

  const panelCls = [
    "dropdown-panel-enter absolute z-50 min-w-full overflow-auto p-1",
    "rounded-md border border-hairline bg-canvas-popover shadow-popover",
    "max-h-64",
    direction === "up" ? "bottom-full mb-1" : "top-full mt-1",
    align === "end" ? "right-0" : "left-0",
    panelClassName,
  ]
    .filter(Boolean)
    .join(" ");

  const renderOption = ({ option, index }: FlatItem): ReactElement => {
    const selected = option.value === value;
    const highlighted = index === active;
    return (
      <li
        key={option.value}
        id={`${listId}-opt-${index}`}
        role="option"
        aria-selected={selected}
        data-idx={index}
        onMouseEnter={(): void => setActive(index)}
        onClick={(): void => select(index)}
        className={[
          "relative flex h-8 cursor-pointer items-center gap-2 rounded-sm px-3 text-base",
          "transition-colors duration-fast ease-out hover:bg-accent",
          selected ? "text-brand" : "text-ink",
          highlighted ? "bg-accent" : "",
        ].join(" ")}
      >
        {selected && (
          <span
            className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-brand"
            aria-hidden="true"
          />
        )}
        <span className="min-w-0 flex-1 truncate">{option.label}</span>
        {option.badge ? (
          <span className="ml-auto shrink-0 text-xs font-medium text-brand">
            {option.badge}
          </span>
        ) : null}
        {selected && <Check size={14} className="shrink-0 text-brand" aria-hidden="true" />}
      </li>
    );
  };

  const panel = open ? (
    <ul
      ref={listRef}
      role="listbox"
      id={listId}
      tabIndex={-1}
      aria-label={listboxLabel}
      aria-activedescendant={`${listId}-opt-${active}`}
      onKeyDown={onListKeyDown}
      className={panelCls}
      style={direction === "up" ? ({ "--dropdown-shift": "4px" } as CSSProperties) : undefined}
    >
      {groups.map(({ name, items }, groupIdx) =>
        name === null ? (
          items.map(renderOption)
        ) : (
          <div
            key={name}
            role="group"
            aria-label={name}
            className={groupIdx > 0 ? "border-t border-hairline" : ""}
          >
            <div className="sticky top-0 z-10 border-b border-hairline bg-canvas-popover px-3 py-1.5">
              <span className="font-mono text-xs font-medium uppercase tracking-eyebrow text-mute">
                {name}
              </span>
            </div>
            {items.map(renderOption)}
          </div>
        ),
      )}
    </ul>
  ) : null;

  if (!isForm) {
    return (
      <div ref={rootRef} className={`relative ${className}`.trim()}>
        {trigger}
        {panel}
      </div>
    );
  }

  return (
    <div ref={rootRef} className={`relative block ${className}`.trim()}>
      {label ? (
        <Label htmlFor={triggerId} required={required}>
          {label}
        </Label>
      ) : null}
      {trigger}
      {panel}
      {error ? (
        <FieldError id={`${triggerId}-error`}>{error}</FieldError>
      ) : helper ? (
        <HelperText id={`${triggerId}-helper`}>{helper}</HelperText>
      ) : null}
    </div>
  );
};
