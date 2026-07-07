import { AbsoluteFill, Sequence } from "remotion";
import { C } from "./theme";
import { Hero } from "./scenes/Hero";
import { Architecture } from "./scenes/Architecture";
import { WebUI } from "./scenes/WebUI";
import { ApprovalTerminal } from "./scenes/ApprovalTerminal";
import { StarMemory } from "./scenes/StarMemory";
import { Outro } from "./scenes/Outro";

export const MyComposition = () => {
  return (
    <AbsoluteFill style={{ backgroundColor: C.bg }}>
      <Sequence durationInFrames={120}>
        <Hero />
      </Sequence>
      <Sequence from={120} durationInFrames={180}>
        <Architecture />
      </Sequence>
      <Sequence from={300} durationInFrames={180}>
        <WebUI />
      </Sequence>
      <Sequence from={480} durationInFrames={180}>
        <ApprovalTerminal />
      </Sequence>
      <Sequence from={660} durationInFrames={150}>
        <StarMemory />
      </Sequence>
      <Sequence from={810} durationInFrames={120}>
        <Outro />
      </Sequence>
    </AbsoluteFill>
  );
};
