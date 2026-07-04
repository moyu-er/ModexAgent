import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor, act } from "@testing-library/react";
import { useState } from "react";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { MermaidBlock } from "./MermaidBlock";

// Mock the mermaid module so we can count how many times render() is invoked.
// renderMock call count is the red-capable signal: if an ancestor re-render
// causes MermaidBlock to re-run its render effect, renderMock is called again.
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

const FENCE = "```mermaid\ngraph TD\n  A-->B\n```";
const CHART = "graph TD\n  A-->B";

/**
 * Regression: rendering a mermaid block, then re-rendering an ancestor for an
 * unrelated reason (e.g. the user typing in the ChatView input box), used to
 * REMOUNT MermaidBlock — resetting its state and re-running mermaid.render(),
 * so the diagram visibly disappeared and re-rendered on every keystroke.
 *
 * Root cause: MarkdownRenderer passed react-markdown a fresh inline
 * `components` object (with fresh `code`/`pre` function identities) on every
 * render. react-markdown renders each node via
 * `React.createElement(components[code], …)`, so a new function reference meant
 * a new element TYPE → React remounted the code-block subtree. Fix: stabilise
 * the components map with useMemo (and hoist remarkPlugins to module scope).
 */
describe("MermaidBlock render stability across ancestor re-renders", () => {
  beforeEach(() => {
    // Shared module-level mock — clear call counts between tests so the
    // render-call assertions are independent.
    renderMock.mockReset();
    initializeMock.mockReset();
  });

  it("renders once via MarkdownRenderer and does NOT re-render on ancestor update", async () => {
    renderMock.mockResolvedValue({ svg: '<svg data-testid="diag"></svg>' });

    let bump: () => void = (): void => {};
    const Wrapper = (): JSX.Element => {
      const [, setN] = useState(0);
      bump = (): void => setN((n) => n + 1);
      return <MarkdownRenderer content={FENCE} />;
    };

    render(<Wrapper />);
    await waitFor(() => expect(renderMock).toHaveBeenCalledTimes(1));

    // Simulate the production trigger: ChatView re-renders on each input
    // keystroke, which re-renders MarkdownRenderer for every message.
    act(() => bump());
    act(() => bump());
    await act(async () => {
      await Promise.resolve();
    });

    expect(renderMock).toHaveBeenCalledTimes(1);
  });

  it("direct MermaidBlock mount does not re-render on ancestor update (effect deps stable)", async () => {
    // Belt-and-suspenders: confirms MermaidBlock's own [chart, isDark, renderId]
    // effect deps are stable, so any future re-render storm must come from a
    // remount higher up the tree — not from this component.
    renderMock.mockResolvedValue({ svg: '<svg data-testid="diag"></svg>' });

    let bump: () => void = (): void => {};
    const Wrapper = (): JSX.Element => {
      const [, setN] = useState(0);
      bump = (): void => setN((n) => n + 1);
      return <MermaidBlock chart={CHART} isDark={false} />;
    };

    render(<Wrapper />);
    await waitFor(() => expect(renderMock).toHaveBeenCalledTimes(1));

    act(() => bump());
    await act(async () => {
      await Promise.resolve();
    });

    expect(renderMock).toHaveBeenCalledTimes(1);
  });
});
