import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ChatView } from "./ChatView";
import type { ApprovalRequestView, UIMessage } from "../types/events";

const baseProps = {
  messages: [] as UIMessage[],
  isStreaming: false,
  isPending: false,
  todos: [],
  submitApproval: vi.fn(),
  onSend: vi.fn(),
};

const pending: ApprovalRequestView[] = [
  {
    tool_call_id: "c1",
    tool_name: "write_file",
    tier: "dangerous",
    arguments: { path: "/tmp/x" },
    status: "pending",
  },
  {
    tool_call_id: "c2",
    tool_name: "shell",
    tier: "dangerous",
    arguments: { cmd: "ls" },
    status: "pending",
  },
];

describe("ChatView approval UI", () => {
  it("renders the hint + [Approve All] when there are pending approvals", () => {
    render(
      <ChatView
        {...baseProps}
        pendingApprovals={pending}
        isApprovingBatch={false}
        onApproveAll={vi.fn()}
      />,
    );
    expect(screen.getByText("Denying any one cancels the whole batch")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /approve all/i }),
    ).toBeTruthy();
  });

  it("does not render the header when there are no pending approvals", () => {
    render(
      <ChatView
        {...baseProps}
        pendingApprovals={[]}
        isApprovingBatch={false}
        onApproveAll={vi.fn()}
      />,
    );
    expect(screen.queryByText("Denying any one cancels the whole batch")).toBeNull();
    expect(screen.queryByRole("button", { name: /approve all/i })).toBeNull();
  });

  it("calls onApproveAll when [Approve All] is clicked", () => {
    const onApproveAll = vi.fn();
    render(
      <ChatView
        {...baseProps}
        pendingApprovals={pending}
        isApprovingBatch={false}
        onApproveAll={onApproveAll}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /approve all/i }));
    expect(onApproveAll).toHaveBeenCalledTimes(1);
  });

  it("disables [Approve All] and per-card buttons when isApprovingBatch", () => {
    render(
      <ChatView
        {...baseProps}
        pendingApprovals={pending}
        isApprovingBatch={true}
        onApproveAll={vi.fn()}
      />,
    );
    expect(
      (screen.getByRole("button", { name: /approve all/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    // Every per-card Approve + Deny All should be disabled too.
    const approveBtns = screen.getAllByRole("button", { name: /^Approve$/i });
    const denyBtns = screen.getAllByRole("button", { name: /Deny All/i });
    expect(approveBtns.every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    expect(denyBtns.every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
  });
});
