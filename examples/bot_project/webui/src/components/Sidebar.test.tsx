// Sidebar: search filters the tree by title/session id; the top pool
// selector (the single selection — history filter AND new-conversation
// target) drives onPoolChange; the new-conversation button reports the
// active pool. No settings entry — Settings lives only in the top-right
// WorkspaceTabBar gear.

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Sidebar } from "./Sidebar";
import { ToastProvider } from "./ToastContext";
import type { TreeNode } from "./SessionTree";

function node(overrides: Partial<TreeNode>): TreeNode {
  return {
    session_id: "abc.main",
    displayName: "abc.main",
    pool: "main",
    parent_session_id: null,
    children: [],
    ...overrides,
  } as TreeNode;
}

const TREE: TreeNode[] = [
  node({
    session_id: "trip.main",
    displayName: "trip.main",
    metadata: { title: "杭州旅行规划" },
    children: [
      node({
        session_id: "trip.child",
        displayName: "child",
        parent_session_id: "trip.main",
      }),
    ],
  }),
  node({ session_id: "coder.explore", displayName: "coder.explore", pool: "coder" }),
];

function renderSidebar(props: Partial<React.ComponentProps<typeof Sidebar>> = {}) {
  return render(
    <ToastProvider>
      <Sidebar
        sessionTree={TREE}
        pools={[{ name: "main" }, { name: "coder" }]}
        selected={null}
        workspacePath="/home"
        activePool="main"
        mobileOpen
        onCloseMobile={() => {}}
        onSelect={() => {}}
        onNew={() => {}}
        onDelete={() => {}}
        onPoolChange={() => {}}
        {...props}
      />
    </ToastProvider>,
  );
}

describe("Sidebar search (PA-09)", () => {
  it("filters by title", () => {
    renderSidebar();
    expect(screen.getByText("杭州旅行规划")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search"), {
      target: { value: "旅行" },
    });
    expect(screen.getByText("杭州旅行规划")).toBeTruthy();
    expect(screen.queryByText("coder.explore")).toBeNull();
  });

  it("matches keep a matching child and its parent (tree context)", () => {
    renderSidebar();
    fireEvent.change(screen.getByLabelText("Search"), {
      target: { value: "trip.child" },
    });
    // The parent stays visible as the ancestor path of the matching child.
    expect(screen.getByText("杭州旅行规划")).toBeTruthy();
    expect(screen.queryByText("coder.explore")).toBeNull();
  });

  it("filters by session id too", () => {
    renderSidebar();
    fireEvent.change(screen.getByLabelText("Search"), {
      target: { value: "coder.explore" },
    });
    expect(screen.getByText("coder.explore")).toBeTruthy();
    expect(screen.queryByText("杭州旅行规划")).toBeNull();
  });

  it("shows a no-match message and clear button", () => {
    renderSidebar();
    fireEvent.change(screen.getByLabelText("Search"), {
      target: { value: "nothing-matches" },
    });
    expect(screen.getByText('No settings match "nothing-matches".')).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Clear search"));
    expect(screen.getByText("杭州旅行规划")).toBeTruthy();
  });
});

describe("Sidebar top pool selector (single selection)", () => {
  it("renders the selector above the search row even with one pool", () => {
    renderSidebar({ pools: [{ name: "main" }] });
    const selector = screen.getByLabelText("Agent pool");
    const search = screen.getByLabelText("Search");
    // The selector sits above the search row (compare DOM position).
    expect(
      selector.compareDocumentPosition(search) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("reports pool changes", () => {
    const onPoolChange = vi.fn();
    renderSidebar({ onPoolChange });
    fireEvent.click(screen.getByLabelText("Agent pool"));
    fireEvent.click(screen.getByRole("option", { name: "coder" }));
    expect(onPoolChange).toHaveBeenCalledWith("coder");
  });

  it("shows a select-pool placeholder when no pool is selected", () => {
    renderSidebar({ activePool: "" });
    expect(screen.getByText("Select a pool…")).toBeTruthy();
  });

  it("new conversation reports the active pool", () => {
    const onNew = vi.fn();
    renderSidebar({ onNew });
    fireEvent.click(screen.getByText("New Conversation"));
    expect(onNew).toHaveBeenCalledWith("main");
  });

  it("has no settings entry — Settings lives only in the tab-bar gear", () => {
    renderSidebar({ onOpenGraphs: () => {} });
    expect(screen.queryByRole("button", { name: "Settings" })).toBeNull();
    expect(screen.getByRole("button", { name: "Graphs" })).toBeTruthy();
  });

  it("pin next to the path header saves the current path as default", () => {
    const onSetDefaultWorkspace = vi.fn();
    renderSidebar({ onSetDefaultWorkspace });
    const pin = screen.getByRole("button", { name: "Set as default workspace" });
    expect(pin.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(pin);
    expect(onSetDefaultWorkspace).toHaveBeenCalledWith("/home");
  });

  it("pin shows the pressed state when the path is the saved default", () => {
    renderSidebar({ isDefaultWorkspace: true, onSetDefaultWorkspace: () => {} });
    const pin = screen.getByRole("button", { name: "Set as default workspace" });
    expect(pin.getAttribute("aria-pressed")).toBe("true");
    expect(pin.getAttribute("title")).toContain("/home");
  });

  it("renders no pin when no callback is provided", () => {
    renderSidebar();
    expect(
      screen.queryByRole("button", { name: "Set as default workspace" }),
    ).toBeNull();
  });
});
