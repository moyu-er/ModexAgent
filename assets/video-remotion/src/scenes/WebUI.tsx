import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { Background } from "../components/Background";
import { Kicker, useRiseIn } from "../components/primitives";
import { C, MONO, SORA } from "../theme";

const REPLY =
  "On it — scanning the workspace, then I'll draft the change and pause for your approval.";

export const WebUI: React.FC = () => {
  const frame = useCurrentFrame();
  const rise = useRiseIn(0);
  const browserIn = useRiseIn(8);

  const chars = Math.floor(
    interpolate(frame, [30, 96], [0, REPLY.length], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
  );
  const shown = REPLY.slice(0, chars);
  const caret = chars < REPLY.length && frame % 16 < 8 ? "▍" : "";

  const todoChecked = frame > 70;

  return (
    <Background>
      <AbsoluteFill
        style={{
          alignItems: "center",
          justifyContent: "center",
          flexDirection: "column",
          gap: 40,
          padding: "0 140px",
        }}
      >
        <div style={{ textAlign: "center", opacity: rise.opacity, translate: rise.translate }}>
          <Kicker start={0}>Browser WebUI</Kicker>
          <h2
            style={{
              margin: "14px 0 0",
              fontFamily: SORA,
              fontWeight: 700,
              fontSize: 110,
              letterSpacing: "-2px",
              color: C.fg,
              lineHeight: 1.05,
            }}
          >
            Browser-first. <span style={{ color: C.accent }}>Zero setup.</span>
          </h2>
        </div>

        {/* browser frame */}
        <div
          style={{
            opacity: browserIn.opacity,
            translate: browserIn.translate,
            width: "100%",
            maxWidth: 1280,
            background: C.surface,
            border: `1px solid ${C.border}`,
            borderRadius: 22,
            overflow: "hidden",
            boxShadow: "0 40px 120px rgba(0,0,0,0.45)",
            display: "flex",
            flexDirection: "column",
            height: 560,
          }}
        >
          {/* top bar */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 16,
              padding: "16px 22px",
              borderBottom: `1px solid ${C.border}`,
              background: C.surfaceRaised,
            }}
          >
            <span style={{ width: 13, height: 13, borderRadius: 13, background: "#ff5f57" }} />
            <span style={{ width: 13, height: 13, borderRadius: 13, background: "#febc2e" }} />
            <span style={{ width: 13, height: 13, borderRadius: 13, background: "#28c840" }} />
            <div
              style={{
                flex: 1,
                marginLeft: 18,
                padding: "8px 18px",
                borderRadius: 999,
                background: C.bg,
                border: `1px solid ${C.border}`,
                fontFamily: MONO,
                fontSize: 21,
                color: C.muted,
              }}
            >
              localhost:21800/webui
            </div>
          </div>

          {/* body: chat + side panel */}
          <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
            {/* chat */}
            <div
              style={{
                flex: 1,
                padding: "26px 30px",
                display: "flex",
                flexDirection: "column",
                gap: 18,
                borderRight: `1px solid ${C.border}`,
              }}
            >
              {/* user bubble */}
              <div style={{ alignSelf: "flex-end", maxWidth: "62%" }}>
                <div
                  style={{
                    background: C.accent,
                    color: C.bg,
                    borderRadius: "18px 18px 4px 18px",
                    padding: "14px 20px",
                    fontFamily: SORA,
                    fontSize: 26,
                    fontWeight: 600,
                  }}
                >
                  Refactor the session registry.
                </div>
              </div>
              {/* assistant bubble (streaming) */}
              <div style={{ alignSelf: "flex-start", maxWidth: "74%" }}>
                <div
                  style={{
                    background: C.surfaceRaised,
                    border: `1px solid ${C.border}`,
                    borderRadius: "18px 18px 18px 4px",
                    padding: "14px 20px",
                    fontFamily: SORA,
                    fontSize: 26,
                    color: C.fg,
                    lineHeight: 1.4,
                    minHeight: 64,
                  }}
                >
                  {shown}
                  <span style={{ color: C.accent }}>{caret}</span>
                </div>
              </div>

              {/* composer */}
              <div style={{ marginTop: "auto" }}>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 14,
                    padding: "12px 16px",
                    borderRadius: 16,
                    background: C.bg,
                    border: `1px solid ${C.border}`,
                  }}
                >
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 9,
                      padding: "7px 14px",
                      borderRadius: 999,
                      background: C.surfaceRaised,
                      border: `1px solid ${C.border}`,
                      fontFamily: MONO,
                      fontSize: 20,
                      color: C.fg,
                    }}
                  >
                    <span
                      style={{
                        width: 9,
                        height: 9,
                        borderRadius: 9,
                        background: C.accent,
                        boxShadow: `0 0 10px ${C.accent}`,
                      }}
                    />
                    kimi · kimi-for-coding
                  </span>
                  <span style={{ flex: 1, fontFamily: MONO, fontSize: 21, color: C.muted }}>
                    Message ModexAgent…
                  </span>
                </div>
              </div>
            </div>

            {/* side panel: TodoPanel */}
            <div style={{ width: 290, padding: "24px 22px", display: "flex", flexDirection: "column", gap: 14 }}>
              <div style={{ fontFamily: MONO, fontSize: 18, letterSpacing: "2px", textTransform: "uppercase", color: C.muted }}>
                Todo
              </div>
              <TodoRow text="Scan workspace" checked={todoChecked} />
              <TodoRow text="Draft change" checked={false} />
              <TodoRow text="Pause for approval" checked={false} />
            </div>
          </div>
        </div>
      </AbsoluteFill>
    </Background>
  );
};

const TodoRow: React.FC<{ text: string; checked: boolean }> = ({ text, checked }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
    <span
      style={{
        width: 22,
        height: 22,
        borderRadius: 7,
        border: `2px solid ${checked ? C.accent : C.border}`,
        background: checked ? C.accent : "transparent",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: C.bg,
        fontFamily: SORA,
        fontWeight: 700,
        fontSize: 16,
        lineHeight: 1,
      }}
    >
      {checked ? "✓" : ""}
    </span>
    <span
      style={{
        fontFamily: SORA,
        fontSize: 24,
        color: checked ? C.muted : C.fg,
        textDecoration: checked ? "line-through" : "none",
      }}
    >
      {text}
    </span>
  </div>
);
