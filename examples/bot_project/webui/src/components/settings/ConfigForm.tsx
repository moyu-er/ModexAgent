// ConfigForm.tsx — render a list of FieldDescriptor rows using UI primitives.
//
// Each field becomes a stacked control: <Label> / primitive / <HelperText or
// FieldError>. Boolean uses Checkbox (label-as-text), string uses Input,
// list and object use Textarea with a per-type helper. `secret` is delegated
// to <SecretField>, which owns its own chrome (and Edit/Clear/Show/Hide/Copy).
// onChange emits a full new values object so the parent can keep its
// immutable update pattern.

import type {
  FieldDescriptor,
  SecretMaskValue,
  SecretWrite,
} from "../../types/config";
import { Input } from "../ui/Input";
import { Textarea } from "../ui/Textarea";
import { Checkbox } from "../ui/Checkbox";
import { Label } from "../ui/Label";
import { HelperText } from "../ui/HelperText";
import { FieldError } from "../ui/FieldError";
import { SecretField } from "./SecretField";

interface Props {
  fields: FieldDescriptor[];
  values: Record<string, unknown>;
  /** Optional field-level error map keyed by field name. */
  errors?: Record<string, string>;
  onChange: (next: Record<string, unknown>) => void;
}

export function ConfigForm({ fields, values, errors, onChange }: Props) {
  const update = (name: string, next: unknown) => {
    onChange({ ...values, [name]: next });
  };

  return (
    <div className="space-y-4">
      {fields.map((field) => {
        const v = values[field.name];
        const error = errors?.[field.name];

        if (field.type === "secret") {
          const secretValue: SecretMaskValue =
            (v as SecretMaskValue | undefined) ?? { has_value: false };
          return (
            <div key={field.name} className="py-1">
              <Label required={field.required}>{field.label}</Label>
              <SecretField
                value={secretValue}
                onChange={(next: SecretWrite | undefined) =>
                  update(field.name, next ?? v)
                }
              />
              {error ? <FieldError>{error}</FieldError> : null}
              {!error && field.description ? (
                <HelperText>{field.description}</HelperText>
              ) : null}
            </div>
          );
        }

        if (field.type === "boolean") {
          return (
            <div key={field.name} className="py-1">
              <Checkbox
                label={field.label}
                required={field.required}
                checked={Boolean(v)}
                onChange={(e) => update(field.name, e.target.checked)}
                error={error}
                helper={error ? undefined : field.description}
              />
            </div>
          );
        }

        if (field.type === "list") {
          const listValue = Array.isArray(v) ? v.join(", ") : "";
          return (
            <Input
              key={field.name}
              label={field.label}
              required={field.required}
              value={listValue}
              className="font-mono text-[13px]"
              onChange={(e) =>
                update(
                  field.name,
                  e.target.value
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean),
                )
              }
              error={error}
              helper={
                error
                  ? undefined
                  : (field.description ?? "One value per line, or comma-separated.")
              }
            />
          );
        }

        if (field.type === "object") {
          const textValue =
            v === undefined || v === null
              ? ""
              : typeof v === "string"
                ? v
                : JSON.stringify(v);
          return (
            <Textarea
              key={field.name}
              label={field.label}
              required={field.required}
              value={textValue}
              onChange={(e) => update(field.name, e.target.value)}
              error={error}
              helper={
                error
                  ? undefined
                  : (field.description ?? "JSON or free-form value, stored as a string.")
              }
              rows={4}
            />
          );
        }

        // string (default)
        return (
          <Input
            key={field.name}
            label={field.label}
            required={field.required}
            value={(v as string | undefined) ?? ""}
            className="font-mono text-[13px]"
            onChange={(e) => update(field.name, e.target.value)}
            error={error}
            helper={error ? undefined : field.description}
          />
        );
      })}
    </div>
  );
}