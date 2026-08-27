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
      base_url: "https://x",
      interface_format: "openai_compatible",
      api_key: { has_value: true, hint: "••••" },
      models: [
        {
          name: "m1",
          model: "m1",
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
    expect(screen.getByLabelText(/Model identifier/)).toBeTruthy(); // model routing input
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

  it("newly added provider has base_url and interface_format defaults", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: /Add provider/ }));
    const next = onChange.mock.calls[0]![0]! as {
      providers: { base_url: string; interface_format: string }[];
    };
    expect(next.providers[1]!.base_url).toBe("");
    expect(next.providers[1]!.interface_format).toBe("openai_compatible");
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
    const trigger = screen.getByLabelText(/Default model/);
    // The trigger shows the current combo; the panel lists it as an option.
    expect(trigger.textContent).toContain("DeepSeek / m1");
    fireEvent.click(trigger);
    // options are keyed by index into the combos array (robust to names with spaces)
    fireEvent.click(screen.getByRole("option", { name: "DeepSeek / m1" }));
    const next = onChange.mock.calls[0]![0]! as {
      default_provider: string;
      default_model: string;
    };
    expect(next.default_provider).toBe("DeepSeek");
    expect(next.default_model).toBe("m1");
  });

  it("interface format dropdown exists and updates the provider", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    const trigger = screen.getByLabelText("Interface format");
    expect(trigger.textContent).toContain("OpenAI Compatible");
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("option", { name: "Anthropic" }));
    const next = onChange.mock.calls[0]![0]! as {
      providers: { interface_format: string }[];
    };
    expect(next.providers[0]!.interface_format).toBe("anthropic");
  });

  it("interface format dropdown offers openai_response", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    fireEvent.click(screen.getByLabelText("Interface format"));
    fireEvent.click(screen.getByRole("option", { name: "OpenAI Responses" }));
    const next = onChange.mock.calls[0]![0]! as {
      providers: { interface_format: string }[];
    };
    expect(next.providers[0]!.interface_format).toBe("openai_response");
  });

  it("headers editor commits edits into the provider payload (round-trip)", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    // The default provider card is expanded; its headers editor starts empty.
    fireEvent.click(screen.getByRole("button", { name: "Add HTTP headers" }));
    fireEvent.change(screen.getByLabelText("HTTP headers key"), {
      target: { value: "X-Custom" },
    });
    fireEvent.change(screen.getByLabelText("HTTP headers value"), {
      target: { value: "abc" },
    });
    const last = onChange.mock.calls.at(-1)![0]! as {
      providers: { headers: Record<string, string> }[];
    };
    expect(last.providers[0]!.headers).toEqual({ "X-Custom": "abc" });
  });

  it("headers editor strips empty-key rows from the payload", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Add HTTP headers" }));
    fireEvent.change(screen.getByLabelText("HTTP headers key"), {
      target: { value: "X-Custom" },
    });
    fireEvent.change(screen.getByLabelText("HTTP headers value"), {
      target: { value: "abc" },
    });
    // A second row with a blank key and a typed value must not emit an "" key.
    fireEvent.click(screen.getByRole("button", { name: "Add HTTP headers" }));
    fireEvent.change(screen.getAllByLabelText("HTTP headers value")[1]!, {
      target: { value: "orphan" },
    });
    const last = onChange.mock.calls.at(-1)![0]! as {
      providers: { headers: Record<string, string> }[];
    };
    expect(last.providers[0]!.headers).toEqual({ "X-Custom": "abc" });
    expect(Object.keys(last.providers[0]!.headers)).not.toContain("");
  });

  it("endpoint_url input updates the provider", () => {
    const onChange = vi.fn();
    render(<ModelEditor values={values} onChange={onChange} />);
    const input = screen.getByLabelText(
      "Endpoint URL (optional)",
    ) as HTMLInputElement;
    expect(input.placeholder).toBe(
      "Leave empty to auto-derive from the interface format",
    );
    fireEvent.change(input, { target: { value: "https://x/v1/responses" } });
    const next = onChange.mock.calls[0]![0]! as {
      providers: { endpoint_url: string }[];
    };
    expect(next.providers[0]!.endpoint_url).toBe("https://x/v1/responses");
  });

  it("top_p input sets a number and clears back to null", () => {
    const onChange = vi.fn();
    // Stateful wrapper (same pattern as the add-provider card test) so the
    // controlled number input re-renders with the committed value — without
    // it React restores the DOM value and a same-value change never fires.
    const Wrapper = () => {
      const [v, setV] = useState<Record<string, unknown>>(values);
      return (
        <ModelEditor
          values={v}
          onChange={(next) => {
            onChange(next);
            setV(next);
          }}
        />
      );
    };
    render(<Wrapper />);
    const input = screen.getByLabelText("Top P");
    fireEvent.change(input, { target: { value: "0.9" } });
    const set = onChange.mock.calls.at(-1)![0]! as {
      providers: { models: { top_p: number | null }[] }[];
    };
    expect(set.providers[0]!.models[0]!.top_p).toBe(0.9);
    fireEvent.change(input, { target: { value: "" } });
    const cleared = onChange.mock.calls.at(-1)![0]! as {
      providers: { models: { top_p: number | null }[] }[];
    };
    expect(cleared.providers[0]!.models[0]!.top_p).toBeNull();
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
    const trigger = screen.getByLabelText("Reasoning effort");
    expect(trigger.textContent).toContain("none");
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("option", { name: "medium" }));
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

  it("Fetch Models button is always available (no dirty-save precondition)", () => {
    render(<ModelEditor values={values} onChange={() => {}} />);
    const fetchBtn = screen.getByRole("button", { name: /Fetch models/ });
    expect(fetchBtn).toBeTruthy();
    expect(fetchBtn.hasAttribute("disabled")).toBe(false);
  });

  it("Fetch Models button appears even when provider has no key (unsaved draft)", () => {
    const draftValues = {
      ...values,
      providers: [
        {
          ...values.providers[0],
          key: "",
          api_key: { has_value: false },
        },
      ],
    };
    render(
      <ModelEditor values={draftValues} onChange={() => {}} />,
    );
    const fetchBtn = screen.getByRole("button", { name: /Fetch models/ });
    expect(fetchBtn).toBeTruthy();
    expect(fetchBtn.hasAttribute("disabled")).toBe(false);
  });
});
