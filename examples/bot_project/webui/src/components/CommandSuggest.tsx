// Generic slash-command autocomplete dropdown for the chat composer.
//
// Renders SuggestionItem[] — each item carries a category label ("skill:",
// "command:", etc.), a name, and an optional description. The category label
// gets its own accent color so the user can distinguish types at a glance.
//
// This component is presentational only: it renders the filtered list and
// handles mouse interaction. Keyboard navigation (arrows / Enter / Tab /
// Escape) is driven by the owning textarea's onKeyDown, which has focus.
//
// Row layout: single line, no wrap. `category:/name` left (mono, colored),
// description right (truncate, whitespace-nowrap).

import { useEffect, useRef, type FC } from "react";
import type { SuggestionCategory, SuggestionItem } from "../types/suggestion";
import { useT } from "../i18n";

const CATEGORY_COLORS: Record<SuggestionCategory, string> = {
  skill: "var(--color-cat-skills)",
  command: "var(--color-cat-pools)",
};

export interface CommandSuggestProps {
  matches: SuggestionItem[];
  active: number;
  onActiveChange: (idx: number) => void;
  onChoose: (idx: number) => void;
  direction?: "up" | "down";
}

export const CommandSuggest: FC<CommandSuggestProps> = ({
  matches,
  active,
  onActiveChange,
  onChoose,
  direction = "down",
}) => {
  const t = useT();
  const listRef = useRef<HTMLUListElement | null>(null);

  useEffect(() => {
    const el = listRef.current?.children[active] as HTMLElement | undefined;
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (matches.length === 0) return null;

  const posClass = direction === "up" ? "bottom-full mb-1.5" : "top-full mt-1.5";

  return (
    <ul
      ref={listRef}
      role="listbox"
      aria-label={t("chat.skillSuggest.ariaLabel")}
      className={`dropdown-panel-enter absolute left-0 right-0 z-20 ${posClass} max-h-64 overflow-y-auto rounded-lg border border-hairline bg-canvas-popover shadow-popover`}
    >
      {matches.map((s, i) => {
        const isActive = i === active;
        const catColor = CATEGORY_COLORS[s.category] ?? "var(--color-cat-skills)";
        return (
          <li
            key={`${s.category}:${s.name}`}
            role="option"
            aria-selected={isActive}
            onMouseEnter={(): void => onActiveChange(i)}
            onMouseDown={(e): void => {
              e.preventDefault();
              onChoose(i);
            }}
            className={
              "flex cursor-pointer items-baseline gap-2 px-3 py-2 text-sm transition-colors duration-fast " +
              (isActive ? "bg-brand-soft" : "hover:bg-hairline-soft")
            }
          >
            <span className="flex shrink-0 items-baseline gap-1 font-mono text-[13px]">
              <span style={{ color: catColor }}>{s.category}:</span>
              <span className={isActive ? "text-brand-bright" : "text-brand"}>
                {s.name}
              </span>
            </span>
            {s.description ? (
              <span className="min-w-0 flex-1 truncate text-body text-xs whitespace-nowrap">
                {s.description}
              </span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
};

// ── Pure helpers (exported for unit tests + reused by the owner) ────────────

export function activeQuery(input: string, caret: number): string | null {
  if (!input.startsWith("/")) return null;
  const upto = input.slice(0, caret);
  if (upto.indexOf(" ") !== -1) return null;
  return upto.slice(1);
}

export function filterSuggestions(
  items: SuggestionItem[],
  query: string,
): SuggestionItem[] {
  const q = query.toLowerCase();
  if (!q) return items;
  return items.filter((s) => s.name.toLowerCase().startsWith(q));
}

export function buildInsertion(
  input: string,
  caret: number,
  name: string,
): { input: string; caret: number } {
  const after = input.slice(caret);
  const prefix = `/${name} `;
  return { input: prefix + after, caret: prefix.length };
}

export interface SuggestKeyboardState {
  open: boolean;
  count: number;
  active: number;
}

export function handleSuggestKey(
  key: string,
  state: SuggestKeyboardState,
  callbacks: {
    setActive: (idx: number) => void;
    choose: (idx: number) => void;
    close: () => void;
  },
): boolean {
  if (!state.open) return false;
  if (key === "ArrowDown") {
    callbacks.setActive((state.active + 1) % state.count);
    return true;
  }
  if (key === "ArrowUp") {
    callbacks.setActive((state.active - 1 + state.count) % state.count);
    return true;
  }
  if (key === "Enter" || key === "Tab") {
    callbacks.choose(state.active);
    return true;
  }
  // Escape falls through — the dropdown is input-driven and cannot be
  // dismissed without destroying the user's input. The browser handles
  // blur/clear; the menu hides when input changes or focus is lost.
  return false;
}
