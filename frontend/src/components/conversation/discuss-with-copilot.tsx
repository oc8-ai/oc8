// Spec §5.6: open a Copilot chat with an object pinned. Pinning is checked
// server-side with the member's own rights; an object they cannot open is
// refused there (422), not hidden here. Opening such a link only creates the
// chat and pins the object -- it never sends a message or starts a run.
import { MessageSquare } from "lucide-react";
import type { MouseEvent } from "react";

import { useMay } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";

import { isPlainClick } from "./object-links";
import { discussHref } from "./open-pinned-chat";
import { useObjectNavigation } from "./use-object-navigation";

/** An outline button (or small link) that opens a new Copilot chat with the
 *  object pinned. Not shown to members without `copilot:use`. `text` replaces
 *  the visible words ("Continue" on a finished run). */
export function DiscussWithCopilot({
  kind,
  id,
  label,
  variant = "button",
  text,
}: {
  kind: string;
  id: string;
  label: string;
  variant?: "button" | "link";
  text?: string;
}) {
  const t = useT();
  const may = useMay();
  const navigate = useObjectNavigation();
  if (!may("copilot:use")) return null;
  const href = discussHref(kind, id);
  const words = text ?? t("Discuss with Copilot", "Mit dem Copilot besprechen");
  return (
    <a
      href={href}
      aria-label={`${words}: ${label}`}
      onClick={(e: MouseEvent<HTMLAnchorElement>) => {
        if (!navigate || !isPlainClick(e)) return;
        e.preventDefault();
        navigate(href);
      }}
      className={
        variant === "button"
          ? "inline-flex h-9 items-center gap-2 rounded-md border border-border bg-transparent px-3 text-sm font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          : "inline-flex items-center gap-1 rounded-md text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      }
    >
      <MessageSquare className={variant === "button" ? "size-4" : "size-3.5"} aria-hidden />
      {words}
    </a>
  );
}
