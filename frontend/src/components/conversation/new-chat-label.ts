import { useT } from "@/lib/i18n";

/** What a chat without a title is called. A chat is named after its first
 *  message in the background (backend `oc8.chat.titles`); until that title
 *  arrives it reads as the new chat it is. */
export function useNewChatLabel(): string {
  const t = useT();
  return t("New chat", "Neuer Chat");
}
