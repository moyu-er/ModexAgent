import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { GraphSpecListPage } from "./GraphSpecListPage";
import {
  getSpecs,
  getTopology,
  type GraphSpecSummary,
  type GraphTopology,
} from "../../lib/graphsApi";

vi.mock("../../lib/graphsApi", () => ({
  getSpecs: vi.fn(),
  getTopology: vi.fn(),
  listInstances: vi.fn(),
  getInstance: vi.fn(),
  getEvents: vi.fn(),
}));

const mockGetSpecs = vi.mocked(getSpecs);
const mockGetTopology = vi.mocked(getTopology);

const SPEC_SUMMARY: GraphSpecSummary = {
  spec_id: "42",
  name: "simple",
  version: "1.0",
};

const TOPOLOGY_DTO: GraphTopology = {
  spec_id: "42",
  name: "simple",
  scheduler: "linear",
  default_trigger: "on_all_preds",
  nodes: [
    {
      name: "worker",
      node_type: "agent",
      config: { agent: "worker", pool: "default" },
      trigger: null,
    },
  ],
  edges: [
    { source: "__start__", target: "worker" },
    { source: "worker", target: "__end__" },
  ],
  entry_node: "__start__",
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GraphSpecListPage", () => {
  it("renders the spec list with MiniTopology + metadata after loading", async () => {
    mockGetSpecs.mockResolvedValue([SPEC_SUMMARY]);
    mockGetTopology.mockResolvedValue(TOPOLOGY_DTO);

    render(
      <GraphSpecListPage
        workspaceId=""
        onEditSpec={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("simple")).toBeTruthy();
    });

    // MiniTopology SVG rendered
    expect(screen.getByTestId("mini-topology")).toBeTruthy();
    // Metadata: "1 nodes · linear · on_all_preds"
    expect(screen.getByText(/1 nodes/)).toBeTruthy();
    expect(screen.getByText(/linear/)).toBeTruthy();
    expect(screen.getByText(/on_all_preds/)).toBeTruthy();
  });

  it("shows the empty-state hint when no specs exist", async () => {
    mockGetSpecs.mockResolvedValue([]);

    render(
      <GraphSpecListPage
        workspaceId=""
        onEditSpec={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(
        screen.getByText("Add a YAML file to config/graphs/ to create a graph spec."),
      ).toBeTruthy();
    });
  });

  it("calls onEditSpec when a spec row is clicked", async () => {
    const onEditSpec = vi.fn();
    mockGetSpecs.mockResolvedValue([SPEC_SUMMARY]);
    mockGetTopology.mockResolvedValue(TOPOLOGY_DTO);

    render(
      <GraphSpecListPage
        workspaceId=""
        onEditSpec={onEditSpec}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("simple")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("simple"));
    expect(onEditSpec).toHaveBeenCalledWith("42");
  });

  it("renders without crashing when the topology fetch fails", async () => {
    mockGetSpecs.mockResolvedValue([SPEC_SUMMARY]);
    mockGetTopology.mockRejectedValue(new Error("boom"));

    render(
      <GraphSpecListPage
        workspaceId=""
        onEditSpec={vi.fn()}
      />,
    );

    // Spec name should still render; MiniTopology just omitted
    await waitFor(() => {
      expect(screen.getByText("simple")).toBeTruthy();
    });
    expect(screen.queryByTestId("mini-topology")).toBeNull();
  });
});
