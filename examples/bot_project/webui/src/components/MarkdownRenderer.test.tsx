import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MarkdownRenderer } from "./MarkdownRenderer";

// Stub MermaidBlock so MarkdownRenderer tests stay focused on routing logic and
// don't pull mermaid's heavy (happy-dom-incompatible) render path into this suite.
vi.mock("./MermaidBlock", () => ({
  MermaidBlock: ({ chart }: { chart: string; isDark: boolean }) => (
    <div data-testid="mermaid-diagram">{chart}</div>
  ),
}));

/**
 * Regression: fenced code blocks WITHOUT a language tag (ASCII art,
 * box-drawing diagrams, etc.) used to collapse into inline <code> because
 * react-markdown v9+ no longer passes an `inline` prop and the old guard
 * required a `language-xxx` match. They must now render as block <pre>.
 */
describe("MarkdownRenderer code block routing", () => {
  it("renders a language-less fenced block as block <pre>, preserving the frame", () => {
    const content = "```\n┌─────┐\n│ box │\n└─────┘\n```";
    const { container } = render(<MarkdownRenderer content={content} />);

    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    // Box-drawing frame must survive intact, not collapse onto one line.
    expect(pre?.textContent ?? "").toContain("┌─────┐");
    expect(pre?.textContent ?? "").toContain("│ box │");
    expect(pre?.textContent ?? "").toContain("└─────┘");
    // Not routed to the mermaid viewer.
    expect(screen.queryByTestId("mermaid-diagram")).toBeNull();
  });

  it("renders a multi-line indented block (no language) as <pre> too", () => {
    const content = "```\nSTART → LLM → TOOL → END\n         ↑      |\n         └──────┘\n```";
    const { container } = render(<MarkdownRenderer content={content} />);
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent ?? "").toContain("START → LLM → TOOL → END");
    expect(pre?.textContent ?? "").toContain("└──────┘");
  });

  it("renders a fenced block with a language via the code-block path (has Copy)", () => {
    const content = "```python\nprint('hi')\n```";
    render(<MarkdownRenderer content={content} />);
    expect(screen.getByText("python")).not.toBeNull();
    expect(screen.getByText("Copy")).not.toBeNull();
  });

  it("renders inline code as inline <code>, not as a block", () => {
    const content = "use the `foo` value";
    const { container } = render(<MarkdownRenderer content={content} />);

    expect(container.querySelector("pre")).toBeNull();
    expect(screen.queryByText("Copy")).toBeNull();
    expect(screen.queryByTestId("mermaid-diagram")).toBeNull();
    expect(screen.getByText("foo").tagName).toBe("CODE");
  });

  it("routes a mermaid block to the MermaidBlock viewer (not the generic code block)", () => {
    const content = "```mermaid\ngraph TD\n  A-->B\n```";
    render(<MarkdownRenderer content={content} />);

    const diag = screen.getByTestId("mermaid-diagram");
    expect(diag.textContent).toContain("graph TD");
    expect(diag.textContent).toContain("A-->B");
    // Generic code-block copy button is absent.
    expect(screen.queryByText("Copy")).toBeNull();
  });
});
