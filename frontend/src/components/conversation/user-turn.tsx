import { useEffect, useState } from "react";
import { Paperclip, Pencil } from "lucide-react";

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
}: {
  turn: UserTurnModel;
  viewerMemberId: string | null;
  canEdit: boolean;
  onEdit: (text: string) => void;
}) {
  const t = useT();
  const m = turn.message;
  const openObject = useObjectNavigation();
  // An admin's answer from the inbox has no author name: no author line then.
  const showAuthor = !!m.authorName && !!m.authorMemberId && m.authorMemberId !== viewerMemberId;
  // The turn that answered an agent's questions: its one-line summary, not
  // the readable `content` the model reads.
  const answered = answersOf(m);
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
      <div className="min-w-0 max-w-[85%] space-y-2 rounded-lg bg-muted px-3 py-2 [overflow-wrap:anywhere]">
        {m.mode && (
          <span className="inline-block rounded-full border border-border px-2 font-mono text-xs text-muted-foreground">
            /{m.mode}
          </span>
        )}
        {answered ? (
          <QuestionSummary questions={answered.questions} answers={answered.answers} />
        ) : (
          <ChatMarkdown text={m.content} onObjectClick={openObject} />
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
      {canEdit && !answered && (
        <button
          type="button"
          onClick={() => onEdit(m.content)}
          className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-xs text-muted-foreground opacity-0 transition-opacity duration-150 hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100 [@media(pointer:coarse)]:min-h-11"
        >
          <Pencil className="size-3.5" aria-hidden />
          {t("Edit and resend", "Bearbeiten und erneut senden")}
        </button>
      )}
    </article>
  );
}
