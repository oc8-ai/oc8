import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { COPILOT_AUTO_OPEN_KEY } from "@/components/copilot-dock";
import { useT } from "@/lib/i18n";
import { useCompleteOnboarding } from "@/lib/hooks";

export function DoneStep() {
  const t = useT();
  const complete = useCompleteOnboarding();
  const [entering, setEntering] = useState(false);

  const enter = async () => {
    setEntering(true);
    try {
      await complete.mutateAsync();
      // Read by CopilotDock on the next mount (this navigation reloads the
      // whole app, so a React state flag would not survive it) to greet the
      // operator the moment they land back in the office.
      window.sessionStorage.setItem(COPILOT_AUTO_OPEN_KEY, "1");
      window.location.href = "/";
    } catch (err) {
      toast.error(
        t("Could not finish onboarding", "Einrichtung konnte nicht abgeschlossen werden"),
        { description: err instanceof Error ? err.message : String(err) },
      );
      setEntering(false);
    }
  };

  return (
    <Panel className="space-y-4 p-8 text-center">
      <h2 className="font-serif text-xl">{t("Your office is open!", "Dein Büro ist eröffnet!")}</h2>
      <button
        onClick={enter}
        disabled={entering}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-brand-gradient-foreground transition hover:brightness-110 disabled:opacity-60"
      >
        {entering ? t("Entering…", "Wird betreten …") : t("Enter your office", "Büro betreten")}
      </button>
    </Panel>
  );
}
