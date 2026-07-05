import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
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
    const select = screen.getByRole("combobox") as HTMLSelectElement;
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
    // m1 starts with ["text"]; clicking Image adds it
    fireEvent.click(screen.getByRole("button", { name: "Image" }));
    const next = onChange.mock.calls[0]![0]! as {
      providers: { models: { capabilities: string[] }[] }[];
    };
    expect(next.providers[0]!.models[0]!.capabilities).toEqual(
      expect.arrayContaining(["text", "image"]),
    );
  });

  it("marks required fields with a star and leaves defaulted numeric fields unmarked", () => {
    render(<ModelEditor values={values} onChange={() => {}} />);
    // required: Key label has a star
    expect(screen.getByText("Key").closest("label")?.textContent).toMatch(/\*/);
    // optional: Temperature label has no star
    expect(screen.getByText("Temperature").closest("label")?.textContent).not.toMatch(
      /\*/,
    );
  });
});
