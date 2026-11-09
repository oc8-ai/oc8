// frontend/src/components/conversation/context-icons.tsx
// One icon and one name per context kind, wherever a ref is shown: pinned
// chips, composer chips and the context search.
import {
  BookOpen,
  Bot,
  Building2,
  CheckSquare,
  Database,
  File,
  FileText,
  ListChecks,
  MessageSquare,
  Play,
  type LucideIcon,
} from "lucide-react";

export const KIND_ICONS: Record<string, LucideIcon> = {
  knowledge_base: BookOpen,
  data_source: Database,
  source_file: FileText,
  file: File,
  agent: Bot,
  department: Building2,
  run: Play,
  approval: CheckSquare,
  responsibility: ListChecks,
  chat_session: MessageSquare,
};

/** The icon for `kind`, a book for one this build does not know yet. */
export function kindIcon(kind: string): LucideIcon {
  return KIND_ICONS[kind] ?? BookOpen;
}

export function kindLabel(kind: string, t: (en: string, de?: string) => string): string {
  switch (kind) {
    case "knowledge_base":
      return t("Knowledge base", "Wissensdatenbank");
    case "data_source":
      return t("Data source", "Datenquelle");
    case "source_file":
      return t("File in a source", "Datei aus einer Quelle");
    case "file":
      return t("File", "Datei");
    case "agent":
      return t("Agent", "Agent");
    case "department":
      return t("Department", "Abteilung");
    case "run":
      return t("Agent run", "Lauf");
    case "approval":
      return t("Approval", "Freigabe");
    case "responsibility":
      return t("Responsibility", "Aufgabe");
    case "chat_session":
      return t("Chat", "Chat");
    default:
      return kind;
  }
}
