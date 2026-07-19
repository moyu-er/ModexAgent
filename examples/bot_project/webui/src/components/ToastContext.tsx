// Toast system — Provider + useToast hook. Toasts render bottom-right, stacked.
// Auto-dismiss after ~4s UNLESS an action is present (actionable toasts stay
// until the user clicks the action or dismisses). Respects prefers-reduced-motion
// (instant render, no transition).

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type FC,
  type ReactNode,
} from "react";
import { Toast, type ToastAction, type ToastTone } from "./Toast";

export interface ShowToastOpts {
  message: string;
  tone?: ToastTone;
  action?: ToastAction;
}

/**
 * Restart indicator state, held alongside the toast system so every save that
 * sets `restart_required` can both surface the toast and arm the persistent red
 * dot on the settings gear. The indicator is best-effort: it is set on any
 * restart-required save and cleared by `clearRestartNeeded()`. We do NOT try
 * to detect the WS reconnect that follows a restart (that path is racy and
 * depends on which view is mounted); callers may clear it after `restartSystem`
 * resolves or simply leave it until page reload.
 */
interface RestartContextValue {
  restartNeeded: boolean;
  setRestartNeeded: (v: boolean) => void;
  clearRestartNeeded: () => void;
}

export interface ToastContextValue {
  show: (opts: ShowToastOpts) => void;
  restart: RestartContextValue;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const AUTO_DISMISS_MS = 4000;

interface ActiveToast {
  id: number;
  message: string;
  tone: ToastTone;
  action?: ToastAction;
}

export const ToastProvider: FC<{ children: ReactNode }> = ({ children }) => {
  const [toasts, setToasts] = useState<ActiveToast[]>([]);
  const [restartNeeded, setRestartNeeded] = useState<boolean>(false);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback((opts: ShowToastOpts): void => {
    const id = nextId.current++;
    setToasts((prev) => [
      ...prev,
      {
        id,
        message: opts.message,
        tone: opts.tone ?? "info",
        action: opts.action,
      },
    ]);
    // Actionable toasts stay until clicked/dismissed — they require a decision.
    if (!opts.action) {
      window.setTimeout(() => dismiss(id), AUTO_DISMISS_MS);
    }
  }, [dismiss]);

  const restart = useMemo<RestartContextValue>(
    () => ({
      restartNeeded,
      setRestartNeeded,
      clearRestartNeeded: () => setRestartNeeded(false),
    }),
    [restartNeeded],
  );

  const value = useMemo<ToastContextValue>(() => ({ show, restart }), [show, restart]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
};

function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ActiveToast[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col items-end gap-2">
      {toasts.map((t) => (
        <Toast
          key={t.id}
          message={t.message}
          tone={t.tone}
          action={t.action}
          onDismiss={() => onDismiss(t.id)}
        />
      ))}
    </div>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used within a ToastProvider");
  }
  return ctx;
}
