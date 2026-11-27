import { useT } from "@/lib/i18n";

/** What a chat without a title is called. A chat is named after its first
 *  message in the background (backend `oc8.chat.titles`); until that title
 *  arrives it reads as the new chat it is. */
export function useNewChatLabel(): string {
  const t = useT();
  return t("New chat", "Neuer Chat");
}

/** What a temporary chat (spec §5.6) is called where one is started, the
 *  chip it carries while open, and when it goes away. */
export function useTemporaryChatText(): { label: string; chip: string; help: string } {
  const t = useT();
  return {
    label: t("Temporary chat", "Temporärer Chat"),
    chip: t("Temporary — not saved", "Temporär – wird nicht gespeichert"),
    help: t(
      "Deleted when you leave it, at the latest after 24 hours.",
      "Wird gelöscht, wenn du ihn verlässt, spätestens nach 24 Stunden.",
    ),
  };
}
