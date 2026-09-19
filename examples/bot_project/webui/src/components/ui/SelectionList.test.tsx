// SelectionList.test.tsx — the shared bounded selection list: one-column
// layout with a fixed scroll ceiling, checkbox-left rows, optional filter,
// disabled rows, and empty fallback.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SelectionList } from "./SelectionList";

const ITEMS = [
  { id: "bash", label: "bash" },
  { id: "todo", label: "todo", description: "task tracking" },
];

function renderList(checked: ReadonlySet<string> = new Set(["bash"])) {
  const onToggle = vi.fn();
  render(
    <SelectionList
      items={ITEMS}
      checked={checked}
      onToggle={onToggle}
      ariaLabel="Capabilities"
    />,
  );
  return { onToggle };
}

describe("SelectionList", () => {
  it("renders one checkbox per item with the label accessible", () => {
    renderList();
    expect(screen.getByRole("checkbox", { name: "bash" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "todo" })).toBeTruthy();
    expect(
      (screen.getByRole("checkbox", { name: "bash" }) as HTMLInputElement)
        .checked,
    ).toBe(true);
    expect(
      (screen.getByRole("checkbox", { name: "todo" }) as HTMLInputElement)
        .checked,
    ).toBe(false);
  });

  it("toggles emit the item id with the next state", () => {
    const { onToggle } = renderList();
    fireEvent.click(screen.getByRole("checkbox", { name: "todo" }));
    expect(onToggle).toHaveBeenCalledWith("todo", true);
    fireEvent.click(screen.getByRole("checkbox", { name: "bash" }));
    expect(onToggle).toHaveBeenCalledWith("bash", false);
  });

  it("uses a single vertical column with a bounded scroll ceiling", () => {
    renderList();
    const list = screen.getByTestId("selection-list");
    expect(list.className).toContain("max-h-80");
    expect(list.className).toContain("overflow-y-auto");
    expect(list.className).not.toContain("grid-cols");
  });

  it("filters by label when a search box is provided", () => {
    render(
      <SelectionList
        items={ITEMS}
        checked={new Set()}
        onToggle={vi.fn()}
        ariaLabel="Capabilities"
        searchLabel="Filter…"
      />,
    );
    fireEvent.change(screen.getByLabelText("Filter…"), {
      target: { value: "tod",
      },
    });
    expect(screen.queryByRole("checkbox", { name: "bash" })).toBeNull();
    expect(screen.getByRole("checkbox", { name: "todo" })).toBeTruthy();
  });

  it("renders disabled rows as real disabled checkboxes (checked or not)", () => {
    render(
      <SelectionList
        items={[
          { id: "a", label: "installed", disabled: true, note: "local" },
          { id: "b", label: "pending", disabled: true },
          { id: "c", label: "free" },
        ]}
        checked={new Set(["a"])}
        onToggle={vi.fn()}
        ariaLabel="Skills"
      />,
    );
    const installed = screen.getByRole("checkbox", {
      name: "installed",
    }) as HTMLInputElement;
    const pending = screen.getByRole("checkbox", {
      name: "pending",
    }) as HTMLInputElement;
    expect(installed.disabled).toBe(true);
    expect(installed.checked).toBe(true);
    expect(pending.disabled).toBe(true);
    expect(pending.checked).toBe(false);
    expect(screen.getByText("local")).toBeTruthy();
    // No fake empty-square rendering — every row is a real input.
    expect(screen.getAllByRole("checkbox")).toHaveLength(3);
  });

  it("locks rows via busyIds and the whole list via disabled", () => {
    const onToggle = vi.fn();
    render(
      <SelectionList
        items={ITEMS}
        checked={new Set()}
        onToggle={onToggle}
        ariaLabel="Capabilities"
        disabled
        busyIds={new Set(["todo"])}
      />,
    );
    for (const box of screen.getAllByRole("checkbox") as HTMLInputElement[]) {
      expect(box.disabled).toBe(true);
    }
    fireEvent.click(screen.getByRole("checkbox", { name: "bash" }));
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("falls back to the empty text when there are no items", () => {
    render(
      <SelectionList
        items={[]}
        checked={new Set()}
        onToggle={vi.fn()}
        ariaLabel="Skills"
        emptyText="No skills available."
      />,
    );
    expect(screen.getByText("No skills available.")).toBeTruthy();
    expect(screen.queryByTestId("selection-list")).toBeNull();
  });
});
