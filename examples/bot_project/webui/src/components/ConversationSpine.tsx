import {
  useEffect,
  useRef,
  useState,
  type FC,
  type RefObject,
} from "react";
import { useT } from "../i18n";

/**
 * ConversationSpine — a right-margin navigation rail.
 *
 * One dot per user question, positioned proportionally to the question's
 * vertical place in the scrollable content (not evenly spaced — the dots
 * mirror where the questions actually sit, so a long assistant answer pushes
 * the next dot further down the rail). Click a dot to scroll that question
 * into the center of the viewport. The dot nearest the current viewport
 * center is highlighted, answering "which question am I reading the answer
 * to right now?".
 *
 * The rail is an editorial margin, not a media scrubber: a 1px hairline, tiny
 * quiet dots that only come alive on hover/active, and a mono-font tooltip
 * peeking left with the question's first words. No numbering — the position
 * IS the information; numbering would be templated decoration.
 */

export interface SpineAnchor {
  /** Stable message id — must match the `msg-${id}` DOM id on the bubble. */
  id: string;
  /** Short preview shown in the hover tooltip. */
  preview: string;
}

export interface ConversationSpineProps {
  /** The scroll container (overflow-y-auto element). */
  scrollRef: RefObject<HTMLDivElement | null>;
  /** The inner content div inside the scroll container. */
  contentRef: RefObject<HTMLDivElement | null>;
  /** User messages, in conversation order. */
  anchors: SpineAnchor[];
}

// ── Pure helpers (unit-testable without a DOM layout engine) ───────────────

/**
 * Index of the anchor whose ratio is nearest the viewport center, or -1.
 *
 * @param ratios   Per-anchor vertical-center ratio (0..1 of scrollHeight);
 *                 non-finite entries are skipped.
 * @param viewportCenterRatio  (scrollTop + clientHeight/2) / scrollHeight.
 */
export function computeActiveIndex(
  ratios: number[],
  viewportCenterRatio: number,
): number {
  if (ratios.length === 0) return -1;
  let best = -1;
  let bestDist = Infinity;
  for (let i = 0; i < ratios.length; i++) {
    const r = ratios[i];
    if (typeof r !== "number" || !Number.isFinite(r)) continue;
    const dist = Math.abs(r - viewportCenterRatio);
    if (dist < bestDist) {
      bestDist = dist;
      best = i;
    }
  }
  return best;
}

/**
 * Scroll target (scrollTop) that places an anchor's center in the viewport
 * center. Clamped to [0, maxScroll].
 */
export function jumpTargetTop(
  ratio: number,
  scrollHeight: number,
  clientHeight: number,
): number {
  if (!Number.isFinite(ratio) || scrollHeight <= 0) return 0;
  const center = ratio * scrollHeight;
  const target = center - clientHeight / 2;
  const max = Math.max(0, scrollHeight - clientHeight);
  return Math.min(max, Math.max(0, target));
}

// ── DOM measurement ─────────────────────────────────────────────────────────

/**
 * Measure each anchor's vertical-center ratio within the scrollable content.
 *
 * Uses getBoundingClientRect so it stays correct regardless of which ancestor
 * is the offsetParent. `(el.top - content.top)` is the anchor's scroll-absolute
 * offset (scroll-invariant, since both rects move together); adding half the
 * anchor's height gives its center; dividing by scrollHeight gives the ratio.
 *
 * Returns NaN for anchors whose DOM node isn't mounted yet (e.g. mid-stream);
 * callers skip non-finite ratios.
 */
function measureRatios(
  anchors: SpineAnchor[],
  scrollEl: HTMLDivElement | null,
  contentEl: HTMLDivElement | null,
): number[] {
  if (!scrollEl || !contentEl || scrollEl.scrollHeight === 0) {
    return anchors.map(() => NaN);
  }
  const contentTop = contentEl.getBoundingClientRect().top;
  const height = scrollEl.scrollHeight;
  return anchors.map((a) => {
    const el = document.getElementById(`msg-${a.id}`);
    if (!el) return NaN;
    const r = el.getBoundingClientRect();
    const centerAbsolute = r.top - contentTop + r.height / 2;
    return height > 0 ? centerAbsolute / height : 0;
  });
}

function viewportCenterRatio(scrollEl: HTMLDivElement | null): number {
  if (!scrollEl || scrollEl.scrollHeight === 0) return 0;
  return (
    (scrollEl.scrollTop + scrollEl.clientHeight / 2) /
    scrollEl.scrollHeight
  );
}

// ── Component ───────────────────────────────────────────────────────────────

export const ConversationSpine: FC<ConversationSpineProps> = ({
  scrollRef,
  contentRef,
  anchors,
}) => {
  const t = useT();
  const [ratios, setRatios] = useState<number[]>([]);
  const [active, setActive] = useState<number>(-1);
  const rafRef = useRef<number | null>(null);
  const measureRafRef = useRef<number | null>(null);

  // Re-measure when anchors change or the content height changes (streaming,
  // image load, mermaid render, etc.). ResizeObserver covers the content div;
  // the window resize covers viewport-driven layout shifts.
  useEffect(() => {
    const remeasure = (): void => {
      const next = measureRatios(anchors, scrollRef.current, contentRef.current);
      setRatios(next);
      setActive(computeActiveIndex(next, viewportCenterRatio(scrollRef.current)));
    };
    // First synchronous measure so newly-added anchors appear immediately
    // instead of waiting for the next frame.
    remeasure();
    const content = contentRef.current;
    let ro: ResizeObserver | undefined;
    if (content && typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => {
        // Streaming deltas change content height at high frequency: rAF
        // coalesces multiple callbacks in the same frame, avoiding N DOM
        // queries + setState per token (the root cause of lag in long chats).
        if (measureRafRef.current != null) return;
        measureRafRef.current = requestAnimationFrame(() => {
          measureRafRef.current = null;
          remeasure();
        });
      });
      ro.observe(content);
    }
    window.addEventListener("resize", remeasure);
    return (): void => {
      ro?.disconnect();
      window.removeEventListener("resize", remeasure);
      if (measureRafRef.current != null) {
        cancelAnimationFrame(measureRafRef.current);
        measureRafRef.current = null;
      }
    };
  }, [anchors, scrollRef, contentRef]);

  // Track the active dot on scroll — rAF-throttled so a fast scroll fling
  // doesn't thrash React state on every wheel event.
  useEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll) return;
    const onScroll = (): void => {
      if (rafRef.current != null) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        setActive(computeActiveIndex(ratios, viewportCenterRatio(scrollRef.current)));
      });
    };
    scroll.addEventListener("scroll", onScroll, { passive: true });
    return (): void => {
      scroll.removeEventListener("scroll", onScroll);
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [ratios, scrollRef]);

  const jumpTo = (i: number): void => {
    const scroll = scrollRef.current;
    const r = ratios[i];
    if (!scroll || typeof r !== "number" || !Number.isFinite(r)) return;
    scroll.scrollTo({
      top: jumpTargetTop(r, scroll.scrollHeight, scroll.clientHeight),
      behavior: "smooth",
    });
  };

  if (anchors.length === 0) return null;

  return (
    <div
      className="absolute bottom-6 right-3 top-6 z-10 hidden w-5 md:flex md:justify-center"
      aria-label={t("chat.conversationNav")}
      role="navigation"
    >
      {/* Hairline rail */}
      <div className="absolute bottom-0 left-1/2 top-0 w-px -translate-x-1/2 bg-hairline" />
      {anchors.map((a, i) => {
        const r = ratios[i];
        const finiteR = typeof r === "number" && Number.isFinite(r) ? r : null;
        // Clamp so dots never escape the rail's visible bounds.
        const topPct = finiteR !== null ? Math.min(98, Math.max(2, finiteR * 100)) : 0;
        const isActive = i === active;
        return (
          <button
            key={a.id}
            type="button"
            onClick={(): void => jumpTo(i)}
            className="group absolute left-1/2 flex h-5 w-5 -translate-x-1/2 -translate-y-1/2 items-center justify-center"
            style={{ top: `${topPct}%`, visibility: finiteR !== null ? "visible" : "hidden" }}
            aria-label={t("chat.jumpToQuestion", { preview: a.preview })}
            aria-current={isActive ? "true" : undefined}
          >
            <span
              className={`block rounded-full transition-all duration-150 ${
                isActive
                  ? "h-2 w-2 bg-brand ring-2 ring-brand"
                  : "h-1.5 w-1.5 bg-mute group-hover:h-2 group-hover:bg-brand"
              }`}
            />
            {/* Mono margin-note tooltip — peeks left into the content gutter. */}
            <span
              className="pointer-events-none absolute right-full mr-2 max-w-[220px] truncate rounded border border-hairline bg-canvas-elevated px-2 py-1 font-mono text-xs text-body opacity-0 shadow-sm transition-opacity duration-150 group-hover:opacity-100"
            >
              {a.preview}
            </span>
          </button>
        );
      })}
    </div>
  );
};
