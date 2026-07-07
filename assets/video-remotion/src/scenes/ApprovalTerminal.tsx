import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { Background } from "../components/Background";
import { Kicker, useRiseIn } from "../components/primitives";
import { C, MONO, SORA, easeOut } from "../theme";

export const ApprovalTerminal: React.FC = () => {
  const frame = useCurrentFrame();
  const rise = useRiseIn(0);

  const steps = ["Tool call", "Paused", "Approve", "Resume"];
  const activeStep = Math.min(
    steps.length - 1,
    Math.floor(frame / 28),
  );

  return (
    <Background>
      <AbsoluteFill
        style={{
          alignItems: "center",
          justifyContent: "center",
          flexDirection: "column",
          gap: 48,
          padding: "0 140px",
        }}
      >
        <div style={{ textAlign: "center", opacity: rise.opacity, translate: rise.translate }}>
          <Kicker start={0}>Safety · Depth</Kicker>
          <h2
            style={{
              margin: "14px 0 0",
              fontFamily: SORA,
              fontWeight: 700,
              fontSize: 108,
              letterSpacing: "-2px",
              color: C.fg,
              lineHeight: 1.05,
            }}
          >
            Pause. Approve. <span style={{ color: C.accent }}>Resume.</span>
          </h2>
        </div>

        <div style={{ display: "flex", gap: 40, width: "100%", maxWidth: 1500, alignItems: "stretch" }}>
          {/* approval column */}
          <Panel title="Interruptible Approval" subtitle="risky writes suspend">
            {/* flow pills */}
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 28, flexWrap: "wrap" }}>
              {steps.map((s, i) => (
                <div key={s} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span
                    style={{
                      padding: "9px 16px",
                      borderRadius: 999,
                      fontFamily: MONO,
                      fontSize: 21,
                      border: `1px solid ${i <= activeStep ? C.accent : C.border}`,
                      background: i === activeStep ? C.accent : "transparent",
                      color: i === activeStep ? C.bg : i < activeStep ? C.accent : C.muted,
                    }}
                  >
                    {s}
                  </span>
                  {i < steps.length - 1 && <span style={{ color: C.muted, fontSize: 22 }}>→</span>}
                </div>
              ))}
            </div>

            {/* approval card */}
            <div
              style={{
                border: `1px solid ${C.accent}`,
                borderRadius: 16,
                padding: "20px 22px",
                background: C.surfaceRaised,
                display: "flex",
                flexDirection: "column",
                gap: 14,
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <span style={{ width: 11, height: 11, borderRadius: 11, background: C.warm, boxShadow: `0 0 12px ${C.warm}` }} />
                <span style={{ fontFamily: SORA, fontWeight: 600, fontSize: 30, color: C.fg }}>write_file</span>
                <span style={{ fontFamily: MONO, fontSize: 20, color: C.muted }}>· awaiting approval</span>
              </div>
              <div
                style={{
                  fontFamily: MONO,
                  fontSize: 22,
                  color: C.muted,
                  background: C.bg,
                  borderRadius: 10,
                  padding: "12px 16px",
                }}
              >
                <span style={{ color: C.warm }}>~/secrets/</span>config.toml
              </div>
              <div style={{ display: "flex", gap: 12 }}>
                <span style={{ padding: "10px 20px", borderRadius: 10, background: C.accent, color: C.bg, fontFamily: SORA, fontWeight: 600, fontSize: 24 }}>
                  Approve
                </span>
                <span style={{ padding: "10px 20px", borderRadius: 10, border: `1px solid ${C.border}`, color: C.fg, fontFamily: SORA, fontWeight: 600, fontSize: 24 }}>
                  Deny
                </span>
              </div>
            </div>
          </Panel>

          {/* terminal column */}
          <Panel title="Interactive Terminal" subtitle="WinPTY · pexpect · tmux · SSH">
            <div
              style={{
                background: C.bg,
                border: `1px solid ${C.border}`,
                borderRadius: 14,
                overflow: "hidden",
                flex: 1,
              }}
            >
              <div style={{ display: "flex", gap: 7, padding: "12px 16px", borderBottom: `1px solid ${C.border}` }}>
                <span style={{ width: 11, height: 11, borderRadius: 11, background: "#ff5f57" }} />
                <span style={{ width: 11, height: 11, borderRadius: 11, background: "#febc2e" }} />
                <span style={{ width: 11, height: 11, borderRadius: 11, background: "#28c840" }} />
              </div>
              <div style={{ padding: "18px 20px", fontFamily: MONO, fontSize: 23, lineHeight: 1.7 }}>
                <TermLine prompt="$ " cmd="cd /workspace" frame={frame} at={10} />
                <TermLine prompt="$ " cmd="git pull origin main" frame={frame} at={34} />
                <TermLine prompt="$ " cmd="ssh deploy@prod" frame={frame} at={64} />
                <div style={{ color: C.accent, opacity: frame > 92 ? 1 : 0, transition: "none" }}>
                  ✓ service restarted
                </div>
              </div>
            </div>
          </Panel>
        </div>
      </AbsoluteFill>
    </Background>
  );
};

const Panel: React.FC<{ title: string; subtitle: string; children: React.ReactNode }> = ({
  title,
  subtitle,
  children,
}) => (
  <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
    <div style={{ marginBottom: 20 }}>
      <div style={{ fontFamily: SORA, fontWeight: 600, fontSize: 34, color: C.fg }}>{title}</div>
      <div style={{ fontFamily: MONO, fontSize: 20, color: C.muted }}>{subtitle}</div>
    </div>
    {children}
  </div>
);

const TermLine: React.FC<{ prompt: string; cmd: string; frame: number; at: number }> = ({
  prompt,
  cmd,
  frame,
  at,
}) => {
  const visible = frame > at;
  const chars = Math.floor(
    interpolate(frame, [at, at + 16], [0, cmd.length], { ...easeOut, extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
  );
  if (!visible) return <div style={{ height: "1.7em" }} />;
  return (
    <div style={{ color: C.fg }}>
      <span style={{ color: C.accent }}>{prompt}</span>
      {cmd.slice(0, chars)}
    </div>
  );
};
