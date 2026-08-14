import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, within, act } from "@testing-library/react";
import { GraphSpecDetail } from "./GraphSpecDetail";
import {
  getInstance,
  getSpec,
  listInstances,
  runGraph,
  type GraphInstance,
  type GraphSpecResponse,
} from "../../lib/graphsApi";

vi.mock("../../lib/graphsApi", () => ({
  getSpec: vi.fn(),
  listInstances: vi.fn(),
  getInstance: vi.fn(),
  runGraph: vi.fn(),
}));

const mockGetSpec = vi.mocked(getSpec);
const mockListInstances = vi.mocked(listInstances);
const mockGetInstance = vi.mocked(getInstance);
const mockRunGraph = vi.mocked(runGraph);

const SPEC_YAML = `
name: simple
version: "1.0"
scheduler: linear
default_trigger: on_all_preds
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

const SPEC_RESPONSE: GraphSpecResponse = {
  spec_id: "42",
  name: "simple",
  version: "1.0",
  yaml_content: SPEC_YAML,
};

const INSTANCE_LIST_ITEM: GraphInstance = {
  spec_id: "42",
  graph_instance_id: "12345",
  status: "running",
  nodes: [],
  result: null,
  created_at: 1000,
  updated_at: 13000,
};

const INSTANCE_DETAIL: GraphInstance = {
  spec_id: "42",
  graph_instance_id: "12345",
  status: "running",
  nodes: [
    { node_name: "worker", node_id: "node_1", status: "running" },
    { node_name: "helper", node_id: "node_2", status: "completed" },
  ],
  result: null,
};

function renderDetail(overrides?: {
  onBack?: () => void;
  onEditYaml?: () => void;
  onOpenInstance?: (id: string) => void;
}) {
  return render(
    <GraphSpecDetail
      workspaceId="ws"
      specId="42"
      onBack={overrides?.onBack ?? vi.fn()}
      onEditYaml={overrides?.onEditYaml ?? vi.fn()}
      onOpenInstance={overrides?.onOpenInstance ?? vi.fn()}
    />,
  );
}

async function waitForDetail(): Promise<void> {
  await waitFor(() => {
    expect(screen.getByText("simple")).toBeTruthy();
  });
}

function getRow(): HTMLElement {
  const row = screen.getByText("#12345").closest("button");
  if (!row) throw new Error("instance row not found");
  return row;
}

async function openModal(): Promise<HTMLElement> {
  const fab = screen.getByRole("button", { name: "New Instance" });
  (fab as HTMLElement).focus();
  fireEvent.click(fab);
  await waitFor(() => {
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
  return screen.getByRole("dialog");
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GraphSpecDetail", () => {
  it("renders spec header, topology canvas, and instance rows", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([INSTANCE_LIST_ITEM]);
    mockGetInstance.mockResolvedValue(INSTANCE_DETAIL);

    renderDetail();
    await waitForDetail();

    expect(screen.getByText("v1.0")).toBeTruthy();
    expect(screen.getByTestId("topology-canvas")).toBeTruthy();
    // Badge query scoped to the row: the canvas legend chips render the same
    // status words as standalone text nodes (T10).
    const row = getRow();
    expect(within(row).getByText("running")).toBeTruthy();
    // Progress from the detail fetch (1 of 2 nodes completed).
    await waitFor(() => {
      expect(within(row).getByText(/1\/2 nodes/)).toBeTruthy();
    });
  });

  it("shows the empty state when the spec has no instances", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail();

    await waitFor(() => {
      expect(screen.getByText("No graph instances")).toBeTruthy();
    });
  });

  it("calls onOpenInstance when an instance row is clicked", async () => {
    const onOpenInstance = vi.fn();
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([INSTANCE_LIST_ITEM]);
    mockGetInstance.mockResolvedValue(INSTANCE_DETAIL);

    renderDetail({ onOpenInstance });

    await waitFor(() => {
      expect(screen.getByText("#12345")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("#12345"));
    expect(onOpenInstance).toHaveBeenCalledWith("12345");
  });

  it("row shows badge, progress, relative time, and status-colored mini topology", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([INSTANCE_LIST_ITEM]);
    mockGetInstance.mockResolvedValue(INSTANCE_DETAIL);

    renderDetail();
    await waitForDetail();

    const row = getRow();
    expect(within(row).getByText("running")).toBeTruthy();
    expect(within(row).getByText(/ago|just now/)).toBeTruthy();
    await waitFor(() => {
      expect(within(row).getByText(/1\/2 nodes · 12s/)).toBeTruthy();
    });
    const mini = within(row).getByTestId("mini-topology");
    const workerDot = mini.querySelector('[data-mini-node="worker"]');
    expect(workerDot?.getAttribute("class")).toContain(
      "fill-graph-status-running",
    );
  });

  it("calls onBack and onEditYaml from the header buttons", async () => {
    const onBack = vi.fn();
    const onEditYaml = vi.fn();
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail({ onBack, onEditYaml });
    await waitForDetail();

    fireEvent.click(screen.getByText("Back"));
    expect(onBack).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByText("Edit YAML"));
    expect(onEditYaml).toHaveBeenCalledOnce();
  });
});

describe("GraphSpecDetail — New Instance modal", () => {
  it("renders no composer or dialog until the FAB is clicked", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail();
    await waitForDetail();

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    const fab = screen.getByRole("button", { name: "New Instance" });
    expect(fab.getAttribute("aria-haspopup")).toBe("dialog");
  });

  it("opens a centered dialog with spec header and focuses the textarea", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail();
    await waitForDetail();
    const dialog = await openModal();

    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("aria-label")).toBe("New Instance");
    expect(within(dialog).getByText("simple")).toBeTruthy();
    expect(within(dialog).getByText("spec v1.0")).toBeTruthy();
    const textarea = within(dialog).getByPlaceholderText(
      "Trigger a new instance... (Enter to run)",
    );
    expect(document.activeElement).toBe(textarea);
  });

  it("closes on Escape and returns focus to the FAB", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail();
    await waitForDetail();
    await openModal();

    fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "New Instance" }),
    );
  });

  it("closes on the X button and on backdrop click", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail();
    await waitForDetail();

    let dialog = await openModal();
    fireEvent.click(within(dialog).getByLabelText("Close"));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    dialog = await openModal();
    fireEvent.click(screen.getByTestId("new-instance-backdrop"));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("keeps focus inside the dialog on Tab", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail();
    await waitForDetail();
    const dialog = await openModal();

    fireEvent.keyDown(window, { key: "Tab" });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("runs the graph from the modal and opens the new instance", async () => {
    const onOpenInstance = vi.fn();
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);
    mockRunGraph.mockResolvedValue({
      graph_instance_id: "new-inst-1",
      status: "pending",
    });

    renderDetail({ onOpenInstance });
    await waitForDetail();
    const dialog = await openModal();

    const textarea = within(dialog).getByPlaceholderText(
      "Trigger a new instance... (Enter to run)",
    );
    fireEvent.change(textarea, { target: { value: "hello graph" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run" }));

    await waitFor(() => {
      expect(mockRunGraph).toHaveBeenCalledWith("ws", "42", "hello graph");
    });
    await waitFor(() => {
      expect(onOpenInstance).toHaveBeenCalledWith("new-inst-1");
    });
    // Input clears only on success.
    expect((textarea as HTMLTextAreaElement).value).toBe("");
  });

  it("submits on Enter but not on Shift+Enter", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);
    mockRunGraph.mockResolvedValue({
      graph_instance_id: "new-inst-2",
      status: "pending",
    });

    renderDetail();
    await waitForDetail();
    const dialog = await openModal();

    const textarea = within(dialog).getByPlaceholderText(
      "Trigger a new instance... (Enter to run)",
    );
    fireEvent.change(textarea, { target: { value: "via keyboard" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(mockRunGraph).not.toHaveBeenCalled();
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    await waitFor(() => {
      expect(mockRunGraph).toHaveBeenCalledWith("ws", "42", "via keyboard");
    });
  });

  it("keeps Run disabled while the input is empty", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail();
    await waitForDetail();
    const dialog = await openModal();

    const runButton = within(dialog).getByRole("button", { name: "Run" });
    expect(runButton.getAttribute("disabled")).not.toBeNull();
    expect(mockRunGraph).not.toHaveBeenCalled();
  });

  it("disables the textarea and Run button while submitting", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);
    let resolveRun: (value: { graph_instance_id: string; status: string }) => void = () => {};
    mockRunGraph.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRun = resolve;
        }),
    );

    renderDetail();
    await waitForDetail();
    const dialog = await openModal();

    const textarea = within(dialog).getByPlaceholderText(
      "Trigger a new instance... (Enter to run)",
    );
    fireEvent.change(textarea, { target: { value: "slow run" } });
    const runButton = within(dialog).getByRole("button", { name: "Run" });
    fireEvent.click(runButton);

    await waitFor(() => {
      expect((textarea as HTMLTextAreaElement).disabled).toBe(true);
      expect(runButton.getAttribute("disabled")).not.toBeNull();
    });
    await act(async () => {
      resolveRun({ graph_instance_id: "new-inst-3", status: "pending" });
    });
  });

  it("shows the run error inside the dialog and re-enables the form", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);
    mockRunGraph.mockRejectedValue(new Error("boom"));

    renderDetail();
    await waitForDetail();
    const dialog = await openModal();

    const textarea = within(dialog).getByPlaceholderText(
      "Trigger a new instance... (Enter to run)",
    );
    fireEvent.change(textarea, { target: { value: "will fail" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run" }));

    await waitFor(() => {
      expect(within(dialog).getByText("boom")).toBeTruthy();
    });
    // Dialog stays open, input preserved, controls usable again.
    expect((textarea as HTMLTextAreaElement).value).toBe("will fail");
    expect((textarea as HTMLTextAreaElement).disabled).toBe(false);
  });
});
