import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ToolTraceCard } from "./ToolTraceCard";
import type { ToolTrace } from "../types/events";

const tool: ToolTrace = {
  tool: "read_file",
  args: { path: "/tmp/x" },
  result: "ok",
};

describe("ToolTraceCard (Teal & Ember §6)", () => {
  it("shares the trace-card language: eyebrow header + left severity bar", () => {
    const { container } = render(<ToolTraceCard tool={tool} />);
    const card = container.querySelector(".trace-card");
    expect(card).toBeTruthy();
    // Tool traces are unclassified → normal (mute) severity bar.
    expect((card as HTMLElement).style.getPropertyValue("--sev")).toBe(
      "var(--color-severity-normal)",
    );
    // Eyebrow header carries the tool name.
    expect(screen.getByText("read_file")).toBeTruthy();
  });

  it("pairs the done status with an icon, never color alone", () => {
    const { container } = render(<ToolTraceCard tool={tool} />);
    expect(screen.getByText("done")).toBeTruthy();
    // Status icon sits next to the label inside the header.
    const header = screen.getByRole("button", { name: /read_file/i });
    expect(header.querySelector("svg")).toBeTruthy();
    expect(container.querySelector(".trace-card")).toBeTruthy();
  });

  it("reveals args/result on expand", () => {
    render(<ToolTraceCard tool={tool} />);
    fireEvent.click(screen.getByRole("button", { name: /read_file/i }));
    expect(screen.getByText(/\/tmp\/x/)).toBeTruthy();
    expect(screen.getByText("ok")).toBeTruthy();
  });
});
