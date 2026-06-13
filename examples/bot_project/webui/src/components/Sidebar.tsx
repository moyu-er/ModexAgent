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
      <div className="px-3 py-2 border-b border-gray-800">
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-semibold text-gray-600 uppercase tracking-wider">
            Workspace
          </span>
          {!isHome && (
            <button
              type="button"
              onClick={onGoHome}
              title="Return to home workspace (exit)"
              className="text-gray-500 hover:text-gray-200 transition-colors flex items-center gap-0.5"
            >
              <span className="text-[10px]">↩</span>
              <span className="text-[10px] font-medium">Home</span>
            </button>
          )}
        </div>
        <button
          type="button"
          onClick={(): void => setBrowserOpen(true)}
          title="Browse for workspace folder"
          className="w-full text-left text-xs text-gray-400 font-mono truncate mt-0.5 hover:text-gray-200 hover:bg-gray-800/50 rounded px-1 py-0.5 -ml-1 transition-colors cursor-pointer flex items-center gap-1"
        >
          <span className="shrink-0 text-xs">📂</span>
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
      <div className="p-4 border-b border-gray-800">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">
          Conversations
        </h2>
      </div>

      {/* Pool selector dropdown */}
      {pools.length > 1 && (
        <div className="px-4 py-2 border-b border-gray-800">
          <select
            value={activePool}
            onChange={(e): void => onPoolChange(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 focus:outline-none focus:border-blue-500"
          >
            {pools.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Session tree */}
      <div className="flex-1 overflow-y-auto">
        {sessionTree.length === 0 ? (
          <p className="px-4 py-3 text-xs text-gray-500">
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
      <div className="p-3 border-t border-gray-800">
        <button
          type="button"
          onClick={handleNew}
          className="w-full py-2 px-3 text-sm font-medium rounded bg-blue-600 text-white hover:bg-blue-500 transition-colors"
        >
          + New Conversation
        </button>
      </div>
    </div>
  );
};
