import type { ReactNode } from "react";

/**
 * ViewCrossfade — wraps the chat↔settings view swap so the incoming view
 * fades+rises in over ~220ms (DESIGN.md §8). The `key` forces React to
 * remount the wrapper on each view change: the outgoing view unmounts
 * immediately, so only the incoming view animates (fade-in + rise) — there
 * is no simultaneous exit animation. Transform + opacity only; zeroed by
 * prefers-reduced-motion (global guard in index.css).
 *
 * Kept as a structural primitive rather than inline markup so the crossfade
 * seam is unit-testable in isolation.
 */
export interface ViewCrossfadeProps {
  /** Stable value per view; changing it remounts the wrapper and replays the
   * enter animation. Typically the view name ("chat" | "settings"). */
  viewKey: string;
  children: ReactNode;
}

export function ViewCrossfade({ viewKey, children }: ViewCrossfadeProps) {
  return (
    <div key={viewKey} className="view-crossfade-enter flex min-h-0 flex-1 flex-col">
      {children}
    </div>
  );
}
