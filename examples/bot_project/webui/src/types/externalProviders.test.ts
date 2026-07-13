import { describe, it, expect } from "vitest";
import {
  EXTERNAL_PROVIDERS,
  DEFAULT_EXTERNAL_PROVIDER,
  DEFAULT_EXTERNAL_PROVIDER_DESCRIPTOR,
  PROVIDER_OPTIONS,
  resolveProvider,
  selectProvider,
  descriptorFor,
} from "./externalProviders";

describe("externalProviders catalog", () => {
  it("currently exposes exactly one provider: OpenCode", () => {
    expect(EXTERNAL_PROVIDERS).toHaveLength(1);
    expect(DEFAULT_EXTERNAL_PROVIDER_DESCRIPTOR.value).toBe("opencode");
    expect(DEFAULT_EXTERNAL_PROVIDER_DESCRIPTOR.label).toBe("OpenCode");
  });

  it("default provider is the first catalog entry", () => {
    expect(DEFAULT_EXTERNAL_PROVIDER).toBe("opencode");
    expect(DEFAULT_EXTERNAL_PROVIDER_DESCRIPTOR.value).toBe(
      DEFAULT_EXTERNAL_PROVIDER,
    );
  });

  it("PROVIDER_OPTIONS mirrors the catalog", () => {
    expect(PROVIDER_OPTIONS).toHaveLength(EXTERNAL_PROVIDERS.length);
    expect(PROVIDER_OPTIONS.map((o) => o.value)).toEqual(
      EXTERNAL_PROVIDERS.map((d) => d.value),
    );
    expect(PROVIDER_OPTIONS.map((o) => o.label)).toEqual(
      EXTERNAL_PROVIDERS.map((d) => d.label),
    );
  });

  it("resolveProvider returns the descriptor for an enabled kind", () => {
    expect(resolveProvider("opencode")?.cliName).toBe("OpenCode");
  });

  it("resolveProvider returns null for an unsupported/absent kind", () => {
    expect(resolveProvider("pi")).toBeNull();
    expect(resolveProvider(null)).toBeNull();
    expect(resolveProvider(undefined)).toBeNull();
  });

  it("selectProvider keeps an enabled kind and falls back to default otherwise", () => {
    expect(selectProvider("opencode")).toBe("opencode");
    expect(selectProvider("pi")).toBe(DEFAULT_EXTERNAL_PROVIDER);
    expect(selectProvider(null)).toBe(DEFAULT_EXTERNAL_PROVIDER);
  });

  it("descriptorFor returns the descriptor for an enabled kind", () => {
    expect(descriptorFor("opencode").cliName).toBe("OpenCode");
  });

  it("descriptorFor falls back to the default descriptor for unsupported/absent kind", () => {
    expect(descriptorFor("pi").value).toBe(DEFAULT_EXTERNAL_PROVIDER);
    expect(descriptorFor(null).value).toBe(DEFAULT_EXTERNAL_PROVIDER);
  });
});
