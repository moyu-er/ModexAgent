// Shared helper: surface the uniform "Saved. Restart to apply." toast with a
// "Restart now" action AND arm the persistent restart indicator (red dot on the
// settings gear). Used by every save path that returns restart_required=true:
// PoolEditor Save, PromptEditor Save, eager skill assign/unassign, GlobalMcpView
// save/delete, and the IM/Models persisted-domain Save (rewired in SettingsView).

import type { ToastContextValue } from "../ToastContext";
import { restartSystem } from "../../lib/api";
import type { TFn } from "../../i18n";

export function restartToast(toast: ToastContextValue, t: TFn): void {
  toast.restart.setRestartNeeded(true);
  toast.show({
    message: t("toast.savedRestart"),
    tone: "success",
    action: {
      label: t("toast.restartNow"),
      onClick: () => {
        void restartSystem()
          .then(() => {
            toast.restart.clearRestartNeeded();
          })
          .catch(() => {
            toast.show({
              message: t("toast.restartUnavailable"),
              tone: "warning",
            });
          });
      },
    },
  });
}
