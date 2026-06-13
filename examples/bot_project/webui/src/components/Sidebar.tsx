import { useState, type FC } from "react";
import type { PoolInfo } from "../lib/api";
import { WorkspaceBrowser } from "./WorkspaceBrowser";
import { SessionTree, type TreeNode } from "./SessionTree";

export interface SidebarProps {
  sessionTree: TreeNode[];
  pools: PoolInfo[];
  selected: string | null;
  workspace: string;
  isHome: boolean;
  activePool: string;
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
  onSelect,
  onNew,
  onDelete,
  onWorkspaceChanged,
  onGoHome,
  onPoolChange,
}) => {
  const [browserOpen, setBrowserOpen] = useState(false);

  const handleNew = (): void => {
    onNew(activePool);
  };

  return (
    <div className="w-full bg-gray-900 border-r border-gray-800 flex flex-col h-full">
      {/* Workspace indicator (click to browse) */}
      <div className="px-3 py-2.5 border-b border-gray-700/60">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
            Workspace
          </span>
          {!isHome && (
            <button
              type="button"
              onClick={onGoHome}
              title="Return to home workspace (exit)"
              className="text-gray-500 hover:text-gray-200 transition-colors flex items-center gap-0.5"
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
          className="w-full text-left text-sm text-gray-400 font-mono truncate mt-1 hover:text-gray-200 hover:bg-gray-800/60 rounded-md px-2 py-1 -ml-2 transition-colors cursor-pointer flex items-center gap-1.5"
        >
          <span className="shrink-0 text-sm">📂</span>
          <span className="truncate">{workspace || "(not set)"}</span>
        </button>
      </div>

      {/* Workspace file browser modal */}
      <WorkspaceBrowser
        open={browserOpen}
        onClose={(): void => setBrowserOpen(false)}
        onChanged={(cwd): void => onWorkspaceChanged(cwd)}
      />

      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-700/60">
        <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
          Conversations
        </h2>
      </div>

      {/* Pool selector badge */}
      {pools.length > 1 && (
        <div className="px-4 py-2.5 border-b border-gray-700/60">
          <div className="relative">
            <select
              value={activePool}
              onChange={(e): void => onPoolChange(e.target.value)}
              className="w-full appearance-none bg-gray-800/70 border border-gray-700 rounded-lg pl-8 pr-8 py-2 text-sm font-semibold text-gray-200 focus:outline-none focus:border-blue-400/50 focus:bg-gray-800 cursor-pointer transition-colors"
            >
              {pools.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
            <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 w-2 h-2 rounded-full bg-blue-400" />
            <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-500 text-xs">
              ▾
            </span>
          </div>
        </div>
      )}

      {/* Session tree */}
      <div className="flex-1 overflow-y-auto py-1">
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
      <div className="p-3 border-t border-gray-700/60">
        <button
          type="button"
          onClick={handleNew}
          className="w-full py-2.5 px-3 text-sm font-semibold rounded-lg bg-blue-600 text-white hover:bg-blue-500 active:bg-blue-700 transition-colors"
        >
          + New Conversation
        </button>
      </div>
    </div>
  );
};
