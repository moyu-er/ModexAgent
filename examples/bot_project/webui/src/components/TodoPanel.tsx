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
    className="animate-spin shrink-0"
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
  >
    <circle
      cx="12"
      cy="12"
      r="9"
      stroke="#f97316"
      strokeWidth="2.5"
      strokeDasharray="42 14"
      strokeLinecap="round"
    />
  </svg>
);

/** Hollow ring — not yet started. */
const Pending: FC = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" className="shrink-0">
    <circle
      cx="12"
      cy="12"
      r="9"
      stroke="#aeaeb2"
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
        <div className="overflow-hidden rounded-2xl border border-[#e8e5e0] bg-[#faf9f7] shadow-xl dark:border-[#3a3a3c] dark:bg-[#2c2c2e]">
          {/* Header */}
          <div className="flex items-center justify-between px-4 pt-4 pb-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-[#1d1d1f] dark:text-[#f5f5f7]">
                Tasks
              </span>
              <span className="rounded-full bg-[#e8e5e0] px-2 py-0.5 text-[11px] font-medium text-[#6e6e73] dark:bg-[#3a3a3c] dark:text-[#a1a1a6]">
                {doneCount}/{todos.length}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-full p-1 text-[#aeaeb2] transition-colors hover:bg-[#e8e5e0] hover:text-[#6e6e73] dark:hover:bg-[#3a3a3c] dark:hover:text-[#a1a1a6]"
              aria-label="Close"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>

          {/* Progress bar */}
          <div className="mx-4 h-1 overflow-hidden rounded-full bg-[#e8e5e0] dark:bg-[#3a3a3c]">
            <div
              className="h-full rounded-full bg-gradient-to-r from-[#f97316] to-[#f59e0b] transition-all duration-500 ease-out"
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
                    className="flex items-start gap-2.5 rounded-lg px-2.5 py-2 transition-colors hover:bg-[#f0ede8] dark:hover:bg-[#3a3a3c]"
                  >
                    {/* Status icon */}
                    <span className="mt-[3px]">
                      {isActive ? <Spinner /> : <Pending />}
                    </span>
                    <span
                      className={
                        "min-w-0 flex-1 text-[13px] leading-snug" +
                        (isActive
                          ? " font-semibold text-[#1d1d1f] dark:text-[#f5f5f7]"
                          : " text-[#6e6e73] dark:text-[#a1a1a6]")
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
            <div className="flex items-center justify-between border-t border-[#e8e5e0] px-4 py-2.5 dark:border-[#3a3a3c]">
              <button
                type="button"
                disabled={effectivePage === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                className="rounded-lg px-3 py-1 text-[11px] font-medium text-[#6e6e73] transition-colors hover:bg-[#e8e5e0] disabled:cursor-not-allowed disabled:opacity-30 dark:text-[#a1a1a6] dark:hover:bg-[#3a3a3c]"
              >
                ← prev
              </button>
              <span className="text-[11px] tabular-nums text-[#aeaeb2]">
                {effectivePage + 1} / {pageCount}
              </span>
              <button
                type="button"
                disabled={effectivePage >= pageCount - 1}
                onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                className="rounded-lg px-3 py-1 text-[11px] font-medium text-[#6e6e73] transition-colors hover:bg-[#e8e5e0] disabled:cursor-not-allowed disabled:opacity-30 dark:text-[#a1a1a6] dark:hover:bg-[#3a3a3c]"
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
          "fixed bottom-20 right-5 z-50 flex items-center gap-2 rounded-full border border-[#f97316]/30 bg-[#faf9f7] px-3.5 py-2 shadow-lg transition-all duration-300 ease-out hover:shadow-xl dark:border-[#f97316]/20 dark:bg-[#2c2c2e]" +
          (open
            ? " translate-y-2 opacity-0 pointer-events-none"
            : " translate-y-0 opacity-100")
        }
        aria-label="Toggle task list"
      >
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[#f97316] text-[10px] font-bold text-white">
          {todos.length}
        </span>
        <span className="text-[13px] font-medium text-[#1d1d1f] dark:text-[#f5f5f7]">
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
            "text-[#aeaeb2] transition-transform duration-300" +
            (open ? " rotate-180" : "")
          }
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
    </>
  );
};
