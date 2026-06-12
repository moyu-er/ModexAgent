import { useState, type FC } from "react";
import type { ConversationInfo } from "../types/events";
import type { PoolInfo } from "../lib/api";

export interface SidebarProps {
  conversations: ConversationInfo[];
  pools: PoolInfo[];
  selected: string | null;
  workspace: string;
  onSelect: (conversationId: string) => void;
  onNew: (pool: string) => void;
}

export const Sidebar: FC<SidebarProps> = ({
  conversations,
  pools,
  selected,
  workspace,
  onSelect,
  onNew,
}) => {
  const [activePool, setActivePool] = useState<string>(
    pools.length > 0 ? pools[0].name : "main",
  );

  // Filter conversations to the active pool
  const poolConvs = conversations.filter(
    (c) => (c.pool || "main") === activePool,
  );

  const handleNew = (): void => {
    onNew(activePool);
  };

  return (
    <div className="w-full bg-gray-900 border-r border-gray-800 flex flex-col h-full">
      {/* Workspace indicator */}
      {workspace && (
        <div className="px-4 py-2 border-b border-gray-800">
          <span className="text-[9px] font-semibold text-gray-600 uppercase tracking-wider">
            Workspace
          </span>
          <p className="text-[11px] text-gray-400 font-mono truncate mt-0.5">
            {workspace}
          </p>
        </div>
      )}

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
            onChange={(e): void => setActivePool(e.target.value)}
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

      {/* Conversation list (filtered by pool) */}
      <div className="flex-1 overflow-y-auto">
        {poolConvs.length === 0 && (
          <p className="px-4 py-3 text-xs text-gray-600">
            No conversations in {activePool}
          </p>
        )}
        {poolConvs.map((conv) => {
          const isSelected = conv.conversation_id === selected;
          return (
            <button
              key={conv.conversation_id}
              type="button"
              onClick={(): void => onSelect(conv.conversation_id)}
              className={`w-full text-left px-4 py-2 text-sm transition-colors border-l-2 ${
                isSelected
                  ? "bg-gray-800 border-blue-500 text-gray-100"
                  : "border-transparent text-gray-400 hover:bg-gray-800/50 hover:text-gray-300"
              }`}
            >
              <span className="block truncate font-mono text-[11px]">
                {conv.conversation_id}
              </span>
            </button>
          );
        })}
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
