// frontend/src/components/conversation/prompt-starters.tsx
// Ways into an empty chat: each card sends its title as the first message.
export function PromptStarters({
  starters,
  onPick,
}: {
  starters: { title: string; subtitle: string }[];
  onPick: (text: string) => void;
}) {
  if (starters.length === 0) return null;
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {starters.map((s) => (
        <button
          key={s.title}
          type="button"
          onClick={() => onPick(s.title)}
          className="min-h-11 rounded-lg border border-border bg-card p-3 text-left hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <span className="block text-sm font-medium">{s.title}</span>
          {s.subtitle && <span className="block text-xs text-muted-foreground">{s.subtitle}</span>}
        </button>
      ))}
    </div>
  );
}
