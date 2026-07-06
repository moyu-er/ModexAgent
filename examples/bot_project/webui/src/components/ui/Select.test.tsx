import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Select } from "./Select";

const options = [
  { value: "a", label: "Alpha" },
  { value: "b", label: "Beta" },
];

describe("Select", () => {
  it("renders the label and all options", () => {
    render(<Select label="Pool" options={options} />);
    expect(screen.getByText("Pool")).toBeTruthy();
    const sel = screen.getByLabelText("Pool");
    expect(sel.tagName).toBe("SELECT");
    expect(sel.querySelectorAll("option").length).toBe(2);
  });

  it("renders the chevron icon", () => {
    render(<Select label="x" options={options} />);
    expect(document.querySelector("svg")).toBeTruthy();
  });

  it("error replaces helper and uses error border", () => {
    render(<Select label="x" options={options} helper="ok" error="bad" />);
    const sel = screen.getByLabelText("x");
    expect(sel.className).toContain("border-error");
    expect(screen.getByRole("alert").textContent).toBe("bad");
  });

  it("forwards ref to the underlying select", () => {
    let captured: HTMLSelectElement | null = null;
    render(
      <Select
        label="x"
        options={options}
        ref={(node) => {
          captured = node;
        }}
      />,
    );
    expect(captured).toBeInstanceOf(HTMLSelectElement);
  });

  it("fires onChange when changed", () => {
    const onChange = vi.fn();
    render(<Select label="x" options={options} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("x"), { target: { value: "b" } });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("required label shows asterisk", () => {
    render(<Select label="Pool" options={options} required />);
    const label = screen.getByText("Pool");
    expect(label.textContent).toContain("*");
  });
});