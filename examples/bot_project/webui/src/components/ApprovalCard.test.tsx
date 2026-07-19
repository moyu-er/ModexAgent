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
  it("renders tool name + tier badge + preview of args", () => {
    render(<ApprovalCard view={view} onApprove={vi.fn()} onDeny={vi.fn()} />);
    expect(screen.getByText("write_file")).toBeTruthy();
    expect(screen.getByText("dangerous")).toBeTruthy();
    // Short args fit the preview, so they are visible without expanding.
    expect(screen.getByText(/tmp\/x/)).toBeTruthy();
    // No chevron when everything already fits in the preview.
    expect(screen.queryByRole("button", { name: /expand arguments/i })).toBeNull();
  });

  it("previews clamped args and toggles the clamp on expand", () => {
    const { container } = render(
      <ApprovalCard view={longView} onApprove={vi.fn()} onDeny={vi.fn()} />,
    );
    const pre = container.querySelector("pre") as HTMLPreElement;
    const toggle = screen.getByRole("button", { name: /expand arguments/i });

    // Collapsed: the full JSON is in the DOM but visually clamped to 3 lines.
    expect(pre.textContent).toMatch(/y{200}/);
    expect(pre.className).toContain("line-clamp-3");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(toggle);
    // Expanded: the clamp is removed so the full content is shown.
    expect(pre.className).not.toContain("line-clamp-3");
    expect(
      screen
        .getByRole("button", { name: /collapse arguments/i })
        .getAttribute("aria-expanded"),
    ).toBe("true");

    // Toggling again re-applies the clamp.
    fireEvent.click(screen.getByRole("button", { name: /collapse arguments/i }));
    expect(pre.className).toContain("line-clamp-3");
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

  it("carries severity in the left bar + status icon + text label, never color alone", () => {
    const { container } = render(
      <ApprovalCard view={view} onApprove={vi.fn()} onDeny={vi.fn()} />,
    );
    // Shared trace-card shell with a 3px severity left bar via --sev.
    const card = container.querySelector(".trace-card") as HTMLElement;
    expect(card).toBeTruthy();
    expect(card.style.getPropertyValue("--sev")).toBe(
      "var(--color-severity-dangerous)",
    );
    // Status icon next to the tier text label.
    const icon = container.querySelector("[data-severity-icon]") as HTMLElement;
    expect(icon).toBeTruthy();
    expect(icon.className).toContain("text-severity-dangerous");
    // Text label means severity is never conveyed by color alone.
    expect(screen.getByText("dangerous")).toBeTruthy();
  });

  it("falls back to normal severity for unknown tiers", () => {
    const { container } = render(
      <ApprovalCard
        view={{ ...view, tier: "safe" }}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    const card = container.querySelector(".trace-card") as HTMLElement;
    expect(card.style.getPropertyValue("--sev")).toBe(
      "var(--color-severity-normal)",
    );
    expect(
      (container.querySelector("[data-severity-icon]") as HTMLElement).className,
    ).toContain("text-severity-normal");
  });

  it("renders the eyebrow header", () => {
    render(<ApprovalCard view={view} onApprove={vi.fn()} onDeny={vi.fn()} />);
    expect(screen.getByText("Approval").className).toContain("eyebrow");
  });
});
