import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { Background } from "../components/Background";
import { Hub } from "../components/Hub";
import { C, MONO, SORA, easeExpo } from "../theme";

export const Hero: React.FC = () => {
  const frame = useCurrentFrame();

  const botScale = interpolate(frame, [6, 30], [0.5, 1], { ...easeExpo });
  const botOpacity = interpolate(frame, [0, 14], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const titleOpacity = interpolate(frame, [24, 44], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const titleY = interpolate(frame, [24, 44], [24, 0], { ...easeExpo });
  const tagOpacity = interpolate(frame, [36, 56], [0, 1], {
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
          gap: 28,
        }}
      >
        <div
          style={{
            width: 360,
            height: 360,
            opacity: botOpacity,
            scale: `${botScale} ${botScale}`,
          }}
        >
          <Hub size={360} />
        </div>

        <div
          style={{
            opacity: titleOpacity,
            translate: `0 ${titleY}px`,
            fontFamily: SORA,
            fontWeight: 700,
            fontSize: 150,
            letterSpacing: "-3px",
            color: C.fg,
            lineHeight: 1,
          }}
        >
          Modex
          <span style={{ color: C.accent }}>Agent</span>
        </div>

        <div
          style={{
            opacity: tagOpacity,
            fontFamily: MONO,
            fontSize: 40,
            letterSpacing: "2px",
            color: C.muted,
          }}
        >
          Modular AI Agent Framework
        </div>
      </AbsoluteFill>
    </Background>
  );
};
