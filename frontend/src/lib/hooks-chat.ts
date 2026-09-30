// Direct 1:1 chat with a single agent (oc8.chat.service on the backend).
// Every message is a real AgentRun under the hood, so guardrails/approvals
// apply exactly as they do to an autonomous run -- this file only has to
// manage the session/message list and the "is the agent still thinking"
// poll, not any of that.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, uploadChatAttachment, type FileAttachmentDTO } from "@/lib/api";
import type { RunComponentDTO } from "@/lib/hooks";

export type { FileAttachmentDTO } from "@/lib/api";

export interface ChatSessionDTO {
  id: string;
  agentId: string;
  title: string;
  createdAt: string;
  lastMessageAt: string | null;
}

export interface ChatContextRefDTO {
  kind: string;
  id: string;
  label: string;
}

export interface ChatMessageDTO {
  id: string;
  sessionId: string;
  role: "user" | "assistant";
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
}

const keys = {
  sessions: (agentId?: string) => ["chat", "sessions", agentId ?? "all"] as const,
  messages: (sessionId: string) => ["chat", "messages", sessionId] as const,
};

export function useChatSessions(agentId?: string) {
  return useQuery({
    queryKey: keys.sessions(agentId),
    queryFn: () =>
      api.get<ChatSessionDTO[]>(`/chat/sessions${agentId ? `?agentId=${agentId}` : ""}`),
  });
}

export function useCreateChatSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (agentId: string) => api.post<ChatSessionDTO>("/chat/sessions", { agentId }),
    onSuccess: (session) => {
      qc.invalidateQueries({ queryKey: keys.sessions(session.agentId) });
      qc.invalidateQueries({ queryKey: keys.sessions() });
    },
  });
}

// Polls only while the transcript's last turn is the user's own -- i.e. the
// run answering it hasn't reached a terminal state yet. There is no WS event
// dedicated to chat (a run's "run.status" event doesn't know it's answering
// a chat session), so this is the whole "is the agent still typing" signal.
// Stops the moment an assistant turn lands, same shape as
// knowledge-connector-hooks.ts's useIngestionJob poll-or-stop.
export function useChatMessages(sessionId: string | null) {
  return useQuery({
    queryKey: keys.messages(sessionId ?? ""),
    queryFn: () => api.get<ChatMessageDTO[]>(`/chat/sessions/${sessionId}/messages`),
    enabled: !!sessionId,
    refetchInterval: (query) => {
      const data = query.state.data as ChatMessageDTO[] | undefined;
      if (!data || data.length === 0) return false;
      return data[data.length - 1].role === "user" ? 2000 : false;
    },
  });
}

export function useRenameChatSession(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ sessionId, title }: { sessionId: string; title: string }) =>
      api.patch<ChatSessionDTO>(`/chat/sessions/${sessionId}`, { title }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.sessions(agentId) });
      qc.invalidateQueries({ queryKey: keys.sessions() });
    },
  });
}

export function useDeleteChatSession(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => api.delete<void>(`/chat/sessions/${sessionId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.sessions(agentId) });
      qc.invalidateQueries({ queryKey: keys.sessions() });
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
      qc.setQueryData<ChatMessageDTO[]>(keys.messages(sessionId), (prev) => [
        ...(prev ?? []),
        message,
      ]);
      qc.invalidateQueries({ queryKey: keys.sessions(message.sessionId) });
    },
  });
}

// Uploads a file ahead of the message that will reference it -- see
// `oc8.api.v1.files`'s `upload_chat_attachment`: it's owned by the SESSION
// until the message that names it in `attachmentIds` is actually sent, at
// which point `chat/service.py::send_message` re-points `owner_id` to the
// new `ChatMessage`. Nothing here removes an attachment the caller uploaded
// and then never sent -- see chat-window.tsx's pending-chip removal.
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
  steps: number;
  toolCalls: Record<string, unknown>[];
  // Same list GET /runs/{id} returns (both routes share run_to_dto) --
  // optional because a run recorded before this field existed simply has
  // none, and because the WS patcher may fill it in before a refetch does.
  stepTimings?: Record<string, unknown>[];
  taskId: string | null;
  question: string | null;
  renderedComponents: RunComponentDTO[];
  // Not part of the GET /runs/{id} response -- only ever appears once the
  // "run.token_delta" WS patcher (live/apply-event.ts) has concatenated at
  // least one fragment onto this cache entry. Absent (not "") until then.
  liveAnswer?: string;
}

// Seeds the exact ["run", runId] cache entry the WS patchers in
// live/apply-event.ts already write to (run.status/output_delta/
// component_rendered/token_delta) -- those patchers only ever UPDATE an
// existing entry (`prev ? {...} : prev`), they never create one, so this
// hook's own fetch is what makes a freshly opened tab's live-activity strip
// receive those patches at all instead of silently discarding them.
export function useCopilotRunActivity(sessionId: string | null, runId: string | null) {
  return useQuery({
    queryKey: ["run", runId ?? ""],
    queryFn: () => api.get<RunActivityDTO>(`/chat/sessions/${sessionId}/runs/${runId}`),
    enabled: !!sessionId && !!runId,
    // A terminal run's state does not change again; a poll interval would
    // just be wasted requests once the WS has already delivered every event
    // an open tab will ever see for this run.
    staleTime: Infinity,
  });
}
