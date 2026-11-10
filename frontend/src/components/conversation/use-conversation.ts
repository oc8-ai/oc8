// The data half of the one chat: the transcript merged with the runs still
// answering it. No polling -- see lib/live/apply-event.ts.
import { useCallback, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  conversationKeys,
  useChatSession,
  withActiveRun,
  type ActiveRunDTO,
  type ChatSessionDetailDTO,
} from "@/lib/conversation-api";
import { useChatMessages, type ChatMessageDTO } from "@/lib/hooks-chat";
import { useWatchRuns } from "@/lib/live/provider";

import type { AssistantTurnModel, TurnModel } from "./types";

export function buildTurns(messages: ChatMessageDTO[], activeRuns: ActiveRunDTO[]): TurnModel[] {
  const replies = new Map<string, ChatMessageDTO[]>();
  for (const m of messages) {
    if (m.role === "assistant" && m.replyToId) {
      replies.set(m.replyToId, [...(replies.get(m.replyToId) ?? []), m]);
    }
  }
  const liveByTurn = new Map<string, string>();
  const orphanRuns: ActiveRunDTO[] = [];
  const userIds = new Set(messages.filter((m) => m.role !== "assistant").map((m) => m.id));
  for (const run of activeRuns) {
    if (run.replyToId && userIds.has(run.replyToId)) liveByTurn.set(run.replyToId, run.runId);
    else orphanRuns.push(run);
  }

  const turns: TurnModel[] = [];
  for (const m of messages) {
    if (m.role === "assistant") {
      // No reply link (older rows), or the turn it answers is not in this
      // list: show it on its own where it sits rather than dropping it.
      if (!m.replyToId || !userIds.has(m.replyToId)) {
        turns.push({
          kind: "assistant",
          key: m.id,
          replyToId: null,
          variants: [m],
          liveRunId: null,
        });
      }
      continue;
    }
    turns.push({ kind: "user", key: m.id, message: m });
    const variants = replies.get(m.id) ?? [];
    const liveRunId = liveByTurn.get(m.id) ?? null;
    if (variants.length > 0 || liveRunId) {
      const answer: AssistantTurnModel = {
        kind: "assistant",
        key: `answer-${m.id}`,
        replyToId: m.id,
        variants,
        liveRunId,
      };
      turns.push(answer);
    }
  }
  for (const run of orphanRuns) {
    turns.push({
      kind: "assistant",
      key: `run-${run.runId}`,
      replyToId: run.replyToId,
      variants: [],
      liveRunId: run.runId,
    });
  }
  return turns;
}

export function useConversation(sessionId: string | null) {
  const qc = useQueryClient();
  const messagesQuery = useChatMessages(sessionId);
  const sessionQuery = useChatSession(sessionId);
  const messages = useMemo(() => messagesQuery.data ?? [], [messagesQuery.data]);
  const activeRuns = sessionQuery.data?.activeRuns ?? [];
  // A run counts as live until its answer has landed, even if the detail
  // query has not been refetched yet.
  const answeredRunIds = new Set(messages.map((m) => m.runId).filter(Boolean) as string[]);
  const live = activeRuns.filter((r) => !answeredRunIds.has(r.runId));
  // A string key keeps the memo stable across renders that rebuild `live`
  // with the same runs; the runs are re-parsed from it inside the memo.
  const liveKey = live.map((r) => `${r.runId}:${r.replyToId ?? ""}:${r.state}`).join(",");
  const turns = useMemo(() => {
    const runs: ActiveRunDTO[] = liveKey
      ? liveKey.split(",").map((entry) => {
          const [runId, replyToId, state] = entry.split(":");
          return { runId, replyToId: replyToId || null, state };
        })
      : [];
    return buildTurns(messages, runs);
  }, [messages, liveKey]);
  const liveRunIds = live.map((r) => r.runId);
  useWatchRuns(liveRunIds);

  const addPendingRun = useCallback(
    (run: ActiveRunDTO) => {
      if (!sessionId) return;
      qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(sessionId), (prev) =>
        withActiveRun(prev, run),
      );
    },
    [qc, sessionId],
  );

  return {
    session: sessionQuery.data,
    messages,
    turns,
    isLoading: messagesQuery.isLoading || sessionQuery.isLoading,
    liveRunIds,
    addPendingRun,
  };
}
