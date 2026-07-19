import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DISPERSE_MS,
  bootParticleCap,
  mountBootParticles,
  reduceBootPhase,
  type BootPhase,
} from "./particles";

// ── Pure phase state machine ────────────────────────────────────────────────

describe("reduceBootPhase", () => {
  it("drift → gather when the logo shape is ready", () => {
    expect(reduceBootPhase("drift", "logoReady")).toBe("gather");
  });

  it("gather → disperse when the backend is ready", () => {
    expect(reduceBootPhase("gather", "backendReady")).toBe("disperse");
  });

  it("drift → disperse when the backend is ready before the logo loaded (fallback)", () => {
    expect(reduceBootPhase("drift", "backendReady")).toBe("disperse");
  });

  it("disperse → done once the disperse window elapses", () => {
    expect(reduceBootPhase("disperse", "disperseElapsed")).toBe("done");
  });

  it("ignores events that do not match the current phase", () => {
    expect(reduceBootPhase("gather", "logoReady")).toBe("gather");
    expect(reduceBootPhase("drift", "disperseElapsed")).toBe("drift");
    expect(reduceBootPhase("done", "backendReady")).toBe("done");
    expect(reduceBootPhase("done", "disperseElapsed")).toBe("done");
  });
});

describe("bootParticleCap", () => {
  it("caps density at 600 on desktop widths", () => {
    expect(bootParticleCap(1280)).toBe(600);
  });

  it("caps density at 300 on mobile widths", () => {
    expect(bootParticleCap(390)).toBe(300);
  });

  it("treats 700px as the desktop boundary", () => {
    expect(bootParticleCap(700)).toBe(600);
    expect(bootParticleCap(699)).toBe(300);
  });
});

// ── Canvas engine (mocked 2D context — no pixel assertions) ────────────────

interface CtxStub {
  fillStyle: string;
  fillRectCalls: number;
  arcCalls: number;
  setTransform: () => void;
  clearRect: () => void;
  beginPath: () => void;
  arc: () => void;
  fill: () => void;
  fillRect: () => void;
  drawImage: () => void;
  getImageData: (
    x: number,
    y: number,
    w: number,
    h: number,
  ) => { data: Uint8ClampedArray };
}

function makeCtxStub(opaque: boolean): CtxStub {
  const stub: CtxStub = {
    fillStyle: "",
    fillRectCalls: 0,
    arcCalls: 0,
    setTransform: () => {},
    clearRect: () => {},
    beginPath: () => {},
    arc: () => {
      stub.arcCalls += 1;
    },
    fill: () => {},
    fillRect: () => {
      stub.fillRectCalls += 1;
    },
    drawImage: () => {},
    getImageData: (_x, _y, w, h) => ({
      // Every pixel opaque → sampling finds points everywhere, far beyond
      // any density cap. Alpha 0 when `opaque` is false (logo "empty").
      data: new Uint8ClampedArray(w * h * 4).fill(opaque ? 255 : 0),
    }),
  };
  return stub;
}

function makeCanvasStub(
  w: number,
  h: number,
  ctx: CtxStub,
  listeners?: string[],
): HTMLCanvasElement {
  return {
    width: 0,
    height: 0,
    clientWidth: w,
    clientHeight: h,
    getContext: () => ctx,
    addEventListener: (type: string) => {
      listeners?.push(type);
    },
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ left: 0, top: 0 }),
  } as unknown as HTMLCanvasElement;
}

class FakeImage {
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  set src(_v: string) {
    queueMicrotask(() => this.onload?.());
  }
}

let rafPending: Array<{ id: number; cb: FrameRequestCallback }>;
let rafSeq: number;

function stepFrames(now: number): void {
  const cbs = rafPending;
  rafPending = [];
  for (const r of cbs) r.cb(now);
}

describe("mountBootParticles", () => {
  beforeEach(() => {
    rafPending = [];
    rafSeq = 0;
    vi.stubGlobal("Image", FakeImage);
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      const id = ++rafSeq;
      rafPending.push({ id, cb });
      return id;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => {
      rafPending = rafPending.filter((r) => r.id !== id);
    });
    // The engine samples the logo via an offscreen canvas created through
    // document.createElement — route those to stubs (happy-dom canvases
    // have no 2D context).
    const realCreate = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation(((
      tag: string,
    ): HTMLElement =>
      tag === "canvas"
        ? (makeCanvasStub(800, 600, makeCtxStub(true)) as unknown as HTMLElement)
        : realCreate(tag as "div")) as typeof document.createElement);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("runs drift → gather → disperse → done", async () => {
    const phases: BootPhase[] = [];
    const handle = mountBootParticles(
      makeCanvasStub(800, 600, makeCtxStub(false)),
      { coarsePointer: true, onPhaseChange: (p) => phases.push(p) },
    );
    expect(handle.phase).toBe("drift");

    await vi.waitFor(() => expect(handle.phase).toBe("gather"));

    handle.setReady();
    expect(handle.phase).toBe("disperse");

    const now = performance.now();
    stepFrames(now + 16.7);
    expect(handle.phase).toBe("disperse");

    stepFrames(now + DISPERSE_MS + 50);
    expect(handle.phase).toBe("done");
    // The loop stops for good once the disperse window elapses.
    expect(rafPending).toHaveLength(0);
    expect(phases).toEqual(["gather", "disperse", "done"]);
    handle.destroy();
  });

  it("caps particle density at 600 on desktop", async () => {
    const handle = mountBootParticles(
      makeCanvasStub(800, 600, makeCtxStub(false)),
      { coarsePointer: true },
    );
    expect(handle.particleCount).toBeLessThanOrEqual(600);
    await vi.waitFor(() => expect(handle.phase).toBe("gather"));
    // Sampling found ~30k opaque points; the gather shape must be capped.
    expect(handle.particleCount).toBe(600);
    handle.destroy();
  });

  it("caps particle density at 300 on mobile widths", () => {
    const handle = mountBootParticles(
      makeCanvasStub(360, 640, makeCtxStub(false)),
      { coarsePointer: true },
    );
    expect(handle.particleCount).toBeLessThanOrEqual(300);
    handle.destroy();
  });

  it("reduced motion draws one static logo frame and never starts the rAF loop", async () => {
    const ctx = makeCtxStub(false);
    const handle = mountBootParticles(makeCanvasStub(800, 600, ctx), {
      reducedMotion: true,
      coarsePointer: true,
    });
    expect(rafPending).toHaveLength(0);
    await vi.waitFor(() => expect(ctx.fillRectCalls).toBeGreaterThan(0));
    // Static branch: fillRect dots, no animation loop even after frames pass.
    expect(ctx.arcCalls).toBe(0);
    expect(rafPending).toHaveLength(0);
    handle.destroy();
  });

  it("binds pointer repel on fine pointers but not on touch (coarse)", () => {
    const fineListeners: string[] = [];
    const fine = mountBootParticles(
      makeCanvasStub(800, 600, makeCtxStub(false), fineListeners),
      { coarsePointer: false },
    );
    expect(fineListeners).toContain("pointermove");
    fine.destroy();

    const coarseListeners: string[] = [];
    const coarse = mountBootParticles(
      makeCanvasStub(800, 600, makeCtxStub(false), coarseListeners),
      { coarsePointer: true },
    );
    expect(coarseListeners).not.toContain("pointermove");
    coarse.destroy();
  });

  it("destroy() stops the loop and detaches listeners", () => {
    const handle = mountBootParticles(
      makeCanvasStub(800, 600, makeCtxStub(false)),
      { coarsePointer: true },
    );
    expect(rafPending.length).toBeGreaterThan(0);
    handle.destroy();
    expect(rafPending).toHaveLength(0);
  });
});
