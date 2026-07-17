// Pure client-side validation for the Models settings page. Runs before
// saveConfig so the user gets an immediate, friendly error instead of a
// raw 400 JSON body from the backend.
//
// Backend truth: bot/service/model_config.py BotModelConfig._validate raises
// when the (default_provider, default_model) combo is not found in providers.
// bot/webui/server.py config PUT returns {"error": "validation", "fields": ...}
// with status 400. Client-side validation is the primary UX — the backend
// check is the safety net.

import type { MessageKey } from "../../i18n";

interface ProviderLike {
  name: string;
  models: { name: string }[];
}

export function validateModelValues(
  values: Record<string, unknown>,
): MessageKey | null {
  const defaultProvider = String(values.default_provider ?? "").trim();
  const defaultModel = String(values.default_model ?? "").trim();

  if (defaultProvider === "" || defaultModel === "") {
    return "settings.models.defaultRequired";
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

  return null;
}
