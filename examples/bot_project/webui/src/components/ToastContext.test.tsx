import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { ToastProvider, useToast } from "./ToastContext";

// A test harness that exposes the toast API via a ref so we can call show()
// imperatively from a test without leaking provider internals.
let lastShow: ((opts: { message: string; action?: { label: string; onClick: () => void } }) => void) | null = null;
function Harness() {
  const { show } = useToast();
  lastShow = show;
  return <div>harness</div>;
}

function renderProvider(): void {
  render(
    <ToastProvider>
      <Harness />
    </ToastProvider>,
  );
}

beforeEach(() => {
  lastShow = null;
});

afterEach(() => {
  vi.useRealTimers();
  lastShow = null;
});

describe("ToastContext", () => {
  it("show({message}) renders the toast", () => {
    renderProvider();
    act(() => lastShow!({ message: "Hello" }));
    expect(screen.getByText("Hello")).toBeTruthy();
  });

  it("action click fires callback and dismisses the toast", () => {
    renderProvider();
    const onClick = vi.fn();
    act(() => lastShow!({ message: "Saved", action: { label: "Undo", onClick } }));
    fireEvent.click(screen.getByText("Undo"));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Saved")).toBeNull();
  });

  it("auto-dismisses after the timeout when no action is present", () => {
    vi.useFakeTimers();
    renderProvider();
    act(() => lastShow!({ message: "Boo" }));
    expect(screen.queryByText("Boo")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(screen.queryByText("Boo")).toBeNull();
  });

  it("actionable toasts do NOT auto-dismiss (stay until clicked)", () => {
    vi.useFakeTimers();
    renderProvider();
    const onClick = vi.fn();
    act(() =>
      lastShow!({ message: "Decide", action: { label: "Go", onClick } }),
    );
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(screen.queryByText("Decide")).toBeTruthy();
  });

  it("dismiss button removes the toast", () => {
    renderProvider();
    act(() => lastShow!({ message: "Bye" }));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Bye")).toBeNull();
  });

  it("useToast throws when used outside the provider", () => {
    // Suppress the expected console.error from React.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Harness />)).toThrow(
      /useToast must be used within a ToastProvider/,
    );
    spy.mockRestore();
  });
});
