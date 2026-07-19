import { useState, useEffect, useCallback, useMemo, useRef, type FC } from "react";
import { createPortal } from "react-dom";
import {
  fetchProviderModels,
  ApiError,
  type FetchedModel,
  type FetchProviderModelsRequest,
} from "../../lib/api";
import { XIcon } from "../ui/icons";
import { IconButton } from "../ui/IconButton";
import { Button } from "../ui/Button";
import { useT, type TFn } from "../../i18n";

export interface FetchModelsModalProps {
  open: boolean;
  onClose: () => void;
  /** Form A (provider_key) or Form B (inline connection info). */
  fetchRequest: FetchProviderModelsRequest;
  existingModelIds: Set<string>;
  onImport: (models: FetchedModel[]) => void;
}

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "success"; models: FetchedModel[] };

/** Label shown in the modal header — `provider_key` for Form A, `base_url` for Form B. */
function displayLabel(req: FetchProviderModelsRequest): string {
  if ("provider_key" in req) return req.provider_key;
  return req.base_url;
}

function describeError(err: unknown, t: TFn): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) return t("settings.modelsFetch.errAuth");
    if (err.status === 404) return t("settings.modelsFetch.errNotFound");
    if (err.status === 502) {
      if (err.detail.includes("All candidates failed"))
        return t("settings.modelsFetch.errNoEndpoint");
      if (err.detail.includes("timed out")) return t("settings.modelsFetch.errTimeout");
      if (err.detail.includes("0 models")) return t("settings.modelsFetch.errZeroModels");
      return err.detail;
    }
    return t("settings.modelsFetch.errFailed", { status: err.status });
  }
  return t("settings.modelsFetch.errNetwork");
}

export const FetchModelsModal: FC<FetchModelsModalProps> = ({
  open,
  onClose,
  fetchRequest,
  existingModelIds,
  onImport,
}) => {
  const t = useT();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const fetchSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++fetchSeq.current;
    setState({ kind: "loading" });
    setSelected(new Set());
    try {
      const result = await fetchProviderModels(fetchRequest);
      if (seq !== fetchSeq.current) return;
      setState({ kind: "success", models: result.models });
    } catch (err) {
      if (seq !== fetchSeq.current) return;
      setState({ kind: "error", message: describeError(err, t) });
    }
  }, [fetchRequest, t]);

  useEffect(() => {
    if (open) {
      load();
    }
  }, [open, load]);

  const filteredModels = useMemo(() => {
    if (state.kind !== "success") return [];
    if (!query.trim()) return state.models;
    const q = query.toLowerCase();
    return state.models.filter(
      (m) =>
        m.id.toLowerCase().includes(q) ||
        (m.owned_by?.toLowerCase().includes(q) ?? false),
    );
  }, [state, query]);

  const grouped = useMemo(() => {
    const groups: Record<string, FetchedModel[]> = {};
    for (const m of filteredModels) {
      const vendor = m.owned_by || "Other";
      (groups[vendor] ??= []).push(m);
    }
    return Object.entries(groups).sort((a, b) => a[0].localeCompare(b[0]));
  }, [filteredModels]);

  const toggle = (id: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleImport = (): void => {
    if (state.kind !== "success") return;
    const chosen = state.models.filter((m) => selected.has(m.id));
    onImport(chosen);
    onClose();
  };

  if (!open) return null;

  const isLoading = state.kind === "loading";
  const isError = state.kind === "error";
  const models = state.kind === "success" ? state.models : [];

  return createPortal(
    <div
      className="modal-scrim-enter fixed inset-0 z-50 flex items-center justify-center bg-overlay"
      onClick={onClose}
      onKeyDown={(e): void => {
        if (e.key === "Escape") onClose();
      }}
      role="presentation"
    >
      <div
        className="modal-panel-enter flex w-[560px] max-w-[90vw] max-h-[75vh] flex-col rounded-lg border border-hairline bg-canvas-popover shadow-popover"
        onClick={(e): void => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-hairline px-4 py-3">
          <h3 className="text-base font-semibold text-ink">
            {t("settings.modelsFetch.title")}
            <span className="ml-2 font-mono text-xs text-body">{displayLabel(fetchRequest)}</span>
          </h3>
          <IconButton
            icon={<XIcon />}
            label={t("settings.modelsFetch.close")}
            onClick={onClose}
            variant="ghost"
            size="sm"
          />
        </div>

        {state.kind === "success" && (
          <div className="shrink-0 border-b border-hairline px-4 py-2">
            <input
              type="text"
              placeholder={t("settings.modelsFetch.searchPlaceholder")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="h-9 w-full rounded-sm border border-hairline bg-canvas-elevated px-3 text-base text-ink placeholder:text-faint focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand"
            />
          </div>
        )}

        <div className="min-h-[240px] flex-1 overflow-y-auto px-2 py-2">
          {isLoading && (
            <div className="flex items-center justify-center py-8">
              <span className="text-base text-body">{t("settings.modelsFetch.fetching")}</span>
            </div>
          )}

          {isError && state.kind === "error" && (
            <div className="flex flex-col items-center gap-3 py-8">
              <p className="px-4 text-center text-base text-error">{state.message}</p>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => load()}
              >
                {t("settings.modelsFetch.retry")}
              </Button>
            </div>
          )}

          {state.kind === "success" && models.length === 0 && (
            <p className="px-2 py-8 text-center text-xs text-faint">
              {t("settings.modelsFetch.empty")}
            </p>
          )}

          {state.kind === "success" && filteredModels.length === 0 && models.length > 0 && (
            <p className="px-2 py-4 text-center text-xs text-faint">
              {t("settings.modelsFetch.noMatch")}
            </p>
          )}

          {grouped.map(([vendor, items]) => (
            <div key={vendor} className="mb-2">
              <div className="px-3 py-1 text-xs font-semibold uppercase tracking-eyebrow text-mute">
                {vendor} ({items.length})
              </div>
              {items.map((m) => {
                const exists = existingModelIds.has(m.id);
                const checked = selected.has(m.id);
                return (
                  <label
                    key={m.id}
                    className={`flex items-center gap-2.5 rounded px-3 py-1.5 text-base transition-colors ${
                      exists
                        ? "cursor-default opacity-50"
                        : "cursor-pointer hover:bg-hairline-soft"
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={exists || checked}
                      disabled={exists}
                      onChange={() => toggle(m.id)}
                      className="h-4 w-4 rounded border-hairline accent-link"
                    />
                    <span className="truncate font-mono text-xs text-ink">{m.id}</span>
                    {exists && (
                      <span className="ml-auto shrink-0 text-xs text-mute">{t("settings.modelsFetch.alreadyAdded")}</span>
                    )}
                  </label>
                );
              })}
            </div>
          ))}
        </div>

        {state.kind === "success" && (
          <div className="flex shrink-0 items-center justify-between border-t border-hairline px-4 py-3">
            <span className="text-xs text-body">
              {t("settings.modelsFetch.selectedCount", { selected: selected.size, total: models.length })}
            </span>
            <div className="flex shrink-0 gap-2">
              <Button
                type="button"
                onClick={onClose}
                variant="ghost"
                size="sm"
                className="text-mute hover:text-ink"
              >
                {t("settings.modelsFetch.cancel")}
              </Button>
              <Button
                type="button"
                onClick={handleImport}
                disabled={selected.size === 0}
                variant="primary"
                size="sm"
              >
                {t("settings.modelsFetch.importSelected", { count: selected.size })}
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
};
