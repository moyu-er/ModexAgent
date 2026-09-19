// sessionTree title display + metadata propagation (PA-02).
//
// Contract (DESIGN §2.6): one shared display function — metadata.title that
// trims to a non-empty string renders the title; anything else (missing,
// non-string, whitespace-only) renders the FULL session id. No preview, no
// placeholder. Metadata flows list → tree node unchanged; there is no second
// mutable title store.

import { describe, it, expect } from "vitest";
import { buildTree, sessionDisplayTitle } from "./sessionTree";
import type { ConversationInfo } from "../types/events";

function session(overrides: Partial<ConversationInfo>): ConversationInfo {
  return {
    session_id: "abc123.main",
    agent_name: "main",
    pool: "main",
    parent_session_id: null,
    ...overrides,
  };
}

describe("sessionDisplayTitle", () => {
  it("returns a plain non-empty string title as-is", () => {
    expect(sessionDisplayTitle("abc.main", { title: "Trip plan" })).toBe("Trip plan");
  });

  it("trims and accepts a title with surrounding whitespace", () => {
    expect(sessionDisplayTitle("abc.main", { title: "  Trip plan  " })).toBe("Trip plan");
  });

  it("falls back to the FULL session id when title is missing", () => {
    expect(sessionDisplayTitle("abc123.main", {})).toBe("abc123.main");
  });

  it("falls back to the full session id when metadata is undefined", () => {
    expect(sessionDisplayTitle("abc123.main", undefined)).toBe("abc123.main");
  });

  it("falls back when title is a non-string (number, object, null)", () => {
    expect(sessionDisplayTitle("abc.main", { title: 42 })).toBe("abc.main");
    expect(sessionDisplayTitle("abc.main", { title: { deep: true } })).toBe("abc.main");
    expect(sessionDisplayTitle("abc.main", { title: null })).toBe("abc.main");
  });

  it("falls back when title is empty or whitespace-only", () => {
    expect(sessionDisplayTitle("abc.main", { title: "" })).toBe("abc.main");
    expect(sessionDisplayTitle("abc.main", { title: "   " })).toBe("abc.main");
    expect(sessionDisplayTitle("abc.main", { title: "\t\n" })).toBe("abc.main");
  });

  it("keeps other metadata keys intact (callers read them separately)", () => {
    const meta = { title: "Keep me", session_tree_paused: true };
    expect(sessionDisplayTitle("abc.main", meta)).toBe("Keep me");
    expect(meta.session_tree_paused).toBe(true);
  });
});

describe("buildTree metadata propagation", () => {
  it("carries metadata from the list onto root tree nodes", () => {
    const tree = buildTree([
      session({ session_id: "abc.main", metadata: { title: "Root title" } }),
    ]);
    expect(tree[0]!.metadata).toEqual({ title: "Root title" });
    expect(tree[0]!.displayName).toBe("abc.main");
  });

  it("carries metadata onto child nodes as well", () => {
    const tree = buildTree([
      session({ session_id: "abc.main", updated_at: 10 }),
      session({
        session_id: "abc.main.explore",
        agent_name: "explore",
        parent_session_id: "abc.main",
        metadata: { title: "Child title" },
        updated_at: 20,
      }),
    ]);
    expect(tree[0]!.children).toHaveLength(1);
    expect(tree[0]!.children[0]!.metadata).toEqual({ title: "Child title" });
  });

  it("leaves metadata undefined for legacy sessions without it", () => {
    const tree = buildTree([session({ session_id: "abc.main" })]);
    expect(tree[0]!.metadata).toBeUndefined();
  });
});
