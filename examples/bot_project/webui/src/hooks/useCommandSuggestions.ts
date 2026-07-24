// Merges all autocomplete suggestion sources into a single SuggestionItem[].
//
// Currently two sources:
//   - Skills: from usePoolSkills (cache read, warmed by usePoolSkillsWarmup).
//     Category "skill", color --color-cat-skills (indigo).
//   - Built-in WebUI commands: static list of slash commands the WebUI
//     pipeline supports (currently just /continue). Category "command",
//     color --color-cat-pools (teal-cyan).
//
// New categories can be added by extending SuggestionCategory and appending
// to the merged list here — the dropdown renders any SuggestionItem.

import { useMemo } from "react";
import type { SuggestionItem } from "../types/suggestion";
import { usePoolSkills, usePoolSkillsWarmup } from "./usePoolSkills";

const BUILTIN_COMMANDS: SuggestionItem[] = [
  {
    name: "continue",
    category: "command",
    description: "Continue the conversation without injecting a message",
  },
];

export function useCommandSuggestions(
  pool: string | undefined,
  mainAgent: string | undefined,
): SuggestionItem[] {
  usePoolSkillsWarmup(pool, mainAgent);
  const skills = usePoolSkills(pool, mainAgent);

  return useMemo(() => {
    const skillItems: SuggestionItem[] = skills.map((s) => ({
      name: s.name,
      category: "skill" as const,
      description: s.description,
    }));
    return [...skillItems, ...BUILTIN_COMMANDS];
  }, [skills]);
}
