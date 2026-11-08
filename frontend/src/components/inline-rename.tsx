import { Check, Pencil, X } from "lucide-react";
import { useState } from "react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** A heading that turns into an edit field when its pencil icon is clicked --
 * used by both the agent and department detail pages so "rename" behaves
 * identically everywhere it appears. Hidden entirely when `disabled` (the
 * caller's own permission check), matching how those pages already hide
 * other manage-only affordances (e.g. `DeleteAgentButton`) rather than
 * showing them greyed out. */
export function InlineRename({
  value,
  onSave,
  disabled,
  label,
  headingClassName,
}: {
  value: string;
  onSave: (name: string) => Promise<unknown>;
  disabled?: boolean;
  label: string;
  headingClassName?: string;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);

  function start() {
    setDraft(value);
    setEditing(true);
  }

  function cancel() {
    setEditing(false);
    setDraft(value);
  }

  async function commit() {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === value) {
      cancel();
      return;
    }
    setSaving(true);
    try {
      await onSave(trimmed);
      setEditing(false);
    } catch {
      // Caller's mutation already surfaces its own error toast; leave the
      // field open with the attempted edit so the user doesn't retype it.
    } finally {
      setSaving(false);
    }
  }

  if (editing) {
    return (
      <div className="flex min-w-0 items-center gap-1.5">
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void commit();
            } else if (e.key === "Escape") {
              cancel();
            }
          }}
          disabled={saving}
          className={cn(
            "min-w-0 flex-1 rounded-md border border-primary/50 bg-background px-2 py-0.5 outline-none disabled:opacity-50",
            headingClassName,
          )}
        />
        <button
          type="button"
          title={t("Save", "Speichern")}
          onClick={() => void commit()}
          disabled={saving}
          className="shrink-0 rounded p-1 text-muted-foreground transition hover:text-status-running disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Check className="h-4 w-4" />
        </button>
        <button
          type="button"
          title={t("Cancel", "Abbrechen")}
          onClick={cancel}
          disabled={saving}
          className="shrink-0 rounded p-1 text-muted-foreground transition hover:text-status-error disabled:cursor-not-allowed disabled:opacity-50"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    );
  }

  return (
    <div className="group flex min-w-0 items-center gap-1.5">
      <span className={cn("truncate", headingClassName)}>{value}</span>
      {!disabled && (
        <button
          type="button"
          title={label}
          onClick={start}
          className="shrink-0 rounded p-1 text-muted-foreground opacity-0 transition group-hover:opacity-100 hover:text-foreground"
        >
          <Pencil className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
