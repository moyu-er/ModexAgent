import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  DeliverPulse,
  pathLength,
  interpolatePathPoint,
  trailDashOffset,
  easeOut,
  prefersReducedMotion,
  DELIVER_PULSE_RADIUS,
  DELIVER_TRAIL_LENGTH,
  DELIVER_DURATION_MS,
  DELIVER_FALLBACK_MS,
} from "./DeliverPulse";
import type { LayoutPoint } from "./layout";

// ── 测试用路径数据 ─────────────────────────────────────────────

const TWO_POINTS: LayoutPoint[] = [
  { x: 0, y: 0 },
  { x: 100, y: 0 },
];

const THREE_POINTS: LayoutPoint[] = [
  { x: 0, y: 0 },
  { x: 50, y: 0 },
  { x: 50, y: 100 },
];

const ZIGZAG: LayoutPoint[] = [
  { x: 0, y: 0 },
  { x: 30, y: 40 },
  { x: 60, y: 0 },
];

// ── matchMedia mock helper ─────────────────────────────────────

function mockMatchMedia(reduce: boolean): void {
  const matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: reduce && query.includes("reduce"),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  vi.stubGlobal("matchMedia", matchMedia);
}

function renderPulse(
  props: Partial<Parameters<typeof DeliverPulse>[0]> = {},
) {
  return render(
    <svg>
      <DeliverPulse points={TWO_POINTS} {...props} />
    </svg>,
  );
}

/** Bridge requestAnimationFrame → setTimeout so vitest fake timers can drive it. */
function mockRaf(): void {
  let frame = 0;
  vi.stubGlobal("requestAnimationFrame", (cb: (t: number) => void) => {
    frame += 16;
    return setTimeout(() => cb(frame), 0) as unknown as number;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => {
    clearTimeout(id);
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ── 纯函数测试 ─────────────────────────────────────────────────

describe("pathLength", () => {
  it("returns 0 for empty or single-point arrays", () => {
    expect(pathLength([])).toBe(0);
    expect(pathLength([{ x: 5, y: 5 }])).toBe(0);
  });

  it("computes the total length of a straight segment", () => {
    expect(pathLength(TWO_POINTS)).toBe(100);
  });

  it("sums multiple segment lengths", () => {
    expect(pathLength(THREE_POINTS)).toBe(150); // 50 + 100
  });

  it("handles diagonal segments via hypot", () => {
    expect(pathLength(ZIGZAG)).toBe(100); // 50 + 50
  });
});

describe("easeOut", () => {
  it("returns 0 at t=0 and 1 at t=1", () => {
    expect(easeOut(0)).toBe(0);
    expect(easeOut(1)).toBe(1);
  });

  it("produces a decelerating curve (ease-out)", () => {
    // At t=0.5, ease-out(0.5) = 1-(0.5)² = 0.75
    expect(easeOut(0.5)).toBeCloseTo(0.75);
    // Curve is above linear: easeOut(0.25) > 0.25
    expect(easeOut(0.25)).toBeGreaterThan(0.25);
  });
});

describe("interpolatePathPoint", () => {
  it("returns the first point at t=0", () => {
    expect(interpolatePathPoint(TWO_POINTS, 0)).toEqual({ x: 0, y: 0 });
  });

  it("returns the last point at t=1", () => {
    expect(interpolatePathPoint(TWO_POINTS, 1)).toEqual({ x: 100, y: 0 });
  });

  it("interpolates the midpoint correctly", () => {
    expect(interpolatePathPoint(TWO_POINTS, 0.5)).toEqual({ x: 50, y: 0 });
  });

  it("handles multi-segment paths at segment boundaries", () => {
    // THREE_POINTS: [0,0]→[50,0]→[50,100], total=150
    // t=1/3 → end of first segment (50,0)
    expect(interpolatePathPoint(THREE_POINTS, 1 / 3)).toEqual({ x: 50, y: 0 });
    // t=2/3 → midpoint of second segment (50,50)
    expect(interpolatePathPoint(THREE_POINTS, 2 / 3)).toEqual({ x: 50, y: 50 });
  });

  it("clamps t outside [0, 1]", () => {
    expect(interpolatePathPoint(TWO_POINTS, -0.5)).toEqual({ x: 0, y: 0 });
    expect(interpolatePathPoint(TWO_POINTS, 1.5)).toEqual({ x: 100, y: 0 });
  });

  it("returns origin for empty array and the point for single-element array", () => {
    expect(interpolatePathPoint([], 0.5)).toEqual({ x: 0, y: 0 });
    expect(interpolatePathPoint([{ x: 7, y: 8 }], 0.5)).toEqual({ x: 7, y: 8 });
  });

  it("handles zero-length segments without division by zero", () => {
    const dup: LayoutPoint[] = [
      { x: 10, y: 10 },
      { x: 10, y: 10 },
      { x: 20, y: 10 },
    ];
    expect(interpolatePathPoint(dup, 0.5)).toEqual({ x: 15, y: 10 });
  });
});

describe("trailDashOffset", () => {
  it("places the trail before the path start at t=0 (invisible)", () => {
    const total = 100;
    const offset = trailDashOffset(total, DELIVER_TRAIL_LENGTH, 0);
    // dashoffset = trailLength - 0 = 24 → dash at [-24, 0], before start
    expect(offset).toBe(DELIVER_TRAIL_LENGTH);
  });

  it("places the trail at the end at t=1", () => {
    const total = 100;
    const offset = trailDashOffset(total, DELIVER_TRAIL_LENGTH, 1);
    // dashoffset = 24 - 100 = -76 → dash at [76, 100], at end
    expect(offset).toBe(DELIVER_TRAIL_LENGTH - total);
  });

  it("places the trail behind the dot at t=0.5", () => {
    const total = 100;
    const offset = trailDashOffset(total, DELIVER_TRAIL_LENGTH, 0.5);
    // dotPos = 50, offset = 24 - 50 = -26 → dash at [26, 50]
    expect(offset).toBe(DELIVER_TRAIL_LENGTH - 50);
  });

  it("clamps t outside [0, 1]", () => {
    const total = 100;
    expect(trailDashOffset(total, DELIVER_TRAIL_LENGTH, -1)).toBe(
      DELIVER_TRAIL_LENGTH,
    );
    expect(trailDashOffset(total, DELIVER_TRAIL_LENGTH, 2)).toBe(
      DELIVER_TRAIL_LENGTH - total,
    );
  });
});

// ── 组件渲染测试 ───────────────────────────────────────────────

describe("DeliverPulse component", () => {
  beforeEach(() => {
    // happy-dom 默认无 matchMedia → prefersReducedMotion() 返回 false
    mockMatchMedia(false);
  });

  it("renders a dot (circle) and a trail (path) in normal mode", () => {
    const { container } = renderPulse();
    const g = screen.getByTestId("deliver-pulse");
    expect(g).toBeTruthy();

    const circle = container.querySelector("circle");
    expect(circle).toBeTruthy();
    expect(circle!.getAttribute("r")).toBe(String(DELIVER_PULSE_RADIUS));
    expect(circle!.getAttribute("fill")).toBe("var(--color-graph-deliver)");
    // glow drop-shadow filter on inline style
    expect(circle!.style.filter).toContain("drop-shadow");
    expect(circle!.style.filter).toContain("var(--color-graph-deliver-glow)");

    const trailPath = container.querySelector(
      '[data-testid="deliver-pulse"] path',
    );
    expect(trailPath).toBeTruthy();
    expect(trailPath!.getAttribute("stroke")).toBe(
      "var(--color-graph-deliver-trail)",
    );
    // dasharray = "24 [totalLen]"
    expect(trailPath!.getAttribute("stroke-dasharray")).toContain(
      String(DELIVER_TRAIL_LENGTH),
    );
  });

  it("sets the dot's initial position to the first path point", () => {
    const { container } = renderPulse({ points: THREE_POINTS });
    const circle = container.querySelector("circle")!;
    expect(circle.getAttribute("cx")).toBe("0");
    expect(circle.getAttribute("cy")).toBe("0");
  });

  it("returns null for degenerate input (< 2 points) in normal mode", () => {
    const { container } = render(
      <svg>
        <DeliverPulse points={[{ x: 5, y: 5 }]} />
      </svg>,
    );
    expect(
      container.querySelector('[data-testid="deliver-pulse"]'),
    ).toBeNull();
    expect(
      container.querySelector('[data-testid="deliver-pulse-fallback"]'),
    ).toBeNull();
  });

  describe("onComplete (mock timers)", () => {
    beforeEach(() => {
      vi.useFakeTimers();
      mockRaf();
    });

    it("calls onComplete after ~600ms in normal mode", () => {
      const onComplete = vi.fn();
      renderPulse({ onComplete });

      // rAF callbacks fire via fake timers; advance past 600ms
      vi.advanceTimersByTime(DELIVER_DURATION_MS + 50);

      expect(onComplete).toHaveBeenCalledTimes(1);
    });

    it("calls onComplete immediately for degenerate input", () => {
      const onComplete = vi.fn();
      render(
        <svg>
          <DeliverPulse points={[]} onComplete={onComplete} />
        </svg>,
      );
      // Degenerate input calls onComplete synchronously in the effect
      vi.advanceTimersByTime(0);
      expect(onComplete).toHaveBeenCalledTimes(1);
    });
  });

  describe("reduced-motion fallback", () => {
    beforeEach(() => {
      mockMatchMedia(true);
    });

    it("renders a static edge highlight path instead of dot/trail", () => {
      const { container } = renderPulse();
      const fallback = screen.getByTestId("deliver-pulse-fallback");
      expect(fallback).toBeTruthy();
      expect(fallback.getAttribute("class")).toContain(
        "stroke-graph-edge-active",
      );

      // No dot or trail in reduced-motion mode
      expect(container.querySelector("circle")).toBeNull();
      expect(
        container.querySelector('[data-testid="deliver-pulse"]'),
      ).toBeNull();
    });

    it("calls onComplete after ~220ms (fallback duration)", () => {
      vi.useFakeTimers();
      const onComplete = vi.fn();
      renderPulse({ onComplete });

      // Before 220ms: not called
      vi.advanceTimersByTime(DELIVER_FALLBACK_MS - 1);
      expect(onComplete).not.toHaveBeenCalled();

      // At 220ms: called
      vi.advanceTimersByTime(1);
      expect(onComplete).toHaveBeenCalledTimes(1);
    });
  });

  describe("prefersReducedMotion", () => {
    it("returns false when matchMedia is unavailable", () => {
      vi.unstubAllGlobals();
      expect(prefersReducedMotion()).toBe(false);
    });

    it("returns true when matchMedia reports prefers-reduced-motion: reduce", () => {
      mockMatchMedia(true);
      expect(prefersReducedMotion()).toBe(true);
    });

    it("returns false when matchMedia reports no preference", () => {
      mockMatchMedia(false);
      expect(prefersReducedMotion()).toBe(false);
    });
  });
});
