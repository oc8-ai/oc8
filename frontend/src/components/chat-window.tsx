// Direct 1:1 chat with a single agent. Reused verbatim in two places: the
// top-level /chat route (pick any agent you can see) and the agent detail
// page's own "Chat" tab (routes/agents.$id.tsx) -- same component, same
// mechanism, per the brainstormed design: a chat turn is a real AgentRun
// (source="chat"), so guardrails/approvals apply exactly as they do to an
// autonomous run.

import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { ChevronDown, MessageSquare, Paperclip, Pencil, Plus, Send, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { ChatMarkdown } from "@/components/chat-markdown";
import { RUN_COMPONENT_REGISTRY } from "@/components/run-record-card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useConfirm } from "@/hooks/use-confirm";
import { MAX_ATTACHMENT_BYTES } from "@/lib/api";
import { useAnswerClarification, useClarifications } from "@/lib/hooks";
import {
  useChatSessions,
  useCreateChatSession,
  useChatMessages,
  useDeleteChatSession,
  useRenameChatSession,
  useSendChatMessage,
  useUploadChatAttachment,
  type ChatSessionDTO,
  type FileAttachmentDTO,
} from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function ChatWindow({ agentId, agentName }: { agentId: string; agentName: string }) {
  const t = useT();
  const { data: sessions, isLoading: sessionsLoading } = useChatSessions(agentId);
  const createSession = useCreateChatSession();
  const [sessionId, setSessionId] = useState<string | null>(null);

  // Default to the most recent session once sessions load. Never runs again
  // once the reader has one selected -- including a brand new one just
  // created below -- so this can't clobber an explicit choice.
  useEffect(() => {
    if (sessionId !== null) return;
    if (sessions && sessions.length > 0) setSessionId(sessions[0].id);
  }, [sessions, sessionId]);

  const { data: messages, isLoading: messagesLoading } = useChatMessages(sessionId);
  const sendMessage = useSendChatMessage(sessionId ?? "");
  const uploadAttachment = useUploadChatAttachment(sessionId ?? "");
  const [draft, setDraft] = useState("");
  const [pendingAttachments, setPendingAttachments] = useState<FileAttachmentDTO[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight });
  }, [messages]);

  function startNewSession() {
    createSession.mutate(agentId, { onSuccess: (session) => setSessionId(session.id) });
  }

  function submit() {
    const trimmed = draft.trim();
    if (!trimmed || !sessionId) return;
    setDraft("");
    const attachmentIds = pendingAttachments.map((a) => a.id);
    sendMessage.mutate(
      { message: trimmed, attachmentIds },
      { onSuccess: () => setPendingAttachments([]) },
    );
  }

  function handleFileChosen(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !sessionId) return;
    // Client half of the 25 MB cap -- fast feedback only; the server's own
    // check is what actually enforces it (see MAX_ATTACHMENT_BYTES).
    if (file.size > MAX_ATTACHMENT_BYTES) {
      toast.error(t("File is too large", "Datei ist zu groß"), {
        description: t("Attachments are limited to 25 MB.", "Anhänge sind auf 25 MB begrenzt."),
      });
      return;
    }
    uploadAttachment.mutate(file, {
      onSuccess: (dto) => setPendingAttachments((p) => [...p, dto]),
      // Without this a rejected upload (413, 422 for a disallowed type, 503
      // for an unreachable object store) did nothing visible at all: no chip
      // appeared and no error was shown.
      onError: (error: Error) =>
        toast.error(t("Couldn't upload the file", "Datei konnte nicht hochgeladen werden"), {
          description: error.message,
        }),
    });
  }

  function removePendingAttachment(id: string) {
    setPendingAttachments((p) => p.filter((a) => a.id !== id));
  }

  // The transcript's own state IS the "is the agent still working" signal --
  // see useChatMessages's poll-or-stop doc comment. No separate flag needed.
  const waitingOnAgent =
    !!messages && messages.length > 0 && messages[messages.length - 1].role === "user";

  // A run parked on `waiting_for_input` never produces an assistant
  // ChatMessage (executor.py deliberately skips record_assistant_reply for
  // that state -- the question lives on the Clarification instead), so
  // without this the box above just says "{agent} is thinking…" forever with
  // no way out: the send box is already disabled while waitingOnAgent, and
  // typing the answer there would only queue a second, competing run rather
  // than resume this one. Matched on agentId, not a tracked run id, so a
  // page reload while parked still finds it (an agent runs at most one
  // thing at a time).
  const [clarificationAnswer, setClarificationAnswer] = useState("");
  const { data: clarifications } = useClarifications(waitingOnAgent ? 3000 : false);
  const openClarification = clarifications?.find((c) => c.agentId === agentId);
  const answerClarification = useAnswerClarification();

  function submitClarificationAnswer() {
    const trimmed = clarificationAnswer.trim();
    if (!trimmed || !openClarification) return;
    answerClarification.mutate(
      { clarificationId: openClarification.id, answer: trimmed },
      { onSuccess: () => setClarificationAnswer("") },
    );
  }

  return (
    <Panel className="flex h-[560px] flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="text-sm font-medium">
          {t(`Chat with ${agentName}`, `Chat mit ${agentName}`)}
        </div>
        <div className="flex items-center gap-2">
          {sessions && sessions.length > 0 && (
            <ChatSessionPicker
              agentId={agentId}
              sessions={sessions}
              sessionId={sessionId}
              onSelect={setSessionId}
            />
          )}
          <button
            type="button"
            onClick={startNewSession}
            disabled={createSession.isPending}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" />
            {t("New chat", "Neuer Chat")}
          </button>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {sessionsLoading ? (
          <div className="py-10 text-center text-xs text-muted-foreground">
            {t("Loading…", "Wird geladen…")}
          </div>
        ) : !sessionId ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 py-10 text-center">
            <MessageSquare className="h-6 w-6 text-muted-foreground/60" />
            <p className="text-sm text-muted-foreground">
              {t(
                `Start a direct chat with ${agentName}.`,
                `Starte einen direkten Chat mit ${agentName}.`,
              )}
            </p>
            <button
              type="button"
              onClick={startNewSession}
              disabled={createSession.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
            >
              <Plus className="h-3.5 w-3.5" />
              {t("Start chat", "Chat starten")}
            </button>
          </div>
        ) : messagesLoading ? (
          <div className="py-10 text-center text-xs text-muted-foreground">
            {t("Loading…", "Wird geladen…")}
          </div>
        ) : !messages || messages.length === 0 ? (
          <div className="py-10 text-center text-xs text-muted-foreground">
            {t("Say hello to get started.", "Sag Hallo, um loszulegen.")}
          </div>
        ) : (
          messages.map((m) => (
            <div
              key={m.id}
              className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}
            >
              <div
                className={cn(
                  "max-w-[80%] space-y-2 rounded-lg px-3 py-2 text-sm",
                  m.role === "user"
                    ? "bg-primary text-primary-foreground"
                    : "border border-border bg-background/60",
                )}
              >
                {m.role === "user" ? (
                  <div className="whitespace-pre-wrap">{m.content}</div>
                ) : (
                  <ChatMarkdown text={m.content} />
                )}
                {m.renderedComponents.length > 0 && (
                  <div className="space-y-2">
                    {m.renderedComponents.map((c, i) => {
                      const Renderer = Object.prototype.hasOwnProperty.call(
                        RUN_COMPONENT_REGISTRY,
                        c.componentKey,
                      )
                        ? RUN_COMPONENT_REGISTRY[c.componentKey]
                        : undefined;
                      return Renderer ? <Renderer key={i} props={c.props} /> : null;
                    })}
                  </div>
                )}
                {m.attachments && m.attachments.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {m.attachments.map((a) => (
                      <span
                        key={a.id}
                        title={a.filename}
                        className={cn(
                          "inline-flex max-w-[180px] items-center gap-1 truncate rounded-full border px-2 py-0.5 text-[11px]",
                          m.role === "user"
                            ? "border-primary-foreground/30 text-primary-foreground/90"
                            : "border-border text-muted-foreground",
                        )}
                      >
                        <Paperclip className="h-2.5 w-2.5 shrink-0" />
                        <span className="truncate">{a.filename}</span>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))
        )}
        {waitingOnAgent && openClarification && (
          <div className="flex justify-start">
            <div className="max-w-[80%] space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
              <p className="font-medium text-foreground">
                {t(
                  `${agentName} needs more information to continue:`,
                  `${agentName} braucht mehr Informationen, um weiterzumachen:`,
                )}
              </p>
              <p className="whitespace-pre-wrap text-foreground/90">{openClarification.question}</p>
              <div className="flex items-end gap-1.5 pt-1">
                <textarea
                  rows={1}
                  value={clarificationAnswer}
                  onChange={(e) => setClarificationAnswer(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      submitClarificationAnswer();
                    }
                  }}
                  placeholder={t("Your answer…", "Deine Antwort…")}
                  className="max-h-24 flex-1 resize-none rounded-md border border-border bg-background px-2 py-1.5 text-xs outline-none focus:border-primary/50"
                />
                <button
                  type="button"
                  onClick={submitClarificationAnswer}
                  disabled={answerClarification.isPending || !clarificationAnswer.trim()}
                  className="inline-flex items-center rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
                >
                  {t("Answer", "Antworten")}
                </button>
              </div>
            </div>
          </div>
        )}
        {waitingOnAgent && !openClarification && (
          <div className="flex justify-start">
            <div className="max-w-[80%] rounded-lg border border-border bg-background/60 px-3 py-2 text-xs text-muted-foreground">
              {t(`${agentName} is thinking…`, `${agentName} überlegt…`)}
            </div>
          </div>
        )}
      </div>

      {sessionId && (
        <div className="border-t border-border px-4 py-3">
          {pendingAttachments.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-1.5">
              {pendingAttachments.map((a) => (
                <span
                  key={a.id}
                  title={a.filename}
                  className="inline-flex max-w-[200px] items-center gap-1 truncate rounded-full border border-border bg-background/60 py-0.5 pl-2 pr-1 text-[11px] text-muted-foreground"
                >
                  <Paperclip className="h-2.5 w-2.5 shrink-0" />
                  <span className="truncate">{a.filename}</span>
                  <button
                    type="button"
                    onClick={() => removePendingAttachment(a.id)}
                    title={t("Remove", "Entfernen")}
                    className="rounded-full p-0.5 text-muted-foreground transition hover:text-foreground"
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="flex items-end gap-2">
            <input
              ref={fileInputRef}
              type="file"
              aria-label={t("Attach a file", "Datei anhängen")}
              onChange={handleFileChosen}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadAttachment.isPending}
              title={t("Attach a file", "Datei anhängen")}
              className="inline-flex items-center justify-center rounded-md border border-border px-2.5 py-2 text-muted-foreground transition hover:text-foreground disabled:opacity-50"
            >
              <Paperclip className="h-3.5 w-3.5" />
            </button>
            <textarea
              rows={1}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder={t("Type a message…", "Nachricht eingeben…")}
              className="max-h-32 flex-1 resize-none rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
            <button
              type="button"
              onClick={submit}
              disabled={sendMessage.isPending || waitingOnAgent || !draft.trim()}
              aria-label={t("Send", "Senden")}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
            >
              <Send className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}
    </Panel>
  );
}

// Session switcher + per-session rename/delete. A native <select> can't host
// per-row buttons, so this is a dropdown of plain rows instead of
// DropdownMenuItem: an Item's onSelect closes the whole menu, which would
// kill an in-progress rename or interrupt a delete confirmation.
export function ChatSessionPicker({
  agentId,
  sessions,
  sessionId,
  onSelect,
}: {
  agentId: string;
  sessions: ChatSessionDTO[];
  sessionId: string | null;
  onSelect: (sessionId: string | null) => void;
}) {
  const t = useT();
  const rename = useRenameChatSession(agentId);
  const del = useDeleteChatSession(agentId);
  const { confirm, ConfirmDialog } = useConfirm();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

  const current = sessions.find((s) => s.id === sessionId);

  function startRename(s: ChatSessionDTO) {
    setEditingId(s.id);
    setEditValue(s.title || t("Untitled chat", "Unbenannter Chat"));
  }

  function commitRename(s: ChatSessionDTO) {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== s.title) rename.mutate({ sessionId: s.id, title: trimmed });
    setEditingId(null);
  }

  async function handleDelete(s: ChatSessionDTO) {
    const label = s.title || t("Untitled chat", "Unbenannter Chat");
    const ok = await confirm({
      title: t("Delete chat?", "Chat löschen?"),
      description: t(
        `This permanently deletes "${label}" and its messages.`,
        `Löscht „${label}“ und alle Nachrichten dauerhaft.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    del.mutate(s.id, { onSuccess: () => sessionId === s.id && onSelect(null) });
  }

  return (
    <>
      {ConfirmDialog}
      <DropdownMenu>
        <DropdownMenuTrigger className="inline-flex max-w-[220px] items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1 text-xs outline-none">
          <span className="truncate">
            {current?.title || t("Untitled chat", "Unbenannter Chat")}
          </span>
          <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72 p-1">
          {sessions.map((s) => (
            <div
              key={s.id}
              className={cn(
                "group flex items-center gap-1 rounded-sm px-1 py-1 text-sm",
                s.id === sessionId && "bg-accent/60",
              )}
            >
              {editingId === s.id ? (
                <input
                  autoFocus
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      commitRename(s);
                    } else if (e.key === "Escape") {
                      setEditingId(null);
                    }
                  }}
                  onBlur={() => commitRename(s)}
                  className="min-w-0 flex-1 rounded border border-primary/50 bg-background px-1.5 py-0.5 text-xs outline-none"
                />
              ) : (
                <button
                  type="button"
                  onClick={() => onSelect(s.id)}
                  className="flex-1 truncate px-1.5 py-0.5 text-left"
                  title={s.title || t("Untitled chat", "Unbenannter Chat")}
                >
                  {s.title || t("Untitled chat", "Unbenannter Chat")}
                  <span className="ml-1.5 text-[10px] text-muted-foreground">
                    {new Date(s.createdAt).toLocaleDateString()}
                  </span>
                </button>
              )}
              {editingId !== s.id && (
                <div className="flex shrink-0 items-center opacity-0 transition group-hover:opacity-100">
                  <button
                    type="button"
                    title={t("Rename", "Umbenennen")}
                    onClick={(e) => {
                      e.stopPropagation();
                      startRename(s);
                    }}
                    className="rounded p-1 text-muted-foreground transition hover:text-foreground"
                  >
                    <Pencil className="h-3 w-3" />
                  </button>
                  <button
                    type="button"
                    title={t("Delete", "Löschen")}
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleDelete(s);
                    }}
                    className="rounded p-1 text-muted-foreground transition hover:text-destructive"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              )}
            </div>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}
