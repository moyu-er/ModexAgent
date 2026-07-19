import { useEffect, useRef, type FC } from "react";
import { useT, type MessageKey } from "../i18n";
import { mountBootParticles, type BootParticlesHandle } from "../lib/particles";

interface BootScreenProps {
  attempts: number;
  lastError: string | null;
  /** True once the backend is ready: the engine disperses and the screen fades out. */
  exiting?: boolean;
  onRetry: () => void;
}

/** Staged status copy while waiting for the backend (DESIGN.md §7). */
export function bootStageKey(attempts: number): MessageKey {
  if (attempts <= 1) return "boot.starting";
  if (attempts <= 10) return "boot.connecting";
  if (attempts <= 30) return "boot.stillStarting";
  return "boot.takingLong";
}

/** Show the error card only after the backend has had a fair chance (~24s). */
const ERROR_CARD_MIN_ATTEMPTS = 24;

const BootScreen: FC<BootScreenProps> = ({
  attempts,
  lastError,
  exiting = false,
  onRetry,
}) => {
  const t = useT();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const engineRef = useRef<BootParticlesHandle | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const engine = mountBootParticles(canvas);
    engineRef.current = engine;
    return (): void => {
      engine.destroy();
      engineRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (exiting) engineRef.current?.setReady();
  }, [exiting]);

  const showError = lastError !== null && attempts >= ERROR_CARD_MIN_ATTEMPTS;

  return (
    <div
      className={`boot-screen fixed inset-0 z-50 flex items-center justify-center bg-canvas${
        exiting ? " boot-exit" : ""
      }`}
    >
      <canvas
        ref={canvasRef}
        className="absolute inset-0 h-full w-full"
        aria-hidden="true"
      />
      {/* Status copy sits below the logo mark; the overlay is click-through
          except for the error card so pointer repel works across the stage. */}
      <div className="pointer-events-none absolute inset-x-0 top-[56%] flex flex-col items-center gap-4 px-4">
        <div className="boot-eyebrow" role="status" aria-live="polite">
          {t(bootStageKey(attempts))}
        </div>
        {showError && (
          <div className="pointer-events-auto flex max-w-md flex-col items-center gap-3 rounded-md border border-hairline bg-canvas-elevated px-4 py-3 shadow-card">
            <div className="boot-eyebrow text-danger">{t("boot.errorHeading")}</div>
            <pre className="max-h-32 w-full overflow-auto whitespace-pre-wrap break-all text-center font-mono text-xs text-mute">
              {lastError}
            </pre>
            <button
              type="button"
              onClick={onRetry}
              className="min-h-9 rounded-md bg-brand px-4 py-1.5 text-base font-medium text-canvas transition-colors hover:bg-brand-deep"
            >
              {t("common.retry")}
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export default BootScreen;
