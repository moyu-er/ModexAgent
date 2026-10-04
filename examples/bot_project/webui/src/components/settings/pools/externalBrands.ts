import type { BrandIcon } from "../../ui/brand-icons";
import { OpenCodeIcon } from "../../ui/brand-icons";

export interface ProviderBrand {
  Icon: BrandIcon;
}

/**
 * External-coding provider brand registry — the single registration point
 * for provider logos in the pools panel (restored with the Implementation
 * selector; the provider list itself comes from the live options endpoint).
 *
 * Providers not listed here render without a logo — the panel still works
 * through the options-driven dropdown.
 */
export const PROVIDER_BRAND_ICONS: Record<string, ProviderBrand> = {
  opencode: { Icon: OpenCodeIcon },
};
