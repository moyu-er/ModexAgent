// Minimal hash-based routing. The app has no react-router dependency (and
// cannot add one — package.json is outside the editable tree), so graph
// views hang off `#/graphs…` hashes parsed by this hook. Unknown or empty
// hashes fall back to the chat view.

import { useCallback, useEffect, useState } from "react";

export type Route =
  | { kind: "chat" }
  | { kind: "graphs" }
  | { kind: "graphSpecEdit"; specId: string }
  | { kind: "graphInstances" }
  | { kind: "graphInstance"; instanceId: string };

function parseHash(hash: string): Route {
  const segments = hash
    .replace(/^#/, "")
    .split("/")
    .filter((s) => s.length > 0);
  const [head, second, third] = segments;
  if (head !== "graphs") return { kind: "chat" };
  if (second === undefined) return { kind: "graphs" };
  if (second === "instances") {
    if (third === undefined) return { kind: "graphInstances" };
    return third
      ? { kind: "graphInstance", instanceId: third }
      : { kind: "graphInstances" };
  }
  if (third === "edit") {
    return second
      ? { kind: "graphSpecEdit", specId: second }
      : { kind: "graphs" };
  }
  return { kind: "graphs" };
}

export interface UseHashRouteResult {
  route: Route;
  /** Navigate to a path like "/graphs/instances/3"; "" returns to chat. */
  navigate: (path: string) => void;
}

export function useHashRoute(): UseHashRouteResult {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));

  useEffect(() => {
    const onHashChange = (): void => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return (): void => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navigate = useCallback((path: string): void => {
    window.location.hash = path;
  }, []);

  return { route, navigate };
}
