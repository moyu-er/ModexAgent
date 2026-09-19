// SessionTree title display + rename entry (PA-02).
//
// Contract: node labels use sessionDisplayTitle (title when set, FULL
// session id otherwise — for both roots and children); every node row
// exposes a Rename affordance (root-visible; children hover) that invokes
// the single shared onRename callback.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SessionTree } from "./SessionTree";
import { buildTree } from "../lib/sessionTree";
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

afterEach(() => {
  vi.clearAllMocks();
});

describe("SessionTree title display", () => {
  it("renders the metadata title for a titled root session", () => {
    const tree = buildTree([session({ metadata: { title: "Trip plan" } })]);
    render(
      <SessionTree tree={tree} selected={null} onSelect={vi.fn()} onDelete={vi.fn()} onRename={vi.fn()} />,
    );
    expect(screen.getByText("Trip plan")).toBeTruthy();
    expect(screen.queryByText("abc123.main")).toBeNull();
  });

  it("renders the FULL session id for untitled and whitespace-title sessions", () => {
    const tree = buildTree([
      session({ session_id: "abc123.main", updated_at: 5 }),
      session({
        session_id: "zzz999.main",
        updated_at: 10,
        metadata: { title: "   " },
      }),
    ]);
    render(
      <SessionTree tree={tree} selected={null} onSelect={vi.fn()} onDelete={vi.fn()} onRename={vi.fn()} />,
    );
    expect(screen.getByText("abc123.main")).toBeTruthy();
    expect(screen.getByText("zzz999.main")).toBeTruthy();
  });

  it("renders child titles too (non-string title falls back to id)", () => {
    const tree = buildTree([
      session({ session_id: "abc.main", updated_at: 1 }),
      session({
        session_id: "abc.main.explore",
        agent_name: "explore",
        parent_session_id: "abc.main",
        metadata: { title: 7 },
        updated_at: 2,
      }),
    ]);
    render(
      <SessionTree
        tree={tree}
        selected={null}
        onSelect={vi.fn()}
        onDelete={vi.fn()}
        onRename={vi.fn()}
        revealSessionId="abc.main.explore"
      />,
    );
    expect(screen.getByText("abc.main.explore")).toBeTruthy();
  });
});

describe("SessionTree rename entry", () => {
  it("root rows expose a Rename affordance that calls onRename(sessionId)", () => {
    const onRename = vi.fn();
    const tree = buildTree([session({ session_id: "abc123.main" })]);
    render(
      <SessionTree tree={tree} selected={null} onSelect={vi.fn()} onDelete={vi.fn()} onRename={onRename} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Rename conversation" }));
    expect(onRename).toHaveBeenCalledWith("abc123.main");
  });

  it("child rows also rename through the same single callback", () => {
    const onRename = vi.fn();
    const tree = buildTree([
      session({ session_id: "abc.main", updated_at: 1 }),
      session({
        session_id: "abc.main.explore",
        agent_name: "explore",
        parent_session_id: "abc.main",
        updated_at: 2,
      }),
    ]);
    render(
      <SessionTree
        tree={tree}
        selected={null}
        onSelect={vi.fn()}
        onDelete={vi.fn()}
        onRename={onRename}
        revealSessionId="abc.main.explore"
      />,
    );
    const renameButtons = screen.getAllByRole("button", {
      name: "Rename conversation",
    });
    expect(renameButtons.length).toBe(2);
    fireEvent.click(renameButtons[1]!);
    expect(onRename).toHaveBeenCalledWith("abc.main.explore");
  });
});
