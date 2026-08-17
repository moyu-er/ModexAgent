import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { GraphStatusBadge, statusLabelKey } from "./shared";

describe("GraphStatusBadge (PRD §6.4 filled chip)", () => {
  it.each([
    ["pending", "graph-badge-pending"],
    ["running", "graph-badge-running"],
    ["paused", "graph-badge-suspended"],
    ["stopped", "graph-badge-canceled"],
    ["crashed", "graph-badge-crashed"],
    ["failed", "graph-badge-crashed"],
    ["completed", "graph-badge-completed"],
  ])("status %s renders the %s chip class", (status, cls) => {
    render(<GraphStatusBadge status={status} label={status} />);
    expect(screen.getByText(status).getAttribute("class")).toContain(cls);
  });

  it("unknown status falls back to the gray pending chip", () => {
    render(<GraphStatusBadge status="weird" label="weird" />);
    expect(screen.getByText("weird").getAttribute("class")).toContain(
      "graph-badge-pending",
    );
  });
});

describe("statusLabelKey", () => {
  it("maps known statuses and falls back to graphs.status", () => {
    expect(statusLabelKey("completed")).toBe("graphs.statusCompleted");
    expect(statusLabelKey("paused")).toBe("graphs.statusPaused");
    expect(statusLabelKey("unknown")).toBe("graphs.status");
  });
});
