// REST hooks for the one chat (components/conversation). Session detail,
// the model picker, regenerate and feedback; the transcript itself stays in
// hooks-chat.ts.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { chatKeys, type ChatSessionDTO, type RunActivityDTO } from "@/lib/hooks-chat";
import { useKnowledgeBases } from "@/lib/hooks";

export interface ActiveRunDTO {
  runId: string;
  replyToId: string | null;
  state: string;
}

export interface ChatSessionDetailDTO extends ChatSessionDTO {
  modelOverrideId: string | null;
  kind: "chat" | "live";
  activeRuns: ActiveRunDTO[];
  readOnly: boolean;
}

export interface ChatModelOptionDTO {
  id: string | null;
  label: string;
  provider: string;
  locality: string;
  isDefault: boolean;
}

export const conversationKeys = {
  session: (sessionId: string) => ["chat", "session", sessionId] as const,
  models: (sessionId: string) => ["chat", "models", sessionId] as const,
};

export function useChatSession(sessionId: string | null) {
  return useQuery({
    queryKey: conversationKeys.session(sessionId ?? ""),
    queryFn: () => api.get<ChatSessionDetailDTO>(`/chat/sessions/${sessionId}`),
    enabled: !!sessionId,
  });
}

export function useSessionModels(sessionId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: conversationKeys.models(sessionId ?? ""),
    queryFn: () => api.get<ChatModelOptionDTO[]>(`/chat/sessions/${sessionId}/models`),
    enabled: !!sessionId && enabled,
    staleTime: 60_000,
  });
}

export function useSetSessionModel(sessionId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (modelConfigId: string | null) =>
      api.put<ChatSessionDetailDTO>(`/chat/sessions/${sessionId}/model`, { modelConfigId }),
    onSuccess: (session) =>
      qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(session.id), (prev) =>
        prev ? { ...prev, modelOverrideId: session.modelOverrideId } : session,
      ),
  });
}

/** `prev` with `run` among its active runs, never twice: the regenerate
 *  response and the conversation's own pending-run bookkeeping can both
 *  report the same run. */
export function withActiveRun(
  prev: ChatSessionDetailDTO | undefined,
  run: ActiveRunDTO,
): ChatSessionDetailDTO | undefined {
  if (!prev || prev.activeRuns.some((r) => r.runId === run.runId)) return prev;
  return { ...prev, activeRuns: [...prev.activeRuns, run] };
}

export function useRegenerate(sessionId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (messageId: string) =>
      api.post<ActiveRunDTO>(`/chat/sessions/${sessionId}/messages/${messageId}/regenerate`),
    onSuccess: (run) => {
      if (!sessionId) return;
      qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(sessionId), (prev) =>
        withActiveRun(prev, run),
      );
    },
  });
}

export function useMessageFeedback(sessionId: string | null) {
  return useMutation({
    mutationFn: ({
      messageId,
      rating,
      note,
    }: {
      messageId: string;
      rating: "up" | "down";
      note?: string;
    }) =>
      api.post<void>(`/chat/sessions/${sessionId}/messages/${messageId}/feedback`, {
        rating,
        note: note ?? "",
      }),
  });
}

/** One turn's run: the same ["run", runId] entry the live patchers write to.
 *  A finished run never changes again, so it is fetched once. */
export function useTurnRun(sessionId: string | null, runId: string | null) {
  return useQuery({
    queryKey: ["run", runId ?? ""],
    queryFn: () => api.get<RunActivityDTO>(`/chat/sessions/${sessionId}/runs/${runId}`),
    enabled: !!sessionId && !!runId,
    staleTime: Infinity,
  });
}

export { chatKeys };

export interface ContextItem {
  kind: string;
  id: string;
  label: string;
  hint?: string;
}

/** What `#` offers while the reader types. C1: knowledge bases. Task 36
 *  replaces the body with GET /chat/context/search, which covers every kind. */
export function useContextSearch(query: string | null): ContextItem[] {
  const { data } = useKnowledgeBases({ search: query ?? undefined, pageSize: 8 });
  if (query === null) return [];
  return (data?.items ?? []).map((kb) => ({
    kind: "knowledge_base",
    id: kb.id,
    label: kb.name,
    hint: kb.description || undefined,
  }));
}
