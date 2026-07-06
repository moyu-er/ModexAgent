import type { ReactNode } from "react";

interface Props {
  children: ReactNode;
}

export function SectionLabel({ children }: Props) {
  return (
    <div className="mb-2 text-[10px] font-mono font-semibold uppercase tracking-wide text-mute">
      {children}
    </div>
  );
}
