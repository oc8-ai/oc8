// frontend/src/components/conversation/prompt-starters.tsx
// Ways into an empty chat: each card sends its prompt (an agent idea's
// "/agent ..." sentence) or, without one, its title as the first message.
export interface PromptStarter {
  title: string;
  subtitle: string;
  prompt?: string;
}

export function PromptStarters({
  starters,
  onPick,
}: {
  starters: PromptStarter[];
  onPick: (text: string) => void;
}) {
  if (starters.length === 0) return null;
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {starters.map((s) => (
        <button
          key={s.title}
          type="button"
          onClick={() => onPick(s.prompt ?? s.title)}
          className="min-h-11 rounded-lg border border-border bg-card p-3 text-left hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <span className="block text-sm font-medium">{s.title}</span>
          {s.subtitle && <span className="block text-xs text-muted-foreground">{s.subtitle}</span>}
        </button>
      ))}
    </div>
  );
}
