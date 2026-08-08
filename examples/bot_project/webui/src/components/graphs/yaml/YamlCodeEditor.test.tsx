import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { YamlCodeEditor, mapErrorsToDiagnostics } from "./YamlCodeEditor";

const SIMPLE_YAML = `name: test_wf
version: "1.0"
scheduler: linear
nodes:
  - name: worker
    node_type: agent
    config:
      agent: worker
      pool: default
edges:
  - source: __start__
    target: worker
  - source: worker
    target: __end__
`;

describe("mapErrorsToDiagnostics (pure function)", () => {
  it("returns empty array for no errors", () => {
    expect(mapErrorsToDiagnostics([], SIMPLE_YAML)).toEqual([]);
  });

  it("maps line 1 error to correct offset", () => {
    const diags = mapErrorsToDiagnostics(
      [{ line: 1, message: "bad name" }],
      SIMPLE_YAML,
    );
    expect(diags).toHaveLength(1);
    expect(diags[0]!.from).toBe(0);
    expect(diags[0]!.message).toBe("bad name");
    expect(diags[0]!.severity).toBe("error");
  });

  it("maps line 3 error to the start offset of that line", () => {
    const diags = mapErrorsToDiagnostics(
      [{ line: 3, message: "bad scheduler" }],
      SIMPLE_YAML,
    );
    expect(diags).toHaveLength(1);
    expect(diags[0]!.from).toBeGreaterThan(0);
    expect(diags[0]!.to).toBeGreaterThan(diags[0]!.from);
  });

  it("clamps out-of-range line numbers to the last line", () => {
    const diags = mapErrorsToDiagnostics(
      [{ line: 999, message: "overflow" }],
      SIMPLE_YAML,
    );
    expect(diags).toHaveLength(1);
    expect(diags[0]!.from).toBeLessThanOrEqual(SIMPLE_YAML.length);
  });

  it("handles empty source", () => {
    const diags = mapErrorsToDiagnostics(
      [{ line: 1, message: "err" }],
      "",
    );
    expect(diags).toHaveLength(1);
    expect(diags[0]!.from).toBe(0);
    expect(diags[0]!.to).toBe(0);
  });

  it("maps multiple errors preserving order", () => {
    const diags = mapErrorsToDiagnostics(
      [
        { line: 1, message: "err1" },
        { line: 3, message: "err2" },
      ],
      SIMPLE_YAML,
    );
    expect(diags).toHaveLength(2);
    expect(diags[0]!.message).toBe("err1");
    expect(diags[1]!.message).toBe("err2");
    expect(diags[0]!.from).toBeLessThan(diags[1]!.from);
  });
});

describe("YamlCodeEditor component", () => {
  beforeEach(() => {
    // CodeMirror uses rAF; bridge to setTimeout for vitest/happy-dom.
    vi.stubGlobal(
      "requestAnimationFrame",
      (cb: (t: number) => void) => setTimeout(() => cb(0), 0) as unknown as number,
    );
    vi.stubGlobal("cancelAnimationFrame", (id: number) => clearTimeout(id));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the host container immediately (before CodeMirror loads)", () => {
    render(<YamlCodeEditor value="" />);
    expect(screen.getByTestId("yaml-editor-host")).toBeTruthy();
  });

  it("shows loading state, then loads CodeMirror and shows ready", async () => {
    render(<YamlCodeEditor value={SIMPLE_YAML} />);

    // Initially loading
    expect(screen.getByTestId("yaml-editor-loading")).toBeTruthy();

    // Wait for CodeMirror to load (dynamic import resolves in real time)
    await waitFor(
      () => {
        expect(screen.queryByTestId("yaml-editor-loading")).toBeNull();
      },
      { timeout: 10000 },
    );
  });

  it("renders fallback textarea on import failure", async () => {
    // Force dynamic import to fail by mocking the module system.
    vi.doMock("@codemirror/state", () => {
      throw new Error("module load failed");
    });

    render(<YamlCodeEditor value="test: 1" />);

    await waitFor(
      () => {
        const fallback = screen.queryByTestId("yaml-editor-fallback");
        expect(fallback).toBeTruthy();
      },
      { timeout: 10000 },
    );

    vi.doUnmock("@codemirror/state");
  });

  it("renders CodeMirror DOM with line numbers gutter after load", async () => {
    render(<YamlCodeEditor value={SIMPLE_YAML} />);

    await waitFor(
      () => {
        expect(screen.queryByTestId("yaml-editor-loading")).toBeNull();
      },
      { timeout: 10000 },
    );

    const container = screen.getByTestId("yaml-editor-container");
    const editorEl = container.querySelector(".cm-editor");
    expect(editorEl).toBeTruthy();
    expect(container.querySelector(".cm-gutters")).toBeTruthy();
  });

  it("applies custom className to the host", () => {
    render(<YamlCodeEditor value="" className="my-custom-cls" />);
    const host = screen.getByTestId("yaml-editor-host");
    expect(host.getAttribute("class")).toContain("my-custom-cls");
  });

  it("renders fallback textarea with correct value on error state", async () => {
    vi.doMock("@codemirror/state", () => {
      throw new Error("fail");
    });

    render(<YamlCodeEditor value="key: value" />);

    await waitFor(
      () => {
        const fallback = screen.queryByTestId("yaml-editor-fallback") as HTMLTextAreaElement | null;
        expect(fallback).toBeTruthy();
        expect(fallback?.value).toBe("key: value");
      },
      { timeout: 10000 },
    );

    vi.doUnmock("@codemirror/state");
  });
});
