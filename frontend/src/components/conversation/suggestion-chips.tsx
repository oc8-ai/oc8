// frontend/src/components/conversation/suggestion-chips.tsx
// One-click replies under the last answer (spec §4.3). The text comes from the
// model, so it is untrusted: shown as plain text only, never as markdown or a
// link. A click sends it as an ordinary message; the pencil puts it in the
// composer to edit first.
import { Pencil } from "lucide-react";

import { useT } from "@/lib/i18n";

export function SuggestionChips({
  suggestions,
  onSend,
  onInsert,
}: {
  suggestions: string[];
  onSend: (text: string) => void;
  onInsert: (text: string) => void;
}) {
  const t = useT();
  if (suggestions.length === 0) return null;
  return (
    <ul
      className="flex flex-wrap gap-2"
      aria-label={t("Suggested replies", "Vorgeschlagene Antworten")}
    >
      {suggestions.map((s, i) => (
        <li
          key={`${i}:${s}`}
          className="inline-flex items-center rounded-full border border-border"
        >
          <button
            type="button"
            onClick={() => onSend(s)}
            className="min-h-9 rounded-full pl-3 pr-1 text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            {s}
          </button>
          <button
            type="button"
            onClick={() => onInsert(s)}
            aria-label={t(`Edit before sending: ${s}`, `Vor dem Senden bearbeiten: ${s}`)}
            className="grid size-9 place-items-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            <Pencil className="size-3.5" aria-hidden />
          </button>
        </li>
      ))}
    </ul>
  );
}
