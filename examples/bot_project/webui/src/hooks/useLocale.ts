// useLocale — the frontend presentation-locale state (PA-11/PA-16).
// The typed catalogs live in src/i18n/index.ts (`catalogs`); this hook owns
// the persisted choice + the document lang attribute. Only locales with a
// registered catalog are offered; unknown persisted values fall back to en.

import { useCallback, useEffect, useSyncExternalStore } from "react";
import { catalogs } from "../i18n";

const STORAGE_KEY = "modexbot_locale";
const CHANGE_EVENT = "modexbot:locale";
let memoryLocale = "en";

function subscribe(notify: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, notify);
  window.addEventListener("storage", notify);
  return () => {
    window.removeEventListener(CHANGE_EVENT, notify);
    window.removeEventListener("storage", notify);
  };
}

export interface LocaleOption {
  value: string;
  label: string;
}

const LABELS: Record<string, string> = {
  en: "English",
  zh: "简体中文",
};

function readInitial(): string {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && saved in catalogs) return saved;
  } catch {
    return memoryLocale;
  }
  return "en";
}

export function useLocale(): {
  locale: string;
  setLocale: (locale: string) => void;
  available: LocaleOption[];
} {
  const locale = useSyncExternalStore(subscribe, readInitial, () => "en");

  useEffect(() => {
    document.documentElement.lang = locale;
    try {
      localStorage.setItem(STORAGE_KEY, locale);
    } catch {
      // localStorage unavailable
    }
  }, [locale]);

  const setLocale = useCallback((next: string): void => {
    if (!(next in catalogs)) return;
    memoryLocale = next;
    try { localStorage.setItem(STORAGE_KEY, next); } catch { /* session-only choice */ }
    window.dispatchEvent(new Event(CHANGE_EVENT));
  }, []);

  const available = Object.keys(catalogs).map((value) => ({
    value,
    label: LABELS[value] ?? value,
  }));

  return { locale, setLocale, available };
}
