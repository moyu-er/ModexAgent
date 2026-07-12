import { describe, it, expect, vi } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { ModelEditor } from "./ModelEditor";

const values = {
  default_provider: "DeepSeek",
  default_model: "m1",
  max_context_tokens: 200000,
  providers: [
    {
      key: "deepseek",
      name: "DeepSeek",
      url: "https://x",
      api_key: { has_value: true, hint: "••••" },
      models: [
        {
          name: "m1",
          model: "openai/m1",
          capabilities: ["text"],
          temperature: 0.7,
          max_output_tokens: 50000,
          reasoning_effort: "none",
        },
      ],
    },
  ],
};

describe("ModelEditor", () => {
  it("renders provider and model names (not [object Object])", () => {
    render(<ModelEditor values={values} onChange={() => {}} />);
    // default provider (DeepSeek) is expanded by default → its fields are visible
    expect(screen.getByDisplayValue("DeepSeek")).toBeTruthy(); // provider name input
    expect(screen.getByDisplayValue("openai/m1")).toBeTruthy(); // model routing input
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("Add provider calls onChange with one more provider", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: /Add provider/ }));
    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[0]![0]! as { providers: unknown[] };
    expect(next.providers).toHaveLength(2);
  });

  it("newly added provider card appears at the expected index in the DOM", () => {
    // A real-life onChange propagates the updated `values` back to the parent,
    // which is SettingsView. To exercise the stable-id auto-scroll contract we
    // mirror that interaction with a small wrapper that re-renders ModelEditor
    // with the updated providers list.
    const Wrapper = () => {
      const [v, setV] = useState<Record<string, unknown>>(values);
      return (
        <ModelEditor
          values={v}
          onChange={(next) => setV(next)}
        />
      );
    };
    const { container } = render(<Wrapper />);
    fireEvent.click(screen.getByRole("button", { name: /Add provider/ }));
    // The new card is assigned id `provider-<index>` so auto-scroll can target
    // it; happy-dom doesn't honor scrollIntoView but the stable id is the
    // contract that proves the freshly-added provider is addressable.
    const newCard = container.querySelector("#provider-1");
    expect(newCard).toBeTruthy();
    const originalCard = container.querySelector("#provider-0");
    expect(originalCard).toBeTruthy();
    // And it appears AFTER the original provider-0 card.
    expect(
      originalCard &&
        newCard &&
        originalCard.compareDocumentPosition(newCard) &
          Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("Remove provider removes it after inline confirm", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove provider" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const next = onChange.mock.calls[0]![0]! as { providers: unknown[] };
    expect(next.providers).toHaveLength(0);
  });

  it("Add model appends to the provider's models", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: /Add model/ }));
    const next = onChange.mock.calls[0]![0]! as {
      providers: { models: unknown[] }[];
    };
    expect(next.providers[0]!.models).toHaveLength(2);
  });

  it("default dropdown lists provider/model combos and selecting updates default", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    const select = screen.getByLabelText(/Default model/) as HTMLSelectElement;
    // the one model combo exists as an option
    const optionText = screen.getByText("DeepSeek / m1");
    expect(optionText).toBeTruthy();
    // options are keyed by index into the combos array (robust to names with spaces)
    fireEvent.change(select, { target: { value: "0" } });
    const next = onChange.mock.calls[0]![0]! as {
      default_provider: string;
      default_model: string;
    };
    expect(next.default_provider).toBe("DeepSeek");
    expect(next.default_model).toBe("m1");
  });

  it("editing max_context_tokens calls onChange", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    const numInput = screen.getByDisplayValue("200000") as HTMLInputElement;
    fireEvent.change(numInput, { target: { value: "128000" } });
    const next = onChange.mock.calls[0]![0]! as { max_context_tokens: number };
    expect(next.max_context_tokens).toBe(128000);
  });

  it("capabilities chips toggle membership (enum multi-select)", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    // m1 starts with ["text"]; clicking Image adds it. Chips now render an
    // SVG alongside the label so we locate them by aria-label.
    fireEvent.click(screen.getByRole("button", { name: "Image" }));
    const next = onChange.mock.calls[0]![0]! as {
      providers: { models: { capabilities: string[] }[] }[];
    };
    expect(next.providers[0]!.models[0]!.capabilities).toEqual(
      expect.arrayContaining(["text", "image"]),
    );
  });

  it("capability chips render an inline SVG icon with currentColor stroke", () => {
    const { container } = render(
      <ModelEditor values={values} onChange={() => {}} />,
    );
    // The Text capability chip is the only one selected (aria-pressed=true).
    const textChip = screen.getByRole("button", { name: "Text" });
    expect(textChip.getAttribute("aria-pressed")).toBe("true");
    const chipSvg = textChip.querySelector("svg");
    expect(chipSvg).toBeTruthy();
    expect(chipSvg?.getAttribute("viewBox")).toBe("0 0 16 16");
    expect(chipSvg?.getAttribute("aria-hidden")).toBe("true");
    // The icon's stroke honors currentColor so callers control the tint.
    const svgPaths = chipSvg?.querySelectorAll("path, rect, ellipse, circle") ?? [];
    let usesCurrentColor = false;
    svgPaths.forEach((node) => {
      const stroke = node.getAttribute("stroke");
      const fill = node.getAttribute("fill");
      if (stroke === "currentColor" || fill === "currentColor") {
        usesCurrentColor = true;
      }
    });
    expect(usesCurrentColor).toBe(true);
    // And every capability chip has its own SVG (4 modals × 1 model).
    const card = container.querySelector("#provider-0") as HTMLElement | null;
    expect(card).toBeTruthy();
    const chipsRow = within(card!).getByRole("button", { name: "Text" }).parentElement;
    expect(chipsRow?.querySelectorAll("svg").length ?? 0).toBeGreaterThanOrEqual(4);
  });

  it("reasoning effort dropdown defaults to none and changing updates the model", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    const reasoningSelect = screen.getByLabelText("Reasoning effort") as HTMLSelectElement;
    expect(reasoningSelect.value).toBe("none");
    fireEvent.change(reasoningSelect, { target: { value: "medium" } });
    const next = onChange.mock.calls[0]![0]! as {
      providers: { models: { reasoning_effort: string }[] }[];
    };
    expect(next.providers[0]!.models[0]!.reasoning_effort).toBe("medium");
  });

  it("marks required fields with a star and leaves defaulted numeric fields unmarked", () => {
    render(<ModelEditor values={values} onChange={() => {}} />);
    // required: Provider key label has a star
    expect(screen.getByText("Provider key").closest("label")?.textContent).toMatch(/\*/);
    // optional: Temperature label has no star
    expect(screen.getByText("Temperature").closest("label")?.textContent).not.toMatch(
      /\*/,
    );
  });
});
