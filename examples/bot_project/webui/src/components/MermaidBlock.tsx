import { useEffect, useId, useRef, useState, type FC } from "react";
import { XIcon } from "./ui/icons";

export interface MermaidBlockProps {
  chart: string;
  isDark: boolean;
}

type RenderState =
  | { status: "loading" }
  | { status: "ok"; svg: string }
  | { status: "error"; message: string };

const ZOOM_MIN = 0.3;
const ZOOM_MAX = 6;
const ZOOM_BASE_VMIN = 80; // diagram width at zoom = 1

const clampZoom = (z: number): number =>
  Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));

/**
 * mermaid bakes a fixed pixel width/height and a `max-width` style into the
 * SVG, which pins it to a small size regardless of its container. Strip those
 * so the diagram scales with its wrapper (and therefore with the zoom level).
 */
function stripFixedSvgSizing(svg: string): string {
  return svg.replace(/<svg\b[^>]*>/, (tag) =>
    tag
      .replace(/\swidth="[^"]*"/g, "")
      .replace(/\sheight="[^"]*"/g, "")
      .replace(/\sstyle="[^"]*"/g, "")
      .replace(/<svg/, '<svg style="width:100%;height:auto;max-width:none"'),
  );
}

type View = "diagram" | "source";

/**
 * Renders a ```mermaid fenced block into an interactive diagram viewer.
 *
 * Capabilities:
 *   - Renders the chart to SVG via dynamically-imported mermaid (kept out of
 *     the main bundle; only conversations with a diagram pay the cost).
 *   - "Copy" copies the raw chart source.
 *   - "Source / Diagram" toggles between the rendered SVG and the raw source.
 *   - "Zoom" opens the SVG in a fullscreen, scrollable overlay.
 *   - On render failure, falls back to showing the raw source + error note so
 *     the user never loses the content.
 */
export const MermaidBlock: FC<MermaidBlockProps> = ({ chart, isDark }) => {
  const rawId = useId();
  // mermaid requires a DOM-id-safe, unique render target; strip the React ":r0:" prefix.
  const renderId = `mermaid-${rawId.replace(/[^a-zA-Z0-9]/g, "")}`;
  const [state, setState] = useState<RenderState>({ status: "loading" });
  const [view, setView] = useState<View>("diagram");
  const [zoomed, setZoomed] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [copied, setCopied] = useState(false);

  const panRef = useRef<HTMLDivElement>(null);

  // Reset zoom whenever a new overlay is opened.
  useEffect(() => {
    if (zoomed) setZoom(1);
  }, [zoomed]);

  // Wheel-to-zoom on the overlay. Attached as a non-passive listener so we can
  // preventDefault (React's onWheel is passive and would still scroll/zoom the page).
  useEffect(() => {
    if (!zoomed) return;
    const el = panRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent): void => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
      setZoom((z) => clampZoom(z * factor));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomed]);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const mermaid = await import("mermaid");
        mermaid.default.initialize({
          startOnLoad: false,
          theme: isDark ? "dark" : "default",
          securityLevel: "strict",
        });
        const { svg } = await mermaid.default.render(renderId, chart);
        if (!cancelled) {
          setState({ status: "ok", svg });
        }
      } catch (err) {
        if (!cancelled) {
          setState({
            status: "error",
            message: err instanceof Error ? err.message : String(err),
          });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [chart, isDark, renderId]);

  // Esc closes the fullscreen overlay.
  useEffect(() => {
    if (!zoomed) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") setZoomed(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoomed]);

  const handleCopy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(chart);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard unavailable — ignore
    }
  };

  const svg = state.status === "ok" ? state.svg : null;
  const showSource = view === "source" || state.status === "error";

  return (
    <div className="mb-3 overflow-hidden rounded-lg border border-hairline">
      <div className="flex items-center justify-between gap-2 border-b border-hairline bg-canvas px-4 py-2">
        <span className="text-xs font-medium text-mute">
          mermaid
          {state.status === "error" ? ` · Render failed (${state.message})` : ""}
        </span>
        <div className="flex items-center gap-3">
          {svg && (
            <button
              type="button"
              onClick={() => setView((v) => (v === "diagram" ? "source" : "diagram"))}
              className="text-xs text-mute transition-colors hover:text-ink"
            >
              {view === "diagram" ? "Source" : "Diagram"}
            </button>
          )}
          {svg && (
            <button
              type="button"
              onClick={() => setZoomed(true)}
              className="text-xs text-mute transition-colors hover:text-ink"
            >
              Zoom
            </button>
          )}
          <button
            type="button"
            onClick={handleCopy}
            className="text-xs text-mute transition-colors hover:text-ink"
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </div>

      {showSource ? (
        <pre className="overflow-x-auto bg-canvas p-4 font-mono text-[13px] leading-relaxed text-ink">
          <code>{chart}</code>
        </pre>
      ) : svg ? (
        <div
          className="flex justify-center overflow-x-auto bg-canvas p-4"
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      ) : (
        <div className="flex items-center justify-center bg-canvas p-4 text-xs text-mute">
          Rendering diagram…
        </div>
      )}

      {zoomed && svg && (
        <div
          className="fixed inset-0 z-50 flex flex-col bg-black/80"
          role="dialog"
          aria-modal="true"
          aria-label="mermaid diagram"
          onClick={() => setZoomed(false)}
        >
          <div className="flex items-center justify-between px-4 py-2 text-white/90">
            <div className="flex items-center gap-2 text-xs">
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setZoom((z) => clampZoom(z / 1.1));
                }}
                className="rounded bg-white/10 px-2 py-1 hover:bg-white/20"
              >
                −
              </button>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setZoom(1);
                }}
                className="hover:underline"
              >
                {Math.round(zoom * 100)}%
              </button>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setZoom((z) => clampZoom(z * 1.1));
                }}
                className="rounded bg-white/10 px-2 py-1 hover:bg-white/20"
              >
                +
              </button>
              <span className="ml-2 text-white/40">Scroll to zoom · drag scrollbar to pan</span>
            </div>
            <button
              type="button"
              onClick={() => setZoomed(false)}
              className="flex items-center gap-1 text-sm text-white/80 transition-colors hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
            >
              <XIcon />
              Close
            </button>
          </div>
          <div
            ref={panRef}
            className="flex-1 overflow-auto p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <div
              className="mx-auto bg-white p-6 dark:bg-zinc-900"
              style={{ width: `${ZOOM_BASE_VMIN * zoom}vmin` }}
              dangerouslySetInnerHTML={{ __html: stripFixedSvgSizing(svg) }}
            />
          </div>
        </div>
      )}
    </div>
  );
};
