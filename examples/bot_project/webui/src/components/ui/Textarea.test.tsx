import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Textarea } from "./Textarea";

describe("Textarea", () => {
  it("renders a textarea with label/helper", () => {
    render(<Textarea label="Notes" helper="markdown supported" />);
    const ta = screen.getByLabelText("Notes");
    expect(ta.tagName).toBe("TEXTAREA");
    expect(screen.getByText("markdown supported")).toBeTruthy();
  });

  it("defaults to mono font class and 80px min-height", () => {
    render(<Textarea label="x" />);
    const ta = screen.getByLabelText("x");
    expect(ta.className).toContain("font-mono");
    expect(ta.className).toContain("min-h-[80px]");
  });

  it("mono=false drops font-mono", () => {
    render(<Textarea label="x" mono={false} />);
    const ta = screen.getByLabelText("x");
    expect(ta.className).not.toContain("font-mono");
  });

  it("error replaces helper and adds error border", () => {
    render(<Textarea label="x" helper="ok" error="nope" />);
    const ta = screen.getByLabelText("x");
    expect(ta.className).toContain("border-error");
    expect(screen.getByRole("alert").textContent).toBe("nope");
    expect(screen.queryByText("ok")).toBeNull();
  });

  it("forwards ref to the underlying textarea", () => {
    let captured: HTMLTextAreaElement | null = null;
    render(
      <Textarea
        label="x"
        ref={(node) => {
          captured = node;
        }}
      />,
    );
    expect(captured).toBeInstanceOf(HTMLTextAreaElement);
  });

  it("fires onChange when typed into", () => {
    const onChange = vi.fn();
    render(<Textarea label="x" onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("x"), { target: { value: "hi" } });
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});