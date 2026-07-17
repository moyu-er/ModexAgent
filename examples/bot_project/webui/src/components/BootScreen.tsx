import type { FC } from "react";
import { useT } from "../i18n";

interface BootScreenProps {
  attempts: number;
  lastError: string | null;
}

const BootScreen: FC<BootScreenProps> = ({ attempts, lastError }) => {
  const t = useT();
  const hint =
    attempts <= 1
      ? t("boot.starting")
      : attempts <= 10
        ? t("boot.connecting")
        : attempts <= 30
          ? t("boot.stillStarting")
          : t("boot.takingLong");

  return (
    <div className="flex h-screen w-screen flex-col items-center justify-center gap-5 bg-canvas text-ink">
      <div
        className="h-9 w-9 animate-spin rounded-full border-[3px] border-link/20 border-t-link"
        role="status"
        aria-label={t("boot.loading")}
      />
      <div className="flex flex-col items-center gap-1.5">
        <div className="text-sm font-medium tracking-wide">{t("boot.loadingModexBot")}</div>
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
