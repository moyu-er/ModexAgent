import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ModelSelector } from "./ModelSelector";
import type { ModelChoice } from "../lib/api";

const models: ModelChoice[] = [
  { provider_name: "MiniMax", model_name: "MiniMax-M2.5", default: true },
  { provider_name: "MiniMax", model_name: "MiniMax-Text-01", default: false },
  { provider_name: "OpenAI", model_name: "gpt-4o", default: false },
];

describe("ModelSelector", () => {
  const onChange = vi.fn();

  beforeEach(() => {
    onChange.mockClear();
    document.documentElement.classList.remove("dark");
  });

  it("renders a compact trigger showing the selected model", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "OpenAI", model: "gpt-4o" }}
        onChange={onChange}
      />,
    );

    const trigger = screen.getByRole("button", { name: /Model/i });
    expect(trigger.textContent).toContain("OpenAI - gpt-4o");
    expect(trigger.getAttribute("aria-haspopup")).toBe("listbox");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("opens on trigger click and closes on a second click", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    const trigger = screen.getByRole("button", { name: /Model/i });
    fireEvent.click(trigger);

    expect(screen.getByRole("listbox")).toBeTruthy();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getAllByRole("option")).toHaveLength(3);

    fireEvent.click(trigger);
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("groups options by provider", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Model/i }));
    const groups = screen.getAllByRole("group");

    expect(groups).toHaveLength(2);
    expect(groups[0]!.getAttribute("aria-label")).toBe("MiniMax");
    expect(groups[1]!.getAttribute("aria-label")).toBe("OpenAI");
  });

  it("selects a model and closes the dropdown", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Model/i }));
    const options = screen.getAllByRole("option");

    fireEvent.click(options[2]!);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith({
      provider: "OpenAI",
      model: "gpt-4o",
    });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("highlights the active item and marks the default model", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-Text-01" }}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Model/i }));
    const options = screen.getAllByRole("option");

    expect(options[1]!.getAttribute("aria-selected")).toBe("true");
    expect(options[0]!.textContent).toContain("Default");
  });

  it("supports keyboard navigation and selection", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Model/i }));
    const listbox = screen.getByRole("listbox");
    const options = screen.getAllByRole("option");

    // Focus sits on the listbox; the active option is the activedescendant.
    expect(document.activeElement).toBe(listbox);
    expect(listbox.getAttribute("aria-activedescendant")).toBe(options[0]!.id);

    fireEvent.keyDown(listbox, { key: "ArrowDown" });
    expect(listbox.getAttribute("aria-activedescendant")).toBe(options[1]!.id);

    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith({
      provider: "MiniMax",
      model: "MiniMax-Text-01",
    });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("Tab closes the dropdown without selecting", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    const trigger = screen.getByRole("button", { name: /Model/i });
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Tab" });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("closes on Escape and returns focus to the trigger", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    const trigger = screen.getByRole("button", { name: /Model/i });
    fireEvent.click(trigger);

    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("closes when clicking outside", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Model/i }));
    expect(screen.getByRole("listbox")).toBeTruthy();

    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("uses the shared popover surface with brand-tint selection", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Model/i }));
    const listbox = screen.getByRole("listbox");
    const selected = screen.getAllByRole("option")[0]!;

    expect(listbox.classList.contains("bg-canvas-popover")).toBe(true);
    expect(listbox.classList.contains("border-hairline")).toBe(true);
    expect(listbox.classList.contains("dropdown-panel-enter")).toBe(true);
    expect(selected.querySelector("span.bg-brand")).toBeTruthy();
    expect(selected.classList.contains("hover:bg-accent")).toBe(true);
  });
});
