import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { GraphInstanceDetail } from "./GraphInstanceDetail";
import { ToastProvider } from "../ToastContext";
import type { GraphEvent } from "../../lib/graphsApi";

let status = "running";
let finishControl: (response: Response) => void;
let finishSnapshot: ((response: Response) => void) | undefined;
let holdSnapshot = false;
const posts: string[] = [];
let events: GraphEvent[] = [];

function snapshot(): Response {
  return Response.json({ spec_id: "1", graph_instance_id: "2", status, nodes: [], result: null });
}

async function tick(ms = 0): Promise<void> {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

beforeEach(() => {
  vi.useFakeTimers();
  status = "running";
  holdSnapshot = false;
  finishSnapshot = undefined;
  posts.length = 0;
  events = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push(url);
      return new Promise<Response>((resolve) => { finishControl = resolve; });
    }
    if (url.endsWith("/events")) return Promise.resolve(Response.json({ events }));
    if (url.endsWith("/invocations")) return Promise.resolve(Response.json([]));
    if (url.endsWith("/topology")) return Promise.resolve(Response.json({
      spec_id: "1", name: "test", scheduler: "parallel", default_trigger: "on_all_preds",
      nodes: [], edges: [{ source: "__start__", target: "__end__" }], entry_node: "__start__",
    }));
    if (url.endsWith("/specs/1")) return Promise.resolve(Response.json({
      spec_id: "1", name: "test", version: "1", yaml_content: "",
    }));
    if (holdSnapshot) return new Promise<Response>((resolve) => { finishSnapshot = resolve; });
    return Promise.resolve(snapshot());
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function open(): Promise<HTMLElement> {
  render(<ToastProvider><GraphInstanceDetail workspaceId="test-ws" instanceId="2" onBack={() => {}} /></ToastProvider>);
  await tick();
  fireEvent.click(screen.getByRole("button", { name: "Topology" }));
  await tick();
  return screen.getByTestId("control-bar");
}

it("shows actual drain, resumes the same instance once, and locks controls through snapshot reconciliation", async () => {
  let bar = await open();
  fireEvent.click(within(bar).getByRole("button", { name: "Pause" }));
  status = "pausing";
  await tick(2000);
  expect(within(bar).getByText("pausing")).toBeTruthy();
  expect(within(bar).queryByRole("button", { name: "Resume" })).toBeNull();
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).disabled).toBe(true);

  status = "paused";
  finishControl(Response.json({ graph_instance_id: "2", status }));
  await tick();
  fireEvent.click(within(bar).getByRole("button", { name: "Resume" }));
  fireEvent.click(within(bar).getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Topology" }));
  bar = screen.getByTestId("control-bar");
  expect((within(bar).getByRole("button", { name: "Resume" }) as HTMLButtonElement).disabled).toBe(true);

  holdSnapshot = true;
  status = "running";
  finishControl(Response.json({ graph_instance_id: "2", status }));
  await tick();
  const resume = within(bar).getByRole("button", { name: "Resume" }) as HTMLButtonElement;
  expect(resume.disabled).toBe(true);
  fireEvent.click(resume);
  expect(posts).toEqual(["/api/graphs/instances/2/pause", "/api/graphs/instances/2/resume"]);
  holdSnapshot = false;
  finishSnapshot!(snapshot());
  await tick();
  expect(within(bar).getByText("running")).toBeTruthy();
  status = "completed";
  await tick(2000);
  expect(within(bar).getByText("completed")).toBeTruthy();
});

it.each(["pausing", "stopping"])("renders %s snapshots without enabling another run", async (transition) => {
  status = transition;
  const bar = await open();
  expect(within(bar).getByText(transition)).toBeTruthy();
  expect(within(bar).queryByRole("button", { name: "Resume" })).toBeNull();
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).disabled).toBe(true);
});

it("shows lifecycle status history and preserves inspectable terminal errors after reload", async () => {
  status = "failed";
  events = [
    { kind: "graph_status_changed", status: "pausing" },
    { kind: "graph_failed", error: "drain failed" },
  ];
  await open();
  const timeline = within(screen.getByTestId("event-timeline"));
  expect(timeline.getByText("pausing")).toBeTruthy();
  fireEvent.click(timeline.getByRole("button", { name: "graph_failed" }));
  expect(timeline.getByText("drain failed")).toBeTruthy();
});

it("re-invokes the same completed instance and continues polling its next run", async () => {
  status = "completed";
  await open();
  fireEvent.click(within(screen.getByTestId("control-bar")).getByRole("button", { name: "Close" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "again" } });
  fireEvent.click(screen.getByRole("button", { name: "Invoke" }));
  status = "running";
  finishControl(Response.json({ graph_instance_id: "2", status }));
  await tick();
  expect(posts).toEqual(["/api/graphs/instances/2/invoke"]);
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).disabled).toBe(true);
  status = "completed";
  await tick(2000);
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).disabled).toBe(false);
});
