import { useState, useEffect, useCallback, type FC } from "react";
import { createPortal } from "react-dom";
import { browseWorkspace, changeWorkspace, type BrowseEntry, type BrowseResult } from "../lib/api";

export interface WorkspaceBrowserProps {
  open: boolean;
  onClose: () => void;
  onChanged: (cwd: string) => void;
  onGoHome: () => Promise<void> | void;
}

function buildBreadcrumbs(p: string): { label: string; path: string }[] {
  if (!p) return [];
  const isWin = /^[A-Za-z]:\\/.test(p);
  if (isWin) {
    const parts = p.split("\\").filter(Boolean);
    const crumbs: { label: string; path: string }[] = [];
    let base = "";
    for (let i = 0; i < parts.length; i++) {
      const seg = parts[i];
      if (!seg) continue;
      base = i === 0 ? seg + "\\" : base + "\\" + seg;
      crumbs.push({ label: seg, path: base });
    }
    return crumbs;
  }
  const parts = p.split("/").filter(Boolean);
  const crumbs: { label: string; path: string }[] = [];
  let base = "";
  for (const seg of parts) {
    base = base + "/" + seg;
    crumbs.push({ label: seg, path: base });
  }
  return crumbs;
}

export const WorkspaceBrowser: FC<WorkspaceBrowserProps> = ({
  open,
  onClose,
  onChanged,
  onGoHome,
}) => {
  const [current, setCurrent] = useState("");
  const [entries, setEntries] = useState<BrowseEntry[]>([]);
  const [drives, setDrives] = useState<BrowseEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);

  const load = useCallback(async (path: string) => {
    setLoading(true);
    setError(null);
    try {
      const result: BrowseResult = await browseWorkspace(path);
      setCurrent(result.path);
      setEntries(result.entries);
      setDrives(result.drives);
    } catch {
      setError("Failed to read directory");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      load("");
    }
  }, [open, load]);

  const handleNavigate = (entry: BrowseEntry): void => {
    if (entry.is_dir) {
      load(entry.path);
    }
  };

  const handleSelect = async (): Promise<void> => {
    if (!current) return;
    setSwitching(true);
    setError(null);
    try {
      const result = await changeWorkspace(current);
      if (result.success) {
        onChanged(result.cwd);
        onClose();
      } else {
        setError(result.notice || "Failed to switch workspace");
      }
    } catch {
      setError("Network error");
    } finally {
      setSwitching(false);
    }
  };

  const crumbs = buildBreadcrumbs(current);

  if (!open) return null;

  // Render via a portal at document.body so the modal escapes the Sidebar,
  // whose CSS transform (mobile slide animation) would otherwise become the
  // containing block for ``position: fixed`` and trap the dialog on the left.
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-overlay">
      <div
        className="flex w-[520px] max-w-[90vw] max-h-[70vh] flex-col rounded-lg border border-card-border bg-content-bg shadow-lg"
        onClick={(e): void => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-divider px-4 py-3">
          <h3 className="text-sm font-semibold text-text-primary">
            Choose Workspace
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="px-1 text-lg leading-none text-text-secondary transition-colors hover:text-text-primary"
          >
            ✕
          </button>
        </div>

        {/* Breadcrumbs */}
        <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-divider px-4 py-2">
          {crumbs.map((crumb, i) => (
            <span key={crumb.path} className="flex shrink-0 items-center gap-1">
              {i > 0 && <span className="text-xs text-text-disabled">/</span>}
              <button
                type="button"
                onClick={(): Promise<void> => load(crumb.path)}
                className="font-mono text-xs text-text-link hover:underline"
              >
                {crumb.label}
              </button>
            </span>
          ))}
        </div>

        {/* Drive letters (Windows root) */}
        {drives.length > 0 && (
          <div className="flex shrink-0 flex-wrap gap-2 border-b border-divider px-4 py-2">
            {drives.map((d) => (
              <button
                key={d.path}
                type="button"
                onClick={(): Promise<void> => load(d.path)}
                className="rounded bg-btn-secondary px-2 py-1 font-mono text-xs text-btn-secondary-text transition-colors hover:bg-sidebar-hover"
              >
                {d.name}
              </button>
            ))}
          </div>
        )}

        {/* Directory listing */}
        <div className="min-h-[240px] flex-1 overflow-y-auto px-2 py-2">
          {loading && (
            <p className="px-2 py-4 text-xs text-text-secondary">Loading...</p>
          )}
          {error && (
            <p className="px-2 py-4 text-xs text-error">{error}</p>
          )}
          {!loading &&
            !error &&
            entries.length === 0 && (
              <p className="px-2 py-4 text-xs text-text-disabled">
                Empty directory
              </p>
            )}
          {entries.map((entry) => (
            <button
              key={entry.path}
              type="button"
              onClick={(): void => handleNavigate(entry)}
              disabled={!entry.is_dir}
              className={`flex w-full items-center gap-2 rounded px-3 py-1.5 text-left text-xs transition-colors ${ entry.is_dir ? "cursor-pointer text-text-body hover:bg-sidebar-hover" : "cursor-default text-text-disabled" }`}
            >
              <span className="w-4 shrink-0 text-center">
                {entry.is_dir ? "📁" : "📄"}
              </span>
              <span className="truncate font-mono">{entry.name}</span>
            </button>
          ))}
        </div>

        {/* Footer */}
        <div className="flex shrink-0 items-center justify-between border-t border-divider px-4 py-3">
          <p className="max-w-[300px] truncate font-mono text-[10px] text-text-secondary">
            {current}
          </p>
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 text-xs text-text-secondary transition-colors hover:text-text-primary"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={(): Promise<void> | void => onGoHome()}
              disabled={switching}
              className="rounded px-3 py-1.5 text-xs text-text-secondary transition-colors hover:bg-sidebar-hover hover:text-text-primary disabled:opacity-50"
            >
              Home
            </button>
            <button
              type="button"
              onClick={handleSelect}
              disabled={switching || !current}
              className="rounded bg-btn-primary px-4 py-1.5 text-xs text-btn-primary-text transition-colors hover:bg-send-btn-hover disabled:opacity-50"
            >
              {switching ? "Switching..." : "Select"}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
};
