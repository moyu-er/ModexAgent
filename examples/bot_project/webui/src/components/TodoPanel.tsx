import { useState, useEffect, useMemo, type FC } from "react";
import type { TodoItemDTO } from "../types/events";

const PAGE_SIZE = 4;

export interface TodoPanelProps {
  todos: TodoItemDTO[];
  /** Close the panel when the session changes (different session = different tasks). */
  sessionId?: string | null;
}

/* ── Inline status icons (no text labels) ──────────────────────────────── */

/** Orange spinning ring — agent is working on this item. */
const Spinner: FC = () => (
  <svg
    className="animate-spin shrink-0 text-task-accent"
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
  >
    <circle
      cx="12"
      cy="12"
      r="9"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeDasharray="42 14"
      strokeLinecap="round"
    />
  </svg>
);

/** Hollow ring — not yet started. */
const Pending: FC = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" className="shrink-0 text-task-text-faint">
    <circle
      cx="12"
      cy="12"
      r="9"
      stroke="currentColor"
      strokeWidth="2"
      opacity="0.5"
    />
  </svg>
);

/* ── Component ─────────────────────────────────────────────────────────── */

/**
 * Floating todo widget — pinned to the bottom-right of the chat view.
 *
 * Collapsed: a pill with count badge.  Expanded: a panel with progress bar,
 * paginated items, and status icons (spinner = in_progress, hollow ring =
 * pending).  Always visible — never scrolled away by streaming output.
 *
 * Uses the dedicated ``task-*`` palette tokens (defined in index.css) so no
 * hex lives in the component; light/dark flip via the CSS variables.
 */
export const TodoPanel: FC<TodoPanelProps> = ({ todos, sessionId }) => {
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
        <div className="overflow-hidden rounded-2xl border border-task-line bg-task-surface shadow-xl">
          {/* Header */}
          <div className="flex items-center justify-between px-4 pt-4 pb-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-task-text">
                Tasks
              </span>
              <span className="rounded-full bg-task-line px-2 py-0.5 text-[11px] font-medium text-task-text-muted">
                {doneCount}/{todos.length}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-full p-1 text-task-text-faint transition-colors hover:bg-task-line hover:text-task-text-muted"
              aria-label="Close"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>

          {/* Progress bar */}
          <div className="mx-4 h-1 overflow-hidden rounded-full bg-task-line">
            <div
              className="h-full rounded-full bg-gradient-to-r from-task-accent to-task-accent-2 transition-all duration-500 ease-out"
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
                    className="flex items-start gap-2.5 rounded-lg px-2.5 py-2 transition-colors hover:bg-task-surface-hover"
                  >
                    {/* Status icon */}
                    <span className="mt-[3px]">
                      {isActive ? <Spinner /> : <Pending />}
                    </span>
                    <span
                      className={
                        "min-w-0 flex-1 text-[13px] leading-snug" +
                        (isActive
                          ? " font-semibold text-task-text"
                          : " text-task-text-muted")
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
            <div className="flex items-center justify-between border-t border-task-line px-4 py-2.5">
              <button
                type="button"
                disabled={effectivePage === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                className="rounded-lg px-3 py-1 text-[11px] font-medium text-task-text-muted transition-colors hover:bg-task-line disabled:cursor-not-allowed disabled:opacity-30"
              >
                ← prev
              </button>
              <span className="text-[11px] tabular-nums text-task-text-faint">
                {effectivePage + 1} / {pageCount}
              </span>
              <button
                type="button"
                disabled={effectivePage >= pageCount - 1}
                onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                className="rounded-lg px-3 py-1 text-[11px] font-medium text-task-text-muted transition-colors hover:bg-task-line disabled:cursor-not-allowed disabled:opacity-30"
              >
                next →
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
          "fixed bottom-20 right-5 z-50 flex items-center gap-2 rounded-full border border-task-accent/30 bg-task-surface px-3.5 py-2 shadow-lg transition-all duration-300 ease-out hover:shadow-xl dark:border-task-accent/20" +
          (open
            ? " translate-y-2 opacity-0 pointer-events-none"
            : " translate-y-0 opacity-100")
        }
        aria-label="Toggle task list"
      >
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-task-accent text-[10px] font-bold text-white">
          {todos.length}
        </span>
        <span className="text-[13px] font-medium text-task-text">
          Tasks
        </span>
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          className={
            "text-task-text-faint transition-transform duration-300" +
            (open ? " rotate-180" : "")
          }
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
    </>
  );
};
