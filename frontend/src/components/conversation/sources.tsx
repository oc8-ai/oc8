import { Globe, BookOpen } from "lucide-react";

import type { ChatSourceDTO } from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

function host(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export function Sources({
  sources,
  highlighted,
}: {
  sources: ChatSourceDTO[];
  highlighted: number | null;
}) {
  const t = useT();
  if (sources.length === 0) return null;
  return (
    <section aria-label={t("Cited sources", "Quellen")} className="space-y-1">
      <h4 className="text-micro">
        {t(`Sources (${sources.length})`, `Quellen (${sources.length})`)}
      </h4>
      <ol className="space-y-1">
        {sources.map((s) => (
          <li
            key={s.n}
            id={`source-${s.n}`}
            className={cn(
              "flex items-start gap-2 rounded-md px-2 py-1 text-xs",
              highlighted === s.n && "bg-muted",
            )}
          >
            <span className="font-mono text-muted-foreground">[{s.n}]</span>
            {s.kind === "web" ? (
              <Globe className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            ) : (
              <BookOpen className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            )}
            <span className="min-w-0">
              {/^https?:\/\//.test(s.url) ? (
                <a
                  href={s.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-foreground hover:underline"
                >
                  {s.title}
                </a>
              ) : (
                <span className="text-foreground">{s.title}</span>
              )}
              <span className="block truncate text-muted-foreground">
                {s.kind === "web" ? host(s.url) : s.snippet}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
