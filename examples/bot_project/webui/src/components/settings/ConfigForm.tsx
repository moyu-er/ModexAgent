import type { FieldDescriptor } from "../../types/config";
import type { SecretMaskValue, SecretWrite } from "../../types/config";
import { SecretField } from "./SecretField";

interface Props {
  fields: FieldDescriptor[];
  values: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}

export function ConfigForm({ fields, values, onChange }: Props) {
  const update = (name: string, next: unknown) => {
    onChange({ ...values, [name]: next });
  };

  return (
    <div className="space-y-3">
      {fields.map((field) => {
        const v = values[field.name];
        if (field.type === "secret") {
          const secretValue: SecretMaskValue =
            (v as SecretMaskValue | undefined) ?? { has_value: false };
          return (
            <div key={field.name}>
              <label className="mb-1 block text-xs font-medium text-text-secondary">
                {field.label}
              </label>
              <SecretField
                value={secretValue}
                onChange={(next: SecretWrite | undefined) => update(field.name, next ?? v)}
              />
            </div>
          );
        }
        if (field.type === "boolean") {
          return (
            <div key={field.name} className="flex items-center gap-2">
              <input
                id={`fld-${field.name}`}
                type="checkbox"
                checked={Boolean(v)}
                onChange={(e) => update(field.name, e.target.checked)}
              />
              <label htmlFor={`fld-${field.name}`} className="text-sm text-text">
                {field.label}
              </label>
            </div>
          );
        }
        if (field.type === "list") {
          const listValue = Array.isArray(v) ? (v as unknown[]).join(", ") : "";
          return (
            <div key={field.name}>
              <label className="mb-1 block text-xs font-medium text-text-secondary">
                {field.label}
              </label>
              <input
                className="w-full rounded border border-divider bg-surface px-2 py-1"
                value={listValue}
                onChange={(e) =>
                  update(
                    field.name,
                    e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  )
                }
              />
            </div>
          );
        }
        // string + object (object rendered as a plain string best-effort in MVP)
        return (
          <div key={field.name}>
            <label className="mb-1 block text-xs font-medium text-text-secondary">
              {field.label}
            </label>
            <input
              className="w-full rounded border border-divider bg-surface px-2 py-1"
              value={(v as string | undefined) ?? ""}
              onChange={(e) => update(field.name, e.target.value)}
            />
          </div>
        );
      })}
    </div>
  );
}
