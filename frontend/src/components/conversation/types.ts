import type { ChatMessageDTO } from "@/lib/hooks-chat";

export type ConversationVariant = "full" | "compact";

export interface UserTurnModel {
  kind: "user";
  key: string;
  message: ChatMessageDTO;
}

export interface AssistantTurnModel {
  kind: "assistant";
  key: string;
  /** The user turn this answers; null for an answer older than reply links. */
  replyToId: string | null;
  /** Every finished answer to that turn, oldest first ("‹ 1/2 ›"). */
  variants: ChatMessageDTO[];
  /** A run still answering this turn, shown after the finished variants. */
  liveRunId: string | null;
}

export type TurnModel = UserTurnModel | AssistantTurnModel;
