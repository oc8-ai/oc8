// An artifact in an answer: a card naming it, which opens it beside the chat.
// While its block is still being written the card says so, and if it is the
// one open, the panel follows the new text.

import { useEffect, useId } from "react";
import { ChevronRight, Code, Globe, Image as ImageIcon, Network } from "lucide-react";

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { useArtifacts } from "./context";
import { kindLabel, type Artifact, type ArtifactKind } from "./extract";

const ICONS: Record<ArtifactKind, typeof Code> = {
  html: Globe,
  svg: ImageIcon,
  mermaid: Network,
  code: Code,
};

export function ArtifactCard({ artifact }: { artifact: Artifact }) {
  const t = useT();
  const artifacts = useArtifacts();
  const isOpen = artifacts?.openId === artifact.id;
  const id = useId();
  const Icon = ICONS[artifact.kind];
  const lineCount = artifact.source ? artifact.source.split("\n").length : 0;

  const sync = artifacts?.sync;
  useEffect(() => {
    if (isOpen && sync) sync(artifact);
    // The artifact is a new object on every render; what it says is not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, sync, artifact.id, artifact.source, artifact.closed, artifact.title]);

  return (
    <button
      type="button"
      onClick={() => artifacts?.open(artifact)}
      aria-pressed={isOpen}
      // Named by what it shows (the title), its state as the description.
      aria-labelledby={`${id}-open ${id}-title`}
      aria-describedby={`${id}-state`}
      data-testid="artifact-card"
      className={cn(
        "flex w-full min-w-0 items-center gap-3 rounded-lg border border-border bg-card px-3 py-2 text-left hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        isOpen && "border-primary",
      )}
    >
      <span className="grid size-9 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground">
        <Icon className="size-4" aria-hidden />
      </span>
      <span className="min-w-0 flex-1">
        <span id={`${id}-open`} className="sr-only">
          {t("Open", "Öffnen")}
        </span>
        <span id={`${id}-title`} className="block truncate text-sm font-medium text-foreground">
          {artifact.title}
        </span>
        <span id={`${id}-state`} className="block truncate text-xs text-muted-foreground">
          {artifact.closed ? (
            <>
              {kindLabel(artifact)} · {t(`${lineCount} lines`, `${lineCount} Zeilen`)}
            </>
          ) : (
            <span className="text-shimmer">{t("writing…", "wird geschrieben …")}</span>
          )}
        </span>
      </span>
      <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
    </button>
  );
}
