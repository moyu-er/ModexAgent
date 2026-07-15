// External coding provider catalog — the single frontend registration point
// for providers exposed in the pool editor. The catalog currently enables
// OpenCode; adding a future provider is one entry here plus the backend
// ProviderKind extension as needed. All component logic is generic and derives
// options/defaults/copy from this seam.
//
// The catalog satisfies ProviderKind at compile time: each descriptor's
// `value` is a ProviderKind. EXTERNAL_PROVIDERS is a non-empty tuple
// ([ProviderDescriptor, ...ProviderDescriptor[]]) so index 0 is provably
// present — no non-null assertions needed.

import type { ProviderKind } from "./pool";

export interface ProviderDescriptor {
  /** Wire value; must be a member of ProviderKind. */
  readonly value: ProviderKind;
  /** Human-facing dropdown label. */
  readonly label: string;
  /** CLI product name used in the managed-runtime help copy. */
  readonly cliName: string;
}

/** Statically non-empty provider tuple — index 0 is always present. */
type ProviderCatalog = readonly [ProviderDescriptor, ...ProviderDescriptor[]];

/**
 * Enabled external providers, in display order. The catalog currently enables
 * OpenCode only.
 */
export const EXTERNAL_PROVIDERS: ProviderCatalog = [
  { value: "opencode", label: "OpenCode", cliName: "OpenCode" },
];

/** Default descriptor (first catalog entry). Guaranteed by the tuple type. */
export const DEFAULT_EXTERNAL_PROVIDER_DESCRIPTOR: ProviderDescriptor =
  EXTERNAL_PROVIDERS[0];

/** Default provider wire value chosen when switching Native → External. */
export const DEFAULT_EXTERNAL_PROVIDER: ProviderKind =
  DEFAULT_EXTERNAL_PROVIDER_DESCRIPTOR.value;

const PROVIDER_BY_KIND: ReadonlyMap<ProviderKind, ProviderDescriptor> =
  new Map(EXTERNAL_PROVIDERS.map((d) => [d.value, d]));

/**
 * Selectable <option> shape for the existing Select primitive.
 * Fields are mutable to satisfy the Select component's SelectOption[] prop.
 */
export interface ProviderOption {
  value: ProviderKind;
  label: string;
}

export const PROVIDER_OPTIONS: ProviderOption[] = EXTERNAL_PROVIDERS.map(
  (d) => ({ value: d.value, label: d.label }),
);

/**
 * Resolve a node's provider_kind to a descriptor when it is an enabled
 * catalog provider; otherwise return null so the caller falls back to the
 * catalog default. An unsupported existing provider (e.g. a legacy pool with
 * a provider not yet enabled in the catalog) is not silently preserved
 * through rendering — the UI shows the default instead.
 */
export function resolveProvider(
  kind: ProviderKind | null | undefined,
): ProviderDescriptor | null {
  if (kind === null || kind === undefined) return null;
  return PROVIDER_BY_KIND.get(kind) ?? null;
}

/**
 * The provider to display/select: the node's kind when it resolves to an
 * enabled catalog provider, otherwise the catalog default.
 */
export function selectProvider(
  kind: ProviderKind | null | undefined,
): ProviderKind {
  return resolveProvider(kind)?.value ?? DEFAULT_EXTERNAL_PROVIDER;
}

/**
 * Always returns a descriptor: the node's kind when it resolves to an enabled
 * catalog provider, otherwise the default descriptor. Use this for help copy
 * and display labels where a null return would be inconvenient.
 */
export function descriptorFor(
  kind: ProviderKind | null | undefined,
): ProviderDescriptor {
  return resolveProvider(kind) ?? DEFAULT_EXTERNAL_PROVIDER_DESCRIPTOR;
}
