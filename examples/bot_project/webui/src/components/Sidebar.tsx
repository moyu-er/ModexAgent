import { useState, type FC, type CSSProperties } from "react";
import type { PoolInfo } from "../lib/api";
import { changeWorkspace } from "../lib/api";
import { WorkspaceBrowser } from "./WorkspaceBrowser";
import { SessionTree, type TreeNode } from "./SessionTree";
import { ThemeToggle } from "./ThemeToggle";
import { useToast } from "./ToastContext";
import { Button } from "./ui/Button";
import { IconButton } from "./ui/IconButton";
import { SelectPrimitive } from "./ui/SelectPrimitive";
import {
  ChevronRightIcon,
  FolderIcon,
  FolderOpenIcon,
  HomeIcon,
  SettingsGearIcon,
} from "./ui/icons";

export interface SidebarProps {
  sessionTree: TreeNode[];
  pools: PoolInfo[];
  selected: string | null;
  workspace: string;
  isHome: boolean;
  activePool: string;
  recentWorkspaces: { path: string }[];
  isLoadingSessions?: boolean;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onSelect: (sessionId: string) => void;
  onNew: (pool: string) => void;
  onDelete: (sessionId: string) => void;
  onWorkspaceChanged: (cwd: string) => void;
  onGoHome: () => void;
  onPoolChange: (pool: string) => void;
  revealSessionId?: string | null;
  style?: CSSProperties;
  onOpenSettings?: () => void;
}

export const Sidebar: FC<SidebarProps> = ({
  sessionTree,
  pools,
  selected,
  workspace,
  isHome,
  activePool,
  recentWorkspaces,
  isLoadingSessions = false,
  mobileOpen,
  onCloseMobile,
  onSelect,
  onNew,
  onDelete,
  onWorkspaceChanged,
  onGoHome,
  onPoolChange,
  revealSessionId,
  style,
  onOpenSettings,
}) => {
  const [browserOpen, setBrowserOpen] = useState(false);
  const [recentOpen, setRecentOpen] = useState(false);
  // Persistent restart indicator: any restart_required save arms a red dot on
  // the settings gear. Best-effort clear after restartSystem resolves (see
  // restartToast); otherwise it stays until page reload.
  const { restart } = useToast();

  const handleNew = (): void => {
    onNew(activePool);
  };

  const handleRecentClick = async (path: string): Promise<void> => {
    setRecentOpen(false);
    onCloseMobile();
    try {
      const result = await changeWorkspace(path);
      if (result.success) {
        // Defensive: backend may serialize cwd as a path object; coerce to string.
        onWorkspaceChanged(
          typeof result.cwd === "string" ? result.cwd : String(result.cwd),
        );
      }
    } catch {
      // Network error — silently ignore; the browser dialog has proper error handling
    }
  };

  const recentFiltered = recentWorkspaces.filter(
    (r) => r.path && r.path !== workspace,
  );

  return (
    <div
      style={style}
      className={`fixed inset-y-0 left-0 z-40 flex h-full w-[260px] flex-col border-r border-hairline bg-canvas transition-transform duration-200 ease-out md:static md:w-[var(--sidebar-width)] md:translate-x-0 ${
        mobileOpen ? "translate-x-0" : "-translate-x-full"
      }`}
    >
      {/* Workspace indicator (click to browse) */}
      <div className="border-b border-hairline px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wider text-mute">
            Workspace
          </span>
          {!isHome && (
            <Button
              variant="ghost"
              size="sm"
              onClick={onGoHome}
              title="Return to home workspace (exit)"
              className="gap-0.5 text-mute hover:text-ink"
            >
              <HomeIcon className="h-3.5 w-3.5" />
              <span className="text-xs font-medium">Home</span>
            </Button>
          )}
        </div>
        <Button
          variant="ghost"
          size="md"
          onClick={(): void => setBrowserOpen(true)}
          title="Browse for workspace folder"
          className="-ml-2 mt-1.5 h-auto w-full justify-start gap-1.5 truncate rounded-sm px-2 py-1 text-left font-mono text-sm text-body hover:bg-hairline-soft hover:text-ink"
        >
          <FolderIcon className="h-4 w-4 shrink-0" />
          <span className="truncate">{String(workspace || "(not set)")}</span>
        </Button>

        {/* Recent workspaces dropdown */}
        {recentFiltered.length > 0 && (
          <div className="relative mt-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={(): void => setRecentOpen(!recentOpen)}
              className="h-auto w-full justify-start gap-1.5 pl-0 pr-2 text-xs text-mute hover:text-body"
            >
              <ChevronRightIcon
                className={`h-3.5 w-3.5 transition-transform ${recentOpen ? "rotate-90" : ""}`}
              />
              <span>Recent</span>
              <span className="text-faint">({recentFiltered.length})</span>
            </Button>
            {recentOpen && (
              <div className="relative mt-1 max-h-40 overflow-y-auto rounded-sm border-l-2 border-hairline pl-3">
                {recentFiltered.map((entry) => (
                  <Button
                    key={String(entry.path)}
                    variant="ghost"
                    size="md"
                    onClick={(): void => { handleRecentClick(String(entry.path)); }}
                    title={String(entry.path)}
                    className="h-auto w-full justify-start gap-1.5 truncate rounded-sm px-2 py-1 text-left font-mono text-xs text-body hover:bg-hairline-soft hover:text-ink"
                  >
                    <FolderOpenIcon className="h-3.5 w-3.5 shrink-0 opacity-50" />
                    <span className="truncate">{String(entry.path)}</span>
                  </Button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Workspace file browser modal */}
      <WorkspaceBrowser
        open={browserOpen}
        onClose={(): void => setBrowserOpen(false)}
        onChanged={(cwd): void => onWorkspaceChanged(cwd)}
        onGoHome={(): void => {
          setBrowserOpen(false);
          onGoHome();
        }}
      />

      {/* Header */}
      <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-mute">
          Conversations
        </h2>
        <div className="flex items-center gap-1">
          {onOpenSettings && (
            <div className="relative">
              <IconButton
                label="Settings"
                size="sm"
                variant="ghost"
                icon={<SettingsGearIcon />}
                onClick={onOpenSettings}
              />
              {restart.restartNeeded && (
                <span
                  aria-label="Restart required"
                  title="Restart required to apply saved changes"
                  className="absolute right-1 top-1 h-2 w-2 rounded-full bg-error"
                />
              )}
            </div>
          )}
          <ThemeToggle />
        </div>
      </div>

      {/* Pool selector badge */}
      {pools.length > 1 && (
        <div className="border-b border-hairline px-4 py-3">
          <SelectPrimitive
            value={activePool}
            onChange={(e): void => onPoolChange(e.target.value)}
            options={pools.map((p) => ({ value: p.name, label: p.name }))}
          />
        </div>
      )}

      {/* Session tree */}
      <div className="flex-1 overflow-y-auto py-2">
        {isLoadingSessions ? (
          <p className="px-4 py-3 text-sm text-mute">
            Loading…
          </p>
        ) : sessionTree.length === 0 ? (
          <p className="px-4 py-3 text-sm text-mute">
            No conversations in {activePool}
          </p>
        ) : (
          <SessionTree
            tree={sessionTree}
            selected={selected}
            onSelect={onSelect}
            onDelete={onDelete}
            revealSessionId={revealSessionId}
          />
        )}
      </div>

      {/* New Conversation button */}
      <div className="border-t border-hairline p-3">
        <Button
          variant="primary"
          size="lg"
          onClick={handleNew}
          className="h-auto w-full rounded-sm py-2.5 text-sm"
        >
          + New Conversation
        </Button>
      </div>
    </div>
  );
};
