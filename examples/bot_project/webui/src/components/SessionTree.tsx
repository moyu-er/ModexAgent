import { useState, type FC } from "react";
import { formatShort } from "../lib/timezone";

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
  const hasChildren = node.children.length > 0;
  const isSelected = node.session_id === selected;
  const isRoot = node.parent_session_id === null;
  const [expanded, setExpanded] = useState(false);

  return (
    <div>
      <div className="flex items-stretch">
        {/* Tree guide lines — vertical lines + horizontal connector arms */}
        {depth > 0 ? (
          <div className="flex shrink-0">
            {Array.from({ length: depth }).map((_, level) => (
              <div key={level} className="relative" style={{ width: "20px" }}>
                <div className="absolute inset-y-0 left-1.5 border-l border-divider-light/60 dark:border-divider-dark/60" />
                {level === depth - 1 && (
                  <div className="absolute left-1.5 right-0 top-1/2 border-t border-divider-light/60 dark:border-divider-dark/60" />
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
              ? "border-ai-brand-light dark:border-ai-brand-dark bg-sidebar-hover-light dark:bg-sidebar-hover-dark ring-1 ring-inset ring-ai-brand-light/20 dark:ring-ai-brand-dark/20"
              : "border-transparent hover:bg-sidebar-hover-light dark:hover:bg-sidebar-hover-dark"
          }`}
        >
          {/* Expand arrow — only if has children */}
          {hasChildren ? (
            <button
              type="button"
              data-testid="expand-arrow"
              onClick={(): void => setExpanded(!expanded)}
              className="mr-2 w-4 shrink-0 text-center text-[10px] leading-none text-text-secondary-light dark:text-text-secondary-dark transition-colors hover:text-text-primary-light dark:hover:text-text-primary-dark"
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
            className={`flex-1 min-w-0 py-1.5 text-left font-mono text-sm transition-colors ${
              isSelected
                ? "text-ai-brand-light dark:text-ai-brand-dark"
                : "text-text-secondary-light dark:text-text-secondary-dark hover:text-text-primary-light dark:hover:text-text-primary-dark"
            }`}
          >
            <span className="block truncate">{node.displayName}</span>
            {typeof node.updated_at === "number" && (
              <span className="block truncate text-[10px] font-sans text-text-disabled-light dark:text-text-disabled-dark">
                {formatShort(node.updated_at)}
              </span>
            )}
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
              className="shrink-0 rounded px-2 py-2 text-sm text-text-disabled-light dark:text-text-disabled-dark transition-colors hover:text-error-light dark:hover:text-error-dark"
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
