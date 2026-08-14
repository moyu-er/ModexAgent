import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
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

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GraphSpecDetail", () => {
  it("renders spec header, topology canvas, and instance rows", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([INSTANCE_LIST_ITEM]);
    mockGetInstance.mockResolvedValue(INSTANCE_DETAIL);

    renderDetail();

    await waitFor(() => {
      expect(screen.getByText("simple")).toBeTruthy();
    });

    expect(screen.getByText("v1.0")).toBeTruthy();
    expect(screen.getByTestId("topology-canvas")).toBeTruthy();
    expect(screen.getByText("#12345")).toBeTruthy();
    expect(screen.getByText("running")).toBeTruthy();
    // Progress from the detail fetch (1 of 2 nodes completed).
    await waitFor(() => {
      expect(screen.getByText(/1\/2 nodes/)).toBeTruthy();
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

  it("runs the graph from the composer and opens the new instance", async () => {
    const onOpenInstance = vi.fn();
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);
    mockRunGraph.mockResolvedValue({
      graph_instance_id: "new-inst-1",
      status: "pending",
    });

    renderDetail({ onOpenInstance });

    const textarea = await screen.findByPlaceholderText(
      "Trigger a new instance... (Enter to run)",
    );
    fireEvent.change(textarea, { target: { value: "hello graph" } });
    fireEvent.click(screen.getByText("Run"));

    await waitFor(() => {
      expect(mockRunGraph).toHaveBeenCalledWith("ws", "42", "hello graph");
    });
    await waitFor(() => {
      expect(onOpenInstance).toHaveBeenCalledWith("new-inst-1");
    });
  });

  it("does not call runGraph when the composer is empty", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail();

    await screen.findByPlaceholderText(
      "Trigger a new instance... (Enter to run)",
    );
    const runButton = screen.getByText("Run").closest("button");
    expect(runButton?.disabled).toBe(true);
    expect(mockRunGraph).not.toHaveBeenCalled();
  });

  it("calls onBack and onEditYaml from the header buttons", async () => {
    const onBack = vi.fn();
    const onEditYaml = vi.fn();
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockListInstances.mockResolvedValue([]);

    renderDetail({ onBack, onEditYaml });

    await waitFor(() => {
      expect(screen.getByText("simple")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("Back"));
    expect(onBack).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByText("Edit YAML"));
    expect(onEditYaml).toHaveBeenCalledOnce();
  });
});
