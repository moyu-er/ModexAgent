import { useState, type FC } from "react";

export interface SessionNodeData {
  session_id: string;
  displayName: string;
  pool: string;
  parent_session_id: string | null;
  created_at?: number;
  updated_at?: number;
}

export interface TreeNode extends SessionNodeData {
  children: TreeNode[];
}

export interface SessionTreeProps {
  tree: TreeNode[];
  selected: string | null;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
}

const SessionNode: FC<{
  node: TreeNode;
  depth: number;
  selected: string | null;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
}> = ({ node, depth, selected, onSelect, onDelete }) => {
  const [expanded, setExpanded] = useState(false);
  const hasChildren = node.children.length > 0;
  const isSelected = node.session_id === selected;
  const isRoot = node.parent_session_id === null;

  return (
    <div>
      <div className="flex items-stretch">
        {/* Tree guide lines — vertical lines + horizontal connector arms */}
        {depth > 0 ? (
          <div className="flex shrink-0">
            {Array.from({ length: depth }).map((_, level) => (
              <div key={level} className="relative" style={{ width: "20px" }}>
                <div className="absolute inset-y-0 left-1.5 border-l border-white/8" />
                {level === depth - 1 && (
                  <div className="absolute left-1.5 right-0 top-1/2 border-t border-white/8" />
                )}
              </div>
            ))}
          </div>
        ) : (
          /* Root node: small left gutter without guide lines */
          <div style={{ width: "8px" }} className="shrink-0" />
        )}

        {/* Node content row */}
        <div
          className={`my-0.5 flex min-w-0 flex-1 items-center rounded-r-md border-l-[3px] ${
            isSelected
              ? "border-brand-500 bg-brand-500/20 shadow-[inset_0_0_0_1px_rgba(139,143,247,0.18)]"
              : "border-transparent hover:bg-ink-800"
          }`}
        >
          {/* Expand arrow — only if has children */}
          {hasChildren ? (
            <button
              type="button"
              data-testid="expand-arrow"
              onClick={(): void => setExpanded(!expanded)}
              className="mr-2 shrink-0 text-[10px] leading-none text-gray-500 transition-colors hover:text-gray-200"
            >
              {expanded ? "▼" : "▶"}
            </button>
          ) : (
            <span className="mr-2 w-4 shrink-0" />
          )}

          {/* Session name */}
          <button
            type="button"
            onClick={(): void => onSelect(node.session_id)}
            className={`flex-1 truncate py-2 text-left font-mono text-sm transition-colors ${
              isSelected ? "text-brand-100" : "text-gray-400 hover:text-gray-200"
            }`}
          >
            {node.displayName}
          </button>

          {/* Delete — only for root sessions */}
          {isRoot && (
            <button
              type="button"
              onClick={(e): void => {
                e.stopPropagation();
                onDelete(node.session_id);
              }}
              title="Delete conversation"
              className="shrink-0 rounded px-2 py-2 text-sm text-gray-600 transition-colors hover:bg-red-400/10 hover:text-red-400"
            >
              {"✕"}
            </button>
          )}
        </div>
      </div>

      {/* Children (rendered when expanded) */}
      {expanded &&
        node.children.map((child) => (
          <SessionNode
            key={child.session_id}
            node={child}
            depth={depth + 1}
            selected={selected}
            onSelect={onSelect}
            onDelete={onDelete}
          />
        ))}
    </div>
  );
};

export const SessionTree: FC<SessionTreeProps> = ({
  tree,
  selected,
  onSelect,
  onDelete,
}) => {
  return (
    <>
      {tree.map((node) => (
        <SessionNode
          key={node.session_id}
          node={node}
          depth={0}
          selected={selected}
          onSelect={onSelect}
          onDelete={onDelete}
        />
      ))}
    </>
  );
};
