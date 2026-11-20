// frontend/src/components/second-factor.tsx
// What the login challenge, the login enrolment step and the profile page
// share about second factors other than the authenticator app: the method
// picker, the 60-second resend cooldown, and enrolling the code by email.
import { useState } from "react";
import { toast } from "sonner";
import { BackupCodesReveal } from "@/components/totp-backup-codes";
import { TotpEnroll } from "@/components/totp-enroll";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";
import { confirmEmailCode, enrollEmailCode, type TwoFactorMethod } from "@/lib/totp";
import { methodLabel } from "@/lib/two-factor";
import { useResendCooldown } from "@/hooks/use-resend-cooldown";

/** One button per method; renders nothing for a single method. */
export function MethodChoice({
  methods,
  value,
  onChange,
}: {
  methods: TwoFactorMethod[];
  value: TwoFactorMethod;
  onChange: (method: TwoFactorMethod) => void;
}) {
  const t = useT();
  if (methods.length < 2) return null;
  return (
    <div className="flex gap-2" role="group" aria-label={t("Method", "Methode")}>
      {methods.map((m) => (
        <button
          key={m}
          type="button"
          aria-pressed={m === value}
          onClick={() => onChange(m)}
          className={cn(
            "flex-1 rounded-md border px-3 py-1.5 text-xs font-medium transition",
            m === value
              ? "border-primary bg-primary/15 text-foreground"
              : "border-border text-muted-foreground hover:text-foreground",
          )}
        >
          {methodLabel(m, t)}
        </button>
      ))}
    </div>
  );
}

/** The "Send again" button, disabled while the cooldown runs. */
export function ResendButton({
  remaining,
  busy,
  onClick,
}: {
  remaining: number;
  busy: boolean;
  onClick: () => void;
}) {
  const t = useT();
  return (
    <button
      type="button"
      disabled={busy || remaining > 0}
      onClick={onClick}
      className="text-xs text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground disabled:no-underline disabled:opacity-60"
    >
      {remaining > 0
        ? t(`Send again in ${remaining} s`, `Erneut senden in ${remaining} s`)
        : t("Send again", "Erneut senden")}
    </button>
  );
}

type EmailEnrollStage = "start" | "code" | "codes";

/** Enrol the code by email: send a code to the sign-in address, confirm it,
 *  then show backup codes if this is the member's first method. */
export function EmailEnroll({ token, onDone }: { token: string; onDone: () => void }) {
  const t = useT();
  const [stage, setStage] = useState<EmailEnrollStage>("start");
  const [sentTo, setSentTo] = useState("");
  const [code, setCode] = useState("");
  const [backupCodes, setBackupCodes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const cooldown = useResendCooldown();

  const sendCode = async () => {
    setBusy(true);
    try {
      const result = await enrollEmailCode(token);
      setSentTo(result.sentTo);
      setStage("code");
      cooldown.start();
    } catch (e: unknown) {
      toast.error(
        e instanceof Error
          ? e.message
          : t("Could not send the code.", "Code konnte nicht gesendet werden."),
      );
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    try {
      const result = await confirmEmailCode(token, code);
      if (result.backupCodes.length > 0) {
        setBackupCodes(result.backupCodes);
        setStage("codes");
      } else {
        onDone();
      }
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : t("Invalid code", "Ungültiger Code"));
    } finally {
      setBusy(false);
    }
  };

  if (stage === "codes") {
    return <BackupCodesReveal codes={backupCodes} onAcknowledge={onDone} />;
  }

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold">
        {t("Get codes by email", "Codes per E-Mail erhalten")}
      </h3>
      {stage === "start" ? (
        <>
          <p className="text-sm text-muted-foreground">
            {t(
              "We send a 6-digit code to your sign-in email. Enter it to confirm the address.",
              "Wir senden einen 6-stelligen Code an deine Anmelde-E-Mail. Gib ihn ein, um die Adresse zu bestätigen.",
            )}
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={sendCode}
            className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {t("Send code", "Code senden")}
          </button>
        </>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            {t(`We sent a code to ${sentTo}.`, `Wir haben einen Code an ${sentTo} gesendet.`)}
          </p>
          <div>
            <label htmlFor="email-enroll-code" className="block text-sm font-medium">
              {t("Enter the 6-digit code", "6-stelligen Code eingeben")}
            </label>
            <input
              id="email-enroll-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              maxLength={6}
              inputMode="numeric"
              autoComplete="one-time-code"
              className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
            />
          </div>
          <button
            type="button"
            disabled={busy || code.length !== 6}
            onClick={confirm}
            className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {busy ? t("Confirming…", "Bestätige…") : t("Confirm", "Bestätigen")}
          </button>
          <ResendButton remaining={cooldown.remaining} busy={busy} onClick={sendCode} />
        </>
      )}
    </div>
  );
}

/** Enrol in one of `methods`, letting the member choose when there are
 *  several. Each method's own component starts its enrolment on its own, so
 *  only the chosen one is mounted. */
export function SecondFactorEnroll({
  token,
  methods,
  onDone,
}: {
  token: string;
  methods: TwoFactorMethod[];
  onDone: () => void;
}) {
  const t = useT();
  const [chosen, setChosen] = useState<TwoFactorMethod | null>(
    methods.length === 1 ? methods[0] : null,
  );
  if (chosen === "totp") return <TotpEnroll token={token} onDone={onDone} />;
  if (chosen === "email") return <EmailEnroll token={token} onDone={onDone} />;
  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold">
        {t("Set up two-factor authentication", "Zwei-Faktor-Authentifizierung einrichten")}
      </h3>
      <p className="text-sm text-muted-foreground">
        {t(
          "Your organisation asks for a second factor. Choose how you want to confirm it is you.",
          "Deine Organisation verlangt einen zweiten Faktor. Wähle, wie du bestätigen möchtest, dass du es bist.",
        )}
      </p>
      <div className="grid gap-2">
        {methods.map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setChosen(m)}
            className="rounded-md border border-border px-3 py-2 text-left text-sm transition hover:border-primary/50"
          >
            {methodLabel(m, t)}
          </button>
        ))}
      </div>
    </div>
  );
}
