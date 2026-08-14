// Shared bits for the graph views: status → token-colored badge, status →
// i18n label key, and ApiError detail formatting (backend 400s carry
// {"error": ..., "detail": ...}).

import type { FC } from "react";
import { ApiError } from "../../lib/api";
import type { MessageKey } from "../../i18n";

const STATUS_CLS: Record<string, string> = {
  pending: "text-mute border-hairline",
  running: "text-brand border-brand",
  paused: "text-warning border-warning",
  stopped: "text-faint border-hairline line-through",
  crashed: "text-danger border-danger",
  completed: "text-success border-success",
  failed: "text-danger border-danger",
};

const STATUS_LABEL_KEYS: Record<string, MessageKey> = {
  pending: "graphs.statusPending",
  running: "graphs.statusRunning",
  paused: "graphs.statusPaused",
  stopped: "graphs.statusStopped",
  crashed: "graphs.statusCrashed",
  completed: "graphs.statusCompleted",
  failed: "graphs.statusFailed",
};

export function statusLabelKey(status: string): MessageKey {
  return STATUS_LABEL_KEYS[status] ?? "graphs.status";
}

export const GraphStatusBadge: FC<{ status: string; label: string }> = ({
  status,
  label,
}) => {
  const cls = STATUS_CLS[status] ?? "text-mute border-hairline";
  return (
    <span
      className={`inline-flex items-center rounded-sm border px-1.5 py-0.5 font-mono text-xs ${cls}`}
    >
      {label}
    </span>
  );
};

/**
 * Human-readable error line for graph REST failures. Backend validation 400s
 * return a JSON body {"error": ..., "detail": ...}; prefer those fields over
 * the raw body when parseable.
 */
export function formatGraphApiError(err: unknown): string {
  if (err instanceof ApiError && err.detail) {
    try {
      const body = JSON.parse(err.detail) as { error?: string; detail?: unknown };
      if (body.error) {
        const detail =
          typeof body.detail === "string"
            ? body.detail
            : body.detail !== undefined
              ? JSON.stringify(body.detail)
              : "";
        return detail ? `${body.error}: ${detail}` : body.error;
      }
    } catch {
      // Not JSON — fall through to the generic message.
    }
  }
  return err instanceof Error ? err.message : String(err);
}
