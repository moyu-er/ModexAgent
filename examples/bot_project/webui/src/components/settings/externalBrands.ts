import type { ProviderKind } from "../../types/pool";
import type { BrandIcon } from "../ui/brand-icons";
import { OpenCodeIcon } from "../ui/brand-icons";

export interface ProviderBrand {
  Icon: BrandIcon;
  color?: string;
}

/**
 * External-coding provider brand registry — the single registration point for
 * provider logos in the pool editor's external runtime panel.
 *
 * Keyed by {@link ProviderKind} (type-safe). Providers not listed here render
 * without a logo — the panel still works via the {@link externalProviders}
 * catalog for dropdown options, defaults, and help copy.
 *
 * To add a new external provider:
 *   1. Extend `ProviderKind` in `types/pool.ts`
 *   2. Add it to `EXTERNAL_PROVIDERS` in `types/externalProviders.ts`
 *   3. Create its icon component in `ui/brand-icons/`
 *   4. Export it from `ui/brand-icons/index.ts`
 *   5. Add one entry here
 */
export const PROVIDER_BRAND_ICONS: Partial<Record<ProviderKind, ProviderBrand>> = {
  opencode: { Icon: OpenCodeIcon },
};
