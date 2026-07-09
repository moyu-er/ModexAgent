import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { Background } from "../components/Background";
import { Kicker, useRiseIn } from "../components/primitives";
import { C, MONO, SORA } from "../theme";

// Generic pluggable nodes around the orchestrator. No fabricated agent names —
// pools are user-defined and configured in the WebUI. Laid out on a circle of
// radius 120 around (180,180), node r=28, all within the 360x360 viewBox with
// safe margins so nothing is clipped at the left/right edges.
const SATS: { x: number; y: number }[] = [
  { x: 180, y: 60 },
  { x: 294, y: 143 },
  { x: 250, y: 277 },
  { x: 110, y: 277 },
  { x: 66, y: 143 },
];
const MEM = [
  { name: "Session", caption: "per-conversation" },
  { name: "Archive", caption: "compressed history" },
  { name: "Knowledge", caption: "SOUL · USER · MEMORY" },
  { name: "Experience", caption: "self-learned" },
];

export const StarMemory: React.FC = () => {
  const frame = useCurrentFrame();
  const rise = useRiseIn(0);

  return (
    <Background>
      <AbsoluteFill
        style={{
          alignItems: "center",
          justifyContent: "center",
          flexDirection: "column",
          gap: 44,
          padding: "0 140px",
        }}
      >
        <div style={{ textAlign: "center", opacity: rise.opacity, translate: rise.translate }}>
          <Kicker start={0}>Collaboration · Memory</Kicker>
          <h2
            style={{
              margin: "14px 0 0",
              fontFamily: SORA,
              fontWeight: 700,
              fontSize: 96,
              letterSpacing: "-2px",
              color: C.fg,
              lineHeight: 1.05,
            }}
          >
            Star topology · <span style={{ color: C.accent }}>multi-tier memory</span>
          </h2>
        </div>

        <div style={{ display: "flex", gap: 60, width: "100%", maxWidth: 1400, alignItems: "center" }}>
          {/* star topology */}
          <div
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              justifyContent: "center",
              alignItems: "center",
              gap: 20,
            }}
          >
            <svg viewBox="0 0 360 360" width={440} height={440}>
              {/* edges */}
              {SATS.map((s, i) => {
                const draw = interpolate(frame, [6 + i * 3, 22 + i * 3], [0, 1], {
                  extrapolateLeft: "clamp",
                  extrapolateRight: "clamp",
                });
                return (
                  <line
                    key={`e${i}`}
                    x1={180}
                    y1={180}
                    x2={s.x}
                    y2={s.y}
                    stroke={C.accent}
                    strokeWidth={2}
                    strokeDasharray="6 7"
                    opacity={0.5 * draw}
                  />
                );
              })}
              {/* generic pluggable sub-agent nodes (no fabricated names) */}
              {SATS.map((s, i) => {
                const pulse = interpolate(Math.sin(frame / 20 + i * 1.6), [-1, 1], [0.55, 1]);
                const appear = interpolate(frame, [6 + i * 3, 22 + i * 3], [0, 1], {
                  extrapolateLeft: "clamp",
                  extrapolateRight: "clamp",
                });
                return (
                  <g key={`s${i}`} opacity={appear}>
                    <circle
                      cx={s.x}
                      cy={s.y}
                      r={28}
                      fill={C.surfaceRaised}
                      stroke={C.accent}
                      strokeWidth={2}
                      opacity={pulse}
                    />
                    <circle cx={s.x} cy={s.y} r={6} fill={C.accent} />
                  </g>
                );
              })}
              {/* center orchestrator */}
              <circle cx={180} cy={180} r={44} fill={C.accent} />
              <circle cx={180} cy={180} r={44} fill="none" stroke={C.accent} strokeWidth={2} opacity={0.4} />
              <text
                x={180}
                y={188}
                textAnchor="middle"
                fontFamily="monospace"
                fontWeight={700}
                fontSize={22}
                fill={C.bg}
              >
                Main
              </text>
            </svg>
            <div style={{ fontFamily: MONO, fontSize: 20, color: C.muted, textAlign: "center", maxWidth: 440 }}>
              pluggable sub-agents · configured per pool in WebUI
            </div>
          </div>

          {/* memory stack */}
          <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 18 }}>
            {MEM.map((m, i) => (
              <MemoryCard key={m.name} mem={m} index={i} />
            ))}
          </div>
        </div>
      </AbsoluteFill>
    </Background>
  );
};

const MemoryCard: React.FC<{ mem: typeof MEM[number]; index: number }> = ({
  mem,
  index,
}) => {
  const frame = useCurrentFrame();
  const start = 8 + index * 6;
  const highlight = index === 2;
  const opacity = interpolate(frame, [start, start + 16], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const ty = interpolate(frame, [start, start + 16], [22, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <div
      style={{
        opacity,
        translate: `0 ${ty}px`,
        display: "flex",
        alignItems: "center",
        gap: 18,
        padding: "20px 26px",
        borderRadius: 16,
        background: highlight ? C.surfaceRaised : C.surface,
        border: `1px solid ${highlight ? C.accent : C.border}`,
      }}
    >
      <span
        style={{
          width: 14,
          height: 14,
          borderRadius: 14,
          background: highlight ? C.accent : C.muted,
          boxShadow: highlight ? `0 0 14px ${C.accent}` : "none",
        }}
      />
      <div style={{ display: "flex", flexDirection: "column" }}>
        <span style={{ fontFamily: SORA, fontWeight: 600, fontSize: 34, color: C.fg }}>{mem.name}</span>
        <span style={{ fontFamily: MONO, fontSize: 20, color: C.muted }}>{mem.caption}</span>
      </div>
    </div>
  );
};
