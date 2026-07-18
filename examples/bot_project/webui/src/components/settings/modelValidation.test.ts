import { describe, it, expect } from "vitest";
import { validateModelValues } from "./modelValidation";

describe("validateModelValues", () => {
  it("returns null when default_provider is empty (allow save without default)", () => {
    expect(validateModelValues({
      default_provider: "",
      default_model: "m1",
      providers: [{ name: "P", models: [{ name: "m1" }] }],
    })).toBeNull();
  });

  it("returns null when default_model is empty (allow save without default)", () => {
    expect(validateModelValues({
      default_provider: "P",
      default_model: "",
      providers: [{ name: "P", models: [{ name: "m1" }] }],
    })).toBeNull();
  });

  it("returns null when both defaults are empty", () => {
    expect(validateModelValues({
      default_provider: "",
      default_model: "",
      providers: [{ name: "P", models: [{ name: "m1" }] }],
    })).toBeNull();
  });

  it("returns null when defaults are missing entirely", () => {
    expect(validateModelValues({
      providers: [{ name: "P", models: [{ name: "m1" }] }],
    })).toBeNull();
  });

  it("returns defaultNotFound when the combo does not exist in providers", () => {
    expect(validateModelValues({
      default_provider: "P",
      default_model: "missing",
      providers: [{ name: "P", models: [{ name: "m1" }] }],
    })).toBe("settings.models.defaultNotFound");
  });

  it("returns defaultNotFound when the provider does not exist", () => {
    expect(validateModelValues({
      default_provider: "Ghost",
      default_model: "m1",
      providers: [{ name: "P", models: [{ name: "m1" }] }],
    })).toBe("settings.models.defaultNotFound");
  });

  it("returns null for a valid combo", () => {
    expect(validateModelValues({
      default_provider: "P",
      default_model: "m1",
      providers: [{ name: "P", models: [{ name: "m1" }] }],
    })).toBeNull();
  });

  it("returns null for a valid combo across multiple providers", () => {
    expect(validateModelValues({
      default_provider: "B",
      default_model: "m2",
      providers: [
        { name: "A", models: [{ name: "m1" }] },
        { name: "B", models: [{ name: "m1" }, { name: "m2" }] },
      ],
    })).toBeNull();
  });

  it("handles renamed-provider edge: default points to old name, provider renamed", () => {
    expect(validateModelValues({
      default_provider: "OldName",
      default_model: "m1",
      providers: [{ name: "NewName", models: [{ name: "m1" }] }],
    })).toBe("settings.models.defaultNotFound");
  });

  it("returns null when providers is empty and defaults are also empty", () => {
    expect(validateModelValues({
      default_provider: "",
      default_model: "",
      providers: [],
    })).toBeNull();
  });

  it("handles whitespace-only defaults as empty (passes validation)", () => {
    expect(validateModelValues({
      default_provider: "  ",
      default_model: "  ",
      providers: [{ name: "P", models: [{ name: "m1" }] }],
    })).toBeNull();
  });

  it("handles missing providers array", () => {
    expect(validateModelValues({
      default_provider: "P",
      default_model: "m1",
    })).toBe("settings.models.defaultNotFound");
  });
});
