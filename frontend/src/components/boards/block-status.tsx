// What a block shows instead of its data: loading, or a neutral, translated
// reason. A refused query (`422 board_query:<code>`) never shows its detail.
import type { ReactNode } from "react";

import type { Translate } from "@/lib/tool-labels";

import { blockErrorText } from "./board-format";

export function BlockMessage({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-16 flex-1 items-center justify-center px-3 py-4 text-center text-xs text-muted-foreground">
      {children}
    </div>
  );
}

/** Loading or failed: the message to show; null once there is data. */
export function BlockStatus({ data, error, t }: { data: unknown; error: unknown; t: Translate }) {
  if (data) return null;
  return (
    <BlockMessage>{error ? blockErrorText(error, t) : t("Loading…", "Wird geladen…")}</BlockMessage>
  );
}
