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

/** The messages from the root down to `leafId`, or null when the leaf is not
 *  among them (a stale id: the chat is then shown linearly). */
function pathTo(messages: ChatMessageDTO[], leafId: string): ChatMessageDTO[] | null {
  const byId = new Map(messages.map((m) => [m.id, m]));
  const path: ChatMessageDTO[] = [];
  const seen = new Set<string>();
  let node = byId.get(leafId);
  if (!node) return null;
  while (node && !seen.has(node.id)) {
    seen.add(node.id);
    path.push(node);
    node = node.parentId ? byId.get(node.parentId) : undefined;
  }
  return path.reverse();
}

/** A chat with a message tree: only the branch up to the current leaf. Edits
 *  of a turn are the siblings of its user message; regenerated answers are the
 *  assistant messages sharing the path answer's parent. */
function buildPathTurns(
  messages: ChatMessageDTO[],
  path: ChatMessageDTO[],
  activeRuns: ActiveRunDTO[],
): TurnModel[] {
  const siblingsOf = (m: ChatMessageDTO) =>
    messages.filter((o) => (o.parentId ?? null) === (m.parentId ?? null) && o.role === m.role);
  const onPath = new Set(path.map((m) => m.id));
  const known = new Set(messages.map((m) => m.id));
  const liveByTurn = new Map<string, string>();
  const orphanRuns: ActiveRunDTO[] = [];
  for (const run of activeRuns) {
    if (run.replyToId && onPath.has(run.replyToId)) liveByTurn.set(run.replyToId, run.runId);
    // A run answering a turn on another branch is not part of this view.
    else if (!run.replyToId || !known.has(run.replyToId)) orphanRuns.push(run);
  }

  const turns: TurnModel[] = [];
  for (const m of path) {
    if (m.role === "assistant") {
      const variants = siblingsOf(m);
      const replyToId = m.replyToId ?? m.parentId ?? null;
      const liveRunId = replyToId ? (liveByTurn.get(replyToId) ?? null) : null;
      turns.push({
        kind: "assistant",
        key: replyToId ? `answer-${replyToId}` : m.id,
        replyToId,
        variants,
        selectedId: m.id,
        liveRunId,
      });
      continue;
    }
    const sibs = m.role === "user" ? siblingsOf(m) : [m];
    turns.push({
      kind: "user",
      key: m.id,
      message: m,
      siblings: { ids: sibs.map((s) => s.id), index: Math.max(0, sibs.indexOf(m)) },
    });
    const next = path[path.indexOf(m) + 1];
    const liveRunId = liveByTurn.get(m.id) ?? null;
    // Answer still being written: nothing of it is on the path yet.
    if (liveRunId && !(next && next.role === "assistant")) {
      turns.push({
        kind: "assistant",
        key: `answer-${m.id}`,
        replyToId: m.id,
        variants: messages.filter((o) => o.role === "assistant" && o.parentId === m.id),
        selectedId: null,
        liveRunId,
      });
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

export function buildTurns(
  messages: ChatMessageDTO[],
  activeRuns: ActiveRunDTO[],
  currentLeafId: string | null = null,
): TurnModel[] {
  const path = currentLeafId ? pathTo(messages, currentLeafId) : null;
  if (path) return buildPathTurns(messages, path, activeRuns);
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
  const currentLeafId = sessionQuery.data?.currentLeafId ?? null;
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
    return buildTurns(messages, runs, currentLeafId);
  }, [messages, liveKey, currentLeafId]);
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
