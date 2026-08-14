import { describe, it, expect } from "vitest";
import { mergeGraphOutput } from "./mergeOutput";
import type { GraphPayload } from "../../../lib/graphsApi";

describe("mergeGraphOutput", () => {
  it("returns empty string for null", () => {
    expect(mergeGraphOutput(null)).toBe("");
  });

  it("returns empty string for undefined", () => {
    expect(mergeGraphOutput(undefined)).toBe("");
  });

  it("returns empty string for empty array", () => {
    expect(mergeGraphOutput([])).toBe("");
  });

  it("returns the single content unchanged for one payload", () => {
    expect(mergeGraphOutput([{ content: "hello" }])).toBe("hello");
  });

  it("joins two payloads with blank line", () => {
    expect(mergeGraphOutput([{ content: "a" }, { content: "b" }])).toBe("a\n\nb");
  });

  it("joins three payloads with blank lines between each", () => {
    expect(
      mergeGraphOutput([
        { content: "line1" },
        { content: "line2" },
        { content: "line3" },
      ]),
    ).toBe("line1\n\nline2\n\nline3");
  });

  it("preserves internal whitespace and newlines in content", () => {
    const payload: GraphPayload = { content: "# Title\n\n- item a\n- item b" };
    expect(mergeGraphOutput([payload])).toBe("# Title\n\n- item a\n- item b");
  });

  it("preserves multi-line content across multiple payloads", () => {
    expect(
      mergeGraphOutput([
        { content: "first\nline" },
        { content: "second\nline" },
      ]),
    ).toBe("first\nline\n\nsecond\nline");
  });

  it("joins empty-content payloads with the separator (no filtering)", () => {
    // join("\n\n") does not drop empty entries: two empty contents -> one separator.
    expect(mergeGraphOutput([{ content: "" }, { content: "" }])).toBe("\n\n");
  });

  it("skips nothing — joins even empty-content payloads with separator", () => {
    // Empty content between non-empty: join still produces the separator.
    expect(
      mergeGraphOutput([{ content: "a" }, { content: "" }, { content: "c" }]),
    ).toBe("a\n\n\n\nc");
  });
});
