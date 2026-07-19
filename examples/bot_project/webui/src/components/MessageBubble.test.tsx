import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageBubble } from "./MessageBubble";
import type { UIMessage } from "../types/events";

function makeMessage(overrides: Partial<UIMessage>): UIMessage {
  return {
    id: "m1",
    role: "assistant",
    agent_name: "main",
    blocks: [{ kind: "text", text: "Hello there" }],
    isStreaming: false,
    ...overrides,
  };
}

describe("MessageBubble (Teal & Ember §6)", () => {
  it("renders assistant messages in a subtle surface with a Bot avatar badge", () => {
    const { container } = render(
      <MessageBubble message={makeMessage({ role: "assistant" })} />,
    );
    // Assistant prose sits in a brand-tinted surface (not a full bubble,
    // not bare canvas).
    expect(container.querySelector(".bubble-assistant")).toBeTruthy();
    expect(container.querySelector(".bubble-user")).toBeNull();
    // Bot glyph avatar (matches the chat header agent indicator) in a
    // brand-tinted circular badge — no project logo mark.
    expect(container.querySelector("svg[data-logo-mark]")).toBeNull();
    const badge = container.querySelector(".text-brand.bg-brand-soft");
    expect(badge).toBeTruthy();
    // Prose still renders.
    expect(screen.getByText("Hello there")).toBeTruthy();
  });

  it("renders user messages in the branded bubble", () => {
    const { container } = render(
      <MessageBubble message={makeMessage({ role: "user" })} />,
    );
    const bubble = container.querySelector(".bubble-user");
    expect(bubble).toBeTruthy();
    expect(screen.getByText("Hello there")).toBeTruthy();
  });

  it("shows staggered brand typing dots while an assistant turn opens", () => {
    const { container } = render(
      <MessageBubble
        message={makeMessage({ role: "assistant", isStreaming: true, blocks: [] })}
      />,
    );
    const dots = container.querySelectorAll(".typing-dots .typing-dot");
    expect(dots.length).toBe(3);
    expect(screen.getByText("thinking")).toBeTruthy();
  });

  it("does not show typing dots once blocks are streaming", () => {
    const { container } = render(
      <MessageBubble
        message={makeMessage({ role: "assistant", isStreaming: true })}
      />,
    );
    expect(container.querySelector(".typing-dots")).toBeNull();
  });
});
