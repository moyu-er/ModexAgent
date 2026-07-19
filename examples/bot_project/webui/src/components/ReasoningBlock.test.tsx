import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ReasoningBlock } from "./ReasoningBlock";

describe("ReasoningBlock (Teal & Ember §6)", () => {
  it("renders a mono eyebrow header and keeps the body collapsed by default", () => {
    render(<ReasoningBlock reasoning="let me think" />);
    const toggle = screen.getByRole("button", { name: /reasoning/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    // Collapsed: body hidden.
    expect(screen.queryByText("let me think")).toBeNull();
  });

  it("expands to show the dimmed reasoning body with a left border", () => {
    const { container } = render(<ReasoningBlock reasoning="let me think" />);
    fireEvent.click(screen.getByRole("button", { name: /reasoning/i }));
    expect(screen.getByText("let me think")).toBeTruthy();
    // 2px brand-alpha left border lives on the shared reasoning-body class.
    expect(container.querySelector(".reasoning-body")).toBeTruthy();
  });

  it("uses a visible brand focus ring (no alpha-modifier tokens)", () => {
    render(<ReasoningBlock reasoning="x" />);
    const toggle = screen.getByRole("button", { name: /reasoning/i });
    expect(toggle.className).toContain("focus-visible:ring-brand");
  });
});
