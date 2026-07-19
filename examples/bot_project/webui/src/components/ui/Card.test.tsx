import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Card } from "./Card";

describe("Card", () => {
  it("renders a div with canvas-elevated and hairline border", () => {
    render(<Card>hello</Card>);
    const el = screen.getByText("hello");
    expect(el.tagName).toBe("DIV");
    expect(el.className).toContain("bg-canvas-elevated");
    expect(el.className).toContain("border-hairline");
    expect(el.className).toContain("rounded-md");
    expect(el.className).toContain("p-4");
  });

  it("elevated adds the card shadow token", () => {
    render(<Card elevated>raised</Card>);
    const el = screen.getByText("raised");
    expect(el.className).toContain("shadow-card");
  });

  it("non-elevated has no shadow", () => {
    render(<Card>flat</Card>);
    const el = screen.getByText("flat");
    expect(el.className).not.toContain("shadow-card");
  });

  it("merges extra className", () => {
    render(<Card className="gap-2">x</Card>);
    const el = screen.getByText("x");
    expect(el.className).toContain("gap-2");
  });

  it("forwards `id` to the rendered element", () => {
    render(<Card id="main-card">x</Card>);
    const el = screen.getByText("x");
    expect(el.id).toBe("main-card");
  });
});
