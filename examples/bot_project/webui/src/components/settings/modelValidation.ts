// Pure client-side validation for the Models settings page. Runs before
// saveConfig so the user gets an immediate, friendly error instead of a
// raw 400 JSON body from the backend.
//
// Backend truth: bot/service/model_config.py BotModelConfig._validate raises
// when the (default_provider, default_model) combo is not found in providers;
// the model domain's WriteModelConfig face rejects a declared
// max_output_tokens above the model's own context_limit (PRD §4.7 D6).
// bot/webui/server.py config PUT returns {"error": "validation", "fields": ...}
// with status 400. Client-side validation is the primary UX — the backend
// check is the safety net.

import type { MessageKey } from "../../i18n";

interface ModelLike {
  name: string;
  context_limit?: number | null;
  max_output_tokens?: number | null;
}

interface ProviderLike {
  name: string;
  models: ModelLike[];
}

export function validateModelValues(
  values: Record<string, unknown>,
): MessageKey | null {
  const defaultProvider = String(values.default_provider ?? "").trim();
  const defaultModel = String(values.default_model ?? "").trim();

  // Allow saving without a default — the user can set it after fetching models.
  if (defaultProvider === "" || defaultModel === "") {
    return null;
  }

  const providers = (values.providers as ProviderLike[] | undefined) ?? [];
  const comboExists = providers.some(
    (p) =>
      p.name === defaultProvider &&
      Array.isArray(p.models) &&
      p.models.some((m) => m.name === defaultModel),
  );

  if (!comboExists) {
    return "settings.models.defaultNotFound";
  }

  // Per-model budget: a declared output budget must fit the model's own
  // declared window (the PUT face rejects it server-side too).
  const outputExceedsLimit = providers.some((p) =>
    Array.isArray(p.models) &&
    p.models.some(
      (m) =>
        typeof m.context_limit === "number" &&
        typeof m.max_output_tokens === "number" &&
        m.max_output_tokens > m.context_limit,
    ),
  );
  if (outputExceedsLimit) {
    return "settings.models.outputExceedsLimit";
  }

  return null;
}
