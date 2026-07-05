import { useState } from "react";
import type { SecretMaskValue, SecretWrite } from "../../types/config";

interface Props {
  value: SecretMaskValue;
  onChange: (next: SecretWrite | undefined) => void;
}

/**
 * Secret editor. The current value is NEVER shown — only the hint. Editing
 * states: untouched → keep; typed non-empty → {value}; empty input → keep;
 * Clear button → {set: false}.
 */
export function SecretField({ value, onChange }: Props) {
  const [editing, setEditing] = useState<boolean>(!value.has_value);
  const [draft, setDraft] = useState<string>("");
  const [cleared, setCleared] = useState<boolean>(false);

  if (cleared) {
    return (
      <div className="flex items-center gap-2">
        <span className="italic text-text-secondary">cleared (on save)</span>
        <button
          className="text-xs text-accent hover:underline"
          onClick={() => {
            setCleared(false);
            setEditing(!value.has_value);
            setDraft("");
            onChange(undefined);
          }}
        >
          Undo
        </button>
      </div>
    );
  }

  if (!editing && value.has_value) {
    return (
      <div className="flex items-center gap-2">
        <span className="font-mono text-text-secondary">{value.hint ?? "••••••••"}</span>
        <button
          className="rounded border border-divider px-2 py-0.5 text-xs hover:bg-surface-hover"
          onClick={() => setEditing(true)}
        >
          Edit
        </button>
        <button
          className="text-xs text-danger hover:underline"
          onClick={() => {
            setCleared(true);
            onChange({ set: false });
          }}
        >
          Clear
        </button>
      </div>
    );
  }

  return (
    <input
      className="w-full rounded border border-divider bg-surface px-2 py-1 font-mono"
      type="password"
      role="textbox"
      aria-label="secret value"
      placeholder={value.has_value ? "leave empty to keep current" : "enter value"}
      value={draft}
      onChange={(e) => {
        const v = e.target.value;
        setDraft(v);
        onChange(v === "" ? undefined : { value: v });
      }}
    />
  );
}
