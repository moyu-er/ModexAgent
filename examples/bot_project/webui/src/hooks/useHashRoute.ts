// Minimal hash-based routing. The app has no react-router dependency (and
// cannot add one — package.json is outside the editable tree), so graph
// views and the standalone settings page hang off hashes parsed by this
// hook. Unknown or empty hashes fall back to the chat view.

import { useCallback, useEffect, useRef, useState } from "react";

export type NavigationGuard = (resume: () => void) => void;

export type Route =
  | { kind: "chat" }
  | { kind: "settings" }
  | { kind: "settingsSection"; section: string }
  | { kind: "graphs" }
  | { kind: "graphSpecDetail"; specId: string }
  | { kind: "graphSpecEdit"; specId: string }
  | { kind: "graphInstance"; instanceId: string };

export function parseHash(hash: string): Route {
  const segments = hash.split("?")[0]!
    .replace(/^#/, "")
    .split("/")
    .filter((s) => s.length > 0);
  const [head, second, third] = segments;
  if (head === "settings") {
    if (second === undefined) return { kind: "settings" };
    return { kind: "settingsSection", section: second };
  }
  if (head !== "graphs") return { kind: "chat" };
  if (second === undefined) return { kind: "graphs" };
  if (second === "instances") {
    if (third === undefined) return { kind: "graphs" };
    return { kind: "graphInstance", instanceId: third };
  }
  if (third === "edit") {
    return second
      ? { kind: "graphSpecEdit", specId: second }
      : { kind: "graphs" };
  }
  if (third === undefined && second) {
    return { kind: "graphSpecDetail", specId: second };
  }
  return { kind: "graphs" };
}

export interface UseHashRouteResult {
  route: Route;
  /** Navigate to a path like "/graphs/instances/3"; "" returns to chat. */
  navigate: (path: string) => void;
  setNavigationGuard: (guard: NavigationGuard | null) => void;
}

export function useHashRoute(): UseHashRouteResult {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  const accepted = useRef(window.location.hash);
  const guard = useRef<NavigationGuard | null>(null);
  const setNavigationGuard = useCallback((next: NavigationGuard | null) => { guard.current = next; }, []);

  useEffect(() => {
    const onHashChange = (): void => {
      const next = window.location.hash;
      if (next === accepted.current) return;
      const base = window.location.pathname + window.location.search;
      const resume = (): void => {
        accepted.current = next;
        window.history.replaceState(null, "", base + next);
        setRoute(parseHash(next));
      };
      if (guard.current) {
        window.history.replaceState(null, "", base + accepted.current);
        guard.current(resume);
      } else resume();
    };
    window.addEventListener("hashchange", onHashChange);
    return (): void => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navigate = useCallback((path: string): void => {
    const resume = (): void => {
      const hash = path ? `#${path.replace(/^#/, "")}` : "";
      accepted.current = hash;
      window.location.hash = hash;
      setRoute(parseHash(hash));
    };
    if (guard.current) guard.current(resume);
    else resume();
  }, []);

  return { route, navigate, setNavigationGuard };
}
