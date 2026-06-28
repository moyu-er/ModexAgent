import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ApprovalCard } from "./ApprovalCard";
import type { ApprovalRequestView } from "../types/events";

const view: ApprovalRequestView = {
  tool_call_id: "c1",
  tool_name: "write_file",
  tier: "dangerous",
  arguments: { path: "/tmp/x" },
  status: "pending",
};

const longView: ApprovalRequestView = {
  tool_call_id: "c2",
  tool_name: "edit_file",
  tier: "safe",
  arguments: {
    path: "/a/really/long/path/to/some/file/that/should/be/truncated.ts",
    content: "x".repeat(400),
    extra: "y".repeat(200),
  },
  status: "pending",
};

describe("ApprovalCard", () => {
  it("renders tool name + tier badge + truncated args by default", () => {
    render(<ApprovalCard view={view} onApprove={vi.fn()} onDeny={vi.fn()} />);
    expect(screen.getByText("write_file")).toBeTruthy();
    expect(screen.getByText("dangerous")).toBeTruthy();
    // mono args block shows the serialized arguments
    expect(screen.getByText(/tmp\/x/)).toBeTruthy();
  });

  it("truncates long args by default and expands to full on toggle", () => {
    render(<ApprovalCard view={longView} onApprove={vi.fn()} onDeny={vi.fn()} />);
    const toggle = screen.getByRole("button", { name: /expand arguments/i });
    // Collapsed state: a preview with ellipsis is shown, not the full blob.
    expect(screen.getByText(/…$/)).toBeTruthy();
    expect(screen.queryByText(/y{200}/)).toBeNull();

    fireEvent.click(toggle);
    // Expanded state: full content (the 200-char run of "y") is now visible.
    expect(screen.getByText(/y{200}/)).toBeTruthy();
  });

  it("calls onApprove with tool_call_id", () => {
    const onApprove = vi.fn();
    render(<ApprovalCard view={view} onApprove={onApprove} onDeny={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Approve$/i }));
    expect(onApprove).toHaveBeenCalledWith("c1");
  });

  it("calls onDeny with tool_call_id (Deny All)", () => {
    const onDeny = vi.fn();
    render(<ApprovalCard view={view} onApprove={vi.fn()} onDeny={onDeny} />);
    fireEvent.click(screen.getByRole("button", { name: /Deny All/i }));
    expect(onDeny).toHaveBeenCalledWith("c1");
  });

  it("disables both buttons when disabled prop is set", () => {
    render(
      <ApprovalCard
        view={view}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
        disabled={true}
      />,
    );
    expect(
      (screen.getByRole("button", { name: /^Approve$/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: /Deny All/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});
