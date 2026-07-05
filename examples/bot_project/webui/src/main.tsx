import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root not found");
}

// App owns its own ToastProvider so Sidebar (restart indicator) + SettingsView
// (toasts) can useToast() without every caller/test wrapping manually.
createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
