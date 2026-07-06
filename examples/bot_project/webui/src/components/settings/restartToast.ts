// Shared helper: surface the uniform "Saved. Restart to apply." toast with a
// "Restart now" action AND arm the persistent restart indicator (red dot on the
// settings gear). Used by every save path that returns restart_required=true:
// PoolEditor Save, PromptEditor Save, eager skill assign/unassign, GlobalMcpView
// save/delete, and the IM/Models persisted-domain Save (rewired in SettingsView).

import type { ToastContextValue } from "../ToastContext";
import { restartSystem } from "../../lib/api";

export function restartToast(toast: ToastContextValue): void {
  toast.restart.setRestartNeeded(true);
  toast.show({
    message: "Saved. Restart to apply.",
    tone: "success",
    action: {
      label: "Restart now",
      onClick: () => {
        void restartSystem()
          .then(() => {
            // Best-effort: clear once the restart call resolves. The WS drops
            // and reconnects shortly after; if a stale dot survives to the
            // reloaded UI it will simply clear on next page load.
            toast.restart.clearRestartNeeded();
          })
          .catch(() => {
            toast.show({
              message: "Restart unavailable — run `modexbot restart` in your terminal.",
              tone: "warning",
            });
          });
      },
    },
  });
}
