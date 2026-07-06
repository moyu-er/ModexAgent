import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { HelperText } from "./HelperText";

describe("HelperText", () => {
  it("renders children inside a paragraph with secondary text", () => {
    render(<HelperText>Pick a name</HelperText>);
    const el = screen.getByText("Pick a name");
    expect(el.tagName).toBe("P");
    expect(el.className).toContain("text-mute");
    expect(el.className).toContain("text-xs");
    expect(el.className).toContain("mt-1");
  });

  it("forwards id and className", () => {
    render(
      <HelperText id="helper-1" className="custom">
        hint
      </HelperText>,
    );
    const el = screen.getByText("hint");
    expect(el.id).toBe("helper-1");
    expect(el.className).toContain("custom");
  });
});