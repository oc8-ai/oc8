// frontend/src/components/totp-enroll.tsx
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { BackupCodesReveal } from "@/components/totp-backup-codes";
import { useT } from "@/lib/i18n";
import { confirmTotp, enrollTotp, renderTotpQrDataUrl } from "@/lib/totp";

type Stage = "loading" | "scan" | "codes" | "error";

export function TotpEnroll({ token, onDone }: { token: string; onDone: () => void }) {
  const t = useT();
  const [stage, setStage] = useState<Stage>("loading");
  const [secret, setSecret] = useState("");
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [code, setCode] = useState("");
  const [backupCodes, setBackupCodes] = useState<string[]>([]);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    let cancelled = false;
    enrollTotp(token)
      .then(async (result) => {
        if (cancelled) return;
        setSecret(result.secret);
        setQrDataUrl(await renderTotpQrDataUrl(result.provisioningUri));
        setStage("scan");
      })
      .catch(() => {
        if (!cancelled) setStage("error");
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleConfirm = async () => {
    setConfirming(true);
    try {
      const result = await confirmTotp(token, secret, code);
      setBackupCodes(result.backupCodes);
      setStage("codes");
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : t("Invalid code", "Ungültiger Code"));
    } finally {
      setConfirming(false);
    }
  };

  if (stage === "loading") {
    return <p className="text-sm text-muted-foreground">{t("Loading…", "Wird geladen…")}</p>;
  }
  if (stage === "error") {
    return (
      <p className="text-sm text-destructive">
        {t("Could not start enrollment.", "Einrichtung konnte nicht gestartet werden.")}
      </p>
    );
  }
  if (stage === "codes") {
    return <BackupCodesReveal codes={backupCodes} onAcknowledge={onDone} />;
  }
  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold">
        {t("Scan with your authenticator app", "Mit deiner Authenticator-App scannen")}
      </h3>
      <img src={qrDataUrl} alt={t("QR code", "QR-Code")} className="mx-auto h-40 w-40" />
      <p className="text-center text-xs text-muted-foreground">
        {t("Or enter manually:", "Oder manuell eingeben:")}
      </p>
      <p className="text-center font-mono text-sm">{secret}</p>
      <div>
        <label htmlFor="totp-enroll-code" className="block text-sm font-medium">
          {t("Enter the 6-digit code", "6-stelligen Code eingeben")}
        </label>
        <input
          id="totp-enroll-code"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          maxLength={6}
          className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
        />
      </div>
      <button
        type="button"
        disabled={confirming || code.length !== 6}
        onClick={handleConfirm}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-brand-gradient-foreground hover:brightness-110 disabled:opacity-50"
      >
        {confirming ? t("Confirming…", "Bestätige…") : t("Confirm", "Bestätigen")}
      </button>
    </div>
  );
}
