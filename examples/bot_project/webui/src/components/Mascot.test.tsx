// Mascot animation: static frame under reduced-motion / animated=false
// (no timers), and a deterministic choreographed loop (round steps + short
// inter-round pause, burst every third round) in animated mode. Matches the
// matchMedia stub style of DeliverPulse.test.tsx (happy-dom lacks matchMedia).

import { describe, it, expect, vi, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { Mascot } from "./Mascot";

const MASCOT_LABEL = "ModexBot mascot";

const stubMatchMedia = (reduced: boolean): void => {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("prefers-reduced-motion") ? reduced : false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      onchange: null,
      dispatchEvent: vi.fn(),
    })),
  );
};

const frameSrc = (): string =>
  screen.getByRole("img", { name: MASCOT_LABEL }).querySelector("img")!
    .getAttribute("src")!;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("Mascot", () => {
  it("renders the static idle frame with an accessible label", () => {
    render(<Mascot size={180} animated={false} />);
    expect(screen.getByRole("img", { name: MASCOT_LABEL })).toBeTruthy();
    expect(frameSrc()).toContain("mascot/f1.png");
  });

  it("stays on the idle frame under prefers-reduced-motion (no timers run)", () => {
    stubMatchMedia(true);
    vi.useFakeTimers();
    render(<Mascot size={180} animated />);
    // Far beyond any idle pause — a running loop would have swapped frames.
    act(() => {
      vi.advanceTimersByTime(30000);
    });
    expect(frameSrc()).toContain("mascot/f1.png");
  });

  it("plays the choreographed round on a timer", () => {
    stubMatchMedia(false);
    vi.useFakeTimers();
    render(<Mascot size={180} animated />);
    expect(frameSrc()).toContain("mascot/f1.png");
    act(() => {
      vi.advanceTimersByTime(500); // settle → ear flick
    });
    expect(frameSrc()).toContain("mascot/f2.png");
    act(() => {
      vi.advanceTimersByTime(500); // ear flick → idle
    });
    expect(frameSrc()).toContain("mascot/f1.png");
    act(() => {
      vi.advanceTimersByTime(400); // idle → wink
    });
    expect(frameSrc()).toContain("mascot/f3.png");
  });

  it("loops rounds and fires the burst every third round", () => {
    stubMatchMedia(false);
    vi.useFakeTimers();
    render(<Mascot size={180} animated />);
    // Round length: 3400ms of steps + 1400ms pause = 4800ms.
    act(() => {
      vi.advanceTimersByTime(4800 + 500); // round 1 + pause → round 2 ear flick
    });
    expect(frameSrc()).toContain("mascot/f2.png");
    // Round 3 carries the burst: f7 right after the base steps. From t=5300:
    // round 2 ends at 8200, pause to 9600, round 3 base takes 3400ms →
    // f7 starts at t=13000.
    act(() => {
      vi.advanceTimersByTime(7700);
    });
    expect(frameSrc()).toContain("mascot/f7.png");
  });
});
