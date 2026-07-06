// SecretField.tsx — secret editor.
//
// The current value is NEVER shown — only the hint. While editing, the
// password input is masked by default; a reveal toggle flips it to plain
// text. The Clear button marks the value for clearing on save; an Undo
// button reverts that. The Copy button writes the hint (or a stand-in for
// the masked value) to the clipboard — the actual secret is never read
// back from the server in this MVP.

import { useState } from "react";
import type { SecretMaskValue, SecretWrite } from "../../types/config";
import { IconButton } from "../ui/IconButton";
import { EyeIcon, EyeOffIcon, CopyIcon, CheckIcon, EditIcon } from "../ui/icons";

interface Props {
  value: SecretMaskValue;
  onChange: (next: SecretWrite | undefined) => void;
}

export function SecretField({ value, onChange }: Props) {
  const [editing, setEditing] = useState<boolean>(!value.has_value);
  const [draft, setDraft] = useState<string>("");
  const [cleared, setCleared] = useState<boolean>(false);
  const [revealed, setRevealed] = useState<boolean>(false);
  const [copied, setCopied] = useState<boolean>(false);

  const handleCopy = async () => {
    const text = value.hint ?? "••••••••";
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      }
    } catch {
      // best-effort: clipboard may be unavailable in non-secure contexts
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };

  if (cleared) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-xs italic text-mute">cleared (on save)</span>
        <button
          type="button"
          className="text-xs text-link hover:underline"
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
        <span className="font-mono text-sm text-ink">
          {value.hint ?? "••••••••"}
        </span>
        <IconButton
          icon={<EditIcon />}
          label="Edit"
          size="sm"
          variant="secondary"
          onClick={() => setEditing(true)}
        />
        <IconButton
          icon={copied ? <CheckIcon /> : <CopyIcon />}
          label={copied ? "Copied" : "Copy hint"}
          size="sm"
          variant="ghost"
          onClick={handleCopy}
        />
        <button
          type="button"
          className="text-xs text-error hover:underline"
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
    <div className="flex items-center gap-2">
      <input
        className="w-full rounded-xs border border-hairline bg-canvas-elevated px-2.5 py-1.5 font-mono text-sm text-ink placeholder:text-faint focus:border-link focus:outline-none focus:ring-1 focus:ring-link"
        type={revealed ? "text" : "password"}
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
      <IconButton
        icon={revealed ? <EyeOffIcon /> : <EyeIcon />}
        label={revealed ? "Hide value" : "Show value"}
        size="sm"
        variant="ghost"
        onClick={() => setRevealed((r) => !r)}
      />
      {value.has_value ? (
        <IconButton
          icon={copied ? <CheckIcon /> : <CopyIcon />}
          label={copied ? "Copied" : "Copy hint"}
          size="sm"
          variant="ghost"
          onClick={handleCopy}
        />
      ) : null}
    </div>
  );
}