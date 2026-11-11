// REST hooks for the one chat (components/conversation). Session detail,
// the model picker, regenerate and feedback; the transcript itself stays in
// hooks-chat.ts.
import { useEffect, useState } from "react";
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";

import { api, ApiError, retryUnlessRefused } from "@/lib/api";
import { isUuid } from "@/lib/ids";
import {
  chatKeys,
  writeSessionToCache,
  type ChatMessageDTO,
  type ChatSessionDTO,
  type RunActivityDTO,
} from "@/lib/hooks-chat";
import type { RunComponentDTO } from "@/lib/hooks";
import { runLiveAnswerKey, runLiveStateKey, type RunLiveState } from "@/lib/live/apply-event";
import type { ReasoningLevel } from "@/lib/reasoning-levels";

export interface ActiveRunDTO {
  runId: string;
  replyToId: string | null;
  state: string;
}

export interface ChatSessionDetailDTO extends ChatSessionDTO {
  /** The sticky mode this chat is in ("agent"), or null. */
  mode: string | null;
  modelOverrideId: string | null;
  /** The reasoning level kept on this chat, or null for the default.
   *  Absent on an older backend. */
  effort?: ReasoningLevel | null;
  kind: "chat" | "live";
  activeRuns: ActiveRunDTO[];
  readOnly: boolean;
  /** What the chat's run is parked on (spec §4.1), or null. */
  pendingQuestion: PendingQuestionDTO | null;
  taskId: string | null;
  /** The chat's pinned context, re-checked for the caller. */
  pinnedRefs: ContextRefViewDTO[];
  projectId: string | null;
  pinned: boolean;
}

/** A pin as the caller sees it now. `available` is false once they can no
 *  longer see it; `label` is then the one stored when it was pinned. The
 *  label is the server's: render it as plain text only. */
export interface ContextRefViewDTO {
  kind: string;
  id: string;
  label: string;
  available: boolean;
}

export interface ChatModelOptionDTO {
  id: string | null;
  label: string;
  provider: string;
  locality: string;
  isDefault: boolean;
  /** Whether this model can think; for "Auto", the agent's default model's. */
  supportsReasoning?: boolean;
  /** The level that applies without a chat pick, or "custom". */
  defaultEffort?: ReasoningLevel | "custom";
}

export const conversationKeys = {
  session: (sessionId: string) => ["chat", "session", sessionId] as const,
  models: (sessionId: string) => ["chat", "models", sessionId] as const,
};

export function useChatSession(sessionId: string | null) {
  return useQuery({
    queryKey: conversationKeys.session(sessionId ?? ""),
    queryFn: () =>
      api.get<ChatSessionDetailDTO>(`/chat/sessions/${encodeURIComponent(sessionId ?? "")}`),
    enabled: !!sessionId,
    // A chat that is gone (404) or not the reader's (403) stays so.
    retry: retryUnlessRefused,
  });
}

/** The model picker's options: the chat's own, or, for a chat that does not
 *  exist yet, its agent's (Ruling 57). The same list either way; listing a
 *  new chat's options never creates it. */
export function useSessionModels(sessionId: string | null, enabled: boolean, agentId?: string) {
  const path = sessionId
    ? `/chat/sessions/${encodeURIComponent(sessionId)}/models`
    : agentId
      ? `/chat/agents/${encodeURIComponent(agentId)}/models`
      : null;
  return useQuery({
    queryKey: sessionId
      ? conversationKeys.models(sessionId)
      : ["chat", "agent-models", agentId ?? ""],
    queryFn: () => api.get<ChatModelOptionDTO[]>(path ?? ""),
    enabled: !!path && enabled,
    staleTime: 60_000,
  });
}

export function useSetSessionModel(sessionId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (modelConfigId: string | null) =>
      api.put<ChatSessionDetailDTO>(`/chat/sessions/${encodeURIComponent(sessionId ?? "")}/model`, {
        modelConfigId,
      }),
    onSuccess: (session) =>
      qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(session.id), (prev) =>
        prev ? { ...prev, modelOverrideId: session.modelOverrideId } : session,
      ),
  });
}

export function useSetSessionEffort(sessionId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (effort: ReasoningLevel | null) =>
      api.put<ChatSessionDetailDTO>(
        `/chat/sessions/${encodeURIComponent(sessionId ?? "")}/effort`,
        { effort },
      ),
    onSuccess: (session) =>
      qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(session.id), (prev) =>
        prev ? { ...prev, effort: session.effort ?? null } : session,
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
      api.post<ActiveRunDTO>(
        `/chat/sessions/${encodeURIComponent(sessionId ?? "")}/messages/${encodeURIComponent(messageId)}/regenerate`,
      ),
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
      api.post<void>(
        `/chat/sessions/${encodeURIComponent(sessionId ?? "")}/messages/${encodeURIComponent(messageId)}/feedback`,
        {
          rating,
          note: note ?? "",
        },
      ),
  });
}

/** One turn's run: the same ["run", runId] entry the live patchers write to.
 *  A finished run never changes again, so it is fetched once. */
/** A fetched turn run with what only the live events know laid over it:
 *  the answer's fragments (the GET never carries them, and some may have
 *  landed before the run was cached) and the state the last run.status
 *  announced (an in-process run commits RUNNING only when it ends). A run
 *  that has ended is taken as fetched: its persisted output is complete. */
export function mergeTurnRun(
  fresh: RunActivityDTO,
  previous: RunActivityDTO | undefined,
  buffered: string | undefined,
  live: RunLiveState | undefined,
): RunActivityDTO {
  if (TURN_TERMINAL.has(fresh.state)) return fresh;
  let merged = fresh;
  const liveAnswer = previous?.liveAnswer || buffered;
  if (liveAnswer) merged = { ...merged, liveAnswer };
  if (previous?.liveReasoning) merged = { ...merged, liveReasoning: previous.liveReasoning };
  if (live && live.state !== fresh.state) {
    merged = { ...merged, state: live.state, phase: live.phase ?? fresh.phase };
  }
  return merged;
}

const TURN_TERMINAL = new Set(["done", "failed", "interrupted"]);

export function useTurnRun(sessionId: string | null, runId: string | null) {
  return useQuery({
    queryKey: ["run", runId ?? ""],
    queryFn: async ({ client }) => {
      const fresh = await api.get<RunActivityDTO>(
        `/chat/sessions/${encodeURIComponent(sessionId ?? "")}/runs/${encodeURIComponent(runId ?? "")}`,
      );
      // Read after the fetch, so fragments that arrived while it was in
      // flight are kept.
      const id = runId ?? "";
      return mergeTurnRun(
        fresh,
        client.getQueryData<RunActivityDTO>(["run", id]),
        client.getQueryData<string>(runLiveAnswerKey(id)),
        client.getQueryData<RunLiveState>(runLiveStateKey(id)),
      );
    },
    enabled: !!sessionId && !!runId,
    staleTime: Infinity,
  });
}

export { chatKeys };

export interface QuestionOptionDTO {
  key: string;
  label: string;
  detail: string;
}

/** One structured `ask_user` question (spec §4.1). */
export interface QuestionDTO {
  id: string;
  prompt: string;
  kind: "single" | "multi" | "text";
  options: QuestionOptionDTO[];
  allowOther: boolean;
  required: boolean;
}

/** `questions` is empty for a plain question: `text` is then all there is. */
export interface PendingQuestionDTO {
  clarificationId: string;
  runId: string;
  text: string;
  questions: QuestionDTO[];
}

/** Per question id: the option keys picked, or a free-text answer. */
export type QuestionAnswers = Record<string, string[] | string>;

/** Answer the question the chat's run is parked on. The answered turn and the
 *  cleared `pendingQuestion` also arrive live (chat.message_created); the
 *  refetch is the backstop. 409 (already answered) and 404 (gone) refetch too,
 *  so the card goes away. Refusals come back as ApiError. */
export function useAnswerQuestion(sessionId: string | null) {
  const qc = useQueryClient();
  const refetch = () => {
    if (!sessionId) return;
    void qc.invalidateQueries({ queryKey: conversationKeys.session(sessionId) });
    void qc.invalidateQueries({ queryKey: chatKeys.messages(sessionId) });
  };
  return useMutation({
    mutationFn: ({
      clarificationId,
      answers,
      text,
    }: {
      clarificationId: string;
      answers?: QuestionAnswers;
      text?: string;
    }) =>
      api.post<{ runId: string }>(
        `/chat/sessions/${encodeURIComponent(sessionId ?? "")}/questions/${encodeURIComponent(clarificationId)}/answer`,
        answers !== undefined ? { answers } : { text },
      ),
    onSuccess: refetch,
    onError: (error) => {
      if (error instanceof ApiError && (error.status === 409 || error.status === 404)) refetch();
    },
  });
}

export interface ContextItem {
  kind: string;
  id: string;
  label: string;
  hint?: string;
  /** False for a data source that is not connected ("Connect"). */
  connected?: boolean;
}

/** One `GET /chat/context/search` hit. */
export interface ContextSearchItemDTO {
  kind: string;
  id: string;
  label: string;
  hint: string | null;
  connected: boolean;
}

/** One entry of a data source folder. A file's `id` is the file part of a
 *  `source_file` ref (`<source>:<id>`); a folder's is the `path` to browse. */
export interface BrowseEntryDTO {
  id: string;
  name: string;
  isFolder: boolean;
  size: number | null;
  contentType: string | null;
}

export interface BrowseListingDTO {
  entries: BrowseEntryDTO[];
  /** The folder holds more than is listed. */
  truncated: boolean;
}

/** The backend's bounds on one search: query length and kinds per call. */
const MAX_SEARCH_QUERY = 100;
const MAX_SEARCH_KINDS = 16;
const SEARCH_DEBOUNCE_MS = 200;
/** At most this many pins per chat (backend MAX_PINNED_REFS). */
export const MAX_PINS = 20;

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

/** What `#` and the + menu offer: every kind the member may see (backend
 *  GET /chat/context/search, authorised per kind with the member's rights),
 *  or only `kinds`. `null` searches nothing. The query is debounced and the
 *  previous hits stay while the next ones load. An empty query sends no
 *  request unless `recents` asks for the server's first hits per kind. */
export function useContextSearchResults(
  query: string | null,
  kinds?: string[],
  options: { recents?: boolean } = {},
): { items: ContextItem[]; isLoading: boolean } {
  const debounced = useDebouncedValue(query, SEARCH_DEBOUNCE_MS);
  // Closing never waits for the debounce, and opening starts from what is typed.
  const q = query === null ? null : (debounced ?? query).trim().slice(0, MAX_SEARCH_QUERY);
  const wanted = (kinds ?? []).slice(0, MAX_SEARCH_KINDS).join(",");
  const enabled = q !== null && (q !== "" || !!options.recents);
  const { data, isPending } = useQuery({
    queryKey: ["chat", "context-search", q ?? "", wanted] as const,
    queryFn: () => {
      const params = new URLSearchParams({ q: q ?? "" });
      if (wanted) params.set("kinds", wanted);
      return api.get<ContextSearchItemDTO[]>(`/chat/context/search?${params.toString()}`);
    },
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 10_000,
  });
  if (!enabled) return { items: [], isLoading: false };
  return {
    items: (data ?? []).map((i) => ({
      kind: i.kind,
      id: i.id,
      label: i.label,
      hint: i.hint ?? undefined,
      connected: i.connected,
    })),
    // Only the first answer is waited for: later ones keep the previous hits.
    isLoading: isPending && data === undefined,
  };
}

/** `useContextSearchResults` without the loading state (the composer's `#`). */
export function useContextSearch(
  query: string | null,
  kinds?: string[],
  options: { recents?: boolean } = {},
): ContextItem[] {
  return useContextSearchResults(query, kinds, options).items;
}

export interface PinRef {
  kind: string;
  id: string;
}

/** One change to a chat's pins. */
export type PinChange = { add: PinRef } | { remove: PinRef };

const sameRef = (a: PinRef, b: PinRef) => a.kind === b.kind && a.id === b.id;

/** Change the chat's pins (owner only, at most MAX_PINS). Each PUT is built
 *  from the session as cached when it runs, and writes run one at a time
 *  (one scope per chat), so quick changes never overwrite each other. A
 *  session refetch still in flight (a live event started it) holds the pins
 *  from before the write: it is cancelled when a write starts and when it
 *  lands, so it never puts them back, and the next write is built from the
 *  server's reply. A 422 names the refused refs: word it with `pinErrorText`. */
export function useSetPins(sessionId: string | null) {
  const qc = useQueryClient();
  const key = conversationKeys.session(sessionId ?? "");
  return useMutation({
    mutationKey: ["chat", "pins", sessionId ?? ""] as const,
    scope: { id: `chat-pins:${sessionId ?? ""}` },
    mutationFn: async (change: PinChange) => {
      await qc.cancelQueries({ queryKey: key, exact: true });
      const current = (
        qc.getQueryData<ChatSessionDetailDTO>(conversationKeys.session(sessionId ?? ""))
          ?.pinnedRefs ?? []
      ).map(({ kind, id }) => ({ kind, id }));
      const refs =
        "add" in change
          ? current.some((r) => sameRef(r, change.add))
            ? current
            : [...current, { kind: change.add.kind, id: change.add.id }]
          : current.filter((r) => !sameRef(r, change.remove));
      return api.put<ChatSessionDetailDTO>(
        `/chat/sessions/${encodeURIComponent(sessionId ?? "")}/pins`,
        { refs },
      );
    },
    onSuccess: async (session) => {
      const landed = conversationKeys.session(session.id);
      // A refetch that started while the PUT ran may have read the old pins.
      await qc.cancelQueries({ queryKey: landed, exact: true });
      qc.setQueryData(landed, session);
      // Anything else that changed meanwhile (runs, a question) comes back.
      void qc.invalidateQueries({ queryKey: landed, exact: true });
    },
  });
}

/** The inline sentence for a failed pin write: a 422 names the refused refs
 *  by the labels in `known`, anything else asks to try again. */
export function pinErrorText(
  error: unknown,
  known: { kind: string; id: string; label: string }[],
  t: (en: string, de?: string) => string,
): string | null {
  if (!error) return null;
  const labels = refusedRefs(error).map((r) => known.find((k) => sameRef(k, r))?.label ?? r.id);
  return labels.length > 0
    ? t(
        `Not available to you: ${labels.join(", ")}. Remove it, then try again.`,
        `Für dich nicht verfügbar: ${labels.join(", ")}. Entfern es und versuch es noch einmal.`,
      )
    : t(
        "Your pins were not saved. Try again.",
        "Deine angehefteten Einträge wurden nicht gespeichert. Versuch es noch einmal.",
      );
}

/** The refs a 422 from `PUT /pins` refused, by the kind and id sent. */
export function refusedRefs(error: unknown): { kind: string; id: string }[] {
  if (!(error instanceof ApiError) || error.status !== 422) return [];
  try {
    const detail = JSON.parse(error.message) as { refused?: unknown };
    return Array.isArray(detail.refused)
      ? detail.refused.filter((r: unknown): r is { kind: string; id: string } => {
          const ref = r as { kind?: unknown; id?: unknown } | null;
          return !!ref && typeof ref.kind === "string" && typeof ref.id === "string";
        })
      : [];
  } catch {
    return [];
  }
}

/** One folder of a data source (`knowledge:view`). Errors come back as
 *  ApiError: 409 the source can't be browsed, 404 gone or not allowed, 422 a
 *  bad path, 502 the connector failed (its message is already scrubbed). */
export function useBrowseSource(sourceId: string | null, path: string) {
  return useQuery({
    queryKey: ["chat", "browse", sourceId ?? "", path] as const,
    queryFn: () =>
      api.get<BrowseListingDTO>(
        `/chat/data-sources/${encodeURIComponent(sourceId ?? "")}/browse?path=${encodeURIComponent(path)}`,
      ),
    enabled: isUuid(sourceId),
    retry: false,
  });
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
  trigger: {
    kind: string;
    label: string | null;
    /** A schedule's zone (IANA); its cron is read there. */
    timezone?: string | null;
  };
  output: string | null;
  steps: number;
  toolCalls: Array<Record<string, unknown>>;
  stepTimings: Array<Record<string, unknown>>;
  startedBy: string | null;
  /** A colleague's run: shown as labels only, never watched live. */
  redacted: boolean;
  /** The cards it rendered; always empty for a `redacted` run. */
  renderedComponents: RunComponentDTO[];
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
      return api.get<LiveTimelineDTO>(
        `/agents/${encodeURIComponent(agentId)}/live${qs ? `?${qs}` : ""}`,
      );
    },
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextBefore,
    select: flattenLivePages,
    // Switching All runs keeps the conversation (and its draft) on screen
    // until the other timeline has loaded.
    placeholderData: keepPreviousData,
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

/** What the reader may do with a running run, and what they wrote to it.
 *  `runId` null asks nothing; `cachedId` then still reads what an earlier
 *  fetch left for that run. A refusal (403/404: not the reader's to steer)
 *  is final, never retried. */
export function useRunSteering(runId: string | null, cachedId: string | null = runId) {
  return useQuery({
    queryKey: runSteeringKey(runId ?? cachedId),
    queryFn: () => api.get<RunSteeringDTO>(`/runs/${encodeURIComponent(runId ?? "")}/steering`),
    enabled: !!runId,
    retry: retryUnlessRefused,
  });
}

/** Write to a running run. Refusals (409 finished / takes no messages, 422
 *  too long, 429 too many waiting) come back as ApiError -- word them with
 *  `runInputError` (components/conversation/run-steering.tsx). */
export function useSendRunInput(runId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (text: string) =>
      api.post<RunInputDTO>(`/runs/${encodeURIComponent(runId ?? "")}/inputs`, { text }),
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
    mutationFn: (runId: string) => api.post<void>(`/runs/${encodeURIComponent(runId)}/stop`),
    onSettled: (_data, _error, runId) => qc.invalidateQueries({ queryKey: runSteeringKey(runId) }),
  });
}

// ---- Projects and pinned chats (the Copilot sidebar) -----------------------

export interface ChatProjectDTO {
  id: string;
  name: string;
  instructions: string;
  pinnedRefs: ContextRefViewDTO[];
  createdAt: string;
}

export const projectKeys = { all: ["chat", "projects"] as const };

export function useChatProjects() {
  return useQuery({
    queryKey: projectKeys.all,
    queryFn: () => api.get<ChatProjectDTO[]>("/chat/projects"),
  });
}

/** Create a project, or change one: on a change `instructions` and
 *  `pinnedRefs` are sent only when given (PATCH is partial; the name is
 *  always sent). The reply is written into the cache, not refetched. */
export function useSaveProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      name,
      instructions,
      pinnedRefs,
    }: {
      id?: string;
      name: string;
      instructions?: string;
      pinnedRefs?: { kind: string; id: string }[];
    }) =>
      id
        ? api.patch<ChatProjectDTO>(`/chat/projects/${encodeURIComponent(id)}`, {
            name,
            ...(instructions !== undefined ? { instructions } : {}),
            ...(pinnedRefs !== undefined ? { pinnedRefs } : {}),
          })
        : api.post<ChatProjectDTO>("/chat/projects", {
            name,
            instructions: instructions ?? "",
            pinnedRefs: pinnedRefs ?? [],
          }),
    onSuccess: (project) =>
      qc.setQueryData<ChatProjectDTO[]>(projectKeys.all, (prev = []) =>
        prev.some((p) => p.id === project.id)
          ? prev.map((p) => (p.id === project.id ? project : p))
          : [...prev, project],
      ),
  });
}

/** Delete a project. Its chats stay and are detached: the cached lists show
 *  them in their date groups at once. */
export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/chat/projects/${encodeURIComponent(id)}`),
    onSuccess: (_void, id) => {
      qc.setQueryData<ChatProjectDTO[]>(projectKeys.all, (prev) =>
        prev?.filter((p) => p.id !== id),
      );
      qc.setQueriesData<ChatSessionDTO[]>({ queryKey: ["chat", "sessions"] }, (prev) =>
        prev?.map((s) => (s.projectId === id ? { ...s, projectId: null } : s)),
      );
      qc.setQueriesData<ChatSessionDetailDTO>({ queryKey: ["chat", "session"] }, (prev) =>
        prev?.projectId === id ? { ...prev, projectId: null } : prev,
      );
    },
  });
}

export function useSetSessionPinned() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ sessionId, pinned }: { sessionId: string; pinned: boolean }) =>
      api.put<ChatSessionDetailDTO>(
        `/chat/sessions/${encodeURIComponent(sessionId ?? "")}/pinned`,
        { pinned },
      ),
    onSuccess: (session) => writeSessionToCache(qc, session),
  });
}

export function useSetSessionProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ sessionId, projectId }: { sessionId: string; projectId: string | null }) =>
      api.put<ChatSessionDetailDTO>(
        `/chat/sessions/${encodeURIComponent(sessionId ?? "")}/project`,
        { projectId },
      ),
    onSuccess: (session) => writeSessionToCache(qc, session),
  });
}

/** Keep a sticky mode on this chat, or leave it (null). Owner-only server-side. */
export function useSetChatMode(sessionId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (mode: string | null) =>
      api.put<ChatSessionDetailDTO>(`/chat/sessions/${encodeURIComponent(sessionId ?? "")}/mode`, {
        mode,
      }),
    onSuccess: (session) => {
      if (!sessionId) return;
      qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(sessionId), (prev) =>
        prev ? { ...prev, mode: session.mode } : prev,
      );
    },
  });
}
