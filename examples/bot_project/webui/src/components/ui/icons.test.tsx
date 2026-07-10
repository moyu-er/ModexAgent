import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  PlusIcon,
  TrashIcon,
  EditIcon,
  EyeIcon,
  EyeOffIcon,
  CopyIcon,
  CheckIcon,
  XIcon,
  UploadIcon,
  SearchIcon,
  WarningIcon,
  FolderIcon,
  FolderOpenIcon,
  HomeIcon,
  SettingsGearIcon,
  RefreshIcon,
  DefaultStarIcon,
  TextIcon,
  ImageIcon,
  VideoIcon,
  AudioIcon,
  SpinnerIcon,
  CircleRingIcon,
} from "./icons";

const all = [
  ["ChevronDownIcon", ChevronDownIcon],
  ["ChevronLeftIcon", ChevronLeftIcon],
  ["ChevronRightIcon", ChevronRightIcon],
  ["PlusIcon", PlusIcon],
  ["TrashIcon", TrashIcon],
  ["EditIcon", EditIcon],
  ["EyeIcon", EyeIcon],
  ["EyeOffIcon", EyeOffIcon],
  ["CopyIcon", CopyIcon],
  ["CheckIcon", CheckIcon],
  ["XIcon", XIcon],
  ["UploadIcon", UploadIcon],
  ["SearchIcon", SearchIcon],
  ["WarningIcon", WarningIcon],
  ["FolderIcon", FolderIcon],
  ["FolderOpenIcon", FolderOpenIcon],
  ["HomeIcon", HomeIcon],
  ["SettingsGearIcon", SettingsGearIcon],
  ["RefreshIcon", RefreshIcon],
  ["DefaultStarIcon", DefaultStarIcon],
  ["TextIcon", TextIcon],
  ["ImageIcon", ImageIcon],
  ["VideoIcon", VideoIcon],
  ["AudioIcon", AudioIcon],
  ["SpinnerIcon", SpinnerIcon],
  ["CircleRingIcon", CircleRingIcon],
] as const;

describe("icons", () => {
  it.each(all)("%s renders a 16x16 viewBox svg with aria-hidden", (_name, Comp) => {
    const { container } = render(<Comp />);
    const svg = container.querySelector("svg");
    expect(svg).toBeTruthy();
    expect(svg?.getAttribute("viewBox")).toBe("0 0 16 16");
    expect(svg?.getAttribute("aria-hidden")).toBe("true");
    // The svg should be queryable as an SVG element
    expect(svg?.tagName).toBe("svg");
  });

  it("forwards className and rest props to the svg", () => {
    const { container } = render(
      <ChevronDownIcon className="text-error h-6 w-6" data-foo="bar" />,
    );
    const svg = container.querySelector("svg") as SVGSVGElement;
    const className = svg.getAttribute("class") ?? "";
    expect(className).toContain("text-error");
    expect(className).toContain("h-6");
    expect(svg.getAttribute("data-foo")).toBe("bar");
  });

  it("ChevronDownIcon with open=true rotates 180deg", () => {
    const { container: openC } = render(<ChevronDownIcon open />);
    const openCls = openC.querySelector("svg")?.getAttribute("class") ?? "";
    expect(openCls).toContain("rotate-180");
    const { container: closedC } = render(<ChevronDownIcon />);
    const closedCls = closedC.querySelector("svg")?.getAttribute("class") ?? "";
    expect(closedCls).not.toContain("rotate-180");
  });

  it("uses currentColor so callers control color via className", () => {
    const { container } = render(<CheckIcon />);
    const path = container.querySelector("svg path");
    expect(path?.getAttribute("stroke")).toBe("currentColor");
  });
});