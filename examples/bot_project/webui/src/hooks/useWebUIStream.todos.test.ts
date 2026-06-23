import { describe, expect, it } from "vitest";
import type { UIMessage } from "../types/events";
import { scanHistoryForTodos } from "./useWebUIStream";

function assistant(blocks: UIMessage["blocks"]): UIMessage {
  return {
    id: "m",
    role: "assistant",
    agent_name: "main",
    blocks,
    isStreaming: false,
    timestamp: 0,
  };
}

function user(text: string): UIMessage {
  return {
    id: "u",
    role: "user",
    agent_name: "main",
    blocks: [{ kind: "text", text }],
    isStreaming: false,
    timestamp: 0,
  };
}

function toolBlock(tool: string, args: unknown, result: string | undefined): UIMessage["blocks"][number] {
  return { kind: "tool", tool: { tool, args: args as Record<string, unknown>, result } };
}

describe("scanHistoryForTodos", () => {
  it("returns undefined when history has no todo tool blocks", () => {
    const history: UIMessage[] = [
      user("hi"),
      assistant([{ kind: "text", text: "hello" }]),
    ];
    expect(scanHistoryForTodos(history)).toBeUndefined();
  });

  it("returns the most recent todo tool result found in history", () => {
    const history: UIMessage[] = [
      assistant([
        toolBlock("todo_write", {}, JSON.stringify([{ content: "first", status: "in_progress" }])),
      ]),
      user("anything"),
      assistant([
        { kind: "text", text: "thinking" },
        toolBlock(
          "todo_write",
          {},
          JSON.stringify([
            { content: "stale", status: "in_progress" }, // older — should be skipped
          ]),
        ),
      ]),
      assistant([
        toolBlock(
          "todo_write",
          {},
          JSON.stringify([{ content: "latest", status: "in_progress" }]),
        ),
      ]),
    ];
    expect(scanHistoryForTodos(history)).toEqual([{ content: "latest", status: "in_progress" }]);
  });

  it("ignores todo tool blocks without a result", () => {
    const history: UIMessage[] = [
      assistant([toolBlock("todo_write", {}, undefined)]),
      assistant([toolBlock("read", {}, "ok")]),
    ];
    expect(scanHistoryForTodos(history)).toBeUndefined();
  });

  it("ignores blocks whose result is an Error", () => {
    const history: UIMessage[] = [
      assistant([toolBlock("todo_write", {}, "Error: no active agent session.")]),
    ];
    expect(scanHistoryForTodos(history)).toBeUndefined();
  });

  it("ignores non-todo tools", () => {
    const history: UIMessage[] = [
      assistant([toolBlock("read", {}, "file contents")]),
    ];
    expect(scanHistoryForTodos(history)).toBeUndefined();
  });

  it("tolerates malformed JSON and non-array results", () => {
    const history: UIMessage[] = [
      assistant([toolBlock("todo_write", {}, "not json")]),
      assistant([toolBlock("todo_write", {}, JSON.stringify({ content: "x" }))]),
      assistant([toolBlock("todo_write", {}, JSON.stringify([]))]),
    ];
    expect(scanHistoryForTodos(history)).toEqual([]);
  });
});
