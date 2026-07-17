// Dependency-free i18n runtime. A single React context provides a translate
// function `t(key, params?)`; the default context value (used when no
// <I18nProvider> wraps the tree) is a working English translator so existing
// tests that render components without a provider keep passing.
//
// Catalogs register by adding one entry to `catalogs`. MessageKey is derived
// from `typeof en` so typos in keys are compile errors.

import {
  createContext,
  createElement,
  useContext,
  useMemo,
  type ReactNode,
} from "react";
import { en } from "./en";

export { en };

// ── Types ───────────────────────────────────────────────────────────────────

/** Shape of a catalog: a nested record of string leaves. */
export type Messages = typeof en;

/**
 * Recursive dotted-key type. For every string leaf in `en`, produces the
 * dotted path (e.g. "settings.models.defaultModel"). Typos in t("...") calls
 * are compile errors because the literal must be a member of this union.
 */
type Path<T, Prefix extends string = ""> = T extends string
  ? Prefix
  : T extends object
    ? {
        [K in keyof T & string]: Path<
          T[K],
          Prefix extends "" ? K : `${Prefix}.${K}`
        >;
      }[keyof T & string]
    : never;

export type MessageKey = Path<Messages>;

// ── Catalog registry ─────────────────────────────────────────────────────────

export const catalogs = { en } satisfies Record<string, Messages>;

// ── Translate ─────────────────────────────────────────────────────────────────

export type TFn = (
  key: MessageKey,
  params?: Record<string, string | number>,
) => string;

/** Walk a catalog object by dotted path; return the string leaf or undefined. */
function lookup(msgs: unknown, key: string): string | undefined {
  const parts = key.split(".");
  let cur: unknown = msgs;
  for (const part of parts) {
    if (cur && typeof cur === "object" && part in (cur as Record<string, unknown>)) {
      cur = (cur as Record<string, unknown>)[part];
    } else {
      return undefined;
    }
  }
  return typeof cur === "string" ? cur : undefined;
}

/** Replace {name} placeholders with params values; unknown names are kept. */
function interpolate(
  template: string,
  params?: Record<string, string | number>,
): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (_match, name: string) => {
    const v = params[name];
    return v === undefined || v === null ? `{${name}}` : String(v);
  });
}

/** Track warned keys so each missing key warns at most once. */
const warnedKeys = new Set<string>();

function makeT(locale: string): TFn {
  return (key, params) => {
    const catalog = (catalogs as Record<string, Messages>)[locale] ?? catalogs.en;
    const template = lookup(catalog, key);
    if (template === undefined) {
      if (!warnedKeys.has(key)) {
        warnedKeys.add(key);
        console.warn(`i18n: missing key "${key}" for locale "${locale}"`);
      }
      return key;
    }
    return interpolate(template, params);
  };
}

// ── React context ─────────────────────────────────────────────────────────────

/** Default English translator — used when no I18nProvider is mounted. */
export const defaultT: TFn = makeT("en");

const I18nContext = createContext<TFn>(defaultT);

export function I18nProvider({
  locale = "en",
  children,
}: {
  locale?: string;
  children: ReactNode;
}): ReactNode {
  const t = useMemo(() => makeT(locale), [locale]);
  return createElement(I18nContext.Provider, { value: t }, children);
}

export function useT(): TFn {
  return useContext(I18nContext);
}
