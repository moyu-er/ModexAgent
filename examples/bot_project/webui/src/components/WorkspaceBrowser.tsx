import { useState, useEffect, useCallback, type FC } from "react";
import { browseWorkspace, changeWorkspace, type BrowseEntry, type BrowseResult } from "../lib/api";

export interface WorkspaceBrowserProps {
  open: boolean;
  onClose: () => void;
  onChanged: (cwd: string) => void;
}

function buildBreadcrumbs(p: string): { label: string; path: string }[] {
  if (!p) return [];
  // Determine separator
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
  // Unix
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

  // Load on open
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

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div
        className="bg-gray-900 border border-gray-700 rounded-lg shadow-2xl w-[520px] max-h-[70vh] flex flex-col"
        onClick={(e): void => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between shrink-0">
          <h3 className="text-sm font-semibold text-gray-200">
            Choose Workspace
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-500 hover:text-gray-300 text-lg leading-none px-1"
          >
            ✕
          </button>
        </div>

        {/* Breadcrumbs */}
        <div className="px-4 py-2 border-b border-gray-800 flex items-center gap-1 overflow-x-auto shrink-0">
          {crumbs.map((crumb, i) => (
            <span key={crumb.path} className="flex items-center gap-1 shrink-0">
              {i > 0 && <span className="text-gray-600 text-xs">/</span>}
              <button
                type="button"
                onClick={(): Promise<void> => load(crumb.path)}
                className="text-xs text-blue-400 hover:text-blue-300 hover:underline font-mono"
              >
                {crumb.label}
              </button>
            </span>
          ))}
        </div>

        {/* Drive letters (Windows root) */}
        {drives.length > 0 && (
          <div className="px-4 py-2 border-b border-gray-800 flex gap-2 flex-wrap shrink-0">
            {drives.map((d) => (
              <button
                key={d.path}
                type="button"
                onClick={(): Promise<void> => load(d.path)}
                className="px-2 py-1 text-xs bg-gray-800 text-gray-300 rounded hover:bg-gray-700 hover:text-gray-100 transition-colors font-mono"
              >
                {d.name}
              </button>
            ))}
          </div>
        )}

        {/* Directory listing */}
        <div className="flex-1 overflow-y-auto px-2 py-2 min-h-[240px]">
          {loading && (
            <p className="text-xs text-gray-500 px-2 py-4">Loading...</p>
          )}
          {error && (
            <p className="text-xs text-red-400 px-2 py-4">{error}</p>
          )}
          {!loading &&
            !error &&
            entries.length === 0 && (
              <p className="text-xs text-gray-600 px-2 py-4">
                Empty directory
              </p>
            )}
          {entries.map((entry) => (
            <button
              key={entry.path}
              type="button"
              onClick={(): void => handleNavigate(entry)}
              disabled={!entry.is_dir}
              className={`w-full text-left px-3 py-1.5 rounded text-xs flex items-center gap-2 transition-colors ${
                entry.is_dir
                  ? "hover:bg-gray-800 text-gray-300 cursor-pointer"
                  : "text-gray-600 cursor-default"
              }`}
            >
              <span className="shrink-0 w-4 text-center">
                {entry.is_dir ? "📁" : "📄"}
              </span>
              <span className="truncate font-mono">{entry.name}</span>
            </button>
          ))}
        </div>

        {/* Footer */}
        <div className="px-4 py-3 border-t border-gray-800 flex items-center justify-between shrink-0">
          <p className="text-[10px] text-gray-500 font-mono truncate max-w-[300px]">
            {current}
          </p>
          <div className="flex gap-2 shrink-0">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 transition-colors"
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
              className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 hover:bg-gray-800 rounded transition-colors disabled:opacity-50"
            >
              Home
            </button>
            <button
              type="button"
              onClick={handleSelect}
              disabled={switching || !current}
              className="px-4 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-500 transition-colors disabled:opacity-50"
            >
              {switching ? "Switching..." : "Select"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
