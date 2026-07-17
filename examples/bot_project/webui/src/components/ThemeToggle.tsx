import { type FC } from "react";
import { Sun, Moon } from "lucide-react";
import { useTheme } from "../hooks/useTheme";
import { useT } from "../i18n";

export const ThemeToggle: FC = () => {
  const { theme, toggleTheme } = useTheme();
  const t = useT();
  const isDark = theme === "dark";

  return (
    <button
      type="button"
      onClick={toggleTheme}
      title={isDark ? t("theme.switchToLight") : t("theme.switchToDark")}
      aria-label={isDark ? t("theme.switchToLight") : t("theme.switchToDark")}
      className="rounded-md p-1.5 text-mute transition-colors hover:bg-hairline-soft hover:text-ink"
    >
      {isDark ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
    </button>
  );
};
