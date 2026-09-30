// The `/`, `@` and `#` affordances, shared by both composers (§5 of the AI
// workplace design). Split out of chat-window.tsx/copilot-dock.tsx rather than
// implemented twice, because two composers that disagree about what `@` does
// is exactly how a convention stops being one.
//
// Two rules are encoded here and are worth stating out loud:
//
//   * `/` is a sigil ONLY at position 0. A slash anywhere else is a slash --
//     "due 12/05", "read /etc/passwd", "and/or". The backend parser has the
//     same rule, so what the composer previews and what the server does agree.
//   * The hint is ALWAYS visible, never a tooltip. §5.2 names an undiscoverable
//     `/` as an anti-pattern; a hint you have to hover to find is that.
//
// The paperclip button in both composers stays, and is NOT a parallel menu with
// overlapping actions: `#` points at knowledge oc8 already holds, the paperclip
// uploads a file it does not. Nothing is reachable both ways.

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export type SigilKind = "/" | "@" | "#";

export interface SigilToken {
  kind: SigilKind;
  /** What has been typed after the sigil, up to the caret. */
  query: string;
  /** Index of the sigil character itself. */
  start: number;
  /** Index just past the query -- i.e. the caret. */
  end: number;
}

const SIGILS: SigilKind[] = ["/", "@", "#"];

/** The sigil token the caret currently sits inside, or null.
 *
 *  Scans backwards from the caret and stops at the first whitespace, so a
 *  finished token ("@sales expert", caret at the end) is no longer active. A
 *  sigil counts only at the start of the text or after whitespace, which is
 *  what keeps "a@b.com" an email address.
 */
export function activeSigil(text: string, caret: number): SigilToken | null {
  for (let i = caret - 1; i >= 0; i--) {
    const ch = text[i];
    if (/\s/.test(ch)) return null;
    if (!SIGILS.includes(ch as SigilKind)) continue;
    const kind = ch as SigilKind;
    // A command is a command only at the very start of the message.
    if (kind === "/" && i !== 0) return null;
    if (i > 0 && !/\s/.test(text[i - 1])) return null;
    return { kind, query: text.slice(i + 1, caret), start: i, end: caret };
  }
  return null;
}

/** The draft with the sigil token swapped for `replacement`. */
export function replaceSigil(text: string, token: SigilToken, replacement: string): string {
  return text.slice(0, token.start) + replacement + text.slice(token.end);
}

/** Next highlighted row, wrapping. Returns 0 for an empty list so a caller
 *  never has to special-case it. */
export function cycleIndex(current: number, delta: number, length: number): number {
  if (length <= 0) return 0;
  return (current + delta + length) % length;
}

export interface SigilItem {
  id: string;
  label: string;
  /** The one-line description §5.2 requires of every offered action. */
  hint?: string;
  /** Set when the row is shown but cannot be chosen, with the reason. Shown
   *  rather than hidden on purpose: "why can I not reach Finance?" is a
   *  question a person should be able to answer from the screen. */
  blocked?: string;
}

export function SigilPopover({
  title,
  items,
  activeIndex,
  onPick,
  onHoverIndex,
  emptyText,
}: {
  title: string;
  items: SigilItem[];
  activeIndex: number;
  onPick: (item: SigilItem) => void;
  onHoverIndex: (index: number) => void;
  emptyText: string;
}) {
  return (
    <div
      role="listbox"
      aria-label={title}
      className="mb-2 max-h-56 overflow-y-auto rounded-lg border border-border bg-panel p-1 shadow-lg"
    >
      <div className="px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground">
        {title}
      </div>
      {items.length === 0 ? (
        <div className="px-2 py-2 text-xs text-muted-foreground">{emptyText}</div>
      ) : (
        items.map((item, index) => (
          <button
            key={item.id}
            type="button"
            role="option"
            aria-selected={index === activeIndex}
            aria-disabled={!!item.blocked}
            onMouseEnter={() => onHoverIndex(index)}
            onClick={() => {
              if (item.blocked) return;
              onPick(item);
            }}
            className={cn(
              "flex w-full flex-col items-start gap-0.5 rounded-sm px-2 py-1.5 text-left",
              index === activeIndex && "bg-accent/60",
              item.blocked && "opacity-60",
            )}
          >
            <span className="text-sm">{item.label}</span>
            {item.hint && <span className="text-[11px] text-muted-foreground">{item.hint}</span>}
            {item.blocked && <span className="text-[11px] text-amber-600">{item.blocked}</span>}
          </button>
        ))
      )}
    </div>
  );
}

/** The always-visible one-liner under a composer. */
export function ComposerHint({ className }: { className?: string }) {
  const t = useT();
  return (
    <div
      data-testid="composer-hint"
      className={cn("mt-1.5 text-[11px] text-muted-foreground", className)}
    >
      <code className="text-foreground/80">/</code> {t("commands", "Befehle")}
      {" · "}
      <code className="text-foreground/80">@</code> {t("agent", "Agent")}
      {" · "}
      <code className="text-foreground/80">#</code> {t("context", "Kontext")}
    </div>
  );
}
