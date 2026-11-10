// The ways into an empty Copilot chat, the same on every surface that talks
// to the Copilot (its page, the dock, the My Work widget).
type Translate = (en: string, de?: string) => string;

export function copilotStarters(t: Translate): { title: string; subtitle: string }[] {
  return [
    {
      title: t("Keep an eye on something for me", "Behalte etwas für mich im Blick"),
      subtitle: t(
        "I check it and tell you when it matters",
        "Ich prüfe es und melde mich, wenn es wichtig wird",
      ),
    },
    {
      title: t("What are you working on for me?", "Woran arbeitest du gerade für mich?"),
      subtitle: t("Open responsibilities and follow-ups", "Offene Aufgaben und Nachfassungen"),
    },
    {
      title: t("What is waiting on me?", "Was wartet auf mich?"),
      subtitle: t("Approvals and questions", "Freigaben und Fragen"),
    },
  ];
}
