import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Paperclip, Pencil } from "lucide-react";

import { ChatMarkdown } from "@/components/chat-markdown";
import { downloadFileAttachment } from "@/lib/api";
import { answersOf } from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";

import { kindIcon } from "./context-icons";
import { QuestionSummary } from "./question-card";
import type { UserTurnModel } from "./types";
import { useObjectNavigation } from "./use-object-navigation";

/** An uploaded image, fetched with the caller's token (the file route is
 *  authenticated, so a plain <img src> cannot load it). */
function ImagePreview({ attachmentId, name }: { attachmentId: string; name: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let revoked: string | null = null;
    void downloadFileAttachment(attachmentId)
      .then((blob) => {
        revoked = URL.createObjectURL(blob);
        setUrl(revoked);
      })
      .catch(() => setUrl(null));
    return () => {
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [attachmentId]);
  return url ? (
    <img src={url} alt={name} className="max-h-40 rounded-md border border-border" />
  ) : (
    <span className="inline-flex items-center gap-1 rounded-full border border-border bg-card px-2 text-xs">
      <Paperclip className="size-3.5" aria-hidden />
      {name}
    </span>
  );
}

export function UserTurn({
  turn,
  viewerMemberId,
  canEdit,
  onEdit,
  onSave,
  onSwitch,
}: {
  turn: UserTurnModel;
  viewerMemberId: string | null;
  canEdit: boolean;
  onEdit: (text: string) => void;
  /** In a message tree: send the edited text as a new branch. When given, Edit
   *  opens an inline editor instead of loading the text into the composer. */
  onSave?: (text: string) => Promise<unknown>;
  /** In a message tree: show the sibling with this id (PUT …/leaf). */
  onSwitch?: (messageId: string) => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);
  const m = turn.message;
  const openObject = useObjectNavigation();
  // An admin's answer from the inbox has no author name: no author line then.
  const showAuthor = !!m.authorName && !!m.authorMemberId && m.authorMemberId !== viewerMemberId;
  // The turn that answered an agent's questions: its one-line summary, not
  // the readable `content` the model reads.
  const answered = answersOf(m);
  // Only text a member typed keeps its links as written (ruling R23). A
  // follow-up bubble (and any other turn without a member author) is written
  // by the copilot from tool input: its links render as plain text, unverified.
  const memberTyped = m.role === "user" && !!m.authorMemberId;
  const siblings = turn.siblings && turn.siblings.ids.length > 1 ? turn.siblings : null;

  async function save() {
    const text = draft.trim();
    if (!text || !onSave || saving) return;
    setSaving(true);
    setSaveFailed(false);
    try {
      await onSave(text);
      setEditing(false);
    } catch {
      setSaveFailed(true);
    } finally {
      setSaving(false);
    }
  }

  return (
    <article
      className="turn-in group flex flex-col items-end gap-1"
      aria-label={
        answered
          ? t("Answers to the questions", "Antworten auf die Fragen")
          : t("Your message", "Deine Nachricht")
      }
    >
      {showAuthor && (
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <span
            className="grid size-4 place-items-center rounded-full bg-muted text-xs"
            aria-hidden
          >
            {m.authorName?.slice(0, 1)}
          </span>
          {m.authorName}
        </span>
      )}
      {editing ? (
        <div className="w-full max-w-[85%] space-y-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label={t("Edit message", "Nachricht bearbeiten")}
            rows={3}
            maxLength={20000}
            autoFocus
            className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          {draft.startsWith("/") && (
            <p className="text-xs text-muted-foreground">
              {t(
                "Text starting with / is sent as written, not run as a command.",
                "Text, der mit / beginnt, wird so gesendet, wie er steht, und nicht als Befehl ausgeführt.",
              )}
            </p>
          )}
          {saveFailed && (
            <p role="alert" className="text-xs text-status-error">
              {t(
                "The edit was not sent. Try again.",
                "Die Änderung wurde nicht gesendet. Versuch es noch einmal.",
              )}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                setSaveFailed(false);
              }}
              className="min-h-9 rounded-md px-3 text-sm text-muted-foreground hover:text-foreground [@media(pointer:coarse)]:min-h-11"
            >
              {t("Cancel", "Abbrechen")}
            </button>
            <button
              type="button"
              onClick={() => void save()}
              disabled={saving || !draft.trim()}
              className="min-h-9 rounded-md bg-primary px-3 text-sm text-primary-foreground disabled:opacity-50 [@media(pointer:coarse)]:min-h-11"
            >
              {t("Save", "Speichern")}
            </button>
          </div>
        </div>
      ) : (
        <div className="min-w-0 max-w-[85%] space-y-2 rounded-lg bg-muted px-3 py-2 [overflow-wrap:anywhere]">
          {m.mode && (
            <span className="inline-block rounded-full border border-border px-2 font-mono text-xs text-muted-foreground">
              /{m.mode}
            </span>
          )}
          {answered ? (
            <QuestionSummary questions={answered.questions} answers={answered.answers} />
          ) : (
            <ChatMarkdown
              text={m.content}
              onObjectClick={openObject}
              memberAuthored={memberTyped}
            />
          )}
          {m.attachments.length > 0 && (
            <ul className="flex flex-wrap gap-1">
              {m.attachments.map((a) =>
                a.isImage ? (
                  <li key={a.id}>
                    <ImagePreview attachmentId={a.id} name={a.filename} />
                  </li>
                ) : (
                  <li
                    key={a.id}
                    className="inline-flex items-center gap-1 rounded-full border border-border bg-card px-2 text-xs"
                  >
                    <Paperclip className="size-3.5" aria-hidden />
                    {a.filename}
                  </li>
                ),
              )}
            </ul>
          )}
          {m.contextRefs.length > 0 && (
            <ul className="flex flex-wrap gap-1">
              {m.contextRefs.map((ref) => {
                const Icon = kindIcon(ref.kind);
                return (
                  <li
                    key={`${ref.kind}-${ref.id}`}
                    className="inline-flex items-center gap-1 rounded-full border border-border bg-card px-2 text-xs text-muted-foreground"
                  >
                    <Icon className="size-3.5" aria-hidden />
                    {ref.label}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
      {siblings && !editing && onSwitch && (
        <span className="inline-flex items-center gap-0.5 text-xs text-muted-foreground">
          <button
            type="button"
            aria-label={t("Previous message", "Vorherige Nachricht")}
            disabled={siblings.index === 0}
            onClick={() => onSwitch(siblings.ids[siblings.index - 1])}
            className="grid size-9 place-items-center rounded-md hover:bg-muted hover:text-foreground disabled:opacity-40"
          >
            <ChevronLeft className="size-4" />
          </button>
          <span className="font-mono tabular-nums">
            {siblings.index + 1}/{siblings.ids.length}
          </span>
          <button
            type="button"
            aria-label={t("Next message", "Nächste Nachricht")}
            disabled={siblings.index === siblings.ids.length - 1}
            onClick={() => onSwitch(siblings.ids[siblings.index + 1])}
            className="grid size-9 place-items-center rounded-md hover:bg-muted hover:text-foreground disabled:opacity-40"
          >
            <ChevronRight className="size-4" />
          </button>
        </span>
      )}
      {canEdit && !answered && !editing && (
        <button
          type="button"
          onClick={() => {
            if (onSave) {
              setDraft(m.content);
              setSaveFailed(false);
              setEditing(true);
            } else onEdit(m.content);
          }}
          className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-xs text-muted-foreground opacity-0 transition-opacity duration-150 hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100 [@media(pointer:coarse)]:min-h-11"
        >
          <Pencil className="size-3.5" aria-hidden />
          {onSave ? t("Edit", "Bearbeiten") : t("Edit and resend", "Bearbeiten und erneut senden")}
        </button>
      )}
    </article>
  );
}
