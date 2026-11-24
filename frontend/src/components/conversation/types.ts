import type { ChatMessageDTO } from "@/lib/hooks-chat";

export type ConversationVariant = "full" | "compact";

export interface UserTurnModel {
  kind: "user";
  key: string;
  message: ChatMessageDTO;
  /** The turns sharing this one's parent (edits of the same turn), oldest
   *  first, and this turn's place among them. Absent in a linear chat. */
  siblings?: { ids: string[]; index: number };
}

export interface AssistantTurnModel {
  kind: "assistant";
  key: string;
  /** The user turn this answers; null for an answer older than reply links. */
  replyToId: string | null;
  /** Every finished answer to that turn, oldest first ("‹ 1/2 ›"). */
  variants: ChatMessageDTO[];
  /** In a message tree: the variant on the visible path. Absent in a linear
   *  chat, where the newest variant is shown. */
  selectedId?: string | null;
  /** A run still answering this turn, shown after the finished variants. */
  liveRunId: string | null;
  /** A message just sent, before its run is known: the turn waits for the
   *  answer (the breathing mark) without a run to read. */
  pending?: boolean;
}

export type TurnModel = UserTurnModel | AssistantTurnModel;
