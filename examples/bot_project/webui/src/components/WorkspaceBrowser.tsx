import { useState, useEffect, useCallback, type FC } from "react";
import { createPortal } from "react-dom";
import { browseWorkspace, changeWorkspace, type BrowseEntry, type BrowseResult } from "../lib/api";

export interface WorkspaceBrowserProps {
  open: boolean;
  onClose: () => void;
  onChanged: (cwd: string) => void;
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-overlay-light dark:bg-overlay-dark">
      <div
        className="flex w-[520px] max-w-[90vw] max-h-[70vh] flex-col rounded-lg border border-card-border-light bg-content-bg-light shadow-lg dark:border-card-border-dark dark:bg-content-bg-dark"
        onClick={(e): void => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-divider-light px-4 py-3 dark:border-divider-dark">
          <h3 className="text-sm font-semibold text-text-primary-light dark:text-text-primary-dark">
            Choose Workspace
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="px-1 text-lg leading-none text-text-secondary-light transition-colors hover:text-text-primary-light dark:text-text-secondary-dark dark:hover:text-text-primary-dark"
          >
            ✕
          </button>
        </div>

        {/* Breadcrumbs */}
        <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-divider-light px-4 py-2 dark:border-divider-dark">
          {crumbs.map((crumb, i) => (
            <span key={crumb.path} className="flex shrink-0 items-center gap-1">
              {i > 0 && <span className="text-xs text-text-disabled-light dark:text-text-disabled-dark">/</span>}
              <button
                type="button"
                onClick={(): Promise<void> => load(crumb.path)}
                className="font-mono text-xs text-text-link-light hover:underline dark:text-text-link-dark"
              >
                {crumb.label}
              </button>
            </span>
          ))}
        </div>

        {/* Drive letters (Windows root) */}
        {drives.length > 0 && (
          <div className="flex shrink-0 flex-wrap gap-2 border-b border-divider-light px-4 py-2 dark:border-divider-dark">
            {drives.map((d) => (
              <button
                key={d.path}
                type="button"
                onClick={(): Promise<void> => load(d.path)}
                className="rounded bg-btn-secondary-light px-2 py-1 font-mono text-xs text-btn-secondary-text-light transition-colors hover:bg-sidebar-hover-light dark:bg-btn-secondary-dark dark:text-btn-secondary-text-dark dark:hover:bg-sidebar-hover-dark"
              >
                {d.name}
              </button>
            ))}
          </div>
        )}

        {/* Directory listing */}
        <div className="min-h-[240px] flex-1 overflow-y-auto px-2 py-2">
          {loading && (
            <p className="px-2 py-4 text-xs text-text-secondary-light dark:text-text-secondary-dark">Loading...</p>
          )}
          {error && (
            <p className="px-2 py-4 text-xs text-error-light dark:text-error-dark">{error}</p>
          )}
          {!loading &&
            !error &&
            entries.length === 0 && (
              <p className="px-2 py-4 text-xs text-text-disabled-light dark:text-text-disabled-dark">
                Empty directory
              </p>
            )}
          {entries.map((entry) => (
            <button
              key={entry.path}
              type="button"
              onClick={(): void => handleNavigate(entry)}
              disabled={!entry.is_dir}
              className={`flex w-full items-center gap-2 rounded px-3 py-1.5 text-left text-xs transition-colors ${
                entry.is_dir
                  ? "cursor-pointer text-text-body-light hover:bg-sidebar-hover-light dark:text-text-body-dark dark:hover:bg-sidebar-hover-dark"
                  : "cursor-default text-text-disabled-light dark:text-text-disabled-dark"
              }`}
            >
              <span className="w-4 shrink-0 text-center">
                {entry.is_dir ? "📁" : "📄"}
              </span>
              <span className="truncate font-mono">{entry.name}</span>
            </button>
          ))}
        </div>

        {/* Footer */}
        <div className="flex shrink-0 items-center justify-between border-t border-divider-light px-4 py-3 dark:border-divider-dark">
          <p className="max-w-[300px] truncate font-mono text-[10px] text-text-secondary-light dark:text-text-secondary-dark">
            {current}
          </p>
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 text-xs text-text-secondary-light transition-colors hover:text-text-primary-light dark:text-text-secondary-dark dark:hover:text-text-primary-dark"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={async (): Promise<void> => {
                setSwitching(true);
                setError(null);
                try {
                  const result = await changeWorkspace("");
                  if (result.success) {
                    onChanged(result.cwd);
                    onClose();
                  } else {
                    setError(result.notice || "Failed to return home");
                  }
                } catch {
                  setError("Network error");
                } finally {
                  setSwitching(false);
                }
              }}
              disabled={switching}
              className="rounded px-3 py-1.5 text-xs text-text-secondary-light transition-colors hover:bg-sidebar-hover-light hover:text-text-primary-light disabled:opacity-50 dark:text-text-secondary-dark dark:hover:bg-sidebar-hover-dark dark:hover:text-text-primary-dark"
            >
              Home
            </button>
            <button
              type="button"
              onClick={handleSelect}
              disabled={switching || !current}
              className="rounded bg-btn-primary-light px-4 py-1.5 text-xs text-btn-primary-text-light transition-colors hover:bg-send-btn-hover-light disabled:opacity-50 dark:bg-btn-primary-dark dark:text-btn-primary-text-dark dark:hover:bg-send-btn-hover-dark"
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
