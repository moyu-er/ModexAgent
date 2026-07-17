import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FC,
  type FocusEvent,
  type KeyboardEvent,
} from "react";
import type { ModelChoice } from "../lib/api";
import { useT } from "../i18n";

export interface ModelSelectorValue {
  provider: string;
  model: string;
}

export interface ModelSelectorProps {
  models: ModelChoice[];
  value: ModelSelectorValue;
  onChange: (value: ModelSelectorValue) => void;
  "aria-label"?: string;
}

type IndexedChoice = ModelChoice & { index: number };

export const ModelSelector: FC<ModelSelectorProps> = ({
  models,
  value,
  onChange,
  "aria-label": ariaLabel,
}) => {
  const t = useT();
  const resolvedAriaLabel = ariaLabel ?? t("composer.model");
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const listboxRef = useRef<HTMLDivElement>(null);
  const baseId = useId();

  const items = useMemo<IndexedChoice[]>(
    () => models.map((m, index) => ({ ...m, index })),
    [models],
  );

  const groups = useMemo(() => {
    const map = new Map<string, IndexedChoice[]>();
    for (const m of items) {
      const list = map.get(m.provider_name) ?? [];
      list.push(m);
      map.set(m.provider_name, list);
    }
    return Array.from(map.entries());
  }, [items]);

  const selectedIndex = useMemo(() => {
    if (!value.provider && !value.model) return -1;
    return items.findIndex(
      (m) => m.provider_name === value.provider && m.model_name === value.model,
    );
  }, [items, value]);

  const [activeIndex, setActiveIndex] = useState(() =>
    selectedIndex >= 0 ? selectedIndex : 0,
  );

  const current = useMemo(
    () =>
      items.find(
        (m) => m.provider_name === value.provider && m.model_name === value.model,
      ),
    [items, value],
  );

  const focusOption = (index: number): void => {
    const el = listboxRef.current?.querySelector(
      `[data-index="${index}"]`,
    ) as HTMLElement | null;
    el?.focus();
  };

  // Focus the active option whenever the dropdown is open and the active index
  // changes (including the moment it opens).
  useEffect(() => {
    if (!open) return;
    focusOption(activeIndex);
  }, [open, activeIndex]);

  // Close when clicking outside.
  useEffect(() => {
    if (!open) return;
    const handleMouseDown = (e: globalThis.MouseEvent): void => {
      if (!containerRef.current?.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleMouseDown);
    return () => document.removeEventListener("mousedown", handleMouseDown);
  }, [open]);

  // Close when focus leaves the widget entirely.
  const handleBlur = (e: FocusEvent<HTMLDivElement>): void => {
    if (!containerRef.current?.contains(e.relatedTarget as Node)) {
      setOpen(false);
    }
  };

  const selectIndex = (index: number): void => {
    const m = items[index];
    if (!m) return;
    onChange({ provider: m.provider_name, model: m.model_name });
    setOpen(false);
    triggerRef.current?.focus();
  };

  const openDropdown = (): void => {
    const target = selectedIndex >= 0 ? selectedIndex : 0;
    setActiveIndex(target);
    setOpen(true);
  };

  const handleTriggerClick = (): void => {
    if (open) {
      setOpen(false);
    } else {
      openDropdown();
    }
  };

  const handleTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>): void => {
    if (!open) {
      if (
        e.key === "ArrowDown" ||
        e.key === "ArrowUp" ||
        e.key === "Enter" ||
        e.key === " "
      ) {
        e.preventDefault();
        if (e.key === "ArrowUp") {
          setActiveIndex(Math.max(items.length - 1, 0));
        } else {
          const target = selectedIndex >= 0 ? selectedIndex : 0;
          setActiveIndex(target);
        }
        setOpen(true);
      }
      return;
    }

    if (e.key === "Tab") {
      e.preventDefault();
      if (e.shiftKey) {
        const idx = Math.max(items.length - 1, 0);
        setActiveIndex(idx);
        focusOption(idx);
      } else {
        setActiveIndex(0);
        focusOption(0);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((prev) => Math.min(prev + 1, items.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((prev) => Math.max(prev - 1, 0));
    }
  };

  const handleOptionKeyDown = (
    index: number,
    e: KeyboardEvent<HTMLDivElement>,
  ): void => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (index < items.length - 1) setActiveIndex(index + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (index > 0) setActiveIndex(index - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      setActiveIndex(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActiveIndex(Math.max(items.length - 1, 0));
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      selectIndex(index);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (e.key === "Tab") {
      e.preventDefault();
      if (e.shiftKey) {
        if (index === 0) {
          triggerRef.current?.focus();
        } else {
          setActiveIndex(index - 1);
        }
      } else if (index === items.length - 1) {
        triggerRef.current?.focus();
      } else {
        setActiveIndex(index + 1);
      }
    }
  };

  const listboxId = `${baseId}-listbox`;
  const triggerLabel = current
    ? `${current.provider_name} - ${current.model_name}`
    : resolvedAriaLabel;

  return (
    <div
      ref={containerRef}
      className="relative shrink-0"
      onBlur={handleBlur}
    >
      <button
        ref={triggerRef}
        type="button"
        id={`${baseId}-trigger`}
        onClick={handleTriggerClick}
        onKeyDown={handleTriggerKeyDown}
        aria-label={current ? `${resolvedAriaLabel}: ${triggerLabel}` : resolvedAriaLabel}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-controls={open ? listboxId : undefined}
        className="flex max-w-[160px] items-center gap-1.5 rounded-full border border-hairline bg-canvas-elevated px-3 py-1.5 text-xs text-mute transition-colors motion-reduce:transition-none hover:bg-hairline-soft hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-link"
      >
        <span className="truncate">{triggerLabel}</span>
        <ChevronIcon open={open} />
      </button>

      {open && (
        <div
          ref={listboxRef}
          id={listboxId}
          role="listbox"
          aria-label={resolvedAriaLabel}
          tabIndex={-1}
          className="absolute bottom-full right-0 z-50 mb-2 min-w-[240px] max-h-[min(60vh,320px)] overflow-y-auto rounded-xl border border-hairline bg-canvas-elevated py-1 shadow-[0_8px_24px_var(--shadow-color)] focus:outline-none"
        >
          {groups.map(([provider, providerModels], groupIdx) => (
            <div
              key={provider}
              role="group"
              aria-label={provider}
              className={`${
                groupIdx > 0 ? "border-t border-hairline" : ""
              }`}
            >
              <div className="sticky top-0 z-10 flex items-center gap-1.5 border-b border-hairline bg-canvas-elevated px-3 py-1.5">
                <span
                  aria-hidden="true"
                  className="h-1.5 w-1.5 rounded-full bg-mute/50"
                />
                <span className="text-[11px] font-bold uppercase tracking-wider text-mute">
                  {provider}
                </span>
              </div>
              {providerModels.map((m) => {
                const isSelected =
                  value.provider === m.provider_name &&
                  value.model === m.model_name;
                const isActive = activeIndex === m.index;
                return (
                  <div
                    key={`${m.provider_name}::${m.model_name}`}
                    role="option"
                    id={`${baseId}-option-${m.index}`}
                    data-index={m.index}
                    tabIndex={-1}
                    aria-selected={isSelected}
                    aria-posinset={m.index + 1}
                    aria-setsize={items.length}
                    onClick={() => selectIndex(m.index)}
                    onFocus={() => setActiveIndex(m.index)}
                    onKeyDown={(e) => handleOptionKeyDown(m.index, e)}
                    data-active={isActive}
                    className="relative flex w-full cursor-pointer items-center justify-between pl-7 pr-3 py-2 text-left text-sm font-medium text-body transition-colors motion-reduce:transition-none hover:bg-hairline-soft hover:text-ink focus:bg-hairline-soft focus:outline-none data-[active=true]:bg-hairline-soft data-[active=true]:text-ink"
                  >
                    <span
                      aria-hidden="true"
                      className="absolute left-3 top-1/2 h-1 w-1 -translate-y-1/2 rounded-full bg-mute/25"
                    />
                    {isSelected && (
                      <span
                        aria-hidden="true"
                        className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-r bg-link"
                      />
                    )}
                    <span className="truncate">{m.model_name}</span>
                    {m.default && (
                      <span className="ml-2 shrink-0 text-[10px] font-medium text-link">
                        {t("composer.default")}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

const ChevronIcon: FC<{ open: boolean }> = ({ open }) => (
  <svg
    width="12"
    height="12"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    className={`shrink-0 transition-transform motion-reduce:transition-none ${
      open ? "rotate-180" : ""
    }`}
  >
    <polyline points="6 9 12 15 18 9" />
  </svg>
);
