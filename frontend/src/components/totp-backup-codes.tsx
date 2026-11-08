// frontend/src/components/totp-backup-codes.tsx
// The one-time backup-code reveal, shared by <TotpEnroll> (Task 11, first
// enrollment) and the profile page's "regenerate backup codes" action (Task
// 14) -- extracted here so both render the SAME markup rather than the
// profile page growing its own copy of a UI that already exists.
import { useT } from "@/lib/i18n";

export function BackupCodesReveal({
  codes,
  onAcknowledge,
}: {
  codes: string[];
  onAcknowledge: () => void;
}) {
  const t = useT();
  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold">
        {t("Save your backup codes", "Speichere deine Backup-Codes")}
      </h3>
      <p className="text-sm text-muted-foreground">
        {t(
          "Each code works once. Store them somewhere safe — you will not see them again.",
          "Jeder Code funktioniert einmal. Bewahre sie sicher auf — du siehst sie nicht erneut.",
        )}
      </p>
      <ul className="grid grid-cols-2 gap-2 rounded-md border border-border bg-background/40 p-3 font-mono text-sm">
        {codes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <button
        type="button"
        onClick={onAcknowledge}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-brand-gradient-foreground hover:brightness-110"
      >
        {t("I've saved these", "Ich habe sie gespeichert")}
      </button>
    </div>
  );
}
