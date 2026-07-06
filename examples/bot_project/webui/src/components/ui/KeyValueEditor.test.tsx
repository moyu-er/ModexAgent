import { describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { KeyValueEditor } from "./KeyValueEditor";

// KeyValueEditor is a fully controlled component: rows are derived from the
// `entries` prop. To exercise add/edit in tests we mirror the real interaction
// shape by wrapping the editor in a small useState container — the same
// pattern used in ModelEditor.test.tsx.

function Controlled({
  initial,
  onChange,
  label,
}: {
  initial: Record<string, string>;
  onChange?: (next: Record<string, string>) => void;
  label?: string;
}) {
  const [entries, setEntries] = useState<Record<string, string>>(initial);
  return (
    <KeyValueEditor
      label={label}
      entries={entries}
      onChange={(next) => {
        setEntries(next);
        onChange?.(next);
      }}
    />
  );
}

describe("KeyValueEditor", () => {
  it("adds a row and emits a trimmed record", () => {
    const onChange = vi.fn();
    render(<Controlled initial={{}} onChange={onChange} label="Headers" />);

    fireEvent.click(screen.getByRole("button", { name: /add headers/i }));
    const keyInputs = screen.getAllByPlaceholderText("KEY");
    const valueInputs = screen.getAllByPlaceholderText("value");
    fireEvent.change(keyInputs[0]!, { target: { value: "Authorization" } });
    fireEvent.change(valueInputs[0]!, { target: { value: "Bearer token" } });

    expect(onChange).toHaveBeenLastCalledWith({ Authorization: "Bearer token" });
  });

  it("removes empty keys from emitted record", () => {
    const onChange = vi.fn();
    render(<Controlled initial={{ a: "1" }} onChange={onChange} />);

    const keyInputs = screen.getAllByPlaceholderText("KEY");
    fireEvent.change(keyInputs[0]!, { target: { value: "" } });

    expect(onChange).toHaveBeenLastCalledWith({});
  });
});