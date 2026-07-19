/* Boot particle-morph engine — vanilla, zero dependencies.
   Simplified port of the website's docs/javascripts/particles.js for the
   boot screen (DESIGN.md §7): ONE shape (the logo mark), phases
   drift → gather → disperse → done. No shape cycling, no text sampling.
   The phase state machine (reduceBootPhase) and the density cap
   (bootParticleCap) are pure exports; all canvas drawing lives in
   mountBootParticles. */

export type BootPhase = "drift" | "gather" | "disperse" | "done";
export type BootPhaseEvent = "logoReady" | "backendReady" | "disperseElapsed";

/** How long the radial disperse plays before the engine stops (~600–800ms). */
export const DISPERSE_MS = 800;

export function reduceBootPhase(phase: BootPhase, event: BootPhaseEvent): BootPhase {
  switch (event) {
    case "logoReady":
      return phase === "drift" ? "gather" : phase;
    case "backendReady":
      return phase === "drift" || phase === "gather" ? "disperse" : phase;
    case "disperseElapsed":
      return phase === "disperse" ? "done" : phase;
  }
}

/** Particle budget: boot is transient — keep it cheap (DESIGN.md §7). */
export function bootParticleCap(width: number): number {
  return width < 700 ? 300 : 600;
}

const TAU = Math.PI * 2;
const DPR_CAP = 2;
const GATHER_SPRING = 0.045;
const GATHER_DAMPING = 0.86;
const DRIFT_SPRING = 0.008;
const DRIFT_DAMPING = 0.98;
const DISPERSE_DAMPING = 0.965;
const REPEL_RADIUS = 110;
const REPEL_FORCE = 2.6;
const EMBER_RATIO = 0.15;
const MIN_SHAPE_POINTS = 30;
/** Logo center, as a fraction of stage height (status copy sits below). */
const LOGO_CY = 0.4;

type RGB = [number, number, number];

interface Palette {
  main: string[];
  ember: string[];
  glow: boolean;
}

/* DESIGN.md §7: firefly teal + ember in dark (matches the dark brand ladder
   --color-brand #2DD4BF / --color-brand-bright #5EEAD4); deepened variants
   and NO glow in light. */
const PALETTES: Record<"dark" | "light", Palette> = {
  dark: {
    main: ["#5EEAD4", "#2DD4BF", "#99F6E4"],
    ember: ["#FBBF24", "#F59E0B"],
    glow: true,
  },
  light: {
    main: ["#0D9488", "#0F766E", "#14B8A6"],
    ember: ["#B45309", "#D97706"],
    glow: false,
  },
};

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  r: number;
  tx: number;
  ty: number;
  seed: number;
  ember: boolean;
  color: RGB;
}

interface ShapePoint {
  x: number;
  y: number;
}

function hexRgb(h: string): RGB {
  return [
    parseInt(h.slice(1, 3), 16),
    parseInt(h.slice(3, 5), 16),
    parseInt(h.slice(5, 7), 16),
  ];
}

function gradColor(stops: string[], t: number): RGB {
  const n = stops.length - 1;
  const x = t * n;
  const i = Math.min(Math.floor(x), n - 1);
  const f = x - i;
  const a = hexRgb(stops[i]!);
  const b = hexRgb(stops[i + 1]!);
  return [
    (a[0] + (b[0] - a[0]) * f) | 0,
    (a[1] + (b[1] - a[1]) * f) | 0,
    (a[2] + (b[2] - a[2]) * f) | 0,
  ];
}

export interface BootParticlesOptions {
  /** Logo image URL; defaults to the public asset copied from assets/. */
  logoSrc?: string;
  reducedMotion?: boolean;
  coarsePointer?: boolean;
  onPhaseChange?: (phase: BootPhase) => void;
}

export interface BootParticlesHandle {
  /** Backend is ready: play the radial disperse, then stop. */
  setReady: () => void;
  destroy: () => void;
  readonly phase: BootPhase;
  readonly particleCount: number;
}

export function mountBootParticles(
  canvas: HTMLCanvasElement,
  options: BootParticlesOptions = {},
): BootParticlesHandle {
  const maybeCtx = canvas.getContext("2d");
  if (!maybeCtx) {
    // Canvas unsupported: degrade to a no-op — the DOM status copy below
    // the stage still communicates boot progress.
    let phase: BootPhase = "drift";
    return {
      setReady: () => {
        phase = "done";
      },
      destroy: () => {},
      get phase() {
        return phase;
      },
      get particleCount() {
        return 0;
      },
    };
  }
  const ctx = maybeCtx;

  const reduced =
    options.reducedMotion ??
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const coarse =
    options.coarsePointer ?? window.matchMedia("(pointer: coarse)").matches;
  const logoSrc = options.logoSrc ?? "/logo-icon.svg";
  const emit = options.onPhaseChange;

  let W = 0;
  let H = 0;
  let DPR = 1;
  let particles: Particle[] = [];
  let shapePoints: ShapePoint[] | null = null;
  let phase: BootPhase = "drift";
  let disperseStart = 0;
  let raf: number | null = null;
  let last = 0;
  let destroyed = false;
  const mouse = { x: -9999, y: -9999 };

  const theme = (): "dark" | "light" =>
    document.documentElement.classList.contains("dark") ? "dark" : "light";

  function fire(event: BootPhaseEvent, now: number = performance.now()): void {
    const next = reduceBootPhase(phase, event);
    if (next === phase) return;
    if (next === "disperse") {
      disperseStart = now;
      explode();
    }
    phase = next;
    emit?.(phase);
    if (phase === "done") stopLoop();
  }

  function newParticle(): Particle {
    return {
      x: Math.random() * W,
      y: Math.random() * H,
      vx: 0,
      vy: 0,
      r: 1.1 + Math.random() * 1.7,
      tx: Math.random() * W,
      ty: Math.random() * H,
      seed: Math.random() * 1000,
      ember: Math.random() < EMBER_RATIO,
      color: [255, 255, 255],
    };
  }

  function recolor(): void {
    const pal = PALETTES[theme()];
    const n = particles.length;
    for (let k = 0; k < n; k++) {
      const p = particles[k]!;
      p.color = p.ember
        ? hexRgb(pal.ember[k % pal.ember.length]!)
        : gradColor(pal.main, n > 1 ? k / (n - 1) : 0);
    }
  }

  /* Rasterize the SVG into an offscreen canvas and sample opaque pixels. */
  function sampleLogo(img: HTMLImageElement): ShapePoint[] | null {
    const off = document.createElement("canvas");
    off.width = W;
    off.height = H;
    const o = off.getContext("2d", { willReadFrequently: true });
    if (!o) return null;
    const s = Math.min(W, H) * 0.42;
    o.drawImage(img, (W - s) / 2, H * LOGO_CY - s / 2, s, s);
    const data = o.getImageData(0, 0, W, H).data;
    const gap = Math.max(3, Math.round(Math.min(W, H) / 160));
    const pts: ShapePoint[] = [];
    for (let y = 0; y < H; y += gap) {
      for (let x = 0; x < W; x += gap) {
        if (data[(y * W + x) * 4 + 3]! > 128) pts.push({ x, y });
      }
    }
    return pts.length >= MIN_SHAPE_POINTS ? pts : null;
  }

  /* Assign gather targets from the sampled logo, capped by density. */
  function applyShape(): void {
    if (!shapePoints) return;
    const n = Math.min(shapePoints.length, bootParticleCap(W));
    const shuffled = shapePoints.slice();
    for (let k = shuffled.length - 1; k > 0; k--) {
      const j = (Math.random() * (k + 1)) | 0;
      const tmp = shuffled[k]!;
      shuffled[k] = shuffled[j]!;
      shuffled[j] = tmp;
    }
    while (particles.length < n) particles.push(newParticle());
    if (particles.length > n) particles.length = n;
    for (let k = 0; k < n; k++) {
      const p = particles[k]!;
      const t = shuffled[k]!;
      p.tx = t.x + (Math.random() - 0.5) * 2;
      p.ty = t.y + (Math.random() - 0.5) * 2;
    }
    recolor();
  }

  /* Radial explosion away from the logo center (spatial continuity: the
     logo "explodes" into the UI as the app fades in). */
  function explode(): void {
    const cx = W / 2;
    const cy = H * LOGO_CY;
    for (const p of particles) {
      const a = Math.atan2(p.y - cy, p.x - cx) + (Math.random() - 0.5) * 0.6;
      const sp = 2 + Math.random() * 7;
      p.vx = Math.cos(a) * sp;
      p.vy = Math.sin(a) * sp;
    }
  }

  function stepDrift(p: Particle, dt: number): void {
    const dx = p.tx - p.x;
    const dy = p.ty - p.y;
    if (dx * dx + dy * dy < 900 || Math.random() < 0.003 * dt) {
      p.tx = Math.random() * W;
      p.ty = Math.random() * H;
    }
    p.vx += dx * DRIFT_SPRING * dt;
    p.vy += dy * DRIFT_SPRING * dt;
    p.vx *= Math.pow(DRIFT_DAMPING, dt);
    p.vy *= Math.pow(DRIFT_DAMPING, dt);
  }

  function dot(x: number, y: number, r: number): void {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, TAU);
    ctx.fill();
  }

  function frame(now: number): void {
    raf = null;
    if (destroyed) return;
    if (phase === "disperse" && now - disperseStart > DISPERSE_MS) {
      fire("disperseElapsed", now);
      return;
    }
    const dt = Math.min((now - last) / 16.7, 3);
    last = now;

    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const glow = PALETTES[theme()].glow;
    const R2 = REPEL_RADIUS * REPEL_RADIUS;
    for (const p of particles) {
      if (phase === "gather") {
        const wob = Math.sin(now * 0.0012 + p.seed) * 0.35;
        p.vx += ((p.tx - p.x) * GATHER_SPRING + wob * 0.05) * dt;
        p.vy += ((p.ty - p.y) * GATHER_SPRING + wob * 0.04) * dt;
        p.vx *= Math.pow(GATHER_DAMPING, dt);
        p.vy *= Math.pow(GATHER_DAMPING, dt);
      } else if (phase === "drift") {
        stepDrift(p, dt);
      } else {
        p.vx *= Math.pow(DISPERSE_DAMPING, dt);
        p.vy *= Math.pow(DISPERSE_DAMPING, dt);
      }
      const dx = p.x - mouse.x;
      const dy = p.y - mouse.y;
      const d2 = dx * dx + dy * dy;
      if (d2 < R2 && d2 > 0.01) {
        const d = Math.sqrt(d2);
        const f = (1 - d / REPEL_RADIUS) * REPEL_FORCE;
        p.vx += (dx / d) * f * dt;
        p.vy += (dy / d) * f * dt;
      }
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      const c = p.color;
      if (glow) {
        ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},0.09)`;
        dot(p.x, p.y, p.r * 2.7);
      }
      ctx.fillStyle = `rgb(${c[0]},${c[1]},${c[2]})`;
      dot(p.x, p.y, p.r);
    }
    raf = requestAnimationFrame(frame);
  }

  function startLoop(): void {
    // Guard against visibilitychange restarting a finished loop: once the
    // disperse has elapsed and phase flipped to "done", the engine must stay
    // stopped (otherwise returning to the tab would re-animate a dead stage).
    if (raf !== null || reduced || destroyed || document.hidden) return;
    if (phase === "done") return;
    last = performance.now();
    raf = requestAnimationFrame(frame);
  }

  function stopLoop(): void {
    if (raf !== null) {
      cancelAnimationFrame(raf);
      raf = null;
    }
  }

  /* Reduced motion: one static frame of the logo, no loop (DESIGN.md §9). */
  function drawStatic(): void {
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!shapePoints) return;
    const pal = PALETTES[theme()];
    const pts = shapePoints;
    for (let i = 0; i < pts.length; i++) {
      const t = pts[i]!;
      const c =
        i % 7 === 0
          ? hexRgb(pal.ember[0]!)
          : gradColor(pal.main, i / pts.length);
      ctx.fillStyle = `rgb(${c[0]},${c[1]},${c[2]})`;
      ctx.fillRect(t.x, t.y, 2, 2);
    }
  }

  function resize(): void {
    DPR = Math.min(window.devicePixelRatio || 1, DPR_CAP);
    W = canvas.clientWidth || window.innerWidth;
    H = canvas.clientHeight || window.innerHeight;
    canvas.width = Math.max(1, Math.round(W * DPR));
    canvas.height = Math.max(1, Math.round(H * DPR));
    if (phase === "drift" || phase === "gather") applyShape();
    if (reduced) drawStatic();
  }

  const onPointerMove = (e: PointerEvent): void => {
    const r = canvas.getBoundingClientRect();
    mouse.x = e.clientX - r.left;
    mouse.y = e.clientY - r.top;
  };
  const onPointerLeave = (): void => {
    mouse.x = -9999;
    mouse.y = -9999;
  };
  const onVisibility = (): void => {
    if (document.hidden) stopLoop();
    else startLoop();
  };
  let resizeTimer: ReturnType<typeof setTimeout> | null = null;
  const onResize = (): void => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resize, 200);
  };
  /* Theme flip during boot: live re-color, no engine restart. */
  const observer = new MutationObserver(() => {
    recolor();
    if (reduced) drawStatic();
  });

  // ── Init ─────────────────────────────────────────────────────────────
  resize();
  // Initial drift population so the stage is alive before the logo loads.
  const n = bootParticleCap(W);
  for (let k = 0; k < n; k++) particles.push(newParticle());
  recolor();

  const img = new Image();
  img.onload = (): void => {
    if (destroyed) return;
    shapePoints = sampleLogo(img);
    if (!shapePoints) return; // graceful fallback: stay in drift
    applyShape();
    fire("logoReady");
    if (reduced) drawStatic();
  };
  img.onerror = (): void => {
    // Logo failed to load: stay in drift — status copy still communicates.
  };
  img.src = logoSrc;

  if (!coarse) {
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerleave", onPointerLeave);
  }
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("resize", onResize);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });

  if (reduced) drawStatic();
  else startLoop();

  return {
    setReady: () => fire("backendReady"),
    destroy: () => {
      destroyed = true;
      stopLoop();
      observer.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("resize", onResize);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerleave", onPointerLeave);
      if (resizeTimer) clearTimeout(resizeTimer);
    },
    get phase() {
      return phase;
    },
    get particleCount() {
      return particles.length;
    },
  };
}
