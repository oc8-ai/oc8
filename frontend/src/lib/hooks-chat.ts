// Direct 1:1 chat with a single agent (oc8.chat.service on the backend).
// Every message is a real AgentRun under the hood, so guardrails/approvals
// apply exactly as they do to an autonomous run -- this file only has to
// manage the session/message list, not any of that. Live updates arrive on
// the socket (lib/live/apply-event.ts); nothing here polls.

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api, uploadChatAttachment, type FileAttachmentDTO } from "@/lib/api";
import type { LiveReasoning, RunComponentDTO } from "@/lib/hooks";

export type { FileAttachmentDTO } from "@/lib/api";

export interface ChatSessionDTO {
  id: string;
  agentId: string;
  title: string;
  createdAt: string;
  lastMessageAt: string | null;
  // Pinned chats are listed first by `GET /chat/sessions`; pick a default with
  // `mostRecentSession` (lib/chat-sessions.ts), not the list's first entry.
  pinned?: boolean;
  projectId?: string | null;
}

export interface ChatContextRefDTO {
  kind: string;
  id: string;
  label: string;
}

export interface ChatMessageDTO {
  id: string;
  sessionId: string;
  role: "user" | "assistant" | "followup";
  content: string;
  runId: string | null;
  renderedComponents: RunComponentDTO[];
  createdAt: string;
  attachments: FileAttachmentDTO[];
  // How this turn was asked -- "ask" | "plan" | "do" | "summarise", or null for
  // an ordinary message. The transcript renders it as a badge; the command word
  // itself is not part of `content` (the backend strips it).
  mode: string | null;
  contextRefs: ChatContextRefDTO[];
  // The member who wrote a human turn (null for assistant turns and old rows).
  authorMemberId: string | null;
  authorName: string | null;
  // For an assistant turn: the user turn it answers. Regenerated answers share it.
  replyToId: string | null;
  // C2: one-click replies the model offered with suggest_replies.
  suggestions?: string[];
  // Typed parts an edition tool returned (spec §6.2), e.g.
  // {type: "sources", items: ChatSourceDTO[]} -- stored and delivered unchanged.
  parts?: Record<string, unknown>[];
}

export interface ChatSourceDTO {
  n: number;
  title: string;
  url: string;
  favicon: string | null;
  snippet: string;
  kind: "web" | "knowledge";
}

/** The items of a message's `sources` part, if an edition tool added one. */
export function sourcesOf(message: ChatMessageDTO | null | undefined): ChatSourceDTO[] {
  const part = (message?.parts ?? []).find((p) => p.type === "sources");
  return Array.isArray(part?.items) ? (part.items as ChatSourceDTO[]) : [];
}

/** The `answers` part of a user turn that answered an agent's questions
 *  (spec §4.1). Its questions are stored unchanged, so in snake_case; only
 *  the fields the summary reads are typed. */
export interface ChatAnswersPart {
  questions: Array<{ id: string; prompt: string; options: Array<{ key: string; label: string }> }>;
  answers: Record<string, string[] | string>;
}

export function answersOf(message: ChatMessageDTO | null | undefined): ChatAnswersPart | null {
  const part = (message?.parts ?? []).find((p) => p.type === "answers");
  if (!part || !Array.isArray(part.questions) || typeof part.answers !== "object" || !part.answers)
    return null;
  return {
    questions: part.questions as ChatAnswersPart["questions"],
    answers: part.answers as ChatAnswersPart["answers"],
  };
}

export const chatKeys = {
  sessions: (agentId?: string) => ["chat", "sessions", agentId ?? "all"] as const,
  messages: (sessionId: string) => ["chat", "messages", sessionId] as const,
};

export function useChatSessions(agentId?: string) {
  return useQuery({
    queryKey: chatKeys.sessions(agentId),
    queryFn: () =>
      api.get<ChatSessionDTO[]>(`/chat/sessions${agentId ? `?agentId=${agentId}` : ""}`),
  });
}

export function useCreateChatSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (agentId: string) => api.post<ChatSessionDTO>("/chat/sessions", { agentId }),
    onSuccess: (session) => {
      qc.invalidateQueries({ queryKey: chatKeys.sessions(session.agentId) });
      qc.invalidateQueries({ queryKey: chatKeys.sessions() });
    },
  });
}

// No polling (spec §2.1): new turns arrive as "chat.message_created" on the
// live socket (lib/live/apply-event.ts), and a terminal "run.status" refetches
// as a backstop.
export function useChatMessages(sessionId: string | null) {
  return useQuery({
    queryKey: chatKeys.messages(sessionId ?? ""),
    queryFn: () => api.get<ChatMessageDTO[]>(`/chat/sessions/${sessionId}/messages`),
    enabled: !!sessionId,
  });
}

/** Newest activity first, pinned chats before the rest: the order of
 *  `GET /chat/sessions`, kept when a write changes one row in the cache. */
function inListOrder(sessions: ChatSessionDTO[]): ChatSessionDTO[] {
  return [...sessions].sort(
    (a, b) =>
      Number(!!b.pinned) - Number(!!a.pinned) ||
      (b.lastMessageAt ?? "").localeCompare(a.lastMessageAt ?? ""),
  );
}

/** A write's reply (the session as the server now has it) written into every
 *  cached session list and the session itself: no refetch of the whole list. */
export function writeSessionToCache(qc: QueryClient, session: ChatSessionDTO) {
  const { id, title, lastMessageAt, pinned, projectId } = session;
  qc.setQueriesData<ChatSessionDTO[]>({ queryKey: ["chat", "sessions"] }, (prev) =>
    prev?.some((s) => s.id === id)
      ? inListOrder(
          prev.map((s) => (s.id === id ? { ...s, title, lastMessageAt, pinned, projectId } : s)),
        )
      : prev,
  );
  qc.setQueryData<ChatSessionDTO>(["chat", "session", id], (prev) =>
    prev ? { ...prev, title, lastMessageAt, pinned, projectId } : prev,
  );
}

export function useRenameChatSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ sessionId, title }: { sessionId: string; title: string }) =>
      api.patch<ChatSessionDTO>(`/chat/sessions/${sessionId}`, { title }),
    onSuccess: (session) => writeSessionToCache(qc, session),
  });
}

export function useDeleteChatSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => api.delete<void>(`/chat/sessions/${sessionId}`),
    onSuccess: (_void, sessionId) => {
      qc.setQueriesData<ChatSessionDTO[]>({ queryKey: ["chat", "sessions"] }, (prev) =>
        prev?.filter((s) => s.id !== sessionId),
      );
      qc.removeQueries({ queryKey: ["chat", "session", sessionId] });
      qc.removeQueries({ queryKey: chatKeys.messages(sessionId) });
    },
  });
}

export function useSendChatMessage(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      message,
      attachmentIds,
      contextRefs,
    }: {
      message: string;
      attachmentIds?: string[];
      contextRefs?: { kind: string; id: string }[];
    }) =>
      api.post<ChatMessageDTO>(`/chat/sessions/${sessionId}/messages`, {
        message,
        attachmentIds,
        contextRefs,
      }),
    onSuccess: (message) => {
      qc.setQueryData<ChatMessageDTO[]>(chatKeys.messages(sessionId), (prev) => [
        ...(prev ?? []),
        message,
      ]);
      qc.invalidateQueries({ queryKey: chatKeys.sessions(message.sessionId) });
    },
  });
}

// Uploads a file ahead of the message that will reference it -- see
// `oc8.api.v1.files`'s `upload_chat_attachment`: it's owned by the SESSION
// until the message that names it in `attachmentIds` is actually sent, at
// which point `chat/service.py::send_message` re-points `owner_id` to the
// new `ChatMessage`. Nothing here removes an attachment the caller uploaded
// and then never sent -- see the Conversation composer's pending-chip removal.
export function useUploadChatAttachment(sessionId: string) {
  return useMutation({
    mutationFn: (file: File) => uploadChatAttachment(sessionId, file),
  });
}

export interface RunActivityDTO {
  id: string;
  agentId: string;
  state: string;
  phase: string | null;
  output: string | null;
  // See RunDTO.failureReason (lib/hooks.ts): both routes share run_to_dto.
  failureReason?: string | null;
  steps: number;
  toolCalls: Record<string, unknown>[];
  // Same list GET /runs/{id} returns (both routes share run_to_dto) --
  // optional because a run recorded before this field existed simply has
  // none, and because the WS patcher may fill it in before a refetch does.
  stepTimings?: Record<string, unknown>[];
  taskId: string | null;
  question: string | null;
  renderedComponents: RunComponentDTO[];
  // filled by the run.component_rendered live patcher
  components?: RunComponentDTO[];
  // Not part of the GET /runs/{id} response -- only ever appears once the
  // "run.token_delta" WS patcher (live/apply-event.ts) has concatenated at
  // least one fragment onto this cache entry. Absent (not "") until then.
  liveAnswer?: string;
  // Same WS-only rule, from "run.reasoning_delta": the reasoning of the step
  // in progress.
  liveReasoning?: LiveReasoning;
}
