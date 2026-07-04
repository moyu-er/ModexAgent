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
    const options = screen.getAllByRole("option");

    expect(document.activeElement).toBe(options[0]!);

    fireEvent.keyDown(options[0]!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(options[1]!);

    fireEvent.keyDown(options[1]!, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith({
      provider: "MiniMax",
      model: "MiniMax-Text-01",
    });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("traps Tab and Shift+Tab while the dropdown is open", () => {
    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    const trigger = screen.getByRole("button", { name: /Model/i });
    fireEvent.click(trigger);
    const options = screen.getAllByRole("option");

    expect(document.activeElement).toBe(options[0]!);

    fireEvent.keyDown(options[0]!, { key: "Tab" });
    expect(document.activeElement).toBe(options[1]!);

    fireEvent.keyDown(options[1]!, { key: "Tab" });
    expect(document.activeElement).toBe(options[2]!);

    fireEvent.keyDown(options[2]!, { key: "Tab" });
    expect(document.activeElement).toBe(trigger);

    fireEvent.keyDown(trigger, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(options[2]!);
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

    const options = screen.getAllByRole("option");
    fireEvent.keyDown(options[0]!, { key: "Escape" });

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

    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("applies dark-mode token classes when .dark is present", () => {
    document.documentElement.classList.add("dark");

    render(
      <ModelSelector
        models={models}
        value={{ provider: "MiniMax", model: "MiniMax-M2.5" }}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Model/i }));
    const listbox = screen.getByRole("listbox");
    const option = screen.getAllByRole("option")[0]!;

    expect(listbox.classList.contains("bg-dropdown-bg")).toBe(true);
    expect(listbox.classList.contains("border-card-border")).toBe(true);
    expect(option.classList.contains("hover:bg-dropdown-hover")).toBe(true);
  });
});
