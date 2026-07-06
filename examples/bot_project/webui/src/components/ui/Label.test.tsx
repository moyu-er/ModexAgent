import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Label } from "./Label";

describe("Label", () => {
  it("renders the children and wires htmlFor", () => {
    render(
      <Label htmlFor="my-input">
        Email
      </Label>,
    );
    const el = screen.getByText("Email");
    expect(el.tagName).toBe("LABEL");
    expect(el.getAttribute("for")).toBe("my-input");
  });

  it("shows a red asterisk when required", () => {
    render(<Label required>Pool name</Label>);
    const el = screen.getByText("Pool name");
    expect(el.textContent).toContain("*");
  });

  it("does not show asterisk when not required", () => {
    render(<Label>Pool name</Label>);
    const el = screen.getByText("Pool name");
    expect(el.textContent?.trim()).toBe("Pool name");
  });

  it("merges extra className", () => {
    render(<Label className="mt-4">X</Label>);
    const el = screen.getByText("X");
    expect(el.className).toContain("mt-4");
    expect(el.className).toContain("text-body");
  });
});