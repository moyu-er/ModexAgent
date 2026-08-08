import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { GraphSpecListPage } from "./GraphSpecListPage";
import {
  getSpec,
  getSpecs,
  type GraphSpecSummary,
  type GraphSpecResponse,
} from "../../lib/graphsApi";

vi.mock("../../lib/graphsApi", () => ({
  getSpecs: vi.fn(),
  getSpec: vi.fn(),
  listInstances: vi.fn(),
  getInstance: vi.fn(),
  getEvents: vi.fn(),
}));

const mockGetSpecs = vi.mocked(getSpecs);
const mockGetSpec = vi.mocked(getSpec);

const SIMPLE_YAML = `
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

const SPEC_SUMMARY: GraphSpecSummary = {
  spec_id: "42",
  name: "simple",
  version: "1.0",
};

const SPEC_RESPONSE: GraphSpecResponse = {
  spec_id: "42",
  name: "simple",
  version: "1.0",
  yaml_content: SIMPLE_YAML,
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GraphSpecListPage", () => {
  it("renders the spec list with MiniTopology + metadata after loading", async () => {
    mockGetSpecs.mockResolvedValue([SPEC_SUMMARY]);
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);

    render(
      <GraphSpecListPage
        workspaceId=""
        onEditSpec={vi.fn()}
        onOpenInstances={vi.fn()}
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
        onOpenInstances={vi.fn()}
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
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);

    render(
      <GraphSpecListPage
        workspaceId=""
        onEditSpec={onEditSpec}
        onOpenInstances={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("simple")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("simple"));
    expect(onEditSpec).toHaveBeenCalledWith("42");
  });

  it("renders without crashing when spec YAML parse fails", async () => {
    mockGetSpecs.mockResolvedValue([SPEC_SUMMARY]);
    mockGetSpec.mockResolvedValue({
      ...SPEC_RESPONSE,
      yaml_content: "not: valid: yaml: [",
    });

    render(
      <GraphSpecListPage
        workspaceId=""
        onEditSpec={vi.fn()}
        onOpenInstances={vi.fn()}
      />,
    );

    // Spec name should still render; MiniTopology just omitted
    await waitFor(() => {
      expect(screen.getByText("simple")).toBeTruthy();
    });
    expect(screen.queryByTestId("mini-topology")).toBeNull();
  });

  it("calls onOpenInstances when the Instances button is clicked", async () => {
    const onOpenInstances = vi.fn();
    mockGetSpecs.mockResolvedValue([]);

    render(
      <GraphSpecListPage
        workspaceId=""
        onEditSpec={vi.fn()}
        onOpenInstances={onOpenInstances}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Instances")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("Instances"));
    expect(onOpenInstances).toHaveBeenCalledOnce();
  });
});
