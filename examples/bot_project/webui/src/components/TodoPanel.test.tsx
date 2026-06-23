import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { TodoPanel } from "./TodoPanel";

describe("TodoPanel", () => {
  it("renders nothing when there are no todos", () => {
    const { container } = render(<TodoPanel todos={[]} sessionId="s1" />);
    expect(container.firstChild).toBeNull();
  });

  it("renders the collapsed pill when todos exist", () => {
    render(
      <TodoPanel
        todos={[{ content: "task one", status: "pending" }]}
        sessionId="s1"
      />,
    );
    expect(screen.getByLabelText("Toggle task list")).toBeTruthy();
  });

  it("does not crash when todos go from empty to non-empty", () => {
    const { rerender } = render(<TodoPanel todos={[]} sessionId="s1" />);
    rerender(
      <TodoPanel
        todos={[{ content: "task one", status: "pending" }]}
        sessionId="s1"
      />,
    );
    expect(screen.getByLabelText("Toggle task list")).toBeTruthy();
  });
});
