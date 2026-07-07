import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { C } from "../theme";

const SATS: [number, number][] = [
  [50, 14],
  [86, 50],
  [50, 86],
  [14, 50],
];
const DIAGS: [number, number][] = [
  [71.2, 28.8],
  [71.2, 71.2],
  [28.8, 71.2],
  [28.8, 28.8],
];

/**
 * Animated V4 "Agent Hub" mark — curved star topology.
 * Idle motion: gentle float, twinkling satellites, breathing diagonals, core glow.
 * Parent wraps it for entrance/exit.
 */
export const Hub: React.FC<{ size: number }> = ({ size }) => {
  const f = useCurrentFrame();
  const float = Math.sin(f / 30) * 4;
  const diag = interpolate(Math.sin(f / 34), [-1, 1], [0.22, 0.55]);
  const coreGlow = interpolate(Math.sin(f / 28), [-1, 1], [0.25, 0.5]);
  const coreR = interpolate(Math.sin(f / 28), [-1, 1], [9, 13]);

  return (
    <AbsoluteFill
      style={{
        alignItems: "center",
        justifyContent: "center",
        translate: `0 ${float}px`,
      }}
    >
      <svg viewBox="0 0 100 100" width={size} height={size}>
        <defs>
          <radialGradient id="hub-core" cx="0.5" cy="0.5" r="0.5">
            <stop offset="0" stopColor={C.accent} stopOpacity={coreGlow} />
            <stop offset="1" stopColor={C.accent} stopOpacity={0} />
          </radialGradient>
        </defs>

        {/* core glow */}
        <circle cx="50" cy="50" r={coreR} fill="url(#hub-core)" />

        {/* cardinal edges */}
        <g stroke={C.accent} strokeLinecap="round" strokeWidth={2.1} fill="none">
          <path d="M50 50 Q58 32 50 14" />
          <path d="M50 50 Q68 42 86 50" />
          <path d="M50 50 Q42 68 50 86" />
          <path d="M50 50 Q32 58 14 50" />
        </g>
        {/* diagonal edges (breathing) */}
        <g
          stroke={C.accent}
          strokeLinecap="round"
          strokeWidth={1.2}
          fill="none"
          opacity={diag / 0.4}
        >
          <path d="M50 50 Q60 40 71.2 28.8" opacity={0.4} />
          <path d="M50 50 Q60 60 71.2 71.2" opacity={0.4} />
          <path d="M50 50 Q40 60 28.8 71.2" opacity={0.4} />
          <path d="M50 50 Q40 40 28.8 28.8" opacity={0.4} />
        </g>

        {/* core diamond */}
        <polygon points="50,39 61,50 50,61 39,50" fill={C.accent} />

        {/* twinkling cardinal satellites */}
        {SATS.map(([x, y], i) => (
          <circle
            key={`s${i}`}
            cx={x}
            cy={y}
            r={4.6}
            fill={C.accent}
            opacity={interpolate(Math.sin(f / 22 + i * 1.7), [-1, 1], [0.6, 1])}
          />
        ))}
        {/* breathing diagonal dots */}
        {DIAGS.map(([x, y], i) => (
          <circle
            key={`d${i}`}
            cx={x}
            cy={y}
            r={2.1}
            fill={C.accent}
            opacity={diag * (i % 2 ? 1 : 0.8)}
          />
        ))}
      </svg>
    </AbsoluteFill>
  );
};
