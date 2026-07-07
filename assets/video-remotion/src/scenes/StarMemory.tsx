import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { Background } from "../components/Background";
import { Kicker, useRiseIn } from "../components/primitives";
import { C, MONO, SORA } from "../theme";

const SATS: { x: number; y: number; label: string }[] = [
  { x: 150, y: 70, label: "Office" },
  { x: 300, y: 200, label: "Query" },
  { x: 150, y: 330, label: "Helper" },
  { x: 0, y: 200, label: "Custom" },
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
          <div style={{ flex: 1, display: "flex", justifyContent: "center" }}>
            <svg viewBox="0 0 300 400" width={420} height={560}>
              {/* edges */}
              {SATS.map((s) => (
                <line
                  key={s.label}
                  x1={150}
                  y1={200}
                  x2={s.x}
                  y2={s.y}
                  stroke={C.accent}
                  strokeWidth={2}
                  strokeDasharray="6 6"
                  opacity={0.5}
                />
              ))}
              {/* satellites */}
              {SATS.map((s, i) => {
                const pulse = interpolate(Math.sin(frame / 20 + i * 1.6), [-1, 1], [0.6, 1]);
                return (
                  <g key={s.label} opacity={pulse}>
                    <circle cx={s.x} cy={s.y} r={22} fill={C.surfaceRaised} stroke={C.accent} strokeWidth={2} />
                    <text
                      x={s.x}
                      y={s.y + 6}
                      textAnchor="middle"
                      fontFamily="monospace"
                      fontSize={15}
                      fill={C.fg}
                    >
                      {s.label}
                    </text>
                  </g>
                );
              })}
              {/* center main */}
              <circle cx={150} cy={200} r={40} fill={C.accent} />
              <circle cx={150} cy={200} r={40} fill="none" stroke={C.accent} strokeWidth={2} opacity={0.4}>
              </circle>
              <text x={150} y={206} textAnchor="middle" fontFamily="monospace" fontWeight={700} fontSize={20} fill={C.bg}>
                Main
              </text>
            </svg>
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
