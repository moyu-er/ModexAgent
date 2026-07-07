import { interpolate, useCurrentFrame } from "remotion";
import { C, MONO, SORA, easeOut } from "../theme";

/** Fade + rise in, driven by the local scene frame. */
export const useRiseIn = (start: number, dur = 18) => {
  const frame = useCurrentFrame();
  return {
    opacity: interpolate(frame, [start, start + dur], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
    translate: `0 ${interpolate(frame, [start, start + dur], [26, 0], {
      ...easeOut,
    })}px`,
  };
};

export const SectionTitle: React.FC<{
  children: React.ReactNode;
  start?: number;
}> = ({ children, start = 0 }) => {
  const rise = useRiseIn(start);
  return (
    <h2
      style={{
        margin: 0,
        fontFamily: SORA,
        fontWeight: 700,
        fontSize: 132,
        lineHeight: 1.05,
        letterSpacing: "-2px",
        color: C.fg,
        opacity: rise.opacity,
        translate: rise.translate,
      }}
    >
      {children}
    </h2>
  );
};

export const Kicker: React.FC<{
  children: React.ReactNode;
  start?: number;
}> = ({ children, start = 0 }) => {
  const rise = useRiseIn(start);
  return (
    <div
      style={{
        fontFamily: MONO,
        fontSize: 34,
        letterSpacing: "3px",
        textTransform: "uppercase",
        color: C.accent,
        opacity: rise.opacity,
        translate: rise.translate,
      }}
    >
      {children}
    </div>
  );
};

export const Card: React.FC<{
  children?: React.ReactNode;
  style?: React.CSSProperties;
}> = ({ children, style }) => (
  <div
    style={{
      background: C.surface,
      border: `1px solid ${C.border}`,
      borderRadius: 20,
      ...style,
    }}
  >
    {children}
  </div>
);

export const Pill: React.FC<{
  children: React.ReactNode;
  tone?: "accent" | "warm" | "muted";
  style?: React.CSSProperties;
}> = ({ children, tone = "muted", style }) => {
  const color =
    tone === "accent" ? C.accent : tone === "warm" ? C.warm : C.muted;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 10,
        padding: "10px 18px",
        borderRadius: 999,
        background: C.surfaceRaised,
        border: `1px solid ${C.border}`,
        fontFamily: MONO,
        fontSize: 26,
        color: C.fg,
        ...style,
      }}
    >
      <span
        style={{
          width: 9,
          height: 9,
          borderRadius: 9,
          background: color,
          boxShadow: `0 0 12px ${color}`,
        }}
      />
      {children}
    </span>
  );
};
