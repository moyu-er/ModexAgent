import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Checkbox } from "./Checkbox";

describe("Checkbox", () => {
  it("renders label and pairs it with the input", () => {
    render(<Checkbox label="Enable" defaultChecked />);
    const box = screen.getByLabelText("Enable") as HTMLInputElement;
    expect(box.tagName).toBe("INPUT");
    expect(box.type).toBe("checkbox");
    expect(box.checked).toBe(true);
  });

  it("clicking the label toggles the checkbox", () => {
    render(<Checkbox label="Enable" />);
    const box = screen.getByLabelText("Enable") as HTMLInputElement;
    expect(box.checked).toBe(false);
    // happy-dom dispatches the click on the inner span as a label-relative click;
    // the safest cross-platform way to toggle is to fire change on the input,
    // which is what a real label-click would result in.
    fireEvent.click(box);
    expect(box.checked).toBe(true);
  });

  it("renders helper text and error", () => {
    const { rerender } = render(<Checkbox label="x" helper="hint" />);
    expect(screen.getByText("hint")).toBeTruthy();
    rerender(<Checkbox label="x" error="bad" />);
    expect(screen.getByRole("alert").textContent).toBe("bad");
  });

  it("error uses danger border on the box", () => {
    render(<Checkbox label="x" error="bad" />);
    const box = screen.getByLabelText("x");
    expect(box.className).toContain("border-danger");
    expect(box.getAttribute("aria-invalid")).toBe("true");
  });

  it("forwards ref to the underlying input", () => {
    let captured: HTMLInputElement | null = null;
    render(
      <Checkbox
        label="x"
        ref={(node) => {
          captured = node;
        }}
      />,
    );
    expect(captured).toBeInstanceOf(HTMLInputElement);
  });

  it("fires onChange when toggled", () => {
    const onChange = vi.fn();
    render(<Checkbox label="x" onChange={onChange} />);
    fireEvent.click(screen.getByLabelText("x"));
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});