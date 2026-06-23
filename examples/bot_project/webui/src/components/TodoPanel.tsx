import { useMemo, useState, type FC } from "react";
import type { TodoItemDTO } from "../types/events";

const PAGE_SIZE = 5;

const STATUS_LABEL: Record<string, string> = {
  in_progress: "进行中",
  pending: "待办",
};

export interface TodoPanelProps {
  todos: TodoItemDTO[];
}

/**
 * Shows the active task list (in_progress + pending) for the selected session.
 * Hidden when empty. Paginated when more than PAGE_SIZE items.
 *
 * in_progress items sort before pending (preserving input order within each
 * group) so the currently-active work stays at the top.
 */
export const TodoPanel: FC<TodoPanelProps> = ({ todos }) => {
  const [page, setPage] = useState(0);

  const ordered = useMemo(() => {
    const rank = (s: string): number => (s === "in_progress" ? 0 : 1);
    return [...todos].sort((a, b) => rank(a.status) - rank(b.status));
  }, [todos]);

  if (ordered.length === 0) return null;

  const pageCount = Math.ceil(ordered.length / PAGE_SIZE);
  const safePage = Math.min(page, pageCount - 1);
  const slice = ordered.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);

  return (
    <div className="mx-auto mb-4 w-full min-w-[60%] rounded-xl border border-divider-light bg-ai-bubble-light/50 p-3 text-sm dark:border-divider-dark dark:bg-ai-bubble-dark/50">
      <div className="mb-2 font-semibold text-text-primary-light dark:text-text-primary-dark">
        任务清单
      </div>
      <ul className="space-y-1">
        {slice.map((t, i) => (
          <li key={`${safePage}-${i}`} className="flex items-center gap-2">
            <span
              className={
                "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold " +
                (t.status === "in_progress"
                  ? "bg-ai-brand-light text-[#ffffff] dark:bg-ai-brand-dark"
                  : "bg-sidebar-hover-light text-text-secondary-light dark:bg-sidebar-hover-dark dark:text-text-secondary-dark")
              }
            >
              {STATUS_LABEL[t.status] ?? t.status}
            </span>
            <span className="flex-1 text-ai-bubble-text-light dark:text-ai-bubble-text-dark">
              {t.content}
            </span>
          </li>
        ))}
      </ul>
      {pageCount > 1 && (
        <div className="mt-2 flex items-center justify-between text-xs text-text-secondary-light dark:text-text-secondary-dark">
          <button
            type="button"
            className="rounded px-2 py-0.5 transition-colors hover:bg-sidebar-hover-light disabled:cursor-not-allowed disabled:opacity-40 dark:hover:bg-sidebar-hover-dark"
            disabled={safePage === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            上一页
          </button>
          <span>
            {safePage + 1} / {pageCount}
          </span>
          <button
            type="button"
            className="rounded px-2 py-0.5 transition-colors hover:bg-sidebar-hover-light disabled:cursor-not-allowed disabled:opacity-40 dark:hover:bg-sidebar-hover-dark"
            disabled={safePage >= pageCount - 1}
            onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
          >
            下一页
          </button>
        </div>
      )}
    </div>
  );
};
