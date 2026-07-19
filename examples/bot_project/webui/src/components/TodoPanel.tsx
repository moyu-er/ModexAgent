import { useState, useEffect, useMemo, type FC } from "react";
import type { TodoItemDTO } from "../types/events";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  SpinnerIcon,
  CircleRingIcon,
  XIcon,
  ChevronDownIcon,
} from "./ui/icons";
import { useT } from "../i18n";

const PAGE_SIZE = 4;

export interface TodoPanelProps {
  todos: TodoItemDTO[];
  /** Close the panel when the session changes (different session = different tasks). */
  sessionId?: string | null;
}

/* ── Component ─────────────────────────────────────────────────────────── */

/**
 * Floating todo widget — pinned to the bottom-right of the chat view.
 *
 * Collapsed: a pill with count badge.  Expanded: a panel with progress bar,
 * paginated items, and status icons (spinner = in_progress, hollow ring =
 * pending).  Always visible — never scrolled away by streaming output.
 *
 * Uses Inter surface tokens with a warm `warning` accent so the panel feels
 * neutral but not cold; light/dark flip via the CSS variables.
 */
export const TodoPanel: FC<TodoPanelProps> = ({ todos, sessionId }) => {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(0);

  // Close the panel when the session changes — tasks belong to a different
  // session and staying open would briefly show stale data.
  useEffect(() => {
    setOpen(false);
  }, [sessionId]);

  const doneCount = useMemo(
    () => todos.filter((t) => t.status === "completed").length,
    [todos],
  );
  const pct = useMemo(
    () => Math.round((doneCount / todos.length) * 100),
    [doneCount, todos.length],
  );

  if (todos.length === 0) return null;

  const pageCount = Math.max(1, Math.ceil(todos.length / PAGE_SIZE));
  const effectivePage = Math.min(page, pageCount - 1);
  const slice = todos.slice(
    effectivePage * PAGE_SIZE,
    effectivePage * PAGE_SIZE + PAGE_SIZE,
  );

  return (
    <>
      {/* ── Expanded panel ──────────────────────────────────────────── */}
      <div
        className={
          "fixed bottom-24 right-5 z-50 w-80 transition-all duration-300 ease-out" +
          (open
            ? " translate-y-0 opacity-100"
            : " translate-y-2 opacity-0 pointer-events-none")
        }
      >
        <div className="overflow-hidden rounded-2xl border border-hairline bg-canvas-elevated shadow-floating">
          {/* Header */}
          <div className="flex items-center justify-between px-4 pt-4 pb-2">
            <div className="flex items-center gap-2">
              <span className="text-base font-semibold text-ink">
                {t("todo.tasks")}
              </span>
              <span className="rounded-full bg-hairline px-2 py-0.5 text-xs font-medium text-body">
                {doneCount}/{todos.length}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-full p-1 text-mute transition-colors hover:bg-hairline-soft hover:text-body"
              aria-label={t("todo.close")}
            >
              <XIcon className="h-3.5 w-3.5" />
            </button>
          </div>

          {/* Progress bar */}
          <div className="mx-4 h-1 overflow-hidden rounded-full bg-hairline">
            <div
              className="h-full rounded-full bg-gradient-to-r from-ember to-warning transition-all duration-500 ease-out"
              style={{ width: `${pct}%` }}
            />
          </div>

          {/* Items */}
          <div className="max-h-[280px] overflow-y-auto px-4 py-2">
            <ul className="space-y-1">
              {slice.map((t, i) => {
                const isActive = t.status === "in_progress";
                return (
                  <li
                    key={`${t.content}-${i}`}
                    className="flex items-start gap-2.5 rounded-lg px-2.5 py-2 transition-colors hover:bg-hairline-soft"
                  >
                    {/* Status icon */}
                    <span className="mt-[3px]">
                      {isActive ? <SpinnerIcon className="text-warning" /> : <CircleRingIcon className="text-mute" />}
                    </span>
                    <span
                      className={
                        "min-w-0 flex-1 text-base leading-snug" +
                        (isActive
                          ? " font-semibold text-ink"
                          : " text-body")
                      }
                    >
                      {t.content}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>

          {/* Pagination */}
          {pageCount > 1 && (
            <div className="flex items-center justify-between border-t border-hairline px-4 py-2.5">
              <button
                type="button"
                disabled={effectivePage === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                aria-label={t("todo.previousPage")}
                className="flex items-center gap-1 rounded-lg px-3 py-1 text-xs font-medium text-body transition-colors hover:bg-hairline-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning disabled:cursor-not-allowed disabled:opacity-45"
              >
                <ChevronLeftIcon className="h-3 w-3" />
                {t("todo.prev")}
              </button>
              <span className="text-xs tabular-nums text-mute">
                {effectivePage + 1} / {pageCount}
              </span>
              <button
                type="button"
                disabled={effectivePage >= pageCount - 1}
                onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                aria-label={t("todo.nextPage")}
                className="flex items-center gap-1 rounded-lg px-3 py-1 text-xs font-medium text-body transition-colors hover:bg-hairline-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning disabled:cursor-not-allowed disabled:opacity-45"
              >
                {t("todo.next")}
                <ChevronRightIcon className="h-3 w-3" />
              </button>
            </div>
          )}
        </div>
      </div>

      {/* ── Collapsed pill ──────────────────────────────────────────── */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={
          "fixed bottom-20 right-5 z-50 flex items-center gap-2 rounded-full border border-warning bg-canvas-elevated px-3.5 py-2 shadow-floating transition-all duration-300 ease-out" +
          (open
            ? " translate-y-2 opacity-0 pointer-events-none"
            : " translate-y-0 opacity-100")
        }
        aria-label={t("todo.toggleTaskList")}
      >
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-warning text-xs font-bold text-canvas">
          {todos.length}
        </span>
        <span className="text-base font-medium text-ink">
          {t("todo.tasks")}
        </span>
        <ChevronDownIcon
          className={`h-3 w-3 text-mute transition-transform duration-300 ${open ? "rotate-180" : ""}`}
        />
      </button>
    </>
  );
};
