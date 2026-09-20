import { useEffect, useRef, useState, type FC } from "react";
import { createPortal } from "react-dom";
import { Folder, FolderSearch, Pin, Search } from "lucide-react";
import { WorkspaceBrowser, type RecentWorkspace } from "./WorkspaceBrowser";
import { pathBasename, sameWorkspacePath } from "../hooks/useWorkspaceTabs";
import { useT } from "../i18n";

export interface OpenWorkspaceMenuProps {
  open: boolean;
  onClose: () => void;
  recentWorkspaces: RecentWorkspace[];
  /** The user's saved default workspace path (null = no default). */
  defaultWorkspace: string | null;
  /** Recent entry picked — the host runs cd (register) then opens a tab. */
  onOpenRecent: (path: string) => void;
  /** Directory picked via the browse modal — the browser already ran cd. */
  onBrowsePicked: (path: string) => void;
  /** Save `path` as the default workspace preference (PA-08). */
  onSetDefault: (path: string) => void;
  /** Browse modal's "home" shortcut — opens the home workspace tab. */
  onGoHome: () => void;
  /** Anchor the popover to the right edge (overflow mode's pinned "+"). */
  anchorRight?: boolean;
}

/**
 * The "+" menu: the single workspace-opening entry point. Opening a path
 * dedupes to an existing tab (PA-08); each recent entry also offers
 * "Set as default workspace" (a preference write, not a switch).
 */
export const OpenWorkspaceMenu: FC<OpenWorkspaceMenuProps> = ({
  open,
  onClose,
  recentWorkspaces,
  defaultWorkspace,
  onOpenRecent,
  onBrowsePicked,
  onSetDefault,
  onGoHome,
  anchorRight = false,
}) => {
  const t = useT();
  const [filter, setFilter] = useState("");
  const [browserOpen, setBrowserOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setFilter("");
      inputRef.current?.focus();
    }
  }, [open]);

  if (!open) return null;

  const q = filter.trim().toLowerCase();
  const items = recentWorkspaces.filter(
    (r) => r.path && (!q || r.path.toLowerCase().includes(q)),
  );

  return createPortal(
    <>
      <div
        className="fixed inset-0 z-40"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        className={`wsopen-menu ${anchorRight ? "right" : ""}`}
        role="menu"
        aria-label={t("tabs.openWorkspace")}
      >
        <div className="wsopen-search">
          <Search size={13} aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            placeholder={t("tabs.filterPlaceholder")}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") onClose();
            }}
          />
        </div>
        <div className="wsopen-sec">{t("tabs.recent")}</div>
        {items.length === 0 && (
          <div className="wsopen-empty">{t("tabs.noMatches")}</div>
        )}
        {items.map((entry) => {
          const path = String(entry.path);
          const base = pathBasename(path);
          const dir = path.slice(0, path.length - base.length);
          const isDefault =
            defaultWorkspace !== null && sameWorkspacePath(path, defaultWorkspace);
          return (
            <div key={path} className="wsopen-item-row">
              <button
                type="button"
                className="wsopen-item"
                title={path}
                onClick={() => {
                  onClose();
                  onOpenRecent(path);
                }}
              >
                <Folder size={14} aria-hidden="true" />
                <span className="wsopen-path">
                  <span className="dir">{dir}</span>
                  <span className="base">{base}</span>
                </span>
              </button>
              <button
                type="button"
                className="wsopen-item-side"
                title={t("tabs.setDefault")}
                aria-label={t("tabs.setDefault")}
                aria-pressed={isDefault}
                onClick={() => {
                  onSetDefault(path);
                }}
              >
                <Pin size={13} aria-hidden="true" />
              </button>
            </div>
          );
        })}
        <div className="wsopen-divider" />
        <button
          type="button"
          className="wsopen-item"
          onClick={() => setBrowserOpen(true)}
        >
          <FolderSearch size={14} aria-hidden="true" />
          <span className="wsopen-label">{t("tabs.browse")}</span>
        </button>
      </div>
      <WorkspaceBrowser
        open={browserOpen}
        onClose={() => {
          setBrowserOpen(false);
          onClose();
        }}
        onChanged={(cwd) => onBrowsePicked(cwd)}
        onGoHome={() => {
          setBrowserOpen(false);
          onClose();
          onGoHome();
        }}
        recentWorkspaces={recentWorkspaces}
      />
    </>,
    document.body,
  );
};
