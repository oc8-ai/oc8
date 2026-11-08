// frontend/src/components/totp-challenge.tsx
import { useState } from "react";
import { toast } from "sonner";
import { useT } from "@/lib/i18n";
import { verifyTotp } from "@/lib/totp";

export function TotpChallenge({
  token,
  onVerified,
}: {
  token: string;
  onVerified: (sessionToken: string) => void;
}) {
  const t = useT();
  const [code, setCode] = useState("");
  const [verifying, setVerifying] = useState(false);

  const handleVerify = async () => {
    setVerifying(true);
    try {
      const result = await verifyTotp(token, code);
      onVerified(result.token);
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : t("Invalid code", "Ungültiger Code"));
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold">
        {t("Enter your authentication code", "Gib deinen Authentifizierungscode ein")}
      </h3>
      <p className="text-sm text-muted-foreground">
        {t(
          "Use your authenticator app, or a backup code.",
          "Verwende deine Authenticator-App oder einen Backup-Code.",
        )}
      </p>
      <div>
        <label htmlFor="totp-challenge-code" className="block text-sm font-medium">
          {t("Code", "Code")}
        </label>
        <input
          id="totp-challenge-code"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
        />
      </div>
      <button
        type="button"
        disabled={verifying || !code}
        onClick={handleVerify}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-brand-gradient-foreground hover:brightness-110 disabled:opacity-50"
      >
        {verifying ? t("Verifying…", "Wird geprüft…") : t("Verify", "Verifizieren")}
      </button>
    </div>
  );
}
