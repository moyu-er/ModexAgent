import type { ApprovalRequestView } from "../types/events";

interface Props {
  view: ApprovalRequestView;
  onApprove: (toolCallId: string) => void;
  onDeny: (toolCallId: string) => void;
  submitting?: boolean;
}

/** Inline approval card — status-driven: pending = amber; approved = green;
 *  denied = grey/readonly. Mono args block; semantic approve/deny buttons. */
export function ApprovalCard({ view, onApprove, onDeny, submitting }: Props) {
  const decided = view.status !== "pending";
  return (
    <div
      className={`rounded-lg border p-3 my-2 ${
        view.status === "approved"
          ? "border-green-600/50 bg-green-50 dark:bg-green-950/30"
          : view.status === "denied"
            ? "border-zinc-400/50 bg-zinc-100 dark:bg-zinc-900/40"
            : "border-amber-500/60 bg-amber-50 dark:bg-amber-950/30"
      }`}
    >
      <div className="flex items-center gap-2 text-sm font-semibold">
        <span className="rounded bg-amber-600 px-1.5 py-0.5 text-[10px] uppercase text-white">
          {view.tier}
        </span>
        <span className="font-mono">{view.tool_name}</span>
        <span className="text-xs text-zinc-500">awaiting approval</span>
      </div>
      <pre className="mt-2 overflow-x-auto rounded bg-zinc-900 p-2 text-xs text-zinc-100">
        {JSON.stringify(view.arguments, null, 2)}
      </pre>
      {!decided && (
        <div className="mt-2 flex gap-2">
          <button
            type="button"
            disabled={submitting}
            onClick={() => onApprove(view.tool_call_id)}
            className="rounded bg-green-600 px-3 py-1 text-sm font-medium text-white hover:bg-green-700 active:bg-green-800 disabled:opacity-50"
          >
            Approve
          </button>
          <button
            type="button"
            disabled={submitting}
            onClick={() => onDeny(view.tool_call_id)}
            className="rounded bg-red-600 px-3 py-1 text-sm font-medium text-white hover:bg-red-700 active:bg-red-800 disabled:opacity-50"
          >
            Deny
          </button>
        </div>
      )}
    </div>
  );
}
