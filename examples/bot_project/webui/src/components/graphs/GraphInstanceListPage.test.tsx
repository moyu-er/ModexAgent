import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { GraphInstanceListPage } from "./GraphInstanceListPage";
import {
  getInstance,
  getSpec,
  listInstances,
  type GraphInstance,
  type GraphSpecResponse,
} from "../../lib/graphsApi";

vi.mock("../../lib/graphsApi", () => ({
  listInstances: vi.fn(),
  getInstance: vi.fn(),
  getSpec: vi.fn(),
  getEvents: vi.fn(),
  GRAPH_INSTANCE_STATUSES: [
    "pending",
    "running",
    "paused",
    "stopped",
    "crashed",
    "completed",
    "failed",
  ] as const,
}));

const mockListInstances = vi.mocked(listInstances);
const mockGetInstance = vi.mocked(getInstance);
const mockGetSpec = vi.mocked(getSpec);

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
};

const INSTANCE_DETAIL: GraphInstance = {
  spec_id: "42",
  graph_instance_id: "12345",
  status: "running",
  nodes: [
    { node_name: "worker", node_id: "node_1", status: "running" },
  ],
  result: null,
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GraphInstanceListPage", () => {
  it("renders instances with MiniTopology + instance ID + spec name + status badge", async () => {
    mockListInstances.mockResolvedValue([INSTANCE_LIST_ITEM]);
    mockGetInstance.mockResolvedValue(INSTANCE_DETAIL);
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);

    render(
      <GraphInstanceListPage
        workspaceId=""
        onOpenInstance={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("12345")).toBeTruthy();
    });

    // MiniTopology rendered
    expect(screen.getByTestId("mini-topology")).toBeTruthy();
    // Spec name
    expect(screen.getByText("simple")).toBeTruthy();
    // Status badge
    expect(screen.getByText("running")).toBeTruthy();
  });

  it("shows empty state when no instances", async () => {
    mockListInstances.mockResolvedValue([]);

    render(
      <GraphInstanceListPage
        workspaceId=""
        onOpenInstance={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("No graph instances")).toBeTruthy();
    });
  });

  it("calls onOpenInstance when a row is clicked", async () => {
    const onOpenInstance = vi.fn();
    mockListInstances.mockResolvedValue([INSTANCE_LIST_ITEM]);
    mockGetInstance.mockResolvedValue(INSTANCE_DETAIL);
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);

    render(
      <GraphInstanceListPage
        workspaceId=""
        onOpenInstance={onOpenInstance}
        onBack={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("12345")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("12345"));
    expect(onOpenInstance).toHaveBeenCalledWith("12345");
  });

  it("de-duplicates spec fetches when multiple instances share the same spec_id", async () => {
    const inst2: GraphInstance = {
      ...INSTANCE_LIST_ITEM,
      graph_instance_id: "12346",
    };
    mockListInstances.mockResolvedValue([INSTANCE_LIST_ITEM, inst2]);
    mockGetInstance.mockResolvedValue(INSTANCE_DETAIL);
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);

    render(
      <GraphInstanceListPage
        workspaceId=""
        onOpenInstance={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("12345")).toBeTruthy();
      expect(screen.getByText("12346")).toBeTruthy();
    });

    // getSpec called only once for spec_id "42" (de-dup)
    expect(mockGetSpec).toHaveBeenCalledTimes(1);
    // getInstance called once per instance
    expect(mockGetInstance).toHaveBeenCalledTimes(2);
  });

  it("shows progress as completed/total nodes", async () => {
    const instWithCompleted: GraphInstance = {
      spec_id: "42",
      graph_instance_id: "12347",
      status: "completed",
      nodes: [],
      result: null,
    };
    const detailWithCompleted: GraphInstance = {
      spec_id: "42",
      graph_instance_id: "12347",
      status: "completed",
      nodes: [
        { node_name: "worker", node_id: "node_1", status: "completed" },
      ],
      result: null,
    };

    mockListInstances.mockResolvedValue([instWithCompleted]);
    mockGetInstance.mockResolvedValue(detailWithCompleted);
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);

    render(
      <GraphInstanceListPage
        workspaceId=""
        onOpenInstance={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/1\/1 nodes/)).toBeTruthy();
    });
  });

  it("calls onBack when the back button is clicked", async () => {
    const onBack = vi.fn();
    mockListInstances.mockResolvedValue([]);

    render(
      <GraphInstanceListPage
        workspaceId=""
        onOpenInstance={vi.fn()}
        onBack={onBack}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Back")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("Back"));
    expect(onBack).toHaveBeenCalledOnce();
  });
});
