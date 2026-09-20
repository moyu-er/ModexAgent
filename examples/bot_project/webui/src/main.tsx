import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { I18nProvider } from "./i18n";
import { useLocale } from "./hooks/useLocale";
import "./index.css";

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root not found");
}

// App owns its own ToastProvider so Sidebar (restart indicator) + SettingsPage
// (toasts) can useToast() without every caller/test wrapping manually.
function Root() {
  const { locale } = useLocale();
  return (
    <StrictMode>
      <I18nProvider locale={locale}>
        <App />
      </I18nProvider>
    </StrictMode>
  );
}

createRoot(rootEl).render(<Root />);
