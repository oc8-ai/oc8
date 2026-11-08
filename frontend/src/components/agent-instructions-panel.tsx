import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { Paperclip, RotateCcw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useConfirm } from "@/hooks/use-confirm";
import { MAX_ATTACHMENT_BYTES } from "@/lib/api";
import {
  useAgentInstructionFiles,
  useAgentInstructionHistory,
  useAssistant,
  useDeleteInstructionFile,
  useUpdateAgentInstructions,
  useUploadInstructionFile,
  type AgentInstructionRevision,
  type FileAttachmentDTO,
} from "@/lib/hooks";
import {
  useChatSessions,
  useCreateChatSession,
  useChatMessages,
  useSendChatMessage,
} from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { useCan } from "@/lib/governance-hooks";
import { cn } from "@/lib/utils";

interface VersionRow {
  text: string;
  ts: string | null;
  by: string | null;
  /** True only for the oldest version once every older page has been
   * loaded -- an as-yet-unloaded page could still hold an earlier
   * transition, so "Since creation" would be a guess until then. */
  isKnownOldest: boolean;
}

/** Rebuilds the version timeline from the current mission plus however much
 * of the audit trail's before/after pairs is loaded so far. `revisions` is
 * newest first (R3, R2, R1 for a history A->B->C->D where D is current): a
 * version's own "became active" timestamp is the NEXT revision's `ts` in
 * this newest-first order (D's is R3.ts, C's is R2.ts, B's is R1.ts),
 * because that revision is the one whose `after` made it current -- the
 * last loaded version's transition-in lives on a revision that may not be
 * loaded yet, so its ts/by only resolve to "since creation" once
 * `allLoaded` confirms there is truly nothing older. */
function buildVersions(
  mission: string,
  revisions: AgentInstructionRevision[],
  allLoaded: boolean,
): VersionRow[] {
  return [
    {
      text: mission,
      ts: revisions[0]?.ts ?? null,
      by: revisions[0]?.by ?? null,
      isKnownOldest: false,
    },
    ...revisions.map((rev, i) => {
      const isLast = i === revisions.length - 1;
      return {
        text: rev.before,
        ts: revisions[i + 1]?.ts ?? null,
        by: revisions[i + 1]?.by ?? null,
        isKnownOldest: isLast && allLoaded,
      };
    }),
  ];
}

/** "Draft with copilot" drawer body for AgentInstructionsPanel below. Mounted
 * only while the drawer is open (`assistOpen && mayUseCopilot` at the call
 * site) so its chat-pipeline hooks -- `useAssistant`, `useChatSessions`, the
 * per-message poll -- only ever fire while someone is actually using it, not
 * on every agent-detail-page view.
 *
 * Talks to the tenant's real Assistant agent over the same session/message
 * hooks `ChatWindow` (chat-window.tsx) and the floating Copilot dock
 * (copilot-dock.tsx) use -- no separate mechanism. There is no "Start chat"
 * step here (unlike ChatWindow): the first Generate click lazily creates the
 * session itself via `pendingMessage`, mirroring copilot-dock.tsx's identical
 * create-then-send bootstrap. Because the Assistant is a single tenant-wide
 * agent, this reuses whatever session already exists for it (e.g. one
 * started from the floating dock) rather than starting a second, parallel
 * conversation.
 *
 * A generate request is matched to its reply by run id, not by transcript
 * position: `POST /chat/sessions/{id}/messages` (api/v1/chat.py) returns the
 * id of the run it just enqueued for THIS specific message, in a `runId`
 * field on the response only (never persisted on the user's own
 * `ChatMessage` row). `awaitingRunId` holds that id, and the reply is taken
 * once an assistant message carrying the same `runId` shows up in the
 * transcript -- not "whichever assistant turn lands next" or "the transcript
 * reached length N". That distinction matters specifically because the dock
 * and this panel share one Assistant session: a message sent through the
 * floating dock while a draft-generate request is still in flight must never
 * be mistaken for the generated draft, and matching by content or position
 * both could be fooled by that interleaving -- matching by run id can't be.
 */
function DraftWithCopilot({
  agentName,
  rough,
  onRoughChange,
  onClose,
  onInsert,
}: {
  agentName: string;
  rough: string;
  onRoughChange: (value: string) => void;
  onClose: () => void;
  onInsert: (text: string) => void;
}) {
  const t = useT();
  const de = t("en", "de") === "de";

  const { data: assistant } = useAssistant();
  const assistantAgentId = assistant?.agentId;
  const { data: sessions } = useChatSessions(assistantAgentId);
  const createSession = useCreateChatSession();
  const [sessionId, setSessionId] = useState<string | null>(null);

  // Same bootstrap as ChatWindow/copilot-dock.tsx: default to the most
  // recent existing session once the Assistant's id and its sessions have
  // both loaded; never runs again once one is selected.
  useEffect(() => {
    if (!assistantAgentId) return;
    if (sessionId !== null) return;
    if (sessions && sessions.length > 0) setSessionId(sessions[0].id);
  }, [assistantAgentId, sessions, sessionId]);

  const { data: messages } = useChatMessages(sessionId);
  const sendMessage = useSendChatMessage(sessionId ?? "");

  const [proposedDraft, setProposedDraft] = useState<string | null>(null);
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  // The run id `POST /chat/sessions/{id}/messages` returns for THIS specific
  // send (api/v1/chat.py's post_message -- a same-response-only signal, not
  // a persisted column). Correlating by run id rather than by transcript
  // position/length matters here specifically because the dock and this
  // panel deliberately talk to the same tenant-wide Assistant session: a
  // message sent through the floating dock while a draft-generate request is
  // in flight would otherwise be able to land in "our" slot and get taken as
  // the generated draft.
  const [awaitingRunId, setAwaitingRunId] = useState<string | null>(null);

  function fail() {
    setAwaitingRunId(null);
    toast.error(t("Couldn't reach the copilot", "Copilot war nicht erreichbar"));
  }

  function sendAndTrack(message: string) {
    sendMessage.mutate(
      { message },
      {
        onSuccess: (userMessage) => {
          if (userMessage.runId) {
            setAwaitingRunId(userMessage.runId);
          } else {
            // No run was enqueued for this message (e.g. the Assistant's
            // secret-blindness gate refused it outright) -- there is nothing
            // to wait for.
            fail();
          }
        },
        onError: fail,
      },
    );
  }

  // A generate request made before any session existed yet: send it as soon
  // as `sessionId` (and therefore a `sendMessage` bound to the right
  // session) lands on the next render.
  useEffect(() => {
    if (!sessionId || pendingMessage === null) return;
    const message = pendingMessage;
    setPendingMessage(null);
    sendAndTrack(message);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, pendingMessage]);

  useEffect(() => {
    if (!awaitingRunId || !messages) return;
    const reply = messages.find((m) => m.role === "assistant" && m.runId === awaitingRunId);
    if (!reply) return;
    setProposedDraft(reply.content);
    setAwaitingRunId(null);
  }, [messages, awaitingRunId]);

  // `awaitingRunId` alone has a gap: it's only set inside sendAndTrack's
  // onSuccess, so it's still null for the moment a mutation is actually in
  // flight (session-create OR the message post itself) -- fold that in too,
  // so Generate can't be double-clicked in that window.
  const generating = awaitingRunId !== null || createSession.isPending || sendMessage.isPending;

  function generateDraft() {
    const desc = rough.trim();
    if (!desc || generating) return;
    const message = de
      ? `Schreibe eine vollständige, klare Instruction (dauerhafter System-Prompt) für den Agenten "${agentName}". Grobe Beschreibung, was er tun soll: ${desc}\n\nGib NUR den fertigen Instruction-Text zurück -- ohne Einleitung, ohne Erklärung, ohne Anführungszeichen drumherum.`
      : `Write a complete, clear instruction (standing system prompt) for the agent "${agentName}". Rough description of what it should do: ${desc}\n\nReturn ONLY the finished instruction text -- no preamble, no explanation, no surrounding quotes.`;
    setProposedDraft(null);
    if (sessionId) {
      sendAndTrack(message);
    } else if (assistantAgentId) {
      createSession.mutate(assistantAgentId, {
        onSuccess: (session) => {
          setSessionId(session.id);
          setPendingMessage(message);
        },
        onError: fail,
      });
    } else {
      fail();
    }
  }

  return (
    <div className="mt-4 rounded-md border border-primary/30 bg-primary/5 p-3">
      <div className="flex items-center gap-1.5 micro-label">
        <img src="/octopus_oc8.svg" alt="" className="h-4 w-4" draggable={false} />
        {t("Describe roughly what this agent should do", "Beschreib grob, was der Agent tun soll")}
      </div>
      <textarea
        value={rough}
        onChange={(e) => onRoughChange(e.target.value)}
        rows={2}
        placeholder={t(
          'e.g. "Handles first-level IT support tickets, replies to users, escalates hardware issues."',
          "z. B. „Bearbeitet First-Level-IT-Support-Tickets, antwortet Anwendern, eskaliert Hardware-Probleme.“",
        )}
        className="mt-2 w-full resize-y rounded-md border border-border bg-background/60 px-3 py-2 text-sm outline-none focus:border-primary/50"
      />
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          onClick={generateDraft}
          disabled={!rough.trim() || generating}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {generating ? t("Generating…", "Wird generiert…") : t("Generate", "Generieren")}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-muted-foreground transition hover:text-foreground"
        >
          {t("Close", "Schließen")}
        </button>
      </div>
      {proposedDraft && (
        <div className="mt-3 rounded-md border border-border bg-background/60 p-3">
          <div className="max-h-48 overflow-auto whitespace-pre-wrap font-mono text-xs text-foreground/80">
            {proposedDraft}
          </div>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => onInsert(proposedDraft)}
              className="rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground transition hover:brightness-110"
            >
              {t("Insert into editor", "In Editor einfügen")}
            </button>
            <button
              type="button"
              onClick={() => setProposedDraft(null)}
              className="rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground"
            >
              {t("Discard", "Verwerfen")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** Agent detail page's "Instructions" tab: the persistent, editable system
 * prompt every run sends (`Agent.mission` on the wire), laid out like
 * Impossible Cloud's IAM policy-version editor (editor pane + a numbered
 * "Versions" side panel with a Default badge and per-row restore) at the
 * user's explicit request -- adapted to oc8's own tamper-evident audit
 * chain (`agent.instructions.updated` events) instead of a bounded five-slot
 * version store, so nothing is pruned. The panel loads history a page at a
 * time (cursor pagination, same shape as the audit screen) so an agent
 * edited a hundred times doesn't drag the tab down loading all of it at
 * once; version numbers still count down from the server-reported total, so
 * "v1" stays correct however much of the list is actually on screen.
 * Restoring loads the old text into the draft rather than saving
 * immediately (Save still gates it), so a restore reads the same as any
 * other edit in the history it creates. Skills stay a separate, more
 * granular mechanism layered on top -- this tab is only the base
 * instructions every run carries. */
export function AgentInstructionsPanel({
  agentId,
  agentName,
  mission,
  mayManage,
}: {
  agentId: string;
  agentName: string;
  mission: string;
  mayManage: boolean;
}) {
  const t = useT();
  // Seeded once from the loaded agent, like AgentRuntimePanel's own `draft`
  // -- a later external change (another tab/session editing the same agent)
  // is the same rare, unhandled edge case that panel also leaves alone.
  const [draft, setDraft] = useState(mission);
  const update = useUpdateAgentInstructions();
  const history = useAgentInstructionHistory(agentId);

  // "Attached files": reference material (PDF, Excel, images) this agent can
  // read on demand via read_instruction_file, the same shape as ChatWindow's
  // upload button but scoped to owner_type="agent_instructions" instead of a
  // single chat turn -- see agent/preamble.py's has_instruction_files.
  const files = useAgentInstructionFiles(agentId);
  const uploadFile = useUploadInstructionFile(agentId);
  const deleteInstructionFile = useDeleteInstructionFile(agentId);
  const { confirm, ConfirmDialog } = useConfirm();
  const fileInputRef = useRef<HTMLInputElement>(null);

  function handleFileChosen(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    // Client half of the 25 MB cap -- fast feedback only; the server's own
    // check is what actually enforces it (see MAX_ATTACHMENT_BYTES).
    if (file.size > MAX_ATTACHMENT_BYTES) {
      toast.error(t("File is too large", "Datei ist zu groß"), {
        description: t("Attachments are limited to 25 MB.", "Anhänge sind auf 25 MB begrenzt."),
      });
      return;
    }
    uploadFile.mutate(file, {
      onError: (error: Error) =>
        toast.error(t("Couldn't upload the file", "Datei konnte nicht hochgeladen werden"), {
          description: error.message,
        }),
    });
  }

  async function handleDeleteFile(file: FileAttachmentDTO) {
    const ok = await confirm({
      title: t("Delete this file?", "Diese Datei löschen?"),
      description: t(
        `Permanently delete "${file.filename}"? The agent will no longer be able to read it.`,
        `"${file.filename}" endgültig löschen? Der Agent kann sie danach nicht mehr lesen.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    deleteInstructionFile.mutate(file.id, {
      onSuccess: () =>
        toast.success(t("File deleted", "Datei gelöscht"), { description: file.filename }),
      onError: (error: Error) =>
        toast.error(t("Couldn't delete the file", "Datei konnte nicht gelöscht werden"), {
          description: error.message,
        }),
    });
  }

  // Same permission the floating oc8 Copilot dock (copilot-dock.tsx) already
  // gates on -- this reuses that dock's chat pipeline against the same
  // tenant-wide Assistant agent, so anyone who can see the dock can also
  // reach it from here.
  const can = useCan();
  const mayUseCopilot = can("copilot:use");
  const [assistOpen, setAssistOpen] = useState(false);
  const [rough, setRough] = useState("");

  const dirty = draft !== mission;
  const revisions = history.data?.pages.flatMap((p) => p.revisions) ?? [];
  const totalCount = history.data?.pages[0]?.totalCount ?? revisions.length;
  const versions = buildVersions(mission, revisions, !history.hasNextPage);
  const totalVersions = totalCount + 1;

  function save() {
    update.mutate(
      { agentId, instructions: draft },
      {
        onSuccess: () => toast.success(t("Instructions saved", "Anweisungen gespeichert")),
        onError: (error: Error) =>
          toast.error(
            t("Couldn't save instructions", "Anweisungen konnten nicht gespeichert werden"),
            {
              description: error.message,
            },
          ),
      },
    );
  }

  function restore(text: string) {
    setDraft(text);
    toast(
      t(
        "Version loaded — click Save to restore it",
        "Version geladen — zum Wiederherstellen auf Speichern klicken",
      ),
    );
  }

  return (
    <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_320px]">
      <Panel className="p-5">
        <div className="flex items-start justify-between gap-2">
          <div>
            <div className="micro-label">
              {t("standing system prompt", "dauerhafter System-Prompt")}
            </div>
            <h3 className="mt-0.5 font-serif text-lg">{t("Instructions", "Anweisungen")}</h3>
          </div>
          {mayManage && (
            <div className="flex shrink-0 items-center gap-2">
              {mayUseCopilot && (
                <button
                  type="button"
                  onClick={() => setAssistOpen((o) => !o)}
                  title={t(
                    "Let the copilot draft this from a rough description",
                    "Vom Copilot aus einer groben Beschreibung entwerfen lassen",
                  )}
                  aria-label={t("Draft with copilot", "Mit Copilot entwerfen")}
                  className={cn(
                    "grid h-8 w-8 place-items-center rounded-md border transition",
                    assistOpen
                      ? "border-primary/50 bg-primary/10"
                      : "border-border hover:border-primary/40 hover:bg-muted/30",
                  )}
                >
                  <img
                    src="/octopus_oc8.svg"
                    alt=""
                    className="h-5 w-5 select-none"
                    draggable={false}
                  />
                </button>
              )}
              <button
                type="button"
                onClick={save}
                disabled={!dirty || update.isPending}
                className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {update.isPending ? t("Saving…", "Wird gespeichert…") : t("Save", "Speichern")}
              </button>
            </div>
          )}
        </div>
        {assistOpen && mayUseCopilot && (
          <DraftWithCopilot
            agentName={agentName}
            rough={rough}
            onRoughChange={setRough}
            onClose={() => setAssistOpen(false)}
            onInsert={(text) => {
              setDraft(text);
              setRough("");
              setAssistOpen(false);
            }}
          />
        )}
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={!mayManage}
          rows={16}
          placeholder={t(
            "What should this agent do? Sent with every run.",
            "Was soll dieser Agent tun? Wird bei jedem Lauf mitgeschickt.",
          )}
          className="mt-4 w-full resize-y rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-sm outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60"
        />
        <p className="mt-3 text-xs text-muted-foreground">
          {t(
            "For task-specific instructions, assign a skill instead — this field is only the agent's base mission.",
            "Für aufgabenspezifische Anweisungen nutze stattdessen Skills — dieses Feld ist nur die Basis-Mission des Agenten.",
          )}
          {!mayManage &&
            ` ${t("Your role does not include agent:manage, so this field is read-only for you.", "Ihre Rolle enthält agent:manage nicht, deshalb ist dieses Feld für Sie schreibgeschützt.")}`}
        </p>

        <div className="mt-6 border-t border-border pt-4">
          <div className="flex items-center justify-between gap-2">
            <h4 className="text-sm font-medium">{t("Attached files", "Angehängte Dateien")}</h4>
            {mayManage && (
              <>
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
                  disabled={uploadFile.isPending}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition hover:border-primary/40 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Paperclip className="h-3 w-3" />
                  {uploadFile.isPending
                    ? t("Uploading…", "Wird hochgeladen…")
                    : t("Attach file", "Datei anhängen")}
                </button>
              </>
            )}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {t(
              "Reference material this agent can read on demand — never injected into every run automatically.",
              "Referenzmaterial, das dieser Agent bei Bedarf lesen kann — wird nicht automatisch in jeden Lauf eingefügt.",
            )}
          </p>
          {files.isLoading && (
            <p className="mt-3 text-xs text-muted-foreground">{t("Loading…", "Wird geladen…")}</p>
          )}
          {files.isError && (
            <p className="mt-3 text-xs text-destructive">
              {t(
                "Couldn't load attached files.",
                "Angehängte Dateien konnten nicht geladen werden.",
              )}
            </p>
          )}
          {files.isSuccess && files.data.length === 0 && (
            <p className="mt-3 text-xs text-muted-foreground">
              {t("No files attached yet.", "Noch keine Dateien angehängt.")}
            </p>
          )}
          {files.isSuccess && files.data.length > 0 && (
            <ul className="mt-3 space-y-1.5">
              {files.data.map((f) => (
                <li
                  key={f.id}
                  className="flex items-center justify-between gap-2 rounded-md border border-border/60 bg-background/30 px-2.5 py-1.5 text-xs"
                >
                  <span className="flex min-w-0 items-center gap-1.5">
                    <Paperclip className="h-3 w-3 shrink-0 text-muted-foreground" />
                    <span className="truncate" title={f.filename}>
                      {f.filename}
                    </span>
                  </span>
                  {mayManage && (
                    <button
                      type="button"
                      onClick={() => handleDeleteFile(f)}
                      title={t("Delete file", "Datei löschen")}
                      aria-label={t("Delete file", "Datei löschen")}
                      className="shrink-0 rounded-md p-1 text-muted-foreground transition hover:text-destructive"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
        {ConfirmDialog}
      </Panel>

      <Panel className="p-5">
        <h3 className="font-serif text-lg">{t("Versions", "Versionen")}</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          {t(
            "Every save is kept — nothing is pruned.",
            "Jede Speicherung bleibt erhalten — nichts wird gelöscht.",
          )}
        </p>
        {history.isLoading && (
          <p className="mt-3 text-sm text-muted-foreground">{t("Loading…", "Wird geladen…")}</p>
        )}
        {history.isSuccess && (
          <ul className="mt-3 space-y-2">
            {versions.map((v, i) => {
              const versionLabel = `v${totalVersions - i}`;
              const isCurrent = i === 0;
              return (
                <li
                  key={i}
                  className="rounded-md border border-border/60 bg-background/30 p-2.5 text-xs"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5">
                      <span className="font-mono font-medium text-foreground">{versionLabel}</span>
                      {isCurrent && (
                        <span className="rounded-full border border-primary/40 bg-primary/10 px-1.5 py-0.5 micro-label text-primary">
                          {t("Default", "Standard")}
                        </span>
                      )}
                    </div>
                    {/* Gated on the DRAFT, not on isCurrent -- once an older
                     * version is loaded into the editor, the still-current
                     * (Default) row is the only way back without retyping
                     * it by hand, so it needs a Restore button too. */}
                    {v.text !== draft && mayManage && (
                      <button
                        type="button"
                        onClick={() => restore(v.text)}
                        title={t("Load into the editor", "In den Editor laden")}
                        className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:border-primary/40 hover:text-foreground"
                      >
                        <RotateCcw className="h-3 w-3" />
                        {t("Restore", "Wiederherstellen")}
                      </button>
                    )}
                  </div>
                  <div className="mt-1 text-muted-foreground">
                    {v.ts
                      ? new Date(v.ts).toLocaleString()
                      : v.isKnownOldest
                        ? t("Since creation", "Seit Erstellung")
                        : ""}
                    {v.by && ` · ${v.by}`}
                  </div>
                  <p className="mt-1.5 line-clamp-2 text-foreground/80">
                    {v.text || t("(empty)", "(leer)")}
                  </p>
                </li>
              );
            })}
          </ul>
        )}
        {history.hasNextPage && (
          <button
            type="button"
            onClick={() => history.fetchNextPage()}
            disabled={history.isFetchingNextPage}
            className="mt-3 w-full rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground transition hover:border-primary/40 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            {history.isFetchingNextPage
              ? t("Loading…", "Wird geladen…")
              : t("Load older versions", "Ältere Versionen laden")}
          </button>
        )}
      </Panel>
    </div>
  );
}
