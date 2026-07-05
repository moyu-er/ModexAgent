import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SecretField } from "./SecretField";

describe("SecretField", () => {
  it("shows hint when set and does not emit while untouched", () => {
    let value: unknown = "UNSET";
    render(
      <SecretField
        value={{ has_value: true, hint: "••••12ab" }}
        onChange={(v) => { value = v; }}
      />,
    );
    expect(screen.getByText(/12ab/)).toBeTruthy();
    expect(value).toBe("UNSET"); // untouched → no onChange fired
  });

  it("Edit + typing a value emits {value}", () => {
    let value: unknown = undefined;
    render(
      <SecretField
        value={{ has_value: true, hint: "••••" }}
        onChange={(v) => { value = v; }}
      />,
    );
    fireEvent.click(screen.getByText("Edit"));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "newkey" } });
    expect(value).toEqual({ value: "newkey" });
  });

  it("Clear emits {set: false}", () => {
    let value: unknown = undefined;
    render(
      <SecretField
        value={{ has_value: true, hint: "••••" }}
        onChange={(v) => { value = v; }}
      />,
    );
    fireEvent.click(screen.getByText("Clear"));
    expect(value).toEqual({ set: false });
  });

  it("not set + typing emits {value}", () => {
    let value: unknown = undefined;
    render(<SecretField value={{ has_value: false }} onChange={(v) => { value = v; }} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "first" } });
    expect(value).toEqual({ value: "first" });
  });

  it("empty input after typing emits undefined (keep current)", () => {
    let value: unknown = "UNSET";
    render(<SecretField value={{ has_value: true, hint: "••••" }} onChange={(v) => { value = v; }} />);
    fireEvent.click(screen.getByText("Edit"));
    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "x" } });
    expect(value).toEqual({ value: "x" });
    fireEvent.change(input, { target: { value: "" } });
    expect(value).toBeUndefined();
  });
});
