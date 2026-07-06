// icons.tsx — unified SVG glyph library.
//
// Every icon is a functional component returning a 16x16 viewBox <svg> that
// uses `currentColor` for stroke/fill so callers control color via Tailwind
// classes. All icons are aria-hidden by default and accept an optional className
// plus rest props that get spread onto the underlying <svg> for callers that
// need to override size etc.

import type { SVGProps } from "react";

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, "children"> {
  className?: string;
}

const baseProps = {
  xmlns: "http://www.w3.org/2000/svg",
  viewBox: "0 0 16 16",
  fill: "none",
  "aria-hidden": true,
} as const;

const STROKE = {
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

export function ChevronDownIcon({ open, className, ...rest }: IconProps & { open?: boolean }) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 transition-transform ${open ? "rotate-180" : ""} ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path d="M4 6l4 4 4-4" {...STROKE} />
    </svg>
  );
}

export function ChevronLeftIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path d="M10 4l-4 4 4 4" {...STROKE} />
    </svg>
  );
}

export function ChevronRightIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path d="M6 4l4 4-4 4" {...STROKE} />
    </svg>
  );
}

export function PlusIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-3.5 w-3.5 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path d="M8 3v10M3 8h10" {...STROKE} />
    </svg>
  );
}

export function TrashIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M3 4h10M6.5 4V2.5h3V4M5 4l.5 8.5a1 1 0 0 0 1 .9h3a1 1 0 0 0 1-.9L11 4M6.5 7v4M9.5 7v4"
        stroke="currentColor"
        strokeWidth={1.3}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function EditIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M11.5 2.5l2 2-7.5 7.5h-2v-2l7.5-7.5z"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M3 13.5h10" stroke="currentColor" strokeWidth={1.4} strokeLinecap="round" />
    </svg>
  );
}

export function EyeIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8s-2.5 4.5-6.5 4.5S1.5 8 1.5 8z"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinejoin="round"
      />
      <circle cx="8" cy="8" r="2" stroke="currentColor" strokeWidth={1.4} />
    </svg>
  );
}

export function EyeOffIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M2 2l12 12M5.5 5.5C3 7 1.5 8 1.5 8s2.5 4.5 6.5 4.5c1 0 1.9-.2 2.7-.5M9 4.1A7 7 0 0 1 14.5 8s-.8 1.4-2.3 2.7"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function CopyIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <rect
        x="5"
        y="5"
        width="8"
        height="9"
        rx="1.2"
        stroke="currentColor"
        strokeWidth={1.4}
      />
      <path
        d="M3 11V3.5A1.5 1.5 0 0 1 4.5 2H10"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinecap="round"
      />
    </svg>
  );
}

export function CheckIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path d="M3 8.5l3 3 7-7" {...STROKE} strokeWidth={1.8} />
    </svg>
  );
}

export function XIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path d="M4 4l8 8M12 4l-8 8" {...STROKE} />
    </svg>
  );
}

export function UploadIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path d="M8 11V3M5 6l3-3 3 3" {...STROKE} />
      <path d="M3 13h10" {...STROKE} />
    </svg>
  );
}

export function SearchIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <circle cx="7" cy="7" r="4" stroke="currentColor" strokeWidth={1.5} />
      <path d="M10 10l3.5 3.5" {...STROKE} />
    </svg>
  );
}

export function WarningIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M8 2.5L1.5 13h13L8 2.5z"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinejoin="round"
      />
      <path d="M8 6.5v3" stroke="currentColor" strokeWidth={1.4} strokeLinecap="round" />
      <circle cx="8" cy="11.5" r="0.6" fill="currentColor" />
    </svg>
  );
}

export function FolderIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M2 5a1.5 1.5 0 0 1 1.5-1.5h3L8 5h4.5A1.5 1.5 0 0 1 14 6.5v5A1.5 1.5 0 0 1 12.5 13h-9A1.5 1.5 0 0 1 2 11.5v-6.5z"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function FolderOpenIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M2 6.5A1.5 1.5 0 0 1 3.5 5h3L8 6.5h4.5A1.5 1.5 0 0 1 14 8v.5H2v-2z"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinejoin="round"
      />
      <path
        d="M2 8.5h10.4a1 1 0 0 1 .95 1.32l-.95 3.16A1.5 1.5 0 0 1 11 14H3.5A1.5 1.5 0 0 1 2 12.5V8.5z"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function HomeIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M3 7l5-4 5 4v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7z"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinejoin="round"
      />
      <path d="M6.5 14V9.5h3V14" stroke="currentColor" strokeWidth={1.4} strokeLinejoin="round" />
    </svg>
  );
}

/** Settings gear icon. Path adapted from Bootstrap Icons `bi-gear-fill` (MIT)
 *  for a clearly mechanical, non-sun-like gear at 16x16. */
export function SettingsGearIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M9.405 1.05c-.413-1.4-2.397-1.4-2.81 0l-.1.34a1.464 1.464 0 0 1-2.105.872l-.31-.17c-1.283-.698-2.686.705-1.987 1.987l.169.311c.446.82.023 1.841-.872 2.105l-.34.1c-1.4.413-1.4 2.397 0 2.81l.34.1a1.464 1.464 0 0 1 .872 2.105l-.17.31c-.698 1.283.705 2.686 1.987 1.987l.311-.169a1.464 1.464 0 0 1 2.105.872l.1.34c.413 1.4 2.397 1.4 2.81 0l.1-.34a1.464 1.464 0 0 1 2.105-.872l.31.17c1.283.698 2.686-.705 1.987-1.987l-.169-.311a1.464 1.464 0 0 1 .872-2.105l.34-.1c1.4-.413 1.4-2.397 0-2.81l-.34-.1a1.464 1.464 0 0 1-.872-2.105l.17-.31c.698-1.283-.705-2.686-1.987-1.987l-.311.169a1.464 1.464 0 0 1-2.105-.872zM8 10.93a2.929 2.929 0 1 1 0-5.86 2.929 2.929 0 0 1 0 5.858z"
        fill="currentColor"
      />
    </svg>
  );
}

export function RefreshIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M3 8a5 5 0 0 1 9.2-2.7M13 8a5 5 0 0 1-9.2 2.7"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinecap="round"
      />
      <path d="M12 2.5v3h-3M4 13.5v-3h3" {...STROKE} />
    </svg>
  );
}

export function DefaultStarIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M8 2l1.8 3.7 4.1.6-3 2.9.7 4.1L8 11.3 4.4 13.3l.7-4.1-3-2.9 4.1-.6L8 2z"
        stroke="currentColor"
        strokeWidth={1.3}
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Small circular spinner for todo/status. */
export function SpinnerIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-3.5 w-3.5 shrink-0 animate-spin ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <circle
        cx="8"
        cy="8"
        r="6"
        stroke="currentColor"
        strokeOpacity="0.25"
        strokeWidth="1.5"
      />
      <path
        d="M14 8a6 6 0 0 0-6-6"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** Hollow ring for pending todo status. */
export function CircleRingIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-3.5 w-3.5 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <circle
        cx="8"
        cy="8"
        r="6"
        stroke="currentColor"
        strokeWidth="1.5"
        opacity="0.5"
      />
    </svg>
  );
}

/* Capability icons — used by capability selector chips (ModelEditor).
 * Drawn as 12x12-equivalent icons that visually correspond to text/image/
 * video/audio modalities. Sizes are controlled by the caller via className.
 */

export function TextIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-3.5 w-3.5 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M3.5 3h9M8 3v10M5.5 13h5"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function ImageIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-3.5 w-3.5 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <rect
        x="2.5"
        y="3.5"
        width="11"
        height="9"
        rx="1.2"
        stroke="currentColor"
        strokeWidth={1.4}
      />
      <circle cx="6" cy="7" r="1" stroke="currentColor" strokeWidth={1.4} />
      <path
        d="M3 11l3-2.5 2.5 2 2-1.5L13 11"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function VideoIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-3.5 w-3.5 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <rect
        x="2"
        y="4"
        width="12"
        height="8"
        rx="1.4"
        stroke="currentColor"
        strokeWidth={1.4}
      />
      <path d="M6.5 6.5l3.5 1.5-3.5 1.5v-3z" fill="currentColor" />
    </svg>
  );
}

/**
 * Compact chevron for inline expand/collapse toggles (e.g. ReasoningBlock,
 * SessionTree, ToolTraceCard). Caller controls the collapsed/expanded state
 * via the ``open`` prop. Sized h-3 w-3 to match the small ``text-[10px]``
 * glyphs that previously lived in JSX.
 */
export function ChevronToggleIcon({
  open,
  className,
  ...rest
}: IconProps & { open?: boolean }) {
  return (
    <svg
      className={`h-3 w-3 shrink-0 transition-transform ${open ? "rotate-90" : ""} ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path d="M5.5 4l4 4-4 4" {...STROKE} />
    </svg>
  );
}

/** Plain sheet-of-paper glyph for non-directory browse entries. */
export function FileIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-4 w-4 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M4 2.5h6l3 3v8a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1z"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinejoin="round"
      />
      <path d="M10 2.5v3h3" {...STROKE} strokeWidth={1.3} />
    </svg>
  );
}

/** Wrench glyph for the tool trace header — standalone Tools icon, not paired.
 *  Path adapted from Bootstrap Icons (MIT) for readability at 14x14. */
export function WrenchIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-3.5 w-3.5 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M.102 2.223A3.004 3.004 0 0 0 3.78 5.897l6.341 6.252A3.003 3.003 0 0 0 13 16a3 3 0 1 0-.851-5.878L5.897 3.781A3.004 3.004 0 0 0 2.223.1l2.141 2.142L4 4l-1.757.364z"
        fill="currentColor"
      />
    </svg>
  );
}

export function AudioIcon({ className, ...rest }: IconProps) {
  return (
    <svg
      className={`h-3.5 w-3.5 shrink-0 ${className ?? ""}`.trim()}
      {...baseProps}
      {...rest}
    >
      <path
        d="M6 12V4l5-1v8"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <ellipse
        cx="4.5"
        cy="12"
        rx="1.5"
        ry="1.5"
        stroke="currentColor"
        strokeWidth={1.4}
      />
      <ellipse
        cx="11"
        cy="11"
        rx="1.7"
        ry="1.7"
        stroke="currentColor"
        strokeWidth={1.4}
      />
    </svg>
  );
}