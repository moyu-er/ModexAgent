/**
 * Append the active workspace as a ``ws`` query parameter.
 *
 * Empty/undefined ``ws`` means the home workspace — matching the backend's
 * ``?ws=`` convention, home requests omit the param so the server reads the
 * canonical home dir. Used both by REST URL builders (attachment download) and
 * by message-block rendering (outbound attachment_card deltas carry a bare
 * download_url that still needs the active ws appended).
 */
export function appendWsParam(url: string, ws?: string): string {
  if (!ws) return url;
  return `${url}${url.includes("?") ? "&" : "?"}ws=${encodeURIComponent(ws)}`;
}

/**
 * Append the session's pool as a ``pool`` query parameter. Same omission
 * convention as ``appendWsParam``: empty/undefined pool leaves the URL alone.
 */
export function appendPoolParam(url: string, pool?: string): string {
  if (!pool) return url;
  return `${url}${url.includes("?") ? "&" : "?"}pool=${encodeURIComponent(pool)}`;
}

/** Append both session-scope params (``ws`` then ``pool``) to a URL. */
export function appendScopeParams(url: string, ws?: string, pool?: string): string {
  return appendPoolParam(appendWsParam(url, ws), pool);
}
