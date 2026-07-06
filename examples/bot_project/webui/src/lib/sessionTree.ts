/** Pure helpers for building the conversation (session) tree shown in the sidebar. */

import type { ConversationInfo } from "../types/events";

/** A session's display-relevant fields (no tree structure). */
export interface SessionNodeData {
  session_id: string;
  displayName: string;
  pool: string;
  parent_session_id: string | null;
  created_at?: number;
  updated_at?: number;
}

/** A session node with its resolved child subtree. */
export interface TreeNode extends SessionNodeData {
  children: TreeNode[];
}

/**
 * Derive a short display name for a child by stripping the shared prefix it
 * inherits from its parent session id, plus the trailing ``.`` separator.
 */
export function computeDisplayName(sessionId: string, parentId?: string): string {
  if (parentId) {
    let i = 0;
    while (i < sessionId.length && i < parentId.length && sessionId[i] === parentId[i]) {
      i++;
    }
    while (i > 0 && sessionId[i - 1] !== ".") {
      i--;
    }
    return sessionId.slice(i) || sessionId;
  }
  return sessionId;
}

/** Build a parent→children tree from a flat conversation list, newest-first per group. */
export function buildTree(sessions: ConversationInfo[]): TreeNode[] {
  const childrenMap = new Map<string, ConversationInfo[]>();
  const roots: ConversationInfo[] = [];

  for (const s of sessions) {
    if (s.parent_session_id) {
      const siblings = childrenMap.get(s.parent_session_id) || [];
      siblings.push(s);
      siblings.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
      childrenMap.set(s.parent_session_id, siblings);
    } else {
      roots.push(s);
    }
  }
  roots.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));

  function toTreeNode(s: ConversationInfo, parentId?: string): TreeNode {
    return {
      ...s,
      displayName: computeDisplayName(s.session_id, parentId),
      pool: s.pool || "main",
      parent_session_id: s.parent_session_id,
      children: (childrenMap.get(s.session_id) || []).map((c) =>
        toTreeNode(c, s.session_id),
      ),
    };
  }

  return roots.map((r) => toTreeNode(r));
}
