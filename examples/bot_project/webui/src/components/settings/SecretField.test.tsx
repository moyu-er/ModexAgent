import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
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
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "x" } });
    expect(value).toEqual({ value: "x" });
    fireEvent.change(input, { target: { value: "" } });
    expect(value).toBeUndefined();
  });

  describe("show/hide toggle", () => {
    it("starts masked and reveals the value when Show is clicked", () => {
      render(
        <SecretField value={{ has_value: true, hint: "••••" }} onChange={() => {}} />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      const input = screen.getByRole("textbox") as HTMLInputElement;
      fireEvent.change(input, { target: { value: "secret-text" } });
      expect(input.type).toBe("password");

      fireEvent.click(screen.getByRole("button", { name: "Show value" }));
      expect(input.type).toBe("text");

      fireEvent.click(screen.getByRole("button", { name: "Hide value" }));
      expect(input.type).toBe("password");
    });
  });

  describe("copy-to-clipboard", () => {
    let writeText: ReturnType<typeof vi.fn>;
    beforeEach(() => {
      writeText = vi.fn().mockResolvedValue(undefined);
      vi.stubGlobal("navigator", { clipboard: { writeText } });
    });
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it("copies the hint when Copy is clicked in display state", async () => {
      render(
        <SecretField value={{ has_value: true, hint: "••••abcd" }} onChange={() => {}} />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Copy hint" }));
      await waitFor(() => expect(writeText).toHaveBeenCalledWith("••••abcd"));
    });

    it("copies the hint when Copy is clicked in editing state", async () => {
      render(
        <SecretField value={{ has_value: true, hint: "••••efgh" }} onChange={() => {}} />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      fireEvent.click(screen.getByRole("button", { name: "Copy hint" }));
      await waitFor(() => expect(writeText).toHaveBeenCalledWith("••••efgh"));
    });

    it("does not render a copy button when no value is set", () => {
      render(<SecretField value={{ has_value: false }} onChange={() => {}} />);
      expect(screen.queryByRole("button", { name: /Copy hint/ })).toBeNull();
    });
  });
});