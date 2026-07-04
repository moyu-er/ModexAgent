import { useState, useEffect, useRef, type FC } from "react";

export function useTypewriter(text: string, isStreaming: boolean, speedMs = 16): string {
  const [displayed, setDisplayed] = useState(() => (isStreaming ? "" : text));
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fullRef = useRef(text);

  useEffect(() => {
    fullRef.current = text;
    if (!isStreaming) {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setDisplayed(text);
      return;
    }

    if (timerRef.current) return;

    const tick = (): void => {
      setDisplayed((prev) => {
        const nextLen = Math.min(fullRef.current.length, prev.length + 1);
        if (nextLen < fullRef.current.length) {
          timerRef.current = setTimeout(tick, speedMs);
        } else {
          timerRef.current = null;
        }
        return fullRef.current.slice(0, nextLen);
      });
    };

    timerRef.current = setTimeout(tick, speedMs);

    return (): void => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [text, isStreaming, speedMs]);

  return displayed;
}

export interface TypewriterTextProps {
  text: string;
  isStreaming: boolean;
  className?: string;
}

export const TypewriterText: FC<TypewriterTextProps> = ({
  text,
  isStreaming,
  className = "",
}) => {
  const displayed = useTypewriter(text, isStreaming);

  return (
    <span className={`${className} whitespace-pre-wrap break-words`}>
      {displayed}
      {isStreaming && (
        <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse rounded-sm bg-ai-brand align-text-bottom" />
      )}
    </span>
  );
};
