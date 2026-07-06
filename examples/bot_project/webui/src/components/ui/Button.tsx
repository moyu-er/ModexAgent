// Button.tsx — primary action primitive.
//
// Five visual variants (primary/secondary/ghost/danger/link) and three sizes.
// `loading` swaps the label for a spinner, sets `aria-busy`, and forces the
// button into a disabled state so the user can't fire two clicks. All other
// native button attributes (type, onClick, autoFocus, data-*, aria-*) are
// forwarded onto the underlying <button>.

import type { ButtonHTMLAttributes, ReactNode } from "react";
import { forwardRef } from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "link";
export type ButtonSize = "sm" | "md" | "lg";
export type ButtonShape = "square" | "pill";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  shape?: ButtonShape;
  loading?: boolean;
  children?: ReactNode;
}

const SIZE_CLS: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-xs gap-1.5",
  md: "h-8 px-3 text-sm gap-2",
  lg: "h-10 px-4 text-sm gap-2",
};

const VARIANT_CLS: Record<ButtonVariant, string> = {
  primary:
    "bg-ink text-canvas hover:opacity-90 border border-transparent",
  secondary:
    "bg-canvas-elevated text-ink border border-hairline hover:bg-hairline-soft",
  ghost:
    "bg-transparent text-ink hover:bg-hairline-soft border border-transparent",
  danger:
    "bg-transparent text-error border border-error hover:bg-error/10",
  link:
    "bg-transparent text-link hover:underline border border-transparent px-0 h-auto",
};

const SHAPE_CLS: Record<ButtonShape, string> = {
  square: "rounded-sm",
  pill: "rounded-pill",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "secondary",
    size = "md",
    shape = "square",
    loading = false,
    disabled,
    className,
    children,
    type,
    ...rest
  },
  ref,
) {
  const isDisabled = disabled || loading;
  const cls = [
    "inline-flex items-center justify-center font-medium transition-colors",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-link/30",
    "disabled:cursor-not-allowed disabled:opacity-60",
    VARIANT_CLS[variant],
    SIZE_CLS[size],
    SHAPE_CLS[shape],
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      className={cls}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? (
        <svg
          className="h-3.5 w-3.5 animate-spin"
          viewBox="0 0 16 16"
          fill="none"
          aria-hidden="true"
        >
          <circle
            cx="8"
            cy="8"
            r="6"
            stroke="currentColor"
            strokeOpacity="0.25"
            strokeWidth="2"
          />
          <path
            d="M14 8a6 6 0 0 0-6-6"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
        </svg>
      ) : null}
      {children}
    </button>
  );
});