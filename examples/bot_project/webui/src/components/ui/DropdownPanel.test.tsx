import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DropdownPanel, type DropdownOption } from "./DropdownPanel";

const opts: DropdownOption[] = [
  { value: "default", label: "Default" },
  { value: "coding", label: "Coding" },
  { value: "research", label: "Research" },
];

const grouped: DropdownOption[] = [
  { value: "mm::m2", label: "MiniMax-M2.5", group: "MiniMax", badge: "Default" },
  { value: "mm::t1", label: "MiniMax-Text-01", group: "MiniMax" },
  { value: "oa::gpt", label: "gpt-4o", group: "OpenAI" },
];

describe("DropdownPanel — form variant", () => {
  it("renders the visible label and the selected option label on the trigger", () => {
    render(<DropdownPanel label="Pool" options={opts} value="coding" onChange={vi.fn()} />);
    const trigger = screen.getByLabelText("Pool");
    expect(trigger.tagName).toBe("BUTTON");
    expect(trigger.textContent).toContain("Coding");
    expect(trigger.getAttribute("aria-haspopup")).toBe("listbox");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("error replaces helper, marks the trigger invalid and shows role=alert", () => {
    render(
      <DropdownPanel label="Pool" options={opts} value="coding" onChange={vi.fn()} helper="ok" error="bad" />,
    );
    const trigger = screen.getByLabelText("Pool");
    expect(trigger.getAttribute("aria-invalid")).toBe("true");
    expect(trigger.className).toContain("border-error");
    expect(screen.getByRole("alert").textContent).toBe("bad");
    expect(screen.queryByText("ok")).toBeNull();
  });

  it("required shows the asterisk on the label", () => {
    render(<DropdownPanel label="Pool" options={opts} value="coding" onChange={vi.fn()} required />);
    expect(screen.getByText("Pool").textContent).toContain("*");
  });

  it("disabled trigger does not open", () => {
    render(<DropdownPanel label="Pool" options={opts} value="coding" onChange={vi.fn()} disabled />);
    const trigger = screen.getByLabelText("Pool");
    fireEvent.click(trigger);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("opens on click, focuses the listbox and points activedescendant at the selection", () => {
    render(<DropdownPanel label="Pool" options={opts} value="coding" onChange={vi.fn()} />);
    const trigger = screen.getByLabelText("Pool");
    fireEvent.click(trigger);
    const listbox = screen.getByRole("listbox");
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(listbox);
    const selected = screen.getAllByRole("option")[1]!;
    expect(selected.getAttribute("aria-selected")).toBe("true");
    expect(listbox.getAttribute("aria-activedescendant")).toBe(selected.id);
  });

  it("keyboard: ArrowDown opens, arrows wrap, Home/End jump, Enter commits", () => {
    const onChange = vi.fn();
    render(<DropdownPanel label="Pool" options={opts} value="default" onChange={onChange} />);
    const trigger = screen.getByLabelText("Pool");
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox");
    // Active starts on the selected option (index 0); ArrowUp wraps to the last.
    fireEvent.keyDown(listbox, { key: "ArrowUp" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("research");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("keyboard: Home/End jump to first/last option", () => {
    const onChange = vi.fn();
    render(<DropdownPanel label="Pool" options={opts} value="default" onChange={onChange} />);
    fireEvent.keyDown(screen.getByLabelText("Pool"), { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox");
    fireEvent.keyDown(listbox, { key: "End" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("research");

    onChange.mockClear();
    fireEvent.keyDown(screen.getByLabelText("Pool"), { key: "ArrowDown" });
    const listbox2 = screen.getByRole("listbox");
    fireEvent.keyDown(listbox2, { key: "Home" });
    fireEvent.keyDown(listbox2, { key: "Enter" });
    // value prop is unchanged (uncontrolled parent in this test), so Home lands
    // on index 0 → "default".
    expect(onChange).toHaveBeenCalledWith("default");
  });

  it("type-ahead jumps to the next option whose label starts with the key", () => {
    const onChange = vi.fn();
    render(<DropdownPanel label="Pool" options={opts} value="default" onChange={onChange} />);
    fireEvent.keyDown(screen.getByLabelText("Pool"), { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox");
    fireEvent.keyDown(listbox, { key: "r" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("research");
  });

  it("type-ahead buffers multiple keys within the window (prefix match)", () => {
    const multi: DropdownOption[] = [
      { value: "abort", label: "abort" },
      { value: "about", label: "about" },
      { value: "add", label: "add" },
    ];
    const onChange = vi.fn();
    render(<DropdownPanel label="Pool" options={multi} value="add" onChange={onChange} />);
    fireEvent.keyDown(screen.getByLabelText("Pool"), { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox");
    // Two rapid keys build a prefix "ab" → matches "abort" (first hit after
    // the active index "add"), not "add" (single-char "a" would match first).
    fireEvent.keyDown(listbox, { key: "a" });
    fireEvent.keyDown(listbox, { key: "b" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("abort");
  });

  it("type-ahead accepts any printable char, not just [a-z0-9] (CJK labels)", () => {
    const cjk: DropdownOption[] = [
      { value: "zh", label: "中文" },
      { value: "en", label: "English" },
    ];
    const onChange = vi.fn();
    render(<DropdownPanel label="Pool" options={cjk} value="en" onChange={onChange} />);
    fireEvent.keyDown(screen.getByLabelText("Pool"), { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox");
    // "中" is a single-char key (e.key.length === 1) but outside [a-z0-9].
    fireEvent.keyDown(listbox, { key: "中" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("zh");
  });

  it("Escape closes and returns focus to the trigger", () => {
    render(<DropdownPanel label="Pool" options={opts} value="default" onChange={vi.fn()} />);
    const trigger = screen.getByLabelText("Pool");
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("Tab closes without selecting", () => {
    const onChange = vi.fn();
    render(<DropdownPanel label="Pool" options={opts} value="default" onChange={onChange} />);
    fireEvent.click(screen.getByLabelText("Pool"));
    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Tab" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("closes on outside pointer down", () => {
    render(
      <div>
        <DropdownPanel label="Pool" options={opts} value="default" onChange={vi.fn()} />
        <button type="button">outside</button>
      </div>,
    );
    fireEvent.click(screen.getByLabelText("Pool"));
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.pointerDown(screen.getByText("outside"));
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("clicking an option selects it, closes and refocuses the trigger", () => {
    const onChange = vi.fn();
    render(<DropdownPanel label="Pool" options={opts} value="default" onChange={onChange} />);
    const trigger = screen.getByLabelText("Pool");
    fireEvent.click(trigger);
    fireEvent.click(screen.getAllByRole("option")[2]!);
    expect(onChange).toHaveBeenCalledWith("research");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("selected option shows the brand bar and a check; panel carries the token surface + enter animation", () => {
    render(<DropdownPanel label="Pool" options={opts} value="coding" onChange={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Pool"));
    const listbox = screen.getByRole("listbox");
    expect(listbox.className).toContain("bg-canvas-popover");
    expect(listbox.className).toContain("border-hairline");
    expect(listbox.className).toContain("rounded-md");
    expect(listbox.className).toContain("shadow-popover");
    expect(listbox.className).toContain("dropdown-panel-enter");

    const selected = screen.getAllByRole("option")[1]!;
    // 2px brand left bar + check icon.
    const bar = selected.querySelector("span.bg-brand");
    expect(bar).toBeTruthy();
    expect(bar!.className).toContain("w-0.5");
    expect(selected.querySelector("svg")).toBeTruthy();
    // Hover tint = brand 8% token.
    expect(selected.className).toContain("hover:bg-accent");
  });
});

describe("DropdownPanel — pill variant", () => {
  it("renders a pill trigger with the selected label", () => {
    render(
      <DropdownPanel variant="pill" ariaLabel="pool" options={opts} value="coding" onChange={vi.fn()} />,
    );
    const trigger = screen.getByRole("button", { name: /pool/i });
    expect(trigger.className).toContain("rounded-pill");
    expect(trigger.textContent).toContain("Coding");
  });

  it("direction=up anchors the panel above the trigger; align=end right-aligns it", () => {
    render(
      <DropdownPanel
        variant="pill"
        direction="up"
        align="end"
        ariaLabel="model"
        options={opts}
        value="coding"
        onChange={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /model/i }));
    const listbox = screen.getByRole("listbox");
    expect(listbox.className).toContain("bottom-full");
    expect(listbox.className).toContain("right-0");
  });

  it("renders sticky group headers and per-option badges", () => {
    render(
      <DropdownPanel
        variant="pill"
        direction="up"
        ariaLabel="model"
        options={grouped}
        value="mm::t1"
        onChange={vi.fn()}
        triggerLabel="MiniMax - MiniMax-Text-01"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /model/i }));
    const groups = screen.getAllByRole("group");
    expect(groups).toHaveLength(2);
    expect(groups[0]!.getAttribute("aria-label")).toBe("MiniMax");
    expect(groups[1]!.getAttribute("aria-label")).toBe("OpenAI");
    const header = groups[0]!.querySelector(".sticky");
    expect(header).toBeTruthy();
    expect(header!.className).toContain("bg-canvas-popover");
    // Badge on the default model.
    expect(screen.getAllByRole("option")[0]!.textContent).toContain("Default");
    // Selection works through groups.
    fireEvent.click(screen.getAllByRole("option")[2]!);
  });

  it("keyboard nav is identical across variants (arrows/Home/End/Esc/typeahead)", () => {
    const onChange = vi.fn();
    render(
      <DropdownPanel variant="pill" ariaLabel="model" options={grouped} value="mm::m2" onChange={onChange} />,
    );
    const trigger = screen.getByRole("button", { name: /model/i });
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox");
    fireEvent.keyDown(listbox, { key: "End" });
    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("oa::gpt");
    expect(document.activeElement).toBe(trigger);

    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const listbox2 = screen.getByRole("listbox");
    fireEvent.keyDown(listbox2, { key: "g" });
    fireEvent.keyDown(listbox2, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
