import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ActionBar } from "./ActionBar";

describe("ActionBar", () => {
  it("renders children and applies the action-bar chrome class", () => {
    render(
      <ActionBar>
        <button>Cancel</button>
        <button>Save</button>
      </ActionBar>,
    );
    const el = screen.getByRole("group");
    // Sticky + blur live in the .action-bar CSS class now (§8), not in Tailwind
    // utilities — assert the class is present rather than the resolved CSS.
    expect(el.className).toContain("action-bar");
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  it("merges extra className", () => {
    render(<ActionBar className="extra">content</ActionBar>);
    const el = screen.getByRole("group");
    expect(el.className).toContain("extra");
  });

  it("does not render an unsaved-changes dot when dirty is false", () => {
    render(
      <ActionBar>
        <button>Save</button>
      </ActionBar>,
    );
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("renders the ember unsaved-changes dot when dirty is true", () => {
    render(
      <ActionBar dirty>
        <button>Save</button>
      </ActionBar>,
    );
    const dot = screen.getByRole("status");
    expect(dot.className).toContain("unsaved-dot");
    // The dot is labeled for assistive tech via aria-label.
    expect(dot.getAttribute("aria-label")).toBe("Unsaved changes");
  });
});
