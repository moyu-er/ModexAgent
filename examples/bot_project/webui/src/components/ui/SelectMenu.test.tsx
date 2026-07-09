import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SelectMenu } from "./SelectMenu";

const opts = [
  { value: "default", label: "Default" },
  { value: "coding", label: "Coding" },
  { value: "research", label: "Research" },
];

describe("SelectMenu", () => {
  it("renders the selected option's label on the trigger", () => {
    render(<SelectMenu options={opts} value="coding" onChange={vi.fn()} ariaLabel="pool" />);
    expect(screen.getByRole("button", { name: /pool/i }).textContent).toContain("Coding");
  });

  it("opens on trigger click and exposes all options", () => {
    render(<SelectMenu options={opts} value="default" onChange={vi.fn()} ariaLabel="pool" />);
    fireEvent.click(screen.getByRole("button", { name: /pool/i }));
    expect(screen.getAllByRole("option")).toHaveLength(3);
  });

  it("navigates with ArrowDown and commits on Enter (keyboard path)", () => {
    const onChange = vi.fn();
    render(<SelectMenu options={opts} value="default" onChange={onChange} ariaLabel="pool" />);
    const trigger = screen.getByRole("button", { name: /pool/i });
    // Open via keyboard — focus must move to the listbox so the list handler runs.
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox");
    // Default is index 0; one ArrowDown highlights index 1 ("Coding").
    fireEvent.keyDown(listbox, { key: "ArrowDown" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("coding");
  });

  it("type-ahead jumps to the next option whose label starts with the key", () => {
    const onChange = vi.fn();
    render(<SelectMenu options={opts} value="default" onChange={onChange} ariaLabel="pool" />);
    fireEvent.keyDown(screen.getByRole("button", { name: /pool/i }), { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox");
    fireEvent.keyDown(listbox, { key: "r" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("research");
  });

  it("closes on Escape", () => {
    render(<SelectMenu options={opts} value="default" onChange={vi.fn()} ariaLabel="pool" />);
    fireEvent.click(screen.getByRole("button", { name: /pool/i }));
    const listbox = screen.getByRole("listbox");
    fireEvent.keyDown(listbox, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("closes on outside pointer down", () => {
    render(
      <div>
        <SelectMenu options={opts} value="default" onChange={vi.fn()} ariaLabel="pool" />
        <button type="button">outside</button>
      </div>,
    );
    fireEvent.click(screen.getByRole("button", { name: /pool/i }));
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.pointerDown(screen.getByText("outside"));
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
