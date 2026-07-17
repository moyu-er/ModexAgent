import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { FetchModelsModal } from "./FetchModelsModal";
import type { FetchedModel } from "../../lib/api";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const mockModels: FetchedModel[] = [
  { id: "claude-sonnet-4", owned_by: "anthropic" },
  { id: "claude-opus-4", owned_by: "anthropic" },
  { id: "gpt-4o", owned_by: "openai" },
];

afterEach(() => vi.unstubAllGlobals());

describe("FetchModelsModal", () => {
  it("shows loading state then renders fetched models", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(200, { models: mockModels })),
    );

    render(
      <FetchModelsModal
        open
        onClose={() => {}}
        providerKey="test"
        existingModelIds={new Set()}
        onImport={() => {}}
      />,
    );

    expect(screen.getByText("Fetching model list…")).toBeTruthy();

    await waitFor(() => {
      expect(screen.getByText("claude-sonnet-4")).toBeTruthy();
    });
    expect(screen.getByText("claude-opus-4")).toBeTruthy();
    expect(screen.getByText("gpt-4o")).toBeTruthy();
  });

  it("groups models by owned_by vendor", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(200, { models: mockModels })),
    );

    render(
      <FetchModelsModal
        open
        onClose={() => {}}
        providerKey="test"
        existingModelIds={new Set()}
        onImport={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/anthropic/)).toBeTruthy();
    });
    expect(screen.getByText(/openai/)).toBeTruthy();
  });

  it("marks existing models as disabled with 'Already added'", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(200, { models: mockModels })),
    );

    render(
      <FetchModelsModal
        open
        onClose={() => {}}
        providerKey="test"
        existingModelIds={new Set(["claude-sonnet-4"])}
        onImport={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("claude-sonnet-4")).toBeTruthy();
    });

    const existingLabel = screen.getByText("claude-sonnet-4").closest("label");
    expect(existingLabel?.classList.contains("opacity-50")).toBe(true);
    expect(screen.getByText("Already added")).toBeTruthy();
  });

  it("filters models by search query", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(200, { models: mockModels })),
    );

    render(
      <FetchModelsModal
        open
        onClose={() => {}}
        providerKey="test"
        existingModelIds={new Set()}
        onImport={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("claude-sonnet-4")).toBeTruthy();
    });

    const searchInput = screen.getByPlaceholderText("Search models…");
    fireEvent.change(searchInput, { target: { value: "gpt" } });

    expect(screen.getByText("gpt-4o")).toBeTruthy();
    expect(screen.queryByText("claude-sonnet-4")).toBeNull();
  });

  it("calls onImport with selected models", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(200, { models: mockModels })),
    );
    const onImport = vi.fn();
    const onClose = vi.fn();

    render(
      <FetchModelsModal
        open
        onClose={onClose}
        providerKey="test"
        existingModelIds={new Set()}
        onImport={onImport}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("claude-sonnet-4")).toBeTruthy();
    });

    const checkbox = screen.getByText("claude-sonnet-4").closest("label")!
      .querySelector('input[type="checkbox"]')!;
    fireEvent.click(checkbox);

    const importBtn = screen.getByText(/Import selected/);
    fireEvent.click(importBtn);

    expect(onImport).toHaveBeenCalledWith([
      expect.objectContaining({ id: "claude-sonnet-4" }),
    ]);
    expect(onClose).toHaveBeenCalled();
  });

  it("shows error message and retry button on failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(401, { error: "auth failed" })),
    );

    render(
      <FetchModelsModal
        open
        onClose={() => {}}
        providerKey="test"
        existingModelIds={new Set()}
        onImport={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/Authentication failed/)).toBeTruthy();
    });
    expect(screen.getByText("Retry")).toBeTruthy();
  });

  it("shows error when provider returns 0 models", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(200, { models: [] })),
    );

    render(
      <FetchModelsModal
        open
        onClose={() => {}}
        providerKey="test"
        existingModelIds={new Set()}
        onImport={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Provider returned 0 models")).toBeTruthy();
    });
  });

  it("import button is disabled when nothing selected", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(200, { models: mockModels })),
    );

    render(
      <FetchModelsModal
        open
        onClose={() => {}}
        providerKey="test"
        existingModelIds={new Set()}
        onImport={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("claude-sonnet-4")).toBeTruthy();
    });

    const importBtn = screen.getByText(/Import selected/).closest("button")!;
    expect(importBtn.disabled).toBe(true);
  });
});
