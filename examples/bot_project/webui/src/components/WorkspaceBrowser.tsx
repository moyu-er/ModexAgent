import { useState, useEffect, useCallback, type FC } from "react";
import { createPortal } from "react-dom";
import { browseWorkspace, changeWorkspace, type BrowseEntry, type BrowseResult } from "../lib/api";
import { FileIcon, FolderIcon, XIcon } from "./ui/icons";
import { Button } from "./ui/Button";
import { IconButton } from "./ui/IconButton";
import { useT } from "../i18n";

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
  const t = useT();
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
      setError(t("workspace.failedReadDir"));
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
        setError(result.notice || t("workspace.failedSwitch"));
      }
    } catch {
      setError(t("workspace.networkError"));
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
    <div
      className="modal-scrim-enter fixed inset-0 z-50 flex items-center justify-center bg-overlay"
      onClick={onClose}
      onKeyDown={(e): void => {
        if (e.key === "Escape") onClose();
      }}
      role="presentation"
    >
      <div
        className="modal-panel-enter flex w-[520px] max-w-[90vw] max-h-[70vh] flex-col rounded-lg border border-hairline bg-canvas-popover shadow-popover"
        onClick={(e): void => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-hairline px-4 py-3">
          <h3 className="text-base font-semibold text-ink">
            {t("workspace.chooseWorkspace")}
          </h3>
          <IconButton
            icon={<XIcon />}
            label={t("workspace.close")}
            onClick={onClose}
            variant="ghost"
            size="sm"
          />
        </div>

        {/* Breadcrumbs */}
        <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-hairline px-4 py-2">
          {crumbs.map((crumb, i) => (
            <span key={crumb.path} className="flex shrink-0 items-center gap-1">
              {i > 0 && <span className="text-xs text-faint">/</span>}
              <button
                type="button"
                onClick={(): Promise<void> => load(crumb.path)}
                className="font-mono text-xs text-link hover:underline"
              >
                {crumb.label}
              </button>
            </span>
          ))}
        </div>

        {/* Drive letters (Windows root) */}
        {drives.length > 0 && (
          <div className="flex shrink-0 flex-wrap gap-2 border-b border-hairline px-4 py-2">
            {drives.map((d) => (
              <Button
                key={d.path}
                type="button"
                onClick={(): Promise<void> => load(d.path)}
                variant="secondary"
                size="sm"
                className="font-mono"
              >
                {d.name}
              </Button>
            ))}
          </div>
        )}

        {/* Directory listing */}
        <div className="min-h-[240px] flex-1 overflow-y-auto px-2 py-2">
          {loading && (
            <p className="px-2 py-4 text-xs text-mute">{t("workspace.loading")}</p>
          )}
          {error && (
            <p className="px-2 py-4 text-xs text-error">{error}</p>
          )}
          {!loading &&
            !error &&
            entries.length === 0 && (
              <p className="px-2 py-4 text-xs text-faint">
                {t("workspace.emptyDirectory")}
              </p>
            )}
          {entries.map((entry) => (
            <button
              key={entry.path}
              type="button"
              onClick={(): void => handleNavigate(entry)}
              disabled={!entry.is_dir}
              className={`flex w-full items-center gap-2 rounded px-3 py-1.5 text-left text-xs transition-colors ${ entry.is_dir ? "cursor-pointer text-body hover:bg-hairline-soft" : "cursor-default text-faint" }`}
            >
              <span className="flex w-4 shrink-0 items-center justify-center">
                {entry.is_dir ? <FolderIcon /> : <FileIcon />}
              </span>
              <span className="truncate font-mono">{entry.name}</span>
            </button>
          ))}
        </div>

        {/* Footer */}
        <div className="flex shrink-0 items-center justify-between border-t border-hairline px-4 py-3">
          <p className="max-w-[300px] truncate font-mono text-xs text-mute">
            {current}
          </p>
          <div className="flex shrink-0 gap-2">
            <Button
              type="button"
              onClick={onClose}
              variant="ghost"
              size="sm"
              className="text-mute hover:text-ink"
            >
              {t("workspace.cancel")}
            </Button>
            <Button
              type="button"
              onClick={(): Promise<void> | void => onGoHome()}
              disabled={switching}
              variant="secondary"
              size="sm"
              className="text-mute hover:text-ink"
            >
              {t("workspace.home")}
            </Button>
            <Button
              type="button"
              onClick={handleSelect}
              disabled={switching || !current}
              variant="primary"
              size="sm"
            >
              {switching ? t("workspace.switching") : t("workspace.select")}
            </Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
};
