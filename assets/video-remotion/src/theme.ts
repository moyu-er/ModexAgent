import { Easing } from "remotion";
import { loadFont as loadSora } from "@remotion/google-fonts/Sora";
import { loadFont as loadDmMono } from "@remotion/google-fonts/DMMono";

export const { fontFamily: SORA } = loadSora();
export const { fontFamily: MONO } = loadDmMono();

export const C = {
  bg: "#0A0E17",
  fg: "#E8ECF1",
  accent: "#2DD4A8",
  accentBright: "#6CF2D0",
  accentDim: "#1A6B56",
  warm: "#F59E4B",
  surface: "#141B2D",
  surfaceRaised: "#1A2338",
  muted: "#64748B",
  border: "#1E293B",
  screen: "#13243C",
};

export const easeOut = {
  easing: Easing.bezier(0.22, 1, 0.36, 1),
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
} as const;

export const easeExpo = {
  easing: Easing.bezier(0.16, 1, 0.3, 1),
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
} as const;

export const easeSharp = {
  easing: Easing.bezier(0.7, 0, 0.3, 1),
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
} as const;
