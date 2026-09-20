import { AbsoluteFill, Img, staticFile, useCurrentFrame } from "remotion";

/**
 * Puppy mascot — plays the same choreographed loop as the WebUI Mascot
 * component, driven by the render frame instead of timers. One round:
 * settle → ear flick → wink → happy tongue → settle, then a short idle
 * rest; every third round appends a bark/tilt burst.
 */

const FPS = 30;
const step = (frame: string, ms: number) =>
  [frame, Math.max(1, Math.round((ms / 1000) * FPS))] as const;

const ROUND = [
  step("f1", 500),
  step("f2", 500), // ear flick
  step("f1", 400),
  step("f3", 450), // wink
  step("f1", 400),
  step("f4", 650), // happy tongue
  step("f1", 500),
];
const BURST = [step("f7", 600), step("f5", 500), step("f1", 300)];
const REST = Math.round(1.4 * FPS);
const BURST_EVERY = 3;

const roundLen = ROUND.reduce((n, [, d]) => n + d, 0);
const burstLen = BURST.reduce((n, [, d]) => n + d, 0);
const CYCLE = roundLen + REST;

const cumulative = (steps: ReadonlyArray<readonly [string, number]>) => {
  let acc = 0;
  return steps.map(([name, d]) => {
    const at = acc;
    acc += d;
    return [name, at, acc] as const;
  });
};
const ROUND_CUM = cumulative(ROUND);
const BURST_ROUND_CUM = cumulative([...ROUND, ...BURST]);

const frameAt = (f: number): string => {
  const cycle = Math.floor(f / CYCLE);
  const t = f % CYCLE;
  const table = cycle % BURST_EVERY === BURST_EVERY - 1 ? BURST_ROUND_CUM : ROUND_CUM;
  const end = cycle % BURST_EVERY === BURST_EVERY - 1 ? roundLen + burstLen : roundLen;
  if (t >= end) return "f1"; // resting between rounds
  for (const [name, , to] of table) {
    if (t < to) return name;
  }
  return "f1";
};

export const Mascot: React.FC<{ size: number }> = ({ size }) => {
  const f = useCurrentFrame();
  const float = Math.sin(f / 30) * 4;
  return (
    <AbsoluteFill
      style={{
        alignItems: "center",
        justifyContent: "center",
        translate: `0 ${float}px`,
      }}
    >
      <Img
        src={staticFile(`mascot/${frameAt(f)}.png`)}
        style={{ width: size, height: size, objectFit: "contain" }}
      />
    </AbsoluteFill>
  );
};
