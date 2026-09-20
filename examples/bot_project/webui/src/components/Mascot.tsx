// ModexBot mascot — the puppy line-art frames under public/mascot/ (512×512,
// transparent, centroid-aligned). Pure JS frame swapping: a choreographed
// round (ear flick → wink → happy tongue) plays back to back, with a short
// pause on the idle frame between rounds; every third round appends a
// bark/tilt burst. No randomness, no hover special-casing, no animation
// library.
//
// Reduced-motion (media query or `animated={false}`): a static f1 frame with
// no timers at all.

import { useEffect, useState, type FC } from "react";
import { useT } from "../i18n";

const IDLE = "f1";

/** One animation step: [frame, duration ms]. */
type Step = readonly [string, number];

/** Base round: settle → ear flick → wink → happy tongue → settle. */
const ROUND: Step[] = [
  ["f1", 500],
  ["f2", 500], // left ear flicks up, emphasis rays
  ["f1", 400],
  ["f3", 450], // wink with a slight head tilt
  ["f1", 400],
  ["f4", 650], // happy arcs + tongue out
  ["f1", 500],
];

/** Appended every BURST_EVERY-th round: bark → tilt with streaks → settle. */
const BURST: Step[] = [
  ["f7", 600], // excited bark ("o" mouth, emphasis lines)
  ["f5", 500], // head tilt with motion streaks
  ["f1", 300],
];

const BURST_EVERY = 3;

/** Idle-frame rest between rounds — a beat, not a nap. */
const ROUND_PAUSE = 1400;

const PRELOAD_FRAMES = ["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8"];

/** happy-dom may not implement matchMedia — treat as full motion. */
function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

const frameUrl = (frame: string): string =>
  `${import.meta.env.BASE_URL}mascot/${frame}.png`;

export interface MascotProps {
  /** Rendered box size in px (frames are square). */
  size: number;
  /** False = static idle frame, no timers. Default true. */
  animated?: boolean;
  className?: string;
}

export const Mascot: FC<MascotProps> = ({
  size,
  animated = true,
  className,
}) => {
  const t = useT();
  const [frame, setFrame] = useState(IDLE);
  const interactive = animated && !prefersReducedMotion();

  // Preload every frame on mount so the first swap never flashes an
  // unloaded image.
  useEffect(() => {
    if (typeof Image !== "function") return;
    for (const f of PRELOAD_FRAMES) {
      const img = new Image();
      img.src = frameUrl(f);
    }
  }, []);

  // Choreographed loop: play one round step by step, rest on f1 for
  // ROUND_PAUSE, then start the next round. Fully torn down on unmount (or
  // when `animated` flips off / reduced-motion is on).
  useEffect(() => {
    if (!interactive) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let round = 0;

    const runRound = (): void => {
      const steps =
        round % BURST_EVERY === BURST_EVERY - 1 ? [...ROUND, ...BURST] : ROUND;
      let i = 0;
      const next = (): void => {
        if (cancelled) return;
        if (i < steps.length) {
          const [f, ms] = steps[i]!;
          i += 1;
          setFrame(f);
          timer = setTimeout(next, ms);
        } else {
          setFrame(IDLE);
          round += 1;
          timer = setTimeout(runRound, ROUND_PAUSE);
        }
      };
      next();
    };

    runRound();
    return (): void => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [interactive]);

  const wrapperClass = ["shrink-0 select-none", className]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      role="img"
      aria-label={t("chat.mascotLabel")}
      className={wrapperClass}
      style={{ width: size, height: size }}
    >
      <img
        src={frameUrl(frame)}
        width={size}
        height={size}
        alt=""
        aria-hidden="true"
        draggable={false}
        className="block h-full w-full object-contain"
      />
    </div>
  );
};
