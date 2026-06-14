import { useState, type FC } from "react";
import type { PoolInfo, RecentWorkspaceEntry } from "../lib/api";
import { changeWorkspace } from "../lib/api";
import { WorkspaceBrowser } from "./WorkspaceBrowser";
import { SessionTree, type TreeNode } from "./SessionTree";

export interface SidebarProps {
  sessionTree: TreeNode[];
  pools: PoolInfo[];
  selected: string | null;
  workspace: string;
  isHome: boolean;
  activePool: string;
  recentWorkspaces: RecentWorkspaceEntry[];
  onSelect: (sessionId: string) => void;
  onNew: (pool: string) => void;
  onDelete: (sessionId: string) => void;
  onWorkspaceChanged: (cwd: string) => void;
  onGoHome: () => void;
  onPoolChange: (pool: string) => void;
}

export const Sidebar: FC<SidebarProps> = ({
  sessionTree,
  pools,
  selected,
  workspace,
  isHome,
  activePool,
  recentWorkspaces,
  onSelect,
  onNew,
  onDelete,
  onWorkspaceChanged,
  onGoHome,
  onPoolChange,
}) => {
  const [browserOpen, setBrowserOpen] = useState(false);
  const [recentOpen, setRecentOpen] = useState(false);

  const handleNew = (): void => {
    onNew(activePool);
  };

  const handleRecentClick = async (path: string): Promise<void> => {
    setRecentOpen(false);
    try {
      const result = await changeWorkspace(path);
      if (result.success) {
        onWorkspaceChanged(result.cwd);
      }
    } catch {
      // Network error — silently ignore; the browser dialog has proper error handling
    }
  };

  const recentFiltered = recentWorkspaces.filter(
    (r) => r.path && r.path !== workspace,
  );

  return (
    <div className="flex h-full w-full flex-col bg-ink-900">
      {/* Workspace indicator (click to browse) */}
      <div className="border-b border-white/5 px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wider text-brand-400/80">
            Workspace
          </span>
          {!isHome && (
            <button
              type="button"
              onClick={onGoHome}
              title="Return to home workspace (exit)"
              className="flex items-center gap-0.5 text-gray-500 transition-colors hover:text-gray-200"
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
          className="-ml-2 mt-1.5 flex w-full cursor-pointer items-center gap-1.5 truncate rounded-md px-2 py-1 text-left font-mono text-sm text-gray-400 transition-colors hover:bg-ink-800 hover:text-gray-200"
        >
          <span className="shrink-0 text-sm">📂</span>
          <span className="truncate">{workspace || "(not set)"}</span>
        </button>

        {/* Recent workspaces dropdown */}
        {recentFiltered.length > 0 && (
          <div className="relative mt-1">
            <button
              type="button"
              onClick={(): void => setRecentOpen(!recentOpen)}
              className="flex w-full items-center gap-1 text-xs text-gray-500 transition-colors hover:text-gray-300"
            >
              <span className={`inline-block transition-transform ${recentOpen ? "rotate-90" : ""}`}>▸</span>
              <span>Recent</span>
              <span className="text-gray-600">({recentFiltered.length})</span>
            </button>
            {recentOpen && (
              <div className="mt-1 max-h-40 overflow-y-auto rounded-md border border-white/10 bg-ink-850">
                {recentFiltered.map((entry) => (
                  <button
                    key={entry.path}
                    type="button"
                    onClick={(): void => { handleRecentClick(entry.path); }}
                    title={entry.path}
                    className="flex w-full items-center gap-1.5 truncate px-2.5 py-1.5 text-left font-mono text-xs text-gray-400 transition-colors hover:bg-ink-800 hover:text-gray-200"
                  >
                    <span className="shrink-0 text-xs opacity-50">📁</span>
                    <span className="truncate">{entry.path}</span>
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
      />

      {/* Header */}
      <div className="border-b border-white/5 px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-brand-400/80">
          Conversations
        </h2>
      </div>

      {/* Pool selector badge */}
      {pools.length > 1 && (
        <div className="border-b border-white/5 px-4 py-3">
          <div className="relative">
            <select
              value={activePool}
              onChange={(e): void => onPoolChange(e.target.value)}
              className="w-full cursor-pointer appearance-none rounded-lg border border-white/15 bg-ink-850 py-3 pl-7 pr-10 text-base font-semibold text-gray-100 transition-colors hover:border-white/25 hover:bg-ink-800 focus:border-brand-500/50 focus:outline-none"
            >
              {pools.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
            <span className="pointer-events-none absolute left-2 top-1/2 h-2.5 w-2.5 -translate-y-1/2 rounded-full bg-brand-500 shadow-sm shadow-brand-500/30" />
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-base leading-none text-brand-300">
              ▾
            </span>
          </div>
        </div>
      )}

      {/* Session tree */}
      <div className="flex-1 overflow-y-auto py-2">
        {sessionTree.length === 0 ? (
          <p className="px-4 py-3 text-sm text-gray-500">
            No conversations in {activePool}
          </p>
        ) : (
          <SessionTree
            tree={sessionTree}
            selected={selected}
            onSelect={onSelect}
            onDelete={onDelete}
          />
        )}
      </div>

      {/* New Conversation button */}
      <div className="border-t border-white/5 p-3">
        <button
          type="button"
          onClick={handleNew}
          className="w-full rounded-lg bg-brand-500/10 px-3 py-2.5 text-sm font-semibold text-brand-300 transition-colors hover:bg-brand-500/20 hover:text-brand-200 active:bg-brand-500/15"
        >
          + New Conversation
        </button>
      </div>
    </div>
  );
};
