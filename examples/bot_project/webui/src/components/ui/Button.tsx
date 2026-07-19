// Button.tsx — primary action primitive (Teal & Ember §5.1).
//
// Five visual variants (primary/secondary/ghost/danger/link) and three sizes.
// - primary: THE single primary-CTA styling — gradient + glow lives in the
//   `.btn-primary` CSS class (index.css); never re-implement it per callsite.
// - secondary: the bordered neutral button — elevated bg + hairline, hover
//   border strengthens to the teal-shimmer border-strong.
// - ghost: borderless, hover tint only.
// - danger: semantic danger color, NEVER a gradient.
// `loading` swaps in a spinner, sets `aria-busy`, and forces the button into
// a disabled state so the user can't fire two clicks. All other native button
// attributes (type, onClick, autoFocus, data-*, aria-*) are forwarded onto the
// underlying <button>.

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

// md = 36px min height per §5.1.
const SIZE_CLS: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-xs gap-1.5",
  md: "h-9 px-3.5 text-base gap-2",
  lg: "h-10 px-4 text-base gap-2",
};

// Hover lift (-1px) + press scale (.98) per §4, except the inline link
// variant. Primary gets its motion from `.btn-primary` (with the glow).
const LIFT_PRESS_CLS = "enabled:hover:-translate-y-px enabled:active:scale-[0.98]";

const VARIANT_CLS: Record<ButtonVariant, string> = {
  primary: "btn-primary border border-transparent",
  secondary: `bg-canvas-elevated text-ink border border-hairline hover:border-border-strong ${LIFT_PRESS_CLS}`,
  ghost: `bg-transparent text-body hover:bg-hairline-soft hover:text-ink border border-transparent ${LIFT_PRESS_CLS}`,
  danger: `bg-transparent text-danger border border-danger hover:bg-hairline-soft ${LIFT_PRESS_CLS}`,
  link: "bg-transparent text-brand hover:underline border border-transparent px-0 h-auto",
};

// Radius scale (§4): small buttons radius-sm, default buttons radius-md.
function radiusCls(size: ButtonSize, shape: ButtonShape): string {
  if (shape === "pill") return "rounded-pill";
  return size === "sm" ? "rounded-sm" : "rounded-md";
}

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
    "inline-flex items-center justify-center font-medium transition-[color,background-color,border-color,transform,box-shadow] duration-app ease-out",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand",
    "disabled:cursor-not-allowed disabled:opacity-45",
    VARIANT_CLS[variant],
    SIZE_CLS[size],
    radiusCls(size, shape),
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
