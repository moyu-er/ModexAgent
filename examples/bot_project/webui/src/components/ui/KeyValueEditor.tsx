// KeyValueEditor.tsx — Postman-style key/value rows for Record<string,string>.
//
// Public contract: controlled component. Parent owns `entries` and reacts to
// `onChange(next)`. Internally we keep `rows` state so an empty draft row
// (just added, not yet typed into) survives a round-trip through the parent —
// otherwise the parent would receive a record with the empty key stripped,
// re-render with that empty record, and our derived `rows` would have no
// draft to show. We detect our own commit echo so we only rebuild rows when
// the parent passes new authoritative data, not when it passes back the
// trimmed output we just emitted.

import { useCallback, useRef, useState, type FC } from "react";
import { Input } from "./Input";
import { IconButton } from "./IconButton";
import { Trash2 } from "lucide-react";
import { PlusIcon } from "./icons";
import { useT } from "../../i18n";

interface Row {
  key: string;
  value: string;
  id: number;
}

interface Props {
  label?: string;
  helper?: string;
  entries: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}

const buildRecord = (rows: Row[]): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const k = r.key.trim();
    if (k) out[k] = r.value;
  }
  return out;
};

const sameRecord = (
  a: Record<string, string>,
  b: Record<string, string>,
): boolean => {
  const aKeys = Object.keys(a);
  const bKeys = Object.keys(b);
  if (aKeys.length !== bKeys.length) return false;
  for (const k of aKeys) {
    if (a[k] !== b[k]) return false;
  }
  return true;
};

export const KeyValueEditor: FC<Props> = ({ label, helper, entries, onChange }) => {
  const t = useT();
  const idCounter = useRef<number>(0);
  const nextId = (): number => {
    idCounter.current += 1;
    return idCounter.current;
  };
  const entriesToRows = (e: Record<string, string>): Row[] =>
    Object.entries(e).map(([key, value]) => ({
      key,
      value,
      id: nextId(),
    }));
  const [rows, setRows] = useState<Row[]>(() => entriesToRows(entries));
  const lastEntriesRef = useRef(entries);

  if (lastEntriesRef.current !== entries) {
    const incoming = entries;
    lastEntriesRef.current = incoming;
    // If `incoming` is exactly what our current rows would commit to, the
    // parent is just echoing back our own trimmed output — keep the user's
    // draft rows intact. Otherwise the parent pushed new authoritative data
    // and we should rebuild from it.
    if (!sameRecord(buildRecord(rows), incoming)) {
      setRows(entriesToRows(incoming));
    }
  }

  const commit = useCallback(
    (next: Row[]) => {
      onChange(buildRecord(next));
    },
    [onChange],
  );

  const setRow = (id: number, patch: Partial<Row>): void => {
    const next = rows.map((r) => (r.id === id ? { ...r, ...patch } : r));
    setRows(next);
    commit(next);
  };

  const addRow = (): void => {
    const next = [...rows, { key: "", value: "", id: nextId() }];
    setRows(next);
    commit(next);
  };

  const removeRow = (id: number): void => {
    const next = rows.filter((r) => r.id !== id);
    setRows(next);
    commit(next);
  };

  const entryLabel = label || t("ui.keyValue.entry");

  return (
    <div className="space-y-2">
      {label ? (
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-body">{label}</span>
        </div>
      ) : null}
      <div className="space-y-1.5">
        {rows.map((r) => (
          <div key={r.id} className="flex items-center gap-2">
            <Input
              aria-label={t("ui.keyValue.entryKey", { label: entryLabel })}
              placeholder={t("ui.keyValue.keyPlaceholder")}
              value={r.key}
              onChange={(e) => setRow(r.id, { key: e.target.value })}
              className="flex-1"
            />
            <Input
              aria-label={t("ui.keyValue.entryValue", { label: entryLabel })}
              placeholder={t("ui.keyValue.valuePlaceholder")}
              value={r.value}
              onChange={(e) => setRow(r.id, { value: e.target.value })}
              className="flex-1"
            />
            <IconButton
              icon={<Trash2 size={16} />}
              label={t("ui.keyValue.removeEntry", { label: entryLabel.toLowerCase() })}
              variant="ghost"
              size="sm"
              onClick={() => removeRow(r.id)}
            />
          </div>
        ))}
        <button
          type="button"
          onClick={addRow}
          className="flex w-full items-center justify-center gap-1.5 rounded-md border border-dashed border-hairline py-1.5 text-xs text-body hover:border-ink hover:bg-hairline-soft hover:text-ink"
        >
          <PlusIcon /> {t("ui.keyValue.addEntry", { label: label || t("ui.keyValue.entry").toLowerCase() })}
        </button>
      </div>
      {helper ? <p className="text-xs text-mute">{helper}</p> : null}
    </div>
  );
};