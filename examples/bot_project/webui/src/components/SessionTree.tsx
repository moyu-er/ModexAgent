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
      <div
        className={`flex items-center border-l-2 ${
          isSelected
            ? "bg-gray-800 border-blue-500"
            : "border-transparent hover:bg-gray-800/50"
        }`}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
      >
        {/* Expand arrow — only if has children */}
        {hasChildren ? (
          <button
            type="button"
            data-testid="expand-arrow"
            onClick={(): void => setExpanded(!expanded)}
            className="w-5 h-5 flex items-center justify-center text-gray-500 hover:text-gray-300 transition-colors shrink-0 text-xs"
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
          className="flex-1 text-left py-2 text-sm text-gray-400 hover:text-gray-300 transition-colors truncate font-mono text-xs"
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
            className="px-2 py-2 text-gray-600 hover:text-red-400 transition-colors text-xs shrink-0"
          >
            {"✕"}
          </button>
        )}
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
