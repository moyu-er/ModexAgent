import type { BrandIcon } from "../ui/brand-icons";
import { QqIcon, TelegramIcon } from "../ui/brand-icons";

export interface ImBrand {
  Icon: BrandIcon;
  color: string;
}

/**
 * IM platform brand registry — the single registration point for IM section
 * logos in the settings view.
 *
 * The key MUST match the backend's IM config section key (e.g. "qq",
 * "telegram") as returned in `ConfigPayload.sections`. Sections whose key is
 * not listed here render with the default {@link SectionLabel} (no logo).
 *
 * To add a new IM platform:
 *   1. Create its icon component in `ui/brand-icons/`
 *   2. Export it from `ui/brand-icons/index.ts`
 *   3. Add one entry here
 */
export const IM_BRAND_ICONS: Record<string, ImBrand> = {
  qq: { Icon: QqIcon, color: "#12b7f5" },
  telegram: { Icon: TelegramIcon, color: "#2aabee" },
};
