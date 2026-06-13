import { useState, type FC } from "react";

export interface SessionNodeData {
  session_id: string;
  displayName: string;
  pool: string;
  parent_session_id: string | null;
  created_at?: number;
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
                {/* Vertical guide line through this depth level */}
                <div className="absolute inset-y-0 left-1.5 border-l border-gray-500/60" />
                {/* Deepest level: horizontal connector arm linking guide to node */}
                {level === depth - 1 && (
                  <div className="absolute top-1/2 left-1.5 right-0 border-t border-gray-500/60" />
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
          className={`flex-1 flex items-center min-w-0 my-0.5 border-l-[3px] rounded-r-md ${
            isSelected
              ? "bg-blue-500/10 border-blue-400"
              : "border-transparent hover:bg-gray-800/60"
          }`}
        >
          {/* Expand arrow — only if has children */}
          {hasChildren ? (
            <button
              type="button"
              data-testid="expand-arrow"
              onClick={(): void => setExpanded(!expanded)}
              className="w-5 h-5 flex items-center justify-center text-gray-500 hover:text-gray-200 transition-colors shrink-0 text-sm"
            >
              {expanded ? "▼" : "▶"}
            </button>
          ) : (
            <span className="w-5 shrink-0" />
          )}

          {/* Session name */}
          <button
            type="button"
            onClick={(): void => onSelect(node.session_id)}
            className="flex-1 text-left py-1.5 text-sm text-gray-400 hover:text-gray-200 transition-colors truncate font-mono"
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
              className="px-2 py-1.5 text-gray-600 hover:text-red-400 hover:bg-red-400/10 rounded transition-colors text-sm shrink-0"
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
