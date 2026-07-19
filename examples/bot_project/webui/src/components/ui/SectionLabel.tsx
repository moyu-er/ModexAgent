import type { ReactNode } from "react";

interface Props {
  children: ReactNode;
}

export function SectionLabel({ children }: Props) {
  return (
    <div className="mb-2 text-xs font-mono font-semibold uppercase tracking-eyebrow text-mute">
      {children}
    </div>
  );
}
