import { useState, type FC, type CSSProperties } from "react";
import type { PoolInfo } from "../lib/api";
import { changeWorkspace } from "../lib/api";
import { WorkspaceBrowser } from "./WorkspaceBrowser";
import { SessionTree, type TreeNode } from "./SessionTree";
import { ThemeToggle } from "./ThemeToggle";
import { useToast } from "./ToastContext";

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
      className={`fixed inset-y-0 left-0 z-40 flex h-full w-[260px] flex-col border-r border-divider bg-sidebar-bg transition-transform duration-200 ease-out md:static md:w-[var(--sidebar-width)] md:translate-x-0 ${
        mobileOpen ? "translate-x-0" : "-translate-x-full"
      }`}
    >
      {/* Workspace indicator (click to browse) */}
      <div className="border-b border-divider px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wider text-text-secondary">
            Workspace
          </span>
          {!isHome && (
            <button
              type="button"
              onClick={onGoHome}
              title="Return to home workspace (exit)"
              className="flex items-center gap-0.5 text-text-secondary transition-colors hover:text-text-primary"
            >
              <span className="text-xs">↩</span>
              <span className="text-xs font-medium">Home</span>
            </button>
          )}
        </div>
        <button
          type="button"
          onClick={(): void => setBrowserOpen(true)}
          title="Browse for workspace folder"
          className="-ml-2 mt-1.5 flex w-full cursor-pointer items-center gap-1.5 truncate rounded-md px-2 py-1 text-left font-mono text-sm text-text-body transition-colors hover:bg-sidebar-hover hover:text-text-primary"
        >
          <span className="shrink-0 text-sm">📂</span>
          <span className="truncate">{String(workspace || "(not set)")}</span>
        </button>

        {/* Recent workspaces dropdown */}
        {recentFiltered.length > 0 && (
          <div className="relative mt-1">
            <button
              type="button"
              onClick={(): void => setRecentOpen(!recentOpen)}
              className="flex w-full items-center gap-1 text-xs text-text-secondary transition-colors hover:text-text-body"
            >
              <span className={`inline-block transition-transform ${recentOpen ? "rotate-90" : ""}`}>▸</span>
              <span>Recent</span>
              <span className="text-text-disabled">({recentFiltered.length})</span>
            </button>
            {recentOpen && (
              <div className="mt-1 max-h-40 overflow-y-auto rounded-md border border-card-border bg-content-bg">
                {recentFiltered.map((entry) => (
                  <button
                    key={String(entry.path)}
                    type="button"
                    onClick={(): void => { handleRecentClick(String(entry.path)); }}
                    title={String(entry.path)}
                    className="flex w-full items-center gap-1.5 truncate px-2.5 py-1.5 text-left font-mono text-xs text-text-body transition-colors hover:bg-sidebar-hover hover:text-text-primary"
                  >
                    <span className="shrink-0 text-xs opacity-50">📁</span>
                    <span className="truncate">{String(entry.path)}</span>
                  </button>
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
      <div className="flex items-center justify-between border-b border-divider px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-text-secondary">
          Conversations
        </h2>
        <div className="flex items-center gap-1">
          {onOpenSettings && (
            <button
              type="button"
              title="Settings"
              className="relative rounded px-1.5 py-0.5 text-text-secondary hover:bg-sidebar-hover"
              onClick={onOpenSettings}
            >
              ⚙
              {restart.restartNeeded && (
                <span
                  aria-label="Restart required"
                  title="Restart required to apply saved changes"
                  className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-error"
                />
              )}
            </button>
          )}
          <ThemeToggle />
        </div>
      </div>

      {/* Pool selector badge */}
      {pools.length > 1 && (
        <div className="border-b border-divider px-4 py-3">
          <div className="relative">
            <select
              value={activePool}
              onChange={(e): void => onPoolChange(e.target.value)}
              className="w-full cursor-pointer appearance-none rounded-lg border border-card-border bg-content-bg py-3 pl-7 pr-10 text-base font-semibold text-text-primary transition-colors hover:bg-sidebar-hover focus:border-input-focus focus:outline-none"
            >
              {pools.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
            <span className="pointer-events-none absolute left-2 top-1/2 h-2.5 w-2.5 -translate-y-1/2 rounded-full bg-ai-brand" />
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-base leading-none text-text-secondary">
              ▾
            </span>
          </div>
        </div>
      )}

      {/* Session tree */}
      <div className="flex-1 overflow-y-auto py-2">
        {isLoadingSessions ? (
          <p className="px-4 py-3 text-sm text-text-secondary">
            Loading…
          </p>
        ) : sessionTree.length === 0 ? (
          <p className="px-4 py-3 text-sm text-text-secondary">
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
      <div className="border-t border-divider p-3">
        <button
          type="button"
          onClick={handleNew}
          className="w-full rounded-lg bg-btn-primary px-3 py-2.5 text-sm font-semibold text-btn-primary-text transition-opacity hover:opacity-90 active:opacity-80"
        >
          + New Conversation
        </button>
      </div>
    </div>
  );
};
