// frontend/src/components/conversation/web-toggle.tsx
// The composer's "Web" switch (spec §10): this message may search the web.
// Shown only while the tenant has a search provider; the caller holds the
// choice (it also goes with a regenerate or an edit).
import { Globe } from "lucide-react";

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function WebToggle({
  on,
  onChange,
  disabled = false,
}: {
  on: boolean;
  onChange: (on: boolean) => void;
  disabled?: boolean;
}) {
  const t = useT();
  return (
    <button
      type="button"
      aria-pressed={on}
      disabled={disabled}
      onClick={() => onChange(!on)}
      title={t("Search the web for this message", "Für diese Nachricht im Web suchen")}
      className={cn(
        "inline-flex h-11 items-center gap-1 rounded-md px-2 text-xs sm:h-9 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60",
        on
          ? "bg-primary/15 text-primary"
          : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
    >
      <Globe className="size-3.5" aria-hidden />
      {t("Web", "Web")}
    </button>
  );
}
