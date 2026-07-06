import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MermaidBlock } from "./MermaidBlock";

// navigator.clipboard is a read-only getter in happy-dom; redefine it.
function stubClipboard(writeText: ReturnType<typeof vi.fn>): void {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
}

// mermaid cannot really render in happy-dom (it hangs on layout/bbox APIs the
// test DOM doesn't implement), so we mock the module and drive its behavior.
const { renderMock, initializeMock } = vi.hoisted(() => ({
  renderMock: vi.fn(),
  initializeMock: vi.fn(),
}));

vi.mock("mermaid", () => ({
  default: {
    initialize: initializeMock,
    render: renderMock,
  },
}));

const CHART = "graph TD\n  A-->B";

describe("MermaidBlock", () => {
  beforeEach(() => {
    renderMock.mockReset();
    initializeMock.mockReset();
  });

  it("renders the SVG on success and wires theme to 'default' in light mode", async () => {
    renderMock.mockResolvedValue({ svg: '<svg data-testid="diag"></svg>' });
    const { container } = render(<MermaidBlock chart={CHART} isDark={false} />);

    await waitFor(() =>
      expect(container.querySelector('[data-testid="diag"]')).not.toBeNull(),
    );
    expect(initializeMock).toHaveBeenCalledWith(
      expect.objectContaining({
        theme: "default",
        securityLevel: "strict",
        startOnLoad: false,
      }),
    );
    expect(renderMock).toHaveBeenCalledWith(expect.any(String), CHART);
    // Viewer controls present.
    expect(screen.getByText("Copy")).not.toBeNull();
    expect(screen.getByText("Source")).not.toBeNull();
    expect(screen.getByText("Zoom")).not.toBeNull();
  });

  it("uses the 'dark' theme when isDark", async () => {
    renderMock.mockResolvedValue({ svg: "<svg></svg>" });
    render(<MermaidBlock chart={CHART} isDark={true} />);
    await waitFor(() => expect(initializeMock).toHaveBeenCalled());
    expect(initializeMock).toHaveBeenCalledWith(
      expect.objectContaining({ theme: "dark" }),
    );
  });

  it("falls back to raw source + error note when render fails", async () => {
    renderMock.mockRejectedValue(new Error("parse boom"));
    const { container } = render(<MermaidBlock chart={CHART} isDark={false} />);

    await waitFor(() =>
      expect(screen.getByText(/Render failed/)).not.toBeNull(),
    );
    // Raw source preserved in a <pre> so content is never lost.
    expect(container.querySelector("pre")?.textContent ?? "").toContain(CHART);
    // No zoom/toggle for a failed render.
    expect(screen.queryByText("Zoom")).toBeNull();
    expect(screen.queryByText("Source")).toBeNull();
  });

  it("copies the raw chart source to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard(writeText);

    renderMock.mockResolvedValue({ svg: "<svg></svg>" });
    render(<MermaidBlock chart={CHART} isDark={false} />);
    await waitFor(() => expect(renderMock).toHaveBeenCalled());

    fireEvent.click(screen.getByText("Copy"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(CHART));
    await waitFor(() => expect(screen.getByText("Copied")).not.toBeNull());
  });

  it("toggles between diagram and source view", async () => {
    renderMock.mockResolvedValue({ svg: '<svg data-testid="diag"></svg>' });
    const { container } = render(<MermaidBlock chart={CHART} isDark={false} />);
    await waitFor(() =>
      expect(container.querySelector('[data-testid="diag"]')).not.toBeNull(),
    );
    expect(container.querySelector("pre")).toBeNull();

    fireEvent.click(screen.getByText("Source"));
    expect(container.querySelector("pre")?.textContent ?? "").toContain(CHART);
    expect(container.querySelector('[data-testid="diag"]')).toBeNull();

    fireEvent.click(screen.getByText("Diagram"));
    expect(container.querySelector('[data-testid="diag"]')).not.toBeNull();
  });

  it("opens and closes a fullscreen zoom overlay", async () => {
    renderMock.mockResolvedValue({ svg: '<svg data-testid="diag"></svg>' });
    const { container } = render(<MermaidBlock chart={CHART} isDark={false} />);
    await waitFor(() =>
      expect(container.querySelector('[data-testid="diag"]')).not.toBeNull(),
    );

    // No overlay yet.
    expect(container.querySelector('[role="dialog"]')).toBeNull();

    fireEvent.click(screen.getByText("Zoom"));
    const dialog = container.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    // Overlay also contains the SVG.
    expect(dialog?.querySelector('[data-testid="diag"]')).not.toBeNull();

    // Close button dismisses it.
    fireEvent.click(screen.getByText(/Close/));
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });

  it("zooms in/out via the toolbar buttons and resets", async () => {
    renderMock.mockResolvedValue({ svg: '<svg data-testid="diag"></svg>' });
    render(<MermaidBlock chart={CHART} isDark={false} />);
    await waitFor(() =>
      expect(screen.getByText("Zoom")).not.toBeNull(),
    );
    fireEvent.click(screen.getByText("Zoom"));

    // Default zoom reads as 100%.
    const pct = () => screen.getByText("100%");
    expect(pct()).not.toBeNull();

    // Each "+" multiplies by 1.1 → ~110%.
    const plus = screen.getByText("+");
    fireEvent.click(plus);
    await waitFor(() => expect(screen.getByText("110%")).not.toBeNull());

    // "−" divides → back near 100%.
    fireEvent.click(screen.getByText("−"));
    await waitFor(() => expect(screen.getByText("100%")).not.toBeNull());

    // Zoom in twice, then click the percentage to reset to 100%.
    fireEvent.click(plus);
    fireEvent.click(plus);
    await waitFor(() => expect(screen.getByText("121%")).not.toBeNull());
    fireEvent.click(screen.getByText("121%"));
    await waitFor(() => expect(screen.getByText("100%")).not.toBeNull());
  });

  it("zooms via mouse wheel over the overlay", async () => {
    renderMock.mockResolvedValue({ svg: '<svg data-testid="diag"></svg>' });
    const { container } = render(<MermaidBlock chart={CHART} isDark={false} />);
    await waitFor(() =>
      expect(container.querySelector('[data-testid="diag"]')).not.toBeNull(),
    );
    fireEvent.click(screen.getByText("Zoom"));
    const dialog = container.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();

    // Wheel up (negative deltaY) zooms in → 100% → 110%.
    fireEvent.wheel(dialog!.querySelector(".overflow-auto")!, { deltaY: -100 });
    await waitFor(() => expect(screen.getByText("110%")).not.toBeNull());
  });
});
