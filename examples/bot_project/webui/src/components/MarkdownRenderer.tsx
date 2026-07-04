import { useMemo, useState, type FC } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import { oneDark, oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import { useTheme } from "../hooks/useTheme";
import { MermaidBlock } from "./MermaidBlock";

SyntaxHighlighter.registerLanguage("tsx", tsx);
SyntaxHighlighter.registerLanguage("typescript", typescript);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("js", javascript);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("py", python);
SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("sh", bash);
SyntaxHighlighter.registerLanguage("css", css);
SyntaxHighlighter.registerLanguage("yaml", yaml);
SyntaxHighlighter.registerLanguage("yml", yaml);
SyntaxHighlighter.registerLanguage("markdown", markdown);
SyntaxHighlighter.registerLanguage("md", markdown);

export interface MarkdownRendererProps {
  content: string;
}

// Module-scope so ReactMarkdown gets a stable `remarkPlugins` identity across
// re-renders (an inline `[remarkGfm]` would be a new array every render).
const REMARK_PLUGINS = [remarkGfm];

interface CodeBlockProps {
  language: string;
  value: string;
  isDark: boolean;
}

const CodeBlock: FC<CodeBlockProps> = ({ language, value, isDark }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // ignore
    }
  };

  const lang = language || "text";

  return (
    <div className="mb-3 overflow-hidden rounded-lg border border-code-border">
      <div className="flex items-center justify-between border-b border-code-border bg-code-bg px-4 py-2">
        <span className="text-xs font-medium text-text-secondary">{lang}</span>
        <button
          type="button"
          onClick={handleCopy}
          className="text-xs text-text-secondary transition-colors hover:text-text-primary"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <SyntaxHighlighter
        language={lang}
        style={isDark ? oneDark : oneLight}
        className="!m-0 !rounded-none !bg-code-bg !p-4"
        codeTagProps={{ className: "font-mono text-[13px] leading-relaxed" }}
      >
        {value}
      </SyntaxHighlighter>
    </div>
  );
};

export const MarkdownRenderer: FC<MarkdownRendererProps> = ({ content }) => {
  const { theme } = useTheme();
  const isDark = theme === "dark";

  // Keep the custom-component map stable across re-renders (only recreating it
  // when isDark changes). react-markdown renders each node via
  // `React.createElement(components[code], …)`, so if `components.code` were a
  // fresh function on every render, React would see a new element TYPE and
  // REMOUNT the subtree — which reset MermaidBlock's state and re-ran its
  // mermaid.render() effect every time an ancestor (e.g. the ChatView input
  // box) re-rendered. Tying identity to isDark means a remount only on theme
  // switch, which is desired (mermaid re-renders with the new theme).
  const components = useMemo<Components>(
    () => ({
      code(props) {
        const { className, children, ...rest } = props;
        const match = /language-(([\w-]+))/.exec((className as string) || "");
        const value = String(children).replace(/\n$/, "");
        const lang = match?.[2];

        // react-markdown v9+ no longer passes an `inline` prop, so we
        // detect block code ourselves: a fenced block has a language
        // hint OR spans multiple lines (ASCII art, box-drawing, etc.).
        // Anything without a newline and without a language stays inline.
        const isBlock = lang !== undefined || value.includes("\n");

        if (isBlock) {
          if (lang === "mermaid") {
            return <MermaidBlock chart={value} isDark={isDark} />;
          }
          return (
            <CodeBlock
              language={lang ?? "text"}
              value={value}
              isDark={isDark}
            />
          );
        }
        return (
          <code className={className} {...rest}>
            {children}
          </code>
        );
      },
      pre({ children }) {
        // CodeBlock/MermaidBlock ship their own containers; unwrap the
        // default <pre> so its styles don't double up with ours.
        return <>{children}</>;
      },
    }),
    [isDark],
  );

  return (
    <div className="prose-chat">
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
};
