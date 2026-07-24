import { useState, useCallback, useRef, type FC } from "react";
import { createPortal } from "react-dom";
import { FolderOpen } from "lucide-react";
import { XIcon } from "./ui/icons";
import { Button } from "./ui/Button";
import { IconButton } from "./ui/IconButton";
import { useT } from "../i18n";
import { pickWorkspace, changeWorkspace, ApiError } from "../lib/api";

export interface RecentWorkspace {
  path: string;
}

export interface WorkspaceBrowserProps {
  open: boolean;
  onClose: () => void;
  onChanged: (cwd: string) => void;
  onGoHome: () => Promise<void> | void;
  recentWorkspaces?: RecentWorkspace[];
}

export const WorkspaceBrowser: FC<WorkspaceBrowserProps> = ({
  open,
  onClose,
  onChanged,
  onGoHome,
  recentWorkspaces = [],
}) => {
  const t = useT();
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancelledRef = useRef(false);

  const handleClose = useCallback((): void => {
    cancelledRef.current = true;
    onClose();
  }, [onClose]);

  const applySwitchResult = useCallback(
    (result: { success: boolean; cwd?: string; notice?: string }): void => {
      if (result.success && result.cwd) {
        onChanged(result.cwd);
        onClose();
      } else {
        setError(result.notice || t("workspace.failedSwitch"));
      }
    },
    [onChanged, onClose, t],
  );

  const handlePick = useCallback(async (): Promise<void> => {
    cancelledRef.current = false;
    setPicking(true);
    setError(null);
    try {
      const result = await pickWorkspace();
      if (cancelledRef.current) return;
      if (result.path === null) return;
      applySwitchResult(result);
    } catch (e) {
      if (cancelledRef.current) return;
      if (e instanceof ApiError && e.status === 503) {
        setError(t("workspace.pickerUnavailable"));
      } else {
        setError(t("workspace.networkError"));
      }
    } finally {
      setPicking(false);
    }
  }, [applySwitchResult, t]);

  const handleRecentClick = useCallback(
    async (path: string): Promise<void> => {
      setError(null);
      try {
        const result = await changeWorkspace(path);
        applySwitchResult(result);
      } catch {
        setError(t("workspace.networkError"));
      }
    },
    [applySwitchResult, t],
  );

  if (!open) return null;

  // Render via a portal at document.body so the modal escapes the Sidebar,
  // whose CSS transform (mobile slide animation) would otherwise become the
  // containing block for position: fixed and trap the dialog on the left.
  return createPortal(
    <div
      className="modal-scrim-enter fixed inset-0 z-50 flex items-center justify-center bg-overlay"
      onClick={handleClose}
      onKeyDown={(e): void => {
        if (e.key === "Escape") handleClose();
      }}
      role="presentation"
    >
      <div
        className="modal-panel-enter flex w-[480px] max-w-[90vw] flex-col rounded-lg border border-hairline bg-canvas-popover shadow-popover"
        onClick={(e): void => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-hairline px-4 py-3">
          <h3 className="text-base font-semibold text-ink">
            {t("workspace.chooseWorkspace")}
          </h3>
          <IconButton
            icon={<XIcon />}
            label={t("workspace.close")}
            onClick={handleClose}
            variant="ghost"
            size="sm"
          />
        </div>

        <div className="flex flex-col gap-4 px-4 py-6">
          <button
            type="button"
            onClick={(): Promise<void> => handlePick()}
            disabled={picking}
            className="flex flex-col items-center justify-center gap-3 rounded-lg border-2 border-dashed border-hairline-strong px-6 py-10 text-center transition-colors hover:border-brand hover:bg-hairline-soft disabled:cursor-not-allowed disabled:opacity-45"
          >
            <FolderOpen size={32} className="shrink-0 text-brand" />
            <span className="text-sm font-medium text-ink">
              {picking ? t("workspace.openingPicker") : t("workspace.openFolder")}
            </span>
            <span className="text-xs text-mute">
              {t("workspace.openFolderHint")}
            </span>
          </button>

          {error && (
            <p className="text-xs text-error" role="alert">
              {error}
            </p>
          )}

          {recentWorkspaces.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <p className="text-xs font-semibold uppercase tracking-wide text-mute">
                {t("workspace.recentWorkspaces")}
              </p>
              {recentWorkspaces.map((entry) => (
                <button
                  key={entry.path}
                  type="button"
                  onClick={(): Promise<void> => handleRecentClick(entry.path)}
                  disabled={picking}
                  title={entry.path}
                  className="flex items-center gap-2 truncate rounded-sm px-2 py-1.5 text-left font-mono text-xs text-body transition-colors hover:bg-hairline-soft hover:text-ink disabled:opacity-45"
                >
                  <FolderOpen size={14} className="shrink-0 opacity-50" />
                  <span className="truncate">{entry.path}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-hairline px-4 py-3">
          <Button
            type="button"
            onClick={handleClose}
            variant="ghost"
            size="sm"
            className="text-mute hover:text-ink"
          >
            {t("workspace.cancel")}
          </Button>
          <Button
            type="button"
            onClick={(): Promise<void> | void => onGoHome()}
            disabled={picking}
            variant="secondary"
            size="sm"
            className="text-mute hover:text-ink"
          >
            {t("workspace.home")}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
};
