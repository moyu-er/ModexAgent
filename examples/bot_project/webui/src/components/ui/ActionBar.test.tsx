import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ActionBar } from "./ActionBar";

describe("ActionBar", () => {
  it("renders children and applies sticky positioning", () => {
    render(
      <ActionBar>
        <button>Cancel</button>
        <button>Save</button>
      </ActionBar>,
    );
    const el = screen.getByRole("group");
    expect(el.className).toContain("sticky");
    expect(el.className).toContain("bottom-0");
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  it("merges extra className", () => {
    render(<ActionBar className="extra">content</ActionBar>);
    const el = screen.getByRole("group");
    expect(el.className).toContain("extra");
  });
});
