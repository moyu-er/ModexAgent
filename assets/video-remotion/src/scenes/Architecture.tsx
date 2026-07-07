import { AbsoluteFill } from "remotion";
import { Background } from "../components/Background";
import { Kicker, useRiseIn } from "../components/primitives";
import { C, MONO, SORA } from "../theme";

const MODULES: { name: string; caption: string }[] = [
  { name: "ReAct Engine", caption: "graph-driven loop" },
  { name: "Tool System", caption: "files · shell · MCP" },
  { name: "Memory Engine", caption: "6-tier · self-learning" },
  { name: "Multi-Agent", caption: "star topology" },
  { name: "Sandbox", caption: "subprocess · docker" },
  { name: "Pipeline", caption: "deep modules" },
];

const Card: React.FC<{ mod: typeof MODULES[number]; index: number }> = ({
  mod,
  index,
}) => {
  const rise = useRiseIn(10 + index * 5);
  return (
    <div
      style={{
        opacity: rise.opacity,
        translate: rise.translate,
        background: C.surface,
        border: `1px solid ${C.border}`,
        borderRadius: 18,
        padding: "26px 30px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span
          style={{
            width: 12,
            height: 12,
            borderRadius: 12,
            background: C.accent,
            boxShadow: `0 0 14px ${C.accent}`,
          }}
        />
        <span style={{ fontFamily: SORA, fontWeight: 600, fontSize: 40, color: C.fg }}>
          {mod.name}
        </span>
      </div>
      <span style={{ fontFamily: MONO, fontSize: 24, color: C.muted }}>
        {mod.caption}
      </span>
    </div>
  );
};

export const Architecture: React.FC = () => {
  const titleRise = useRiseIn(0);
  return (
    <Background>
      <AbsoluteFill
        style={{
          alignItems: "center",
          justifyContent: "center",
          flexDirection: "column",
          gap: 54,
          padding: "0 140px",
        }}
      >
        <div style={{ textAlign: "center", opacity: titleRise.opacity, translate: titleRise.translate }}>
          <Kicker start={0}>Deep modules</Kicker>
          <h2
            style={{
              margin: "14px 0 0",
              fontFamily: SORA,
              fontWeight: 700,
              fontSize: 124,
              letterSpacing: "-2px",
              color: C.fg,
              lineHeight: 1.05,
            }}
          >
            Modular by design
          </h2>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(3, 1fr)",
            gap: 26,
            width: "100%",
            maxWidth: 1500,
          }}
        >
          {MODULES.map((m, i) => (
            <Card key={m.name} mod={m} index={i} />
          ))}
        </div>
      </AbsoluteFill>
    </Background>
  );
};
