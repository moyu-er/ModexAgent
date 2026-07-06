import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Input } from "./Input";

describe("Input", () => {
  it("renders the label, helper, and underlying input", () => {
    render(<Input label="Email" helper="we'll never share it" />);
    expect(screen.getByText("Email")).toBeTruthy();
    expect(screen.getByText("we'll never share it")).toBeTruthy();
    const input = screen.getByLabelText("Email");
    expect(input.tagName).toBe("INPUT");
  });

  it("spreads extra props (placeholder, type, value) onto the input", () => {
    render(<Input label="x" placeholder="type here" type="email" />);
    const input = screen.getByLabelText("x");
    expect(input.getAttribute("placeholder")).toBe("type here");
    expect(input.getAttribute("type")).toBe("email");
  });

  it("error swaps border to error and replaces helper", () => {
    render(<Input label="x" helper="ok" error="too short" />);
    const input = screen.getByLabelText("x");
    expect(input.className).toContain("border-error");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toBe("too short");
    expect(screen.queryByText("ok")).toBeNull();
  });

  it("helper has aria-describedby wiring when no error", () => {
    render(<Input label="x" helper="help" />);
    const input = screen.getByLabelText("x");
    const describedBy = input.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy as string)?.textContent).toBe("help");
  });

  it("required label shows red asterisk", () => {
    render(<Input label="Pool name" required />);
    const label = screen.getByText("Pool name");
    expect(label.textContent).toContain("*");
  });

  it("forwards ref to the underlying input", () => {
    let captured: HTMLInputElement | null = null;
    render(
      <Input
        label="x"
        ref={(node) => {
          captured = node;
        }}
      />,
    );
    expect(captured).toBeInstanceOf(HTMLInputElement);
  });

  it("fires onChange when typed into", () => {
    const onChange = vi.fn();
    render(<Input label="x" onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("x"), { target: { value: "abc" } });
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});