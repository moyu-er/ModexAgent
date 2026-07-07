import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { Background } from "../components/Background";
import { Hub } from "../components/Hub";
import { C, MONO, SORA, easeExpo } from "../theme";

export const Outro: React.FC = () => {
  const frame = useCurrentFrame();
  const fade = interpolate(frame, [0, 14, 90, 114], [0, 1, 1, 0.85], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const hubOpacity = interpolate(frame, [0, 16], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const hubScale = interpolate(frame, [0, 26], [0.6, 1], { ...easeExpo });
  const titleOpacity = interpolate(frame, [14, 32], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const ctaOpacity = interpolate(frame, [26, 44], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <Background>
      <AbsoluteFill
        style={{
          alignItems: "center",
          justifyContent: "center",
          flexDirection: "column",
          gap: 26,
          opacity: fade,
        }}
      >
        <div style={{ width: 300, height: 300, opacity: hubOpacity, scale: `${hubScale} ${hubScale}` }}>
          <Hub size={300} />
        </div>
        <div
          style={{
            opacity: titleOpacity,
            fontFamily: SORA,
            fontWeight: 700,
            fontSize: 130,
            letterSpacing: "-3px",
            color: C.fg,
          }}
        >
          Modex<span style={{ color: C.accent }}>Agent</span>
        </div>
        <div
          style={{
            opacity: ctaOpacity,
            fontFamily: MONO,
            fontSize: 38,
            color: C.accent,
            letterSpacing: "1px",
          }}
        >
          github.com/moyu-er/ModexAgent
        </div>
      </AbsoluteFill>
    </Background>
  );
};
