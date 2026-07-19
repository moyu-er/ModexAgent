// IconButton.tsx — circular icon-only button.
//
// The `label` prop is mandatory and used as both aria-label and title so the
// button is accessible by name even though its only visible content is the
// SVG. `icon` is a ReactNode so callers can pass any SVG (usually one of the
// components in icons.tsx). Size mapping matches the Inter circular icon
// button: sm 28px / md 32px.

import type { ButtonHTMLAttributes, ReactNode } from "react";
import { forwardRef } from "react";

export type IconButtonVariant = "ghost" | "primary" | "secondary" | "danger";
export type IconButtonSize = "sm" | "md";

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: ReactNode;
  label: string;
  variant?: IconButtonVariant;
  size?: IconButtonSize;
}

const SIZE_CLS: Record<IconButtonSize, string> = {
  sm: "h-7 w-7",
  md: "h-8 w-8",
};

const VARIANT_CLS: Record<IconButtonVariant, string> = {
  primary: "btn-primary border border-transparent",
  secondary:
    "bg-canvas-elevated text-ink border border-hairline hover:border-border-strong",
  ghost:
    "bg-transparent text-body hover:bg-hairline-soft hover:text-ink border border-transparent",
  danger:
    "bg-transparent text-danger hover:bg-hairline-soft border border-transparent",
};

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  {
    icon,
    label,
    variant = "secondary",
    size = "md",
    disabled,
    className,
    type,
    "aria-label": ariaLabelProp,
    title: titleProp,
    ...rest
  },
  ref,
) {
  const cls = [
    "inline-flex items-center justify-center rounded-full transition-[color,background-color,border-color,transform,box-shadow] duration-app ease-out",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand",
    "disabled:cursor-not-allowed disabled:opacity-45",
    VARIANT_CLS[variant],
    SIZE_CLS[size],
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      className={cls}
      disabled={disabled}
      aria-label={ariaLabelProp ?? label}
      title={titleProp ?? label}
      {...rest}
    >
      {icon}
    </button>
  );
});