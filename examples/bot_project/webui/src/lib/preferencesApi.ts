// preferencesApi.ts — the personal-assistant preference seam (PA-06/PA-07
// frontend client). The backend registers the `personal_assistant` config
// domain (singleton) at the usual config surface, so the client rides the
// existing GET/PUT /api/config/personal_assistant contract; only the two
// typed fields cross this seam.
//
// `default_workspace`: null | canonical server path (opening entry only —
//   never touches ScopeRegistry.home or running roots).
// `default_pool`: non-empty pool name; a NEW-selection preselection only —
//   existing sessions keep their routing.

import { assertOk, API_BASE } from "./api";

export interface PersonalAssistantPreferences {
  defaultWorkspace: string | null;
  defaultPool: string;
}

interface PrefsPayload {
  values?: {
    default_workspace?: string | null;
    default_pool?: string;
  };
}

function readValues(body: PrefsPayload): PersonalAssistantPreferences {
  const values = body.values ?? {};
  return {
    defaultWorkspace:
      typeof values.default_workspace === "string" ? values.default_workspace : null,
    defaultPool:
      typeof values.default_pool === "string" && values.default_pool.length > 0
        ? values.default_pool
        : "",
  };
}

export async function fetchPreferences(): Promise<PersonalAssistantPreferences> {
  const resp = await fetch(`${API_BASE}/config/personal_assistant`);
  await assertOk(resp);
  return readValues((await resp.json()) as PrefsPayload);
}

export async function savePreferences(
  prefs: PersonalAssistantPreferences,
): Promise<PersonalAssistantPreferences> {
  const resp = await fetch(`${API_BASE}/config/personal_assistant`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      default_workspace: prefs.defaultWorkspace,
      default_pool: prefs.defaultPool,
    }),
  });
  await assertOk(resp);
  return readValues((await resp.json()) as PrefsPayload);
}
