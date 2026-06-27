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

describe("ApprovalCard", () => {
  it("renders tool name + args and calls onApprove with tool_call_id", () => {
    const onApprove = vi.fn();
    render(<ApprovalCard view={view} onApprove={onApprove} onDeny={vi.fn()} />);
    expect(screen.getByText("write_file")).toBeTruthy();
    // mono args block shows the serialized arguments
    expect(screen.getByText(/tmp\/x/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onApprove).toHaveBeenCalledWith("c1");
  });

  it("calls onDeny with tool_call_id", () => {
    const onDeny = vi.fn();
    render(<ApprovalCard view={view} onApprove={vi.fn()} onDeny={onDeny} />);
    fireEvent.click(screen.getByRole("button", { name: /deny/i }));
    expect(onDeny).toHaveBeenCalledWith("c1");
  });

  it("disables buttons while submitting", () => {
    render(
      <ApprovalCard
        view={view}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
        submitting={true}
      />,
    );
    expect(
      (screen.getByRole("button", { name: /approve/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: /deny/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});
