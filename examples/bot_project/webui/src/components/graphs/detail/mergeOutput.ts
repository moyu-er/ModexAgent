import type { GraphPayload } from "../../../lib/graphsApi";

/** Merge a list of GraphPayload into a single string (all content joined with "\n\n"). */
export function mergeGraphOutput(result: GraphPayload[] | null | undefined): string {
  if (!result || result.length === 0) return "";
  return result.map((p) => p.content).join("\n\n");
}
