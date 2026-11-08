// Renders an assistant/copilot reply's markdown (headings, bold, lists,
// links, ...) instead of showing the raw "**bold**"/"## Heading" characters
// literally. Shared by copilot-dock.tsx and chat-window.tsx -- both surfaces
// hit the same bug: an LLM defaults to markdown-formatted prose unless told
// otherwise, and neither bubble was parsing it.
//
// react-markdown never touches innerHTML (it builds React elements), so this
// stays safe against a message containing literal HTML/script tags without
// needing a sanitizer on top.

import { useState } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";

// react-markdown/remark-gfm hand `pre` a single `<code>` child whose own
// className carries the fence info string as `language-*` (e.g. ` ```ts `
// produces `<code className="language-ts">`) -- that's the only place the
// language is available, and the only place the raw code text is available
// unmodified by any syntax highlighting this component doesn't do.
function CodeBlock({ children }: { children?: ReactNode }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const codeElement = Array.isArray(children) ? children[0] : children;
  const codeProps = (
    codeElement as { props?: { className?: string; children?: ReactNode } } | undefined
  )?.props;
  const language = /language-(\w+)/.exec(codeProps?.className ?? "")?.[1];
  const codeText = String(codeProps?.children ?? "").replace(/\n$/, "");

  function copy() {
    navigator.clipboard.writeText(codeText).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  return (
    <div className="rounded-md bg-muted/50">
      <div className="flex items-center justify-between px-2 py-1 text-[0.7em] text-muted-foreground">
        <span className="uppercase">{language ?? ""}</span>
        <button
          type="button"
          onClick={copy}
          className="flex items-center gap-1 hover:text-foreground"
        >
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          {copied ? t("Copied", "Kopiert") : t("Copy", "Kopieren")}
        </button>
      </div>
      <pre className="overflow-x-auto bg-transparent p-2">{children}</pre>
    </div>
  );
}

// Sizing/max-width is the caller's concern (bubble vs. plain flex row) --
// this component only owns markdown-element typography.
export function ChatMarkdown({ text, className }: { text: string; className?: string }) {
  return (
    <div
      className={cn(
        "space-y-2 text-sm leading-relaxed text-foreground/90 [&_a]:text-primary [&_a]:underline [&_code]:rounded [&_code]:bg-muted/50 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[0.85em] [&_h1]:text-base [&_h1]:font-semibold [&_h2]:text-sm [&_h2]:font-semibold [&_h3]:text-sm [&_h3]:font-medium [&_li]:ml-4 [&_ol]:list-decimal [&_ol]:space-y-0.5 [&_p]:leading-relaxed [&_strong]:font-semibold [&_ul]:list-disc [&_ul]:space-y-0.5 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_table]:block [&_table]:w-full [&_table]:overflow-x-auto [&_table]:border-collapse [&_th]:border [&_th]:border-border [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:font-semibold [&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1",
        className,
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{ pre: ({ children }) => <CodeBlock>{children}</CodeBlock> }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
