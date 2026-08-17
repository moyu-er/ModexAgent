import { useEffect, useRef, useState, type FC } from "react";
import { createPortal } from "react-dom";
import { Folder, FolderSearch, Search } from "lucide-react";
import { WorkspaceBrowser, type RecentWorkspace } from "./WorkspaceBrowser";
import { pathBasename } from "../hooks/useWorkspaceTabs";
import { useT } from "../i18n";

export interface OpenWorkspaceMenuProps {
  open: boolean;
  onClose: () => void;
  recentWorkspaces: RecentWorkspace[];
  /** Recent entry picked — the host runs cd (register) then opens a tab. */
  onOpenRecent: (path: string) => void;
  /** Directory picked via the browse modal — the browser already ran cd. */
  onBrowsePicked: (path: string) => void;
  /** Browse modal's "home" shortcut — activates the pinned home tab. */
  onGoHome: () => void;
  /** Anchor the popover to the right edge (overflow mode's pinned "+"). */
  anchorRight?: boolean;
}

/**
 * The "+" menu: the single workspace-opening entry point. Opening ALWAYS
 * appends a new tab — this menu deliberately shows no "already open" state.
 */
export const OpenWorkspaceMenu: FC<OpenWorkspaceMenuProps> = ({
  open,
  onClose,
  recentWorkspaces,
  onOpenRecent,
  onBrowsePicked,
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
          return (
            <button
              key={path}
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
