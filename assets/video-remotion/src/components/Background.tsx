import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { C } from "../theme";

// accent = #2DD4A8 -> rgb(45,212,168); warm = #F59E4B -> rgb(245,158,75)
const TEAL = "45,212,168";
const WARM = "245,158,75";

export const Background: React.FC<{ children?: React.ReactNode }> = ({
  children,
}) => {
  const frame = useCurrentFrame();
  const breathe = interpolate(Math.sin(frame / 90), [-1, 1], [0.12, 0.22]);

  return (
    <AbsoluteFill style={{ backgroundColor: C.bg }}>
      {/* teal glow, top-left */}
      <div
        style={{
          position: "absolute",
          top: "-18%",
          left: "-10%",
          width: "70%",
          height: "70%",
          background: `radial-gradient(circle, rgba(${TEAL},${breathe}) 0%, transparent 62%)`,
        }}
      />
      {/* warm glow, bottom-right */}
      <div
        style={{
          position: "absolute",
          bottom: "-22%",
          right: "-12%",
          width: "65%",
          height: "65%",
          background: `radial-gradient(circle, rgba(${WARM},${breathe * 0.6}) 0%, transparent 62%)`,
        }}
      />
      {/* faint dot grid */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          opacity: 0.5,
          backgroundImage: `radial-gradient(${C.border} 1.4px, transparent 1.4px)`,
          backgroundSize: "46px 46px",
        }}
      />
      {children}
    </AbsoluteFill>
  );
};
