import type { FC } from "react";

interface BootScreenProps {
  attempts: number;
  lastError: string | null;
}

const BootScreen: FC<BootScreenProps> = ({ attempts, lastError }) => {
  const hint =
    attempts <= 1
      ? "Starting backend…"
      : attempts <= 10
        ? "Connecting to backend…"
        : attempts <= 30
          ? "Still starting — this can take a few seconds on first launch."
          : "Taking longer than usual. Check logs if this persists.";

  return (
    <div className="flex h-screen w-screen flex-col items-center justify-center gap-5 bg-canvas text-ink">
      <div
        className="h-9 w-9 animate-spin rounded-full border-[3px] border-link/20 border-t-link"
        role="status"
        aria-label="Loading"
      />
      <div className="flex flex-col items-center gap-1.5">
        <div className="text-sm font-medium tracking-wide">Loading ModexBot…</div>
        <div className="min-h-[16px] text-xs text-ink/50">{hint}</div>
      </div>
      {lastError && attempts > 3 && (
        <div className="max-w-md rounded-md bg-canvas-elevated px-3 py-2 text-center font-mono text-[11px] text-ink/40">
          {lastError}
        </div>
      )}
    </div>
  );
};

export default BootScreen;
