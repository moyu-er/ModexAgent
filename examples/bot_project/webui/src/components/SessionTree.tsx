import { useEffect, useRef, useState, type FC } from "react";
import { MessageSquare } from "lucide-react";
import { formatShort } from "../lib/timezone";
import type { TreeNode } from "../lib/sessionTree";
import { ChevronToggleIcon, XIcon } from "./ui/icons";
import { IconButton } from "./ui/IconButton";

// Re-export so existing `import { TreeNode } from "./SessionTree"` callers
// (e.g. Sidebar) keep working without reaching into the lib directly.
export type { TreeNode };

export interface SessionTreeProps {
  tree: TreeNode[];
  selected: string | null;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
  /**
   * A session id that should be made visible.  When it changes (e.g. a new
   * subagent session is produced), every ancestor up to the root is expanded
   * so the node appears in the tree — cascading through grandparents.
   */
  revealSessionId?: string | null;
}

/** Flatten the tree into a session_id → parent_session_id map. */
function buildParentMap(nodes: TreeNode[]): Map<string, string | null> {
  const map = new Map<string, string | null>();
  const walk = (node: TreeNode): void => {
    map.set(node.session_id, node.parent_session_id);
    node.children.forEach(walk);
  };
  nodes.forEach(walk);
  return map;
}

/** Return the ancestor chain of *id* (parent, grandparent, …, root). */
function ancestorsOf(
  id: string,
  parentMap: Map<string, string | null>,
): string[] {
  const chain: string[] = [];
  let current = parentMap.get(id);
  const guard = new Set<string>();
  while (current && !guard.has(current)) {
    chain.push(current);
    guard.add(current);
    current = parentMap.get(current) ?? null;
  }
  return chain;
}

const SessionNode: FC<{
  node: TreeNode;
  depth: number;
  expanded: boolean;
  selected: string | null;
  onSelect: (sessionId: string) => void;
  onToggleExpand: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
}> = ({ node, depth, expanded, selected, onSelect, onToggleExpand, onDelete }) => {
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
                <div className="absolute inset-y-0 left-1.5 border-l border-hairline/60" />
                {level === depth - 1 && (
                  <div className="absolute left-1.5 right-0 top-1/2 border-t border-hairline/60" />
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
          className={`session-row my-0.5 flex min-w-0 flex-1 items-center rounded-r-sm${
            isSelected ? " active" : ""
          }`}
        >
          {/* Expand/collapse area — fixed-width so leaf nodes align with
           * branch nodes and the session ID stays at the same x-position. */}
          <div className="mr-2 flex h-7 w-4 shrink-0 items-center justify-center">
            {hasChildren && (
              <IconButton
                label={expanded ? "Collapse" : "Expand"}
                size="sm"
                variant="ghost"
                data-testid="expand-arrow"
                onClick={(): void => onToggleExpand(node.session_id)}
                icon={<ChevronToggleIcon open={expanded} />}
                className="h-7 w-4 rounded-sm text-mute hover:text-ink focus-visible:ring-link/50"
              />
            )}
          </div>

          {/* Session name — kept as a styled button because the row is a
           * composite clickable tree entry (icon + truncated name +
           * timestamp). The semantic role of the element is a list-row
           * selection target rather than a discrete action button.
           * Geist tokens are used for every visual decision. */}
          <button
            type="button"
            onClick={(): void => onSelect(node.session_id)}
            className={`flex-1 min-w-0 py-1.5 text-left font-mono text-sm transition-colors ${
              isSelected
                ? "text-link"
                : "text-mute hover:text-ink"
            }`}
          >
            <span className="flex items-center gap-1.5">
              {isRoot && (
                <MessageSquare size={13} className="shrink-0 text-faint" />
              )}
              <span className="truncate">{node.displayName}</span>
            </span>
            {typeof node.updated_at === "number" && (
              <span className="block truncate text-[10px] font-sans text-faint">
                {formatShort(node.updated_at)}
              </span>
            )}
          </button>

          {/* Delete — only for root sessions */}
          {isRoot && (
            <IconButton
              label="Delete conversation"
              size="sm"
              variant="ghost"
              onClick={(e): void => {
                e.stopPropagation();
                onDelete(node.session_id);
              }}
              icon={<XIcon className="h-3.5 w-3.5" />}
              className="text-faint hover:text-error focus-visible:ring-link/50"
            />
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
            expanded={expanded}
            selected={selected}
            onSelect={onSelect}
            onToggleExpand={onToggleExpand}
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
  revealSessionId = null,
}) => {
  // Expansion is owned here so a reveal can cascade-expand ancestor chains
  // without clobbering per-node toggle state.  All nodes start collapsed.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Remember the last reveal we acted on so a manual collapse afterward is
  // respected (we don't re-expand on every unrelated tree update).
  const consumedRevealRef = useRef<string | null>(null);

  useEffect(() => {
    if (!revealSessionId) return;
    // Only react to a *new* reveal target.
    if (revealSessionId === consumedRevealRef.current) return;

    const parentMap = buildParentMap(tree);
    // The target session isn't in the tree yet (backend refresh pending) —
    // wait for the next tree update before expanding.
    if (!parentMap.has(revealSessionId)) return;

    const chain = ancestorsOf(revealSessionId, parentMap);
    if (chain.length > 0) {
      setExpanded((prev) => {
        const next = new Set(prev);
        for (const id of chain) next.add(id);
        return next;
      });
    }
    consumedRevealRef.current = revealSessionId;
  }, [revealSessionId, tree]);

  const toggle = (sessionId: string): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(sessionId)) {
        next.delete(sessionId);
      } else {
        next.add(sessionId);
      }
      return next;
    });
  };

  return (
    <>
      {tree.map((node) => (
        <SessionNode
          key={node.session_id}
          node={node}
          depth={0}
          expanded={expanded.has(node.session_id)}
          selected={selected}
          onSelect={onSelect}
          onToggleExpand={toggle}
          onDelete={onDelete}
        />
      ))}
    </>
  );
};
