import { useState, useEffect, useCallback, useMemo, useRef, type FC } from "react";
import { createPortal } from "react-dom";
import { fetchProviderModels, ApiError, type FetchedModel } from "../../lib/api";
import { XIcon } from "../ui/icons";
import { IconButton } from "../ui/IconButton";
import { Button } from "../ui/Button";

export interface FetchModelsModalProps {
  open: boolean;
  onClose: () => void;
  providerKey: string;
  existingModelIds: Set<string>;
  onImport: (models: FetchedModel[]) => void;
}

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "success"; models: FetchedModel[] };

function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) return "认证失败，请检查 API Key";
    if (err.status === 404) return "Provider 未保存或不存在，请先保存";
    if (err.status === 502) {
      if (err.detail.includes("All candidates failed"))
        return "未找到模型列表端点，请检查 Base URL 或手动填写模型列表 URL";
      if (err.detail.includes("timed out")) return "请求超时，请重试";
      if (err.detail.includes("0 models")) return "Provider 返回了 0 个模型";
      return err.detail;
    }
    return `拉取失败 (${err.status})`;
  }
  return "网络错误，请检查连接";
}

export const FetchModelsModal: FC<FetchModelsModalProps> = ({
  open,
  onClose,
  providerKey,
  existingModelIds,
  onImport,
}) => {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const fetchSeq = useRef(0);

  const load = useCallback(async (key: string) => {
    const seq = ++fetchSeq.current;
    setState({ kind: "loading" });
    setSelected(new Set());
    try {
      const result = await fetchProviderModels(key);
      if (seq !== fetchSeq.current) return;
      setState({ kind: "success", models: result.models });
    } catch (err) {
      if (seq !== fetchSeq.current) return;
      setState({ kind: "error", message: describeError(err) });
    }
  }, []);

  useEffect(() => {
    if (open && providerKey) {
      load(providerKey);
    }
  }, [open, providerKey, load]);

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
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay"
      onClick={onClose}
      onKeyDown={(e): void => {
        if (e.key === "Escape") onClose();
      }}
      role="presentation"
    >
      <div
        className="flex w-[560px] max-w-[90vw] max-h-[75vh] flex-col rounded-lg border border-hairline bg-canvas-elevated shadow-lg"
        onClick={(e): void => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-hairline px-4 py-3">
          <h3 className="text-sm font-semibold text-ink">
            从 Provider 拉取模型
            <span className="ml-2 font-mono text-xs text-body">{providerKey}</span>
          </h3>
          <IconButton
            icon={<XIcon />}
            label="Close"
            onClick={onClose}
            variant="ghost"
            size="sm"
          />
        </div>

        {/* Search */}
        {state.kind === "success" && (
          <div className="shrink-0 border-b border-hairline px-4 py-2">
            <input
              type="text"
              placeholder="搜索模型..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="w-full rounded-md border border-hairline bg-canvas px-3 py-1.5 text-sm text-ink placeholder:text-faint focus:border-link focus:outline-none"
            />
          </div>
        )}

        {/* Body */}
        <div className="min-h-[240px] flex-1 overflow-y-auto px-2 py-2">
          {isLoading && (
            <div className="flex items-center justify-center py-8">
              <span className="text-sm text-body">正在拉取模型列表...</span>
            </div>
          )}

          {isError && state.kind === "error" && (
            <div className="flex flex-col items-center gap-3 py-8">
              <p className="px-4 text-center text-sm text-error">{state.message}</p>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => load(providerKey)}
              >
                重试
              </Button>
            </div>
          )}

          {state.kind === "success" && models.length === 0 && (
            <p className="px-2 py-8 text-center text-xs text-faint">
              Provider 返回了 0 个模型
            </p>
          )}

          {state.kind === "success" && filteredModels.length === 0 && models.length > 0 && (
            <p className="px-2 py-4 text-center text-xs text-faint">
              没有匹配的模型
            </p>
          )}

          {grouped.map(([vendor, items]) => (
            <div key={vendor} className="mb-2">
              <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wide text-mute">
                {vendor} ({items.length})
              </div>
              {items.map((m) => {
                const exists = existingModelIds.has(m.id);
                const checked = selected.has(m.id);
                return (
                  <label
                    key={m.id}
                    className={`flex items-center gap-2.5 rounded px-3 py-1.5 text-sm transition-colors ${
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
                      <span className="ml-auto shrink-0 text-[10px] text-mute">已添加</span>
                    )}
                  </label>
                );
              })}
            </div>
          ))}
        </div>

        {/* Footer */}
        {state.kind === "success" && (
          <div className="flex shrink-0 items-center justify-between border-t border-hairline px-4 py-3">
            <span className="text-xs text-body">
              已选 {selected.size} 个 · 共 {models.length} 个模型
            </span>
            <div className="flex shrink-0 gap-2">
              <Button
                type="button"
                onClick={onClose}
                variant="ghost"
                size="sm"
                className="text-mute hover:text-ink"
              >
                取消
              </Button>
              <Button
                type="button"
                onClick={handleImport}
                disabled={selected.size === 0}
                variant="primary"
                size="sm"
              >
                导入选中 ({selected.size})
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
};
