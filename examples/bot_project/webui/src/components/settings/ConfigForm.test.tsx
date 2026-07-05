import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ConfigForm } from "./ConfigForm";
import type { FieldDescriptor } from "../../types/config";

const fields: FieldDescriptor[] = [
  { name: "app_id", label: "App ID", type: "string", required: false },
  { name: "sandbox", label: "Sandbox", type: "boolean", required: false },
  { name: "allow_from", label: "Allow from", type: "list", required: false },
  { name: "secret", label: "Secret", type: "secret", required: false },
];

const values = {
  app_id: "A",
  sandbox: false,
  allow_from: ["*"],
  secret: { has_value: true, hint: "x" },
};

describe("ConfigForm", () => {
  it("renders string/boolean/list/secret field labels", () => {
    render(<ConfigForm fields={fields} values={values} onChange={() => {}} />);
    expect(screen.getByText("App ID")).toBeTruthy();
    expect(screen.getByText("Sandbox")).toBeTruthy();
    expect(screen.getByText("Allow from")).toBeTruthy();
    expect(screen.getByText("Secret")).toBeTruthy();
  });

  it("editing a string fires onChange with the new value", () => {
    const onChange = vi.fn();
    render(<ConfigForm fields={fields} values={values} onChange={onChange} />);
    const input = screen.getByDisplayValue("A") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "B" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ app_id: "B" }));
  });

  it("toggling a boolean fires onChange", () => {
    const onChange = vi.fn();
    render(<ConfigForm fields={fields} values={values} onChange={onChange} />);
    const cb = screen.getByRole("checkbox") as HTMLInputElement;
    fireEvent.click(cb);
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ sandbox: true }));
  });

  it("editing a list field fires onChange with a split array", () => {
    const onChange = vi.fn();
    render(<ConfigForm fields={fields} values={values} onChange={onChange} />);
    const input = screen.getByDisplayValue("*") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "a, b ,c" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ allow_from: ["a", "b", "c"] }));
  });
});
