// REST hooks for the one chat (components/conversation). Session detail,
// the model picker, regenerate and feedback; the transcript itself stays in
// hooks-chat.ts.
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";

import { api } from "@/lib/api";
import {
  chatKeys,
  type ChatMessageDTO,
  type ChatSessionDTO,
  type RunActivityDTO,
} from "@/lib/hooks-chat";
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

/** A run on the Live timeline (backend LiveRunDTO). For a colleague's run
 *  under "All runs" (`redacted`), `toolCalls`/`stepTimings` carry step
 *  labels only. */
export interface LiveRunDTO {
  id: string;
  state: string;
  source: string;
  createdAt: string;
  /** `kind` is the run's source; `label` the name beside it (a cron
   *  expression, "<source>/<type>", the delegating agent, the member). */
  trigger: { kind: string; label: string | null };
  output: string | null;
  steps: number;
  toolCalls: Array<Record<string, unknown>>;
  stepTimings: Array<Record<string, unknown>>;
  startedBy: string | null;
  /** A colleague's run: shown as labels only, never watched live. */
  redacted: boolean;
}

export interface LiveItemDTO {
  kind: "message" | "run";
  at: string;
  message: ChatMessageDTO | null;
  run: LiveRunDTO | null;
}

export interface LiveTimelineDTO {
  sessionId: string;
  items: LiveItemDTO[];
  /** Cursor for the next older page; null at the start of the timeline. */
  nextBefore: string | null;
  mayViewAllRuns: boolean;
}

export interface RunInputDTO {
  id: string;
  text: string;
  authorName: string;
  createdAt: string;
  consumedAt: string | null;
  consumedStep: number | null;
}

export interface RunSteeringDTO {
  /** The run is still running, so the reader may write to or stop it. */
  mayWrite: boolean;
  /** Its runtime reads messages at all; when not, only Stop is offered. */
  acceptsInputs: boolean;
  inputs: RunInputDTO[];
}

export const liveTimelineKey = (agentId: string, allRuns: boolean) =>
  ["chat", "live", agentId, allRuns] as const;

/** Every loaded page, oldest first, as one timeline: page 0 is the newest,
 *  each further page the one before it (`before` = the previous cursor). */
export function flattenLivePages(data: InfiniteData<LiveTimelineDTO>): LiveTimelineDTO {
  const newest = data.pages[0];
  const last = data.pages[data.pages.length - 1];
  return {
    sessionId: newest?.sessionId ?? "",
    mayViewAllRuns: newest?.mayViewAllRuns ?? false,
    nextBefore: last?.nextBefore ?? null,
    items: [...data.pages].reverse().flatMap((page) => page.items),
  };
}

const LIVE_TERMINAL = new Set(["done", "failed", "interrupted"]);
/** How often the timeline is refetched while a colleague's run is running. */
export const COLLEAGUE_POLL_MS = 10_000;

/** A colleague's run is never watched over the socket (the server would not
 *  send its steps to this reader), so while one is running the timeline is
 *  refetched every 10 s instead -- and not at all once none is. */
export function liveTimelineRefetchInterval(
  data: InfiniteData<LiveTimelineDTO> | undefined,
  isColleague: (run: LiveRunDTO) => boolean = (run) => run.redacted,
): number | false {
  const running = data?.pages.some((page) =>
    page.items.some((i) => i.run && !LIVE_TERMINAL.has(i.run.state) && isColleague(i.run)),
  );
  return running ? COLLEAGUE_POLL_MS : false;
}

/** The Live tab's timeline. `data` is every loaded page merged; `fetchOlder`
 *  loads the page before the oldest one (scrolling up). A colleague's run
 *  (`redacted`) is refetched every 10 s while it runs. */
export function useLiveTimeline(agentId: string, allRuns: boolean) {
  const query = useInfiniteQuery({
    queryKey: liveTimelineKey(agentId, allRuns),
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams();
      if (allRuns) params.set("allRuns", "true");
      if (pageParam) params.set("before", pageParam);
      const qs = params.toString();
      return api.get<LiveTimelineDTO>(`/agents/${agentId}/live${qs ? `?${qs}` : ""}`);
    },
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextBefore,
    select: flattenLivePages,
    enabled: !!agentId,
    refetchInterval: (query) => liveTimelineRefetchInterval(query.state.data),
  });
  return {
    ...query,
    hasOlder: query.hasNextPage,
    loadingOlder: query.isFetchingNextPage,
    fetchOlder: () => {
      if (query.hasNextPage && !query.isFetchingNextPage) void query.fetchNextPage();
    },
  };
}

export const runSteeringKey = (runId: string | null) => ["run-steering", runId ?? ""] as const;

export function useRunSteering(runId: string | null) {
  return useQuery({
    queryKey: runSteeringKey(runId),
    queryFn: () => api.get<RunSteeringDTO>(`/runs/${runId}/steering`),
    enabled: !!runId,
  });
}

/** Write to a running run. Refusals (409 finished / takes no messages, 422
 *  too long, 429 too many waiting) come back as ApiError -- word them with
 *  `runInputError` (components/conversation/run-steering.tsx). */
export function useSendRunInput(runId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (text: string) => api.post<RunInputDTO>(`/runs/${runId}/inputs`, { text }),
    onSuccess: (input) =>
      qc.setQueryData<RunSteeringDTO>(runSteeringKey(runId), (prev) =>
        prev && !prev.inputs.some((i) => i.id === input.id)
          ? { ...prev, inputs: [...prev.inputs, input] }
          : prev,
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: runSteeringKey(runId) }),
  });
}

/** Cooperative stop (202): the run ends at its next step boundary, and
 *  "run.status" reports it. 409 when it had already finished. */
export function useStopRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.post<void>(`/runs/${runId}/stop`),
    onSettled: (_data, _error, runId) => qc.invalidateQueries({ queryKey: runSteeringKey(runId) }),
  });
}
