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

  it("renders the preparing label with the received char count while args stream", () => {
    const preparing: ToolTrace = {
      tool: "write_file",
      args: {},
      call_id: "call_1",
      preparing: { chars: 1024, preview: '{"path": "a' },
    };
    render(<ToolTraceCard tool={preparing} />);
    expect(screen.getByText("write_file")).toBeTruthy();
    expect(screen.getByText("Preparing")).toBeTruthy();
    expect(screen.getByText("1024 chars received")).toBeTruthy();
    // No completion signal in the preparing state.
    expect(screen.queryByText("done")).toBeNull();
  });

  it("shows the preparing label alone for the identity announcement (chars=0)", () => {
    const announced: ToolTrace = {
      tool: "write_file",
      args: {},
      call_id: "call_1",
      preparing: { chars: 0, preview: "" },
    };
    render(<ToolTraceCard tool={announced} />);
    expect(screen.getByText("Preparing")).toBeTruthy();
    // The counter starts with the first real fragment — no "0 chars" flash.
    expect(screen.queryByText("0 chars received")).toBeNull();
  });

  it("reveals the streamed-args preview on expand while preparing", () => {
    const preparing: ToolTrace = {
      tool: "write_file",
      args: {},
      call_id: "call_1",
      preparing: { chars: 1024, preview: '{"path": "a' },
    };
    render(<ToolTraceCard tool={preparing} />);
    fireEvent.click(screen.getByRole("button", { name: /write_file/i }));
    expect(screen.getByText("Streamed args")).toBeTruthy();
    expect(screen.getByText(/\{"path": "a/)).toBeTruthy();
  });

  it("renders the done label as before once a result is present", () => {
    render(<ToolTraceCard tool={tool} />);
    expect(screen.getByText("done")).toBeTruthy();
    expect(screen.queryByText("Preparing")).toBeNull();
  });
});
