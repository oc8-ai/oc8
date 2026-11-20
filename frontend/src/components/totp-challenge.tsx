// frontend/src/components/totp-challenge.tsx
import { useState } from "react";
import { toast } from "sonner";
import { MethodChoice, ResendButton } from "@/components/second-factor";
import { useResendCooldown } from "@/hooks/use-resend-cooldown";
import { loginMethods } from "@/lib/two-factor";
import { passkeyErrorMessage, signInWithPasskey } from "@/lib/passkey";
import { useT } from "@/lib/i18n";
import { sendEmailCode, verifyEmailCode, verifyTotp, type TwoFactorMethod } from "@/lib/totp";

/** Which input the member is answering with. A backup code goes through the
 *  TOTP verify route, which accepts one whatever the member's method is. */
type Answer = TwoFactorMethod | "backup";

export function TotpChallenge({
  token,
  methods,
  onVerified,
}: {
  token: string;
  /** The login response's `twoFactorMethods`; none means the authenticator app. */
  methods?: string[];
  onVerified: (sessionToken: string) => void;
}) {
  const t = useT();
  const offered = loginMethods(methods);
  const [method, setMethod] = useState<TwoFactorMethod>(offered[0]);
  const [answer, setAnswer] = useState<Answer>(offered[0]);
  const [code, setCode] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [sending, setSending] = useState(false);
  const [sentTo, setSentTo] = useState<string | null>(null);
  const cooldown = useResendCooldown();

  const pick = (next: TwoFactorMethod) => {
    setMethod(next);
    setAnswer(next);
    setCode("");
  };

  const handleSend = async () => {
    setSending(true);
    try {
      const result = await sendEmailCode(token);
      setSentTo(result.sentTo);
      cooldown.start();
    } catch (e: unknown) {
      toast.error(
        e instanceof Error
          ? e.message
          : t("Could not send the code.", "Code konnte nicht gesendet werden."),
      );
    } finally {
      setSending(false);
    }
  };

  const handleVerify = async () => {
    setVerifying(true);
    try {
      const result =
        answer === "email" ? await verifyEmailCode(token, code) : await verifyTotp(token, code);
      onVerified(result.token);
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : t("Invalid code", "Ungültiger Code"));
    } finally {
      setVerifying(false);
    }
  };

  const handlePasskey = async () => {
    setVerifying(true);
    try {
      const result = await signInWithPasskey(token);
      onVerified(result.token);
    } catch (e: unknown) {
      toast.error(passkeyErrorMessage(e, t));
    } finally {
      setVerifying(false);
    }
  };

  const emailWaiting = answer === "email" && sentTo === null;

  let hint: string;
  if (answer === "backup") {
    hint = t(
      "Enter one of the backup codes you saved when you set up two-factor authentication.",
      "Gib einen der Backup-Codes ein, die du bei der Einrichtung gespeichert hast.",
    );
  } else if (answer === "email") {
    hint =
      sentTo === null
        ? t(
            "We send a 6-digit code to your sign-in email.",
            "Wir senden einen 6-stelligen Code an deine Anmelde-E-Mail.",
          )
        : t(
            `We sent a code to ${sentTo}. It is valid for 10 minutes.`,
            `Wir haben einen Code an ${sentTo} gesendet. Er ist 10 Minuten gültig.`,
          );
  } else if (answer === "passkey") {
    hint = t(
      "Use your passkey or security key: your browser asks you to confirm it is you.",
      "Verwende deinen Passkey oder Sicherheitsschlüssel: Dein Browser bittet dich, zu bestätigen, dass du es bist.",
    );
  } else {
    hint = t(
      "Use your authenticator app, or a backup code.",
      "Verwende deine Authenticator-App oder einen Backup-Code.",
    );
  }

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold">
        {answer === "passkey"
          ? t("Confirm your sign-in", "Bestätige deine Anmeldung")
          : t("Enter your authentication code", "Gib deinen Authentifizierungscode ein")}
      </h3>
      <MethodChoice methods={offered} value={method} onChange={pick} />
      <p className="text-sm text-muted-foreground">{hint}</p>
      {answer === "passkey" ? (
        <button
          type="button"
          disabled={verifying}
          onClick={handlePasskey}
          className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {verifying
            ? t("Waiting for the passkey…", "Warte auf den Passkey…")
            : t("Use passkey", "Passkey verwenden")}
        </button>
      ) : emailWaiting ? (
        <button
          type="button"
          disabled={sending}
          onClick={handleSend}
          className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {sending ? t("Sending…", "Wird gesendet…") : t("Send code", "Code senden")}
        </button>
      ) : (
        <>
          <div>
            <label htmlFor="totp-challenge-code" className="block text-sm font-medium">
              {answer === "backup" ? t("Backup code", "Backup-Code") : t("Code", "Code")}
            </label>
            <input
              id="totp-challenge-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoComplete="one-time-code"
              className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
            />
          </div>
          <button
            type="button"
            disabled={verifying || !code}
            onClick={handleVerify}
            className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {verifying ? t("Verifying…", "Wird geprüft…") : t("Verify", "Verifizieren")}
          </button>
        </>
      )}
      {method !== "totp" && (
        // The authenticator input above already takes a backup code; email
        // and passkey need their own way there.
        <div className="flex flex-wrap items-center justify-between gap-2">
          {answer === "email" && sentTo !== null ? (
            <ResendButton remaining={cooldown.remaining} busy={sending} onClick={handleSend} />
          ) : (
            <span />
          )}
          <button
            type="button"
            onClick={() => {
              setAnswer(answer === "backup" ? method : "backup");
              setCode("");
            }}
            className="text-xs text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground"
          >
            {answer !== "backup"
              ? t("Use a backup code", "Backup-Code verwenden")
              : method === "passkey"
                ? t("Use your passkey instead", "Stattdessen deinen Passkey verwenden")
                : t("Use the code by email instead", "Stattdessen den Code per E-Mail verwenden")}
          </button>
        </div>
      )}
    </div>
  );
}
