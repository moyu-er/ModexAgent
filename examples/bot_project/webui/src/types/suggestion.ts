// Generic autocomplete suggestion item — supports multiple command categories.
//
// Each suggestion has a `category` label (e.g. "skill", "command") displayed
// as a prefix, a `name` (the slash-command token, without the leading '/'),
// and an optional `description` shown right-aligned in the dropdown.
//
// The category label is a short string shown before the name (e.g. "skill:",
// "command:") so the user can visually distinguish suggestion types. It is
// also used as a CSS color key via the `categoryColor` field so each category
// gets its own accent color.

export type SuggestionCategory = "skill" | "command";

export interface SuggestionItem {
  /** The slash-command token without leading '/', e.g. "weather" or "continue". */
  name: string;
  /** Category label shown as a prefix before the name. */
  category: SuggestionCategory;
  /** Short human-readable description, shown right-aligned (truncated). */
  description?: string;
}
