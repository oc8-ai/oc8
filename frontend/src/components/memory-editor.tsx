import { Lock } from "lucide-react";
import type { KnowledgeEntry } from "@/lib/mock-data";
import { cn } from "@/lib/utils";

type Entry = KnowledgeEntry;

export function MemoryEditor({
  scope,
  seed,
  emptyLabel,
  accent,
}: {
  /** e.g. "vertrieb" or "agent:vera" — used only for toast copy */
  scope: string;
  seed: Entry[];
  emptyLabel?: string;
  accent?: string;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <Lock className="h-3 w-3" /> {scope} · {seed.length} records · control-plane managed
        </span>
      </div>
      <p className="rounded-md border border-border bg-background/30 p-3 text-xs text-muted-foreground">
        Memory is written by the Memory Router under the tiered access and approval rules in the
        technical specification. Manual browser edits are intentionally unavailable until a
        policy-enforced editor API exists.
      </p>
      {seed.length === 0 ? (
        <p className="rounded-md border border-dashed border-border/70 bg-background/30 p-4 text-center text-sm text-muted-foreground">
          {emptyLabel ?? "No memories yet."}
        </p>
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border bg-background/30">
          {seed.map((e) => (
            <li key={e.id} className="flex items-start gap-3 p-3">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-medium">{e.title}</span>
                  <span
                    className={cn(
                      "rounded-full border px-2 py-0.5 text-micro",
                      "border-border bg-background/40 text-muted-foreground",
                    )}
                    style={
                      accent
                        ? {
                            color: accent,
                            borderColor: `color-mix(in oklab, ${accent} 35%, transparent)`,
                            background: `color-mix(in oklab, ${accent} 10%, transparent)`,
                          }
                        : undefined
                    }
                  >
                    {e.category}
                  </span>
                  <span className="ml-auto text-xs text-muted-foreground">{e.updated}</span>
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{e.snippet}</p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
