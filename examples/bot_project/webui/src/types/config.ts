export type FieldType = "string" | "boolean" | "list" | "secret" | "object";
export type DomainFlavor = "singleton" | "registry";

export interface FieldDescriptor {
  name: string;
  label: string;
  type: FieldType;
  required: boolean;
  /** Optional helper text shown beneath the input. */
  description?: string;
}

export interface SecretMaskValue {
  has_value: boolean;
  hint?: string;
}

// A secret field, when being written, is one of:
//   { value: string }            -> overwrite
//   { set: false }               -> clear
//   { has_value, hint } | omitted -> keep current (default; prevents accidental wipe)
export type SecretWrite =
  | { value: string }
  | { set: false }
  | { has_value: boolean; hint?: string };

export interface RegistrySection {
  label: string;
  values: Record<string, unknown>;
  fields: FieldDescriptor[];
}

export interface ConfigPayload {
  domain: string;
  label: string;
  flavor: DomainFlavor;
  restart_required: boolean;
  values?: Record<string, unknown>;
  fields?: FieldDescriptor[];
  sections?: Record<string, RegistrySection>;
}
