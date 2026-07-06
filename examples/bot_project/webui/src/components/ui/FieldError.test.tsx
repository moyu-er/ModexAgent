import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { FieldError } from "./FieldError";

describe("FieldError", () => {
  it("renders with role=alert and error text color", () => {
    render(<FieldError>Required</FieldError>);
    const el = screen.getByRole("alert");
    expect(el.textContent).toBe("Required");
    expect(el.className).toContain("text-error");
    expect(el.className).toContain("text-xs");
  });

  it("forwards id and className", () => {
    render(
      <FieldError id="err-1" className="custom">
        boom
      </FieldError>,
    );
    const el = screen.getByRole("alert");
    expect(el.id).toBe("err-1");
    expect(el.className).toContain("custom");
  });
});