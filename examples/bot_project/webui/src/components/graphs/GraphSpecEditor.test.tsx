import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { GraphSpecEditor, type GraphSpecEditorProps } from "./GraphSpecEditor";
import { ApiError } from "../../lib/api";

// ── Test fixtures ───────────────────────────────────────────────────────────

const SPEC_YAML = `name: test_wf
version: "1.0"
scheduler: linear
default_trigger: on_receive
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

const SPEC_RESPONSE = {
  spec_id: "test_wf",
  name: "test_wf",
  version: "1.0",
  yaml_content: SPEC_YAML,
};

const RUN_RESPONSE = {
  graph_instance_id: "inst-123",
  status: "pending",
};

// ── Mocks ───────────────────────────────────────────────────────────────────

vi.mock("../../lib/graphsApi", () => ({
  getSpec: vi.fn(),
  updateSpec: vi.fn(),
  runGraph: vi.fn(),
}));

// Mock YamlCodeEditor to a simple controlled textarea.
vi.mock("./yaml/YamlCodeEditor", () => ({
  YamlCodeEditor: ({
    value,
    onChange,
    errors,
    className,
  }: {
    value: string;
    onChange?: (v: string) => void;
    errors?: ReadonlyArray<{ line: number; message: string }>;
    className?: string;
  }) => (
    <div data-testid="yaml-editor-mock" className={className}>
      <textarea
        data-testid="yaml-editor-textarea"
        value={value}
        onChange={(e) => onChange?.(e.target.value)}
      />
      {errors?.map((err, i) => (
        <span key={i} data-testid={`lint-error-${err.line}`}>
          {err.message}
        </span>
      ))}
    </div>
  ),
}));

// Mock TopologyCanvas to avoid heavy SVG rendering in tests.
vi.mock("./topology/TopologyCanvas", () => ({
  TopologyCanvas: ({ topology }: { topology: unknown }) => (
    <div
      data-testid="topology-canvas-mock"
      data-name={(topology as { name: string })?.name}
    >
      topology-canvas
    </div>
  ),
}));

import { getSpec, updateSpec, runGraph } from "../../lib/graphsApi";

function renderEditor(props: Partial<GraphSpecEditorProps> = {}) {
  const defaultProps: GraphSpecEditorProps = {
    workspaceId: "ws-1",
    specId: "test_wf",
    onBack: vi.fn(),
    onRun: vi.fn(),
    ...props,
  };
  return render(<GraphSpecEditor {...defaultProps} />);
}

async function waitForLoaded(): Promise<void> {
  await waitFor(() => {
    expect(screen.queryByText("Loading…")).toBeNull();
  });
}

describe("GraphSpecEditor", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "requestAnimationFrame",
      (cb: (t: number) => void) => setTimeout(() => cb(0), 0) as unknown as number,
    );
    vi.stubGlobal("cancelAnimationFrame", (id: number) => clearTimeout(id));
    vi.mocked(getSpec).mockResolvedValue(SPEC_RESPONSE);
    vi.mocked(updateSpec).mockResolvedValue(SPEC_RESPONSE);
    vi.mocked(runGraph).mockResolvedValue(RUN_RESPONSE);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // ── Loading ──────────────────────────────────────────────────────────────

  it("shows loading state while fetching spec", () => {
    vi.mocked(getSpec).mockReturnValue(new Promise(() => {}));
    renderEditor();
    expect(screen.getByText("Loading…")).toBeTruthy();
  });

  // ── Full-canvas layout ───────────────────────────────────────────────────

  it("renders full-canvas topology preview after load", async () => {
    renderEditor();
    await waitForLoaded();
    expect(screen.getByTestId("topology-canvas-mock")).toBeTruthy();
  });

  it("renders topology preview when backend YAML serializes a trigger-less node as trigger: null", async () => {
    // Representative of backend `_yaml()` output (yaml.dump of
    // GraphSpec.model_dump(mode="json"), sort_keys=False): NodeSpec.trigger
    // defaults to None, so trigger-less nodes round-trip as `trigger: null`.
    const BACKEND_YAML = `name: test_wf
nodes:
- name: worker
  node_type: agent
  config:
    agent: worker
    pool: default
  trigger: null
edges:
- source: __start__
  target: worker
- source: worker
  target: __end__
state_class: default
scheduler: linear
version: '1.0'
metadata: {}
max_iterations: 25
default_trigger: on_all_preds
`;
    vi.mocked(getSpec).mockResolvedValue({
      ...SPEC_RESPONSE,
      yaml_content: BACKEND_YAML,
    });
    renderEditor();
    await waitForLoaded();
    expect(screen.getByTestId("topology-canvas-mock")).toBeTruthy();
  });

  it("renders Back button and spec name in header", async () => {
    renderEditor();
    await waitForLoaded();
    expect(screen.getByText("Back")).toBeTruthy();
    expect(screen.getByText("test_wf")).toBeTruthy();
  });

  it("renders Edit YAML button in header", async () => {
    renderEditor();
    await waitForLoaded();
    expect(screen.getByTestId("spec-editor-edit-yaml")).toBeTruthy();
  });

  // ── YAML slide-out panel ─────────────────────────────────────────────────

  it("YAML editor is not visible until Edit YAML is clicked", async () => {
    renderEditor();
    await waitForLoaded();
    expect(screen.queryByTestId("yaml-editor-mock")).toBeNull();
  });

  it("opens YAML panel on Edit YAML click", async () => {
    renderEditor();
    await waitForLoaded();
    fireEvent.click(screen.getByTestId("spec-editor-edit-yaml"));
    expect(screen.getByTestId("spec-editor-panel")).toBeTruthy();
    expect(screen.getByTestId("yaml-editor-mock")).toBeTruthy();
  });

  it("closes YAML panel on Cancel", async () => {
    renderEditor();
    await waitForLoaded();
    fireEvent.click(screen.getByTestId("spec-editor-edit-yaml"));
    expect(screen.getByTestId("spec-editor-panel")).toBeTruthy();

    // Click Cancel (in the panel footer)
    const cancelBtn = screen.getByText("Cancel");
    fireEvent.click(cancelBtn);
    expect(screen.queryByTestId("spec-editor-panel")).toBeNull();
  });

  it("closes YAML panel on scrim click", async () => {
    renderEditor();
    await waitForLoaded();
    fireEvent.click(screen.getByTestId("spec-editor-edit-yaml"));
    expect(screen.getByTestId("spec-editor-panel")).toBeTruthy();

    fireEvent.click(screen.getByTestId("spec-editor-scrim"));
    expect(screen.queryByTestId("spec-editor-panel")).toBeNull();
  });

  // ── Save flow ────────────────────────────────────────────────────────────

  it("calls updateSpec on Save and closes panel on success", async () => {
    renderEditor();
    await waitForLoaded();
    fireEvent.click(screen.getByTestId("spec-editor-edit-yaml"));

    // Edit YAML in panel
    const textarea = screen.getByTestId(
      "yaml-editor-textarea",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: SPEC_YAML + "\n# comment" } });

    // Click Save
    const saveButtons = screen.getAllByText("Save");
    fireEvent.click(saveButtons[0]!);

    await waitFor(() => {
      expect(vi.mocked(updateSpec)).toHaveBeenCalled();
    });
    // Panel closes on success
    await waitFor(() => {
      expect(screen.queryByTestId("spec-editor-panel")).toBeNull();
    });
  });

  it("shows error in panel when Save fails", async () => {
    const apiErr = new ApiError(
      400,
      "Bad Request",
      JSON.stringify({
        error: "validation_error",
        detail: "unknown node_type 'foo' at line 5",
      }),
    );
    vi.mocked(updateSpec).mockRejectedValue(apiErr);

    renderEditor();
    await waitForLoaded();
    fireEvent.click(screen.getByTestId("spec-editor-edit-yaml"));

    const saveButtons = screen.getAllByText("Save");
    fireEvent.click(saveButtons[0]!);

    await waitFor(() => {
      const panel = screen.queryByTestId("spec-editor-error-panel");
      expect(panel).toBeTruthy();
      expect(panel?.textContent).toContain("validation_error");
    });
    // Panel stays open on error
    expect(screen.getByTestId("spec-editor-panel")).toBeTruthy();
  });

  it("maps backend line errors to lint markers on Save failure", async () => {
    const apiErr = new ApiError(
      400,
      "Bad Request",
      JSON.stringify({
        error: "validation_error",
        detail: "unknown node_type 'foo' at line 5",
      }),
    );
    vi.mocked(updateSpec).mockRejectedValue(apiErr);

    renderEditor();
    await waitForLoaded();
    fireEvent.click(screen.getByTestId("spec-editor-edit-yaml"));

    const saveButtons = screen.getAllByText("Save");
    fireEvent.click(saveButtons[0]!);

    await waitFor(() => {
      const lintMarker = screen.queryByTestId("lint-error-5");
      expect(lintMarker).toBeTruthy();
      expect(lintMarker?.textContent).toContain("line 5");
    });
  });

  // ── Run flow ─────────────────────────────────────────────────────────────

  it("calls runGraph and navigates to instance on Run", async () => {
    const onRun = vi.fn();
    renderEditor({ onRun });
    await waitForLoaded();

    fireEvent.click(screen.getByTestId("spec-editor-run"));

    await waitFor(() => {
      expect(vi.mocked(runGraph)).toHaveBeenCalledWith(
        "ws-1",
        "test_wf",
        undefined,
      );
    });

    await waitFor(() => {
      expect(onRun).toHaveBeenCalledWith("inst-123");
    });
  });

  it("passes user input to runGraph when provided", async () => {
    renderEditor();
    await waitForLoaded();

    const input = screen.getByPlaceholderText(
      "Content delivered to the graph's start node",
    );
    fireEvent.change(input, { target: { value: "hello world" } });

    fireEvent.click(screen.getByTestId("spec-editor-run"));

    await waitFor(() => {
      expect(vi.mocked(runGraph)).toHaveBeenCalledWith(
        "ws-1",
        "test_wf",
        "hello world",
      );
    });
  });
});
