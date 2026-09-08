// FormSection.tsx — collapsible SectionLabel group for the pools panel forms.
// Header is a full-width button (≥36px target, focus ring) with a chevron;
// content animates via the shared duration tokens.

import { useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { SectionLabel } from "../../ui/SectionLabel";

export function FormSection({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <section className="rounded-lg border border-hairline bg-canvas-elevated p-4">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="-mx-1 flex min-h-9 w-[calc(100%+0.5rem)] items-center justify-between rounded-sm px-1 text-left transition-colors duration-fast ease-out hover:bg-hairline-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
      >
        <SectionLabel>{title}</SectionLabel>
        <ChevronDown
          size={14}
          aria-hidden="true"
          className={`shrink-0 text-mute transition-transform duration-app ease-out ${open ? "" : "-rotate-90"}`}
        />
      </button>
      {open ? <div className="mt-1 space-y-4">{children}</div> : null}
    </section>
  );
}
