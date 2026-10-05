// Direct 1:1 chat with a single agent. Reused verbatim in two places: the
// top-level /chat route (pick any agent you can see) and the agent detail
// page's own "Chat" tab (routes/agents.$id.tsx) -- same component, same
// mechanism, per the brainstormed design: a chat turn is a real AgentRun
// (source="chat"), so guardrails/approvals apply exactly as they do to an
// autonomous run.

import { useEffect, useRef, useState, type ChangeEvent, type ReactNode } from "react";
import {
  BookOpen,
  ChevronDown,
  MessageSquare,
  Paperclip,
  Pencil,
  Plus,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { ChatMarkdown } from "@/components/chat-markdown";
import {
  ComposerHint,
  SigilPopover,
  activeSigil,
  cycleIndex,
  replaceSigil,
  type SigilItem,
  type SigilToken,
} from "@/components/composer-sigils";
import { RUN_COMPONENT_REGISTRY } from "@/components/run-record-card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useConfirm } from "@/hooks/use-confirm";
import { MAX_ATTACHMENT_BYTES } from "@/lib/api";
import {
  chatCommands,
  commandSummary,
  detectCommand,
  matchCommands,
  useChatModes,
} from "@/lib/chat-commands";
import { nextStepChips } from "@/lib/chat-suggestions";
import { useCan } from "@/lib/governance-hooks";
import { useAnswerClarification, useClarifications, useKnowledgeBases } from "@/lib/hooks";
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

export function ChatWindow({
  agentId,
  agentName,
  promptStarters = [],
  className,
  hideHeader = false,
  sessionId: controlledSessionId,
  onSessionChange,
  emptyIntro,
}: {
  agentId: string;
  agentName: string;
  /** Shipped with the agent template (`AgentDTO.promptStarters`, §5.3),
   *  offered as chips on an empty conversation. Passed down from the route
   *  that already holds the full agent object (routes/agents.$id.tsx)
   *  rather than fetched again here. */
  promptStarters?: string[];
  /** Replaces the default fixed height (h-[560px]) of the panel. */
  className?: string;
  hideHeader?: boolean;
  /** Controlled session id; without it the window keeps its own state. */
  sessionId?: string | null;
  onSessionChange?: (sessionId: string | null) => void;
  /** Shown above the starters, only on an empty transcript. */
  emptyIntro?: ReactNode;
}) {
  const t = useT();
  const { data: sessions, isLoading: sessionsLoading } = useChatSessions(agentId);
  const createSession = useCreateChatSession();
  const [innerSessionId, setInnerSessionId] = useState<string | null>(null);
  const sessionId = controlledSessionId !== undefined ? controlledSessionId : innerSessionId;
  const setSessionId = onSessionChange ?? setInnerSessionId;

  // Default to the most recent session once sessions load. Never runs again
  // once the reader has one selected -- including a brand new one just
  // created below -- so this can't clobber an explicit choice.
  useEffect(() => {
    if (sessionId !== null) return;
    if (sessions && sessions.length > 0) setSessionId(sessions[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setSessionId is a stable choice per render
  }, [sessions, sessionId]);

  const { data: messages, isLoading: messagesLoading } = useChatMessages(sessionId);
  const sendMessage = useSendChatMessage(sessionId ?? "");
  const uploadAttachment = useUploadChatAttachment(sessionId ?? "");
  const [draft, setDraft] = useState("");
  const [pendingAttachments, setPendingAttachments] = useState<FileAttachmentDTO[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const can = useCan();
  const { data: modes } = useChatModes();
  const commands = chatCommands(modes, can);
  const [caret, setCaret] = useState(0);
  const [pickerIndex, setPickerIndex] = useState(0);
  const [contextRefs, setContextRefs] = useState<{ id: string; label: string }[]>([]);
  const draftRef = useRef<HTMLTextAreaElement>(null);
  const sigil: SigilToken | null = activeSigil(draft, caret);
  // Only fetched while the `#` picker is actually open: a knowledge-base list
  // is a paged query and nobody needs it for every chat render.
  const { data: bases } = useKnowledgeBases({
    search: sigil?.kind === "#" ? sigil.query : undefined,
    pageSize: 8,
  });
  const armed = detectCommand(draft, commands);

  const sigilItems: SigilItem[] =
    sigil?.kind === "/"
      ? matchCommands(sigil.query, commands).map((c) => ({
          id: c.key,
          label: `/${c.key}`,
          hint: commandSummary(c.key, c.summary, t),
        }))
      : sigil?.kind === "#"
        ? (bases?.items ?? [])
            .filter((kb) => !contextRefs.some((ref) => ref.id === kb.id))
            .map((kb) => ({ id: kb.id, label: kb.name, hint: kb.description || undefined }))
        : [];

  function pickSigilItem(item: SigilItem) {
    if (!sigil) return;
    if (sigil.kind === "/") {
      // The command STAYS in the text: the backend is the parser, and a mode
      // held only in React state would be lost by any other door.
      setDraft(replaceSigil(draft, sigil, `/${item.id.replace(/^\//, "")} `));
    } else {
      setContextRefs((prev) => [...prev, { id: item.id, label: item.label }]);
      setDraft(replaceSigil(draft, sigil, ""));
    }
    setPickerIndex(0);
    draftRef.current?.focus();
  }

  useEffect(() => {
    const el = scrollRef.current;
    if (el && typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight });
  }, [messages]);

  function startNewSession(firstMessage?: string) {
    createSession.mutate(agentId, {
      onSuccess: (session) => {
        if (firstMessage) setPendingStarter({ sessionId: session.id, message: firstMessage });
        setSessionId(session.id);
      },
    });
  }

  // A starter clicked before any session exists: the session is created first,
  // and the starter goes out as its first message once `sendMessage` is bound
  // to that session id (it is a per-session hook).
  const [pendingStarter, setPendingStarter] = useState<{
    sessionId: string;
    message: string;
  } | null>(null);
  useEffect(() => {
    if (!pendingStarter || pendingStarter.sessionId !== sessionId) return;
    setPendingStarter(null);
    sendMessage.mutate({ message: pendingStarter.message });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fire once per pending starter
  }, [pendingStarter, sessionId]);

  function submit() {
    const trimmed = draft.trim();
    if (!trimmed || !sessionId) return;
    setDraft("");
    const attachmentIds = pendingAttachments.map((a) => a.id);
    const refs = contextRefs.map((ref) => ({ kind: "knowledge_base", id: ref.id }));
    sendMessage.mutate(
      { message: trimmed, attachmentIds, contextRefs: refs },
      {
        onSuccess: () => {
          setPendingAttachments([]);
          // Per-message, not per-session: `#` attaches context to THIS turn
          // (§5.2), so the next one starts clean.
          setContextRefs([]);
        },
      },
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

  // Same deterministic chips as the copilot dock (§5.3, lib/chat-suggestions.ts).
  // This page has no approvals/budget fetch of its own (unlike the dock, which
  // is mounted once for the whole app session) -- passing zero-value defaults
  // here means those two chips simply never appear, which is fine: the
  // `/do`-carry-out-plan and summarise chips, plus this agent's own prompt
  // starters, are what matter on a single-agent chat page.
  const hasMessages = !!messages && messages.length > 0;
  const lastUserTurn = [...(messages ?? [])].reverse().find((m) => m.role === "user");
  const suggestions = nextStepChips(
    {
      hasMessages,
      lastTurnRole: messages?.length ? messages[messages.length - 1].role : null,
      lastUserMode: lastUserTurn?.mode ?? null,
      pendingApprovals: 0,
      budgetSoftExceeded: false,
      mayViewBudget: can("budget:view"),
      promptStarters,
    },
    t,
  );

  return (
    <Panel className={cn("flex flex-col overflow-hidden", className ?? "h-[560px]")}>
      {!hideHeader && (
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
              onClick={() => startNewSession()}
              disabled={createSession.isPending}
              className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
            >
              <Plus className="h-3.5 w-3.5" />
              {t("New chat", "Neuer Chat")}
            </button>
          </div>
        </div>
      )}

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {sessionsLoading ? (
          <div className="py-10 text-center text-xs text-muted-foreground">
            {t("Loading…", "Wird geladen…")}
          </div>
        ) : !sessionId ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 py-10 text-center">
            {emptyIntro ?? <MessageSquare className="h-6 w-6 text-muted-foreground/60" />}
            <p className="text-sm text-muted-foreground">
              {t(
                `Start a direct chat with ${agentName}.`,
                `Starte einen direkten Chat mit ${agentName}.`,
              )}
            </p>
            <button
              type="button"
              onClick={() => startNewSession()}
              disabled={createSession.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
            >
              <Plus className="h-3.5 w-3.5" />
              {t("Start chat", "Chat starten")}
            </button>
            {promptStarters.length > 0 && (
              <div className="flex flex-wrap justify-center gap-1.5">
                {promptStarters.map((starter) => (
                  <button
                    key={starter}
                    type="button"
                    disabled={createSession.isPending}
                    onClick={() => startNewSession(starter)}
                    className="rounded-full border border-border bg-background/40 px-2.5 py-1 text-[11px] text-muted-foreground transition hover:text-foreground disabled:opacity-50"
                  >
                    {starter}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : messagesLoading ? (
          <div className="py-10 text-center text-xs text-muted-foreground">
            {t("Loading…", "Wird geladen…")}
          </div>
        ) : !messages || messages.length === 0 ? (
          <div className="py-10 text-center text-xs text-muted-foreground">
            {emptyIntro}
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
                    : m.role === "followup"
                      ? "border border-dashed border-border bg-muted/40 text-muted-foreground"
                      : "border border-border bg-background/60",
                )}
              >
                {m.role === "followup" ? (
                  <div className="space-y-1">
                    <div className="text-[10px] font-medium uppercase tracking-wide">
                      {t("Follow-up", "Wiedervorlage")}
                    </div>
                    <div className="whitespace-pre-wrap">{m.content}</div>
                  </div>
                ) : m.role === "user" ? (
                  <div className="space-y-1">
                    {m.mode && (
                      <span className="inline-block rounded-full bg-primary-foreground/20 px-1.5 py-0.5 font-mono text-[10px]">
                        /{m.mode}
                      </span>
                    )}
                    <div className="whitespace-pre-wrap">{m.content}</div>
                    {m.contextRefs.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {m.contextRefs.map((ref) => (
                          <span
                            key={ref.id}
                            className="inline-flex items-center gap-1 rounded-full border border-primary-foreground/30 px-1.5 py-0.5 text-[10px]"
                          >
                            <BookOpen className="h-2.5 w-2.5" />
                            {ref.label}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
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

      {sessionId && suggestions.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-4 pb-2">
          {suggestions.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => {
                setDraft(s.insert);
                draftRef.current?.focus();
              }}
              className="rounded-full border border-border bg-background/40 px-2.5 py-1 text-[11px] text-muted-foreground transition hover:text-foreground"
            >
              {s.label}
            </button>
          ))}
        </div>
      )}

      {sessionId && (
        <div className="border-t border-border px-4 py-3">
          {sigil && (sigil.kind === "/" || sigil.kind === "#") && (
            <SigilPopover
              title={
                sigil.kind === "/"
                  ? t("Commands", "Befehle")
                  : t("Attach context", "Kontext anhängen")
              }
              items={sigilItems}
              activeIndex={pickerIndex}
              onPick={pickSigilItem}
              onHoverIndex={setPickerIndex}
              emptyText={
                sigil.kind === "/"
                  ? t("No command matches that.", "Kein Befehl passt dazu.")
                  : t("No knowledge base matches that.", "Keine Wissensbasis passt dazu.")
              }
            />
          )}
          {armed && (
            <div className="mb-2 flex items-center gap-1.5 text-[11px]">
              <span className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-primary">
                /{armed.command.key}
              </span>
              <span className="text-muted-foreground">
                {commandSummary(armed.command.key, armed.command.summary, t)}
              </span>
            </div>
          )}
          {contextRefs.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-1.5">
              {contextRefs.map((ref) => (
                <span
                  key={ref.id}
                  className="inline-flex max-w-[200px] items-center gap-1 truncate rounded-full border border-border bg-background/60 py-0.5 pl-2 pr-1 text-[11px] text-muted-foreground"
                >
                  <BookOpen className="h-2.5 w-2.5 shrink-0" />
                  <span className="truncate">{ref.label}</span>
                  <button
                    type="button"
                    onClick={() => setContextRefs((p) => p.filter((r) => r.id !== ref.id))}
                    title={t("Remove", "Entfernen")}
                    className="rounded-full p-0.5 transition hover:text-foreground"
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                </span>
              ))}
            </div>
          )}
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
              ref={draftRef}
              rows={1}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setCaret(e.target.selectionStart ?? e.target.value.length);
                setPickerIndex(0);
              }}
              onKeyUp={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
              onClick={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
              onKeyDown={(e) => {
                const open = sigil && (sigil.kind === "/" || sigil.kind === "#");
                if (open && sigilItems.length > 0) {
                  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                    e.preventDefault();
                    setPickerIndex((i) =>
                      cycleIndex(i, e.key === "ArrowDown" ? 1 : -1, sigilItems.length),
                    );
                    return;
                  }
                  if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
                    // Enter picks from the picker rather than sending: a reader
                    // mid-`@`/`#`/`/` is choosing, not finished.
                    e.preventDefault();
                    pickSigilItem(sigilItems[pickerIndex]);
                    return;
                  }
                }
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
          <ComposerHint />
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
